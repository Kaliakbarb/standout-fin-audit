"""Trusted acceptance budgets must not weaken mutation classification/cleanup."""

import json
import signal
import sys
from pathlib import Path

import pytest

from fin_audit import stage4


def harness_process(monkeypatch, *, timed_out=False):
    calls = {"waits": [], "signals": []}

    class Child:
        pid = 123456789

        def wait(self, timeout=None):
            calls["waits"].append(timeout)
            if timed_out and timeout == 300:
                raise stage4.subprocess.TimeoutExpired("public-harness-fixture", timeout)
            return 0

    def launch(command, **kwargs):
        calls["command"] = command
        calls["options"] = kwargs
        return Child()

    monkeypatch.setattr(stage4.subprocess, "Popen", launch)
    # This fake PID must never reach a real signal syscall.
    monkeypatch.setattr(stage4.os, "killpg", lambda pid, sig: calls["signals"].append((pid, sig)))
    return calls


def assert_owned_harness_cleanup(calls):
    assert calls["waits"] == [300, None]
    assert calls["signals"] == [(123456789, signal.SIGKILL)]
    assert calls["options"]["stdout"].closed


def test_trusted_mutation_acceptance_passes_explicit_budgets_and_preserves_parent_bound(monkeypatch, tmp_path):
    calls = harness_process(monkeypatch)
    (tmp_path / "summary.json").write_text(json.dumps({"passed": True}))
    assert stage4.run_mutation_checks(output=tmp_path) == {"passed": True}
    root = Path(stage4.__file__).resolve().parents[2]
    assert calls["command"] == [sys.executable, str(root / "scripts/mutation_check.py"),
                                "--timeout", "60", "--baseline-timeout", "60", "--output", str(tmp_path.absolute())]
    options = calls["options"]
    assert options["cwd"] == root
    assert options["stdin"] == stage4.subprocess.DEVNULL
    assert options["stderr"] == stage4.subprocess.STDOUT
    assert options["start_new_session"] is True
    assert_owned_harness_cleanup(calls)


def test_zero_exit_cannot_replace_failed_mutation_evidence(monkeypatch, tmp_path):
    calls = harness_process(monkeypatch)
    (tmp_path / "summary.json").write_text(json.dumps({"passed": False}))
    with pytest.raises(RuntimeError, match="mutation_harness_did_not_pass"):
        stage4.run_mutation_checks(output=tmp_path)
    assert_owned_harness_cleanup(calls)


def test_parent_harness_timeout_remains_fatal_and_cleans_owned_process(monkeypatch, tmp_path):
    calls = harness_process(monkeypatch, timed_out=True)
    with pytest.raises(RuntimeError, match="mutation_harness_timeout"):
        stage4.run_mutation_checks(output=tmp_path)
    assert_owned_harness_cleanup(calls)
