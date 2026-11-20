"""Hand-calculated public-only books and corruption checks for all four families."""

from __future__ import annotations

import copy
import random
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
from zoneinfo import ZoneInfo

import pytest

from fin_audit.checker import DataValidationError, reconcile
from fin_audit.contracts import evidence_registry


def policy():
    return {
        "schema_version": "0.2.0", "accounting_model": "receivables_multi_v2",
        "accounts": {"receivable": "AR", "revenue": "REVENUE", "cash": "CASH", "unallocated": "UNALLOCATED", "fx_gain": "FX_GAIN", "fx_loss": "FX_LOSS"},
        "allocation_rule": "atomic_full_designation", "invoice_recognition": "issued_at", "payment_recognition": "timestamp",
        "period_bounds": "[start,end)", "rounding": "ROUND_HALF_EVEN", "amount_currency": "base_currency", "amount_tolerance_minor": 0,
        "receipt_lifecycle": "one_atomic_journal_no_later_clearing",
        "unknown_payment_designation": "do_not_infer_an_invoice_from_entity_date_or_amount",
        "fx_date_rule": "source_document_date_in_company_timezone", "fx_rate_convention": "target_major_per_source_major",
        "carrying_rule": "correct_invoice_source_date_rate", "fx_balance_rule": "cash_minus_carrying_minus_unallocated",
        "refund_rule": "native_credit_note_full_designation", "source_amount_rule": "whole_document_amount_per_journal",
        "canonical_rules": {
            "duplicate_invoice": "all_receivable_debit_recognition_row_ids_including_original", "payment_allocation": "payment_id_and_invoice_id",
            "fx_conversion": "primary_ledger_row_id", "period_cutoff": "primary_ledger_row_id",
        },
        "amount_rules": {
            "duplicate_invoice": "excess_gross_recognition_in_base_minor_units", "payment_allocation": "excess_unallocated_receipt_credit_in_observed_valuation",
            "fx_conversion": "absolute_primary_valuation_difference_in_base_minor_units", "period_cutoff": "absolute_primary_amount_in_base_minor_units",
        },
    }


def counts(book):
    values = {key: len(book[key]) for key in ("ledger", "invoices", "payments", "fx")}
    book["manifest"].update(ledger_rows=values["ledger"], document_count=values["invoices"] + values["payments"], table_counts=values)
    return book


def manifest(exponent=2):
    return {
        "schema_version": "0.2.0", "episode_id": "ep_hand", "base_currency": "KZT",
        "period_start": "2026-11-01T00:00:00+05:00", "period_end": "2026-12-01T00:00:00+05:00",
        "timezone": "Asia/Almaty", "rounding": "ROUND_HALF_EVEN", "currency_exponents": {"KZT": 2, "USD": exponent},
    }


def row(identity, journal, account, signed_debit, document, *, payment=False, invoice_id="invoice", posted_at=None, applied_rate="1", applied_date=None, allocated=None):
    source_time = document["timestamp" if payment else "issued_at"]
    day = datetime.fromisoformat(source_time).astimezone(ZoneInfo("Asia/Almaty")).date().isoformat()
    return {
        "row_id": identity, "timestamp": posted_at or source_time, "account": account,
        "debit_minor": max(signed_debit, 0), "credit_minor": max(-signed_debit, 0), "currency": "KZT",
        "entity_id": "customer", "invoice_id": invoice_id, "payment_id": document["payment_id"] if payment else None,
        "journal_id": journal, "source_currency": document["currency"], "source_amount_minor": document["amount_minor" if payment else "total_minor"],
        "applied_fx_date": applied_date or day, "applied_fx_rate": applied_rate, "allocated_minor": allocated,
    }


def invoice(identity="invoice", total=100_000, currency="KZT", issued="2026-11-05T12:00:00+05:00"):
    return {
        "doc_id": "doc_" + identity, "invoice_id": identity, "entity_id": "customer", "issued_at": issued,
        "due_at": (datetime.fromisoformat(issued) + timedelta(days=30)).isoformat(), "currency": currency, "total_minor": total,
        "line_items": [{"description": "Fictional service", "quantity": 1, "unit_price_minor": total, "total_minor": total}],
    }


def foreign_book(*, applied_rate="495", allocated=None, payment_amount=None, exponent=2, payment_time="2026-11-10T12:00:00+05:00", posted_at=None, invoice_rate="500", correct_payment_rate="505", applied_date=None):
    """$1000 invoice, $600 payment; direct arithmetic independent of production helpers."""
    scale = 10**exponent
    invoice_doc = invoice(total=1000 * scale, currency="USD")
    allocated = 500 * scale if allocated is None else allocated
    amount = 600 * scale if payment_amount is None else payment_amount
    payment_doc = {"doc_id": "doc_payment", "payment_id": "payment", "timestamp": payment_time, "currency": "USD", "amount_minor": amount, "invoice_refs": ["invoice"]}
    correct_day = datetime.fromisoformat(payment_time).astimezone(ZoneInfo("Asia/Almaty")).date()
    used_day = applied_date or (correct_day.isoformat() if applied_rate == correct_payment_rate else (correct_day - timedelta(days=1)).isoformat())

    def value(source, rate):
        return int((Decimal(source) * Decimal(rate) * Decimal(10) ** (2 - exponent)).to_integral_value(rounding=ROUND_HALF_EVEN))

    gross = value(invoice_doc["total_minor"], invoice_rate)
    cash, carrying, suspense = value(amount, applied_rate), value(allocated, invoice_rate), value(amount - allocated, applied_rate)
    difference = cash - carrying - suspense
    ledger = [
        row("invoice_ar", "journal_invoice", "AR", gross, invoice_doc, applied_rate=invoice_rate),
        row("invoice_revenue", "journal_invoice", "REVENUE", -gross, invoice_doc, applied_rate=invoice_rate),
    ]
    for identity, account, signed, link in [
        ("payment_cash", "CASH", cash, "invoice"),
        ("payment_ar", "AR", -carrying, "invoice"),
        ("payment_unallocated", "UNALLOCATED", -suspense, None),
        ("payment_fx", "FX_GAIN" if difference > 0 else "FX_LOSS", -difference, "invoice"),
    ]:
        if signed:
            ledger.append(row(identity, "journal_payment", account, signed, payment_doc, payment=True, invoice_id=link, posted_at=posted_at, applied_rate=applied_rate, applied_date=used_day, allocated=allocated))
    fx = [
        {"record_id": "fx_invoice", "date": "2026-11-05", "source_currency": "USD", "target_currency": "KZT", "rate": invoice_rate},
        {"record_id": "fx_payment", "date": correct_day.isoformat(), "source_currency": "USD", "target_currency": "KZT", "rate": correct_payment_rate},
    ]
    if used_day != correct_day.isoformat():
        fx.append({"record_id": "fx_applied", "date": used_day, "source_currency": "USD", "target_currency": "KZT", "rate": applied_rate})
    return counts({"manifest": manifest(exponent), "policy": policy(), "invoices": [invoice_doc], "payments": [payment_doc], "ledger": ledger, "fx": fx})


def native_unpaid(*, issued="2026-11-05T12:00:00+05:00", posted_at=None):
    doc = invoice(issued=issued)
    registry = manifest()
    registry["currency_exponents"] = {"KZT": 2}
    return counts({"manifest": registry, "policy": policy(), "invoices": [doc], "payments": [], "fx": [], "ledger": [
        row("invoice_ar", "journal_invoice", "AR", 100_000, doc, posted_at=posted_at),
        row("invoice_revenue", "journal_invoice", "REVENUE", -100_000, doc, posted_at=posted_at),
    ]})


def refund_book():
    book = native_unpaid()
    original = book["invoices"][0]
    credit = invoice("credit_note", -25_000, issued="2026-11-12T12:00:00+05:00")
    credit["original_invoice_id"] = "invoice"
    book["invoices"].append(credit)
    for identity, source, amount, timestamp in [
        ("original_payment", original, 100_000, "2026-11-10T12:00:00+05:00"),
        ("refund", credit, -25_000, "2026-11-13T12:00:00+05:00"),
    ]:
        payment_doc = {"doc_id": "doc_" + identity, "payment_id": identity, "timestamp": timestamp, "currency": "KZT", "amount_minor": amount, "invoice_refs": [source["invoice_id"]]}
        book["payments"].append(payment_doc)
        book["ledger"].extend([
            row(identity + "_cash", "journal_" + identity, "CASH", amount, payment_doc, payment=True, invoice_id=source["invoice_id"], allocated=amount),
            row(identity + "_ar", "journal_" + identity, "AR", -amount, payment_doc, payment=True, invoice_id=source["invoice_id"], allocated=amount),
        ])
    book["ledger"].extend([
        row("credit_ar", "journal_credit", "AR", -25_000, credit, invoice_id="credit_note"),
        row("credit_revenue", "journal_credit", "REVENUE", 25_000, credit, invoice_id="credit_note"),
    ])
    return counts(book)


def test_hand_calculated_allocation_fx_interaction_and_complete_proofs():
    book = foreign_book()
    issues = {issue["type"]: issue for issue in reconcile(book)}
    assert set(issues) == {"payment_allocation", "fx_conversion"}
    assert issues["payment_allocation"]["expected_amount_minor"] == 4_950_000
    assert issues["fx_conversion"]["expected_amount_minor"] == 600_000
    assert issues["fx_conversion"]["affected_ids"] == ["payment_cash"]
    assert issues["fx_conversion"]["evidence_sets"] == [["doc_payment", "fx_payment", "payment_cash", "policy"]]
    assert set(issues["payment_allocation"]["evidence_sets"][0]) == {"doc_invoice", "doc_payment", "payment_cash", "payment_ar", "payment_fx", "payment_unallocated", "policy"}
    fixed = {issue["type"]: issue for issue in reconcile(foreign_book(applied_rate="505"))}
    assert set(fixed) == {"payment_allocation"}
    assert fixed["payment_allocation"]["expected_amount_minor"] == 5_050_000


@pytest.mark.parametrize("exponent", [0, 2, 3])
def test_source_exponents_do_not_change_economic_amount(exponent):
    found = {issue["type"]: issue["expected_amount_minor"] for issue in reconcile(foreign_book(exponent=exponent))}
    assert found == {"fx_conversion": 600_000, "payment_allocation": 4_950_000}


def test_legitimate_partial_payment_and_realized_fx_difference_are_clean():
    assert reconcile(foreign_book(applied_rate="505", allocated=60_000)) == []


def test_wrong_numeric_rate_need_not_equal_a_published_rate_to_be_detected():
    book = foreign_book(applied_rate="512.25", applied_date="2026-11-10", allocated=60_000)
    issue, = reconcile(book)
    assert issue["type"] == "fx_conversion"
    assert issue["expected_amount_minor"] == 435_000


def test_wrong_rate_with_zero_rounded_effect_is_not_a_truth_issue():
    assert reconcile(foreign_book(applied_rate="505.000000001", applied_date="2026-11-10", allocated=60_000)) == []


def test_zero_base_effect_of_unallocated_source_cent_is_not_a_truth_issue():
    book = foreign_book(applied_rate="0.01", invoice_rate="0.01", correct_payment_rate="0.01", payment_amount=10_001, allocated=10_000)
    assert reconcile(book) == []


def test_allocation_cutoff_interaction_uses_source_period_not_utc_date():
    book = foreign_book(applied_rate="505", payment_time="2026-11-30T18:30:00+00:00", posted_at="2026-12-01T00:30:00+05:00")
    issues = {issue["type"]: issue for issue in reconcile(book)}
    assert set(issues) == {"payment_allocation", "period_cutoff"}
    assert issues["period_cutoff"]["affected_ids"] == ["payment_cash"]
    assert issues["period_cutoff"]["expected_amount_minor"] == 30_300_000
    assert issues["period_cutoff"]["evidence_sets"] == [["doc_payment", "payment_cash", "policy"]]
    assert issues["payment_allocation"]["expected_amount_minor"] == 5_050_000


@pytest.mark.parametrize(
    "issued,posted,expected_cutoff",
    [
        ("2026-12-01T00:00:00+05:00", "2026-11-30T23:59:59+05:00", True),
        ("2026-12-01T00:00:00+05:00", "2026-11-30T19:00:00+00:00", False),
        ("2026-11-01T00:00:00+05:00", "2026-10-31T23:59:59+05:00", True),
        ("2026-11-01T00:00:00+05:00", "2026-10-31T19:00:00+00:00", False),
        ("2026-11-05T12:00:00+05:00", "2026-11-06T12:00:00+05:00", False),
    ],
)
def test_period_membership_boundaries_and_equivalent_offsets(issued, posted, expected_cutoff):
    issues = reconcile(native_unpaid(issued=issued, posted_at=posted))
    assert [issue["type"] for issue in issues] == (["period_cutoff"] if expected_cutoff else [])


def test_repeated_recognition_is_derived_blind_and_distinct_documents_are_clean():
    book = native_unpaid()
    copied = copy.deepcopy(book["ledger"])
    for entry in copied:
        entry["row_id"] += "_copy"
        entry["journal_id"] = "journal_copy"
    book["ledger"].extend(copied)
    issue, = reconcile(counts(book))
    assert issue["type"] == "duplicate_invoice"
    assert issue["affected_ids"] == ["invoice_ar", "invoice_ar_copy"]
    assert issue["expected_amount_minor"] == 100_000
    second = copy.deepcopy(book["invoices"][0])
    second.update(invoice_id="invoice_two", doc_id="doc_invoice_two")
    book["invoices"].append(second)
    for entry in copied:
        entry["invoice_id"] = "invoice_two"
    assert reconcile(counts(book)) == []


def test_unknown_source_designation_is_never_guessed_from_recorded_allocation():
    book = foreign_book(applied_rate="505")
    book["payments"][0]["invoice_refs"] = None
    assert reconcile(book) == []


def test_signed_documented_native_credit_and_refund_are_clean():
    assert reconcile(refund_book()) == []


def test_native_refund_cutoff_uses_outgoing_cash_primary():
    book = refund_book()
    for entry in book["ledger"]:
        if entry["payment_id"] == "refund":
            entry["timestamp"] = "2026-12-01T12:00:00+05:00"
    issue, = reconcile(book)
    assert issue["type"] == "period_cutoff"
    assert issue["affected_ids"] == ["refund_cash"]
    assert issue["expected_amount_minor"] == 25_000


def test_missing_public_fx_evidence_is_rejected_instead_of_a_partial_answer():
    book = foreign_book()
    book["fx"] = [record for record in book["fx"] if record["record_id"] != "fx_payment"]
    with pytest.raises(DataValidationError, match="missing_fx_rate"):
        reconcile(counts(book))


@pytest.mark.parametrize("field,value", [("source_currency", "KZT"), ("source_amount_minor", 60001), ("allocated_minor", None), ("applied_fx_date", "2099-01-01")])
def test_unexplained_observed_metadata_is_not_silently_accepted(field, value):
    book = foreign_book()
    for entry in book["ledger"]:
        if entry["payment_id"] == "payment":
            entry[field] = value
    with pytest.raises(DataValidationError):
        reconcile(book)


def test_balanced_but_unmodeled_counterparts_are_rejected():
    book = foreign_book(applied_rate="505", allocated=60_000)
    primary = next(entry for entry in book["ledger"] if entry["row_id"] == "payment_cash")
    for identity, account, debit, credit in [("unexpected_gain", "FX_GAIN", 0, 1), ("unexpected_loss", "FX_LOSS", 1, 0)]:
        copied = dict(primary, row_id=identity, account=account, debit_minor=debit, credit_minor=credit)
        book["ledger"].append(copied)
    with pytest.raises(DataValidationError, match="receipt_counterpart_mismatch"):
        reconcile(counts(book))


def test_balanced_wrong_value_without_observed_rate_support_is_rejected():
    book = native_unpaid()
    book["ledger"][0]["debit_minor"] += 1
    book["ledger"][1]["credit_minor"] += 1
    with pytest.raises(DataValidationError, match="observed_valuation_metadata_mismatch"):
        reconcile(book)


@pytest.mark.parametrize("case", ["timestamp", "metadata", "entity", "duplicate_fx", "unknown_credit", "refund_allocation"])
def test_broken_sibling_or_source_relationships_fail_closed(case):
    book = refund_book() if case in {"unknown_credit", "refund_allocation"} else foreign_book()
    if case == "timestamp":
        book["ledger"][-1]["timestamp"] = "2026-11-11T12:00:00+05:00"
    elif case == "metadata":
        book["ledger"][-1]["allocated_minor"] = 1
    elif case == "entity":
        book["ledger"][-1]["entity_id"] = "different_customer"
    elif case == "duplicate_fx":
        book["fx"].append(dict(book["fx"][0], record_id="new_fx_id"))
    elif case == "unknown_credit":
        book["invoices"][-1]["original_invoice_id"] = "missing_original"
    elif case == "refund_allocation":
        for entry in book["ledger"]:
            if entry["payment_id"] == "refund":
                entry["allocated_minor"] += 1
    with pytest.raises(DataValidationError):
        reconcile(counts(book))


def test_table_order_and_consistent_id_renaming_preserve_meaning_and_proof_validity():
    book = foreign_book()
    expected = reconcile(book)
    randomizer = random.Random(1701)
    for name in ("ledger", "invoices", "payments", "fx"):
        randomizer.shuffle(book[name])
    assert reconcile(book) == expected
    registry = evidence_registry(book)
    for issue in expected:
        assert 0 < len(issue["evidence_sets"][0]) <= 10
        assert set(issue["evidence_sets"][0]) <= registry
    identities = {book["manifest"]["episode_id"]}
    for table in ("invoices", "payments", "ledger", "fx"):
        for record in book[table]:
            for key in ("doc_id", "invoice_id", "payment_id", "entity_id", "row_id", "record_id", "journal_id"):
                value = record.get(key)
                if value is not None:
                    identities.add(value)
    mapping = {value: f"opaque_{index:03}" for index, value in enumerate(reversed(sorted(identities)))}

    def renamed(value):
        if isinstance(value, str):
            return mapping.get(value, value)
        if isinstance(value, list):
            return [renamed(item) for item in value]
        if isinstance(value, dict):
            return {key: renamed(item) for key, item in value.items()}
        return value

    actual = reconcile(renamed(book))
    for original, transformed in zip(expected, actual, strict=True):
        assert transformed["type"] == original["type"]
        assert transformed["expected_amount_minor"] == original["expected_amount_minor"]
        assert transformed["affected_ids"] == sorted(mapping[identity] for identity in original["affected_ids"])
        assert transformed["evidence_sets"] == [sorted(mapping.get(identity, identity) for identity in original["evidence_sets"][0])]


def test_journal_id_collision_with_a_later_record_rejects_every_permutation():
    book = foreign_book()
    for entry in book["ledger"]:
        if entry["journal_id"] == "journal_invoice":
            entry["journal_id"] = "payment_cash"
    for order in (book["ledger"], list(reversed(book["ledger"]))):
        candidate = copy.deepcopy(book)
        candidate["ledger"] = copy.deepcopy(order)
        with pytest.raises(DataValidationError, match="journal_record_identifier_collision"):
            reconcile(candidate)


@pytest.mark.parametrize("table", ["manifest", "policy", "ledger", "invoices", "payments", "fx"])
def test_reserved_private_fields_cannot_bypass_blind_checker_or_storage(table):
    book = foreign_book()
    target = book[table] if isinstance(book[table], dict) else book[table][0]
    target["truth"] = {"seed": 19}
    with pytest.raises(DataValidationError, match="reserved_private_public_key"):
        reconcile(book)


def test_historical_native_schema_remains_supported():
    from fin_audit.generator import generate

    source = generate(19, {"invoice_count": 3, "ledger_rows": None})
    historical = copy.deepcopy(source.public)
    historical["manifest"]["schema_version"] = "0.1.0"
    historical["policy"]["schema_version"] = "0.1.0"
    assert reconcile(historical) == source.truth


@pytest.mark.parametrize("seed", range(24))
def test_complete_generated_combination_and_clean_controls_for_independent_seeds(seed):
    from fin_audit.multi_generator import generate_multi

    config = {"fx_count": 1, "cutoff_count": 1, "allocation_count": 2, "interaction_allocation_fx": True, "interaction_allocation_cutoff": True, "refund_count": 2}
    generated = generate_multi(seed, config)
    assert reconcile(generated.public) == generated.truth
    assert {issue["type"] for issue in generated.truth} == {"duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"}
    assert all(issue["expected_amount_minor"] > 0 for issue in generated.truth)
    clean = generate_multi(seed, {"fx_count": 0, "cutoff_count": 0, "duplicate_count": 0, "allocation_count": 0, "refund_count": 2})
    assert reconcile(clean.public) == []
