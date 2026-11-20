"""Presentation renaming must preserve facts, evidence, mistakes and scoring."""

from __future__ import annotations

import copy
import hashlib

import pytest

from fin_audit.checker import reconcile
from fin_audit.contracts import EpisodeBundle, canonical_json, evidence_registry, validate_public_privacy
from fin_audit.evaluation import evaluate
from fin_audit.generator import generate
from fin_audit.profiles import stage2_cases
from fin_audit.storage import export_episode, load_episode
from fin_audit.transforms import TransformError, remap_findings, transform_episode

_ID_FIELDS = {"episode_id", "entity_id", "invoice_id", "payment_id", "doc_id", "row_id", "record_id", "journal_id", "original_invoice_id"}


def oracle(bundle):
    return {
        "episode_id": bundle.public["manifest"]["episode_id"],
        "findings": [{
            "type": issue["type"], "affected_ids": list(issue["affected_ids"]),
            "claimed_amount_minor": issue["expected_amount_minor"], "currency": issue["currency"],
            "evidence_ids": list(issue["evidence_sets"][0]), "explanation": "A documented, reproducible discrepancy.",
        } for issue in bundle.truth],
    }


def grade(payload, bundle, steps=7):
    result = evaluate(payload, bundle.truth, evidence_registry(bundle.public), steps, bundle.public["manifest"]["episode_id"])
    return result.tp, result.fp, result.fn, result.precision, result.recall, result.f1, result.reward, result.status


def snapshot(bundle):
    return canonical_json({"public": bundle.public, "truth": bundle.truth, "provenance": bundle.provenance})


def native(version):
    bundle = generate(71)
    public, provenance = copy.deepcopy(bundle.public), copy.deepcopy(bundle.provenance)
    public["manifest"]["schema_version"] = version
    public["policy"]["schema_version"] = version
    provenance["schema_version"] = version
    provenance["public_sha256"] = hashlib.sha256(canonical_json(public).encode()).hexdigest()
    return EpisodeBundle(public, reconcile(public), provenance)


@pytest.mark.parametrize("version", ["0.1.0", "0.2.0"])
def test_native_historical_and_current_versions_reconcile_and_score_identically(version):
    source = native(version)
    transformed = transform_episode(source, 1234)
    assert transformed.bundle.public["manifest"]["schema_version"] == version
    assert transformed.bundle.truth == reconcile(transformed.bundle.public)
    assert grade(oracle(source), source) == grade(remap_findings(oracle(source), transformed.mapping, transformed.bundle.public["manifest"]["episode_id"]), transformed.bundle)


@pytest.mark.parametrize("case", list(stage2_cases()))
def test_every_multicurrency_profile_preserves_full_partial_false_and_duplicate_answers(case):
    source = generate(1729, stage2_cases()[case])
    transformed = transform_episode(source, 991)
    new_episode = transformed.bundle.public["manifest"]["episode_id"]
    complete = oracle(source)
    partial = copy.deepcopy(complete)
    partial["findings"] = partial["findings"][:1]
    false_positive = copy.deepcopy(partial)
    false_positive["findings"].append({
        "type": "duplicate_invoice", "affected_ids": ["never-a-real-record"], "claimed_amount_minor": 1,
        "currency": "KZT", "evidence_ids": ["policy"], "explanation": "Unfounded accusation stays unfounded.",
    })
    duplicate = copy.deepcopy(complete)
    if complete["findings"]:
        duplicate["findings"].append(copy.deepcopy(complete["findings"][0]))
    for payload in (complete, partial, false_positive, duplicate):
        remapped = remap_findings(payload, transformed.mapping, new_episode)
        assert grade(payload, source) == grade(remapped, transformed.bundle)
    assert transformed.bundle.truth == reconcile(transformed.bundle.public)
    assert len(transformed.issue_mapping) == len(source.truth)
    assert set(transformed.issue_mapping) == {issue["issue_id"] for issue in source.truth}
    assert set(transformed.issue_mapping.values()) == {issue["issue_id"] for issue in transformed.bundle.truth}
    assert not set(transformed.issue_mapping).intersection(transformed.issue_mapping.values())


def test_mapping_and_shuffling_are_deterministic_disjoint_and_leave_original_untouched():
    source = generate(19, stage2_cases()["allocation_fx"])
    before = snapshot(source)
    first, second = transform_episode(source, -123), transform_episode(source, -123)
    assert first == second
    assert snapshot(source) == before
    assert first.mapping != transform_episode(source, -124).mapping
    assert len(first.mapping) == len(set(first.mapping.values()))
    assert not set(first.mapping).intersection(first.mapping.values())
    assert "policy" not in first.mapping and "policy" not in first.mapping.values()
    assert first.bundle.public["policy"] == source.public["policy"]
    assert first.bundle.public["manifest"]["episode_id"] == first.mapping[source.public["manifest"]["episode_id"]]
    assert [record["row_id"] for record in first.bundle.public["ledger"]] != [first.mapping[record["row_id"]] for record in source.public["ledger"]]
    first.bundle.public["policy"]["accounts"]["cash"] = "MUTATED_COPY"
    first.bundle.provenance["config"]["allocation_count"] = 999
    assert snapshot(source) == before
    assert second.bundle.public["policy"]["accounts"]["cash"] == "CASH"


def test_all_public_ids_and_references_change_but_financial_values_do_not():
    source = generate(19, {**stage2_cases()["all_four"], "refund_count": 2})
    result = transform_episode(source, 121)
    mapping = result.mapping
    for table, identity in (("ledger", "row_id"), ("invoices", "doc_id"), ("payments", "doc_id"), ("fx", "record_id")):
        by_id = {record[identity]: record for record in result.bundle.public[table]}
        for old in source.public[table]:
            new = by_id[mapping[old[identity]]]
            for key, value in old.items():
                if key in _ID_FIELDS:
                    assert new[key] == (None if value is None else mapping[value])
                elif key == "invoice_refs":
                    assert new[key] == (None if value is None else [mapping[item] for item in value])
                else:
                    assert new[key] == value
    assert result.bundle.public["policy"] == source.public["policy"]
    for key, value in source.public["manifest"].items():
        assert result.bundle.public["manifest"][key] == (mapping[value] if key == "episode_id" else value)


def test_ids_equal_to_currency_account_or_fx_text_do_not_rewrite_financial_meaning():
    source = generate(19, stage2_cases()["all_four"])
    public = copy.deepcopy(source.public)
    old_entity = public["invoices"][0]["entity_id"]
    for table in ("invoices", "ledger"):
        for record in public[table]:
            if record["entity_id"] == old_entity:
                record["entity_id"] = "KZT"
    public["ledger"][0]["row_id"] = "AR"
    public["fx"][0]["record_id"] = public["fx"][0]["rate"]
    source = EpisodeBundle(public, reconcile(public), {})
    result = transform_episode(source, 1)
    assert {"KZT", "AR", public["fx"][0]["rate"]} <= set(result.mapping)
    assert all(record["currency"] == "KZT" for record in result.bundle.public["ledger"])
    assert result.bundle.public["policy"]["accounts"]["receivable"] == "AR"
    assert sorted(record["rate"] for record in result.bundle.public["fx"]) == sorted(record["rate"] for record in public["fx"])


def test_human_entity_labels_change_without_changing_item_descriptions_or_policy():
    original = generate(19)
    public = copy.deepcopy(original.public)
    public["manifest"]["company_name"] = "Fictional Company"
    for record in public["invoices"]:
        record["entity_name"] = "Fictional Customer"
    source = EpisodeBundle(public, reconcile(public), {})
    result = transform_episode(source, 2)
    assert result.bundle.public["manifest"]["company_name"] != "Fictional Company"
    assert len({record["entity_name"] for record in result.bundle.public["invoices"]}) == 1
    assert result.bundle.public["invoices"][0]["entity_name"] != "Fictional Customer"
    by_doc = {record["doc_id"]: record for record in result.bundle.public["invoices"]}
    for record in source.public["invoices"]:
        assert by_doc[result.mapping[record["doc_id"]]]["line_items"] == record["line_items"]
    assert result.bundle.public["policy"] == source.public["policy"]


@pytest.mark.parametrize("field", ["affected_ids", "evidence_ids"])
def test_unknown_id_equal_to_new_valid_id_stays_false_positive(field):
    source = generate(19, stage2_cases()["allocation_fx"])
    result = transform_episode(source, 3)
    payload = oracle(source)
    original_id = payload["findings"][0][field][0]
    assert original_id != "policy"
    payload["findings"][0][field][0] = result.mapping[original_id]
    before = grade(payload, source)
    remapped = remap_findings(payload, result.mapping, result.bundle.public["manifest"]["episode_id"])
    assert remapped["findings"][0][field][0] == original_id
    assert grade(remapped, result.bundle) == before
    assert before[1] >= 1


def test_findings_remapping_does_not_guess_unknown_ids_or_repair_bad_fields():
    source = generate(19)
    result = transform_episode(source, 4)
    payload = oracle(source)
    payload["findings"][0]["affected_ids"] = ["unknown-record"]
    payload["findings"][0]["evidence_ids"] = ["policy", "unknown-document", "unknown-document"]
    payload["findings"][0]["currency"] = "WRONG"
    payload["findings"][0]["explanation"] = next(iter(result.mapping))
    before = copy.deepcopy(payload)
    remapped = remap_findings(payload, result.mapping, result.bundle.public["manifest"]["episode_id"])
    assert payload == before
    assert remapped["findings"][0] == before["findings"][0]
    assert grade(payload, source) == grade(remapped, result.bundle)
    payload["episode_id"] = "unknown-episode"
    remapped = remap_findings(payload, result.mapping, result.bundle.public["manifest"]["episode_id"])
    assert remapped["episode_id"] == "unknown-episode"
    assert grade(payload, source) == grade(remapped, result.bundle)


@pytest.mark.parametrize("invalid", [None, {}, {"findings": False}, {"episode_id": "unknown", "findings": [None]}])
def test_structurally_invalid_answers_are_not_silently_repaired(invalid):
    source = generate(19)
    result = transform_episode(source, 5)
    remapped = remap_findings(invalid, result.mapping, result.bundle.public["manifest"]["episode_id"])
    assert grade(invalid, source) == grade(remapped, result.bundle)


@pytest.mark.parametrize("mapping", [{"a": "same", "b": "same"}, {"a": "b", "b": "a"}, {"policy": "x"}, {"a": "policy"}])
def test_ambiguous_or_reserved_identifier_maps_fail_closed(mapping):
    with pytest.raises(TransformError):
        remap_findings({}, mapping, "same")


@pytest.mark.parametrize("seed", [True, 1.0, "1", None])
def test_presentation_seed_requires_a_real_integer(seed):
    with pytest.raises(TransformError, match="presentation_seed_requires_integer"):
        transform_episode(generate(19), seed)


def test_wrong_source_truth_or_receipt_is_rejected_before_presentation():
    source = generate(19)
    wrong = copy.deepcopy(source)
    wrong.truth[0]["expected_amount_minor"] += 1
    with pytest.raises(TransformError, match="source_truth_does_not_match"):
        transform_episode(wrong, 5)
    wrong = copy.deepcopy(source)
    wrong.provenance["public_sha256"] = "wrong"
    with pytest.raises(TransformError, match="source_public_digest_mismatch"):
        transform_episode(wrong, 5)


def test_private_history_and_canonical_truth_survive_repeated_transform_and_storage(tmp_path):
    source = generate(19, stage2_cases()["allocation_cutoff"])
    first = transform_episode(source, 6)
    second = transform_episode(first.bundle, 7)
    provenance = second.bundle.provenance
    assert provenance["seed"] == source.provenance["seed"]
    assert provenance["clean_public_sha256"] == source.provenance["clean_public_sha256"]
    assert provenance["clean_public_sha256_scope"] == "original_pre_presentation_generation"
    assert len(provenance["presentation_transforms"]) == 2
    assert provenance["presentation_transforms"][0] == first.bundle.provenance["presentation_transforms"][0]
    assert provenance["presentation_transforms"][1]["before_public_sha256"] == first.bundle.provenance["public_sha256"]
    assert provenance["public_sha256"] == hashlib.sha256(canonical_json(second.bundle.public).encode()).hexdigest()
    validate_public_privacy(second.bundle.public)
    assert "presentation_seed" not in canonical_json(second.bundle.public)
    assert "presentation_transforms" not in canonical_json(second.bundle.public)
    export_episode(second.bundle, tmp_path / "transformed")
    loaded = load_episode(tmp_path / "transformed")
    assert loaded == second.bundle
    assert loaded.truth == reconcile(loaded.public)
