"""Trusted audit environment with a separate, JSON-only agent interface.

AuditEnv belongs to the trusted runner. Never pass this Python object to policy
code: it contains private truth. Agent workers receive only returned JSON values.
"""

from __future__ import annotations

import hashlib
import math
import time
from copy import deepcopy
from typing import Any, Callable

from fin_audit.calculator import CalculationError, calculate
from fin_audit.contracts import (
    ISSUE_TYPES,
    MAX_PAGE_SIZE,
    MAX_STEPS,
    PUBLIC_TABLES,
    EpisodeBundle,
    canonical_json,
    evidence_registry,
    validate_public_privacy,
)

Transition = tuple[dict[str, Any], float, bool, bool, dict[str, Any]]


class EnvironmentStateError(RuntimeError):
    """A bounded trusted lifecycle error."""


class _ToolError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class AuditEnv:
    """Read-only tools, an immutable terminal outcome, and trusted evaluation.

    ``reset(seed, config)`` is trusted-only; its observation never contains seed.
    All policies execute in a separate runner process, not in this class.
    """

    def __init__(
        self,
        *,
        mode: str = "train",
        clock: Callable[[], float] = time.monotonic,
        time_limit_seconds: float = 60.0,
    ) -> None:
        if mode not in ("train", "closed"):
            raise ValueError("invalid_mode")
        if (
            isinstance(time_limit_seconds, bool)
            or not isinstance(time_limit_seconds, (int, float))
            or not math.isfinite(time_limit_seconds)
            or time_limit_seconds <= 0
        ):
            raise ValueError("invalid_time_limit")
        self._mode = mode
        self._clock = clock
        self._time_limit = float(time_limit_seconds)
        self._clear_episode()

    def _clear_episode(self) -> None:
        self._loaded = False
        self._terminated = False
        self._truncated = False
        self._total_steps = 0
        self._inspection_steps = 0
        self._evaluation: dict[str, Any] | None = None

    def reset(
        self, seed: int, config: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Generate a fresh episode through the trusted generator facade."""
        from fin_audit.generator import generate

        self._clear_episode()
        started_at = self._clock()
        bundle = generate(seed, {} if config is None else config)
        return self.load_bundle(bundle, _started_at=started_at)

    def load_bundle(
        self, bundle: EpisodeBundle, *, _started_at: float | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Load a trusted, validated bundle; useful for hand-checked fixtures."""
        self._clear_episode()
        started_at = self._clock() if _started_at is None else _started_at
        public = deepcopy(bundle.public)
        validate_public_privacy(public)
        if not isinstance(public, dict) or set(public) != {
            "manifest", "policy", *PUBLIC_TABLES
        }:
            raise ValueError("invalid_public_bundle")
        if not isinstance(public["manifest"], dict) or not isinstance(public["policy"], dict):
            raise ValueError("invalid_public_bundle")
        episode_id = public["manifest"].get("episode_id")
        if not isinstance(episode_id, str) or not episode_id:
            raise ValueError("invalid_episode_id")
        for table in PUBLIC_TABLES:
            if not isinstance(public[table], list) or not all(
                isinstance(row, dict) for row in public[table]
            ):
                raise ValueError("invalid_public_table")
        # This also rejects non-JSON and non-finite public values before exposure.
        fingerprint = hashlib.sha256(canonical_json(public).encode()).hexdigest()
        self._public = public
        self._truth = deepcopy(bundle.truth)
        self._episode_id = episode_id
        self._registry = evidence_registry(public)
        tolerance = public["policy"].get("amount_tolerance_minor", 0)
        if type(tolerance) is not int or tolerance < 0:
            raise ValueError("invalid_tolerance")
        self._tolerances = {issue_type: tolerance for issue_type in ISSUE_TYPES}
        type_tolerances = public["policy"].get("amount_tolerances_minor", {})
        if not isinstance(type_tolerances, dict) or any(
            key not in ISSUE_TYPES or type(value) is not int or value < 0
            for key, value in type_tolerances.items()
        ):
            raise ValueError("invalid_tolerance")
        self._tolerances.update(type_tolerances)
        self._record_indexes: dict[str, dict[str, dict[str, Any]]] = {}
        self._documents: dict[str, dict[str, Any]] = {"policy": public["policy"]}
        self._cursor_positions: dict[str, tuple[str, int]] = {}
        self._position_cursors: dict[tuple[str, int], str] = {}
        for table in PUBLIC_TABLES:
            index: dict[str, dict[str, Any]] = {}
            for record in public[table]:
                if table == "ledger":
                    keys = (record.get("row_id"),)
                elif table == "invoices":
                    keys = (record.get("invoice_id"), record.get("doc_id"))
                elif table == "payments":
                    keys = (record.get("payment_id"), record.get("doc_id"))
                else:
                    keys = (record.get("record_id"),)
                for key in keys:
                    if not isinstance(key, str) or not key:
                        raise ValueError("invalid_record_id")
                    if key in index and index[key] is not record:
                        raise ValueError("duplicate_record_id")
                    index[key] = record
                    if table in ("invoices", "payments"):
                        if key in self._documents and self._documents[key] is not record:
                            raise ValueError("duplicate_document_id")
                        self._documents[key] = record
            self._record_indexes[table] = index
            for position in range(1, len(public[table])):
                token = "c_" + hashlib.sha256(
                    f"{fingerprint}:{table}:{position}".encode()
                ).hexdigest()[:32]
                self._cursor_positions[token] = (table, position)
                self._position_cursors[(table, position)] = token
        self._total_steps = 0
        self._inspection_steps = 0
        self._terminated = False
        self._truncated = False
        self._evaluation = None
        self._deadline = started_at + self._time_limit
        self._loaded = True
        return self._initial_observation(), {}

    def _initial_observation(self) -> dict[str, Any]:
        return {
            "task": (
                "Inspect the company's public records and submit supported findings "
                "under the published accounting policy."
            ),
            "manifest": deepcopy(self._public["manifest"]),
            "policy": deepcopy(self._public["policy"]),
            "remaining_steps": MAX_STEPS,
        }

    @property
    def total_steps(self) -> int:
        return self._total_steps

    @property
    def inspection_steps(self) -> int:
        return self._inspection_steps

    @property
    def remaining_time_seconds(self) -> float:
        """Trusted broker budget; never include timing diagnostics in agent info."""
        if not self._loaded or self._terminated or self._truncated:
            return 0.0
        return max(0.0, self._deadline - self._clock())

    def evaluation_result(self) -> dict[str, Any] | None:
        """Trusted-runner accessor. This result must never enter agent wire."""
        return deepcopy(self._evaluation)

    def abort(self, reason: str = "agent_failed") -> Transition:
        """Trusted broker-only cancellation; this is not an agent action."""
        if not self._loaded:
            raise EnvironmentStateError("reset_required")
        if self._terminated or self._truncated:
            return self._transition({"error": {"code": "episode_finished"}})
        if reason not in {"agent_timeout", "agent_failed", "protocol_error"}:
            raise ValueError("invalid_abort_reason")
        return self._truncate(reason)

    def _transition(self, observation: dict[str, Any], reward: float = 0.0) -> Transition:
        observation = deepcopy(observation)
        observation["remaining_steps"] = MAX_STEPS - self._total_steps
        visible_reward = reward if self._mode == "train" else 0.0
        return observation, visible_reward, self._terminated, self._truncated, {}

    def _score(self, findings: Any) -> dict[str, Any]:
        from fin_audit.evaluation import evaluate

        result = evaluate(
            findings,
            self._truth,
            self._registry,
            self._inspection_steps,
            self._episode_id,
            self._tolerances,
        )
        return result.to_dict()

    def _finish(
        self,
        evaluation: dict[str, Any],
        *,
        terminated: bool,
        reason: str,
        observation: dict[str, Any],
    ) -> Transition:
        self._terminated = terminated
        self._truncated = not terminated
        self._evaluation = deepcopy(evaluation)
        self._evaluation.update(
            termination_reason=reason,
            total_steps=self._total_steps,
            inspection_steps=self._inspection_steps,
            terminated=self._terminated,
            truncated=self._truncated,
        )
        return self._transition(observation, float(evaluation["reward"]))

    def _truncate(
        self, reason: str, observation: dict[str, Any] | None = None
    ) -> Transition:
        evaluation = self._score({"episode_id": self._episode_id, "findings": []})
        result = deepcopy(observation) if observation is not None else {}
        result["final_findings"] = []
        return self._finish(
            evaluation, terminated=False, reason=reason, observation=result
        )

    def step(self, action: Any) -> Transition:
        """Process one bounded action; all correctness feedback is private in closed."""
        if not self._loaded:
            raise EnvironmentStateError("reset_required")
        if self._terminated or self._truncated:
            # No new transition or repeat terminal reward after completion.
            return self._transition({"error": {"code": "episode_finished"}})
        if self._clock() >= self._deadline:
            return self._truncate("time_limit")

        self._total_steps += 1
        is_submit = isinstance(action, dict) and action.get("action") == "submit"
        if not is_submit:
            self._inspection_steps += 1

        if is_submit:
            payload: Any = None
            if set(action) == {"action", "args"} and isinstance(action["args"], dict):
                if set(action["args"]) == {"findings"}:
                    payload = action["args"]["findings"]
            evaluation = self._score(payload)
            if self._clock() >= self._deadline:
                return self._truncate("time_limit")
            return self._finish(
                evaluation,
                terminated=True,
                reason=(
                    "submitted" if evaluation["status"] == "scored" else "invalid_submit"
                ),
                observation={"result": {"submitted": True}},
            )

        try:
            if (
                not isinstance(action, dict)
                or set(action) != {"action", "args"}
                or not isinstance(action.get("action"), str)
                or not isinstance(action.get("args"), dict)
            ):
                raise _ToolError("invalid_action")
            observation = {"result": self._dispatch(action["action"], action["args"])}
        except _ToolError as error:
            observation = {"error": {"code": error.code}}

        if self._clock() >= self._deadline:
            return self._truncate("time_limit")
        if self._total_steps == MAX_STEPS:
            return self._truncate("step_limit", observation)
        return self._transition(observation)

    @staticmethod
    def _arguments(
        args: dict[str, Any], required: set[str], optional: set[str] | None = None
    ) -> None:
        if not required <= set(args) or set(args) - required - (optional or set()):
            raise _ToolError("invalid_arguments")

    @staticmethod
    def _id(value: Any) -> str:
        if not isinstance(value, str) or not value or len(value) > 256:
            raise _ToolError("invalid_arguments")
        return value

    def _table(self, value: Any) -> str:
        if not isinstance(value, str) or value not in PUBLIC_TABLES:
            raise _ToolError("invalid_arguments")
        return value

    def _dispatch(self, action: str, args: dict[str, Any]) -> Any:
        if action == "list_records":
            self._arguments(args, {"table"}, {"cursor", "limit"})
            table = self._table(args["table"])
            limit = args.get("limit", MAX_PAGE_SIZE)
            if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
                raise _ToolError("invalid_arguments")
            cursor = args.get("cursor")
            position = 0
            if cursor is not None:
                if not isinstance(cursor, str) or len(cursor) > 256:
                    raise _ToolError("invalid_cursor")
                found = self._cursor_positions.get(cursor)
                if found is None or found[0] != table:
                    raise _ToolError("invalid_cursor")
                position = found[1]
            rows = self._public[table][position : position + limit]
            next_position = position + len(rows)
            next_cursor = (
                self._position_cursors[(table, next_position)]
                if next_position < len(self._public[table])
                else None
            )
            return {"records": rows, "next_cursor": next_cursor}
        if action == "get_record":
            self._arguments(args, {"table", "id"})
            table = self._table(args["table"])
            record_id = self._id(args["id"])
            record = self._record_indexes[table].get(record_id)
            if record is None:
                raise _ToolError("record_not_found")
            return record
        if action == "get_document":
            self._arguments(args, {"doc_id"})
            doc_id = self._id(args["doc_id"])
            document = self._documents.get(doc_id)
            if document is None:
                raise _ToolError("document_not_found")
            return document
        if action == "calculator":
            self._arguments(args, {"expression"})
            try:
                return {"value": calculate(args["expression"])}
            except CalculationError as error:
                raise _ToolError(error.code) from None
        raise _ToolError("invalid_action")
