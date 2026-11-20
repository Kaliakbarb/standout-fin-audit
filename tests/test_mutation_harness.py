"""Fast harness regressions; the full frozen mutation suite is a separate CLI gate."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

import mutation_support as harness


ROOT = Path(__file__).resolve().parents[1]
CLEAN_TEST = harness.TEST_PATH + "::test_clean_silence_is_correct_despite_zero_precision_and_recall"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def event(outcome="passed", *, when="call", assertion=False):
    return {"origin_verified": True, "records": [{"nodeid": "test.py::test_contract", "when": when,
            "outcome": outcome, "semantic_assertion": assertion}], "collection_errors": []}


def test_frozen_faults_change_one_unique_ast_target_and_leave_production_untouched():
    plan = harness.load_plan(ROOT)
    original = (ROOT / harness.SCORER_PATH).read_bytes()
    patches = [harness.apply_mutation(original.decode(), spec["operator"]) for spec in plan["mutants"]]
    assert len(patches) == len({patch.mutated_sha256 for patch in patches}) == 12
    for patch in patches:
        compile(ast.parse(patch.source), "mutant.py", "exec")
        assert patch.target_matches == 1
        assert patch.source_sha256 == sha(original)
        assert patch.mutated_sha256 == sha(patch.source.encode())
        assert patch.patch_sha256 == sha(patch.diff.encode())
        assert patch.ast_before_sha256 != patch.ast_after_sha256
    assert (ROOT / harness.SCORER_PATH).read_bytes() == original


@pytest.mark.parametrize("code,events,kwargs,status,reason", [
    (0, event(), {}, "survived", "all_detection_tests_passed"),
    (0, event(), {"baseline": True}, "passed", None),
    (1, event("failed", assertion=True), {}, "killed", "assertion_failure"),
    (1, event("failed", assertion=True), {"baseline": True}, "failed", "baseline_assertion_failure"),
    (1, event("failed"), {}, "invalid", "runtime_or_fixture_error"),
    (1, event("failed", when="setup"), {}, "invalid", "runtime_or_fixture_error"),
    (-9, event("failed", assertion=True), {}, "invalid", "unexpected_exitcode"),
    (0, event("skipped"), {}, "invalid", "no_executed_detection_tests"),
    (4, None, {}, "invalid", "missing_or_invalid_child_report"),
    (70, {"origin_verified": False}, {}, "invalid", "origin_or_import_error"),
    (2, {**event(), "collection_errors": ["missing test"]}, {}, "invalid", "launcher_or_collection_error"),
    (0, event(), {"timed_out": True}, "invalid", "timeout"),
    (0, event(), {"output_limit": True}, "invalid", "output_limit"),
])
def test_only_real_call_assertions_are_semantic_kills(code, events, kwargs, status, reason):
    result = harness.classify_execution(code, events, **kwargs)
    assert (result["status"], result["reason"]) == (status, reason)
    assert result["detected"] == (status == "killed")
    assert result["passed"] == (status == "passed")


def test_actual_isolated_child_loads_patched_scorer_and_detects_wrong_clean_f1():
    source = (ROOT / harness.SCORER_PATH).read_text()
    baseline = harness.run_detection(ROOT, source, [CLEAN_TEST], baseline=True)
    assert baseline["passed"], baseline
    mutation = harness.apply_mutation(source, "wrong_clean_f1")
    mutant = harness.run_detection(ROOT, mutation.source, [CLEAN_TEST])
    assert mutant["status"] == "killed", mutant
    assert mutant["exitcode"] == 1
    assert mutant["events"]["origins"]["evaluation"]["sha256"] == mutation.mutated_sha256
    assert mutant["events"]["origins"]["evaluation"]["path"] != str(ROOT / harness.SCORER_PATH)
    assert mutant["killed_by"] == [CLEAN_TEST]


def test_actual_import_failure_is_invalid_and_not_a_kill():
    source = (ROOT / harness.SCORER_PATH).read_text() + "\nraise RuntimeError('import failure fixture')\n"
    result = harness.run_detection(ROOT, source, [CLEAN_TEST])
    assert result["status"] == "invalid"
    assert result["reason"] == "origin_or_import_error"
    assert not result["detected"]
    assert result["exitcode"] == 70


def test_actual_runtime_exception_is_invalid_while_undetected_mutant_survives():
    source = (ROOT / harness.SCORER_PATH).read_text()
    # A currency mutant cannot affect a clean empty answer. A weak detector
    # must honestly report a survivor, even though another test kills it.
    weak = harness.run_detection(ROOT, harness.apply_mutation(source, "ignore_currency").source, [CLEAN_TEST])
    assert weak["status"] == "survived" and not weak["detected"]
    assert weak["reason"] == "all_detection_tests_passed"
    broken = source + "\ndef evaluate(*args, **kwargs):\n    raise RuntimeError('runtime fixture')\n"
    result = harness.run_detection(ROOT, broken, [CLEAN_TEST])
    assert result["status"] == "invalid"
    assert result["reason"] == "runtime_or_fixture_error"
    assert result["exitcode"] == 1 and not result["detected"]


def test_stdout_eof_waits_for_actual_exit_instead_of_killing_teardown(monkeypatch):
    monkeypatch.setattr(harness, "_CHILD", '''import json, os, sys, time
from pathlib import Path
Path(sys.argv[2]).write_text(json.dumps({"origin_verified": True, "records": [
    {"nodeid": "fixture", "when": "call", "outcome": "passed", "semantic_assertion": False}
], "collection_errors": []}))
os.close(1)
os.close(2)
time.sleep(0.1)
raise SystemExit(0)
''')
    result = harness.run_detection(ROOT, (ROOT / harness.SCORER_PATH).read_text(), [CLEAN_TEST], baseline=True)
    assert result["passed"] and result["exitcode"] == 0
    assert result["elapsed_seconds"] >= 0.1


def test_actual_deadline_and_output_flood_are_bounded_invalid_runs():
    source = (ROOT / harness.SCORER_PATH).read_text()
    timed = harness.run_detection(ROOT, source + "\nimport time; time.sleep(20)\n", [CLEAN_TEST], timeout_seconds=0.15)
    assert timed["status"] == "invalid" and timed["reason"] == "timeout"
    assert timed["elapsed_seconds"] < 3
    flooded = harness.run_detection(ROOT, source + "\nimport os\nwhile True: os.write(1, b'x' * 65536)\n", [CLEAN_TEST])
    assert flooded["status"] == "invalid" and flooded["reason"] == "output_limit"
    assert len(flooded["stdout"].encode()) <= harness.MAX_CAPTURE_BYTES
    assert flooded["stdout_bytes_read"] > harness.MAX_STREAM_BYTES
    assert flooded["stdout_truncated"]


def test_stop_denied_for_live_child_is_not_suppressed(monkeypatch):
    class Child:
        pid = 123

        def poll(self):
            return None

    def denied(*args):
        raise PermissionError("fixture")

    monkeypatch.setattr(harness.os, "killpg", denied)
    with pytest.raises(PermissionError):
        harness._stop(Child())


def test_stop_denied_after_reaped_child_preserves_real_exitcode(monkeypatch):
    class Child:
        pid = 123

        def poll(self):
            return 0

        def wait(self, timeout):
            assert timeout == 3
            return 0

    def denied(*args):
        raise PermissionError("fixture")

    monkeypatch.setattr(harness.os, "killpg", denied)
    harness._stop(Child())


def fake_run(*, baseline=False, status="killed"):
    if baseline:
        return {"passed": True, "status": "passed", "stdout": "baseline fixture", "exitcode": 0}
    return {"passed": False, "status": status, "stdout": "detector fixture", "exitcode": 1 if status == "killed" else 0,
            "detected": status == "killed", "killed_by": [CLEAN_TEST] if status == "killed" else [],
            "reason": "assertion_failure" if status == "killed" else None, "elapsed_seconds": 0.0}


def test_full_plan_manifest_keeps_every_declared_fault_and_explains_survivor(tmp_path, monkeypatch):
    calls = []

    def run(root, source, tests, *, baseline=False, **kwargs):
        calls.append((source, tests, baseline))
        return fake_run(baseline=baseline, status="survived" if len(calls) == 5 else "killed")

    monkeypatch.setattr(harness, "run_detection", run)
    output = tmp_path / "mutations"
    result = harness.run_mutation_suite(ROOT, output)
    plan = harness.load_plan(ROOT)
    assert not result["passed"]
    assert result["baseline"]["passed"]
    assert result["counts"] == {"declared": 12, "killed": 11, "survived": 1, "invalid": 0, "not_run": 0, "timeouts": 0}
    assert [entry["id"] for entry in result["mutants"]] == [spec["id"] for spec in plan["mutants"]]
    assert len(calls) == 13
    assert calls[0][1] == [harness.TEST_PATH]
    for (_, selectors, baseline), spec in zip(calls[1:], plan["mutants"]):
        assert not baseline and selectors == spec["detection_tests"]
    manifest = json.loads((output / "manifest.json").read_text())
    complete = json.loads((output / "COMPLETE.json").read_text())
    assert manifest["summary_sha256"] == sha((output / "summary.json").read_bytes())
    assert complete["sha256"]["frozen-plan.json"] == sha((ROOT / harness.PLAN_PATH).read_bytes())
    assert result["source_snapshot_unchanged"]
    for entry in result["mutants"]:
        assert entry["mutated_source_sha256"] == sha((output / entry["artifacts"]["source"]).read_bytes())
        assert entry["patch_sha256"] == sha((output / entry["artifacts"]["patch"]).read_bytes())


def test_failed_baseline_prevents_mutations_without_dropping_denominator(tmp_path, monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs)
        return {"passed": False, "status": "invalid", "stdout": "fixture import failed", "exitcode": 70}

    monkeypatch.setattr(harness, "run_detection", run)
    result = harness.run_mutation_suite(ROOT, tmp_path / "failed-baseline")
    assert not result["passed"]
    assert len(calls) == 1 and calls[0]["baseline"]
    assert result["counts"]["not_run"] == result["counts"]["declared"] == 12
    assert all(entry["reason"] == "baseline_failed" for entry in result["mutants"])


@pytest.mark.parametrize("kind", ["directory", "dangling_symlink"])
def test_existing_output_and_dangling_symlink_are_rejected_before_execution(tmp_path, monkeypatch, kind):
    output = tmp_path / "existing"
    if kind == "directory":
        output.mkdir()
    else:
        output.symlink_to(tmp_path / "must-not-be-created", target_is_directory=True)

    def unexpected(*args, **kwargs):
        pytest.fail("execution started before exclusive publication validation")

    monkeypatch.setattr(harness, "run_detection", unexpected)
    with pytest.raises(FileExistsError):
        harness.run_mutation_suite(ROOT, output)
    assert not (tmp_path / "must-not-be-created").exists()
