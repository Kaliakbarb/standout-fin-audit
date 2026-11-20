"""Evidence-based matching and the published common terminal reward.

The scorer is trusted code. It accepts JSON values rather than an agent object,
does not inspect explanation wording, and does not choose monetary tolerances
based on the answer. Invalid trusted configuration raises ``ValueError``;
invalid agent submissions produce a terminal ``invalid_submit`` result.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from fractions import Fraction
from typing import Any

from .contracts import (
    ISSUE_TYPES,
    MAX_EVIDENCE_IDS,
    MAX_EXPLANATION_LENGTH,
    MAX_FINDINGS,
    MAX_STEPS,
)

_SUBMISSION_FIELDS = frozenset({"episode_id", "findings"})
_FINDING_FIELDS = frozenset(
    {
        "type",
        "affected_ids",
        "claimed_amount_minor",
        "currency",
        "evidence_ids",
        "explanation",
    }
)
_TRUTH_FIELDS = frozenset(
    {
        "issue_id",
        "type",
        "affected_ids",
        "expected_amount_minor",
        "currency",
        "evidence_sets",
    }
)


class InvalidSubmission(ValueError):
    """A bounded, public-schema error code, containing no submitted values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class EvaluationResult:
    """Private evaluator result; a closed run must not send this to the agent.

    Invalid submissions have undefined precision/recall/F1 (``None``), reward
    -1, and no matches. Their zero predictions leave all truth issues missed.
    ``matched_pairs`` contains original finding indices and private issue IDs.
    """

    tp: int
    fp: int
    fn: int
    precision: float | None
    recall: float | None
    f1: float | None
    reward: float
    status: str
    matched_pairs: tuple[tuple[int, str], ...]
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["matched_pairs"] = [list(pair) for pair in self.matched_pairs]
        return result


@dataclass(frozen=True)
class _TruthIssue:
    issue_id: str
    type: str
    affected_ids: frozenset[str]
    expected_amount_minor: int
    currency: str
    evidence_sets: tuple[frozenset[str], ...]


def _string_array(value: Any) -> bool:
    return type(value) is list and all(type(item) is str for item in value)


def validate_submission(
    findings: Any, episode_id: str
) -> list[dict[str, Any]]:
    """Validate the public submit schema and copy/normalize individual findings.

    Unknown type/currency/ID *strings* remain semantic predictions and can
    become FP. Missing fields, non-JSON types, negative monetary magnitudes,
    extra fields, and raw limits are structural submission errors. IDs are
    opaque: only set order and within-array repetition are normalized.

    This function handles a recognized submit payload, not malformed transport
    JSON, oversized frames, or stdout flooding. Those belong to the broker.
    """

    if type(episode_id) is not str or not episode_id:
        raise ValueError("invalid_episode_id")
    if type(findings) is not dict or set(findings) != _SUBMISSION_FIELDS:
        raise InvalidSubmission("invalid_submission_fields")
    if type(findings["episode_id"]) is not str:
        raise InvalidSubmission("invalid_episode_id_type")
    if findings["episode_id"] != episode_id:
        raise InvalidSubmission("episode_id_mismatch")

    raw_findings = findings["findings"]
    if type(raw_findings) is not list:
        raise InvalidSubmission("invalid_findings_array")
    if len(raw_findings) > MAX_FINDINGS:
        raise InvalidSubmission("too_many_findings")

    normalized: list[dict[str, Any]] = []
    for finding in raw_findings:
        if type(finding) is not dict or set(finding) != _FINDING_FIELDS:
            raise InvalidSubmission("invalid_finding_fields")
        if type(finding["type"]) is not str:
            raise InvalidSubmission("invalid_finding_type")
        if not _string_array(finding["affected_ids"]):
            raise InvalidSubmission("invalid_affected_ids")
        amount = finding["claimed_amount_minor"]
        if type(amount) is not int or amount < 0:
            raise InvalidSubmission("invalid_claimed_amount_minor")
        if type(finding["currency"]) is not str:
            raise InvalidSubmission("invalid_currency")
        evidence = finding["evidence_ids"]
        if not _string_array(evidence):
            raise InvalidSubmission("invalid_evidence_ids")
        if len(evidence) > MAX_EVIDENCE_IDS:
            raise InvalidSubmission("too_many_evidence_ids")
        explanation = finding["explanation"]
        if type(explanation) is not str:
            raise InvalidSubmission("invalid_explanation")
        if len(explanation) > MAX_EXPLANATION_LENGTH:
            raise InvalidSubmission("explanation_too_long")
        normalized.append(
            {
                "type": finding["type"],
                "affected_ids": sorted(set(finding["affected_ids"])),
                "claimed_amount_minor": amount,
                "currency": finding["currency"],
                "evidence_ids": sorted(set(evidence)),
                "explanation": explanation,
            }
        )
    return normalized


def _validate_truth(
    truth: Any, registry: set[str]
) -> list[_TruthIssue]:
    if type(truth) is not list:
        raise ValueError("invalid_truth_array")
    issues: list[_TruthIssue] = []
    seen_ids: set[str] = set()
    for item in truth:
        if type(item) is not dict or not _TRUTH_FIELDS.issubset(item):
            raise ValueError("invalid_truth_fields")
        issue_id = item["issue_id"]
        if type(issue_id) is not str or not issue_id or issue_id in seen_ids:
            raise ValueError("invalid_truth_issue_id")
        seen_ids.add(issue_id)
        if type(item["type"]) is not str or item["type"] not in ISSUE_TYPES:
            raise ValueError("invalid_truth_type")
        affected = item["affected_ids"]
        if not _string_array(affected) or not affected or any(not x for x in affected):
            raise ValueError("invalid_truth_affected_ids")
        amount = item["expected_amount_minor"]
        if type(amount) is not int or amount < 0:
            raise ValueError("invalid_truth_amount")
        currency = item["currency"]
        if type(currency) is not str or not currency:
            raise ValueError("invalid_truth_currency")
        raw_sets = item["evidence_sets"]
        if type(raw_sets) is not list or not raw_sets:
            raise ValueError("invalid_truth_evidence_sets")
        proof_sets: list[frozenset[str]] = []
        for proof in raw_sets:
            if (
                not _string_array(proof)
                or not proof
                or len(proof) > MAX_EVIDENCE_IDS
                or not set(proof).issubset(registry)
            ):
                raise ValueError("invalid_truth_evidence_set")
            proof_sets.append(frozenset(proof))
        issues.append(
            _TruthIssue(
                issue_id=issue_id,
                type=item["type"],
                affected_ids=frozenset(affected),
                expected_amount_minor=amount,
                currency=currency,
                evidence_sets=tuple(proof_sets),
            )
        )
    # Issue ID is private and only resolves ties, never determines correctness.
    # Generic matcher fixtures may contain ambiguous canonical keys; generated
    # episodes additionally reject indistinguishable issues in their checker.
    return sorted(issues, key=lambda item: item.issue_id)


def _maximum_matching(
    adjacency: list[list[int]], truth_count: int
) -> tuple[tuple[int, int], ...]:
    """Deterministic maximum-cardinality bipartite matching by augmenting paths.

    Iterate findings in their original order and candidate truth in sorted
    issue-ID order. Every accepted truth node has exactly one owner. A new
    finding may move a previous owner to another candidate instead of losing
    a TP as a greedy first-fit algorithm would.
    """

    owners = [-1] * truth_count

    def augment(finding_index: int, seen: set[int]) -> bool:
        for truth_index in adjacency[finding_index]:
            if truth_index in seen:
                continue
            seen.add(truth_index)
            owner = owners[truth_index]
            if owner == -1 or augment(owner, seen):
                owners[truth_index] = finding_index
                return True
        return False

    for finding_index in range(len(adjacency)):
        augment(finding_index, set())
    return tuple(
        sorted(
            (owner, truth_index)
            for truth_index, owner in enumerate(owners)
            if owner != -1
        )
    )


def evaluate(
    findings: Any,
    truth: list[dict[str, Any]],
    registry: set[str],
    inspection_steps: int,
    episode_id: str,
    tolerances: dict[str, int] | None = None,
) -> EvaluationResult:
    """Score one recognized submit, or a trusted empty result after truncation.

    ``registry``, truth, counters, and tolerances are trusted inputs, not fields
    accepted from the agent. All submitted evidence IDs must exist in this
    episode, and at least one complete admissible witness must be present.
    Extra known evidence is allowed; extra unknown evidence prevents a match.

    For step/time truncation the caller supplies an empty valid envelope and
    preserves its own truncated flag. The common formula is unchanged, so a
    truncated clean episode can have a positive reward. Closed-mode masking is
    also the caller's responsibility: this is the true private score.
    """

    if type(inspection_steps) is not int or not 0 <= inspection_steps <= MAX_STEPS:
        raise ValueError("invalid_inspection_steps")
    if type(episode_id) is not str or not episode_id:
        raise ValueError("invalid_episode_id")
    if not isinstance(registry, (set, frozenset)) or any(
        type(item) is not str or not item for item in registry
    ):
        raise ValueError("invalid_evidence_registry")
    if tolerances is None:
        configured_tolerances: dict[str, int] = {}
    elif type(tolerances) is not dict:
        raise ValueError("invalid_tolerances")
    else:
        configured_tolerances = dict(tolerances)
    if any(
        kind not in ISSUE_TYPES or type(value) is not int or value < 0
        for kind, value in configured_tolerances.items()
    ):
        raise ValueError("invalid_tolerances")
    issues = _validate_truth(truth, registry)

    try:
        normalized = validate_submission(findings, episode_id)
    except InvalidSubmission as exc:
        return EvaluationResult(
            tp=0,
            fp=0,
            fn=len(issues),
            precision=None,
            recall=None,
            f1=None,
            reward=-1.0,
            status="invalid_submit",
            matched_pairs=(),
            error=exc.code,
        )

    adjacency: list[list[int]] = []
    for finding in normalized:
        affected = frozenset(finding["affected_ids"])
        evidence = frozenset(finding["evidence_ids"])
        candidates: list[int] = []
        if evidence.issubset(registry):
            for index, issue in enumerate(issues):
                if (
                    finding["type"] == issue.type
                    and affected == issue.affected_ids
                    and True
                    and abs(
                        finding["claimed_amount_minor"] - issue.expected_amount_minor
                    )
                    <= configured_tolerances.get(issue.type, 0)
                    and any(proof.issubset(evidence) for proof in issue.evidence_sets)
                ):
                    candidates.append(index)
        adjacency.append(candidates)

    matching = _maximum_matching(adjacency, len(issues))
    tp = len(matching)
    fp = len(normalized) - tp
    fn = len(issues) - tp
    precision = Fraction(tp, tp + fp) if tp + fp else Fraction(0)
    recall = Fraction(tp, tp + fn) if tp + fn else Fraction(0)
    f1 = Fraction(2 * tp, 2 * tp + fp + fn) if issues or normalized else Fraction(1)
    reward = 2 * f1 - 1 - Fraction(inspection_steps, 500)
    reward = min(Fraction(1), max(Fraction(-1), reward))
    return EvaluationResult(
        tp=tp,
        fp=fp,
        fn=fn,
        precision=float(precision),
        recall=float(recall),
        f1=float(f1),
        reward=float(reward),
        status="scored",
        matched_pairs=tuple(
            (finding_index, issues[truth_index].issue_id)
            for finding_index, truth_index in matching
        ),
        error=None,
    )
