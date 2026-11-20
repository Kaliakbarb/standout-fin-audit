"""Frozen open corpus, trusted holdout diversity, and tamper-safe verification."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import shutil
import stat
from collections import Counter
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from fin_audit import corpus
from fin_audit.checker import reconcile
from fin_audit.contracts import canonical_json
from fin_audit.corpus import (
    CorpusError, PLAN_VERSION, corpus_index_schema, corpus_specs,
    export_corpus, generate_holdout_specs, holdout_plan, verify_corpus,
)
from fin_audit.environment import AuditEnv
from fin_audit.generator import generate
from fin_audit.storage import export_episode, load_episode

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def open_corpus(tmp_path_factory):
    parent = tmp_path_factory.mktemp("open-corpus")
    target = parent / "datasets"
    manifest = export_corpus(target)
    yield target, manifest
    shutil.rmtree(parent)


@pytest.fixture
def corpus_copy(open_corpus, tmp_path):
    target = tmp_path / "copy"
    shutil.copytree(open_corpus[0], target)
    yield target
    shutil.rmtree(target)


def _read_index(directory):
    return json.loads((directory / "manifest.json").read_text())


def _write_index(directory, index):
    (directory / "manifest.json").write_text(canonical_json(index) + "\n", encoding="utf-8")


def _structural_fingerprint(config):
    # Ignore seeds and mere size changes: novelty must also cover meaningful
    # known-task/currency/refund/interaction combinations.
    omitted = {"ledger_rows", "invoice_count", "foreign_invoice_count"}
    return canonical_json({key: value for key, value in config.items() if key not in omitted})


def test_open_plan_has_exact_disjoint_ten_train_and_ten_dev():
    specs = corpus_specs()
    assert {split: len(items) for split, items in specs.items()} == {"train": 10, "dev": 10}
    all_specs = [item for items in specs.values() for item in items]
    assert all(set(item) == {"key", "seed", "config"} for item in all_specs)
    assert len({item["seed"] for item in all_specs}) == 20
    assert len({canonical_json(item["config"]) for item in all_specs}) == 20
    specs["train"][0]["config"]["refund_count"] = 999
    assert corpus_specs()["train"][0]["config"]["refund_count"] == 1


def test_exported_manifest_contract_hashes_and_full_financial_verification(open_corpus):
    directory, manifest = open_corpus
    Draft202012Validator.check_schema(corpus_index_schema())
    Draft202012Validator(corpus_index_schema()).validate(manifest)
    report = verify_corpus(directory)
    assert report["passed"] and all(report["checks"].values())
    assert report["episode_count"] == 20
    assert report["split_counts"] == {"train": 10, "dev": 10}
    assert report["coverage"]["foreign_exponents"] == [0, 2, 3]
    assert report["coverage"]["fx_modes"] == ["date", "rate"]
    assert report["coverage"]["allocation_modes"] == ["missing", "partial"]
    assert len(report["coverage"]["clean_ledger_sizes"]) >= 3
    assert report["coverage"]["clean_dirty_ledger_size_overlap"]
    assert report["coverage"]["interaction_allocation_fx_count"] > 0
    assert report["coverage"]["interaction_allocation_cutoff_count"] > 0


def test_corpus_is_committed_source_material_not_only_runtime_artifacts():
    report = verify_corpus(ROOT / "datasets")
    assert report["passed"] and report["episode_count"] == 20


def test_structural_plan_files_are_exact_versioned_mirrors():
    open_plan = json.loads((ROOT / "configs" / "corpus-plan.json").read_text())
    assert open_plan == {"plan_version": PLAN_VERSION, "splits": corpus_specs()}
    assert json.loads((ROOT / "configs" / "holdout-plan.json").read_text()) == holdout_plan()
    assert json.loads((ROOT / "configs" / "corpus-index.schema.json").read_text()) == corpus_index_schema()
    assert "seed" not in holdout_plan()["episodes"][0]
    assert "ledger_rows" not in holdout_plan()["episodes"][0]["config"]


def test_corpus_verification_accepts_git_file_modes_without_changing_source(corpus_copy):
    for private in corpus_copy.glob("*/*/private"):
        os.chmod(private, 0o755)
        for path in private.iterdir():
            os.chmod(path, 0o644)
    before = {path.relative_to(corpus_copy): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in corpus_copy.rglob("*") if path.is_file()}
    assert verify_corpus(corpus_copy)["passed"]
    after = {path.relative_to(corpus_copy): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in corpus_copy.rglob("*") if path.is_file()}
    assert after == before


def test_corpus_export_never_overwrites_an_existing_directory(open_corpus):
    directory, _ = open_corpus
    before = (directory / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        export_corpus(directory)
    assert (directory / "manifest.json").read_bytes() == before


def test_tampered_public_bytes_are_rejected_even_before_financial_loading(corpus_copy):
    path = corpus_copy / "train" / "train-01" / "public" / "ledger.csv"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(CorpusError, match="file_hash_mismatch"):
        verify_corpus(corpus_copy)


def test_rehashed_wrong_gold_still_fails_independent_reconciliation(corpus_copy):
    index = _read_index(corpus_copy)
    entry = index["splits"]["train"][1]
    relative = entry["path"] + "/private/truth.json"
    path = corpus_copy / relative
    path.write_text("[]\n")
    entry["files"]["private/truth.json"] = hashlib.sha256(path.read_bytes()).hexdigest()
    _write_index(corpus_copy, index)
    with pytest.raises(CorpusError):
        verify_corpus(corpus_copy)


def test_manifest_cannot_replace_a_frozen_config_with_equal_looking_boolean(corpus_copy):
    index = _read_index(corpus_copy)
    index["splits"]["train"][0]["config"]["refund_count"] = True
    _write_index(corpus_copy, index)
    with pytest.raises(CorpusError, match="frozen_corpus_spec_mismatch"):
        verify_corpus(corpus_copy)


def test_manifest_paths_cannot_escape_the_corpus(corpus_copy):
    index = _read_index(corpus_copy)
    index["splits"]["train"][0]["path"] = "../../outside"
    _write_index(corpus_copy, index)
    with pytest.raises(CorpusError, match="unsafe_or_unexpected_episode_path"):
        verify_corpus(corpus_copy)


def test_source_symlink_is_not_followed_even_when_its_bytes_match(corpus_copy, tmp_path):
    path = corpus_copy / "train" / "train-01" / "public" / "manifest.json"
    outside = tmp_path / "outside.json"
    path.rename(outside)
    path.symlink_to(outside)
    with pytest.raises(CorpusError, match="symlink_forbidden"):
        verify_corpus(corpus_copy)


def test_unindexed_private_files_are_not_silently_ignored(corpus_copy):
    (corpus_copy / "train" / "train-01" / "private" / "extra.json").write_text("{}")
    with pytest.raises(CorpusError, match="unindexed_or_missing_episode_file"):
        verify_corpus(corpus_copy)


@pytest.mark.parametrize("master", [0, 19, 731])
def test_holdout_cohorts_new_structures_and_actual_128bit_seeds(master):
    specs = generate_holdout_specs(master)
    assert len(specs) == 20
    assert Counter(spec["cohort"] for spec in specs) == {"clean": 5, "interacting": 5, "other": 10}
    assert len({spec["seed"] for spec in specs}) == 20
    open_specs = [spec for split in corpus_specs().values() for spec in split]
    assert not {spec["seed"] for spec in specs} & {spec["seed"] for spec in open_specs}
    assert all(type(spec["seed"]) is int and 0 <= spec["seed"] < 2**128 for spec in specs)
    open_structures = {_structural_fingerprint(spec["config"]) for spec in open_specs}
    assert all(_structural_fingerprint(spec["config"]) not in open_structures for spec in specs)
    assert all(360 <= spec["config"]["ledger_rows"] <= 1500 for spec in specs)
    assert generate_holdout_specs(master) == specs
    assert generate_holdout_specs(master + 1) != specs


def test_holdout_seed_collision_with_open_corpus_is_resolved(monkeypatch):
    original = hmac.new
    collide = corpus_specs()["train"][0]["seed"]
    forced = False

    class ForcedDigest:
        def digest(self):
            return collide.to_bytes(16, "big") + bytes(16)

    def new_hmac(key, message, digestmod):
        nonlocal forced
        if not forced and message.startswith(b"genesis-holdout:"):
            forced = True
            return ForcedDigest()
        return original(key, message, digestmod)

    monkeypatch.setattr(hmac, "new", new_hmac)
    specs = generate_holdout_specs(718)
    assert forced and all(spec["seed"] != collide for spec in specs)
    assert len({spec["seed"] for spec in specs}) == 20


def test_all_twenty_holdouts_preflight_via_generator_checker_and_storage(tmp_path):
    # Reuse one directory at a time; secure pytest /tmp remains bounded.
    for spec in generate_holdout_specs(731):
        bundle = generate(spec["seed"], spec["config"])
        assert reconcile(bundle.public) == bundle.truth
        assert bool(bundle.truth) == (spec["cohort"] != "clean")
        if spec["cohort"] == "interacting":
            assert spec["config"]["interaction_allocation_fx"] or spec["config"]["interaction_allocation_cutoff"]
        folder = tmp_path / spec["key"]
        export_episode(bundle, folder)
        assert load_episode(folder) == bundle
        shutil.rmtree(folder)


def test_corpus_index_and_holdout_metadata_never_enter_agent_observation():
    spec = generate_holdout_specs(731)[5]
    bundle = generate(spec["seed"], spec["config"])
    observation, info = AuditEnv(mode="closed").load_bundle(bundle)
    assert info == {}

    def keys(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key
                yield from keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from keys(item)

    assert not {"cohort", "seed", "config", "private", "provenance", "truth", "splits", "plan_sha256"} & set(keys(observation))


@pytest.mark.parametrize("master", [True, False, -1, 2**256, 1.5, "731", None])
def test_invalid_master_seed_is_rejected(master):
    with pytest.raises(CorpusError):
        generate_holdout_specs(master)
