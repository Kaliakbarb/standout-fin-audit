"""Hand-calculated accounting cases and public-only generation invariants."""

from __future__ import annotations

import copy
import json
import random

import pytest

from fin_audit.checker import DataValidationError, reconcile
from fin_audit.contracts import SCHEMA_VERSION, canonical_json, evidence_registry
from fin_audit.generator import GenerationError, generate
from fin_audit.money import convert_minor, minor_integer, split_minor


def _counts(public):
    counts = {name: len(public[name]) for name in ("ledger", "invoices", "payments", "fx")}
    public["manifest"].update(
        ledger_rows=counts["ledger"],
        document_count=counts["invoices"] + counts["payments"],
        table_counts=counts,
    )


def _hand_book(*, missing_allocation=True):
    """Invoice100000 KZT, real partial payment60000, recorded allocation50000."""
    invoice_time = "2026-11-05T12:00:00+05:00"
    payment_time = "2026-11-10T12:00:00+05:00"
    policy = {
        "schema_version": SCHEMA_VERSION,
        "accounting_model": "receivables_native_v1",
        "accounts": {"receivable": "AR", "revenue": "REVENUE", "cash": "CASH", "unallocated": "UNALLOCATED"},
        "allocation_rule": "atomic_full_designation",
        "invoice_recognition": "issued_at", "payment_recognition": "timestamp",
        "period_bounds": "[start,end)", "rounding": "ROUND_HALF_EVEN",
        "amount_currency": "base_currency", "amount_tolerance_minor": 0,
        "canonical_rules": {
            "duplicate_invoice": "all_receivable_debit_recognition_row_ids_including_original",
            "payment_allocation": "payment_id_and_invoice_id",
        },
        "amount_rules": {
            "duplicate_invoice": "excess_gross_recognition_in_base_minor_units",
            "payment_allocation": "excess_unallocated_receipt_credit_in_observed_valuation",
        },
        "unknown_payment_designation": "do_not_infer_an_invoice_from_entity_date_or_amount",
        "receipt_lifecycle": "one_atomic_journal_no_later_clearing",
    }

    def row(identity, account, debit, credit, payment_id=None):
        return {
            "row_id": identity, "timestamp": payment_time if payment_id else invoice_time,
            "account": account, "debit_minor": debit, "credit_minor": credit,
            "currency": "KZT", "entity_id": "customer",
            "invoice_id": None if account == "UNALLOCATED" else "invoice",
            "payment_id": payment_id,
        }

    public = {
        "manifest": {
            "schema_version": SCHEMA_VERSION, "episode_id": "episode",
            "base_currency": "KZT", "currency_exponents": {"KZT": 2},
            "period_start": "2026-11-01T00:00:00+05:00",
            "period_end": "2026-12-01T00:00:00+05:00",
            "timezone": "Asia/Almaty", "rounding": "ROUND_HALF_EVEN",
        },
        "policy": policy,
        "invoices": [{
            "doc_id": "invoice_doc", "invoice_id": "invoice", "entity_id": "customer",
            "issued_at": invoice_time, "due_at": "2026-12-05T12:00:00+05:00",
            "currency": "KZT", "total_minor": 10_000_000,
            "line_items": [{"description": "Поставка", "quantity": 1, "unit_price_minor": 10_000_000, "total_minor": 10_000_000}],
        }],
        "payments": [{
            "doc_id": "payment_doc", "payment_id": "payment", "timestamp": payment_time,
            "currency": "KZT", "amount_minor": 6_000_000, "invoice_refs": ["invoice"],
        }],
        "ledger": [
            row("recognition", "AR", 10_000_000, 0),
            row("revenue", "REVENUE", 0, 10_000_000),
            row("cash", "CASH", 6_000_000, 0, "payment"),
            row("allocation", "AR", 0, 5_000_000 if missing_allocation else 6_000_000, "payment"),
        ],
        "fx": [],
    }
    if missing_allocation:
        public["ledger"].append(row("suspense", "UNALLOCATED", 0, 1_000_000, "payment"))
    _counts(public)
    return public


def test_hand_calculated_allocation_is_10000_not_unpaid_40000_or_50000():
    public = _hand_book()
    issues = reconcile(public)
    assert len(issues) == 1
    issue = issues[0]
    assert issue["type"] == "payment_allocation"
    assert issue["expected_amount_minor"] == 1_000_000
    assert issue["currency"] == "KZT"
    assert issue["affected_ids"] == ["invoice", "payment"]
    assert issue["evidence_sets"] == [["allocation", "cash", "invoice_doc", "payment_doc", "policy", "suspense"]]


def test_real_partial_payment_and_remaining_debt_are_clean():
    assert reconcile(_hand_book(missing_allocation=False)) == []


def test_fully_missing_allocation_keeps_cash_and_is_provable():
    public = _hand_book()
    public["ledger"] = [row for row in public["ledger"] if row["row_id"] != "allocation"]
    next(row for row in public["ledger"] if row["row_id"] == "suspense")["credit_minor"] = 6_000_000
    _counts(public)
    issue, = reconcile(public)
    assert issue["expected_amount_minor"] == 6_000_000
    assert set(issue["evidence_sets"][0]) == {"policy", "invoice_doc", "payment_doc", "cash", "suspense"}


def test_blind_checker_finds_duplicate_without_provenance_or_expected_truth():
    public = _hand_book(missing_allocation=False)
    for identity in ("recognition", "revenue"):
        copied = copy.deepcopy(next(row for row in public["ledger"] if row["row_id"] == identity))
        copied["row_id"] = identity + "_copy"
        public["ledger"].append(copied)
    _counts(public)
    issue, = reconcile(public)
    assert issue["type"] == "duplicate_invoice"
    assert issue["expected_amount_minor"] == 10_000_000
    assert issue["affected_ids"] == ["recognition", "recognition_copy"]
    assert set(issue["evidence_sets"][0]) == {"policy", "invoice_doc", "recognition", "recognition_copy"}


def test_same_sum_on_distinct_invoices_is_not_a_duplicate():
    public = _hand_book(missing_allocation=False)
    invoice = copy.deepcopy(public["invoices"][0])
    invoice.update(doc_id="second_invoice_doc", invoice_id="second_invoice")
    public["invoices"].append(invoice)
    for identity in ("recognition", "revenue"):
        copied = copy.deepcopy(next(row for row in public["ledger"] if row["row_id"] == identity))
        copied.update(row_id=identity + "_second", invoice_id="second_invoice")
        public["ledger"].append(copied)
    _counts(public)
    assert reconcile(public) == []


def test_null_source_reference_never_invents_allocation_truth():
    public = _hand_book()
    public["payments"][0]["invoice_refs"] = None
    assert reconcile(public) == []


def test_unknown_reference_is_not_guessed_from_matching_amount_and_entity():
    public = _hand_book()
    public["payments"][0]["invoice_refs"] = ["not_a_real_invoice"]
    with pytest.raises(DataValidationError, match="unknown_invoice_reference"):
        reconcile(public)


def test_reconciler_rejects_unexplained_balanced_amount_corruption():
    public = _hand_book(missing_allocation=False)
    public["ledger"][0]["debit_minor"] += 100
    public["ledger"][1]["credit_minor"] += 100
    with pytest.raises(DataValidationError, match="recognition_amount_mismatch"):
        reconcile(public)


def test_reconciler_rejects_unbalanced_books_instead_of_returning_known_issues():
    public = _hand_book()
    public["ledger"][0]["debit_minor"] += 1
    with pytest.raises(DataValidationError, match="unbalanced_general_ledger"):
        reconcile(public)


@pytest.mark.parametrize("value", [True, 10.0, "100", None, -1])
def test_invalid_ledger_minor_units_are_rejected(value):
    public = _hand_book()
    public["ledger"][0]["debit_minor"] = value
    with pytest.raises(DataValidationError):
        reconcile(public)


def test_checker_rejects_private_policy_override():
    public = _hand_book()
    override = copy.deepcopy(public["policy"])
    override["allocation_rule"] = "ignore_receipts"
    with pytest.raises(DataValidationError, match="policy_does_not_match_public"):
        reconcile(public, override)


def test_stage1_does_not_claim_to_validate_foreign_currency():
    public = _hand_book()
    public["payments"][0]["currency"] = "USD"
    with pytest.raises(DataValidationError, match="unsupported_source_currency"):
        reconcile(public)


def test_equivalent_timestamp_offset_does_not_change_issues():
    public = _hand_book()
    expected = reconcile(public)
    public["invoices"][0]["issued_at"] = "2026-11-05T07:00:00+00:00"
    assert reconcile(public) == expected


def test_naive_timestamp_is_rejected():
    public = _hand_book()
    public["payments"][0]["timestamp"] = "2026-11-10T12:00:00"
    with pytest.raises(DataValidationError, match="naive_timestamp"):
        reconcile(public)


@pytest.mark.parametrize("seed", [0, 1, 19, 991])
def test_default_generation_has_valid_proof_budgets_and_balanced_books(seed):
    bundle = generate(seed)
    public = bundle.public
    assert 200 <= len(public["ledger"]) <= 2000
    assert 20 <= len(public["invoices"]) + len(public["payments"]) <= 200
    assert sum(row["debit_minor"] for row in public["ledger"]) == sum(row["credit_minor"] for row in public["ledger"])
    assert {issue["type"] for issue in bundle.truth} == {"duplicate_invoice", "payment_allocation"}
    registry = evidence_registry(public)
    for issue in bundle.truth:
        assert issue["expected_amount_minor"] > 0
        assert all(0 < len(evidence) <= 10 and set(evidence) <= registry for evidence in issue["evidence_sets"])
    assert reconcile(public) == bundle.truth


def test_generation_is_exactly_repeatable_and_returns_isolated_objects():
    first, second = generate(2026), generate(2026)
    assert canonical_json(first.public) == canonical_json(second.public)
    assert first.truth == second.truth
    assert first.provenance == second.provenance
    first.public["ledger"][0]["debit_minor"] = -999
    assert all(row["debit_minor"] >= 0 for row in second.public["ledger"])


def test_toggling_error_counts_preserves_source_world_and_documents():
    clean = generate(2026, {"duplicate_count": 0, "allocation_count": 0})
    mutated = generate(2026)
    assert clean.truth == []
    assert clean.provenance["clean_public_sha256"] == mutated.provenance["clean_public_sha256"]
    for table, identity in (("invoices", "invoice_id"), ("payments", "payment_id")):
        assert sorted(clean.public[table], key=lambda row: row[identity]) == sorted(mutated.public[table], key=lambda row: row[identity])


def test_generated_clean_cases_include_repeats_split_payments_and_true_partial_payment():
    public = generate(77, {"duplicate_count": 0, "allocation_count": 0}).public
    amounts = [invoice["total_minor"] for invoice in public["invoices"]]
    assert len(set(amounts)) < len(amounts)
    paid = {}
    receipt_counts = {}
    for payment in public["payments"]:
        invoice_id, = payment["invoice_refs"]
        paid[invoice_id] = paid.get(invoice_id, 0) + payment["amount_minor"]
        receipt_counts[invoice_id] = receipt_counts.get(invoice_id, 0) + 1
    assert any(count > 1 for count in receipt_counts.values())
    assert any(0 < paid.get(invoice["invoice_id"], 0) < invoice["total_minor"] for invoice in public["invoices"])
    assert any(paid.get(invoice["invoice_id"], 0) == 0 for invoice in public["invoices"])
    assert reconcile(public) == []


def test_table_permutation_does_not_change_derived_issues_or_proofs():
    public = generate(81).public
    expected = reconcile(public)
    randomizer = random.Random(72)
    for name in ("ledger", "invoices", "payments"):
        randomizer.shuffle(public[name])
    assert reconcile(public) == expected


def test_public_snapshot_contains_no_private_generation_fields():
    bundle = generate(19)

    def all_keys(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key
                yield from all_keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from all_keys(item)

    assert not {"seed", "truth", "provenance", "duplicate_count", "allocation_count", "issue_id", "is_error"} & set(all_keys(bundle.public))
    assert json.loads(canonical_json(bundle.public)) == bundle.public


@pytest.mark.parametrize("mode", ["partial", "missing"])
def test_each_error_family_is_independently_configurable(mode):
    duplicate = generate(5, {"invoice_count": 3, "ledger_rows": None, "duplicate_count": 1, "allocation_count": 0})
    allocation = generate(5, {"invoice_count": 3, "ledger_rows": None, "duplicate_count": 0, "allocation_count": 1, "allocation_mode": mode})
    assert [issue["type"] for issue in duplicate.truth] == ["duplicate_invoice"]
    assert [issue["type"] for issue in allocation.truth] == ["payment_allocation"]


def test_maximum_clean_profile_stays_within_public_document_and_ledger_budgets():
    bundle = generate(101, {"invoice_count": 100, "ledger_rows": 2000, "duplicate_count": 0, "allocation_count": 0})
    assert len(bundle.public["ledger"]) == 2000
    assert bundle.public["manifest"]["document_count"] <= 200
    assert bundle.truth == []


@pytest.mark.parametrize("config", [
    {"invoice_count": True}, {"invoice_count": 2}, {"duplicate_count": 13},
    {"allocation_count": -1}, {"ledger_rows": 3.5}, {"ledger_rows": 10},
    {"unsupported_count": 1}, {"period_start": "2026-11-01T00:00:00"},
    {"timezone": "Not/AZone"}, {"allocation_mode": "anything"},
    {"invoice_count": 3, "ledger_rows": None, "duplicate_count": 0, "allocation_count": 4},
    {"ledger_rows": 2000},
])
def test_impossible_or_unsupported_configurations_fail_explicitly(config):
    with pytest.raises(GenerationError):
        generate(1, config)


@pytest.mark.parametrize("value", [True, False, 100.0, "100", None])
def test_money_primitive_rejects_implicit_conversion(value):
    with pytest.raises(ValueError):
        minor_integer(value)


def test_exact_currency_conversion_uses_exponents_and_one_rounding():
    assert convert_minor(125, "1", 2, 1) == 12
    assert convert_minor(125, "1", 2, 1, rounding="ROUND_HALF_UP") == 13
    assert convert_minor(1000, "500", 0, 2) == 50_000_000
    assert convert_minor(-125, "1", 2, 1) == -12
    huge = 10**60 + 1
    assert convert_minor(huge, "1.000", 2, 2) == huge
    assert split_minor(101, 3) == [34, 34, 33]


@pytest.mark.parametrize("rate", [1.5, "NaN", "Infinity", "1e3", "0", "-1", "1/2"])
def test_fx_primitive_rejects_unsafe_or_ambiguous_rate_formats(rate):
    with pytest.raises(ValueError):
        convert_minor(100, rate, 2, 2)
