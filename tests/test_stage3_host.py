"""Host-side repeatability and private state; these do not require a Docker daemon."""

from __future__ import annotations

import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

_path = Path(__file__).resolve().parents[1] / "scripts" / "docker_common.py"
_spec = importlib.util.spec_from_file_location("fin_audit_docker_common_test", _path)
assert _spec and _spec.loader
host = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(host)


def test_control_seed_created_privately_and_reused_after_concurrent_launches(tmp_path):
    path = tmp_path / "state" / "seed.json"
    with ThreadPoolExecutor(max_workers=8) as pool:
        seeds = list(pool.map(lambda _: host.load_or_create_master_seed(path), range(16)))
    assert len(set(seeds)) == 1
    assert 0 <= seeds[0] < 2**128
    assert host.load_or_create_master_seed(path) == seeds[0]
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(path.parent.iterdir()) == [path]


def test_control_seed_does_not_follow_symlink_or_silently_repair_permissions(tmp_path):
    original = tmp_path / "original"
    host.load_or_create_master_seed(original)
    link = tmp_path / "link"
    link.symlink_to(original)
    with pytest.raises(OSError):
        host.load_or_create_master_seed(link)
    original.chmod(0o644)
    with pytest.raises(ValueError, match="private regular"):
        host.load_or_create_master_seed(original)
    assert original.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("receipt", [
    {}, [], {"version": True, "master_seed": 1}, {"version": 1, "master_seed": True},
    {"version": 1, "master_seed": -1}, {"version": 1, "master_seed": 2**128},
    {"version": 1, "master_seed": "123"}, {"version": 1, "master_seed": 1, "unexpected": 0},
])
def test_control_seed_corruption_fails_without_changing_receipt(tmp_path, receipt):
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(receipt))
    path.chmod(0o600)
    original = path.read_bytes()
    with pytest.raises(ValueError):
        host.load_or_create_master_seed(path)
    assert path.read_bytes() == original


def test_control_seed_oversized_input_rejected_before_json_parse(tmp_path):
    path = tmp_path / "seed.json"
    path.write_text(" " * 1025)
    path.chmod(0o600)
    with pytest.raises(ValueError, match="Invalid control"):
        host.load_or_create_master_seed(path)


@pytest.mark.parametrize("command", ["verify", "datasets"])
def test_cli_export_destination_cannot_be_a_dangling_symlink(tmp_path, command):
    from fin_audit.cli import main

    target = tmp_path / "target"
    link = tmp_path / "requested"
    link.symlink_to(target, target_is_directory=True)
    arguments = (["verify", "--stage", "3", "--output", str(link)] if command == "verify"
                 else ["datasets", "export", "--directory", str(link)])
    with pytest.raises(SystemExit) as error:
        main(arguments)
    assert error.value.code == 1
    assert link.is_symlink() and not target.exists()


def test_explicit_docker_host_failure_never_falls_back_to_working_project_engine(monkeypatch):
    from types import SimpleNamespace

    calls = []
    monkeypatch.setenv("DOCKER_HOST", "unix:///missing-clean-daemon.sock")
    monkeypatch.delenv("FIN_AUDIT_DOCKER_CONTEXT", raising=False)
    monkeypatch.setattr(host.shutil, "which", lambda name: "/fake/docker")

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(host.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="explicit DOCKER_HOST"):
        host.docker_command()
    assert calls == [["docker", "info"]]
