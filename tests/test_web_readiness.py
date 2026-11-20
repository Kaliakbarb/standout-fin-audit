"""Release readiness executes the actual HTTP lifecycle and policy processes."""

from fin_audit.web import worker_profile
from fin_audit.web_readiness import run_web_readiness


def test_real_offline_web_readiness_assets_lab_closed_workers_and_cleanup():
    result = run_web_readiness(profile=worker_profile())
    assert result["passed"] and all(result["checks"].values())
    assert result["ordinary_worker_executions"] == 6
    assert set(result["assets"]) == {"/", "/app.js", "/style.css", "/lesson.mjs", "/replay.mjs"}
    assert [item["families"] for item in result["runs"]] == [2, 0, 4]
    assert result["checks"]["http_server_thread_stopped"]
