"""Offline, read-only HTML from a completed and verified showcase receipt.

This is a trusted post-run renderer, never an agent tool or live feedback path.
Private provenance/config/seed data and full evaluation payloads are not embedded.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import secrets
import stat
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import EpisodeBundle, canonical_json, strict_json_loads, validate_public_privacy
from .environment import AuditEnv
from .storage import load_episode

_VERSION = "1.0"
_CORE = frozenset({"analysis.json", "summary.json", "evaluation.json", "run-manifest.json", "trajectories.jsonl"})
_EPISODE_FILES = frozenset({
    "public/manifest.json", "public/policy.json", "public/ledger.csv", "public/invoices.jsonl",
    "public/payments.jsonl", "public/fx.csv", "private/truth.json", "private/provenance.json",
})
_CASE_ORDER = (
    "clean", "duplicate_only", "fx_only", "masked_both", "allocation_fx",
    "allocation_fx_repaired", "allocation_cutoff", "allocation_cutoff_repaired",
)
_CASES = frozenset((*_CASE_ORDER, "transformed_masked"))
_POLICIES = frozenset({"simple", "silent", "oracle"})
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_MAX_BYTES = 64 * 1024 * 1024
_ISSUE_NAMES = {
    "duplicate_invoice": "Повторное отражение счёта",
    "fx_conversion": "Неверный валютный пересчёт",
    "payment_allocation": "Потерянное распределение платежа",
    "period_cutoff": "Неверный отчётный период",
}
_POLICY_NAMES = {"simple": "Алгоритм сверки", "silent": "Молчание · контроль", "oracle": "Oracle · готовые ответы"}


class ShowcaseReportError(ValueError):
    """A bounded receipt/renderer error, with no private data in diagnostics."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ShowcaseReportError(code)


def _directory(path: Path) -> Path:
    path = path.absolute()
    for current in (path, *path.parents):
        _require(not current.is_symlink() and current.is_dir(), "report_directory_missing_or_symlink")
    return path.resolve(strict=True)


def _read(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            _require(stat.S_ISREG(metadata.st_mode) and metadata.st_size <= _MAX_BYTES, "invalid_receipt_file")
            data = handle.read(_MAX_BYTES + 1)
            _require(len(data) <= _MAX_BYTES, "receipt_file_too_large")
            return data
    except (OSError, ValueError) as error:
        if isinstance(error, ShowcaseReportError):
            raise
        raise ShowcaseReportError("receipt_file_missing_or_unsafe") from error


def _json(data: bytes) -> Any:
    try:
        return strict_json_loads(data.decode("utf-8", errors="strict"))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ShowcaseReportError("invalid_receipt_json") from error


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _hash_matches(data: bytes, expected: Any) -> bool:
    return type(expected) is str and _SHA.fullmatch(expected) is not None and hashlib.sha256(data).hexdigest() == expected


def _successful_checks(value: Any) -> bool:
    return type(value) is dict and bool(value) and all(item is True for item in value.values())


def _without_human_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_human_copy(child) for key, child in value.items() if key not in {"label", "formula"}}
    if isinstance(value, list):
        return [_without_human_copy(child) for child in value]
    return value


def _validate_runs(runs: Any, bundles: dict[str, EpisodeBundle], trajectory_data: bytes, profile: str) -> None:
    _require(type(runs) is list and len(runs) == 27, "invalid_showcase_runs")
    by_run, by_case = {}, {}
    for report in runs:
        _require(type(report) is dict, "invalid_showcase_run")
        case, policy, run_id = report.get("case"), report.get("policy"), report.get("trajectory_run_id")
        _require(case in _CASES and policy in _POLICIES and (case, policy) not in by_case, "invalid_run_case_or_policy")
        _require(type(run_id) is str and re.fullmatch(r"showcase-[0-9]{3}", run_id) is not None and run_id not in by_run, "invalid_trajectory_run_identifier")
        _require(report.get("episode_id") == bundles[case].public["manifest"]["episode_id"], "run_episode_mismatch")
        _require(report.get("mode") == "closed", "report_requires_closed_runs")
        _require(report.get("profile") == ("trusted_oracle" if policy == "oracle" else profile), "run_profile_mismatch")
        elapsed = report.get("elapsed_seconds")
        _require(type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0, "invalid_run_duration")
        _require(report.get("agent_exitcode") in (None, 0), "showcase_worker_failed")
        evaluation = report.get("evaluation")
        _require(type(evaluation) is dict and evaluation.get("status") == "scored" and evaluation.get("termination_reason") == "submitted", "showcase_run_not_completed")
        by_run[run_id], by_case[case, policy] = report, report
    _require(set(by_case) == {(case, policy) for case in _CASES for policy in _POLICIES}, "missing_showcase_run")
    grouped: dict[str, list[dict]] = defaultdict(list)
    lines = trajectory_data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    _require(0 < len(lines) <= 27 * 101, "invalid_showcase_trajectory_size")
    for line in lines:
        row = _json(line)
        _require(type(row) is dict and row.get("run_id") in by_run, "unknown_trajectory_run")
        grouped[row["run_id"]].append(row)
    _require(set(grouped) == set(by_run), "missing_showcase_trajectory")
    for run_id, report in by_run.items():
        bundle = bundles[report["case"]]
        env = AuditEnv(mode="closed", clock=lambda: 0.0)
        initial, info = env.load_bundle(bundle)
        rows = grouped[run_id]
        _require(2 <= len(rows) <= 101, "invalid_showcase_trajectory_length")
        first = rows[0]
        _require(first.get("episode_id") == report["episode_id"] and first.get("policy") == report["policy"], "trajectory_identity_mismatch")
        _require(type(first.get("step")) is int and first["step"] == 0 and first.get("action") is None, "invalid_initial_trajectory")
        expected_initial = {"observation": initial, "reward": 0.0, "terminated": False, "truncated": False, "info": info}
        _require(canonical_json({key: first.get(key) for key in expected_initial}) == canonical_json(expected_initial), "initial_trajectory_mismatch")
        for index, row in enumerate(rows[1:], 1):
            _require(row.get("episode_id") == report["episode_id"] and row.get("policy") == report["policy"], "trajectory_identity_mismatch")
            _require(type(row.get("step")) is int and row["step"] == index, "trajectory_step_mismatch")
            observation, reward, terminated, truncated, info = env.step(row.get("action"))
            expected = {"observation": observation, "reward": reward, "terminated": terminated, "truncated": truncated, "info": info}
            _require(canonical_json({key: row.get(key) for key in expected}) == canonical_json(expected), "wire_trajectory_mismatch")
            validate_public_privacy(observation)
            _require(not (terminated or truncated) or index == len(rows) - 1, "trajectory_after_completion")
        _require(canonical_json(env.evaluation_result()) == canonical_json(report["evaluation"]), "actual_evaluation_mismatch")
        expected_count = len(bundle.truth)
        if report["policy"] in {"simple", "oracle"}:
            _require((report["evaluation"]["tp"], report["evaluation"]["fp"], report["evaluation"]["fn"]) == (expected_count, 0, 0), "showcase_reference_answer_failed")
        if report["policy"] == "oracle":
            _require(report["evaluation"]["reward"] == 1, "oracle_upper_bound_mismatch")


def _load_receipt(receipt_directory: Path) -> tuple[dict, dict, dict[str, EpisodeBundle]]:
    receipt = _directory(receipt_directory)
    complete = _json(_read(receipt / "COMPLETE.json"))
    _require(type(complete) is dict and complete.get("showcase_version") == _VERSION, "unsupported_showcase_receipt")
    _require(complete.get("completed") is True and complete.get("passed") is True, "showcase_not_successfully_completed")
    hashes = complete.get("sha256")
    _require(type(hashes) is dict and set(hashes) == _CORE, "invalid_complete_file_manifest")
    blobs = {name: _read(receipt / name) for name in _CORE}
    _require(all(_hash_matches(blobs[name], hashes[name]) for name in _CORE), "showcase_receipt_digest_mismatch")
    analysis, summary, manifest, evaluations = (_json(blobs[name]) for name in ("analysis.json", "summary.json", "run-manifest.json", "evaluation.json"))
    for value in (analysis, summary, manifest):
        _require(type(value) is dict and value.get("showcase_version") == _VERSION, "unsupported_showcase_format")
    _require(analysis.get("scope") == summary.get("scope") == "post_run_only", "report_requires_post_run_scope")
    _require(analysis.get("currency") == "KZT" and summary.get("passed") is True, "showcase_not_successfully_completed")
    _require(_successful_checks(analysis.get("checks")) and _successful_checks(summary.get("checks")), "showcase_checks_not_passed")
    _require(summary.get("profile") in {"secure", "cooperative"} and manifest.get("profile") == summary["profile"], "invalid_showcase_profile")
    _require(type(summary.get("case_count")) is int and summary["case_count"] == 9, "invalid_showcase_case_count")
    _require(canonical_json(evaluations) == canonical_json(summary.get("runs")), "compact_evaluations_mismatch")
    entries = manifest.get("episodes")
    _require(type(entries) is list and len(entries) == 9, "invalid_episode_manifest")
    bundles: dict[str, EpisodeBundle] = {}
    for entry in entries:
        _require(type(entry) is dict and entry.get("case") in _CASES and entry["case"] not in bundles, "invalid_episode_case")
        case = entry["case"]
        directory = _directory(receipt / "episodes" / case)
        files = entry.get("files")
        _require(type(files) is dict and set(files) == _EPISODE_FILES, "invalid_episode_file_manifest")
        for name in _EPISODE_FILES:
            _directory((directory / name).parent)
            _require(_hash_matches(_read(directory / name), files[name]), "showcase_episode_file_digest_mismatch")
        bundle = load_episode(directory)
        _require(bundle.public["manifest"]["episode_id"] == entry.get("episode_id"), "showcase_episode_identity_mismatch")
        _require(_digest(bundle.public) == entry.get("public_sha256") and _digest(bundle.truth) == entry.get("truth_sha256"), "showcase_episode_digest_mismatch")
        _require(bundle.public["manifest"]["schema_version"] == manifest.get("schema_version") == summary.get("schema_version"), "showcase_schema_version_mismatch")
        bundles[case] = bundle
    _require(set(bundles) == _CASES, "missing_showcase_episode")
    _require(type(analysis.get("case_order")) is list and len(analysis["case_order"]) == 8 and set(analysis["case_order"]) == set(_CASE_ORDER), "invalid_analysis_case_order")
    from .showcase import showcase_analysis

    independent = showcase_analysis({case: bundles[case] for case in analysis["case_order"]})
    _require(canonical_json(_without_human_copy(independent)) == canonical_json(_without_human_copy(analysis)), "showcase_analysis_mismatch")
    _validate_runs(summary.get("runs"), bundles, blobs["trajectories.jsonl"], summary["profile"])
    return analysis, summary, bundles


def _h(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _money(amount: int, currency: str = "KZT", exponent: int = 2, *, signed: bool = False) -> str:
    _require(type(amount) is int and type(exponent) is int and 0 <= exponent <= 9, "invalid_display_money")
    negative = amount < 0
    whole, fraction = divmod(abs(amount), 10**exponent)
    number = f"{whole:,}".replace(",", " ")
    if exponent:
        number += f",{fraction:0{exponent}d}"
    sign = "−" if negative else ("+" if signed and amount else "")
    return f"{sign}{number} {'₸' if currency == 'KZT' else currency}"


def _timestamp(value: str, timezone: str) -> str:
    parsed = datetime.fromisoformat(value).astimezone(ZoneInfo(timezone))
    offset = parsed.strftime("%z")
    return parsed.strftime("%d.%m.%Y · %H:%M:%S") + f" (UTC{offset[:3]}:{offset[3:]})"


def _number(value: Any, digits: int = 3, *, signed: bool = False) -> str:
    _require(type(value) in (int, float) and math.isfinite(value), "invalid_display_number")
    return (f"{value:+.{digits}f}" if signed else f"{value:.{digits}f}").replace(".", ",").replace("-", "−")


def _calculation(issue: dict) -> str:
    values, kind = issue["calculation"], issue["type"]
    currency, exponent = values["source_currency"], values["source_exponent"]
    source = _h(_money(values["source_amount_minor"], currency, exponent))
    expected = _h(_money(values["expected_base_minor"])) if kind in {"duplicate_invoice", "fx_conversion"} else ""
    observed = _h(_money(values["observed_base_minor"]))
    discrepancy = _h(_money(values["discrepancy_minor"]))
    if kind == "duplicate_invoice":
        text = f"Счёт на <strong>{source}</strong> отражён <strong>{_h(values['recognition_count'])} раза</strong>. В учёте {observed} вместо {expected}."
    elif kind == "fx_conversion":
        text = f"Для <strong>{source}</strong> положен курс <strong>{_h(values['correct_fx_rate'])}</strong>; применён <strong>{_h(values['applied_fx_rate'])}</strong>. Получилось {observed} вместо {expected}."
    elif kind == "payment_allocation":
        allocated = _h(_money(values["allocated_source_minor"], currency, exponent))
        remainder = _h(_money(values["unallocated_source_minor"], currency, exponent))
        text = f"Из платежа <strong>{source}</strong> распределено {allocated}. Осталось <strong>{remainder}</strong>; по применённому курсу это {observed}. По документу весь платёж предназначен этому счёту."
    else:
        source_time = _h(_timestamp(values["source_timestamp"], values["policy_timezone"]))
        posted_time = _h(_timestamp(values["posted_timestamp"], values["policy_timezone"]))
        text = f"Документ датирован <strong>{source_time}</strong>, запись — <strong>{posted_time}</strong>. Они попали в разные отчётные периоды. Сумма записи: {observed}."
    return f'<p>{text}</p><div class="calculation-result"><span>Доказанное расхождение</span><strong>{discrepancy}</strong></div><details class="formula"><summary>Правило расчёта</summary><code>{_h(values["formula"])}</code></details>'


def _sources(bundle: EpisodeBundle) -> dict[str, tuple[str, dict]]:
    result = {"policy": ("policy", bundle.public["policy"])}
    for table, key in (("ledger", "row_id"), ("invoices", "doc_id"), ("payments", "doc_id"), ("fx", "record_id")):
        for record in bundle.public[table]:
            result[record[key]] = table, record
    return result


def _source_card(identity: str, table: str, record: dict, bundle: EpisodeBundle, anchor: str) -> str:
    if table == "invoices":
        title = "Счёт · " + _money(record["total_minor"], record["currency"], bundle.public["manifest"]["currency_exponents"][record["currency"]])
    elif table == "payments":
        title = "Платёж · " + _money(record["amount_minor"], record["currency"], bundle.public["manifest"]["currency_exponents"][record["currency"]])
    elif table == "ledger":
        signed = record["debit_minor"] - record["credit_minor"]
        title = f"Проводка {record['account']} · " + _money(signed, signed=True)
    elif table == "fx":
        title = f"Курс {record['source_currency']} → {record['target_currency']} · {record['rate']}"
    else:
        title = "Правила учёта компании"
    raw = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    return f'<details class="source-card" id="{anchor}"><summary><span>{_h(title)}</span><code>{_h(identity)}</code></summary><pre>{_h(raw)}</pre></details>'


def _policy_results(case: str, summary: dict) -> str:
    indexed = {run["policy"]: run for run in summary["runs"] if run["case"] == case}
    rows = []
    for policy in ("simple", "silent", "oracle"):
        run, evaluation = indexed[policy], indexed[policy]["evaluation"]
        rows.append(f'<tr class="policy-{policy}"><th scope="row">{_POLICY_NAMES[policy]}</th><td>{evaluation["tp"]}</td><td>{evaluation["fp"]}</td><td>{evaluation["fn"]}</td><td>{_number(evaluation["f1"], 2)}</td><td>{_number(evaluation["reward"], signed=True)}</td><td>{evaluation["total_steps"]}</td></tr>')
    return '<div class="table-scroll"><table class="policy-results" data-testid="policy-results"><caption>Результаты завершённых запусков</caption><thead><tr><th>Политика</th><th><abbr title="Верные находки">TP</abbr></th><th><abbr title="Ложные обвинения">FP</abbr></th><th><abbr title="Пропущенные ошибки">FN</abbr></th><th>F1</th><th>Награда</th><th>Шаги</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></div><p class="fineprint">Oracle получает готовый правильный ответ. Его результат проверяет оценщик; он не показывает способность ИИ расследовать документы.</p>'


def _case_card(card: dict, bundle: EpisodeBundle, summary: dict, index: int) -> str:
    sources = _sources(bundle)
    evidence = list(dict.fromkeys(identity for issue in card["issues"] for proof in issue["evidence_sets"] for identity in proof))
    if not evidence:
        evidence = [*(record["doc_id"] for record in bundle.public["invoices"][:2]), "policy"]
    anchors = {identity: f"document-{index}-{position}" for position, identity in enumerate(evidence)}
    issue_fragments = []
    for position, issue in enumerate(card["issues"]):
        links = "".join(f'<a href="#{anchors[identity]}" class="evidence-link">{_h(identity)}</a>' for identity in issue["evidence_sets"][0])
        issue_fragments.append(f'<article class="issue" id="issue-{card["key"]}-{position}"><h3>{_ISSUE_NAMES[issue["type"]]}</h3>{_calculation(issue)}<div class="evidence"><span>Документы и записи</span>{links}</div></article>')
    if not issue_fragments:
        issue_fragments.append('<div class="clean-note">Расхождений не обнаружено. Документы и проводки согласованы с правилами этого вымышленного эпизода.</div>')
    source_cards = "".join(_source_card(identity, *sources[identity], bundle, anchors[identity]) for identity in evidence)
    status = "Ошибок нет" if card["issue_count"] == 0 else f"Расхождений: {card['issue_count']}"
    return f'<section class="case-card" id="case-{card["key"]}" data-case="{card["key"]}"><header class="case-heading"><div><p class="eyebrow">Проверенный вариант</p><h2>{_h(card["label"])}</h2></div><span class="status {"good" if not card["issues"] else "bad"}">{_h(status)}</span></header><p class="case-total">Сумма признания счетов в учёте <strong>{_h(_money(card["recognition_total_minor"]))}</strong></p><div class="issues">{"".join(issue_fragments)}</div>{_policy_results(card["key"], summary)}<div class="source-heading"><h3>Открыть источники</h3><p>Карточки содержат исходные поля документов. Нажмите на строку, чтобы развернуть её.</p></div><div class="sources">{source_cards}</div></section>'


def _html(analysis: dict, summary: dict, bundles: dict[str, EpisodeBundle]) -> str:
    masking, fx, cutoff = analysis["masking"], analysis["dependent_fx"], analysis["dependent_cutoff"]
    cards = {card["key"]: card for card in analysis["cards"]}
    effects = []
    masked = cards[masking["masked_key"]]
    for effect in masking["signed_effects"]:
        issue_index = next(index for index, issue in enumerate(masked["issues"]) if issue["type"] == effect["type"])
        effects.append(f'<a class="effect" href="#issue-{masking["masked_key"]}-{issue_index}"><span>{_ISSUE_NAMES[effect["type"]]}</span><strong>{_h(_money(effect["signed_minor"], signed=True))}</strong></a>')
    nav = "".join(f'<a href="#case-{key}">{_h(cards[key]["label"])}</a>' for key in analysis["case_order"])
    content = "".join(_case_card(cards[key], bundles[key], summary, index) for index, key in enumerate(analysis["case_order"]))
    profile = "Изолированный запуск" if summary["profile"] == "secure" else "Локальный запуск · изоляция не подтверждена"
    return f'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'none'; img-src 'none'; base-uri 'none'; form-action 'none'"><title>Две ошибки за правильным итогом · Genesis</title><style>{_STYLE}</style></head>
<body><main><header class="masthead"><span>GENESIS / ФИНАНСОВЫЙ АУДИТ</span><span>ЗАВЕРШЁННОЕ ДОСЬЕ</span></header>
<section class="hero"><div class="hero-copy"><p class="eyebrow">Все данные вымышлены · {_h(profile)}</p><h1>Две ошибки<br>за правильным итогом</h1><p class="lead">Сумма признанных счетов совпала с правильной. Документы показывают две отдельные ошибки, которые скрыли друг друга.</p></div><div class="completion"><span class="completion-mark">✓</span><strong>Проверка завершена</strong><p>9 вариантов · 27 запусков</p><p>Этот отчёт открыт после завершения всех запусков. Он не передаёт ответы работающему агенту.</p></div></section>
<section class="masking-panel" aria-label="Ошибки, скрывшие друг друга"><div class="sum-grid"><div><span>Корректный учёт</span><strong data-testid="clean-total">{_h(_money(masking["clean_total_minor"]))}</strong></div><span class="equals" aria-label="равно">=</span><div><span>Учёт с двумя ошибками</span><strong data-testid="masked-total">{_h(_money(masking["masked_total_minor"]))}</strong></div></div><div class="effects">{"".join(effects)}<div class="net"><span>Изменение общего итога</span><strong data-testid="net-effect">{_h(_money(masking["net_effect_minor"], signed=True))}</strong></div></div><p class="panel-note">Одинаковый итог не подтверждает правильность каждой операции. Нажмите на расхождение, чтобы увидеть расчёт и документы.</p></section>
<section class="repairs" aria-label="Исправление причин"><article><p class="eyebrow">Исправляем курс</p><h2>Меняется и сумма распределения</h2><dl><dt>Валютная ошибка</dt><dd>{_h(_money(fx["before_fx_error_minor"]))} → <span class="fixed">{_h(_money(fx["after_fx_error_minor"]))}</span></dd><dt>Нераспределённая часть</dt><dd>{_h(_money(fx["before_allocation_minor"]))} → {_h(_money(fx["after_allocation_minor"]))}</dd></dl><p>После исправления курса оставшаяся ошибка распределения пересчитывается.</p><a href="#case-{fx["after_key"]}">Посмотреть результат исправления →</a></article><article><p class="eyebrow">Исправляем период</p><h2>Каждая причина проверяется отдельно</h2><dl><dt>Ошибка периода</dt><dd>{_h(_money(cutoff["before_cutoff_minor"]))} → <span class="fixed">{_h(_money(cutoff["after_cutoff_minor"]))}</span></dd><dt>Нераспределённая часть</dt><dd>{_h(_money(cutoff["before_allocation_minor"]))} → {_h(_money(cutoff["after_allocation_minor"]))}</dd></dl><p>Верная дата устраняет ошибку периода. Распределение платежа проверяется самостоятельно.</p><a href="#case-{cutoff["after_key"]}">Посмотреть результат исправления →</a></article></section>
<nav class="case-nav" aria-label="Варианты проверки">{nav}</nav>{content}
<section class="renaming"><h2>Проверка после смены идентификаторов</h2><p>Тот же случай с двумя ошибками выполнен с новыми ID и другим порядком записей. Ниже приведены реальные результаты алгоритма и контрольных политик.</p>{_policy_results("transformed_masked", summary)}<p class="fineprint">Совпадение результата этого прогона не означает, что любой агент выберет одинаковый порядок действий после перемешивания документов.</p></section>
<footer><strong>Самодостаточный отчёт</strong><p>Файлы квитанции, документы эпизодов и журналы действий проверены по контрольным суммам. Записанные действия воспроизведены без новых запусков агентов.</p><p>Oracle — привилегированный контроль оценщика. Алгоритм сверки — скриптовая политика. Демо не заявляет обучение модели или пригодность для настоящего аудита.</p></footer></main></body></html>'''


def _publish_new(output_file: Path, content: str) -> Path:
    output = output_file.absolute()
    parent = _directory(output.parent)
    output = parent / output.name
    _require(output.name not in {"", ".", ".."}, "invalid_report_filename")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(parent, flags)
    temporary = f".showcase-report-{secrets.token_hex(16)}.tmp"
    created = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # A same-directory hard link publishes the complete file atomically and
        # fails for an existing file or symlink; no destination is overwritten.
        os.link(temporary, output.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
        return output
    finally:
        if created:
            os.unlink(temporary, dir_fd=directory_fd)
        os.close(directory_fd)


def render_showcase_report(receipt_directory: Path, output_file: Path) -> Path:
    """Verify a completed receipt and create one new offline HTML file."""
    receipt = _directory(Path(receipt_directory))
    requested = Path(output_file).absolute()
    output = _directory(requested.parent) / requested.name
    _require(not output.is_relative_to(receipt), "report_must_be_outside_receipt")
    try:
        analysis, summary, bundles = _load_receipt(receipt)
        rendered = _html(analysis, summary, bundles)
    except ShowcaseReportError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, RecursionError) as error:
        raise ShowcaseReportError("invalid_showcase_receipt") from error
    return _publish_new(output, rendered)


_STYLE = r'''
:root{--paper:#EEF2F6;--ink:#14243A;--link:#245B8A;--error:#B14E3F;--fixed:#296C61;--line:#D1DCE5;--display:Georgia,'Times New Roman',serif;--body:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}html{scroll-padding-top:24px}body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--body);font-size:15px;line-height:1.6}main{max-width:1160px;margin:0 auto;padding:34px 28px 0}a{color:var(--link);text-underline-offset:3px}a:hover{color:var(--ink)}a:focus-visible,summary:focus-visible{outline:3px solid var(--link);outline-offset:4px}h1,h2,h3,p{margin-top:0}h1,h2{font-family:var(--display);font-weight:400;line-height:1.13}h1{font-size:clamp(2.3rem,5.6vw,4.3rem);letter-spacing:-.045em;margin-bottom:20px}h2{font-size:1.7rem;margin-bottom:16px}h3{font-size:1rem;line-height:1.35}code,pre,td,strong[data-testid],.sum-grid strong,.effect strong,.net strong,.calculation-result strong{font-family:var(--mono);font-variant-numeric:tabular-nums}.masthead{display:flex;justify-content:space-between;gap:16px;border-bottom:2px solid var(--ink);padding-bottom:14px;font:11px/1.4 var(--mono);letter-spacing:.09em}.hero{display:grid;grid-template-columns:minmax(0,1fr)260px;gap:60px;padding:46px 0 28px}.eyebrow{font:11px/1.5 var(--mono);letter-spacing:.065em;text-transform:uppercase;color:var(--link);margin-bottom:14px}.lead{font-size:17px;max-width:640px;margin:0;line-height:1.6}.completion{align-self:center;border-left:2px solid var(--line);padding:14px 0 14px 24px;font-size:12px;line-height:1.65}.completion-mark{display:block;color:var(--fixed);font:28px var(--mono);margin-bottom:12px}.completion strong{display:block;font-size:15px}.completion p{margin:8px 0 0}.masking-panel{border:1px solid var(--line);background:#fff;border-top:5px solid var(--ink);padding:28px 30px 18px;margin-bottom:30px;box-shadow:0 7px 0 -3px var(--line)}.sum-grid{display:grid;grid-template-columns:minmax(0,1fr)40px minmax(0,1fr);align-items:center;gap:18px}.sum-grid span{display:block;font-size:12px;margin-bottom:5px}.sum-grid strong{display:block;font-size:clamp(1.2rem,2.6vw,2.1rem);font-weight:500;letter-spacing:-.035em}.sum-grid .equals{font:30px var(--mono);text-align:center;margin:0}.effects{display:grid;grid-template-columns:1fr 1fr 1fr;gap:0;margin-top:28px;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}.effect,.net{padding:18px 16px;border-right:1px solid var(--line);min-width:0}.effect{color:var(--error);text-decoration:none;position:relative}.effect::before{content:'';position:absolute;left:0;top:18px;bottom:18px;width:3px;background:var(--error)}.effect span,.net span{display:block;font-size:11px;margin-bottom:8px}.effect strong,.net strong{font-size:1.22rem;font-weight:500}.net{border-right:0}.panel-note{margin:16px 0 0;font-size:12px;color:#42566D}.repairs{display:grid;grid-template-columns:1fr 1fr;gap:28px;margin:44px 0}.repairs article{border-top:2px solid var(--link);padding-top:20px;min-width:0}.repairs h2{font-size:1.55rem;max-width:440px}.repairs p:not(.eyebrow){font-size:13px}.repairs dl{margin:20px 0;font-size:12px}.repairs dt{color:#42566D;margin-top:10px}.repairs dd{margin:0;font:13px/1.8 var(--mono);font-variant-numeric:tabular-nums}.repairs a{font-size:12px}.fixed{color:var(--fixed);font-weight:700}.case-nav{display:flex;flex-wrap:wrap;gap:8px 18px;padding:16px 0;margin:12px 0 28px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);font-size:12px}.case-card{background:#fff;border:1px solid var(--line);border-radius:4px;margin-bottom:24px;padding:28px 30px;min-width:0}.case-heading{display:flex;justify-content:space-between;align-items:flex-start;gap:24px}.case-heading .eyebrow{margin-bottom:6px}.case-heading h2{font-size:1.8rem;margin-bottom:12px}.status{font:11px var(--mono);padding:8px 10px;white-space:nowrap;border:1px solid currentColor;border-radius:2px}.good{color:var(--fixed)}.bad{color:var(--error)}.case-total{font-size:12px;color:#42566D;margin:0 0 24px}.case-total strong{display:inline-block;margin-left:8px;font:13px var(--mono);color:var(--ink)}.issues{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,310px),1fr));gap:20px}.issue{border-left:3px solid var(--error);background:var(--paper);padding:18px 18px 16px;min-width:0}.issue h3{margin-bottom:12px}.issue p{font-size:13px;overflow-wrap:anywhere}.calculation-result{border-top:1px solid var(--line);padding-top:12px;margin-top:16px}.calculation-result span{display:block;font-size:11px;margin-bottom:4px}.calculation-result strong{font-size:1.3rem;font-weight:500}.formula{font-size:11px;margin:10px 0}.formula summary{cursor:pointer;color:var(--link)}.formula code{display:block;padding:8px 0;overflow-wrap:anywhere;white-space:pre-wrap}.evidence{display:flex;flex-wrap:wrap;gap:5px;margin-top:14px}.evidence>span{width:100%;font-size:10px;color:#42566D;margin-bottom:3px}.evidence-link{font:9px/1.5 var(--mono);border-bottom:1px solid var(--line);text-decoration:none;overflow-wrap:anywhere;max-width:100%}.table-scroll{overflow-x:auto;margin-top:25px}table{width:100%;border-collapse:collapse;font-size:12px;text-align:right;font-variant-numeric:tabular-nums}caption{text-align:left;font-size:12px;font-weight:600;margin-bottom:10px}th,td{padding:9px 10px;border-bottom:1px solid var(--line);white-space:nowrap}th:first-child{text-align:left;padding-left:0}thead th{font-size:10px;font-weight:500;color:#42566D}tbody th{font-size:11px;font-weight:500}.policy-oracle{color:#52667D}.fineprint{font-size:11px;line-height:1.6;color:#52667D;margin:12px 0 20px}.clean-note{background:var(--paper);border-left:3px solid var(--fixed);padding:18px;font-size:13px}.source-heading{border-top:1px solid var(--line);padding-top:20px;margin-top:22px}.source-heading h3{margin-bottom:4px}.source-heading p{font-size:11px;color:#52667D}.sources{display:grid;grid-template-columns:1fr 1fr;gap:8px 16px}.source-card{border:1px solid var(--line);min-width:0;font-size:11px;align-self:start}.source-card summary{cursor:pointer;padding:12px;list-style-position:inside}.source-card summary span{font-size:12px;font-weight:600}.source-card summary code{display:block;padding-top:4px;color:#52667D;font-size:9px;overflow-wrap:anywhere}.source-card pre{font-size:10px;padding:12px;line-height:1.65;margin:0;background:var(--paper);white-space:pre-wrap;overflow-wrap:anywhere;border-top:1px solid var(--line)}.source-card:target,.issue:target{outline:3px solid var(--link);outline-offset:3px}.renaming{padding:24px 0 10px;margin-top:42px;border-top:2px solid var(--ink)}.renaming>p{font-size:13px;max-width:740px}footer{margin:30px -28px 0;padding:24px 28px 30px;border-top:1px solid var(--line);font-size:11px;color:#52667D}footer strong{color:var(--ink)}footer p{max-width:800px;margin:8px 0 0}
@media(max-width:760px){main{padding:20px 16px 0}.masthead{font-size:9px;flex-wrap:wrap;gap:8px}.hero{grid-template-columns:1fr;gap:22px;padding:30px 0 24px}.completion{border-left:0;border-top:1px solid var(--line);padding:15px 0 0}.completion-mark{display:inline-block;font-size:18px;margin:0 8px 0 0}.completion strong{display:inline}.completion p{max-width:500px}.hero h1 br{display:none}.masking-panel{padding:20px 18px 16px}.sum-grid{grid-template-columns:1fr;gap:8px}.sum-grid .equals{text-align:left;font-size:22px}.sum-grid strong{font-size:1.6rem}.effects{grid-template-columns:1fr}.effect,.net{padding:13px 12px;border-right:0;border-bottom:1px solid var(--line)}.net{border-bottom:0}.effect span,.net span{margin-bottom:3px}.effect strong,.net strong{font-size:1.1rem}.repairs{grid-template-columns:1fr;gap:26px;margin:32px 0}.case-card{padding:21px 17px}.case-heading{flex-direction:column;gap:0;margin-bottom:15px}.case-heading h2{font-size:1.6rem}.sources{grid-template-columns:1fr}.case-total strong{display:block;margin:4px 0 0}footer{margin-left:-16px;margin-right:-16px;padding-left:16px;padding-right:16px}.lead{font-size:15px}}
@media print{body{background:#fff;font-size:10pt}main{max-width:none;padding:0}.masthead{font-size:8pt}.hero{padding:20px 0;gap:20px}.hero h1{font-size:30pt}.completion{font-size:8pt}.masking-panel{box-shadow:none;break-inside:avoid}.repairs{break-inside:avoid;margin:25px 0}.case-nav{display:none}.case-card{break-before:auto;border-radius:0;padding:18px;margin-bottom:18px}.case-heading h2{font-size:19pt}.issue,.source-card,tr{break-inside:avoid}.evidence-link{font-size:7pt}.source-card pre{font-size:7pt}.source-card::details-content{content-visibility:visible;height:auto}.source-card:not([open])>pre{display:block!important}.formula{display:none}a{color:var(--ink);text-decoration:none}footer{margin:15px 0 0;padding:15px 0}.fineprint{font-size:8pt}}
'''
