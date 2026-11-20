"""Local scorer regressions; these are not the organizer's hidden checks."""

from __future__ import annotations

import copy
import itertools
import json
from fractions import Fraction

import pytest

from fin_audit.evaluation import InvalidSubmission, evaluate, validate_submission


EPISODE_ID = "public-episode"
REGISTRY = {"ledger-a", "ledger-b", "invoice-doc", "payment-doc", "policy", "extra-doc"}


def truth_issue(**changes):
    issue = {
        "issue_id": "private-issue-a",
        "type": "duplicate_invoice",
        "affected_ids": ["ledger-a", "ledger-b"],
        "expected_amount_minor": 1_000_000,
        "currency": "KZT",
        "evidence_sets": [["ledger-a", "ledger-b", "invoice-doc", "policy"]],
    }
    issue.update(changes)
    return issue


def finding(**changes):
    result = {
        "type": "duplicate_invoice",
        "affected_ids": ["ledger-a", "ledger-b"],
        "claimed_amount_minor": 1_000_000,
        "currency": "KZT",
        "evidence_ids": ["ledger-a", "ledger-b", "invoice-doc", "policy"],
        "explanation": "One source invoice was recognized twice.",
    }
    result.update(changes)
    return result


def submission(*findings):
    return {"episode_id": EPISODE_ID, "findings": list(findings)}


def score(payload, truth=None, steps=0, **kwargs):
    if truth is None:
        truth = [truth_issue()]
    return evaluate(payload, truth, REGISTRY, steps, EPISODE_ID, **kwargs)


def allocation_case():
    issue = truth_issue(
        issue_id="private-issue-b",
        type="payment_allocation",
        affected_ids=["business-payment", "business-invoice"],
        expected_amount_minor=250_000,
        evidence_sets=[["ledger-b", "payment-doc", "policy"]],
    )
    claim = finding(
        type=issue["type"],
        affected_ids=list(issue["affected_ids"]),
        claimed_amount_minor=issue["expected_amount_minor"],
        evidence_ids=["ledger-b", "payment-doc", "policy"],
    )
    return issue, claim


@pytest.mark.parametrize("steps,expected_reward", [(0, 1.0), (30, 0.94), (80, 0.84), (99, 0.802)])
def test_correct_proven_answer_receives_published_reward(steps, expected_reward):
    result = score(submission(finding()), steps=steps)
    assert (result.tp, result.fp, result.fn) == (1, 0, 0)
    assert (result.precision, result.recall, result.f1) == (1.0, 1.0, 1.0)
    assert result.reward == expected_reward
    assert result.status == "scored"
    assert result.matched_pairs == ((0, "private-issue-a"),)
    assert result.error is None


def test_clean_silence_is_correct_despite_zero_precision_and_recall():
    result = score(submission(), truth=[], steps=30)
    assert (result.tp, result.fp, result.fn) == (0, 0, 0)
    assert (result.precision, result.recall, result.f1) == (0.0, 0.0, 1.0)
    assert result.reward == 0.94


def test_false_accusation_is_worse_than_silence_on_clean_company():
    accusation = score(submission(finding()), truth=[], steps=30)
    silence = score(submission(), truth=[], steps=30)
    assert (accusation.tp, accusation.fp, accusation.fn) == (0, 1, 0)
    assert accusation.f1 == 0.0
    assert accusation.reward == -1.0 < silence.reward


def test_missing_all_errors_has_zero_f1_and_clipped_reward():
    result = score(submission(), steps=99)
    assert (result.tp, result.fp, result.fn) == (0, 0, 1)
    assert result.f1 == 0.0
    assert result.reward == -1.0
    assert score(submission(finding()), steps=99).reward > result.reward


def test_two_real_findings_use_business_ids_separately_from_evidence_ids():
    issue, claim = allocation_case()
    assert not set(issue["affected_ids"]).intersection(REGISTRY)
    result = score(submission(finding(), claim), truth=[truth_issue(), issue], steps=30)
    assert (result.tp, result.fp, result.fn) == (2, 0, 0)
    assert result.reward == 0.94


def test_repeating_a_correct_finding_adds_fp_without_reusing_truth():
    issue, claim = allocation_case()
    result = score(
        submission(finding(), claim, finding()), truth=[truth_issue(), issue], steps=30
    )
    assert (result.tp, result.fp, result.fn) == (2, 1, 0)
    assert result.precision == pytest.approx(2 / 3)
    assert result.recall == 1.0
    assert result.f1 == 0.8
    assert result.reward == 0.54


def test_valid_shotgun_is_bad_even_if_one_claim_is_correct():
    issue, _ = allocation_case()
    claims = [finding()] + [finding(claimed_amount_minor=10 + i) for i in range(29)]
    result = score(submission(*claims), truth=[truth_issue(), issue], steps=30)
    assert result.status == "scored"
    assert (result.tp, result.fp, result.fn) == (1, 29, 1)
    assert result.f1 == 0.0625
    assert result.reward == -0.935


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "unknown_type"},
        {"type": "payment_allocation"},
        {"currency": "USD"},
        {"currency": "kzt"},
        {"claimed_amount_minor": 999_999},
        {"claimed_amount_minor": 0},
        {"affected_ids": ["ledger-a"]},
        {"affected_ids": ["ledger-a", "ledger-b", "extra-doc"]},
        {"affected_ids": ["LEDGER-A", "ledger-b"]},
        {"affected_ids": []},
        {"evidence_ids": []},
        {"evidence_ids": ["ledger-a", "ledger-b", "invoice-doc"]},
        {"evidence_ids": ["ledger-a", "ledger-b", "invoice-doc", "other-episode-policy"]},
        {"evidence_ids": ["ledger-a", "ledger-b", "invoice-doc", "policy", "guessed-id"]},
    ],
)
def test_semantically_wrong_claim_is_fp_and_missed_truth(changes):
    result = score(submission(finding(**changes)))
    assert result.status == "scored"
    assert (result.tp, result.fp, result.fn) == (0, 1, 1)
    assert result.f1 == 0.0
    assert result.reward == -1.0


def test_one_complete_alternative_proof_is_required_not_any_intersection():
    issue = truth_issue(
        evidence_sets=[
            ["ledger-a", "invoice-doc", "policy"],
            ["ledger-b", "payment-doc", "policy"],
        ]
    )
    partial = score(
        submission(finding(evidence_ids=["ledger-a", "payment-doc", "policy"])),
        truth=[issue],
    )
    alternative = score(
        submission(finding(evidence_ids=["ledger-b", "payment-doc", "policy"])),
        truth=[issue],
    )
    assert partial.tp == 0
    assert alternative.tp == 1


def test_extra_existing_evidence_and_guessed_but_complete_ids_are_allowed():
    result = score(
        submission(finding(evidence_ids=list(REGISTRY))), steps=0
    )
    assert result.tp == 1
    assert result.reward == 1.0


def test_set_normalization_does_not_change_opaque_ids_or_duplicate_findings():
    result = score(
        submission(
            finding(
                affected_ids=["ledger-b", "ledger-a", "ledger-a"],
                evidence_ids=["policy", "invoice-doc", "ledger-b", "ledger-a", "policy"],
            )
        )
    )
    assert (result.tp, result.fp, result.fn) == (1, 0, 0)


@pytest.mark.parametrize("delta,expected_tp", [(-2, 0), (-1, 1), (0, 1), (1, 1), (2, 0)])
def test_published_absolute_tolerance_has_inclusive_boundaries(delta, expected_tp):
    result = score(
        submission(finding(claimed_amount_minor=1_000_000 + delta)),
        tolerances={"duplicate_invoice": 1},
    )
    assert result.tp == expected_tp


def test_large_minor_units_are_compared_exactly_beyond_float_precision():
    amount = 10**20 + 1
    issue = truth_issue(expected_amount_minor=amount)
    near = submission(finding(claimed_amount_minor=amount - 1))
    assert score(near, truth=[issue]).tp == 0
    assert score(near, truth=[issue], tolerances={"duplicate_invoice": 1}).tp == 1


def test_explanation_wording_never_changes_a_score():
    claims = [
        finding(explanation=""),
        finding(explanation="I am absolutely certain this is fraud."),
        finding(explanation="Короткое проверяемое обоснование."),
    ]
    results = [score(submission(claim)) for claim in claims]
    assert results[0] == results[1] == results[2]


@pytest.mark.parametrize(
    "payload,error",
    [
        (None, "invalid_submission_fields"),
        ({}, "invalid_submission_fields"),
        ({"episode_id": EPISODE_ID, "findings": [], "reward": 1}, "invalid_submission_fields"),
        ({"episode_id": 7, "findings": []}, "invalid_episode_id_type"),
        ({"episode_id": "other-episode", "findings": []}, "episode_id_mismatch"),
        ({"episode_id": EPISODE_ID, "findings": {}}, "invalid_findings_array"),
        (submission(None), "invalid_finding_fields"),
        (submission({}), "invalid_finding_fields"),
        (submission(finding(type=None)), "invalid_finding_type"),
        (submission(finding(affected_ids=None)), "invalid_affected_ids"),
        (submission(finding(affected_ids=[False])), "invalid_affected_ids"),
        (submission(finding(claimed_amount_minor=True)), "invalid_claimed_amount_minor"),
        (submission(finding(claimed_amount_minor=1.0)), "invalid_claimed_amount_minor"),
        (submission(finding(claimed_amount_minor=-1)), "invalid_claimed_amount_minor"),
        (submission(finding(currency=None)), "invalid_currency"),
        (submission(finding(evidence_ids="ledger-a")), "invalid_evidence_ids"),
        (submission(finding(evidence_ids=[False])), "invalid_evidence_ids"),
        (submission(finding(explanation=None)), "invalid_explanation"),
    ],
)
def test_structural_errors_end_with_minus_one_even_on_clean_truth(payload, error):
    result = score(payload, truth=[])
    assert result.status == "invalid_submit"
    assert result.reward == -1.0
    assert result.precision is result.recall is result.f1 is None
    assert result.error == error
    assert result.matched_pairs == ()
    with pytest.raises(InvalidSubmission, match=error):
        validate_submission(payload, EPISODE_ID)


def test_extra_finding_fields_and_missing_required_fields_are_invalid():
    extra = finding()
    extra["true_positive"] = True
    missing = finding()
    del missing["currency"]
    for claim in (extra, missing):
        assert score(submission(claim)).error == "invalid_finding_fields"


def test_raw_limits_are_checked_before_deduplicating_ids_or_findings():
    assert score(submission(*[finding()] * 30)).status == "scored"
    assert score(submission(*[finding()] * 31)).error == "too_many_findings"
    claim = finding(evidence_ids=["policy"] * 10)
    assert score(submission(claim)).status == "scored"
    claim["evidence_ids"].append("policy")
    assert score(submission(claim)).error == "too_many_evidence_ids"


def test_explanation_limit_counts_characters_instead_of_utf8_bytes():
    assert score(submission(finding(explanation="я" * 1000))).tp == 1
    assert score(submission(finding(explanation="я" * 1001))).error == "explanation_too_long"


def test_clean_truncation_empty_answer_keeps_common_formula():
    # Environment owns truncated=true; the scorer must not invent an override.
    result = score(submission(), truth=[], steps=100)
    assert result.status == "scored"
    assert result.f1 == 1.0
    assert result.reward == 0.8


@pytest.mark.parametrize("steps", [-1, 101, True, 1.0])
def test_invalid_trusted_counter_is_not_blame_assigned_to_agent(steps):
    with pytest.raises(ValueError, match="invalid_inspection_steps"):
        score(submission(), steps=steps)


@pytest.mark.parametrize(
    "tolerances",
    [{"duplicate_invoice": -1}, {"duplicate_invoice": True}, {"unknown_type": 1}, []],
)
def test_invalid_trusted_tolerance_configuration_raises(tolerances):
    with pytest.raises(ValueError, match="invalid_tolerances"):
        score(submission(finding()), tolerances=tolerances)


@pytest.mark.parametrize(
    "truth,error",
    [
        (None, "invalid_truth_array"),
        ([{}], "invalid_truth_fields"),
        ([truth_issue(), truth_issue()], "invalid_truth_issue_id"),
        ([truth_issue(type="unpublished_type")], "invalid_truth_type"),
        ([truth_issue(expected_amount_minor=True)], "invalid_truth_amount"),
        ([truth_issue(affected_ids=[])], "invalid_truth_affected_ids"),
        ([truth_issue(evidence_sets=[])], "invalid_truth_evidence_sets"),
        ([truth_issue(evidence_sets=[[]])], "invalid_truth_evidence_set"),
        ([truth_issue(evidence_sets=[["private-provenance"]])], "invalid_truth_evidence_set"),
    ],
)
def test_broken_trusted_truth_is_not_scored_as_an_agent_mistake(truth, error):
    with pytest.raises(ValueError, match=error):
        evaluate(submission(finding()), truth, REGISTRY, 0, EPISODE_ID)


def graph_case(adjacency):
    """Generic matcher fixture, not a valid generated accounting episode.

    Each truth node has its own one-ID witness; a finding's evidence encodes
    which truth nodes it can match. Generated worlds additionally enforce
    unique canonical issue keys, unlike this intentionally ambiguous fixture.
    """
    count = max((max(row, default=-1) for row in adjacency), default=-1) + 1
    truth = [
        truth_issue(
            issue_id=f"issue-{i}",
            evidence_sets=[[f"proof-{i}"]],
        )
        for i in range(count)
    ]
    claims = [finding(evidence_ids=[f"proof-{i}" for i in row]) for row in adjacency]
    registry = {f"proof-{i}" for i in range(count)}
    return submission(*claims), truth, registry


def test_augmenting_path_avoids_greedy_first_fit_loss():
    payload, truth, registry = graph_case([[0, 1], [0]])
    result = evaluate(payload, truth, registry, 0, EPISODE_ID)
    assert result.tp == 2
    assert result.matched_pairs == ((0, "issue-1"), (1, "issue-0"))


def brute_force_matching_size(adjacency):
    def search(index, used):
        if index == len(adjacency):
            return 0
        options = [search(index + 1, used)]
        for candidate in adjacency[index]:
            if candidate not in used:
                options.append(1 + search(index + 1, used | {candidate}))
        return max(options)

    return search(0, set())


def test_matching_agrees_with_independent_exhaustive_search_on_small_graphs():
    # All 512 three-by-three graphs, including empty and many-to-one edges.
    for bits in itertools.product((False, True), repeat=9):
        adjacency = [
            [j for j in range(3) if bits[3 * i + j]]
            for i in range(3)
        ]
        payload, truth, registry = graph_case(adjacency)
        result = evaluate(payload, truth, registry, 0, EPISODE_ID)
        assert result.tp == brute_force_matching_size(adjacency), adjacency
        assert len({pair[0] for pair in result.matched_pairs}) == result.tp
        assert len({pair[1] for pair in result.matched_pairs}) == result.tp


def test_permutations_and_consistent_id_renaming_preserve_score():
    issue, claim = allocation_case()
    truth = [truth_issue(), issue]
    claims = [finding(), claim, finding(claimed_amount_minor=9)]
    reference = score(submission(*claims), truth=truth, steps=23)
    for ordered_truth in itertools.permutations(truth):
        for ordered_claims in itertools.permutations(claims):
            result = score(submission(*ordered_claims), truth=list(ordered_truth), steps=23)
            assert (result.tp, result.fp, result.fn, result.f1, result.reward) == (
                reference.tp, reference.fp, reference.fn, reference.f1, reference.reward
            )

    def rename(value):
        if type(value) is str:
            return "renamed:" + value
        if type(value) is list:
            return [rename(item) for item in value]
        return value

    renamed_truth = copy.deepcopy(truth)
    for item in renamed_truth:
        item["issue_id"] = rename(item["issue_id"])
        item["affected_ids"] = rename(item["affected_ids"])
        item["evidence_sets"] = rename(item["evidence_sets"])
    renamed_claims = copy.deepcopy(claims)
    for item in renamed_claims:
        item["affected_ids"] = rename(item["affected_ids"])
        item["evidence_ids"] = rename(item["evidence_ids"])
    renamed = evaluate(
        submission(*renamed_claims), renamed_truth, {rename(x) for x in REGISTRY}, 23, EPISODE_ID
    )
    assert (renamed.tp, renamed.fp, renamed.fn, renamed.f1, renamed.reward) == (
        reference.tp, reference.fp, reference.fn, reference.f1, reference.reward
    )


def test_evaluation_does_not_mutate_inputs_or_expose_mutable_result_state():
    payload = submission(finding())
    truth = [truth_issue()]
    before = copy.deepcopy((payload, truth, REGISTRY))
    result = score(payload, truth=truth)
    assert (payload, truth, REGISTRY) == before
    rendered = result.to_dict()
    assert json.loads(json.dumps(rendered, allow_nan=False)) == rendered
    rendered["matched_pairs"].clear()
    assert result.matched_pairs == ((0, "private-issue-a"),)
    assert score(payload, truth=truth) == result


def test_exact_fraction_reward_is_not_computed_from_rounded_display_f1():
    issue, _ = allocation_case()
    result = score(submission(finding()), truth=[truth_issue(), issue], steps=1)
    assert result.f1 == float(Fraction(2, 3))
    assert result.reward == float(2 * Fraction(2, 3) - 1 - Fraction(1, 500))
