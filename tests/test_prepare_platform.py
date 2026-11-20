"""Target selection must not silently build a host-architecture release."""

import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def prepare(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    loader = importlib.machinery.SourceFileLoader("test_prepare_entry", str(ROOT / "scripts/prepare"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.mark.parametrize("architecture,expected", [("amd64", "linux/amd64"), ("x86_64", "linux/amd64"),
                                                   ("arm64", "linux/arm64"), ("aarch64", "linux/arm64")])
def test_daemon_aliases_normalize_without_using_host_architecture(prepare, monkeypatch, architecture, expected):
    import docker_common
    monkeypatch.setattr(docker_common.subprocess, "check_output", lambda *args, **kwargs: f"linux/{architecture}\n")
    assert prepare.daemon_platform(["docker", "--context", "chosen"]) == expected


def test_explicit_target_is_built_and_validated_before_export(prepare, monkeypatch, tmp_path):
    import docker_common
    calls = []
    docker = ["docker", "--context", "chosen"]
    monkeypatch.setattr(prepare, "docker_command", lambda context: docker)
    monkeypatch.setattr(prepare.subprocess, "run", lambda command, **kwargs: calls.append(command))
    monkeypatch.setattr(docker_common.subprocess, "check_output", lambda *args, **kwargs: json.dumps({
        "Id": "sha256:actual", "Os": "linux", "Architecture": "amd64"}))
    archive = tmp_path / "new-image.tar"
    prepare.main(["--context", "chosen", "--platform", "linux/amd64", "--export-image", str(archive)])
    assert calls[0][:len(docker)] == docker
    assert ["--platform", "linux/amd64"] == calls[0][len(docker)+1:len(docker)+3]
    assert calls[-1] == docker + ["image", "save", "-o", str(archive), prepare.IMAGE]


def test_wrong_target_image_is_never_exported(prepare, monkeypatch, tmp_path):
    import docker_common
    calls = []
    monkeypatch.setattr(prepare, "docker_command", lambda context: ["docker"])
    monkeypatch.setattr(prepare.subprocess, "run", lambda command, **kwargs: calls.append(command))
    monkeypatch.setattr(docker_common.subprocess, "check_output", lambda *args, **kwargs: json.dumps({
        "Id": "sha256:wrong", "Os": "linux", "Architecture": "arm64"}))
    with pytest.raises(SystemExit) as error:
        prepare.main(["--platform", "linux/amd64", "--export-image", str(tmp_path / "image.tar")])
    assert error.value.code == 1
    assert len(calls) == 1 and calls[0][1] == "build"


def test_existing_archive_is_rejected_before_build(prepare, monkeypatch, tmp_path):
    archive = tmp_path / "old.tar"
    archive.write_bytes(b"preserved")
    monkeypatch.setattr(prepare, "docker_command", lambda context: pytest.fail("must not contact Docker"))
    with pytest.raises(SystemExit):
        prepare.main(["--export-image", str(archive)])
    assert archive.read_bytes() == b"preserved"


def test_versioned_tag_comes_from_project_metadata(prepare):
    import tomllib
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert prepare.IMAGE == f"genesis-fin-audit:release-{version}"
