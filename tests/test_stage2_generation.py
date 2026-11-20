"""Public, hand-calculated checks of the full four-family synthetic model."""

from __future__ import annotations

import copy
import math
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from fin_audit.checker import reconcile
from fin_audit.contracts import canonical_json, evidence_registry
from fin_audit.generator import GenerationError, generate

BASE = {
    "foreign_invoice_count": 6, "refund_count": 1,
    "duplicate_count": 0, "allocation_count": 0, "fx_count": 0, "cutoff_count": 0,
}


def _multi(seed=19, **config):
    return generate(seed, {**BASE, **config})


def _counts(public):
    counts = {name: len(public[name]) for name in ("ledger", "invoices", "payments", "fx")}
    public["manifest"].update(ledger_rows=counts["ledger"], document_count=counts["invoices"] + counts["payments"], table_counts=counts)


@pytest.mark.parametrize("seed", [0, 1, 19, 991])
def test_four_families_have_independent_proofs_and_balanced_journals(seed):
    bundle = _multi(seed, duplicate_count=1, allocation_count=1, fx_count=1, cutoff_count=1)
    public = bundle.public
    assert Counter(issue["type"] for issue in bundle.truth) == {
        "duplicate_invoice": 1, "payment_allocation": 1, "fx_conversion": 1, "period_cutoff": 1,
    }
    journals = defaultdict(list)
    for row in public["ledger"]:
        journals[row["journal_id"]].append(row)
        assert row["currency"] == "KZT"
        assert type(row["debit_minor"]) is int and type(row["credit_minor"]) is int
        assert row["debit_minor"] >= 0 and row["credit_minor"] >= 0
    for rows in journals.values():
        assert sum(row["debit_minor"] for row in rows) == sum(row["credit_minor"] for row in rows)
        for key in ("timestamp", "source_currency", "source_amount_minor", "applied_fx_date", "applied_fx_rate", "allocated_minor"):
            assert len({row[key] for row in rows}) == 1
    registry = evidence_registry(public)
    assert all(set(proof) <= registry and len(proof) <= 10 for issue in bundle.truth for proof in issue["evidence_sets"])
    assert reconcile(public) == bundle.truth


def test_clean_world_has_foreign_gains_partial_receipts_refund_and_boundary_controls():
    bundle = _multi()
    public = bundle.public
    assert bundle.truth == []
    assert any(row["account"] == "FX_GAIN" for row in public["ledger"])
    assert any(payment["amount_minor"] < 0 for payment in public["payments"])
    zone = ZoneInfo(public["manifest"]["timezone"])
    start, end = (datetime.fromisoformat(public["manifest"][key]) for key in ("period_start", "period_end"))
    assert any(datetime.fromisoformat(invoice["issued_at"]) < start for invoice in public["invoices"])
    assert any(datetime.fromisoformat(payment["timestamp"]).date() != datetime.fromisoformat(payment["timestamp"]).astimezone(zone).date() for payment in public["payments"])
    assert any(0 < (end - datetime.fromisoformat(payment["timestamp"])).total_seconds() <= 1800 for payment in public["payments"])
    assert any(Decimal(row["rate"]) % 1 for row in public["fx"])


def test_refund_is_documented_and_uses_two_clean_balanced_reverse_journals():
    public = _multi(refund_count=3).public
    originals = {invoice["invoice_id"]: invoice for invoice in public["invoices"]}
    credit_notes = [invoice for invoice in public["invoices"] if invoice["total_minor"] < 0]
    assert len(credit_notes) == 3
    for note in credit_notes:
        original = originals[note["original_invoice_id"]]
        assert original["currency"] == note["currency"] == "KZT"
        assert original["entity_id"] == note["entity_id"]
        assert all(line["total_minor"] < 0 and line["quantity"] > 0 for line in note["line_items"])
        refund, = [payment for payment in public["payments"] if payment["invoice_refs"] == [note["invoice_id"]]]
        assert refund["amount_minor"] == note["total_minor"]
        journal = [row for row in public["ledger"] if row["payment_id"] == refund["payment_id"]]
        assert {row["account"] for row in journal} == {"AR", "CASH"}
        assert next(row for row in journal if row["account"] == "AR")["debit_minor"] == -note["total_minor"]
        assert next(row for row in journal if row["account"] == "CASH")["credit_minor"] == -note["total_minor"]
    assert reconcile(public) == []


def test_allocation_fx_hand_amounts_change_when_only_exchange_rate_is_repaired():
    public = _multi(allocation_count=1, fx_count=1, interaction_allocation_fx=True).public
    issues = reconcile(public)
    allocation = next(issue for issue in issues if issue["type"] == "payment_allocation")
    fx_issue = next(issue for issue in issues if issue["type"] == "fx_conversion")
    # $600 received, $500 allocated. Previous day's wrong rate is499.25;
    # correct payment rate500.00. Missing$100 is currently valued at49925KZT;
    # the cash valuation error is$600*0.75=450KZT.
    assert allocation["expected_amount_minor"] == 4_992_500
    assert fx_issue["expected_amount_minor"] == 45_000
    primary = next(row for row in public["ledger"] if row["row_id"] == fx_issue["affected_ids"][0])
    journal = [row for row in public["ledger"] if row["journal_id"] == primary["journal_id"]]
    payment = next(payment for payment in public["payments"] if payment["payment_id"] == primary["payment_id"])
    assert payment["amount_minor"] == 60_000
    assert primary["allocated_minor"] == 50_000
    assert next(row for row in journal if row["account"] == "AR")["credit_minor"] == 24_962_500
    for row in journal:
        row["applied_fx_rate"] = "500.00"
        row["applied_fx_date"] = "2026-11-01"
        if row["account"] == "CASH":
            row["debit_minor"] = 30_000_000
        elif row["account"] == "UNALLOCATED":
            row["credit_minor"] = 5_000_000
    balancing = copy.deepcopy(primary)
    balancing.update(row_id="independent_repair_fx_gain", account="FX_GAIN", debit_minor=0, credit_minor=37_500)
    public["ledger"].append(balancing)
    _counts(public)
    repaired_issue, = reconcile(public)
    assert repaired_issue["type"] == "payment_allocation"
    assert repaired_issue["expected_amount_minor"] == 5_000_000


def test_cutoff_and_allocation_share_event_and_repair_keeps_allocation_visible():
    public = _multi(allocation_count=1, cutoff_count=1, interaction_allocation_cutoff=True).public
    allocation = next(issue for issue in reconcile(public) if issue["type"] == "payment_allocation")
    cutoff = next(issue for issue in reconcile(public) if issue["type"] == "period_cutoff")
    primary = next(row for row in public["ledger"] if row["row_id"] == cutoff["affected_ids"][0])
    assert primary["payment_id"] in allocation["affected_ids"]
    payment = next(doc for doc in public["payments"] if doc["payment_id"] == primary["payment_id"])
    assert datetime.fromisoformat(primary["timestamp"]) >= datetime.fromisoformat(public["manifest"]["period_end"])
    expected_amount = allocation["expected_amount_minor"]
    for row in public["ledger"]:
        if row["journal_id"] == primary["journal_id"]:
            row["timestamp"] = payment["timestamp"]
    remaining, = reconcile(public)
    assert remaining["type"] == "payment_allocation"
    assert remaining["expected_amount_minor"] == expected_amount


@pytest.mark.parametrize("mode", ["partial", "missing"])
def test_both_interactions_coexist_on_distinct_receipts(mode):
    bundle = _multi(allocation_count=2, fx_count=1, cutoff_count=1,
                    interaction_allocation_fx=True, interaction_allocation_cutoff=True,
                    allocation_mode=mode)
    assert Counter(issue["type"] for issue in bundle.truth) == {
        "payment_allocation": 2, "fx_conversion": 1, "period_cutoff": 1,
    }
    assert all(len(proof) <= 10 for issue in bundle.truth for proof in issue["evidence_sets"])


def test_boundary_interaction_reserves_its_target_before_random_fx_selection():
    for seed in range(20):
        bundle = _multi(seed, allocation_count=1, fx_count=1, cutoff_count=1,
                        interaction_allocation_cutoff=True)
        assert Counter(issue["type"] for issue in bundle.truth) == {
            "payment_allocation": 1, "fx_conversion": 1, "period_cutoff": 1,
        }


@pytest.mark.parametrize("currency,exponent", [("JPY", 0), ("USD", 2), ("KWD", 3)])
@pytest.mark.parametrize("mode", ["date", "rate"])
@pytest.mark.parametrize("target", ["invoice", "payment"])
def test_fx_variants_and_currency_exponents_are_publicly_reconcilable(currency, exponent, mode, target):
    bundle = _multi(73, fx_count=1, foreign_currency=currency, foreign_exponent=exponent,
                    fx_mode=mode, fx_target=target)
    issue, = bundle.truth
    assert issue["type"] == "fx_conversion"
    assert bundle.public["manifest"]["currency_exponents"][currency] == exponent
    assert issue["expected_amount_minor"] > 0
    assert reconcile(bundle.public) == bundle.truth


@pytest.mark.parametrize("field", ["duplicate_count", "allocation_count", "fx_count", "cutoff_count"])
def test_each_family_can_reach_multiple_issues_with_exact_count(field):
    bundle = _multi(33, invoice_count=48, foreign_invoice_count=24, ledger_rows=500, **{field: 12})
    assert len(bundle.truth) == 12
    expected_type = {"duplicate_count": "duplicate_invoice", "allocation_count": "payment_allocation", "fx_count": "fx_conversion", "cutoff_count": "period_cutoff"}[field]
    assert {issue["type"] for issue in bundle.truth} == {expected_type}
    assert max(len(proof) for issue in bundle.truth for proof in issue["evidence_sets"]) <= 10


def test_supported_large_dirty_profile_remains_within_full_scan_bound():
    bundle = _multi(115, invoice_count=80, foreign_invoice_count=20, ledger_rows=1800,
                    duplicate_count=3, allocation_count=3, fx_count=3, cutoff_count=3, refund_count=2)
    public = bundle.public
    assert len(public["ledger"]) <= 2000
    assert len(public["invoices"]) + len(public["payments"]) <= 200
    assert len(public["fx"]) <= 200
    full_scan = sum(math.ceil(len(public[table]) / 50) for table in ("ledger", "invoices", "payments", "fx")) + 1
    assert full_scan <= 50
    assert len(bundle.truth) == 12


def test_same_inputs_are_exactly_repeatable_and_permutation_does_not_change_gold():
    config = {**BASE, "duplicate_count": 1, "allocation_count": 2, "fx_count": 1, "cutoff_count": 1,
              "interaction_allocation_fx": True, "interaction_allocation_cutoff": True}
    first, second = generate(71, config), generate(71, config)
    assert canonical_json(first.public) == canonical_json(second.public)
    assert first.provenance == second.provenance
    assert first.truth == second.truth
    for table in ("ledger", "invoices", "payments", "fx"):
        second.public[table].reverse()
    assert reconcile(second.public) == first.truth


def test_error_toggles_keep_causal_documents_and_clean_history_identical():
    clean = _multi(71)
    dirty = _multi(71, duplicate_count=1, allocation_count=2, fx_count=1, cutoff_count=1,
                   interaction_allocation_fx=True, interaction_allocation_cutoff=True)
    assert clean.provenance["history"] == dirty.provenance["history"]
    assert clean.provenance["clean_public_sha256"] == dirty.provenance["clean_public_sha256"]


@pytest.mark.parametrize("config", [
    {"foreign_invoice_count": True}, {"foreign_invoice_count": 100}, {"foreign_exponent": 4},
    {"foreign_currency": "KZT"}, {"foreign_currency": "usd"}, {"refund_count": 11},
    {"fx_count": -1}, {"cutoff_count": True}, {"fx_mode": "magic"},
    {"interaction_allocation_fx": 1}, {"interaction_allocation_fx": True},
    {"interaction_allocation_cutoff": True},
    {"allocation_count": 1, "fx_count": 1, "cutoff_count": 1, "interaction_allocation_fx": True, "interaction_allocation_cutoff": True},
    {"foreign_invoice_count": 0, "fx_count": 1},
    {"duplicate_count": 4, "allocation_count": 4, "fx_count": 3, "cutoff_count": 2},
    {"ledger_rows": 2000, "duplicate_count": 1},
])
def test_unsupported_or_impossible_stage2_configurations_fail_explicitly(config):
    with pytest.raises(GenerationError):
        _multi(19, **config)
