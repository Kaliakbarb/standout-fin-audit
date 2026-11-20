"""One real 20+20 rehearsal plus fast orchestration failures and aggregation.

The fake runner below is explicitly a unit-test double, never an evaluation
artifact used to claim public-only policy performance or secure isolation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import stat
from collections import Counter

import pytest

from fin_audit import protocol
from fin_audit.contracts import canonical_json
from fin_audit.corpus import generate_holdout_specs
from fin_audit.environment import AuditEnv
from fin_audit.generator import generate
from fin_audit.runner import oracle_action


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def remove_protocol_test_artifacts(tmp_path):
    # Whole-batch artifacts are intentionally substantial; do not retain many
    # passing test runs inside the supported container's bounded /tmp tmpfs.
    yield
    shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.fixture
def fast_protocol(monkeypatch):
    """Keep real accounting/transforms/storage, replace only runner and sizes."""
    cache, calls = {}, []

    def miniature(seed, config):
        key = (seed, canonical_json(config))
        if key not in cache:
            cache[key] = generate(seed, {**config, "ledger_rows": None})
        return cache[key]

    def simulated(bundle, *, policy, mode, profile):
        calls.append((bundle.public["manifest"]["episode_id"], policy))
        env = AuditEnv(mode=mode)
        initial, info = env.load_bundle(bundle)
        episode = initial["manifest"]["episode_id"]
        rows = [{"episode_id": episode, "step": 0, "action": None,
                 "observation": initial, "reward": 0.0, "terminated": False,
                 "truncated": False, "info": info}]
        if policy in {"simple", "oracle"}:
            action = oracle_action(bundle)
        elif policy == "random":
            action = {"action": "submit", "args": {"findings": {"episode_id": episode, "findings": []}}}
        else:
            row = bundle.public["ledger"][0]
            claim = {"type": "duplicate_invoice", "affected_ids": [row["row_id"]],
                     "claimed_amount_minor": row["debit_minor"] + row["credit_minor"],
                     "currency": "KZT", "evidence_ids": ["policy"], "explanation": "Unit-test FP"}
            action = {"action": "submit", "args": {"findings": {"episode_id": episode, "findings": [claim]}}}
        observation, reward, terminated, truncated, info = env.step(action)
        rows.append({"episode_id": episode, "step": env.total_steps, "action": action,
                     "observation": observation, "reward": reward, "terminated": terminated,
                     "truncated": truncated, "info": info})
        return {"episode_id": episode, "policy": policy, "mode": mode,
                "profile": "trusted_oracle" if policy == "oracle" else profile,
                "trajectory": rows, "evaluation": env.evaluation_result(),
                "stderr": "", "agent_exitcode": None if policy == "oracle" else 0,
                "elapsed_seconds": 0.001}

    monkeypatch.setattr(protocol, "generate", miniature)
    monkeypatch.setattr(protocol, "run_episode", simulated)
    monkeypatch.setattr(protocol, "_source_hashes", lambda: {"test-double-source": "a" * 64})
    return calls, simulated


def test_real_twenty_episode_twenty_pair_cooperative_protocol(tmp_path):
    master = 2**100 + 147
    output = tmp_path / "protocol"
    summary = protocol.run_protocol(output=output, master_seed=master)
    assert summary["stage"] == 3
    assert summary["passed"] is True, summary["checks"]
    assert len(summary["runs"]) == 80
    assert len(summary["pair_checks"]) == 20
    assert all(row["passed"] for row in summary["pair_checks"])
    assert len(summary["replay_checks"]) == 80
    assert all(row["passed"] for row in summary["replay_checks"])
    counts = summary["execution_counts"]
    assert counts["cohort_policy_runs"] == 80
    assert counts["paired_oracle_runs"] == 40
    assert counts["ordinary_worker_processes"] == 61
    assert counts["total_run_episode_calls"] == 121
    assert counts["direct_equivalence_cases"] == 80
    assert counts["direct_evaluations"] == 160
    assert Counter(run["cohort"] for run in summary["runs"]) == {"clean": 20, "interacting": 20, "other": 40}
    assert Counter(run["policy"] for run in summary["runs"]) == dict.fromkeys(protocol.POLICIES, 20)
    assert _read(output / "summary.json") == summary
    receipt = _read(output / "COMPLETE.json")
    assert receipt["completed"] is True and receipt["passed"] is True
    for filename, digest in receipt["sha256"].items():
        assert hashlib.sha256((output / filename).read_bytes()).hexdigest() == digest
    for filename in ("evaluation.json", "run-manifest.json"):
        assert stat.S_IMODE((output / filename).stat().st_mode) == 0o600
    manifest = _read(output / "run-manifest.json")
    assert manifest["master_seed"] == master
    assert manifest["source_sha256"]
    assert len(manifest["episodes"]) == len(manifest["pairs"]) == 20
    assert all("seed" in episode and "config" in episode for episode in manifest["episodes"])
    assert len(list((output / "episodes").iterdir())) == 20
    assert len(list((output / "paired_episodes").iterdir())) == 20
    private = _read(output / "evaluation.json")
    assert all("trajectory" not in report and report["trajectory_run_id"] for report in private["cohort"])
    assert "master_seed" not in canonical_json(summary)
    for line in (output / "trajectories.jsonl").read_text().splitlines():
        row = json.loads(line)
        assert row["reward"] == 0 and row["info"] == {}
        assert not protocol._PRIVATE_KEYS.intersection(protocol._private_keys(row["observation"]))
    aggregates = summary["aggregates"]
    assert aggregates["by_policy"]["oracle"]["return"] == 1
    assert aggregates["by_policy"]["mark_all"]["return"] == -1
    assert aggregates["by_policy"]["simple"]["valid_submit_rate"] == 1
    assert aggregates["by_policy"]["simple"]["truncated_rate"] == 0
    assert aggregates["by_cohort"]["clean"]["simple"]["clean_false_accusation_rate"] == 0
    assert aggregates["by_type"]["fx_conversion"]["mark_all"]["clean_false_accusation_rate"] == 0
    assert aggregates["by_type"]["duplicate_invoice"]["mark_all"]["clean_false_accusation_rate"] == 1
    assert summary["rss"]["self_peak_rss_bytes"] > 0
    assert "not a sum" in summary["rss"]["scope"]


def test_unit_orchestration_uses_same_twenty_worlds_and_withholds_reports_until_last_pair(tmp_path, monkeypatch, fast_protocol):
    calls, original = fast_protocol
    output = tmp_path / "protocol"

    def inspect_visibility(*args, **kwargs):
        assert not output.exists()
        for stage in tmp_path.glob(".protocol-staging-*"):
            assert not (stage / "summary.json").exists()
            assert not (stage / "evaluation.json").exists()
            assert not (stage / "COMPLETE.json").exists()
        return original(*args, **kwargs)

    monkeypatch.setattr(protocol, "run_episode", inspect_visibility)
    summary = protocol.run_protocol(output=output, master_seed=25)
    assert summary["passed"] is True
    assert len(calls) == 121
    cohort_calls = calls[:80]
    for offset in range(0, 80, 4):
        group = cohort_calls[offset:offset + 4]
        assert len({episode for episode, _ in group}) == 1
        assert [policy for _, policy in group] == list(protocol.POLICIES)


@pytest.mark.parametrize("failure_call", [1, 80, 120, 121])
def test_interrupted_batches_leave_no_completed_output(tmp_path, monkeypatch, fast_protocol, failure_call):
    _, original = fast_protocol
    counter = 0

    def fail(*args, **kwargs):
        nonlocal counter
        counter += 1
        if counter == failure_call:
            raise RuntimeError("injected_test_failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(protocol, "run_episode", fail)
    output = tmp_path / "protocol"
    with pytest.raises(RuntimeError, match="injected_test_failure"):
        protocol.run_protocol(output=output, master_seed=26)
    assert not output.exists()
    assert not list(tmp_path.glob(".protocol-staging-*"))
    receipt, = tmp_path.glob(".protocol-FAILED-*.json")
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert _read(receipt)["completed"] is False
    assert _read(receipt)["master_seed"] == 26


def test_existing_output_and_dangling_symlink_are_never_followed(tmp_path, fast_protocol):
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "user-file").write_text("preserve")
    with pytest.raises(FileExistsError):
        protocol.run_protocol(output=existing, master_seed=27)
    assert (existing / "user-file").read_text() == "preserve"
    target = tmp_path / "must-not-be-created"
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(FileExistsError):
        protocol.run_protocol(output=link, master_seed=27)
    assert link.is_symlink()
    assert not target.exists()


@pytest.mark.parametrize("master", [True, -1, 1.5, "25", 2**256])
def test_invalid_master_seed_fails_before_export(tmp_path, master):
    with pytest.raises(protocol.ProtocolError, match="invalid_master_seed"):
        protocol.run_protocol(output=tmp_path / "protocol", master_seed=master)
    assert list(tmp_path.iterdir()) == []


def test_open_overlap_and_wrong_cohort_are_rejected_before_workers(tmp_path, monkeypatch, fast_protocol):
    specs = generate_holdout_specs(28)
    specs[0]["seed"] = protocol.corpus_specs()["train"][0]["seed"]
    monkeypatch.setattr(protocol, "generate_holdout_specs", lambda master: copy.deepcopy(specs))
    with pytest.raises(protocol.ProtocolError, match="holdout_seed_overlap"):
        protocol.run_protocol(output=tmp_path / "protocol", master_seed=28)
    specs[0]["seed"] = 123
    specs[0]["cohort"] = "other"
    with pytest.raises(protocol.ProtocolError, match="invalid_holdout_cohort_composition"):
        protocol.run_protocol(output=tmp_path / "protocol", master_seed=28)


def test_source_changes_prevent_a_green_completed_result(tmp_path, monkeypatch, fast_protocol):
    count = 0

    def changing_source():
        nonlocal count
        count += 1
        return {"source": str(count)}

    monkeypatch.setattr(protocol, "_source_hashes", changing_source)
    summary = protocol.run_protocol(output=tmp_path / "protocol", master_seed=29)
    assert summary["passed"] is False
    assert summary["checks"]["source_snapshot_unchanged"] is False
    assert _read(tmp_path / "protocol" / "COMPLETE.json")["passed"] is False


def test_equivalent_answer_defect_is_reported_instead_of_hidden(tmp_path, monkeypatch, fast_protocol):
    original = protocol.remap_findings

    def wrong_currency(payload, mapping, new_episode_id):
        result = original(payload, mapping, new_episode_id)
        if result.get("findings"):
            result["findings"][0]["currency"] = "WRONG"
        return result

    monkeypatch.setattr(protocol, "remap_findings", wrong_currency)
    summary = protocol.run_protocol(output=tmp_path / "protocol", master_seed=30)
    assert summary["passed"] is False
    assert summary["checks"]["twenty_pair_checks"] is False
    assert any(not pair["passed"] for pair in summary["pair_checks"])


@pytest.mark.parametrize("reason", ["agent_timeout", "agent_failed", "protocol_error", "time_limit"])
def test_external_events_are_not_faked_as_exact_action_replay(reason):
    report = {"case": "h00", "policy": "simple", "evaluation": {"termination_reason": reason}}
    result = protocol._replay(report, None, True)
    assert result["passed"] is False
    assert result["status"] == "external_event_not_fixed_action_replay"


def test_json_null_invalid_action_replays_exactly_before_a_valid_submit():
    bundle = generate(901, {"invoice_count": 3, "ledger_rows": None,
                            "duplicate_count": 0, "allocation_count": 0})
    env = AuditEnv(mode="closed")
    initial, info = env.load_bundle(bundle)
    episode = initial["manifest"]["episode_id"]
    trajectory = [{"episode_id": episode, "step": 0, "action": None,
                   "observation": initial, "reward": 0.0, "terminated": False,
                   "truncated": False, "info": info}]
    for action in (None, {"action": "submit", "args": {"findings": {"episode_id": episode, "findings": []}}}):
        transition = env.step(action)
        trajectory.append(protocol._transition_row(episode, env.total_steps, action, transition))
    assert trajectory[1]["observation"]["error"]["code"] == "invalid_action"
    assert env.inspection_steps == 1 and env.total_steps == 2
    assert env.evaluation_result()["termination_reason"] == "submitted"
    report = {"case": "h00", "policy": "simple", "trajectory": trajectory,
              "evaluation": env.evaluation_result()}
    replay = protocol._replay(report, bundle, True)
    assert replay["passed"] is True
    assert replay["trajectory_equal"] is True
    assert replay["private_score_equal"] is True
    assert replay["replayed_actions"] == 2


def test_empty_and_clean_macro_metrics_preserve_defined_conventions():
    assert protocol._metrics(0, 0, 0) == {"precision": 0, "recall": 0, "f1": 1}
    assert protocol._aggregate([], {})["f1"] is None
    assert protocol._metrics(0, 1, 0)["f1"] == 0
