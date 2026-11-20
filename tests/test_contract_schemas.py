"""Stage-one schema checks against real generator and environment output.

These are local checks. JSON Schema cannot distinguish integer-valued 1.0 from
1 by its standard integer type, so our runtime profile deliberately uses a
strict Python-integer type checker as well as a standard schema lint.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, validators

from fin_audit.contracts import SCHEMA_VERSION, SUPPORTED_SCHEMA_VERSIONS, canonical_json
from fin_audit.environment import AuditEnv
from fin_audit.evaluation import InvalidSubmission, validate_submission
from fin_audit.generator import generate
from fin_audit.runner import run_episode
from fin_audit.storage import export_episode, load_episode


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "episode.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
StrictValidator = validators.extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
        "integer", lambda checker, value: type(value) is int
    ),
)


def validator(definition=None):
    schema = SCHEMA
    if definition is not None:
        schema = {
            "$schema": SCHEMA["$schema"],
            "$ref": f"#/$defs/{definition}",
            "$defs": SCHEMA["$defs"],
        }
    return StrictValidator(schema, format_checker=FormatChecker())


def valid_payload():
    return {
        "episode_id": "test-episode",
        "findings": [{
            "type": "duplicate_invoice",
            "affected_ids": ["row-original", "row-duplicate"],
            "claimed_amount_minor": 12_000,
            "currency": "KZT",
            "evidence_ids": ["doc-invoice", "row-original", "row-duplicate", "policy"],
            "explanation": "One invoice was recognized twice.",
        }],
    }


def test_schemas_are_valid_draft_2020_12_and_versioned_with_contracts():
    Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert set(SCHEMA["$defs"]["manifest"]["properties"]["schema_version"]["enum"]) == set(
        SUPPORTED_SCHEMA_VERSIONS
    )
    assert SCHEMA_VERSION in SUPPORTED_SCHEMA_VERSIONS
    for name in SCHEMA["$defs"]:
        Draft202012Validator.check_schema(validator(name).schema)


@pytest.mark.parametrize("seed", [0, 1, 1123])
@pytest.mark.parametrize(
    "config",
    [
        {"duplicate_count": 0, "allocation_count": 0},
        {"duplicate_count": 1, "allocation_count": 0},
        {"duplicate_count": 0, "allocation_count": 1, "allocation_mode": "missing"},
        {"duplicate_count": 1, "allocation_count": 1},
        {"invoice_count": 3, "ledger_rows": None},
    ],
)
def test_actual_generated_public_records_and_truth_match_schemas(seed, config):
    bundle = generate(seed, config)
    # Serialization is independently checked: schemas do not authorize Python
    # objects, NaN, or non-JSON values merely because they pass a loose type.
    assert json.loads(canonical_json(bundle.public)) == bundle.public
    validator().validate(bundle.public)
    for issue in bundle.truth:
        validator("truth_issue").validate(issue)
    assert bundle.public["manifest"]["ledger_rows"] == len(bundle.public["ledger"])
    assert bundle.public["manifest"]["document_count"] == (
        len(bundle.public["invoices"]) + len(bundle.public["payments"])
    )


def test_native_schema_branch_rejects_private_labels_and_non_native_books():
    bundle = generate(10)
    cases = []
    private_manifest = copy.deepcopy(bundle.public)
    private_manifest["manifest"]["seed"] = 10
    cases.append(private_manifest)
    private_row = copy.deepcopy(bundle.public)
    private_row["ledger"][0]["is_error"] = True
    cases.append(private_row)
    foreign = copy.deepcopy(bundle.public)
    foreign["invoices"][0]["currency"] = "USD"
    cases.append(foreign)
    refund = copy.deepcopy(bundle.public)
    refund["payments"][0]["amount_minor"] *= -1
    cases.append(refund)
    unsupported_fx = copy.deepcopy(bundle.public)
    unsupported_fx["fx"] = [{
        "date": "2026-11-01", "source_currency": "USD",
        "target_currency": "KZT", "rate": "500",
    }]
    cases.append(unsupported_fx)
    for public in cases:
        assert not validator().is_valid(public)
    unsupported_issue = copy.deepcopy(bundle.truth[0])
    unsupported_issue["type"] = "unsupported_issue"
    assert not validator("truth_issue").is_valid(unsupported_issue)


def test_offset_and_minor_unit_rules_are_explicit():
    public = generate(11).public
    naive = copy.deepcopy(public)
    naive["ledger"][0]["timestamp"] = "2026-11-01T12:00:00"
    assert not validator().is_valid(naive)
    boolean_money = copy.deepcopy(public)
    boolean_money["ledger"][0]["debit_minor"] = True
    assert not validator().is_valid(boolean_money)
    two_sides = copy.deepcopy(public)
    two_sides["ledger"][0]["debit_minor"] = 1
    two_sides["ledger"][0]["credit_minor"] = 1
    assert not validator().is_valid(two_sides)


def test_null_payment_reference_is_a_schema_value_not_a_guessed_relation():
    document = copy.deepcopy(generate(12).public["payments"][0])
    document["invoice_refs"] = None
    validator("payment_document").validate(document)


def test_semantically_wrong_strings_are_valid_structure_for_fp_scoring():
    payload = valid_payload()
    payload["findings"][0].update(
        type="unrecognized-type", currency="wrong-currency",
        affected_ids=[], evidence_ids=["guessed-but-not-validated-here"],
    )
    validator("findings").validate(payload)
    validate_submission(payload, "test-episode")


def malformed_payloads():
    cases = [None, {}, {"episode_id": "test-episode", "findings": {}}]
    extra = valid_payload()
    extra["reward"] = 1
    cases.append(extra)
    for key, value in [
        ("claimed_amount_minor", True),
        ("claimed_amount_minor", 1.0),
        ("claimed_amount_minor", -1),
        ("evidence_ids", "doc-invoice"),
        ("explanation", None),
    ]:
        payload = valid_payload()
        payload["findings"][0][key] = value
        cases.append(payload)
    missing = valid_payload()
    del missing["findings"][0]["currency"]
    cases.append(missing)
    extra_finding = valid_payload()
    extra_finding["findings"][0]["true_positive"] = True
    cases.append(extra_finding)
    many = valid_payload()
    many["findings"] *= 31
    cases.append(many)
    evidence = valid_payload()
    evidence["findings"][0]["evidence_ids"] = ["policy"] * 11
    cases.append(evidence)
    explanation = valid_payload()
    explanation["findings"][0]["explanation"] = "я" * 1001
    cases.append(explanation)
    return cases


@pytest.mark.parametrize("payload", malformed_payloads())
def test_adversarial_submit_structure_fails_schema_and_runtime(payload):
    assert not validator("findings").is_valid(payload)
    with pytest.raises(InvalidSubmission):
        validate_submission(payload, "test-episode")


def test_standard_integer_semantics_are_not_misrepresented_as_lexical_enforcement():
    payload = valid_payload()
    payload["findings"][0]["claimed_amount_minor"] = 1.0
    # This is intentional JSON Schema behavior, not a scorer exemption.
    assert Draft202012Validator(validator("findings").schema).is_valid(payload)
    assert not validator("findings").is_valid(payload)
    with pytest.raises(InvalidSubmission):
        validate_submission(payload, "test-episode")


def test_exact_submit_limits_remain_valid_before_semantic_matching():
    payload = valid_payload()
    payload["findings"][0]["evidence_ids"] = ["policy"] * 10
    payload["findings"][0]["explanation"] = "я" * 1000
    payload["findings"] *= 30
    validator("findings").validate(payload)
    validate_submission(payload, "test-episode")


def test_real_environment_error_and_invalid_submit_trajectories_validate():
    bundle = generate(21, {"invoice_count": 3, "ledger_rows": None})
    env = AuditEnv(mode="closed")
    observation, info = env.load_bundle(bundle)
    validator("observation").validate(observation)
    assert info == {}
    episode_id = bundle.public["manifest"]["episode_id"]
    actions = [
        {"action": "list_records", "args": {"table": "ledger", "limit": 2}},
        {"action": "get_document", "args": {"doc_id": bundle.public["invoices"][0]["doc_id"]}},
        {"action": "calculator", "args": {"expression": "100000 - 90000"}},
        {"action": "get_record", "args": {"table": "ledger", "id": "unknown-id"}},
        {"action": "unknown_action", "args": {}},
        {"action": "submit", "args": {"findings": {"episode_id": episode_id}}},
    ]
    rows = []
    for action in actions:
        observation, reward, terminated, truncated, info = env.step(action)
        row = {
            "episode_id": episode_id, "step": env.total_steps, "action": action,
            "observation": observation, "reward": reward,
            "terminated": terminated, "truncated": truncated,
        }
        validator("trajectory").validate(row)
        assert json.loads(canonical_json(row)) == row
        assert info == {}
        rows.append(row)
    assert all(row["reward"] == 0 for row in rows)
    assert rows[-1]["terminated"] is True
    assert env.evaluation_result()["status"] == "invalid_submit"
    # Invalid agent actions are data in a valid error trajectory.
    assert not validator("action").is_valid(actions[-2])
    assert not validator("action").is_valid(actions[-1])
    for action in actions[:-2]:
        validator("action").validate(action)


def test_trajectory_schema_catches_impossible_flags_and_nonterminal_reward():
    base = {
        "episode_id": "episode", "step": 1, "action": None,
        "observation": {"remaining_steps": 99}, "reward": 0,
        "terminated": False, "truncated": False,
    }
    validator("trajectory").validate(base)
    impossible = {**base, "terminated": True, "truncated": True}
    shaped = {**base, "reward": 0.2}
    leaked = copy.deepcopy(base)
    leaked["observation"]["seed"] = 123
    for row in (impossible, shaped, leaked):
        assert not validator("trajectory").is_valid(row)


@pytest.mark.parametrize("policy", ["simple", "random", "mark_all", "oracle"])
@pytest.mark.parametrize("mode", ["train", "closed"])
def test_actual_subprocess_runner_trajectories_match_schema(policy, mode):
    report = run_episode(generate(31), policy=policy, mode=mode, timeout=5)
    assert report["evaluation"] is not None
    for row in report["trajectory"]:
        validator("trajectory").validate(row)
        assert json.loads(canonical_json(row)) == row
        if row["action"] is not None:
            validator("action").validate(row["action"])
    assert report["trajectory"][0]["step"] == 0
    assert report["trajectory"][-1]["terminated"] is True


def test_exported_csv_json_round_trip_remains_schema_valid(tmp_path):
    bundle = generate(32)
    export_episode(bundle, tmp_path / "episode")
    restored = load_episode(tmp_path / "episode")
    validator().validate(restored.public)
    for issue in restored.truth:
        validator("truth_issue").validate(issue)
    assert restored.public == bundle.public
    assert restored.truth == bundle.truth


def test_actual_cli_published_trajectory_file_matches_schema(tmp_path):
    from fin_audit.cli import execute_batch

    summary = execute_batch(profile="cooperative", output=tmp_path, seed=33, verify=True)
    assert summary["stage"] == 1
    assert summary["passed"] is True
    rows = [
        json.loads(line)
        for line in (tmp_path / "trajectories.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rows
    for row in rows:
        validator("trajectory").validate(row)
        assert row["run_id"]
        assert row["policy"]


def test_addressable_fx_row_requires_decimal_string_rate():
    row = {
        "date": "2026-11-01", "source_currency": "USD",
        "target_currency": "KZT", "rate": "500.125", "record_id": "fx-rate",
    }
    validator("fx_row").validate(row)
    for rate in (500.125, "NaN", "Infinity", "0", "0.000", "-1"):
        assert not validator("fx_row").is_valid({**row, "rate": rate})
