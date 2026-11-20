"""Local, human-facing audit workbench over the existing environment.

This is an educational UI, not the sealed benchmark runner. Human episodes have
a 30-minute deadline; the scoring rules and 100-action budget are unchanged.
Only terminal attempts reveal review data. No files in the workspace are served.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .contracts import ISSUE_TYPES, PUBLIC_TABLES, EpisodeBundle, canonical_json
from .environment import AuditEnv
from .presentation import with_names
from .repair_lab import repair_experiment
from .runner import run_episode
from .showcase import showcase_cases

ASSETS = Path(__file__).with_name("web_assets")


def worker_profile() -> str:
    """Prepared offline images require the real jail; never downgrade it."""
    return "secure" if os.environ.get("FIN_AUDIT_REQUIRE_SECURE") == "1" else "cooperative"
CASE_INFO = (
    {"id": "masked_both", "title": "Итог сошёлся", "subtitle": "Проверьте, достаточно ли общей суммы для заключения.", "tag": "Для первого знакомства", "company": "ТОО «Маяк Логистик»", "level": "Базовый"},
    {"id": "allocation_fx", "title": "Платёж в другой валюте", "subtitle": "Проследите путь платежа от документа до распределения.", "tag": "Связанные операции", "company": "ТОО «Вектор Трейд»", "level": "Продвинутый"},
    {"id": "allocation_cutoff", "title": "На границе месяца", "subtitle": "Сопоставьте даты документов и отчётный период.", "tag": "Связанные операции", "company": "ТОО «Север Сервис»", "level": "Продвинутый"},
    {"id": "clean", "title": "Контрольная книга", "subtitle": "Проверьте документы и вынесите собственное заключение.", "tag": "Проверка точности", "company": "ТОО «Исток Поставка»", "level": "Базовый"},
    {"id": "generated", "title": "Новая компания", "subtitle": "Новый эпизод генератора с выбранными семействами ошибок.", "tag": "Лаборатория", "company": "Синтетическая компания", "level": "Новый эпизод"},
)
EXPLANATIONS = {
    "duplicate_invoice": "Один счёт признан несколько раз. Сумма находки — превышение над исходным счётом. В затронутые записи входят все строки признания дебиторской задолженности, включая исходную.",
    "fx_conversion": "Сумма исходного документа пересчитана по неверному курсу. Используйте курс на дату документа в часовом поясе компании. Сумма находки — абсолютная разница с учётной оценкой.",
    "payment_allocation": "У платежа есть однозначное назначение на счёт, но часть поступления осталась нераспределённой. Сумма находки определяется по наблюдаемой оценке в книге; при исправлении курса она может измениться.",
    "period_cutoff": "Дата первичного документа не соответствует периоду признания операции в книге. Период включает начало и исключает конец. Сумма находки — абсолютная сумма основной проводки.",
}


class WebError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)


@dataclass
class Session:
    bundle: EpisodeBundle
    env: AuditEnv
    view: dict[str, Any]
    events: list[dict[str, Any]]
    case_id: str = ""
    created: float = field(default_factory=time.monotonic)
    lock: Any = field(default_factory=threading.Lock)
    result: dict[str, Any] | None = None


class Workbench:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()

    def create(self, case: str, families: Any = None) -> dict[str, Any]:
        info = next((item for item in CASE_INFO if item["id"] == case), None)
        if info is None:
            raise WebError(400, "Неизвестный кейс. Выберите кейс из списка.")
        if case == "generated":
            from .generator import generate
            selected = list(ISSUE_TYPES) if families is None else families
            if (type(selected) is not list or len(selected) > 4
                    or any(type(item) is not str or item not in ISSUE_TYPES for item in selected)
                    or len(set(selected)) != len(selected)):
                raise WebError(400, "Выберите от нуля до четырёх типов ошибок.")
            config = {"invoice_count": 6, "ledger_rows": None, "foreign_invoice_count": 3, "refund_count": 1,
                      "duplicate_count": int("duplicate_invoice" in selected),
                      "allocation_count": int("payment_allocation" in selected),
                      "fx_count": int("fx_conversion" in selected),
                      "cutoff_count": int("period_cutoff" in selected)}
            bundle = with_names(generate(secrets.randbits(60), config), presentation_seed=secrets.randbits(60))
            display_name = f'Вымышленная компания «{secrets.choice(("Атлас", "Транзит", "Горизонт", "Контур"))} {secrets.token_hex(2)}»'
        else:
            if families is not None:
                raise WebError(400, "Типы ошибок настраиваются в лаборатории новых компаний.")
            bundle = with_names(showcase_cases()[case], presentation_seed=501)
            display_name = info["company"]
        # This public fictional display label does not change financial facts.
        before_label = bundle.provenance["public_sha256"]
        bundle.public["manifest"]["company_name"] = display_name
        after_label = hashlib.sha256(canonical_json(bundle.public).encode()).hexdigest()
        bundle.provenance["public_sha256"] = after_label
        bundle.provenance["web_display_label"] = {"before_public_sha256": before_label,
                                                 "after_public_sha256": after_label,
                                                 "scope": "fictional_company_name_only"}
        env = AuditEnv(mode="train", time_limit_seconds=1800)
        observation, _ = env.load_bundle(bundle)
        view = {"manifest": observation["manifest"], "policy": observation["policy"]}
        events = []
        for table in PUBLIC_TABLES:
            rows, cursor = [], None
            while True:
                obs, _, terminated, truncated, _ = env.step({"action": "list_records", "args": {"table": table, "cursor": cursor, "limit": 50}})
                if terminated or truncated or "error" in obs:
                    raise WebError(500, "Не удалось прочитать учебную книгу.")
                rows.extend(obs["result"]["records"])
                events.append({"step": env.total_steps, "action": "list_records", "table": table, "records": len(obs["result"]["records"])})
                cursor = obs["result"]["next_cursor"]
                if cursor is None:
                    break
            view[table] = rows
        session = Session(bundle, env, view, events, case_id=case)
        token = secrets.token_urlsafe(24)
        with self.lock:
            expired = [key for key, item in self.sessions.items() if time.monotonic() - item.created > 3600]
            for key in expired:
                del self.sessions[key]
            if len(self.sessions) >= 64:
                raise WebError(429, "Слишком много попыток. Перезапустите локальный сервер.")
            self.sessions[token] = session
        return self.state(token, session)

    def get(self, token: str) -> Session:
        with self.lock:
            session = self.sessions.get(token)
        if session is None or time.monotonic() - session.created > 3600:
            raise WebError(404, "Попытка больше не доступна. Начните кейс заново.")
        return session

    @staticmethod
    def state(token: str, session: Session) -> dict[str, Any]:
        return {"session_id": token, "public": session.view, "remaining_steps": 100 - session.env.total_steps,
                "events": session.events, "result": session.result}

    @staticmethod
    def finish(session: Session, findings: list, *, reference: dict | None = None) -> dict:
        evaluation = session.env.evaluation_result()
        if evaluation is None:
            raise WebError(500, "Попытка не завершена.")
        review_evaluation = reference["evaluation"] if reference is not None else evaluation
        matched_issues = {pair[1] for pair in review_evaluation["matched_pairs"]}
        review = [{"type": issue["type"], "affected_ids": issue["affected_ids"],
                   "claimed_amount_minor": issue["expected_amount_minor"], "currency": issue["currency"],
                   "evidence_ids": issue["evidence_sets"][0], "explanation": EXPLANATIONS[issue["type"]],
                   "found": issue["issue_id"] in matched_issues}
                  for issue in session.bundle.truth]
        session.result = {"evaluation": evaluation, "submitted_findings": findings, "review": review,
                          "reference": reference}
        return session.result

    def act(self, token: str, operation: str, payload: dict) -> dict:
        session = self.get(token)
        with session.lock:
            if operation == "state":
                return self.state(token, session)
            if operation == "counterfactual":
                if session.result is None:
                    raise WebError(409, "Сначала завершите проверку. Эксперимент откроется после результата.")
                if session.case_id != "masked_both":
                    raise WebError(400, "Откройте основной пример с двумя маскирующимися ошибками.")
                if (set(payload) != {"fix_duplicate", "fix_fx"}
                        or any(type(value) is not bool for value in payload.values())):
                    raise WebError(400, "Укажите два переключателя исправлений: включено или выключено.")
                return repair_experiment(session.bundle.public, **payload)
            if session.result is not None:
                raise WebError(409, "Попытка завершена. Начните новую, чтобы изменить ответ.")
            if operation == "document":
                doc_id = payload.get("id")
                if type(doc_id) is not str or len(doc_id) > 256:
                    raise WebError(400, "Выберите документ из книги.")
                obs, _, terminated, truncated, _ = session.env.step({"action": "get_document", "args": {"doc_id": doc_id}})
                session.events.append({"step": session.env.total_steps, "action": "get_document", "id": doc_id})
                if terminated or truncated:
                    self.finish(session, [])
                if "error" in obs:
                    raise WebError(404, "Документ не найден.")
                return {"document": obs.get("result"), **self.state(token, session)}
            if operation == "submit":
                findings = payload.get("findings")
                # Structural mistakes are terminal, exactly as in the core API.
                session.env.step({"action": "submit", "args": {"findings": {
                    "episode_id": session.view["manifest"]["episode_id"], "findings": findings}}})
                session.events.append({"step": session.env.total_steps, "action": "submit"})
                self.finish(session, findings if isinstance(findings, list) else [])
                return self.state(token, session)
            if operation in {"reference", "arena"}:
                # A real separate-process public-only policy. No oracle/gold input.
                profile = worker_profile()
                report = run_episode(session.bundle, policy="simple", profile=profile, mode="closed")
                if report["agent_exitcode"] != 0 or report["evaluation"]["termination_reason"] != "submitted":
                    raise WebError(500, "Аудитор не завершил запуск. Попробуйте ещё раз.")
                comparison = None
                if operation == "arena":
                    baseline = run_episode(session.bundle, policy="silent", profile=profile, mode="closed")
                    if baseline["agent_exitcode"] != 0 or baseline["evaluation"]["termination_reason"] != "submitted":
                        raise WebError(500, "Сравнительный запуск не завершён. Попробуйте ещё раз.")
                    comparison = {key: baseline[key] for key in ("policy", "evaluation", "trajectory", "elapsed_seconds")}
                findings = report["trajectory"][-1]["action"]["args"]["findings"]["findings"]
                session.env.step({"action": "submit", "args": {"findings": {
                    "episode_id": session.view["manifest"]["episode_id"], "findings": findings}}})
                session.events.append({"step": session.env.total_steps, "action": "submit", "source": "reference"})
                self.finish(session, findings, reference={"policy": "simple", "profile": profile,
                            "evaluation": report["evaluation"], "trajectory": report["trajectory"],
                            "elapsed_seconds": report["elapsed_seconds"], "comparison": comparison})
                return self.state(token, session)
            raise WebError(404, "Действие не найдено.")


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int]) -> None:
        self.workbench = Workbench()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: WorkbenchServer

    def respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def json(self, status: int, value: Any) -> None:
        self.respond(status, canonical_json(value).encode(), "application/json; charset=utf-8")

    def local_request(self) -> None:
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed:
            raise WebError(403, "Откройте тренажёр по локальному адресу сервера.")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {"http://" + host for host in allowed}:
            raise WebError(403, "Запрос разрешён только из локального тренажёра.")

    def do_GET(self) -> None:
        try:
            self.local_request()
            path = urlsplit(self.path).path
            if path == "/api/cases":
                self.json(200, {"cases": CASE_INFO})
                return
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/lesson.mjs": ("lesson.mjs", "text/javascript; charset=utf-8"),
                      "/replay.mjs": ("replay.mjs", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path not in assets:
                raise WebError(404, "Страница не найдена.")
            filename, content_type = assets[path]
            self.respond(200, (ASSETS / filename).read_bytes(), content_type)
        except WebError as error:
            self.json(error.status, {"error": str(error)})

    def do_POST(self) -> None:
        try:
            self.local_request()
            if self.headers.get_content_type() != "application/json":
                raise WebError(415, "Нужен JSON-запрос.")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise WebError(400, "Неверная длина запроса.") from None
            if not 0 < length <= 65536:
                raise WebError(413, "Слишком большой запрос.")
            try:
                payload = json.loads(self.rfile.read(length), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError):
                raise WebError(400, "Не удалось прочитать JSON-запрос.") from None
            if not isinstance(payload, dict):
                raise WebError(400, "Нужен объект JSON.")
            parts = urlsplit(self.path).path.strip("/").split("/")
            if parts == ["api", "sessions"]:
                self.json(201, self.server.workbench.create(payload.get("case"), payload.get("families")))
            elif len(parts) == 4 and parts[:2] == ["api", "sessions"]:
                self.json(200, self.server.workbench.act(parts[2], parts[3], payload))
            else:
                raise WebError(404, "Действие не найдено.")
        except WebError as error:
            self.json(error.status, {"error": str(error)})
        except (RuntimeError, ValueError, OSError):
            self.json(500, {"error": "Не удалось выполнить действие. Начните новую попытку."})

    def log_message(self, format: str, *args: Any) -> None:
        pass


def serve(port: int = 8765) -> None:
    with WorkbenchServer(("127.0.0.1", port)) as server:
        print(f"Genesis Audit Lab: http://127.0.0.1:{server.server_port}/", flush=True)
        print("Локальный учебный интерфейс. Ctrl+C — остановить.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    serve(parser.parse_args().port)
