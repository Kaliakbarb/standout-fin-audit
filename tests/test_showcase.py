"""The demonstration's numbers and repairs are checked against source documents."""

from __future__ import annotations

import copy
import json
import shutil
from collections import Counter, defaultdict
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from fin_audit.checker import reconcile
from fin_audit.contracts import EpisodeBundle, canonical_json, evidence_registry, validate_public_privacy
from fin_audit.environment import AuditEnv
from fin_audit.evaluation import evaluate
from fin_audit.runner import oracle_action, run_episode
from fin_audit.showcase import (
    CASE_ORDER, ShowcaseError, repair_payment_fx, repair_period_cutoff,
    showcase_analysis, showcase_cases,
)
from fin_audit.storage import export_episode, load_episode
from fin_audit.transforms import remap_findings, transform_episode

EXPECTED = {
    "clean": [], "duplicate_only": ["duplicate_invoice"], "fx_only": ["fx_conversion"],
    "masked_both": ["duplicate_invoice", "fx_conversion"],
    "allocation_fx": ["fx_conversion", "payment_allocation"],
    "allocation_fx_repaired": ["payment_allocation"],
    "allocation_cutoff": ["payment_allocation", "period_cutoff"],
    "allocation_cutoff_repaired": ["payment_allocation"],
}


@pytest.fixture(scope="module")
def cases():
    return showcase_cases()


def test_masking_has_550000_kzt_total_and_exact_opposite_50000_effects(cases):
    report = showcase_analysis(cases)
    assert report["scope"] == "post_run_only"
    assert report["case_order"] == list(CASE_ORDER)
    assert all(type(value) is bool and value for value in report["checks"].values())
    totals = {card["key"]: card["recognition_total_minor"] for card in report["cards"]}
    assert [totals[key] for key in CASE_ORDER[:4]] == [55_000_000, 60_000_000, 50_000_000, 55_000_000]
    assert report["masking"]["net_effect_minor"] == 0
    assert report["masking"]["gross_absolute_effect_minor"] == 10_000_000
    assert [effect["signed_minor"] for effect in report["masking"]["signed_effects"]] == [5_000_000, -5_000_000]
    assert all(effect["scope"] == "recognition_and_revenue_projection" for effect in report["masking"]["signed_effects"])
    assert "not total financial damages" in report["masking"]["scope"]
    assert Counter(issue["type"] for issue in cases["masked_both"].truth) == {"duplicate_invoice": 1, "fx_conversion": 1}
    assert {issue["expected_amount_minor"] for issue in cases["masked_both"].truth} == {5_000_000}


def test_all_four_masking_variants_preserve_all_source_documents_and_policy(cases):
    clean = cases["clean"].public
    for key in CASE_ORDER[:4]:
        public = cases[key].public
        for field in ("invoices", "payments", "fx", "policy"):
            assert public[field] == clean[field]
        assert len(public["invoices"]) == 2 and public["payments"] == []
    assert {doc["currency"]: doc["total_minor"] for doc in clean["invoices"]} == {"KZT": 5_000_000, "USD": 100_000}
    assert {row["date"]: row["rate"] for row in clean["fx"]} == {"2026-11-04": "450", "2026-11-05": "500"}


@pytest.mark.parametrize("key", CASE_ORDER)
def test_each_case_obeys_unchanged_production_book_schema_and_full_reconciliation(cases, key):
    bundle = cases[key]
    schema = json.loads((Path(__file__).resolve().parents[1] / "schemas" / "episode.schema.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(bundle.public)
    validate_public_privacy(bundle.public)
    assert reconcile(bundle.public) == bundle.truth
    assert sorted(issue["type"] for issue in bundle.truth) == EXPECTED[key]
    journals = defaultdict(list)
    for row in bundle.public["ledger"]:
        journals[row["journal_id"]].append(row)
    for rows in journals.values():
        assert sum(row["debit_minor"] for row in rows) == sum(row["credit_minor"] for row in rows)
    registry = evidence_registry(bundle.public)
    assert all(0 < len(proof) <= 10 and set(proof) <= registry for issue in bundle.truth for proof in issue["evidence_sets"])


def test_fx_repair_changes_only_valuation_and_recomputes_dependent_allocation(cases):
    before = cases["allocation_fx"]
    original = canonical_json(before.public)
    repaired = repair_payment_fx(before.public)
    assert canonical_json(before.public) == original
    assert repaired == cases["allocation_fx_repaired"].public
    for table in ("invoices", "payments", "fx", "policy"):
        assert repaired[table] == before.public[table]
    before_types = {issue["type"]: issue["expected_amount_minor"] for issue in reconcile(before.public)}
    after_issue, = reconcile(repaired)
    assert before_types == {"fx_conversion": 45_000, "payment_allocation": 4_992_500}
    assert (after_issue["type"], after_issue["expected_amount_minor"]) == ("payment_allocation", 5_000_000)
    original_allocations = {row["journal_id"]: row["allocated_minor"] for row in before.public["ledger"]}
    assert all(row["allocated_minor"] == original_allocations[row["journal_id"]] for row in repaired["ledger"])


def test_cutoff_repair_changes_timestamps_not_amounts_or_source_facts(cases):
    before = cases["allocation_cutoff"]
    repaired = repair_period_cutoff(before.public)
    assert repaired == cases["allocation_cutoff_repaired"].public
    original_rows = {row["row_id"]: row for row in before.public["ledger"]}
    for row in repaired["ledger"]:
        original = original_rows[row["row_id"]]
        assert {key: value for key, value in row.items() if key != "timestamp"} == {key: value for key, value in original.items() if key != "timestamp"}
    for table in ("invoices", "payments", "fx", "policy"):
        assert repaired[table] == before.public[table]
    before_amounts = {issue["type"]: issue["expected_amount_minor"] for issue in before.truth}
    assert before_amounts == {"payment_allocation": 5_217_500, "period_cutoff": 31_305_000}
    after_issue, = reconcile(repaired)
    assert (after_issue["type"], after_issue["expected_amount_minor"]) == ("payment_allocation", 5_217_500)


def test_repair_helpers_need_only_public_data_and_reject_noop_claims(cases):
    # There is no truth/provenance argument for a repair to consult.
    source = copy.deepcopy(cases["allocation_fx"].public)
    assert reconcile(repair_payment_fx(source))[0]["type"] == "payment_allocation"
    with pytest.raises(ShowcaseError, match="no_foreign_receipt_fx_error"):
        repair_payment_fx(cases["clean"].public)
    with pytest.raises(ShowcaseError, match="no_cutoff_error"):
        repair_period_cutoff(cases["allocation_fx"].public)


def test_calculation_cards_have_reproducible_source_terms_and_do_not_mutate_cases(cases):
    snapshots = {key: canonical_json({"public": bundle.public, "truth": bundle.truth, "provenance": bundle.provenance}) for key, bundle in cases.items()}
    analysis = showcase_analysis(cases)
    assert all(issue["amount_minor"] == issue["calculation"]["discrepancy_minor"] for card in analysis["cards"] for issue in card["issues"])
    masked = next(card for card in analysis["cards"] if card["key"] == "masked_both")
    fx = next(issue for issue in masked["issues"] if issue["type"] == "fx_conversion")["calculation"]
    assert fx["source_amount_minor"] == 100_000 and fx["source_currency"] == "USD"
    assert fx["correct_fx_rate"] == "500" and fx["applied_fx_rate"] == "450"
    assert fx["expected_base_minor"] == 50_000_000 and fx["observed_base_minor"] == 45_000_000
    assert analysis["dependent_fx"]["allocation_change_minor"] == 7_500
    assert analysis["dependent_cutoff"]["before_allocation_minor"] == analysis["dependent_cutoff"]["after_allocation_minor"]
    assert snapshots == {key: canonical_json({"public": bundle.public, "truth": bundle.truth, "provenance": bundle.provenance}) for key, bundle in cases.items()}


def test_all_scenarios_export_and_load_with_protected_gold(cases, tmp_path):
    for key, bundle in cases.items():
        folder = tmp_path / key
        export_episode(bundle, folder)
        assert load_episode(folder) == bundle
        shutil.rmtree(folder)


@pytest.mark.parametrize("key", CASE_ORDER)
def test_presentation_change_preserves_evidence_score_at_equal_budget(cases, key):
    source = cases[key]
    transformed = transform_episode(source, 137)
    payload = oracle_action(source)["args"]["findings"]
    remapped = remap_findings(payload, transformed.mapping, transformed.bundle.public["manifest"]["episode_id"])
    first = evaluate(payload, source.truth, evidence_registry(source.public), 9, source.public["manifest"]["episode_id"])
    second = evaluate(remapped, transformed.bundle.truth, evidence_registry(transformed.bundle.public), 9, transformed.bundle.public["manifest"]["episode_id"])
    assert (first.tp, first.fp, first.fn, first.f1, first.reward) == (second.tp, second.fp, second.fn, second.f1, second.reward)


@pytest.mark.parametrize("key", CASE_ORDER)
def test_actual_public_worker_proves_each_demonstration(cases, key):
    report = run_episode(cases[key], policy="simple", mode="closed", timeout=5)
    score = report["evaluation"]
    assert (score["tp"], score["fp"], score["fn"]) == (len(EXPECTED[key]), 0, 0)
    assert score["termination_reason"] == "submitted" and score["status"] == "scored"
    assert report["agent_exitcode"] == 0
    assert all(row["reward"] == 0 and row["info"] == {} for row in report["trajectory"])


def test_post_run_cards_are_absent_from_closed_start_observations(cases):
    env = AuditEnv(mode="closed")
    observation, info = env.load_bundle(cases["masked_both"])
    assert info == {}
    text = canonical_json(observation)
    assert not {"cards", "signed_effects", "showcase_case", "masking", "checks"}.intersection(observation)
    assert all(issue["issue_id"] not in text for issue in cases["masked_both"].truth)


def test_analysis_rejects_missing_case_and_tampered_gold(cases):
    partial = dict(cases)
    partial.pop("clean")
    with pytest.raises(ShowcaseError, match="eight_named_cases"):
        showcase_analysis(partial)
    tampered = dict(cases)
    original = cases["masked_both"]
    tampered["masked_both"] = EpisodeBundle(original.public, [], original.provenance)
    with pytest.raises(ShowcaseError, match="gold_does_not_match"):
        showcase_analysis(tampered)


def test_showcase_cases_are_repeatable_and_independently_mutable():
    first, second = showcase_cases(), showcase_cases()
    assert first == second
    first["clean"].public["invoices"][0]["total_minor"] = 1
    assert second["clean"].public["invoices"][0]["total_minor"] != 1
