"""G2 checks through the actual public-only subprocess policy and tool wire.

Cooperative runs verify protocol/accounting integration, not Linux isolation.
Positive accounting, privacy and replay cases use the production 60s budget:
a 5s override can truncate valid workers under full-system emulation.
Adversarial deadline enforcement is covered separately.
Final held-out 20+20 evaluation and 10+10 release datasets are separate work.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, validators

from fin_audit.checker import reconcile
from fin_audit.contracts import EpisodeBundle, canonical_json, evidence_registry
from fin_audit.environment import AuditEnv
from fin_audit.generator import generate
from fin_audit.profiles import stage2_cases
from fin_audit.runner import run_episode


EXPECTED = {
    "clean_multicurrency": set(), "duplicate_only": {"duplicate_invoice"},
    "allocation_only": {"payment_allocation"}, "fx_only": {"fx_conversion"},
    "cutoff_only": {"period_cutoff"},
    "all_four": {"duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"},
    "allocation_fx": {"payment_allocation", "fx_conversion"},
    "allocation_cutoff": {"payment_allocation", "period_cutoff"},
    "fx_wrong_date": {"fx_conversion"},
}


def _private_keys(value):
    if isinstance(value, dict):
        return set(value).union(*(_private_keys(child) for child in value.values()))
    if isinstance(value, list):
        return set().union(*(_private_keys(child) for child in value))
    return set()


@pytest.mark.parametrize("case", list(stage2_cases()))
def test_actual_public_worker_proves_each_family_and_both_interactions(case):
    bundle = generate(81, stage2_cases()[case])
    assert Counter(issue["type"] for issue in bundle.truth) == Counter(EXPECTED[case])
    report = run_episode(bundle, policy="simple", mode="closed")
    result = report["evaluation"]
    assert report["agent_exitcode"] == 0
    assert (result["tp"], result["fp"], result["fn"]) == (len(EXPECTED[case]), 0, 0)
    assert result["f1"] == 1.0
    assert result["status"] == "scored"
    assert result["inspection_steps"] <= 50
    assert result["total_steps"] == result["inspection_steps"] + 1
    assert all(row["reward"] == 0 and row["info"] == {} for row in report["trajectory"])
    assert report["trajectory"][-1]["terminated"] is True
    assert report["trajectory"][-1]["truncated"] is False


def test_clean_foreign_split_partial_and_refund_cases_do_not_create_accusations():
    bundle = generate(82, stage2_cases()["clean_multicurrency"])
    assert bundle.truth == []
    assert any(doc["currency"] != "KZT" for doc in bundle.public["invoices"])
    assert any(doc["total_minor"] < 0 and doc.get("original_invoice_id") for doc in bundle.public["invoices"])
    assert any(doc["amount_minor"] < 0 for doc in bundle.public["payments"])
    references = Counter(
        doc["invoice_refs"][0] for doc in bundle.public["payments"]
        if doc["amount_minor"] > 0 and doc["invoice_refs"]
    )
    assert any(count > 1 for count in references.values())
    report = run_episode(bundle, policy="simple", mode="closed")
    assert (report["evaluation"]["tp"], report["evaluation"]["fp"], report["evaluation"]["fn"]) == (0, 0, 0)
    assert report["evaluation"]["f1"] == 1.0


@pytest.mark.parametrize("case,other", [("allocation_fx", "fx_conversion"), ("allocation_cutoff", "period_cutoff")])
def test_interaction_findings_refer_to_the_same_real_payment(case, other):
    bundle = generate(83, stage2_cases()[case])
    allocation = next(issue for issue in bundle.truth if issue["type"] == "payment_allocation")
    paired = next(issue for issue in bundle.truth if issue["type"] == other)
    primary = next(row for row in bundle.public["ledger"] if row["row_id"] == paired["affected_ids"][0])
    assert primary["payment_id"] in allocation["affected_ids"]
    report = run_episode(bundle, policy="simple", mode="closed")
    assert report["evaluation"]["tp"] == 2


@pytest.mark.parametrize("currency,exponent", [("JPY", 0), ("USD", 2), ("KWD", 3)])
@pytest.mark.parametrize("fx_mode", ["date", "rate"])
def test_worker_uses_declared_currency_exponents_and_fx_submode(currency, exponent, fx_mode):
    config = {**stage2_cases()["allocation_fx"], "foreign_currency": currency,
              "foreign_exponent": exponent, "fx_mode": fx_mode}
    bundle = generate(84, config)
    assert bundle.public["manifest"]["currency_exponents"][currency] == exponent
    report = run_episode(bundle, policy="simple", mode="closed")
    assert (report["evaluation"]["tp"], report["evaluation"]["fp"], report["evaluation"]["fn"]) == (2, 0, 0)


def test_foreign_fx_records_are_accessible_by_their_evidence_ids():
    bundle = generate(85, stage2_cases()["fx_only"])
    registry = evidence_registry(bundle.public)
    env = AuditEnv(mode="closed")
    env.load_bundle(bundle)
    for row in bundle.public["fx"]:
        assert row["record_id"] in registry
        observation, reward, terminated, truncated, info = env.step({
            "action": "get_record", "args": {"table": "fx", "id": row["record_id"]},
        })
        assert observation["result"] == row
        assert reward == 0 and not terminated and not truncated and info == {}
    observation, *_ = env.step({"action": "get_document", "args": {"doc_id": bundle.public["fx"][0]["record_id"]}})
    assert observation["error"]["code"] == "document_not_found"


def test_maximum_supported_clean_book_can_be_read_with_fifty_inspections():
    config = {**stage2_cases()["clean_multicurrency"], "invoice_count": 100, "ledger_rows": 2000}
    bundle = generate(86, config)
    assert len(bundle.public["ledger"]) == 2000
    assert len(bundle.public["invoices"]) + len(bundle.public["payments"]) <= 200
    env = AuditEnv(mode="closed")
    env.load_bundle(bundle)
    for table in ("ledger", "invoices", "payments", "fx"):
        cursor = None
        records = []
        while True:
            observation, _, terminated, truncated, _ = env.step({
                "action": "list_records", "args": {"table": table, "cursor": cursor, "limit": 50},
            })
            assert not terminated and not truncated
            result = observation["result"]
            records.extend(result["records"])
            cursor = result["next_cursor"]
            if cursor is None:
                break
        assert records == bundle.public[table]
    env.step({"action": "get_document", "args": {"doc_id": "policy"}})
    assert env.inspection_steps <= 50
    report = run_episode(bundle, policy="simple", mode="closed")
    assert report["evaluation"]["f1"] == 1.0
    assert report["evaluation"]["inspection_steps"] <= 50


def test_closed_stage_two_observations_do_not_contain_private_truth_or_provenance():
    original = generate(87, stage2_cases()["all_four"])
    sentinel = "stage-two-private-marker-never-on-observation-wire"
    truth = copy.deepcopy(original.truth)
    for issue in truth:
        issue["private_marker"] = sentinel
    bundle = EpisodeBundle(original.public, truth, {**original.provenance, "private_marker": sentinel})
    report = run_episode(bundle, policy="simple", mode="closed")
    forbidden = {"seed", "truth", "provenance", "issue_id", "evidence_sets", "expected_amount_minor", "private_marker"}
    for row in report["trajectory"]:
        assert not forbidden.intersection(_private_keys(row["observation"]))
        assert sentinel not in canonical_json(row["observation"])
        assert row["reward"] == 0 and row["info"] == {}
    assert report["evaluation"]["tp"] == 4


@pytest.mark.parametrize("policy", ["simple", "random", "mark_all", "oracle"])
def test_stage_two_replay_and_closed_train_visibility_are_preserved(policy):
    bundle = generate(88, stage2_cases()["all_four"])
    first = run_episode(bundle, policy=policy, mode="closed")
    second = run_episode(generate(88, stage2_cases()["all_four"]), policy=policy, mode="closed")
    train = run_episode(bundle, policy=policy, mode="train")
    assert canonical_json(first["trajectory"]) == canonical_json(second["trajectory"])
    assert first["evaluation"] == second["evaluation"] == train["evaluation"]
    assert all(row["reward"] == 0 for row in first["trajectory"])
    assert all(row["reward"] == 0 for row in train["trajectory"][:-1])
    assert train["trajectory"][-1]["reward"] == train["evaluation"]["reward"]
    if policy == "oracle":
        assert first["profile"] == "trusted_oracle"
        assert first["evaluation"]["reward"] == 1
        assert first["evaluation"]["inspection_steps"] == 0
    if policy == "mark_all":
        assert first["evaluation"]["fp"] == 30
        assert first["evaluation"]["reward"] == -1


def test_consistent_id_renaming_and_order_changes_preserve_worker_score():
    original = generate(89, stage2_cases()["all_four"])
    public = copy.deepcopy(original.public)
    id_fields = {"row_id", "doc_id", "invoice_id", "payment_id", "entity_id", "journal_id", "record_id", "original_invoice_id"}
    ids = {public["manifest"]["episode_id"]}
    for table in ("ledger", "invoices", "payments", "fx"):
        ids.update(value for row in public[table] for key, value in row.items() if key in id_fields and isinstance(value, str))
    mapping = {value: "presented_" + hashlib.sha256(value.encode()).hexdigest()[:24] for value in ids}

    def transform(value):
        if isinstance(value, str):
            return mapping.get(value, value)
        if isinstance(value, dict):
            return {key: transform(child) for key, child in value.items()}
        if isinstance(value, list):
            return [transform(child) for child in value]
        return value

    public = transform(public)
    for table in ("ledger", "invoices", "payments", "fx"):
        public[table].reverse()
    truth = reconcile(public)
    assert sorted((item["type"], item["expected_amount_minor"]) for item in truth) == sorted(
        (item["type"], item["expected_amount_minor"]) for item in original.truth
    )
    transformed = EpisodeBundle(public, truth, {"schema_version": "0.2.0"})
    first = run_episode(original, policy="simple", mode="closed")
    second = run_episode(transformed, policy="simple", mode="closed")
    for field in ("tp", "fp", "fn", "f1", "reward", "inspection_steps"):
        assert first["evaluation"][field] == second["evaluation"][field]


def test_stage_two_worker_preserves_large_minor_unit_values():
    from finance_examples import fx_example

    # Integer 500/495/505 quotes make this exact scaling fixture valid.
    # Scaling already-rounded quarter-rate books would change the accounting
    # facts and must not be mistaken for a product precision regression.
    public = fx_example()
    original_truth = reconcile(public)
    factor = 2**60 + 7
    for invoice in public["invoices"]:
        invoice["total_minor"] *= factor
        for line in invoice["line_items"]:
            line["unit_price_minor"] *= factor
            line["total_minor"] *= factor
    for payment in public["payments"]:
        payment["amount_minor"] *= factor
    for row in public["ledger"]:
        for field in ("debit_minor", "credit_minor", "source_amount_minor"):
            row[field] *= factor
        if row["allocated_minor"] is not None:
            row["allocated_minor"] *= factor
    truth = reconcile(public)
    assert sorted((item["type"], item["expected_amount_minor"]) for item in truth) == sorted(
        (item["type"], item["expected_amount_minor"] * factor) for item in original_truth
    )
    report = run_episode(EpisodeBundle(public, truth, {"schema_version": "0.2.0"}))
    assert report["evaluation"]["tp"] == 2
    assert report["evaluation"]["f1"] == 1.0


def test_actual_stage_two_cli_publishes_valid_completed_reports(tmp_path):
    from fin_audit.cli import execute_stage2_batch

    summary = execute_stage2_batch(profile="cooperative", output=tmp_path, seed=91, verify=True)
    assert summary["stage"] == 2
    assert summary["passed"] is True
    assert len(summary["runs"]) == 5 * len(stage2_cases())
    assert summary["secure_isolation_verified"] is False
    assert "10 train + 10 dev exports" in summary["remaining_competition_work"]
    schema = json.loads((Path(__file__).resolve().parents[1] / "schemas" / "episode.schema.json").read_text())
    strict = validators.extend(
        Draft202012Validator,
        type_checker=Draft202012Validator.TYPE_CHECKER.redefine("integer", lambda checker, value: type(value) is int),
    )
    trajectory_validator = strict({
        "$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": "#/$defs/trajectory",
    }, format_checker=FormatChecker())
    rows = [json.loads(line) for line in (tmp_path / "trajectories.jsonl").read_text().splitlines()]
    assert rows
    for row in rows:
        trajectory_validator.validate(row)
        assert row["reward"] == 0
    assert (tmp_path / "evaluation.json").is_file()
