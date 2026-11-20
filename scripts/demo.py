#!/usr/bin/env python3
"""Serve the sealed demo locally through Docker exec, with no container network.

The host runs only this stdlib proxy. The app and its actual secure workers use
the verified image. No port is published and no bridge network is created.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
from release_support import ReleaseError, _docker_result, load_verified_image

ROOT = Path(__file__).resolve().parents[1]

MAX_REQUEST = 65_536
MAX_RESPONSE = 2 * 1024 * 1024
ASSET_PATHS = {"/", "/app.js", "/style.css", "/lesson.mjs", "/replay.mjs", "/api/cases"}
API_PATH = re.compile(r"/api/sessions(?:/[A-Za-z0-9_-]{16,64}/(?:state|document|submit|reference|arena|counterfactual))?\Z")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")

_CLIENT = r'''import base64,http.client,json,sys
data=json.loads(sys.stdin.buffer.read(100000))
connection=http.client.HTTPConnection('127.0.0.1',int(sys.argv[1]),timeout=float(sys.argv[2]))
body=base64.b64decode(data['body'],validate=True)
headers={'Host':'127.0.0.1:'+sys.argv[1]}
if data['method']=='POST':headers.update({'Content-Type':'application/json','Origin':'http://127.0.0.1:'+sys.argv[1]})
connection.request(data['method'],data['path'],body=body or None,headers=headers)
response=connection.getresponse(); payload=response.read(2097153)
if len(payload)>2097152:raise ValueError('demo_response_limit')
allowed={'content-type','cache-control','content-security-policy','x-content-type-options'}
result={'status':response.status,'headers':{key.lower():value for key,value in response.getheaders() if key.lower() in allowed},'body':base64.b64encode(payload).decode()}
connection.close();print(json.dumps(result))
'''


def bridge_request(docker: list[str], container: str, method: str, path: str,
                   body: bytes = b"", *, backend_port: int = 8765, timeout: float = 130) -> dict:
    if not CONTAINER_ID.fullmatch(container) or not 1 <= backend_port <= 65535:
        raise ReleaseError("invalid_demo_container")
    request = json.dumps({"method": method, "path": path, "body": base64.b64encode(body).decode()}).encode()
    # A bounded stdin payload never appears in shell arguments or process lists.
    command = docker + ["exec", "-i", container, "python", "-I", "-B", "-c", _CLIENT, str(backend_port), str(timeout)]
    result = _docker_result(command, input_data=request, maximum=3 * 1024 * 1024, timeout=timeout + 2)
    try:
        value = json.loads(result)
        if set(value) != {"status", "headers", "body"} or type(value["status"]) is not int or not 100 <= value["status"] <= 599:
            raise ValueError()
        payload = base64.b64decode(value["body"], validate=True)
        headers = value["headers"]
        if (len(payload) > MAX_RESPONSE or not isinstance(headers, dict)
                or any(key not in {"content-type", "cache-control", "content-security-policy", "x-content-type-options"}
                       or type(item) is not str or "\r" in item or "\n" in item for key, item in headers.items())):
            raise ValueError()
    except (ValueError, TypeError, KeyError) as error:
        raise ReleaseError("invalid_demo_bridge_response") from error
    return {"status": value["status"], "headers": headers, "body": payload}


class DemoProxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], docker: list[str], container: str):
        self.docker, self.container = docker, container
        self.requests = threading.BoundedSemaphore(4)
        super().__init__(address, ProxyHandler)


class ProxyHandler(BaseHTTPRequestHandler):
    server: DemoProxy

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(15)

    def error(self, status: int, text: str) -> None:
        self.send_payload(status, json.dumps({"error": text}, ensure_ascii=False).encode(),
                          {"content-type": "application/json; charset=utf-8", "cache-control": "no-store"})

    def send_payload(self, status: int, body: bytes, headers: dict) -> None:
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def proxy(self) -> None:
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if (self.headers.get("Host") not in allowed or self.headers.get("Origin") is not None
                and self.headers["Origin"] not in {"http://" + item for item in allowed}):
            self.error(403, "Откройте демо по локальному адресу сервера.")
            return
        if len(self.path) > 2048 or urlsplit(self.path).query or urlsplit(self.path).fragment:
            self.error(404, "Действие не найдено.")
            return
        path = urlsplit(self.path).path
        if ((self.command == "GET" and path not in ASSET_PATHS)
                or (self.command == "POST" and not API_PATH.fullmatch(path))):
            self.error(404, "Действие не найдено.")
            return
        body = b""
        if self.command == "POST":
            if self.headers.get_content_type() != "application/json":
                self.error(415, "Нужен JSON-запрос.")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 < length <= MAX_REQUEST:
                self.error(413, "Недопустимый размер запроса.")
                return
            try:
                body = self.rfile.read(length)
            except TimeoutError:
                self.error(408, "Запрос не был передан вовремя.")
                return
            if len(body) != length:
                self.error(400, "Неполный запрос.")
                return
        if not self.server.requests.acquire(blocking=False):
            self.error(429, "Подождите завершения текущего действия.")
            return
        try:
            result = bridge_request(self.server.docker, self.server.container, self.command, path, body)
            self.send_payload(result["status"], result["body"], result["headers"])
        except (ReleaseError, OSError):
            self.error(502, "Демо-контейнер не ответил. Перезапустите ./demo.")
        finally:
            self.server.requests.release()

    do_GET = proxy
    do_POST = proxy

    def log_message(self, format: str, *args) -> None:
        pass


def demo_run_command(docker: list[str], runtime: dict) -> list[str]:
    return docker + ["run", "--detach", "--pull=never", "--platform=linux/" + runtime["architecture"],
                     "--network=none", "--read-only", "--cpus=2", "--memory=4g", "--memory-swap=4g",
                     "--pids-limit=64", "--cap-drop=ALL", "--cap-add=SYS_CHROOT", "--cap-add=SETUID",
                     "--cap-add=SETGID", "--cap-add=KILL", "--security-opt=no-new-privileges=true",
                     "--tmpfs=/tmp:rw,noexec,nosuid,size=256m", "--env=PYTHONDONTWRITEBYTECODE=1",
                     "--env=FIN_AUDIT_REQUIRE_SECURE=1", runtime["loaded_image_id"],
                     "python", "-m", "fin_audit.web", "--port", "8765"]


def wait_backend(docker: list[str], container: str) -> None:
    deadline = time.monotonic() + 30
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReleaseError("demo_startup_timeout")
        try:
            if bridge_request(docker, container, "GET", "/api/cases", timeout=min(3, remaining))["status"] == 200:
                return
        except ReleaseError:
            pass
        if time.monotonic() >= deadline:
            raise ReleaseError("demo_startup_timeout")
        time.sleep(0.2)


def run_demo(bundle: Path, docker: list[str], port: int) -> None:
    if not 1 <= port <= 65535:
        raise ReleaseError("invalid_demo_port")
    manifest = load_verified_image(bundle, docker)
    runtime = {**manifest["runtime"], "loaded_image_id": manifest["loaded_image_id"]}
    container = None
    try:
        container = _docker_result(demo_run_command(docker, runtime), maximum=1024).decode().strip()
        if not CONTAINER_ID.fullmatch(container):
            raise ReleaseError("invalid_demo_container_id")
        wait_backend(docker, container)
        inspected = json.loads(_docker_result(docker + ["inspect", container], maximum=65536))[0]
        host = inspected.get("HostConfig", {})
        if (inspected.get("Image") != runtime["loaded_image_id"] or host.get("NetworkMode") != "none"
                or host.get("PortBindings") or inspected.get("NetworkSettings", {}).get("Ports")
                or host.get("ReadonlyRootfs") is not True):
            raise ReleaseError("demo_container_network_or_identity_mismatch")
        with DemoProxy(("127.0.0.1", port), docker, container) as server:
            print(f"Genesis Audit Lab: http://127.0.0.1:{server.server_port}/", flush=True)
            print("Проверенный образ; контейнер без сети; серверы только localhost. Ctrl+C — остановить.", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if container is not None and CONTAINER_ID.fullmatch(container):
            _docker_result(docker + ["rm", "--force", container], maximum=4096, timeout=30)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-bundle", action="store_true", help="The immutable manifest is always required.")
    parser.add_argument("--offline", action="store_true", help="Explicit offline mode; the container always has network=none.")
    parser.add_argument("--context")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        # The manifest is checked before any Docker operation, including info.
        from release_support import verify_bundle
        verify_bundle(ROOT)
        from docker_common import docker_command
        run_demo(ROOT, docker_command(args.context), args.port)
    except (ReleaseError, ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Demo failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
