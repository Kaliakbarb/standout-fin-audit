"""The packaged host proxy reaches the image's loopback without a network."""

from __future__ import annotations

import http.client
import importlib.util
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from fin_audit.web import WorkbenchServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("fin_audit_demo", ROOT / "scripts/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)
CID = "a" * 64


@pytest.fixture
def proxy(monkeypatch):
    backend = WorkbenchServer(("127.0.0.1", 0))
    backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
    backend_thread.start()
    calls = []
    original_docker = demo._docker_result

    def execute(command, **kwargs):
        assert command[:3] == ["docker", "exec", "-i"]
        assert command[3] == CID
        assert command[-2] == "8765"
        calls.append(command)
        return original_docker([sys.executable, "-I", "-B", "-c", command[-3],
                                str(backend.server_port), command[-1]], **kwargs)

    monkeypatch.setattr(demo, "_docker_result", execute)
    server = demo.DemoProxy(("127.0.0.1", 0), ["docker"], CID)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, calls
    server.shutdown()
    thread.join(timeout=3)
    server.server_close()
    backend.shutdown()
    backend_thread.join(timeout=3)
    backend.server_close()


def request(server, method, path, value=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=20)
    connection.request(method, path, None if value is None else json.dumps(value),
                       {"Content-Type": "application/json", **(headers or {})})
    response = connection.getresponse()
    result = response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    connection.close()
    return result


def test_actual_bridge_http_assets_lab_policy_run_and_private_boundary(proxy):
    server, calls = proxy
    assert server.server_address[0] == "127.0.0.1"
    for path in ("/", "/style.css", "/app.js", "/lesson.mjs", "/replay.mjs"):
        status, headers, data = request(server, "GET", path)
        assert status == 200 and data and headers["cache-control"] == "no-store"
    status, _, data = request(server, "POST", "/api/sessions", {"case": "masked_both"})
    assert status == 201
    state = json.loads(data)
    assert state["result"] is None and "review" not in state
    token = state["session_id"]
    # Host port is deliberately different from the fixed container port.
    origin = {"Origin": f"http://127.0.0.1:{server.server_port}"}
    status, _, data = request(server, "POST", f"/api/sessions/{token}/arena", {}, origin)
    assert status == 200
    result = json.loads(data)["result"]
    assert result["reference"]["evaluation"]["f1"] == 1
    assert result["reference"]["comparison"]["evaluation"]["fn"] == 2
    assert calls


def test_host_origin_paths_methods_and_size_are_rejected_before_docker_exec(proxy):
    server, calls = proxy
    assert request(server, "GET", "/", headers={"Host": "evil.example"})[0] == 403
    assert request(server, "POST", "/api/sessions", {}, {"Origin": "https://evil.example"})[0] == 403
    assert request(server, "POST", "/api/sessions", {}, {"Content-Type": "text/plain"})[0] == 415
    for path in ("/../private/truth.json", "/api/truth", "/?external=example.com"):
        assert request(server, "GET", path)[0] == 404
    assert request(server, "POST", "/api/sessions", {"huge": "x" * 66000})[0] == 413
    assert calls == []


def test_packaged_proxy_forwards_post_run_repair_without_changing_original_score(proxy):
    server, _ = proxy
    status, _, data = request(server, "POST", "/api/sessions", {"case": "masked_both"})
    assert status == 201
    token = json.loads(data)["session_id"]
    path = f"/api/sessions/{token}"
    flags = {"fix_duplicate": True, "fix_fx": False}
    assert request(server, "POST", path + "/counterfactual", flags)[0] == 409
    _, _, data = request(server, "POST", path + "/submit", {"findings": []})
    original = json.loads(data)
    status, _, data = request(server, "POST", path + "/counterfactual", flags)
    assert status == 200
    assert json.loads(data)["after"] == {"revenue_minor": 50_000_000, "issue_count": 1, "issue_types": ["fx_conversion"]}
    _, _, data = request(server, "POST", path + "/state", {})
    assert json.loads(data) == original


def test_demo_container_has_no_published_port_network_or_mutable_root():
    command = demo.demo_run_command(["docker"], {"architecture": "amd64", "loaded_image_id": "sha256:" + "b" * 64})
    assert "--platform=linux/amd64" in command
    assert "--network=none" in command and "--pull=never" in command and "--read-only" in command
    assert not any(value in {"-p", "--publish", "--network=host", "--privileged"} for value in command)
    assert "--env=FIN_AUDIT_REQUIRE_SECURE=1" in command


@pytest.mark.parametrize("status", [403, 404, 500])
def test_startup_deadline_applies_to_repeated_http_failures(monkeypatch, status):
    now = [0.0]
    monkeypatch.setattr(demo.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(demo.time, "sleep", lambda duration: now.__setitem__(0, now[0] + 10))
    monkeypatch.setattr(demo, "bridge_request", lambda *args, **kwargs: {"status": status})
    with pytest.raises(demo.ReleaseError, match="demo_startup_timeout"):
        demo.wait_backend(["docker"], CID)
    assert now[0] == 30


def test_missing_release_manifest_fails_before_querying_docker(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("demo.py", "release_support.py", "docker_common.py"):
        (scripts / name).write_bytes((ROOT / "scripts" / name).read_bytes())
    result = subprocess.run([sys.executable, "-B", str(scripts / "demo.py"), "--offline"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1 and "Demo failed" in result.stderr
    assert not list(tmp_path.rglob("__pycache__"))
