"""Exercise human/API boundaries and actual reference-policy scoring."""

import http.client
import hashlib
import json
import threading

import pytest

from fin_audit.contracts import PRIVATE_PUBLIC_KEYS, canonical_json, validate_public_privacy
from fin_audit.web import CASE_INFO, WebError, Workbench, WorkbenchServer


@pytest.fixture
def workbench():
    return Workbench()


@pytest.mark.parametrize("case", [item["id"] for item in CASE_INFO])
def test_public_view_and_reference_process(workbench, case):
    initial = workbench.create(case)
    validate_public_privacy(initial["public"])
    assert initial["result"] is None
    assert initial["remaining_steps"] == 100 - len(initial["events"])
    assert len(initial["events"]) >= 4
    assert not (PRIVATE_PUBLIC_KEYS & initial.keys())
    token = initial["session_id"]
    assert workbench.get(token).bundle.provenance["public_sha256"] == hashlib.sha256(canonical_json(initial["public"]).encode()).hexdigest()
    result = workbench.act(token, "reference", {})["result"]
    reference = result["reference"]
    assert reference["policy"] == "simple"
    assert reference["evaluation"]["f1"] == 1
    assert reference["evaluation"]["fp"] == 0
    assert reference["evaluation"]["total_steps"] == 101 - initial["remaining_steps"]
    assert result["evaluation"]["f1"] == 1
    assert all(item["found"] for item in result["review"])
    assert len(result["review"]) == (0 if case == "clean" else 4 if case == "generated" else 2)
    assert all(item["reward"] == 0 and item["info"] == {} for item in reference["trajectory"])
    with pytest.raises(WebError, match="завершена"):
        workbench.act(token, "submit", {"findings": []})


def test_silent_answers_clean_and_masked_are_scored_differently(workbench):
    for case, f1, missed in (("clean", 1, 0), ("masked_both", 0, 2)):
        state = workbench.create(case)
        state = workbench.act(state["session_id"], "submit", {"findings": []})
        assert state["result"]["evaluation"]["f1"] == f1
        assert state["result"]["evaluation"]["fn"] == missed


def test_arena_runs_real_policies_on_the_same_book_without_midrun_feedback(workbench):
    state = workbench.create("masked_both")
    reference = workbench.act(state["session_id"], "arena", {})["result"]["reference"]
    baseline = reference["comparison"]
    assert reference["evaluation"]["tp"] == 2 and reference["evaluation"]["f1"] == 1
    assert baseline["policy"] == "silent" and baseline["evaluation"]["fn"] == 2
    assert baseline["evaluation"]["reward"] == -1
    assert reference["trajectory"][0]["episode_id"] == baseline["trajectory"][0]["episode_id"]
    assert all(item["reward"] == 0 and item["info"] == {} for item in baseline["trajectory"])


@pytest.mark.parametrize("families", [[], ["fx_conversion"], ["duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"]])
def test_lab_generates_fresh_books_and_respects_selected_families(workbench, families):
    first = workbench.create("generated", families)
    second = workbench.create("generated", families)
    assert first["public"]["manifest"]["episode_id"] != second["public"]["manifest"]["episode_id"]
    validate_public_privacy(first["public"])
    assert first["result"] is None
    result = workbench.act(first["session_id"], "arena", {})["result"]
    assert {item["type"] for item in result["review"]} == set(families)
    assert result["reference"]["evaluation"]["f1"] == 1


@pytest.mark.parametrize("families", [False, ["unknown"], ["fx_conversion", "fx_conversion"], [False]])
def test_lab_rejects_invalid_family_settings(workbench, families):
    with pytest.raises(WebError):
        workbench.create("generated", families)


def test_reference_review_uses_worker_result_when_human_attempt_has_expired(workbench):
    state = workbench.create("masked_both")
    token = state["session_id"]
    workbench.get(token).env._deadline = 0  # Simulate an expired human attempt.
    result = workbench.act(token, "reference", {})["result"]
    assert result["evaluation"]["termination_reason"] == "time_limit"
    assert result["reference"]["evaluation"]["f1"] == 1
    assert all(item["found"] for item in result["review"])


def test_manual_answer_uses_real_amount_and_evidence_rules(workbench):
    state = workbench.create("masked_both")
    token = state["session_id"]
    bundle = workbench.get(token).bundle
    findings = [{"type": issue["type"], "affected_ids": issue["affected_ids"],
                 "claimed_amount_minor": issue["expected_amount_minor"], "currency": issue["currency"],
                 "evidence_ids": issue["evidence_sets"][0], "explanation": "Сверено с документом"}
                for issue in bundle.truth]
    # A wrong monetary claim must not be silently accepted by a UI adapter.
    findings[0]["claimed_amount_minor"] += 100
    result = workbench.act(token, "submit", {"findings": findings})["result"]
    assert (result["evaluation"]["tp"], result["evaluation"]["fp"], result["evaluation"]["fn"]) == (1, 1, 1)
    assert sorted(item["found"] for item in result["review"]) == [False, True]
    assert result["reference"] is None


def test_document_inspection_is_counted_and_private_review_is_terminal(workbench):
    state = workbench.create("masked_both")
    token = state["session_id"]
    doc = state["public"]["invoices"][0]
    state = workbench.act(token, "document", {"id": doc["doc_id"]})
    assert state["remaining_steps"] == 95
    assert state["document"] == doc
    assert state["result"] is None
    assert "review" not in state
    state = workbench.act(token, "submit", {"findings": []})
    assert state["result"]["evaluation"]["inspection_steps"] == 5
    assert len(state["result"]["review"]) == 2
    assert not any(item["found"] for item in state["result"]["review"])


def test_malformed_submission_is_terminal_not_free_feedback(workbench):
    state = workbench.create("masked_both")
    result = workbench.act(state["session_id"], "submit", {"findings": False})["result"]
    assert result["evaluation"]["status"] == "invalid_submit"
    assert result["evaluation"]["reward"] == -1


def test_unknown_sessions_cases_and_operations(workbench):
    with pytest.raises(WebError):
        workbench.create("../../private")
    with pytest.raises(WebError):
        workbench.get("not-a-session")
    state = workbench.create("clean")
    with pytest.raises(WebError):
        workbench.act(state["session_id"], "truth", {})


@pytest.fixture
def http_server():
    server = WorkbenchServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=3)
    server.server_close()


def request(server, method, path, data=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    body = None if data is None else json.dumps(data)
    options = {"Content-Type": "application/json", **(headers or {})}
    connection.request(method, path, body=body, headers=options)
    response = connection.getresponse()
    result = (response.status, dict(response.getheaders()), response.read())
    connection.close()
    return result


def test_http_workflow_and_only_allowlisted_assets(http_server):
    status, headers, body = request(http_server, "GET", "/")
    assert status == 200 and 'lang="ru"' in body.decode()
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    for asset in ("/style.css", "/app.js", "/lesson.mjs", "/replay.mjs"):
        assert request(http_server, "GET", asset)[0] == 200
    for path in ("/../pyproject.toml", "/datasets/manifest.json", "/private/truth.json", "/api/truth"):
        assert request(http_server, "GET", path)[0] == 404
    assert len(json.loads(request(http_server, "GET", "/api/cases")[2])["cases"]) == 5
    status, _, body = request(http_server, "POST", "/api/sessions", {"case": "masked_both"})
    assert status == 201
    token = json.loads(body)["session_id"]
    status, _, body = request(http_server, "POST", f"/api/sessions/{token}/submit", {"findings": []})
    assert status == 200
    assert json.loads(body)["result"]["evaluation"]["fn"] == 2


def test_http_blocks_cross_origin_and_dns_rebinding(http_server):
    assert request(http_server, "POST", "/api/sessions", {"case": "clean"}, {"Origin": "https://example.com"})[0] == 403
    assert request(http_server, "GET", "/", headers={"Host": "evil.example"})[0] == 403
    assert request(http_server, "POST", "/api/sessions", {"case": "clean"}, {"Content-Type": "text/plain"})[0] == 415


def test_cli_serve_dispatches_without_creating_run_output(monkeypatch):
    from fin_audit import cli, web
    called = []
    monkeypatch.setattr(web, "serve", called.append)
    cli.main(["serve", "--port", "9876"])
    assert called == [9876]


def test_prepared_image_requires_secure_workers_without_cooperative_fallback(monkeypatch):
    from fin_audit import web
    monkeypatch.setenv("FIN_AUDIT_REQUIRE_SECURE", "1")
    calls = []

    def fail_secure(*args, **kwargs):
        calls.append(kwargs["profile"])
        raise RuntimeError("secure_runtime_unavailable")

    monkeypatch.setattr(web, "run_episode", fail_secure)
    workbench = Workbench()
    state = workbench.create("masked_both")
    with pytest.raises(RuntimeError, match="secure_runtime_unavailable"):
        workbench.act(state["session_id"], "arena", {})
    assert calls == ["secure"]


def test_repair_lab_requires_terminal_result_and_never_changes_original_attempt(workbench):
    initial = workbench.create("masked_both")
    token = initial["session_id"]
    with pytest.raises(WebError) as error:
        workbench.act(token, "counterfactual", {"fix_duplicate": True, "fix_fx": False})
    assert error.value.status == 409
    workbench.act(token, "submit", {"findings": []})
    completed = canonical_json(workbench.act(token, "state", {}))
    for duplicate, fx, total, count in ((False, False, 55_000_000, 2), (True, False, 50_000_000, 1),
                                       (False, True, 60_000_000, 1), (True, True, 55_000_000, 0)):
        report = workbench.act(token, "counterfactual", {"fix_duplicate": duplicate, "fix_fx": fx})
        assert (report["after"]["revenue_minor"], report["after"]["issue_count"]) == (total, count)
        assert canonical_json(workbench.act(token, "state", {})) == completed


@pytest.mark.parametrize("payload", [{}, {"fix_duplicate": True}, {"fix_duplicate": 1, "fix_fx": False},
    {"fix_duplicate": True, "fix_fx": False, "book": {}}, {"fix_duplicate": False, "fix_fx": "false"}])
def test_repair_lab_rejects_invalid_flags_and_client_supplied_books(workbench, payload):
    state = workbench.create("masked_both")
    workbench.act(state["session_id"], "submit", {"findings": []})
    with pytest.raises(WebError) as error:
        workbench.act(state["session_id"], "counterfactual", payload)
    assert error.value.status == 400


def test_repair_lab_is_not_exposed_for_other_cases(workbench):
    state = workbench.create("clean")
    workbench.act(state["session_id"], "submit", {"findings": []})
    with pytest.raises(WebError) as error:
        workbench.act(state["session_id"], "counterfactual", {"fix_duplicate": True, "fix_fx": True})
    assert error.value.status == 400


def test_http_repair_lab_is_terminal_only_and_returns_a_checked_book(http_server):
    _, _, body = request(http_server, "POST", "/api/sessions", {"case": "masked_both"})
    token = json.loads(body)["session_id"]
    path = f"/api/sessions/{token}/counterfactual"
    flags = {"fix_duplicate": True, "fix_fx": True}
    assert request(http_server, "POST", path, flags)[0] == 409
    assert request(http_server, "POST", f"/api/sessions/{token}/submit", {"findings": []})[0] == 200
    status, _, body = request(http_server, "POST", path, flags)
    assert status == 200
    report = json.loads(body)
    assert report["scope"] == "post_run_educational_copy"
    assert report["after"]["issue_count"] == 0
    from fin_audit.checker import reconcile
    assert reconcile(report["book"]) == []
    assert request(http_server, "POST", path, flags, {"Origin": "https://foreign.invalid"})[0] == 403
