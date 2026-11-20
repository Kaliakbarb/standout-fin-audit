"""Actual loopback HTTP and secure-worker smoke for the shipped frontend.

This executes inside the same offline container as stage five. Browser scripts
are shipped and hashed; JavaScript behavior has its separate development QA.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import threading
import time

from .contracts import ISSUE_TYPES, validate_public_privacy
from .web import WorkbenchServer


def run_web_readiness(*, profile: str) -> dict:
    started = time.monotonic()
    server = WorkbenchServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    checks, assets, runs = {}, {}, []

    def request(method: str, path: str, payload=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=150)
        body = None if payload is None else json.dumps(payload)
        connection.request(method, path, body, {"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        data = response.read(2 * 1024 * 1024)
        result = response.status, dict(response.getheaders()), data
        connection.close()
        return result

    def post(path, payload):
        status, _, data = request("POST", path, payload)
        if status not in {200, 201}:
            raise ValueError("web_readiness_http_action_failed")
        return json.loads(data)

    try:
        for name in ("/", "/app.js", "/style.css", "/lesson.mjs", "/replay.mjs"):
            status, headers, data = request("GET", name)
            assets[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                            "content_type": headers.get("Content-Type")}
            checks["asset:" + name] = status == 200 and len(data) > 0
        checks["loopback_bind_only"] = server.server_address[0] == "127.0.0.1"
        checks["workspace_files_not_served"] = all(request("GET", path)[0] == 404 for path in
                                                  ("/private/truth.json", "/../pyproject.toml", "/release-manifest.json"))
        checks["foreign_origin_rejected"] = request("POST", "/api/sessions", {"case": "clean"},
                                                    {"Origin": "https://example.invalid"})[0] == 403
        checks["foreign_host_rejected"] = request("GET", "/", headers={"Host": "example.invalid"})[0] == 403
        checks["invalid_lab_settings_rejected"] = request("POST", "/api/sessions", {"case": "generated", "families": ["unknown"]})[0] == 400
        first_generated = None
        for case, settings, expected_count in (
                ("masked_both", {}, 2),
                ("generated", {"families": []}, 0),
                ("generated", {"families": list(ISSUE_TYPES)}, 4)):
            state = post("/api/sessions", {"case": case, **settings})
            validate_public_privacy(state["public"])
            if state["result"] is not None or set(state) != {"session_id", "public", "remaining_steps", "events", "result"}:
                raise ValueError("web_readiness_nonterminal_privacy_failed")
            token = state["session_id"]
            stable = post(f"/api/sessions/{token}/state", {})
            if stable != state:
                raise ValueError("web_readiness_resume_changed_episode")
            if case == "generated":
                identifier = state["public"]["manifest"]["episode_id"]
                if first_generated is None:
                    first_generated = identifier
                elif identifier == first_generated:
                    raise ValueError("web_readiness_generator_reused_episode")
            completed = post(f"/api/sessions/{token}/arena", {})
            result = completed["result"]
            reference = result["reference"]
            baseline = reference["comparison"]
            ordinary, silent = reference["evaluation"], baseline["evaluation"]
            if (reference["profile"] != profile or ordinary["f1"] != 1
                    or ordinary["tp"] != expected_count or ordinary["fp"] != 0 or ordinary["fn"] != 0
                    or len(result["review"]) != expected_count or any(not item["found"] for item in result["review"])
                    or baseline["policy"] != "silent" or silent["fn"] != expected_count
                    or silent["f1"] != (1 if expected_count == 0 else 0)):
                raise ValueError("web_readiness_actual_worker_result_failed")
            both = (reference["trajectory"], baseline["trajectory"])
            if any(row["reward"] != 0 or row["info"] != {} for trajectory in both for row in trajectory):
                raise ValueError("web_readiness_closed_feedback_unmasked")
            if request("POST", f"/api/sessions/{token}/arena", {})[0] != 409:
                raise ValueError("web_readiness_terminal_result_mutable")
            runs.append({"case": case, "families": expected_count, "profile": reference["profile"],
                         "simple": ordinary, "silent": silent,
                         "public_sha256": hashlib.sha256(json.dumps(state["public"], sort_keys=True,
                             separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(),
                         "closed_trajectory_rows": sum(map(len, both))})
        checks.update(actual_masked_two_policy_run=True, actual_fresh_four_family_run=True,
                      actual_clean_company_run=True, neutral_closed_feedback=True,
                      no_private_answers_before_terminal=True, immutable_terminal_answers=True,
                      worker_profile_matches_verification=True, randomized_lab_episodes_distinct=True)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    checks["http_server_thread_stopped"] = not thread.is_alive()
    return {"scope": "packaged-web-assets-and-real-loopback-api; browser QA is separate",
            "profile": profile, "bind_host": "127.0.0.1", "checks": checks,
            "passed": all(checks.values()), "assets": assets, "runs": runs,
            "ordinary_worker_executions": 6, "elapsed_seconds": time.monotonic() - started}
