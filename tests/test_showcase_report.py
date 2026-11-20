"""Completed receipts, actual trajectories, HTML safety and publication guards."""

from __future__ import annotations

import hashlib
import html
import json
import shutil
import stat
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import pytest

from fin_audit.contracts import canonical_json
from fin_audit.showcase_report import ShowcaseReportError, render_showcase_report
from fin_audit.stage4 import run_showcase


class Document(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.ids = []
        self.hrefs = []
        self.resources = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        attributes = dict(attrs)
        if "id" in attributes:
            self.ids.append(attributes["id"])
        if "href" in attributes:
            self.hrefs.append(attributes["href"])
        for key in ("src", "srcset", "action"):
            if key in attributes:
                self.resources.append(attributes[key])
        assert not any(key.startswith("on") for key in attributes)

    def handle_data(self, value):
        self.text.append(value)


@pytest.fixture(scope="module")
def completed_receipt(tmp_path_factory):
    folder = tmp_path_factory.mktemp("showcase-render-input").resolve()
    receipt = folder / "receipt"
    # This unit fixture executes all 27 actual workers. Source-freeze acceptance
    # is checked by the real CLI; parallel development must not make this safety
    # regression depend on other agents' edits.
    with patch("fin_audit.stage4.source_hashes", return_value={"unit-fixture": "a" * 64}):
        summary = run_showcase(profile="cooperative", output=receipt)
    assert summary["passed"] is True
    return receipt


@pytest.fixture
def receipt(completed_receipt, tmp_path):
    directory = tmp_path.resolve() / "receipt"
    shutil.copytree(completed_receipt, directory)
    return directory


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")


def reseal(receipt, *names):
    complete = read_json(receipt / "COMPLETE.json")
    for name in names:
        complete["sha256"][name] = hashlib.sha256((receipt / name).read_bytes()).hexdigest()
    write_json(receipt / "COMPLETE.json", complete)


def test_real_completed_report_has_exact_money_offline_links_and_explicit_oracle(receipt, tmp_path):
    output = render_showcase_report(receipt, tmp_path / "showcase.html")
    content = output.read_text(encoding="utf-8")
    document = Document()
    document.feed(content)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert '<html lang="ru">' in content
    assert content.count('data-testid="clean-total"') == 1
    assert content.count('data-testid="masked-total"') == 1
    assert "550 000,00 ₸" in content
    assert "+50 000,00 ₸" in content and "−50 000,00 ₸" in content
    assert "49 925,00 ₸" in content and "50 000,00 ₸" in content
    assert "Oracle · готовые ответы" in content
    assert "Oracle получает готовый правильный ответ" in content
    assert "Локальный запуск · изоляция не подтверждена" in content
    assert "expected_amount_minor" not in content
    assert "presentation_transforms" not in content
    assert "master_seed" not in content
    assert "provenance" not in content
    assert "iframe" not in document.tags and "script" not in document.tags
    assert not set(document.tags).intersection({"img", "object", "embed", "link", "form"})
    assert not document.resources
    assert len(document.ids) == len(set(document.ids))
    assert document.hrefs and all(href.startswith("#") and href[1:] in document.ids for href in document.hrefs)
    assert content.count('data-testid="policy-results"') == 9
    assert len(content.encode()) < 256 * 1024


def test_all_untrusted_human_text_is_escaped_even_in_completed_resealed_analysis(receipt, tmp_path):
    payload = '<script>alert("xss")</script><img src=x onerror=alert(1)><a href="javascript:alert(1)">bad</a>'
    analysis = read_json(receipt / "analysis.json")
    analysis["cards"][0]["label"] = payload
    analysis["cards"][1]["issues"][0]["calculation"]["formula"] = payload
    write_json(receipt / "analysis.json", analysis)
    reseal(receipt, "analysis.json")
    output = render_showcase_report(receipt, tmp_path / "escaped.html")
    content = output.read_text(encoding="utf-8")
    assert html.escape(payload, quote=True) in content
    assert payload not in content
    document = Document()
    document.feed(content)
    assert "script" not in document.tags and "img" not in document.tags
    assert not any(href.startswith("javascript:") for href in document.hrefs)


@pytest.mark.parametrize("name", ["analysis.json", "summary.json", "evaluation.json", "run-manifest.json", "trajectories.jsonl"])
def test_any_changed_receipt_file_fails_before_creating_output(receipt, tmp_path, name):
    with (receipt / name).open("ab") as handle:
        handle.write(b" ")
    output = tmp_path / "never-created.html"
    with pytest.raises(ShowcaseReportError, match="showcase_receipt_digest_mismatch"):
        render_showcase_report(receipt, output)
    assert not output.exists()


@pytest.mark.parametrize("change", ["incomplete", "failed", "wrong_version", "missing_file", "bad_manifest"])
def test_incomplete_or_wrong_receipts_never_become_successful_html(receipt, tmp_path, change):
    complete = read_json(receipt / "COMPLETE.json")
    if change == "incomplete":
        complete["completed"] = False
    elif change == "failed":
        complete["passed"] = False
    elif change == "wrong_version":
        complete["showcase_version"] = "99"
    elif change == "missing_file":
        (receipt / "evaluation.json").unlink()
    elif change == "bad_manifest":
        complete["sha256"]["../../outside"] = "0" * 64
    write_json(receipt / "COMPLETE.json", complete)
    output = tmp_path / "invalid.html"
    with pytest.raises(ShowcaseReportError):
        render_showcase_report(receipt, output)
    assert not output.exists()


def test_resealed_false_financial_numbers_are_independently_rejected(receipt, tmp_path):
    analysis = read_json(receipt / "analysis.json")
    analysis["masking"]["masked_total_minor"] += 1
    write_json(receipt / "analysis.json", analysis)
    reseal(receipt, "analysis.json")
    with pytest.raises(ShowcaseReportError, match="showcase_analysis_mismatch"):
        render_showcase_report(receipt, tmp_path / "false-total.html")


def test_actual_episode_documents_are_covered_by_the_manifest(receipt, tmp_path):
    path = receipt / "episodes/masked_both/public/ledger.csv"
    with path.open("ab") as handle:
        handle.write(b" ")
    with pytest.raises(ShowcaseReportError, match="showcase_episode_file_digest_mismatch"):
        render_showcase_report(receipt, tmp_path / "changed-source.html")


def test_resealed_summary_and_evaluation_cannot_forge_actual_run_reward(receipt, tmp_path):
    summary = read_json(receipt / "summary.json")
    summary["runs"][0]["evaluation"]["reward"] = 0.123
    write_json(receipt / "summary.json", summary)
    write_json(receipt / "evaluation.json", summary["runs"])
    reseal(receipt, "summary.json", "evaluation.json")
    with pytest.raises(ShowcaseReportError, match="actual_evaluation_mismatch"):
        render_showcase_report(receipt, tmp_path / "forged-reward.html")


def test_resealed_wire_observation_cannot_disagree_with_recorded_actions(receipt, tmp_path):
    path = receipt / "trajectories.jsonl"
    rows = [json.loads(line) for line in path.read_text().split("\n") if line]
    rows[1]["observation"]["remaining_steps"] -= 1
    path.write_text("".join(canonical_json(row) + "\n" for row in rows), encoding="utf-8")
    reseal(receipt, "trajectories.jsonl")
    with pytest.raises(ShowcaseReportError, match="wire_trajectory_mismatch"):
        render_showcase_report(receipt, tmp_path / "forged-wire.html")


def test_existing_files_and_output_symlinks_are_never_replaced(receipt, tmp_path):
    existing = tmp_path / "existing.html"
    existing.write_text("keep this user file")
    with pytest.raises(FileExistsError):
        render_showcase_report(receipt, existing)
    assert existing.read_text() == "keep this user file"
    target = tmp_path / "missing-target.html"
    link = tmp_path / "output-link.html"
    link.symlink_to(target)
    with pytest.raises(FileExistsError):
        render_showcase_report(receipt, link)
    assert link.is_symlink() and not target.exists()
    assert not list(tmp_path.glob(".showcase-report-*.tmp"))


def test_report_cannot_be_written_inside_receipt_even_through_dotdot(receipt, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    bypass = outside / ".." / "receipt" / "new-report.html"
    with pytest.raises(ShowcaseReportError, match="report_must_be_outside_receipt"):
        render_showcase_report(receipt, bypass)
    assert not (receipt / "new-report.html").exists()


def test_symlink_receipt_or_output_parent_is_rejected(receipt, tmp_path):
    receipt_link = tmp_path / "receipt-link"
    receipt_link.symlink_to(receipt, target_is_directory=True)
    with pytest.raises(ShowcaseReportError, match="report_directory_missing_or_symlink"):
        render_showcase_report(receipt_link, tmp_path / "link-input.html")
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ShowcaseReportError, match="report_directory_missing_or_symlink"):
        render_showcase_report(receipt, parent_link / "report.html")
    assert not (real_parent / "report.html").exists()


def test_symlinked_receipt_file_cannot_bypass_digest_validation(receipt, tmp_path):
    complete = receipt / "COMPLETE.json"
    target = tmp_path / "complete-copy.json"
    target.write_bytes(complete.read_bytes())
    complete.unlink()
    complete.symlink_to(target)
    with pytest.raises(ShowcaseReportError, match="receipt_file_missing_or_unsafe"):
        render_showcase_report(receipt, tmp_path / "unsafe-input.html")
