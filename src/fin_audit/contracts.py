"""Small shared contracts; no private episode state is exposed to agent workers."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = "0.2.0"
SUPPORTED_SCHEMA_VERSIONS = ("0.1.0", "0.2.0")
PROTOCOL_VERSION = "1.0"
ISSUE_TYPES = (
    "duplicate_invoice",
    "payment_allocation",
    "fx_conversion",
    "period_cutoff",
)
MAX_STEPS = 100
MAX_PAGE_SIZE = 50
MAX_FINDINGS = 30
MAX_EVIDENCE_IDS = 10
MAX_EXPLANATION_LENGTH = 1000
PUBLIC_TABLES = ("ledger", "invoices", "payments", "fx")
JsonDict = dict[str, Any]
PRIVATE_PUBLIC_KEYS = frozenset({
    "seed", "hidden_seed", "truth", "provenance", "issue_id", "evidence_sets",
    "expected_amount_minor", "is_error", "anomaly_type", "duplicate_count",
    "allocation_count", "fx_count", "cutoff_count",
    "interaction_allocation_fx", "interaction_allocation_cutoff",
})


@dataclass(frozen=True)
class EpisodeBundle:
    """Trusted generator output. Never serialize this object to an agent."""

    public: JsonDict
    truth: list[JsonDict]
    provenance: JsonDict


def validate_public_privacy(public: JsonDict) -> None:
    """Reject reserved private labels before a public bundle reaches a worker.

    This is a guard against accidental authoring leaks, not a claim that any
    arbitrary malicious text can be proven independent of all private facts.
    """
    pending: list[Any] = [public]
    seen: set[int] = set()
    while pending:
        value = pending.pop()
        if isinstance(value, (dict, list)):
            if id(value) in seen:
                continue
            seen.add(id(value))
        if isinstance(value, dict):
            if PRIVATE_PUBLIC_KEYS.intersection(value):
                raise ValueError("reserved_private_public_key")
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)


def evidence_registry(public: JsonDict) -> set[str]:
    """All public evidence IDs; business IDs are not document IDs."""
    ids = {"policy"}
    for row in public.get("ledger", []):
        ids.add(row["row_id"])
    for table in ("invoices", "payments"):
        for doc in public.get(table, []):
            ids.add(doc["doc_id"])
    for row in public.get("fx", []):
        record_id = row.get("record_id")
        if record_id is not None:
            ids.add(record_id)
    return ids


def canonical_json(value: Any) -> str:
    """Stable UTF-8-compatible JSON, rejecting non-finite numbers."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> JsonDict:
    result: JsonDict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non_finite_json_number")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non_finite_json_number")
    return value


def strict_json_loads(text: str) -> Any:
    """Parse wire JSON without duplicate keys or NaN/Infinity extensions."""
    return json.loads(
        text, object_pairs_hook=_unique_object,
        parse_constant=_reject_constant, parse_float=_finite_float,
    )
