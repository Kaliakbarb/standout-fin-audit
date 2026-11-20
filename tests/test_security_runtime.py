"""Real Linux sandbox checks, skipped only outside the prepared runtime.

The image sets FIN_AUDIT_REQUIRE_SECURE=1, turning missing sandbox prerequisites
into a failure. A local macOS pytest run cannot claim these checks passed.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "src/fin_audit/sandbox_launcher.py"
SENTINEL = Path("/private/fin_audit_sentinel.json")


@pytest.fixture(autouse=True)
def secure_runtime():
    available = (
        sys.platform == "linux"
        and os.geteuid() == 0
        and Path("/sandbox/usr/local/bin/python3.11").is_file()
        and SENTINEL.is_file()
    )
    if not available:
        if os.environ.get("FIN_AUDIT_REQUIRE_SECURE") == "1":
            pytest.fail("Required real Linux sandbox is unavailable; refusing secure skip")
        pytest.skip("Real sandbox checks require the prepared Linux container")


def start_message():
    return {
        "protocol_version": "1.0",
        "observation": {
            "manifest": {"episode_id": "public_security_probe"},
            "policy": {},
            "remaining_steps": 100,
        },
        "info": {},
    }


def response(*, done=False):
    return {
        "observation": {
            "result": {"records": [], "next_cursor": None},
            "remaining_steps": 99,
        },
        "reward": 0,
        "terminated": done,
        "truncated": False,
        "info": {},
    }


def test_actual_worker_cannot_cross_files_process_environment_or_network_boundary():
    # Pass a real protected descriptor and secret env on purpose. The launcher,
    # not the test's Popen defaults, must close/sanitize them before policy code.
    with SENTINEL.open("rb") as protected:
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", str(LAUNCHER), "probe"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            pass_fds=(protected.fileno(),),
            env={**os.environ, "FIN_AUDIT_SEED": "must-not-reach-worker"},
        )
        messages = [start_message(), response(), response(done=True)]
        try:
            # This positive boundary probe uses the supported episode budget;
            # short startup/import timing is not the security property tested.
            stdout, stderr = process.communicate(
                "".join(json.dumps(message) + "\n" for message in messages), timeout=60
            )
        finally:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=3)
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
    assert process.returncode == 0, stderr
    reports = [line.removeprefix("ISOLATION_PROBE=") for line in stderr.splitlines() if line.startswith("ISOLATION_PROBE=")]
    assert len(reports) == 1, stderr
    checks = json.loads(reports[0])
    assert len(checks) >= 16
    failed = [name for name, passed in checks.items() if passed is not True]
    assert not failed, failed
    actions = [json.loads(line) for line in stdout.splitlines()]
    assert [request["action"] for request in actions] == ["list_records", "submit"]
    assert "must-not-reach-worker" not in stdout + stderr
    assert SENTINEL.is_file()


def test_trusted_parent_can_kill_a_worker_after_its_uid_has_changed():
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", str(LAUNCHER), "silent"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        dropped = False
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            status = Path(f"/proc/{process.pid}/status").read_text()
            uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
            if uid_line.split()[1:] == ["65534"] * 4:
                dropped = True
                break
            time.sleep(0.01)
        assert dropped, "Worker did not enter its unprivileged identity"
        os.kill(process.pid, signal.SIGKILL)
        assert process.wait(timeout=2) == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def test_launcher_fails_closed_for_an_unprivileged_caller():
    def drop_identity():
        os.setgroups([])
        os.setresgid(65534, 65534, 65534)
        os.setresuid(65534, 65534, 65534)

    completed = subprocess.run(
        [sys.executable, "-I", "-S", str(LAUNCHER), "silent"],
        input="",
        text=True,
        capture_output=True,
        timeout=5,
        preexec_fn=drop_identity,
    )
    assert completed.returncode == 78
    assert completed.stdout == ""
    assert completed.stderr.strip() == "secure_runtime_unavailable"


def test_agent_filesystem_contains_runtime_but_no_financial_application_or_private_data():
    root = Path("/sandbox")
    assert (root / "agent_worker.py").is_file()
    assert not (root / "private").exists()
    assert not (root / "proc").exists()
    assert not (root / "app").exists()
    assert not (root / "bin/sh").exists()
    assert not (root / "usr/local/lib/python3.11/site-packages").exists()
    assert not list(root.rglob("fin_audit"))
    assert all(not path.stat().st_mode & 0o222 for path in [root, *root.rglob("*")])


def test_secure_marker_never_allows_missing_runtime_to_be_reported_as_a_skip():
    assert os.environ.get("FIN_AUDIT_REQUIRE_SECURE") == "1"
