"""Presentation-only episode transforms with independently checked semantics.

This module is trusted evaluation code. Presentation seeds, ID maps, and private
generation history must never be placed in agent observations or a live viewer.
"""

from __future__ import annotations

import hashlib
import random
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .checker import reconcile
from .contracts import EpisodeBundle, PUBLIC_TABLES, canonical_json, strict_json_loads

TRANSFORM_VERSION = "1.0"
_PUBLIC_ID_FIELDS = frozenset({
    "episode_id", "entity_id", "invoice_id", "payment_id", "doc_id", "row_id",
    "record_id", "journal_id", "original_invoice_id",
})
_PRIVATE_REFERENCE_FIELDS = _PUBLIC_ID_FIELDS | {
    "unallocated_row_id", "primary_row_id",
}
_REFERENCE_LISTS = frozenset({
    "invoice_refs", "affected_ids", "evidence_ids", "evidence_sets", "added_row_ids",
})
_LABEL_FIELDS = frozenset({"company_name", "entity_name", "customer_name", "display_name"})
_TRUTH_FIELDS = frozenset({
    "issue_id", "type", "affected_ids", "expected_amount_minor", "currency", "evidence_sets",
})


class TransformError(ValueError):
    """Invalid source gold, an ambiguous mapping, or changed task semantics."""


@dataclass(frozen=True)
class TransformResult:
    bundle: EpisodeBundle
    mapping: dict[str, str]
    issue_mapping: dict[str, str] = field(default_factory=dict)


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _rng(seed: int, label: str) -> random.Random:
    digest = hashlib.sha256(f"fin-audit/presentation/{TRANSFORM_VERSION}/{seed}/{label}".encode()).digest()
    return random.Random(int.from_bytes(digest, "big"))


def _identities(value: Any, key: str | None = None) -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        for child_key, child in value.items():
            result.update(_identities(child, child_key))
    elif isinstance(value, list):
        for child in value:
            result.update(_identities(child, key))
    elif value is not None and (key in _PUBLIC_ID_FIELDS or key == "invoice_refs"):
        if type(value) is not str or not value:
            raise TransformError("invalid_public_identifier")
        if value != "policy":
            result.add(value)
    return result


def _rewrite(
    value: Any,
    mapping: dict[str, str],
    *,
    key: str | None = None,
    private: bool = False,
    issue_mapping: dict[str, str] | None = None,
) -> Any:
    if isinstance(value, dict):
        return {
            child_key: _rewrite(child, mapping, key=child_key, private=private, issue_mapping=issue_mapping)
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [_rewrite(child, mapping, key=key, private=private, issue_mapping=issue_mapping) for child in value]
    if isinstance(value, str):
        if private and key == "issue_id":
            return (issue_mapping or {}).get(value, value)
        fields = _PRIVATE_REFERENCE_FIELDS if private else _PUBLIC_ID_FIELDS
        if key in fields or key in _REFERENCE_LISTS:
            return mapping.get(value, value)
    return deepcopy(value)


def _semantic(issue: dict) -> tuple:
    if type(issue) is not dict or set(issue) != _TRUTH_FIELDS:
        raise TransformError("invalid_source_truth")
    if any(type(issue[name]) is not str or not issue[name] for name in ("issue_id", "type", "currency")):
        raise TransformError("invalid_source_truth")
    amount = issue["expected_amount_minor"]
    affected, proofs = issue["affected_ids"], issue["evidence_sets"]
    if type(amount) is not int or amount <= 0 or type(affected) is not list or not affected:
        raise TransformError("invalid_source_truth")
    if any(type(value) is not str or not value for value in affected) or len(set(affected)) != len(affected):
        raise TransformError("invalid_source_truth")
    if type(proofs) is not list or not proofs:
        raise TransformError("invalid_source_truth")
    normalized_proofs = []
    for proof in proofs:
        if type(proof) is not list or not 1 <= len(proof) <= 10:
            raise TransformError("invalid_source_truth")
        if any(type(value) is not str or not value for value in proof) or len(set(proof)) != len(proof):
            raise TransformError("invalid_source_truth")
        normalized_proofs.append(tuple(sorted(proof)))
    return issue["type"], tuple(sorted(affected)), amount, issue["currency"], tuple(sorted(normalized_proofs))


def _truth_semantics(truth: Any) -> list[tuple]:
    if type(truth) is not list:
        raise TransformError("invalid_source_truth")
    semantics = [_semantic(issue) for issue in truth]
    if len({issue["issue_id"] for issue in truth}) != len(truth):
        raise TransformError("duplicate_source_issue_identifier")
    if len(set(semantics)) != len(semantics):
        raise TransformError("duplicate_source_issue")
    return sorted(semantics)


def _new_mapping(identities: set[str], seed: int) -> dict[str, str]:
    randomizer = _rng(seed, "identifiers")
    forbidden = identities | {"policy"}
    mapping: dict[str, str] = {}
    for identity in sorted(identities):
        while True:
            candidate = f"opaque_{randomizer.getrandbits(128):032x}"
            if candidate not in forbidden:
                break
        mapping[identity] = candidate
        forbidden.add(candidate)
    return mapping


def _rename_human_labels(public: dict, seed: int) -> int:
    """Only entity/company display labels; item descriptions and policy stay put."""
    randomizer = _rng(seed, "human_labels")
    replacements: dict[tuple[str, str], str] = {}
    changes = 0
    for record in [public["manifest"], *(record for table in PUBLIC_TABLES for record in public[table])]:
        for key in sorted(_LABEL_FIELDS.intersection(record)):
            text = record[key]
            if type(text) is not str:
                continue
            category = "Company" if key == "company_name" else "Entity"
            label_key = category, text
            if label_key not in replacements:
                replacements[label_key] = f"{category} {randomizer.getrandbits(64):016x}"
            record[key] = replacements[label_key]
            changes += 1
    return changes


def transform_episode(bundle: EpisodeBundle, presentation_seed: int) -> TransformResult:
    """Rename all public IDs and shuffle tables without changing the task.

    Both original and transformed books are independently reconciled. New truth
    IDs are canonical for the transformed episode; all private history stays in
    provenance. No assertion is made about an ordinary policy's search order.
    """
    if not isinstance(bundle, EpisodeBundle):
        raise TransformError("transform_requires_episode_bundle")
    if type(presentation_seed) is not int:
        raise TransformError("presentation_seed_requires_integer")
    try:
        snapshot = strict_json_loads(canonical_json({"public": bundle.public, "truth": bundle.truth, "provenance": bundle.provenance}))
    except (TypeError, ValueError) as error:
        raise TransformError("episode_is_not_strict_json") from error
    source, original_truth, provenance = snapshot["public"], snapshot["truth"], snapshot["provenance"]
    if type(source) is not dict or set(source) != {"manifest", "policy", *PUBLIC_TABLES} or type(provenance) is not dict:
        raise TransformError("invalid_episode_fields")
    original_derived = reconcile(source)
    if _truth_semantics(original_truth) != _truth_semantics(original_derived):
        raise TransformError("source_truth_does_not_match_reconciliation")
    before_digest = _digest(source)
    if "public_sha256" in provenance and provenance["public_sha256"] != before_digest:
        raise TransformError("source_public_digest_mismatch")
    if "schema_version" in provenance and provenance["schema_version"] != source["manifest"]["schema_version"]:
        raise TransformError("source_provenance_version_mismatch")
    identities = _identities(source["manifest"])
    for table in PUBLIC_TABLES:
        identities.update(_identities(source[table]))
    mapping = _new_mapping(identities, presentation_seed)
    transformed = {"policy": deepcopy(source["policy"])}
    transformed["manifest"] = _rewrite(source["manifest"], mapping)
    order_rng = _rng(presentation_seed, "table_order")
    for table in PUBLIC_TABLES:
        transformed[table] = _rewrite(source[table], mapping)
        order_rng.shuffle(transformed[table])
    label_changes = _rename_human_labels(transformed, presentation_seed)
    derived = reconcile(transformed)
    remapped_truth = _rewrite(original_truth, mapping, private=True)
    if _truth_semantics(derived) != _truth_semantics(remapped_truth):
        raise TransformError("presentation_changed_task_semantics")
    by_semantics = {_semantic(issue): issue for issue in derived}
    issue_mapping = {
        original["issue_id"]: by_semantics[_semantic(remapped)]["issue_id"]
        for original, remapped in zip(original_truth, remapped_truth, strict=True)
    }
    previous_transforms = deepcopy(provenance.pop("presentation_transforms", []))
    if type(previous_transforms) is not list:
        raise TransformError("invalid_presentation_history")
    provenance = _rewrite(provenance, mapping, private=True, issue_mapping=issue_mapping)
    after_digest = _digest(transformed)
    provenance["public_sha256"] = after_digest
    if "clean_public_sha256" in provenance:
        provenance["clean_public_sha256_scope"] = "original_pre_presentation_generation"
    provenance["presentation_transforms"] = [*previous_transforms, {
        "version": TRANSFORM_VERSION,
        "presentation_seed": presentation_seed,
        "from_episode_id": source["manifest"]["episode_id"],
        "to_episode_id": transformed["manifest"]["episode_id"],
        "before_public_sha256": before_digest,
        "after_public_sha256": after_digest,
        "mapping": deepcopy(mapping),
        "issue_mapping": deepcopy(issue_mapping),
        "human_label_changes": label_changes,
    }]
    return TransformResult(EpisodeBundle(transformed, derived, provenance), mapping, issue_mapping)


def remap_findings(payload: Any, mapping: dict[str, str], new_episode_id: str) -> Any:
    """Remap only ID-bearing claim fields, preserving mistakes and raw limits.

    Extend each old->new pair to a swap. Thus an unknown original ID which happens
    to equal a new valid ID maps to the removed old ID and stays unknown. Other
    unknown IDs stay unchanged; no valid link is guessed. Wrong/missing episode
    IDs, explanation wording, amounts, currencies and malformed fields are not
    repaired. ``policy`` is always reserved.
    """
    if type(mapping) is not dict or any(type(key) is not str or type(value) is not str for key, value in mapping.items()):
        raise TransformError("invalid_identifier_mapping")
    if "policy" in mapping or "policy" in mapping.values():
        raise TransformError("reserved_policy_identifier")
    if len(set(mapping.values())) != len(mapping) or set(mapping).intersection(mapping.values()):
        raise TransformError("ambiguous_identifier_mapping")
    if type(new_episode_id) is not str or new_episode_id not in mapping.values():
        raise TransformError("invalid_target_episode_identifier")
    reverse = {value: key for key, value in mapping.items()}

    def identity(value: Any) -> Any:
        if type(value) is not str or value == "policy":
            return deepcopy(value)
        return mapping.get(value, reverse.get(value, value))

    result = deepcopy(payload)
    if type(result) is not dict:
        return result
    if "episode_id" in result:
        result["episode_id"] = identity(result["episode_id"])
    if type(result.get("findings")) is list:
        for finding in result["findings"]:
            if type(finding) is not dict:
                continue
            for key in ("affected_ids", "evidence_ids"):
                if type(finding.get(key)) is list:
                    finding[key] = [identity(value) for value in finding[key]]
    return result
