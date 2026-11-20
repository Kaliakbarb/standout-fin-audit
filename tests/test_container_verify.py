"""Whole-container measurements and test/application failure propagation."""

from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

path = Path(__file__).resolve().parents[1] / "scripts/container_verify.py"
spec = importlib.util.spec_from_file_location("fin_audit_container_verify", path)
assert spec and spec.loader
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


def test_cgroup_v2_peak_limits_events_and_missing_optional_counters(tmp_path):
    files = {"cgroup.controllers": "cpu memory pids", "memory.peak": "1234567\n",
             "memory.current": "765432", "memory.max": "4294967296", "memory.swap.max": "0",
             "cpu.max": "200000 100000", "cpu.stat": "usage_usec 123\nnr_throttled 4\n",
             "memory.events": "oom 0\noom_kill 0\n", "pids.current": "4", "pids.max": "64"}
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    result = entry.cgroup_metrics(tmp_path)
    assert result["cgroup_v2"] is True
    assert result["values"]["memory.peak"] == 1234567
    assert result["values"]["memory.max"] == 4294967296
    assert result["values"]["memory.events"] == {"oom": 0, "oom_kill": 0}
    assert result["values"]["cpu.max"] == "200000 100000"
    assert result["values"]["cpu.stat"] == {"usage_usec": 123, "nr_throttled": 4}
    assert "pids.peak" in result["unavailable"]


@pytest.mark.parametrize("codes,expected,app_code", [([9], 9, None), ([0, 7], 7, 7), ([0, 0], 0, 0)])
def test_entry_preserves_real_failures_and_collects_metrics_after_children_exit(tmp_path, monkeypatch, codes, expected, app_code):
    outcomes = iter(codes)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=next(outcomes))

    def collect():
        assert len(calls) == len(codes)
        return {"scope": "unit-fixture", "values": {"memory.peak": 1000}}

    monkeypatch.setattr(entry.subprocess, "run", run)
    monkeypatch.setattr(entry, "cgroup_metrics", collect)
    assert entry.main(["--stage", "5", "--output", str(tmp_path), "--master-seed", "123"]) == expected
    metrics_path = tmp_path / "cgroup-metrics.json"
    metrics = json.loads(metrics_path.read_text())
    assert metrics["pytest_exitcode"] == codes[0]
    assert metrics["application_exitcode"] == app_code
    assert metrics["passed"] is (expected == 0)
    assert stat.S_IMODE(metrics_path.stat().st_mode) == 0o600
    if len(codes) == 2:
        assert calls[1][-2:] == ["--master-seed", "123"]
        assert "secure" in calls[1]
