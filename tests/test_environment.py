from copy import deepcopy
from dataclasses import dataclass

import pytest
from hypothesis import given, strategies as st

from fin_audit.contracts import EpisodeBundle, canonical_json
from fin_audit.environment import AuditEnv, EnvironmentStateError


@dataclass
class Clock:
    now: float = 0.0

    def __call__(self):
        return self.now


def bundle(*, count=3, dirty=False, episode_id="ep_fixture"):
    ledger = [
        {
            "row_id": f"r_{index}",
            "timestamp": "2026-11-10T12:00:00+05:00",
            "account": "AR",
            "debit_minor": 10000,
            "credit_minor": 0,
            "currency": "KZT",
            "entity_id": "entity_fixture",
            "invoice_id": "i_invoice",
            "payment_id": None,
        }
        for index in range(count)
    ]
    public = {
        "manifest": {
            "schema_version": "0.1.0",
            "episode_id": episode_id,
            "base_currency": "KZT",
            "period_start": "2026-11-01T00:00:00+05:00",
            "period_end": "2026-12-01T00:00:00+05:00",
            "timezone": "Asia/Almaty",
            "ledger_rows": count,
            "document_count": 2,
            "rounding": "ROUND_HALF_EVEN",
        },
        "policy": {"amount_tolerance_minor": 0, "rounding": "ROUND_HALF_EVEN"},
        "ledger": ledger,
        "invoices": [
            {
                "doc_id": "d_invoice",
                "invoice_id": "i_invoice",
                "entity_id": "entity_fixture",
                "issued_at": "2026-11-10T12:00:00+05:00",
                "due_at": "2026-11-20T12:00:00+05:00",
                "currency": "KZT",
                "total_minor": 10000,
                "line_items": [{"description": "fictional service", "amount_minor": 10000}],
            }
        ],
        "payments": [
            {
                "doc_id": "d_payment",
                "payment_id": "p_payment",
                "timestamp": "2026-11-11T12:00:00+05:00",
                "currency": "KZT",
                "amount_minor": 10000,
                "invoice_refs": ["i_invoice"],
            }
        ],
        "fx": [],
    }
    truth = (
        [
            {
                "issue_id": "PRIVATE_ISSUE_SENTINEL",
                "type": "duplicate_invoice",
                "affected_ids": ["r_0", "r_1"],
                "expected_amount_minor": 10000,
                "currency": "KZT",
                "evidence_sets": [["d_invoice", "r_0", "r_1"]],
            }
        ]
        if dirty
        else []
    )
    return EpisodeBundle(public, truth, {"seed": 8675309, "secret": "PRIVATE_PROVENANCE"})


def action(name, **args):
    return {"action": name, "args": args}


def submission(*, correct=False, episode_id="ep_fixture"):
    findings = []
    if correct:
        findings = [
            {
                "type": "duplicate_invoice",
                "affected_ids": ["r_0", "r_1"],
                "claimed_amount_minor": 10000,
                "currency": "KZT",
                "evidence_ids": ["d_invoice", "r_0", "r_1"],
                "explanation": "One invoice was recognized twice.",
            }
        ]
    return action("submit", findings={"episode_id": episode_id, "findings": findings})


def environment(*, dirty=False, mode="train", count=3, clock=None):
    env = AuditEnv(mode=mode, clock=clock or Clock())
    env.load_bundle(bundle(count=count, dirty=dirty))
    return env


def test_initial_observation_contains_only_public_task_and_policy():
    env = AuditEnv(clock=Clock())
    observation, info = env.load_bundle(bundle(dirty=True))
    assert set(observation) == {"task", "manifest", "policy", "remaining_steps"}
    assert observation["remaining_steps"] == 100
    assert info == {}
    serialized = canonical_json(observation)
    for secret in ("PRIVATE_ISSUE_SENTINEL", "PRIVATE_PROVENANCE", "8675309", "seed"):
        assert secret not in serialized
    assert env.evaluation_result() is None


def test_observations_and_input_bundle_are_isolated_snapshots():
    source = bundle()
    env = AuditEnv(clock=Clock())
    initial, _ = env.load_bundle(source)
    initial["policy"]["rounding"] = "ATTACK"
    source.public["invoices"][0]["line_items"][0]["description"] = "ATTACK"
    first = env.step(action("get_document", doc_id="d_invoice"))[0]
    assert first["result"]["line_items"][0]["description"] == "fictional service"
    first["result"]["line_items"][0]["description"] = "MUTATED_RETURN_VALUE"
    second = env.step(action("get_document", doc_id="i_invoice"))[0]
    assert second["result"]["line_items"][0]["description"] == "fictional service"
    second["result"]["line_items"][0]["description"] = "SECOND_SNAPSHOT"
    assert first["result"]["line_items"][0]["description"] == "MUTATED_RETURN_VALUE"
    assert env.step(action("get_document", doc_id="policy"))[0]["result"]["rounding"] == "ROUND_HALF_EVEN"


def test_full_record_pagination_has_no_gaps_and_replays_stable_cursors():
    source = bundle(count=117)
    env = AuditEnv(clock=Clock())
    env.load_bundle(source)
    cursor = None
    transitions = []
    records = []
    while True:
        transition = env.step(action("list_records", table="ledger", cursor=cursor, limit=50))
        transitions.append(transition)
        page = transition[0]["result"]
        assert len(page["records"]) <= 50
        records.extend(page["records"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert records == source.public["ledger"]
    assert env.inspection_steps == 3
    env.load_bundle(source)
    cursor = None
    for expected in transitions:
        actual = env.step(action("list_records", table="ledger", cursor=cursor, limit=50))
        assert actual == expected
        cursor = actual[0]["result"]["next_cursor"]


def test_cursor_is_scoped_to_table_and_episode():
    env = environment(count=60)
    cursor = env.step(action("list_records", table="ledger", limit=1))[0]["result"]["next_cursor"]
    assert env.step(action("list_records", table="invoices", cursor=cursor))[0]["error"]["code"] == "invalid_cursor"
    env.load_bundle(bundle(count=60, episode_id="another_episode"))
    assert env.step(action("list_records", table="ledger", cursor=cursor))[0]["error"]["code"] == "invalid_cursor"


@pytest.mark.parametrize("limit", [0, -1, 51, True, 1.5, "50", None])
def test_invalid_page_limit_costs_exactly_one_step(limit):
    env = environment()
    observation, reward, terminated, truncated, info = env.step(
        action("list_records", table="ledger", limit=limit)
    )
    assert observation == {"error": {"code": "invalid_arguments"}, "remaining_steps": 99}
    assert (reward, terminated, truncated, info) == (0, False, False, {})
    assert (env.total_steps, env.inspection_steps) == (1, 1)


def test_record_document_aliases_policy_and_empty_fx():
    env = environment()
    assert env.step(action("get_record", table="ledger", id="r_0"))[0]["result"]["row_id"] == "r_0"
    for key in ("i_invoice", "d_invoice"):
        assert env.step(action("get_record", table="invoices", id=key))[0]["result"]["doc_id"] == "d_invoice"
    for key in ("p_payment", "d_payment"):
        assert env.step(action("get_document", doc_id=key))[0]["result"]["doc_id"] == "d_payment"
    assert env.step(action("get_document", doc_id="policy"))[0]["result"]["amount_tolerance_minor"] == 0
    assert env.step(action("list_records", table="fx"))[0]["result"] == {"records": [], "next_cursor": None}


@pytest.mark.parametrize(
    "tool_action",
    [
        action("shell", command="cat /private/truth.json"),
        action("get_document", doc_id="../../private/truth.json"),
        action("get_record", table="truth", id="PRIVATE_ISSUE_SENTINEL"),
        action("get_record", table="ledger", id="missing"),
        action("calculator", expression="open('/private/truth.json').read()"),
        action("calculator", expression="1/0"),
        action("reset", seed=8675309),
        {"action": "list_records", "args": {"table": "ledger", "debug": True}},
        {"action": "list_records", "args": {"table": "ledger"}, "secret": True},
        None,
        [],
        42,
    ],
)
def test_invalid_actions_return_bounded_errors_without_private_data(tool_action):
    env = environment(dirty=True)
    observation, reward, terminated, truncated, info = env.step(tool_action)
    assert set(observation) == {"error", "remaining_steps"}
    assert len(observation["error"]["code"]) < 40
    serialized = canonical_json(observation)
    assert "PRIVATE" not in serialized
    assert "truth.json" not in serialized
    assert observation["remaining_steps"] == 99
    assert reward == 0 and not terminated and not truncated and info == {}


def test_submit_on_step_100_is_valid_and_inspection_cost_excludes_submit():
    env = environment(dirty=True)
    for _ in range(99):
        assert env.step(action("calculator", expression="1+1"))[1:4] == (0, False, False)
    observation, reward, terminated, truncated, _ = env.step(submission(correct=True))
    assert (terminated, truncated) == (True, False)
    assert observation["remaining_steps"] == 0
    assert reward == pytest.approx(0.802)
    result = env.evaluation_result()
    assert (result["tp"], result["fp"], result["fn"]) == (1, 0, 0)
    assert (result["total_steps"], result["inspection_steps"]) == (100, 99)


@pytest.mark.parametrize("dirty,expected_reward", [(False, 0.8), (True, -1.0)])
def test_100th_inspection_truncates_with_empty_final_and_common_formula(dirty, expected_reward):
    env = environment(dirty=dirty)
    for _ in range(99):
        env.step(action("get_record", table="ledger", id="missing"))
    observation, reward, terminated, truncated, _ = env.step(action("calculator", expression="1+1"))
    assert (terminated, truncated) == (False, True)
    assert observation["remaining_steps"] == 0
    assert observation["final_findings"] == []
    assert reward == pytest.approx(expected_reward)
    result = env.evaluation_result()
    assert result["termination_reason"] == "step_limit"
    assert (result["total_steps"], result["inspection_steps"]) == (100, 100)
    assert result["tp"] == 0


def test_completed_outcome_is_immutable_and_reward_cannot_be_collected_twice():
    env = environment(dirty=True)
    first = env.step(submission(correct=True))
    assert first[1] == 1
    original = env.evaluation_result()
    exposed_copy = env.evaluation_result()
    exposed_copy["reward"] = 999
    exposed_copy["matched_pairs"].append([99, "fake"])
    for request in (submission(), action("calculator", expression="1+1")):
        observation, reward, terminated, truncated, _ = env.step(request)
        assert observation["error"]["code"] == "episode_finished"
        assert (reward, terminated, truncated) == (0, True, False)
        assert env.evaluation_result() == original
    assert env.total_steps == 1


def test_closed_wire_does_not_disclose_submission_correctness():
    right = environment(dirty=True, mode="closed")
    wrong = environment(dirty=True, mode="closed")
    assert right.step(submission(correct=True)) == wrong.step(submission())
    assert right.evaluation_result()["reward"] == 1
    assert wrong.evaluation_result()["reward"] == -1
    assert right.evaluation_result()["tp"] == 1
    assert wrong.evaluation_result()["tp"] == 0


@pytest.mark.parametrize("payload", [None, {}, {"episode_id": "ep_fixture", "findings": False}])
def test_malformed_submit_terminates_with_private_minus_one(payload):
    env = environment(mode="closed")
    transition = env.step(action("submit", findings=payload))
    assert transition[1:4] == (0, True, False)
    assert transition[0]["remaining_steps"] == 99
    result = env.evaluation_result()
    assert result["status"] == "invalid_submit"
    assert result["reward"] == -1
    assert (env.total_steps, env.inspection_steps) == (1, 0)


def test_deadline_reached_before_action_does_not_execute_or_consume_a_step():
    clock = Clock()
    env = environment(dirty=True, clock=clock)
    clock.now = 60
    observation, reward, terminated, truncated, _ = env.step(submission(correct=True))
    assert (reward, terminated, truncated) == (-1, False, True)
    assert observation["final_findings"] == []
    assert (env.total_steps, env.inspection_steps) == (0, 0)
    assert env.evaluation_result()["termination_reason"] == "time_limit"


@pytest.mark.parametrize("tool_action", [action("calculator", expression="1+1"), submission(correct=True)])
def test_action_or_submit_must_complete_before_deadline(tool_action):
    timestamps = iter([0, 0, 60])
    env = AuditEnv(clock=lambda: next(timestamps))
    env.load_bundle(bundle(dirty=True))
    observation, reward, terminated, truncated, _ = env.step(tool_action)
    assert (reward, terminated, truncated) == (-1, False, True)
    assert observation["final_findings"] == []
    assert env.evaluation_result()["tp"] == 0


def test_loading_a_new_episode_resets_outcome_counters_and_cursors():
    env = environment(dirty=True, count=60)
    cursor = env.step(action("list_records", table="ledger", limit=1))[0]["result"]["next_cursor"]
    env.step(submission(correct=True))
    observation, _ = env.load_bundle(bundle(episode_id="new_episode"))
    assert observation["manifest"]["episode_id"] == "new_episode"
    assert observation["remaining_steps"] == 100
    assert (env.total_steps, env.inspection_steps) == (0, 0)
    assert env.evaluation_result() is None
    assert env.step(action("list_records", table="ledger", cursor=cursor))[0]["error"]["code"] == "invalid_cursor"


def test_generator_reset_full_submit_and_replay_use_the_same_contract():
    from fin_audit.generator import generate

    config = {"invoice_count": 3, "ledger_rows": None, "duplicate_count": 1, "allocation_count": 1}
    source = generate(19, config)
    findings = [
        {
            "type": issue["type"],
            "affected_ids": issue["affected_ids"],
            "claimed_amount_minor": issue["expected_amount_minor"],
            "currency": issue["currency"],
            "evidence_ids": issue["evidence_sets"][0],
            "explanation": "Trusted test oracle; not an ordinary agent policy.",
        }
        for issue in source.truth
    ]
    requests = [
        action("list_records", table="ledger", limit=50),
        action("get_document", doc_id="policy"),
        action("submit", findings={"episode_id": source.public["manifest"]["episode_id"], "findings": findings}),
    ]
    env = AuditEnv(clock=Clock())
    initial = env.reset(19, config)
    transitions = [env.step(request) for request in requests]
    assert transitions[-1][1] == pytest.approx(0.996)
    assert (env.evaluation_result()["tp"], env.evaluation_result()["fp"], env.evaluation_result()["fn"]) == (2, 0, 0)
    assert env.reset(19, config) == initial
    assert [env.step(request) for request in requests] == transitions
    fresh, _ = env.reset(20, config)
    assert fresh["manifest"]["episode_id"] != initial[0]["manifest"]["episode_id"]
    assert fresh["remaining_steps"] == 100
    assert env.evaluation_result() is None


def test_trusted_abort_scores_empty_final_and_cannot_regrade_a_finished_episode():
    env = environment(dirty=True, mode="closed")
    env.step(action("calculator", expression="2+2"))
    observation, reward, terminated, truncated, _ = env.abort("agent_timeout")
    assert observation["final_findings"] == []
    assert (reward, terminated, truncated) == (0, False, True)
    result = env.evaluation_result()
    assert result["reward"] == -1
    assert result["termination_reason"] == "agent_timeout"
    assert (result["total_steps"], result["inspection_steps"]) == (1, 1)
    assert env.abort("agent_failed")[1:4] == (0, False, True)
    assert env.evaluation_result() == result

    successful = environment()
    successful.step(submission())
    before = successful.evaluation_result()
    assert successful.abort("protocol_error")[1:4] == (0, True, False)
    assert successful.evaluation_result() == before


def test_abort_is_not_an_agent_tool_and_trusted_time_budget_is_shared():
    clock = Clock()
    env = environment(clock=clock)
    assert env.remaining_time_seconds == 60
    clock.now = 17.5
    assert env.remaining_time_seconds == 42.5
    assert env.step(action("abort", reason="agent_timeout"))[0]["error"]["code"] == "invalid_action"
    env.abort("agent_failed")
    assert env.remaining_time_seconds == 0


def test_step_requires_successful_reset_and_failed_load_does_not_reuse_old_episode():
    env = AuditEnv(clock=Clock())
    with pytest.raises(EnvironmentStateError, match="^reset_required$"):
        env.step(action("calculator", expression="1"))
    env.load_bundle(bundle())
    env.step(submission())
    assert env.evaluation_result() is not None
    invalid = deepcopy(bundle())
    invalid.public["ledger"][0]["row_id"] = None
    with pytest.raises(ValueError, match="^invalid_record_id$"):
        env.load_bundle(invalid)
    with pytest.raises(EnvironmentStateError, match="^reset_required$"):
        env.step(submission())
    assert env.evaluation_result() is None
    assert (env.total_steps, env.inspection_steps) == (0, 0)


@given(st.integers(0, 99))
def test_every_invalid_non_submit_attempt_spends_budget(attempts):
    env = environment()
    for _ in range(attempts):
        env.step(action("unknown"))
    transition = env.step(submission())
    assert transition[1] == pytest.approx(1 - 0.002 * attempts)
    assert env.total_steps == attempts + 1
    assert env.inspection_steps == attempts


@pytest.mark.parametrize("mode", ["debug", None, False])
def test_invalid_trusted_mode_is_rejected(mode):
    with pytest.raises(ValueError, match="^invalid_mode$"):
        AuditEnv(mode=mode)


@pytest.mark.parametrize("duration", [0, -1, True, float("inf"), float("nan"), "60"])
def test_invalid_trusted_time_limit_is_rejected(duration):
    with pytest.raises(ValueError, match="^invalid_time_limit$"):
        AuditEnv(time_limit_seconds=duration)
