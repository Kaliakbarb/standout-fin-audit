"""Trusted, lossless public/private episode export for supported versioned models.

This module is never copied into the agent runtime. Export requires a new target
directory and never overwrites an existing one. A failed publication is removed;
the whole directory tree is not claimed to be transactionally atomic to readers.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from .checker import reconcile
from .contracts import EpisodeBundle, canonical_json, strict_json_loads


class StorageError(ValueError):
    """An episode package is malformed, inconsistent, or unsafe to interpret."""


LEDGER_COLUMNS = (
    "row_id", "timestamp", "account", "debit_minor", "credit_minor", "currency",
    "entity_id", "invoice_id", "payment_id",
)
FX_COLUMNS = ("date", "source_currency", "target_currency", "rate")
_PUBLIC_FILES = {"manifest.json", "policy.json", "ledger.csv", "invoices.jsonl", "payments.jsonl", "fx.csv"}
_PRIVATE_FILES = {"truth.json", "provenance.json"}
_REFERENCE_FIELDS = {"invoice_id", "payment_id"}
_MINOR_FIELDS = {"debit_minor", "credit_minor"}
_CSV_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_MAX_FILE_BYTES = 64 * 1024 * 1024


def _normalized_truth(truth: Any) -> list[str]:
    """Normalize order, not meaning, for this version's canonical private truth."""
    if not isinstance(truth, list):
        raise StorageError("truth_requires_array")
    required = {"issue_id", "type", "affected_ids", "expected_amount_minor", "currency", "evidence_sets"}
    result = []
    for issue in truth:
        if not isinstance(issue, dict) or set(issue) != required:
            raise StorageError("invalid_truth_record_fields")
        for name in ("issue_id", "type", "currency"):
            if not isinstance(issue[name], str) or not issue[name]:
                raise StorageError("invalid_truth_string")
        amount = issue["expected_amount_minor"]
        if type(amount) is not int or amount <= 0:
            raise StorageError("invalid_truth_amount")
        affected = issue["affected_ids"]
        if not isinstance(affected, list) or not affected or any(not isinstance(identity, str) or not identity for identity in affected):
            raise StorageError("invalid_truth_affected_ids")
        if len(affected) != len(set(affected)):
            raise StorageError("duplicate_truth_affected_id")
        proofs = issue["evidence_sets"]
        if not isinstance(proofs, list) or not proofs:
            raise StorageError("truth_requires_evidence_sets")
        normalized_proofs = []
        for proof in proofs:
            if not isinstance(proof, list) or not 1 <= len(proof) <= 10:
                raise StorageError("invalid_truth_evidence_set")
            if any(not isinstance(identity, str) or not identity for identity in proof) or len(proof) != len(set(proof)):
                raise StorageError("invalid_truth_evidence_id")
            normalized_proofs.append(sorted(proof))
        normalized = dict(issue)
        normalized["affected_ids"] = sorted(affected)
        normalized["evidence_sets"] = sorted(normalized_proofs)
        result.append(canonical_json(normalized))
    return sorted(result)


def _validated_bundle(bundle: EpisodeBundle) -> EpisodeBundle:
    if not isinstance(bundle, EpisodeBundle):
        raise StorageError("export_requires_episode_bundle")
    try:
        snapshot = strict_json_loads(canonical_json({
            "public": bundle.public, "truth": bundle.truth, "provenance": bundle.provenance,
        }))
    except (TypeError, ValueError) as exc:
        raise StorageError("episode_is_not_strict_json") from exc
    public, truth, provenance = snapshot["public"], snapshot["truth"], snapshot["provenance"]
    if not isinstance(public, dict) or set(public) != {"manifest", "policy", "ledger", "invoices", "payments", "fx"}:
        raise StorageError("invalid_public_episode_fields")
    try:
        derived = reconcile(public)
    except ValueError as exc:
        raise StorageError(f"invalid_public_episode:{exc}") from exc
    if _normalized_truth(truth) != _normalized_truth(derived):
        raise StorageError("private_truth_does_not_match_independent_reconciliation")
    if not isinstance(provenance, dict):
        raise StorageError("provenance_requires_object")
    schema_version = provenance.get("schema_version")
    if schema_version is not None and schema_version != public["manifest"]["schema_version"]:
        raise StorageError("provenance_schema_version_mismatch")
    public_digest = hashlib.sha256(canonical_json(public).encode("utf-8")).hexdigest()
    if "public_sha256" in provenance and provenance["public_sha256"] != public_digest:
        raise StorageError("provenance_public_digest_mismatch")
    return EpisodeBundle(public=public, truth=truth, provenance=provenance)


def _write_text(path: Path, text: str, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor = -1
            handle.write(text)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _csv_text(rows: list[dict[str, Any]], mandatory: tuple[str, ...]) -> str:
    extras = set()
    for row in rows:
        if not isinstance(row, dict) or not set(mandatory).issubset(row):
            raise StorageError("csv_record_missing_mandatory_field")
        for key in row:
            if not isinstance(key, str) or not key or len(key) > 200:
                raise StorageError("invalid_csv_column_name")
        extras.update(set(row) - set(mandatory))
    fields = [*mandatory, *sorted(extras)]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        encoded = {}
        for key in fields:
            value = row.get(key)
            if key in mandatory and key in _MINOR_FIELDS:
                if type(value) is not int or value < 0:
                    raise StorageError("csv_money_requires_nonnegative_integer")
                encoded[key] = str(value)
            elif key in mandatory and key in _REFERENCE_FIELDS:
                if value is None:
                    encoded[key] = "null"
                elif not isinstance(value, str) or not value or value == "null":
                    raise StorageError("invalid_or_reserved_csv_reference")
                else:
                    encoded[key] = value
            elif key in mandatory:
                if not isinstance(value, str) or not value:
                    raise StorageError("csv_core_value_requires_nonempty_string")
                encoded[key] = value
            else:
                # Versioned public extensions have JSON-encoded cells. Thus a
                # decimal string stays a string, integers stay integers, and
                # null is distinct from the literal string "null". Empty CSV
                # extension cells mean the key was absent, not present/null.
                encoded[key] = canonical_json(value) if key in row else ""
        writer.writerow(encoded)
    return buffer.getvalue()


def export_episode(bundle: EpisodeBundle, directory: Path) -> None:
    """Export six public files and two protected evaluator-only files.

    The target directory must not exist, even if empty. Existing files are never
    replaced. Native v1 and multi-currency v2 truth are independently verified
    before writes. Signed source documents and JSON-encoded CSV metadata retain
    their exact integer/string/null types.
    """
    directory = Path(directory)
    snapshot = _validated_bundle(bundle)
    if directory.exists() or directory.is_symlink():
        raise FileExistsError(f"episode_directory_exists:{directory}")
    directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".fin-audit-export-", dir=directory.parent))
    reserved = False
    try:
        public_dir, private_dir = staging / "public", staging / "private"
        public_dir.mkdir(mode=0o755)
        private_dir.mkdir(mode=0o700)
        os.chmod(public_dir, 0o755)
        os.chmod(private_dir, 0o700)
        for name in ("manifest", "policy"):
            _write_text(public_dir / f"{name}.json", canonical_json(snapshot.public[name]) + "\n", 0o644)
        for name in ("invoices", "payments"):
            text = "".join(canonical_json(record) + "\n" for record in snapshot.public[name])
            _write_text(public_dir / f"{name}.jsonl", text, 0o644)
        _write_text(public_dir / "ledger.csv", _csv_text(snapshot.public["ledger"], LEDGER_COLUMNS), 0o644)
        _write_text(public_dir / "fx.csv", _csv_text(snapshot.public["fx"], FX_COLUMNS), 0o644)
        _write_text(private_dir / "truth.json", canonical_json(snapshot.truth) + "\n", 0o600)
        _write_text(private_dir / "provenance.json", canonical_json(snapshot.provenance) + "\n", 0o600)
        # Exclusive mkdir provides a no-overwrite guarantee even when two
        # exporters race for the same name. Portable rename alone may replace
        # an existing empty directory on POSIX.
        directory.mkdir(mode=0o755)
        reserved = True
        os.chmod(directory, 0o755)
        os.rename(private_dir, directory / "private")
        os.rename(public_dir, directory / "public")
    except BaseException:
        if reserved:
            shutil.rmtree(directory)
        raise
    finally:
        shutil.rmtree(staging)


def _read_text(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise StorageError("episode_file_missing_or_not_regular")
    if path.stat().st_size > _MAX_FILE_BYTES:
        raise StorageError("episode_file_size_limit_exceeded")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise StorageError("episode_file_unreadable") from exc


def _json_file(path: Path) -> Any:
    try:
        return strict_json_loads(_read_text(path))
    except StorageError:
        raise
    except (TypeError, ValueError) as exc:
        raise StorageError("invalid_episode_json") from exc


def _jsonl_file(path: Path) -> list[dict[str, Any]]:
    result = []
    text = _read_text(path)
    if not text:
        return result
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    # str.splitlines also splits U+2028/U+2029 *inside* valid JSON strings.
    # JSON Lines records are delimited by actual LF, so preserve those Unicode
    # text characters rather than manufacturing extra records.
    for line in lines:
        try:
            item = strict_json_loads(line)
        except (TypeError, ValueError) as exc:
            raise StorageError("invalid_episode_jsonl") from exc
        if not isinstance(item, dict):
            raise StorageError("jsonl_record_requires_object")
        result.append(item)
    return result


def _csv_file(path: Path, mandatory: tuple[str, ...]) -> list[dict[str, Any]]:
    try:
        reader = csv.DictReader(io.StringIO(_read_text(path), newline=""), strict=True)
        fields = reader.fieldnames
        if not fields or len(set(fields)) != len(fields) or not set(mandatory).issubset(fields):
            raise StorageError("invalid_csv_header")
        if any(not name or len(name) > 200 for name in fields):
            raise StorageError("invalid_csv_column_name")
        result = []
        for raw in reader:
            if None in raw or any(value is None for value in raw.values()):
                raise StorageError("csv_row_width_mismatch")
            decoded = {}
            for key, value in raw.items():
                if key in mandatory and key in _MINOR_FIELDS:
                    if not _CSV_INTEGER.fullmatch(value):
                        raise StorageError("invalid_csv_minor_integer")
                    decoded[key] = int(value)
                elif key in mandatory and key in _REFERENCE_FIELDS:
                    if not value:
                        raise StorageError("csv_reference_requires_explicit_null")
                    decoded[key] = None if value == "null" else value
                elif key in mandatory:
                    if not value:
                        raise StorageError("empty_csv_core_value")
                    decoded[key] = value
                else:
                    if value == "":
                        continue
                    try:
                        decoded[key] = strict_json_loads(value)
                    except (TypeError, ValueError) as exc:
                        raise StorageError("invalid_json_encoded_csv_extension") from exc
            result.append(decoded)
        return result
    except csv.Error as exc:
        raise StorageError("invalid_episode_csv") from exc


def load_episode(directory: Path) -> EpisodeBundle:
    """Read an evaluator-owned package and reject inconsistent public/gold data.

    This is a trusted loader, never an agent tool. Private directory/file modes
    must prohibit group/other access; symlinks and incomplete packages fail closed.
    """
    directory = Path(directory)
    public_dir, private_dir = directory / "public", directory / "private"
    for path in (directory, public_dir, private_dir):
        if path.is_symlink() or not path.is_dir():
            raise StorageError("episode_directory_missing_or_not_regular")
    if {path.name for path in public_dir.iterdir()} != _PUBLIC_FILES:
        raise StorageError("public_file_set_mismatch")
    if {path.name for path in private_dir.iterdir()} != _PRIVATE_FILES:
        raise StorageError("private_file_set_mismatch")
    if stat.S_IMODE(private_dir.stat().st_mode) & 0o077:
        raise StorageError("private_directory_permissions_too_broad")
    for name in _PRIVATE_FILES:
        path = private_dir / name
        if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise StorageError("private_file_permissions_too_broad")
    public = {
        "manifest": _json_file(public_dir / "manifest.json"),
        "policy": _json_file(public_dir / "policy.json"),
        "ledger": _csv_file(public_dir / "ledger.csv", LEDGER_COLUMNS),
        "invoices": _jsonl_file(public_dir / "invoices.jsonl"),
        "payments": _jsonl_file(public_dir / "payments.jsonl"),
        "fx": _csv_file(public_dir / "fx.csv", FX_COLUMNS),
    }
    return _validated_bundle(EpisodeBundle(
        public=public,
        truth=_json_file(private_dir / "truth.json"),
        provenance=_json_file(private_dir / "provenance.json"),
    ))
