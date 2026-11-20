"""Real showcase lifecycle and failure publication, separate from data math."""

from __future__ import annotations

import json
import shutil
import stat

import pytest

from fin_audit import stage4


@pytest.fixture(autouse=True)
def clean_outputs(tmp_path):
    yield
    shutil.rmtree(tmp_path, ignore_errors=True)


def test_actual_showcase_waits_for_all_workers_and_records_real_closed_trajectories(tmp_path, monkeypatch):
    original = stage4.run_episode
    output = tmp_path / "showcase"
    calls = []

    def observe(*args, **kwargs):
        calls.append(kwargs["policy"])
        assert not output.exists()
        for staging in tmp_path.glob(".showcase-staging-*"):
            assert not (staging / "analysis.json").exists()
            assert not (staging / "summary.json").exists()
            assert not (staging / "COMPLETE.json").exists()
        return original(*args, **kwargs)

    monkeypatch.setattr(stage4, "run_episode", observe)
    # Source freeze is independently enforced by the real acceptance command;
    # keep this worker/publication regression independent of parallel dev edits.
    monkeypatch.setattr(stage4, "source_hashes", lambda root: {"test-source": "a" * 64})
    summary = stage4.run_showcase(profile="cooperative", output=output)
    assert summary["passed"] is True, summary["checks"]
    assert len(calls) == 27
    assert summary["execution_counts"] == {"run_episode_calls": 27, "ordinary_workers": 18, "trusted_oracles": 9}
    assert json.loads((output / "summary.json").read_text()) == summary
    evaluation = json.loads((output / "evaluation.json").read_text())
    assert evaluation == summary["runs"]
    run_ids = {row["trajectory_run_id"] for row in evaluation}
    assert len(run_ids) == 27
    seen = set()
    for line in (output / "trajectories.jsonl").read_text().splitlines():
        row = json.loads(line)
        assert row["reward"] == 0 and row["info"] == {}
        assert row["run_id"] in run_ids
        seen.add(row["run_id"])
    assert seen == run_ids
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    for name in ("analysis.json", "summary.json", "evaluation.json", "run-manifest.json"):
        assert stat.S_IMODE((output / name).stat().st_mode) == 0o600


@pytest.mark.parametrize("call_to_fail", [1, 27])
def test_interrupted_real_showcase_never_publishes_success(tmp_path, monkeypatch, call_to_fail):
    original = stage4.run_episode
    calls = 0

    def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == call_to_fail:
            raise RuntimeError("controlled_showcase_failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(stage4, "run_episode", fail)
    output = tmp_path / "showcase"
    with pytest.raises(RuntimeError, match="controlled_showcase_failure"):
        stage4.run_showcase(profile="cooperative", output=output)
    assert not output.exists()
    assert not list(tmp_path.glob(".showcase-staging-*"))
    receipts = list(tmp_path.glob(".showcase-FAILED-*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["completed"] is False
    assert stat.S_IMODE(receipts[0].stat().st_mode) == 0o600


def test_existing_destination_and_dangling_symlink_are_not_replaced(tmp_path):
    output = tmp_path / "showcase"
    output.mkdir()
    marker = output / "user-file"
    marker.write_text("keep")
    with pytest.raises(FileExistsError):
        stage4.run_showcase(profile="cooperative", output=output)
    assert marker.read_text() == "keep"
    link = tmp_path / "link"
    target = tmp_path / "target"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(FileExistsError):
        stage4.run_showcase(profile="cooperative", output=link)
    assert link.is_symlink() and not target.exists()
