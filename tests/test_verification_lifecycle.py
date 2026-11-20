"""Private portable output and cleanup of one exact secure container/volume."""

from __future__ import annotations

import importlib.util
import json
import os
import runpy
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verification_lifecycle_host", ROOT / "scripts/docker_common.py")
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)
CID = "a" * 64


class Docker:
    def __init__(self, fault=None, exit_code=0, attach_code=0):
        self.fault, self.exit_code, self.attach_code = fault, exit_code, attach_code
        self.commands, self.exists, self.removed = [], False, False
        self.state = {"Status": "exited", "Running": False, "ExitCode": exit_code}

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        assert command[:3] == ["docker", "--context", "chosen-engine"]
        operation = command[3]
        if operation == "create":
            self.exists = True
            self.name = command[command.index("--name") + 1]
            self.label = command[command.index("--label") + 1].split("=", 1)
            cidfile = Path(command[command.index("--cidfile") + 1])
            if self.fault == "create-timeout":
                raise subprocess.TimeoutExpired(command, 120)
            if self.fault == "partial-create":
                return SimpleNamespace(returncode=9)
            cidfile.write_text("wrong-id" if self.fault == "invalid-id" else CID)
            return SimpleNamespace(returncode=0)
        if operation == "ps":
            assert "name=^/" + self.name + "$" in command
            assert "label=" + "=".join(self.label) in command
            return SimpleNamespace(returncode=0, stdout=CID + "\n" if self.exists else "")
        if operation == "inspect":
            assert command[-1] == CID
            if "--format" in command:
                return SimpleNamespace(returncode=0, stdout="not json" if self.fault == "bad-json" else json.dumps(self.state))
            value = {"Id": CID, "Name": "/" + self.name, "Config": {"Labels": dict([self.label])}}
            if self.fault == "foreign-owner":
                value["Config"]["Labels"] = {}
            return SimpleNamespace(returncode=0, stdout=json.dumps([value]))
        if operation == "start":
            assert command[-1] == CID and "--attach" in command
            if self.fault == "interrupt":
                raise KeyboardInterrupt
            return SimpleNamespace(returncode=self.attach_code)
        if operation == "cp":
            assert command[4] == CID + ":/output/." and "-a" not in command and "--archive" not in command
            assert kwargs["timeout"] == 300
            if self.fault == "copy-timeout":
                raise subprocess.TimeoutExpired(command, 300)
            if self.fault == "copy":
                return SimpleNamespace(returncode=1)
            target = Path(command[5])
            private = target / "run"
            private.mkdir(mode=0o700)
            result = private / "private.json"
            result.write_text('{"passed": true}\n')
            result.chmod(0o600)
            (target / "tests.xml").write_text("<testsuites/>\n")
            # Simulate a volume root mode copied by Docker. Its parent remains
            # private while copying, and the published host root stays private.
            target.chmod(0o755)
            return SimpleNamespace(returncode=0)
        if operation == "rm":
            assert command[4:] == ["--force", "--volumes", CID]
            self.exists = False
            self.removed = True
            return SimpleNamespace(returncode=1 if self.fault == "cleanup" else 0)
        raise AssertionError(command)


def invoke(tmp_path, monkeypatch, docker):
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    monkeypatch.setattr(host.subprocess, "run", docker)
    result = host.run_secure_container(["docker", "--context", "chosen-engine"], "sha256:fixed", ["python", "verify.py"],
                                      output, cwd=ROOT, platform="linux/amd64")
    return result, output


def test_single_volume_has_exact_existing_security_and_resource_boundaries():
    flags = host.secure_create_flags("linux/amd64")
    assert flags[0] == "create" and "--rm" not in flags
    assert "--network=none" in flags and "--read-only" in flags and "--pull=never" in flags
    assert "--cpus=2" in flags and "--memory=4g" in flags and "--memory-swap=4g" in flags
    assert "--pids-limit=64" in flags and "--cap-drop=ALL" in flags
    assert {item for item in flags if item.startswith("--cap-add=")} == {
        "--cap-add=SYS_CHROOT", "--cap-add=SETUID", "--cap-add=SETGID", "--cap-add=KILL"}
    assert "--security-opt=no-new-privileges=true" in flags
    assert "--tmpfs=/tmp:rw,noexec,nosuid,size=256m" in flags
    assert flags.count("--mount") == 1 and flags[flags.index("--mount") + 1] == "type=volume,target=/output,volume-nocopy"
    assert "--platform=linux/amd64" in flags and not any("type=bind" in item for item in flags)
    with pytest.raises(ValueError, match="platform"):
        host.secure_create_flags("linux/unknown")


@pytest.mark.parametrize("exit_code,attach_code", [(0, 0), (17, 1), (8, 0)])
def test_copies_private_results_and_uses_actual_exit_code_then_removes_owned_volume(tmp_path, monkeypatch, exit_code, attach_code):
    docker = Docker(exit_code=exit_code, attach_code=attach_code)
    actual, output = invoke(tmp_path, monkeypatch, docker)
    assert actual == exit_code and docker.removed
    assert len([command for command in docker.commands if command[3] == "create"]) == 1
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE((output / "run").stat().st_mode) == 0o700
    private = output / "run/private.json"
    assert private.stat().st_uid == os.getuid() and stat.S_IMODE(private.stat().st_mode) == 0o600
    assert json.loads(private.read_text())["passed"] is True
    assert sorted(path.name for path in output.iterdir()) == ["run", "tests.xml"]
    assert [command[3] for command in docker.commands].index("cp") < [command[3] for command in docker.commands].index("rm")


@pytest.mark.parametrize("fault,exception", [
    ("partial-create", RuntimeError), ("create-timeout", subprocess.TimeoutExpired),
    ("invalid-id", RuntimeError), ("interrupt", KeyboardInterrupt), ("copy", RuntimeError),
    ("copy-timeout", subprocess.TimeoutExpired), ("bad-json", RuntimeError),
])
def test_partial_creation_error_timeout_and_control_c_all_cleanup_exact_owned_container(tmp_path, monkeypatch, fault, exception):
    docker = Docker(fault=fault)
    with pytest.raises(exception):
        invoke(tmp_path, monkeypatch, docker)
    assert docker.removed
    assert list((tmp_path / "output").iterdir()) == []
    removals = [command for command in docker.commands if command[3] == "rm"]
    assert removals == [["docker", "--context", "chosen-engine", "rm", "--force", "--volumes", CID]]


@pytest.mark.parametrize("state", [None, [], {}, {"Status": "running", "Running": True, "ExitCode": 0},
                                  {"Status": "exited", "Running": False, "ExitCode": False},
                                  {"Status": "exited", "Running": False, "ExitCode": "0"},
                                  {"Status": "exited", "Running": False, "ExitCode": -1},
                                  {"Status": "exited", "Running": False, "ExitCode": 256}])
def test_corrupt_or_nonterminal_exit_state_never_counts_as_success(tmp_path, monkeypatch, state):
    docker = Docker()
    docker.state = state
    with pytest.raises(RuntimeError, match="exit status"):
        invoke(tmp_path, monkeypatch, docker)
    assert docker.removed and not any(command[3] == "cp" for command in docker.commands)


def test_attach_transport_failure_with_zero_container_status_is_failure(tmp_path, monkeypatch):
    docker = Docker(attach_code=1)
    with pytest.raises(RuntimeError, match="attach failed"):
        invoke(tmp_path, monkeypatch, docker)
    assert docker.removed and (tmp_path / "output/tests.xml").exists()


def test_cleanup_failure_never_reports_success(tmp_path, monkeypatch):
    docker = Docker(fault="cleanup")
    with pytest.raises(RuntimeError, match="remove owned"):
        invoke(tmp_path, monkeypatch, docker)


def test_foreign_ownership_is_never_removed(tmp_path, monkeypatch):
    docker = Docker(fault="foreign-owner")
    with pytest.raises(RuntimeError, match="ownership"):
        invoke(tmp_path, monkeypatch, docker)
    assert not docker.removed and not any(command[3] == "rm" for command in docker.commands)


def test_world_readable_output_is_rejected_before_docker(tmp_path, monkeypatch):
    output = tmp_path / "output"
    output.mkdir(mode=0o755)
    monkeypatch.setattr(host.subprocess, "run", lambda *args, **kwargs: pytest.fail("Docker must not be called"))
    with pytest.raises(ValueError, match="empty private"):
        host.run_secure_container(["docker"], "image", [], output, cwd=ROOT)


def test_verify_timeout_is_bounded_and_does_not_print_private_command(tmp_path, monkeypatch, capsys):
    secret = "UNPUBLISHED-CONTROL-SEED"

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(["docker", "create", "--master-seed", secret], 120)

    module = ModuleType("docker_common")
    module.IMAGE = "test:image"
    module.docker_command = lambda context: ["docker"]
    module.load_or_create_master_seed = lambda path: 1
    module.run_secure_container = timeout
    monkeypatch.setitem(sys.modules, "docker_common", module)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(sys, "argv", [str(ROOT / "scripts/verify"), "--stage", "1", "--output", str(tmp_path / "results")])
    with pytest.raises(SystemExit) as outcome:
        runpy.run_path(str(ROOT / "scripts/verify"), run_name="__main__")
    assert outcome.value.code == 1
    error = capsys.readouterr().err
    assert "timed out" in error and secret not in error and "Traceback" not in error
    assert stat.S_IMODE((tmp_path / "results").stat().st_mode) == 0o700
