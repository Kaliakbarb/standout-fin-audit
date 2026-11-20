"""Lossless episode packages and rejection of tampered accounting/evaluator data."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from fin_audit.contracts import EpisodeBundle, canonical_json
from fin_audit.generator import generate
from fin_audit.storage import FX_COLUMNS, LEDGER_COLUMNS, StorageError, export_episode, load_episode


@pytest.mark.parametrize("clean", [False, True])
def test_actual_generated_episode_roundtrips_exactly(tmp_path, clean):
    config = {"duplicate_count": 0, "allocation_count": 0} if clean else {}
    bundle = generate(19, config)
    target = tmp_path / "episode"
    export_episode(bundle, target)
    assert load_episode(target) == bundle
    assert {path.name for path in (target / "public").iterdir()} == {
        "manifest.json", "policy.json", "ledger.csv", "invoices.jsonl", "payments.jsonl", "fx.csv",
    }
    assert {path.name for path in (target / "private").iterdir()} == {"truth.json", "provenance.json"}


def test_csv_uses_explicit_null_and_exact_minor_integers(tmp_path):
    bundle = generate(19)
    target = tmp_path / "episode"
    export_episode(bundle, target)
    with (target / "public" / "ledger.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == list(LEDGER_COLUMNS)
        rows = list(reader)
    assert any(row["payment_id"] == "null" for row in rows)
    assert all(row["payment_id"] != "" and row["invoice_id"] != "" for row in rows)
    assert all(row["debit_minor"].isdigit() and row["credit_minor"].isdigit() for row in rows)
    assert load_episode(target).public == bundle.public


def test_empty_fx_still_contains_all_required_headers(tmp_path):
    target = tmp_path / "episode"
    export_episode(generate(2), target)
    assert (target / "public" / "fx.csv").read_text() == ",".join(FX_COLUMNS) + "\n"


def test_unicode_dates_offsets_and_rounding_survive_roundtrip(tmp_path):
    bundle = generate(7)
    target = tmp_path / "episode"
    export_episode(bundle, target)
    invoice_text = (target / "public" / "invoices.jsonl").read_text(encoding="utf-8")
    assert "Оборудование" in invoice_text
    loaded = load_episode(target)
    assert loaded.public["invoices"][0]["issued_at"].endswith("+05:00")
    assert loaded.public["manifest"]["currency_exponents"] == {"KZT": 2}
    assert loaded.public["policy"]["rounding"] == "ROUND_HALF_EVEN"


def test_protected_artifacts_have_restrictive_permissions(tmp_path):
    target = tmp_path / "episode"
    export_episode(generate(3), target)
    assert stat.S_IMODE((target / "private").stat().st_mode) == 0o700
    for name in ("truth.json", "provenance.json"):
        assert stat.S_IMODE((target / "private" / name).stat().st_mode) == 0o600


def test_existing_directory_is_never_overwritten_even_when_empty(tmp_path):
    target = tmp_path / "episode"
    target.mkdir()
    with pytest.raises(FileExistsError):
        export_episode(generate(4), target)
    assert list(target.iterdir()) == []


def test_existing_episode_is_unchanged_by_second_export(tmp_path):
    target = tmp_path / "episode"
    original = generate(4)
    export_episode(original, target)
    with pytest.raises(FileExistsError):
        export_episode(generate(5), target)
    assert load_episode(target) == original


def test_competing_exporters_cannot_replace_each_other(tmp_path):
    target = tmp_path / "episode"

    def attempt(seed):
        try:
            export_episode(generate(seed), target)
            return seed
        except FileExistsError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, [19, 20]))
    winner, = [seed for seed in outcomes if seed is not None]
    assert load_episode(target) == generate(winner)
    assert not list(tmp_path.glob(".fin-audit-export-*"))


def test_corrupt_public_balance_is_rejected_without_using_private_truth(tmp_path):
    target = tmp_path / "episode"
    export_episode(generate(10), target)
    path = target / "public" / "ledger.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields, rows = reader.fieldnames, list(reader)
    row = next(row for row in rows if int(row["debit_minor"]) > 0)
    row["debit_minor"] = str(int(row["debit_minor"]) + 1)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(StorageError, match="unbalanced_general_ledger"):
        load_episode(target)


def test_wrong_private_gold_amount_is_rejected(tmp_path):
    target = tmp_path / "episode"
    export_episode(generate(11), target)
    path = target / "private" / "truth.json"
    truth = json.loads(path.read_text())
    truth[0]["expected_amount_minor"] += 1
    path.write_text(canonical_json(truth), encoding="utf-8")
    with pytest.raises(StorageError, match="truth_does_not_match"):
        load_episode(target)


def test_missing_private_issue_is_rejected_by_full_reconciliation(tmp_path):
    target = tmp_path / "episode"
    export_episode(generate(12), target)
    (target / "private" / "truth.json").write_text("[]\n")
    with pytest.raises(StorageError, match="truth_does_not_match"):
        load_episode(target)


def test_incomplete_gold_proof_is_not_accepted(tmp_path):
    target = tmp_path / "episode"
    export_episode(generate(13), target)
    path = target / "private" / "truth.json"
    truth = json.loads(path.read_text())
    truth[0]["evidence_sets"][0].pop()
    path.write_text(canonical_json(truth))
    with pytest.raises(StorageError, match="truth_does_not_match"):
        load_episode(target)


@pytest.mark.parametrize("text", [
    '{"schema_version":"0.1.0","schema_version":"other"}',
    '{"schema_version":NaN}',
    "{broken",
])
def test_non_strict_or_malformed_json_is_rejected(tmp_path, text):
    target = tmp_path / "episode"
    export_episode(generate(14), target)
    (target / "public" / "manifest.json").write_text(text)
    with pytest.raises(StorageError, match="invalid_episode_json"):
        load_episode(target)


def test_blank_reference_cell_is_not_silently_guessed_as_null(tmp_path):
    target = tmp_path / "episode"
    export_episode(generate(15), target)
    path = target / "public" / "ledger.csv"
    path.write_text(path.read_text().replace(",null\n", ",\n", 1))
    with pytest.raises(StorageError, match="explicit_null"):
        load_episode(target)


def test_incomplete_or_symlinked_package_fails_closed(tmp_path):
    target = tmp_path / "episode"
    export_episode(generate(16), target)
    manifest = target / "public" / "manifest.json"
    moved = tmp_path / "manifest-copy.json"
    manifest.rename(moved)
    manifest.symlink_to(moved)
    with pytest.raises(StorageError, match="not_regular"):
        load_episode(target)


def test_private_permissions_cannot_accidentally_expose_evaluator_data(tmp_path):
    target = tmp_path / "episode"
    export_episode(generate(17), target)
    os.chmod(target / "private" / "truth.json", 0o644)
    with pytest.raises(StorageError, match="permissions_too_broad"):
        load_episode(target)


def test_invalid_bundle_is_rejected_before_creating_destination(tmp_path):
    original = generate(18)
    invalid = EpisodeBundle(public=original.public, truth=[], provenance=original.provenance)
    target = tmp_path / "episode"
    with pytest.raises(StorageError, match="truth_does_not_match"):
        export_episode(invalid, target)
    assert not target.exists()


def test_public_extension_cells_have_lossless_explicit_json_types(tmp_path):
    original = generate(21)
    public = copy.deepcopy(original.public)
    for row in public["ledger"]:
        row["journal_id"] = "journal-shared"
        row["source_amount_minor"] = row["debit_minor"] + row["credit_minor"]
        row["source_currency"] = "KZT"
        row["optional_note"] = None
    provenance = copy.deepcopy(original.provenance)
    provenance["public_sha256"] = hashlib.sha256(canonical_json(public).encode()).hexdigest()
    bundle = EpisodeBundle(public=public, truth=original.truth, provenance=provenance)
    target = tmp_path / "episode"
    export_episode(bundle, target)
    assert load_episode(target) == bundle


def test_missing_extension_key_null_and_literal_null_string_stay_distinct(tmp_path):
    original = generate(21)
    public = copy.deepcopy(original.public)
    public["ledger"][0]["optional_note"] = None
    public["ledger"][1]["optional_note"] = "null"
    public["ledger"][2]["optional_note"] = ""
    provenance = copy.deepcopy(original.provenance)
    provenance["public_sha256"] = hashlib.sha256(canonical_json(public).encode()).hexdigest()
    bundle = EpisodeBundle(public=public, truth=original.truth, provenance=provenance)
    target = tmp_path / "episode"
    export_episode(bundle, target)
    loaded = load_episode(target)
    assert loaded == bundle
    assert loaded.public["ledger"][0]["optional_note"] is None
    assert loaded.public["ledger"][1]["optional_note"] == "null"
    assert loaded.public["ledger"][2]["optional_note"] == ""
    assert "optional_note" not in loaded.public["ledger"][3]


def test_jsonl_preserves_unicode_line_separator_inside_text(tmp_path):
    original = generate(23)
    public = copy.deepcopy(original.public)
    public["invoices"][0]["line_items"][0]["description"] = "Первая строка\u2028Вторая строка"
    provenance = copy.deepcopy(original.provenance)
    provenance["public_sha256"] = hashlib.sha256(canonical_json(public).encode()).hexdigest()
    bundle = EpisodeBundle(public=public, truth=original.truth, provenance=provenance)
    target = tmp_path / "episode"
    export_episode(bundle, target)
    assert load_episode(target) == bundle


def test_wrong_public_digest_in_provenance_is_rejected(tmp_path):
    target = tmp_path / "episode"
    export_episode(generate(22), target)
    path = target / "private" / "provenance.json"
    provenance = json.loads(path.read_text())
    provenance["public_sha256"] = "0" * 64
    path.write_text(canonical_json(provenance))
    with pytest.raises(StorageError, match="digest_mismatch"):
        load_episode(target)
