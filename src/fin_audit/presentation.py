"""Versioned synthetic names over immutable financial books and frozen corpora.

The opt-in wrapper changes display labels only. It does not change the existing
generator defaults, money, references, policy, gold, or corpus0.3 source bytes.
"""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Any

from .checker import reconcile
from .contracts import EpisodeBundle, canonical_json, strict_json_loads, validate_public_privacy
from .generator import generate

PRESENTATION_VERSION = "names-1.0"
_WORDS = ("Север", "Маяк", "Лист", "Ритм", "Берег", "Сфера", "Вектор", "Исток")
_LABEL = re.compile(r"[^\x00-\x1f\x7f]{1,160}\Z")


class PresentationError(ValueError):
    """Invalid version, label, source truth, or a changed financial meaning."""


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _name(kind: str, identity: str, seed: int) -> str:
    digest = hashlib.sha256(f"fin-audit:{PRESENTATION_VERSION}:{kind}:{seed}:{identity}".encode()).digest()
    title = "Вымышленная компания" if kind == "company" else "Вымышленный контрагент"
    return f"{title} «{_WORDS[digest[0] % len(_WORDS)]} {_WORDS[digest[1] % len(_WORDS)]} {digest[2:8].hex()}»"


def _valid_label(value: Any) -> bool:
    return type(value) is str and _LABEL.fullmatch(value) is not None


def with_names(bundle: EpisodeBundle, presentation_seed: int) -> EpisodeBundle:
    """Attach deterministic fictional company/entity names without changing gold.

    Human names depend on a separate presentation seed and identity, never error
    counts or findings. An entity's name is consistent across its documents and
    postings, including native refunds and foreign receipts.
    """
    if not isinstance(bundle, EpisodeBundle):
        raise PresentationError("names_require_episode_bundle")
    if type(presentation_seed) is not int:
        raise PresentationError("presentation_seed_requires_integer")
    try:
        snapshot = strict_json_loads(canonical_json({"public": bundle.public, "truth": bundle.truth, "provenance": bundle.provenance}))
    except (TypeError, ValueError) as error:
        raise PresentationError("names_source_is_not_strict_json") from error
    source, truth, provenance = snapshot["public"], snapshot["truth"], snapshot["provenance"]
    validate_public_privacy(source)
    if type(provenance) is not dict or type(source) is not dict:
        raise PresentationError("invalid_names_source")
    manifest = source["manifest"]
    if "presentation_version" in manifest and manifest["presentation_version"] != PRESENTATION_VERSION:
        raise PresentationError("unsupported_presentation_version")
    if "company_name" in manifest and not _valid_label(manifest["company_name"]):
        raise PresentationError("invalid_existing_company_name")
    for table in ("ledger", "invoices"):
        for row in source[table]:
            if "entity_name" in row and not _valid_label(row["entity_name"]):
                raise PresentationError("invalid_existing_entity_name")
    if reconcile(source) != truth:
        raise PresentationError("source_truth_does_not_match_reconciliation")
    before = _digest(source)
    if "public_sha256" in provenance and provenance["public_sha256"] != before:
        raise PresentationError("source_public_digest_mismatch")
    public = copy.deepcopy(source)
    public["manifest"].update(
        presentation_version=PRESENTATION_VERSION,
        company_name=_name("company", public["manifest"]["episode_id"], presentation_seed),
    )
    for table in ("ledger", "invoices"):
        for row in public[table]:
            row["entity_name"] = _name("entity", row["entity_id"], presentation_seed)
    # Adding labels must not move a single financial fact or change matching.
    derived = reconcile(public)
    if derived != truth:
        raise PresentationError("names_changed_financial_semantics")
    validate_public_privacy(public)
    after = _digest(public)
    history = provenance.setdefault("presentation_name_history", [])
    if type(history) is not list:
        raise PresentationError("invalid_name_history")
    history.append({"version": PRESENTATION_VERSION, "presentation_seed": presentation_seed,
                    "before_public_sha256": before, "after_public_sha256": after,
                    "scope": "synthetic_display_labels_only"})
    provenance["public_sha256"] = after
    if "clean_public_sha256" in provenance:
        provenance["clean_public_sha256_scope"] = "original_pre_names_generation"
    return EpisodeBundle(public=public, truth=derived, provenance=provenance)


def generate_named(
    seed: int, config: dict[str, Any] | None = None, *, presentation_seed: int
) -> EpisodeBundle:
    """Opt-in named generation; the frozen core generator remains unchanged."""
    return with_names(generate(seed, config), presentation_seed)
