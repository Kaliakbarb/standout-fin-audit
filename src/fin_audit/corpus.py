"""Versioned open corpus and trusted-only local holdout configuration plans.

Open train/dev seeds and gold are deliberately inspectable outside the agent
jail. Actual holdout seeds are derived only in the trusted caller's process;
neither a corpus index nor a holdout spec is an agent observation.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import os
import re
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from .checker import reconcile
from .contracts import SCHEMA_VERSION, canonical_json
from .generator import generate
from .storage import export_episode, load_episode

PLAN_VERSION = "0.3.0"
_SPLITS = ("train", "dev")
_FILES = (
    "public/manifest.json", "public/policy.json", "public/ledger.csv",
    "public/invoices.jsonl", "public/payments.jsonl", "public/fx.csv",
    "private/truth.json", "private/provenance.json",
)
_TRAIN = (
    {},
    {"duplicate_count": 1, "ledger_rows": 240},
    {"allocation_count": 1, "foreign_invoice_count": 0, "invoice_count": 18, "ledger_rows": 200},
    {"fx_count": 1, "fx_target": "invoice", "ledger_rows": 320, "refund_count": 0},
    {"cutoff_count": 1, "ledger_rows": 260},
    {"allocation_count": 1, "fx_count": 1, "interaction_allocation_fx": True, "ledger_rows": 280},
    {"allocation_count": 1, "cutoff_count": 1, "interaction_allocation_cutoff": True, "foreign_currency": "JPY", "foreign_exponent": 0, "ledger_rows": 280},
    {"fx_count": 1, "fx_mode": "rate", "foreign_currency": "KWD", "foreign_exponent": 3, "invoice_count": 30, "foreign_invoice_count": 9, "ledger_rows": 360, "refund_count": 2},
    {"duplicate_count": 1, "allocation_count": 1, "fx_count": 1, "cutoff_count": 1, "invoice_count": 40, "foreign_invoice_count": 12, "ledger_rows": 620, "refund_count": 2},
    {"foreign_currency": "JPY", "foreign_exponent": 0, "invoice_count": 28, "foreign_invoice_count": 8, "ledger_rows": 252},
)
_DEV = (
    {"foreign_currency": "KWD", "foreign_exponent": 3, "ledger_rows": 250, "refund_count": 2},
    {"duplicate_count": 1, "invoice_count": 20, "foreign_invoice_count": 5, "ledger_rows": 233},
    {"allocation_count": 1, "allocation_mode": "missing", "ledger_rows": 271},
    {"fx_count": 1, "foreign_currency": "JPY", "foreign_exponent": 0, "invoice_count": 28, "foreign_invoice_count": 8, "ledger_rows": 313},
    {"cutoff_count": 1, "foreign_currency": "KWD", "foreign_exponent": 3, "invoice_count": 28, "foreign_invoice_count": 8, "ledger_rows": 347, "refund_count": 2},
    {"allocation_count": 1, "fx_count": 1, "fx_mode": "rate", "interaction_allocation_fx": True, "foreign_currency": "KWD", "foreign_exponent": 3, "invoice_count": 30, "foreign_invoice_count": 9, "ledger_rows": 389},
    {"allocation_count": 1, "cutoff_count": 1, "allocation_mode": "missing", "interaction_allocation_cutoff": True, "invoice_count": 30, "foreign_invoice_count": 10, "ledger_rows": 431},
    {"duplicate_count": 1, "allocation_count": 2, "fx_count": 1, "cutoff_count": 1, "interaction_allocation_fx": True, "interaction_allocation_cutoff": True, "foreign_currency": "JPY", "foreign_exponent": 0, "invoice_count": 36, "foreign_invoice_count": 12, "ledger_rows": 547, "refund_count": 2},
    {"duplicate_count": 2, "allocation_count": 2, "fx_count": 2, "cutoff_count": 2, "invoice_count": 48, "foreign_invoice_count": 20, "ledger_rows": 880, "refund_count": 2},
    {"invoice_count": 60, "foreign_invoice_count": 18, "ledger_rows": 1400, "refund_count": 3},
)
_HOLDOUT = (
    ("clean", {"invoice_count": 32, "foreign_invoice_count": 10, "ledger_rows": 420, "refund_count": 2}),
    ("clean", {"foreign_currency": "JPY", "foreign_exponent": 0, "invoice_count": 26, "foreign_invoice_count": 7, "ledger_rows": 310, "refund_count": 2}),
    ("clean", {"foreign_currency": "KWD", "foreign_exponent": 3, "invoice_count": 34, "foreign_invoice_count": 11, "ledger_rows": 510, "refund_count": 3}),
    ("clean", {"invoice_count": 26, "foreign_invoice_count": 0, "ledger_rows": 293, "refund_count": 2}),
    ("clean", {"invoice_count": 52, "foreign_invoice_count": 16, "ledger_rows": 1200, "refund_count": 4}),
    ("interacting", {"allocation_count": 1, "fx_count": 1, "interaction_allocation_fx": True, "foreign_currency": "JPY", "foreign_exponent": 0, "invoice_count": 26, "foreign_invoice_count": 8, "ledger_rows": 333, "refund_count": 2}),
    ("interacting", {"allocation_count": 1, "fx_count": 1, "fx_mode": "rate", "interaction_allocation_fx": True, "invoice_count": 34, "foreign_invoice_count": 11, "ledger_rows": 555, "refund_count": 3}),
    ("interacting", {"allocation_count": 1, "cutoff_count": 1, "interaction_allocation_cutoff": True, "foreign_currency": "KWD", "foreign_exponent": 3, "invoice_count": 32, "foreign_invoice_count": 10, "ledger_rows": 477, "refund_count": 3}),
    ("interacting", {"allocation_count": 1, "cutoff_count": 1, "allocation_mode": "missing", "interaction_allocation_cutoff": True, "foreign_currency": "JPY", "foreign_exponent": 0, "invoice_count": 36, "foreign_invoice_count": 12, "ledger_rows": 641, "refund_count": 2}),
    ("interacting", {"duplicate_count": 2, "allocation_count": 2, "fx_count": 2, "cutoff_count": 2, "interaction_allocation_fx": True, "interaction_allocation_cutoff": True, "fx_mode": "rate", "foreign_currency": "KWD", "foreign_exponent": 3, "invoice_count": 46, "foreign_invoice_count": 18, "ledger_rows": 921, "refund_count": 3}),
    ("other", {"duplicate_count": 3, "foreign_currency": "JPY", "foreign_exponent": 0, "invoice_count": 30, "foreign_invoice_count": 9, "ledger_rows": 355, "refund_count": 2}),
    ("other", {"allocation_count": 3, "allocation_mode": "missing", "foreign_currency": "KWD", "foreign_exponent": 3, "invoice_count": 34, "foreign_invoice_count": 12, "ledger_rows": 463}),
    ("other", {"fx_count": 2, "fx_target": "invoice", "foreign_currency": "JPY", "foreign_exponent": 0, "invoice_count": 36, "foreign_invoice_count": 12, "ledger_rows": 533, "refund_count": 2}),
    ("other", {"fx_count": 3, "fx_mode": "rate", "invoice_count": 38, "foreign_invoice_count": 12, "ledger_rows": 583, "refund_count": 3}),
    ("other", {"cutoff_count": 3, "foreign_invoice_count": 0, "invoice_count": 30, "ledger_rows": 411, "refund_count": 2}),
    ("other", {"duplicate_count": 1, "fx_count": 1, "fx_target": "invoice", "foreign_currency": "KWD", "foreign_exponent": 3, "invoice_count": 34, "foreign_invoice_count": 11, "ledger_rows": 643, "refund_count": 2}),
    ("other", {"allocation_count": 2, "cutoff_count": 1, "invoice_count": 32, "foreign_invoice_count": 10, "ledger_rows": 487, "refund_count": 3}),
    ("other", {"duplicate_count": 2, "allocation_count": 1, "fx_count": 1, "foreign_currency": "JPY", "foreign_exponent": 0, "invoice_count": 40, "foreign_invoice_count": 14, "ledger_rows": 777, "refund_count": 2}),
    ("other", {"duplicate_count": 3, "allocation_count": 3, "fx_count": 3, "cutoff_count": 3, "invoice_count": 54, "foreign_invoice_count": 24, "ledger_rows": 1001, "refund_count": 3}),
    ("other", {"duplicate_count": 1, "allocation_count": 2, "fx_count": 1, "cutoff_count": 1, "fx_target": "invoice", "fx_mode": "rate", "foreign_currency": "KWD", "foreign_exponent": 3, "invoice_count": 60, "foreign_invoice_count": 18, "ledger_rows": 1501, "refund_count": 3}),
)


class CorpusError(ValueError):
    """An open corpus is not the frozen, independently verified release corpus."""


def _book(overrides: dict[str, Any]) -> dict[str, Any]:
    return {
        "invoice_count": 24, "ledger_rows": 200,
        "foreign_invoice_count": 6, "foreign_currency": "USD", "foreign_exponent": 2,
        "duplicate_count": 0, "allocation_count": 0, "fx_count": 0, "cutoff_count": 0,
        "refund_count": 1, "allocation_mode": "partial", "fx_mode": "date", "fx_target": "payment",
        "interaction_allocation_fx": False, "interaction_allocation_cutoff": False,
        "timezone": "Asia/Almaty", "period_start": "2026-11-01T00:00:00+05:00",
        "period_end": "2026-12-01T00:00:00+05:00", **copy.deepcopy(overrides),
    }


def corpus_specs() -> dict[str, list[dict[str, Any]]]:
    """Return fresh, public train/dev seed/config plans; no worker receives them."""
    result = {}
    for split, overrides in (("train", _TRAIN), ("dev", _DEV)):
        result[split] = []
        for index, override in enumerate(overrides, 1):
            key = f"{split}-{index:02}"
            seed = int.from_bytes(hashlib.sha256(f"genesis-open-corpus:{PLAN_VERSION}:{key}".encode()).digest()[:16], "big")
            result[split].append({"key": key, "seed": seed, "config": _book(override)})
    return result


def holdout_plan() -> dict[str, Any]:
    """Frozen structural plan, deliberately containing no actual evaluation seed."""
    episodes = []
    for index, (cohort, override) in enumerate(_HOLDOUT):
        config = _book(override)
        config.pop("ledger_rows")
        episodes.append({"key": f"h{index:02}", "cohort": cohort, "config": config})
    return {
        "plan_version": PLAN_VERSION, "schema_version": SCHEMA_VERSION,
        "purpose": "local_holdout_relative_to_agent_not_official_hidden_set",
        "seed_derivation": "HMAC-SHA256(master_seed_256bit,version/key/collision_counter) first128bits",
        "parameter_sampling": {"ledger_rows": {
            "minimum": 360, "maximum": 1500,
            "rule": "360 + HMAC-SHA256(master_seed_256bit,genesis-holdout-size:version/key) modulo1141",
            "same_distribution_for_all_cohorts": True,
        }},
        "episodes": episodes,
    }


def generate_holdout_specs(master_seed: int) -> list[dict[str, Any]]:
    """Derive 20 trusted-only specs from a master seed supplied by the harness.

    Five clean, five interacting and ten other cases contain new structural
    combinations of known tasks. Public source code freezes the plan; local
    holdout secrecy is relative to isolated agents, not to repository readers.
    """
    if type(master_seed) is not int or not 0 <= master_seed < 2**256:
        raise CorpusError("master_seed_requires_unsigned_256bit_integer")
    key = master_seed.to_bytes(32, "big")
    seen = {spec["seed"] for specs in corpus_specs().values() for spec in specs}
    result = []
    for item in holdout_plan()["episodes"]:
        counter = 0
        while True:
            message = f"genesis-holdout:{PLAN_VERSION}:{item['key']}:{counter}".encode()
            seed = int.from_bytes(hmac.new(key, message, hashlib.sha256).digest()[:16], "big")
            if seed not in seen:
                seen.add(seed)
                break
            counter += 1
        size_message = f"genesis-holdout-size:{PLAN_VERSION}:{item['key']}".encode()
        ledger_rows = 360 + int.from_bytes(hmac.new(key, size_message, hashlib.sha256).digest(), "big") % 1141
        result.append({**item, "seed": seed, "config": {**item["config"], "ledger_rows": ledger_rows}})
    return result


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def corpus_index_schema() -> dict[str, Any]:
    """JSON Schema for the public corpus index, with runtime financial checks too."""
    digest = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    spec = {
        "type": "object", "additionalProperties": False,
        "required": ["key", "seed", "config", "path", "episode_id", "files", "public_sha256", "issue_counts", "table_counts"],
        "properties": {
            "key": {"type": "string", "pattern": "^(train|dev)-[0-9]{2}$"},
            "seed": {"type": "integer", "minimum": 0, "maximum": 2**128 - 1},
            "config": {"type": "object"}, "path": {"type": "string"},
            "episode_id": {"type": "string", "minLength": 1},
            "files": {"type": "object", "required": list(_FILES), "properties": {name: digest for name in _FILES}, "additionalProperties": False},
            "public_sha256": digest,
            "issue_counts": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 1}},
            "table_counts": {"type": "object", "required": ["ledger", "invoices", "payments", "fx"], "properties": {name: {"type": "integer", "minimum": 0} for name in ("ledger", "invoices", "payments", "fx")}, "additionalProperties": False},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:genesis:corpus-index:{PLAN_VERSION}",
        "type": "object", "additionalProperties": False,
        "required": ["corpus_version", "schema_version", "purpose", "plan_sha256", "index_schema_sha256", "hash_scheme", "splits", "coverage"],
        "properties": {
            "corpus_version": {"const": PLAN_VERSION}, "schema_version": {"const": SCHEMA_VERSION},
            "purpose": {"const": "open_train_dev_outside_agent_jail"},
            "plan_sha256": digest, "index_schema_sha256": digest,
            "hash_scheme": {"const": "sha256_raw_file_bytes"},
            "splits": {"type": "object", "required": list(_SPLITS), "properties": {split: {"type": "array", "minItems": 10, "maxItems": 10, "items": spec} for split in _SPLITS}, "additionalProperties": False},
            "coverage": {"type": "object"},
        },
    }


def _coverage(entries: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    all_entries = [entry for split in _SPLITS for entry in entries[split]]
    return {
        "issue_types": sorted({kind for entry in all_entries for kind in entry["issue_counts"]}),
        "clean_episode_count": sum(not entry["issue_counts"] for entry in all_entries),
        "foreign_exponents": sorted({entry["config"]["foreign_exponent"] for entry in all_entries if entry["config"]["foreign_invoice_count"]}),
        "fx_modes": sorted({entry["config"]["fx_mode"] for entry in all_entries if entry["config"]["fx_count"]}),
        "allocation_modes": sorted({entry["config"]["allocation_mode"] for entry in all_entries if entry["config"]["allocation_count"]}),
        "interaction_allocation_fx_count": sum(entry["config"]["interaction_allocation_fx"] for entry in all_entries),
        "interaction_allocation_cutoff_count": sum(entry["config"]["interaction_allocation_cutoff"] for entry in all_entries),
        "refund_episode_count": sum(entry["config"]["refund_count"] > 0 for entry in all_entries),
        "clean_ledger_sizes": sorted({entry["table_counts"]["ledger"] for entry in all_entries if not entry["issue_counts"]}),
        "clean_dirty_ledger_size_overlap": sorted(
            {entry["table_counts"]["ledger"] for entry in all_entries if not entry["issue_counts"]}
            & {entry["table_counts"]["ledger"] for entry in all_entries if entry["issue_counts"]}
        ),
        "ledger_size_range": [min(entry["table_counts"]["ledger"] for entry in all_entries), max(entry["table_counts"]["ledger"] for entry in all_entries)],
    }


def _safe_file(root: Path, relative: str) -> Path:
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts) or relative.startswith("/") or "\\" in relative:
        raise CorpusError("unsafe_corpus_path")
    current = root
    if root.is_symlink() or not root.is_dir():
        raise CorpusError("corpus_root_missing_or_symlinked")
    for index, part in enumerate(parts):
        current = current / part
        if current.is_symlink():
            raise CorpusError("corpus_symlink_forbidden")
        if index < len(parts) - 1 and not current.is_dir():
            raise CorpusError("corpus_directory_missing")
    if not current.is_file() or not current.resolve().is_relative_to(root.resolve()):
        raise CorpusError("corpus_file_missing_or_outside_root")
    if current.stat().st_size > 64 * 1024 * 1024:
        raise CorpusError("corpus_file_size_limit_exceeded")
    return current


def _file_hashes(root: Path, episode_path: str) -> dict[str, str]:
    return {name: hashlib.sha256(_safe_file(root, episode_path + "/" + name).read_bytes()).hexdigest() for name in _FILES}


def _protected_load(root: Path, episode_path: str):
    # Git preserves the executable bit, not0600/0700. These open gold fixtures
    # are intentionally readable to repository users. Do not chmod source data:
    # stage a protected trusted copy and retain strict runtime loader guarantees.
    for category in ("public", "private"):
        parent = root / episode_path / category
        expected = {name.split("/", 1)[1] for name in _FILES if name.startswith(category + "/")}
        if parent.is_symlink() or not parent.is_dir() or {path.name for path in parent.iterdir()} != expected:
            raise CorpusError("unindexed_or_missing_episode_file")
    with tempfile.TemporaryDirectory(prefix="fin-audit-open-corpus-") as temporary:
        target = Path(temporary) / "episode"
        (target / "public").mkdir(parents=True)
        (target / "private").mkdir(mode=0o700)
        os.chmod(target / "private", 0o700)
        for name in _FILES:
            source = _safe_file(root, episode_path + "/" + name)
            destination = target / name
            shutil.copyfile(source, destination)
            os.chmod(destination, 0o600 if name.startswith("private/") else 0o644)
        return load_episode(target)


def export_corpus(directory: Path) -> dict[str, Any]:
    """Create the frozen20 open episodes in a new directory; never overwrite."""
    directory = Path(directory)
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir()  # exclusive, including an existing empty directory
    try:
        entries = {split: [] for split in _SPLITS}
        for split, specs in corpus_specs().items():
            (directory / split).mkdir()
            for spec in specs:
                bundle = generate(spec["seed"], spec["config"])
                if reconcile(bundle.public) != bundle.truth:
                    raise CorpusError("generator_truth_does_not_match_blind_checker")
                relative = f"{split}/{spec['key']}"
                export_episode(bundle, directory / relative)
                if load_episode(directory / relative) != bundle:
                    raise CorpusError("export_roundtrip_mismatch")
                entries[split].append({
                    **spec, "path": relative,
                    "episode_id": bundle.public["manifest"]["episode_id"],
                    "files": _file_hashes(directory, relative),
                    "public_sha256": _digest(bundle.public),
                    "issue_counts": dict(sorted(Counter(issue["type"] for issue in bundle.truth).items())),
                    "table_counts": bundle.public["manifest"]["table_counts"],
                })
        manifest = {
            "corpus_version": PLAN_VERSION, "schema_version": SCHEMA_VERSION,
            "purpose": "open_train_dev_outside_agent_jail",
            "plan_sha256": _digest(corpus_specs()), "index_schema_sha256": _digest(corpus_index_schema()),
            "hash_scheme": "sha256_raw_file_bytes", "splits": entries, "coverage": _coverage(entries),
        }
        (directory / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
        (directory / "README.md").write_text(
            "# Открытые эпизоды FinTech\n\n"
            "10 train и10 dev эпизодов, план0.3.0, схема books0.2.0. Полные seed/config и SHA-256 находятся в manifest.json.\n\n"
            "Это открытые учебные fixtures: gold, provenance и seeds доступны человеку в репозитории. Агент получает только public записи через инструменты; индекс, seed и private не попадают в его runtime.\n\n"
            "Git не сохраняет права0600/0700: verify_corpus проверяет source hashes и использует временную protected копию для строгого load_episode, не меняя исходные файлы/права. Runtime export/load сохраняют строгие private permissions.\n\n"
            "Проверка: ./scripts/dev python -c \"from pathlib import Path; from fin_audit.corpus import verify_corpus; print(verify_corpus(Path('datasets')))\"\n",
            encoding="utf-8",
        )
        verify_corpus(directory)
        return manifest
    except BaseException:
        shutil.rmtree(directory)
        raise


def verify_corpus(directory: Path) -> dict[str, Any]:
    """Verify hashes, frozen split plans, regeneration and every financial book.

    Corrupt/incomplete/unsafe input raises CorpusError, never a green partial
    report. Source permissions and bytes are never altered during verification.
    """
    directory = Path(directory)
    from .contracts import strict_json_loads

    try:
        manifest = strict_json_loads(_safe_file(directory, "manifest.json").read_text(encoding="utf-8"))
        required = {"corpus_version", "schema_version", "purpose", "plan_sha256", "index_schema_sha256", "hash_scheme", "splits", "coverage"}
        if type(manifest) is not dict or set(manifest) != required:
            raise CorpusError("invalid_corpus_manifest_fields")
        if manifest["corpus_version"] != PLAN_VERSION or manifest["schema_version"] != SCHEMA_VERSION:
            raise CorpusError("unsupported_corpus_version")
        if manifest["purpose"] != "open_train_dev_outside_agent_jail" or manifest["hash_scheme"] != "sha256_raw_file_bytes":
            raise CorpusError("unsupported_corpus_purpose_or_hash_scheme")
        if manifest["plan_sha256"] != _digest(corpus_specs()) or manifest["index_schema_sha256"] != _digest(corpus_index_schema()):
            raise CorpusError("frozen_corpus_contract_hash_mismatch")
        splits = manifest["splits"]
        if type(splits) is not dict or set(splits) != set(_SPLITS):
            raise CorpusError("invalid_corpus_splits")
        seen_seeds, seen_configs, episode_ids = set(), set(), set()
        for split, expected_specs in corpus_specs().items():
            if type(splits[split]) is not list or len(splits[split]) != 10:
                raise CorpusError("corpus_requires_ten_per_split")
            split_directory = directory / split
            if split_directory.is_symlink() or not split_directory.is_dir() or {path.name for path in split_directory.iterdir()} != {spec["key"] for spec in expected_specs}:
                raise CorpusError("unindexed_or_missing_episode_directory")
            for entry, spec in zip(splits[split], expected_specs, strict=True):
                expected_fields = {"key", "seed", "config", "path", "episode_id", "files", "public_sha256", "issue_counts", "table_counts"}
                if type(entry) is not dict or set(entry) != expected_fields:
                    raise CorpusError("invalid_corpus_entry_fields")
                if entry["key"] != spec["key"] or entry["seed"] != spec["seed"] or type(entry["seed"]) is not int or canonical_json(entry["config"]) != canonical_json(spec["config"]):
                    raise CorpusError("frozen_corpus_spec_mismatch")
                relative = f"{split}/{spec['key']}"
                if entry["path"] != relative:
                    raise CorpusError("unsafe_or_unexpected_episode_path")
                if type(entry["files"]) is not dict or set(entry["files"]) != set(_FILES):
                    raise CorpusError("invalid_corpus_file_index")
                if any(type(value) is not str or not re.fullmatch("[0-9a-f]{64}", value) for value in entry["files"].values()):
                    raise CorpusError("invalid_corpus_file_digest")
                if entry["files"] != _file_hashes(directory, relative):
                    raise CorpusError("corpus_file_hash_mismatch")
                bundle = _protected_load(directory, relative)
                generated = generate(spec["seed"], spec["config"])
                if bundle != generated or reconcile(bundle.public) != bundle.truth:
                    raise CorpusError("corpus_regeneration_or_reconciliation_mismatch")
                if entry["public_sha256"] != _digest(bundle.public):
                    raise CorpusError("corpus_public_digest_mismatch")
                if entry["episode_id"] != bundle.public["manifest"]["episode_id"] or entry["episode_id"] in episode_ids:
                    raise CorpusError("corpus_episode_identity_mismatch")
                episode_ids.add(entry["episode_id"])
                if canonical_json(entry["table_counts"]) != canonical_json(bundle.public["manifest"]["table_counts"]) or canonical_json(entry["issue_counts"]) != canonical_json(dict(sorted(Counter(issue["type"] for issue in bundle.truth).items()))):
                    raise CorpusError("corpus_summary_mismatch")
                config_identity = canonical_json(spec["config"])
                if spec["seed"] in seen_seeds or config_identity in seen_configs:
                    raise CorpusError("corpus_splits_not_disjoint")
                seen_seeds.add(spec["seed"])
                seen_configs.add(config_identity)
        coverage = _coverage(splits)
        if canonical_json(manifest["coverage"]) != canonical_json(coverage):
            raise CorpusError("corpus_coverage_mismatch")
        if set(coverage["issue_types"]) != {"duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"} or coverage["foreign_exponents"] != [0, 2, 3] or coverage["fx_modes"] != ["date", "rate"] or len(coverage["clean_ledger_sizes"]) < 3 or not coverage["clean_dirty_ledger_size_overlap"]:
            raise CorpusError("corpus_minimum_coverage_missing")
        return {
            "passed": True, "corpus_version": PLAN_VERSION, "episode_count": 20,
            "split_counts": {split: len(splits[split]) for split in _SPLITS},
            "plan_sha256": manifest["plan_sha256"], "index_schema_sha256": manifest["index_schema_sha256"],
            "checks": {name: True for name in (
                "frozen_index_and_hashes", "ten_train_ten_dev", "disjoint_seeds_and_configs",
                "exact_regeneration", "truth_blind_full_reconciliation", "strict_storage_roundtrip",
                "protected_copy_without_source_changes", "four_families_and_diverse_controls",
            )}, "coverage": coverage,
        }
    except CorpusError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        raise CorpusError("invalid_corpus_package") from error
