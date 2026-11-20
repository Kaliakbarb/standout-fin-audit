"""The packaged launcher cannot downgrade a missing manifest to dev mode."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("marker", ["missing", "dangling"])
def test_forced_release_rejects_missing_manifest_before_touching_docker(tmp_path, marker):
    bundle = tmp_path / "bundle"
    scripts = bundle / "scripts"
    scripts.mkdir(parents=True)
    for name in ("verify", "docker_common.py", "release_support.py"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    if marker == "dangling":
        (bundle / "release-manifest.json").symlink_to(bundle / "missing.json")
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    docker.write_text('#!/bin/sh\nprintf called > "$FIN_AUDIT_DOCKER_MARKER"\nexit 0\n')
    docker.chmod(0o755)
    called = tmp_path / "docker-called"
    environment = dict(os.environ, PATH=str(binary), FIN_AUDIT_DOCKER_MARKER=str(called))
    result = subprocess.run([sys.executable, "-B", str(scripts / "verify"), "--release-bundle", "--offline"],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "Verification failed:" in result.stderr
    assert not called.exists()
    assert not list(bundle.rglob("__pycache__"))
