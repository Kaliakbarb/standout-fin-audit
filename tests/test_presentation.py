"""Optional synthetic labels leave frozen books and financial evidence intact."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from fin_audit.checker import reconcile
from fin_audit.contracts import EpisodeBundle, canonical_json, validate_public_privacy
from fin_audit.corpus import corpus_specs
from fin_audit.environment import AuditEnv
from fin_audit.generator import generate
from fin_audit.presentation import PRESENTATION_VERSION, PresentationError, generate_named, with_names
from fin_audit.profiles import stage2_cases
from fin_audit.storage import export_episode, load_episode
from fin_audit.transforms import transform_episode

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schemas/episode.schema.json").read_text())


def strip_names(public):
    public = copy.deepcopy(public)
    public["manifest"].pop("company_name", None)
    public["manifest"].pop("presentation_version", None)
    for table in ("ledger", "invoices"):
        for row in public[table]:
            row.pop("entity_name", None)
    return public


def snapshot(bundle):
    return canonical_json({"public": bundle.public, "truth": bundle.truth, "provenance": bundle.provenance})


@pytest.mark.parametrize("config", [None, *stage2_cases().values()])
def test_names_are_schema_valid_consistent_and_only_add_display_fields(config):
    original = generate(1729, config)
    before = snapshot(original)
    named = with_names(original, 811)
    assert snapshot(original) == before
    assert strip_names(named.public) == original.public
    assert named.truth == original.truth == reconcile(named.public)
    assert named.public["manifest"]["presentation_version"] == PRESENTATION_VERSION
    assert named.public["manifest"]["company_name"].startswith("Вымышленная компания «")
    by_entity = {}
    for table in ("invoices", "ledger"):
        for record in named.public[table]:
            assert record["entity_name"].startswith("Вымышленный контрагент «")
            assert by_entity.setdefault(record["entity_id"], record["entity_name"]) == record["entity_name"]
    assert len(set(by_entity.values())) == len(by_entity)
    assert named.provenance["public_sha256"] == hashlib.sha256(canonical_json(named.public).encode()).hexdigest()
    assert named.provenance["clean_public_sha256"] == original.provenance["clean_public_sha256"]
    assert named.provenance["clean_public_sha256_scope"] == "original_pre_names_generation"
    assert named.provenance["presentation_name_history"][-1]["presentation_seed"] == 811
    Draft202012Validator(SCHEMA).validate(named.public)
    validate_public_privacy(named.public)


def test_name_generation_is_deterministic_and_independent_of_error_counts():
    clean = {**stage2_cases()["clean_multicurrency"], "ledger_rows": 250}
    dirty = {**clean, "duplicate_count": 1, "allocation_count": 1, "fx_count": 1, "cutoff_count": 1}
    first = generate_named(55, clean, presentation_seed=19)
    assert first == generate_named(55, clean, presentation_seed=19)
    second = generate_named(55, dirty, presentation_seed=19)
    assert first.public["manifest"]["company_name"] == second.public["manifest"]["company_name"]
    names = lambda bundle: {row["entity_id"]: row["entity_name"] for row in bundle.public["invoices"]}
    assert names(first) == names(second)
    different = generate_named(55, clean, presentation_seed=20)
    assert different.public["manifest"]["company_name"] != first.public["manifest"]["company_name"]
    assert names(different) != names(first)
    assert strip_names(different.public) == strip_names(first.public)


def test_named_refunds_keep_original_names_and_storage_and_transform_are_lossless(tmp_path):
    named = generate_named(19, stage2_cases()["all_four"], presentation_seed=-10)
    invoices = {row["invoice_id"]: row for row in named.public["invoices"]}
    creditnotes = [row for row in invoices.values() if row["total_minor"] < 0]
    assert creditnotes
    for note in creditnotes:
        assert note["entity_name"] == invoices[note["original_invoice_id"]]["entity_name"]
    export_episode(named, tmp_path / "named")
    loaded = load_episode(tmp_path / "named")
    assert loaded == named
    transformed = transform_episode(loaded, 331)
    assert transformed.bundle.truth == reconcile(transformed.bundle.public)
    assert transformed.bundle.public["manifest"]["company_name"] != named.public["manifest"]["company_name"]
    Draft202012Validator(SCHEMA).validate(transformed.bundle.public)
    export_episode(transformed.bundle, tmp_path / "transformed")
    assert load_episode(tmp_path / "transformed") == transformed.bundle


def test_names_never_publish_private_seed_or_history_in_environment_observations():
    named = generate_named(19, stage2_cases()["all_four"], presentation_seed=174762553770321)
    env = AuditEnv(mode="closed")
    observation, info = env.load_bundle(named)
    public_wire = canonical_json({"observation": observation, "info": info})
    assert "174762553770321" not in public_wire
    for private_key in ("provenance", "presentation_name_history", "presentation_seed", "truth", "evidence_sets"):
        assert private_key not in public_wire
    assert observation["manifest"]["company_name"] == named.public["manifest"]["company_name"]


def test_open_corpus_default_generation_and_committed_fixture_hashes_are_untouched():
    spec = corpus_specs()["train"][0]
    ordinary = generate(spec["seed"], spec["config"])
    with_names(ordinary, 92)
    assert "presentation_version" not in ordinary.public["manifest"]
    assert "company_name" not in ordinary.public["manifest"]
    assert all("entity_name" not in row for row in ordinary.public["invoices"])
    assert ordinary == generate(spec["seed"], spec["config"])


@pytest.mark.parametrize("bad", [True, None, 1.0, "1", [], {}])
def test_presentation_seed_is_strict_integer(bad):
    with pytest.raises(PresentationError, match="presentation_seed_requires_integer"):
        generate_named(19, presentation_seed=bad)


@pytest.mark.parametrize("field,value", [("company_name", None), ("company_name", 99), ("company_name", ""), ("company_name", "A\nB"), ("company_name", "A" * 161), ("presentation_version", "names-unknown")])
def test_wrapper_and_schema_reject_invalid_existing_display_fields(field, value):
    source = generate(19)
    source.public["manifest"][field] = value
    assert not Draft202012Validator(SCHEMA).is_valid(source.public)
    with pytest.raises(PresentationError):
        with_names(source, 9)


def test_wrapper_rejects_corrupt_truth_and_hash_and_supports_sequential_names():
    source = generate(19)
    corrupt = EpisodeBundle(copy.deepcopy(source.public), [], copy.deepcopy(source.provenance))
    with pytest.raises(PresentationError, match="source_truth"):
        with_names(corrupt, 1)
    corrupt = copy.deepcopy(source)
    corrupt.provenance["public_sha256"] = "0" * 64
    with pytest.raises(PresentationError, match="source_public_digest"):
        with_names(corrupt, 1)
    first = with_names(source, 1)
    second = with_names(first, 2)
    assert strip_names(second.public) == source.public
    assert second.truth == first.truth
    assert len(second.provenance["presentation_name_history"]) == 2


@pytest.mark.parametrize("bad", [False, 1, [], None, "", "bad\x00name"])
def test_entity_names_are_strict_display_strings(bad):
    source = generate(19)
    source.public["invoices"][0]["entity_name"] = bad
    assert not Draft202012Validator(SCHEMA).is_valid(source.public)
    with pytest.raises(PresentationError, match="invalid_existing_entity_name"):
        with_names(source, 1)
