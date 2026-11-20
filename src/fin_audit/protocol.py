"""Frozen local 20-episode/20-pair rehearsal, never official hidden scoring.

All policy feedback is masked. Mathematical scores, private manifests, and
reviewer artifacts are published only after the complete cohort and pairs.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import math
import os
import platform
import re
import resource
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any

from . import __version__
from .checker import reconcile
from .contracts import ISSUE_TYPES, PROTOCOL_VERSION, SCHEMA_VERSION, canonical_json, evidence_registry
from .corpus import corpus_specs, generate_holdout_specs
from .environment import AuditEnv
from .evaluation import evaluate
from .generator import generate
from .runner import oracle_action, run_episode
from .storage import export_episode, load_episode
from .transforms import remap_findings, transform_episode


POLICIES = ("random", "simple", "mark_all", "oracle")
REHEARSAL_VERSION = "local-20-plus-20-v1"
_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_PRIVATE_KEYS = {"seed", "master_seed", "truth", "provenance", "issue_id", "evidence_sets", "expected_amount_minor"}
_SCORE_FIELDS = ("tp", "fp", "fn", "precision", "recall", "f1", "reward", "status", "error")


class ProtocolError(ValueError):
    """A bounded trusted orchestration failure; no result is called complete."""


def _json_file(path: Path, value: Any, *, private: bool = False) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o644)
    os.fchmod(descriptor, 0o600 if private else 0o644)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(canonical_json(value) + "\n")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_hashes() -> dict[str, str]:
    root = Path(__file__).resolve().parents[2]
    paths = [root / name for name in ("pyproject.toml", "uv.lock")]
    for folder in ("src/fin_audit", "schemas", "configs", "scripts", "docker"):
        paths.extend(path for path in (root / folder).rglob("*") if path.is_file()
                     and "__pycache__" not in path.parts and path.suffix != ".pyc")
    return {str(path.relative_to(root)): _digest(path) for path in sorted(set(paths)) if path.is_file()}


def _presentation_seed(master_seed: int, index: int) -> int:
    digest = hmac.new(master_seed.to_bytes(32, "big"),
                      f"genesis-protocol:presentation:v1:{index}".encode(), hashlib.sha256).digest()
    return int.from_bytes(digest[:16], "big")


def _validate_specs(specs: Any, opened: Any) -> None:
    if type(specs) is not list or len(specs) != 20:
        raise ProtocolError("holdout_requires_twenty_episodes")
    if type(opened) is not dict or set(opened) != {"train", "dev"}:
        raise ProtocolError("invalid_open_split_plan")
    if any(type(opened[name]) is not list or len(opened[name]) != 10 for name in opened):
        raise ProtocolError("open_split_requires_ten_train_ten_dev")
    open_specs = opened["train"] + opened["dev"]
    if any(type(item) is not dict or not {"seed", "config"} <= set(item)
           or type(item["seed"]) is not int or item["seed"] < 0 or type(item["config"]) is not dict
           for item in open_specs):
        raise ProtocolError("invalid_open_split_spec")
    open_seeds = {item["seed"] for item in open_specs}
    open_configs = {canonical_json(item["config"]) for item in open_specs}
    keys, seeds = set(), set()
    for spec in specs:
        if type(spec) is not dict or not {"key", "seed", "config", "cohort"} <= set(spec):
            raise ProtocolError("invalid_holdout_spec")
        if type(spec["key"]) is not str or not _KEY.fullmatch(spec["key"]) or spec["key"] in keys:
            raise ProtocolError("invalid_holdout_key")
        if type(spec["seed"]) is not int or not 0 <= spec["seed"] < 2**128:
            raise ProtocolError("invalid_holdout_seed")
        if spec["seed"] in seeds or spec["seed"] in open_seeds:
            raise ProtocolError("holdout_seed_overlap")
        if type(spec["config"]) is not dict or canonical_json(spec["config"]) in open_configs:
            raise ProtocolError("holdout_config_not_novel")
        if type(spec["cohort"]) is not str or spec["cohort"] not in {"clean", "interacting", "other"}:
            raise ProtocolError("invalid_holdout_cohort")
        keys.add(spec["key"])
        seeds.add(spec["seed"])
    if Counter(spec["cohort"] for spec in specs) != {"clean": 5, "interacting": 5, "other": 10}:
        raise ProtocolError("invalid_holdout_cohort_composition")


def _private_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_private_keys(child) for child in value.values()))
    if isinstance(value, list):
        return set().union(*(_private_keys(child) for child in value))
    return set()


def _payload(report: dict, episode_id: str) -> Any:
    evaluation = report["evaluation"]
    if evaluation["termination_reason"] not in {"submitted", "invalid_submit"}:
        return {"episode_id": episode_id, "findings": []}
    for row in reversed(report["trajectory"]):
        action = row["action"]
        if isinstance(action, dict) and action.get("action") == "submit":
            if set(action) == {"action", "args"} and type(action["args"]) is dict and set(action["args"]) == {"findings"}:
                return action["args"]["findings"]
            return None
    raise ProtocolError("terminal_submission_missing_from_trajectory")


def _score_vector(score: dict) -> dict:
    return {name: score[name] for name in _SCORE_FIELDS}


def _inspect_report(report: dict, bundle: Any) -> dict[str, bool]:
    evaluation = report["evaluation"]
    rows = report["trajectory"]
    if not rows or evaluation is None:
        raise ProtocolError("missing_episode_report")
    private_ids = {issue["issue_id"] for issue in bundle.truth}
    public_only = all(
        not _PRIVATE_KEYS.intersection(_private_keys(row["observation"]))
        and not any(identity in canonical_json(row["observation"]) for identity in private_ids)
        for row in rows
    )
    rescored = evaluate(_payload(report, report["episode_id"]), bundle.truth,
                         evidence_registry(bundle.public), evaluation["inspection_steps"], report["episode_id"],
                         _tolerances(bundle.public)).to_dict()
    return {
        "closed_wire_masked": report["mode"] == "closed" and all(row["reward"] == 0 and row["info"] == {} for row in rows),
        "observations_public_only": public_only,
        "trusted_score_reproduced": _score_vector(rescored) == _score_vector(evaluation),
        "terminal_outcome_recorded": bool(evaluation["terminated"]) != bool(evaluation["truncated"]),
        "worker_exited": report["agent_exitcode"] in (None, 0),
        "elapsed_time_valid": type(report["elapsed_seconds"]) in (int, float)
        and math.isfinite(report["elapsed_seconds"]) and report["elapsed_seconds"] >= 0,
    }


def _tolerances(public: dict) -> dict[str, int]:
    policy = public["policy"]
    result = {name: policy.get("amount_tolerance_minor", 0) for name in ISSUE_TYPES}
    result.update(policy.get("amount_tolerances_minor", {}))
    return result


def _has_interacting_payment(bundle: Any) -> bool:
    allocations = [issue for issue in bundle.truth if issue["type"] == "payment_allocation"]
    rows = {row["row_id"]: row for row in bundle.public["ledger"]}
    return any(
        rows[issue["affected_ids"][0]]["payment_id"] in allocation["affected_ids"]
        for allocation in allocations for issue in bundle.truth
        if issue["type"] in {"fx_conversion", "period_cutoff"}
    )


def _transition_row(episode_id: str, step: int, action: Any, transition: tuple) -> dict:
    observation, reward, terminated, truncated, info = transition
    return {"episode_id": episode_id, "step": step, "action": action, "observation": observation,
            "reward": reward, "terminated": terminated, "truncated": truncated, "info": info}


def _replay(report: dict, regenerated: Any, world_equal: bool) -> dict:
    result = {"case": report["case"], "policy": report["policy"], "world_equal": world_equal,
              "trajectory_equal": False, "private_score_equal": False, "passed": False}
    # External cancellation and wall-clock timing are not determined by actions.
    # Do not silently replace them with a fake successful replay.
    if report["evaluation"]["termination_reason"] in {"agent_timeout", "agent_failed", "protocol_error", "time_limit"}:
        return {**result, "status": "external_event_not_fixed_action_replay"}
    env = AuditEnv(mode="closed")
    initial, info = env.load_bundle(regenerated)
    episode_id = initial["manifest"]["episode_id"]
    rows = [{"episode_id": episode_id, "step": 0, "action": None, "observation": initial,
             "reward": 0.0, "terminated": False, "truncated": False, "info": info}]
    for recorded in report["trajectory"][1:]:
        # JSON null is a legitimate invalid action, with a deterministic error
        # and step cost. External cancellation is distinguished by its trusted
        # termination reason above, never by the action value alone.
        action = copy.deepcopy(recorded["action"])
        rows.append(_transition_row(episode_id, env.total_steps + 1, action, env.step(action)))
    result.update(trajectory_equal=canonical_json(rows) == canonical_json(report["trajectory"]),
                  private_score_equal=env.evaluation_result() == report["evaluation"],
                  status="fixed_actions_replayed", replayed_actions=len(rows) - 1)
    result["passed"] = result["world_equal"] and result["trajectory_equal"] and result["private_score_equal"]
    return result


def _equivalent_answers(bundle: Any) -> dict[str, dict]:
    full = oracle_action(bundle)["args"]["findings"]
    silence = {"episode_id": full["episode_id"], "findings": []}
    partial = copy.deepcopy(full)
    partial["findings"] = partial["findings"][:-1]
    repeated = copy.deepcopy(full)
    if repeated["findings"]:
        repeated["findings"].append(copy.deepcopy(repeated["findings"][0]))
    else:
        row = bundle.public["ledger"][0]
        repeated["findings"] = [{"type": "duplicate_invoice", "affected_ids": [row["row_id"]],
                                 "claimed_amount_minor": row["debit_minor"] + row["credit_minor"],
                                 "currency": bundle.public["manifest"]["base_currency"],
                                 "evidence_ids": [row["row_id"], "policy"],
                                 "explanation": "Controlled false accusation on a clean episode."}]
    return {"complete": full, "silence": silence, "partial": partial, "duplicate_or_false": repeated}


def _metrics(tp: int, fp: int, fn: int) -> dict[str, float]:
    return {"precision": tp / (tp + fp) if tp + fp else 0.0,
            "recall": tp / (tp + fn) if tp + fn else 0.0,
            "f1": 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else 1.0}


def _typed_counts(report: dict, bundle: Any, kind: str) -> tuple[int, int, int]:
    issues = {issue["issue_id"]: issue for issue in bundle.truth}
    matched = report["evaluation"]["matched_pairs"]
    tp = sum(issues[identity]["type"] == kind for _, identity in matched)
    matched_indices = {index for index, _ in matched}
    payload = _payload(report, report["episode_id"])
    predictions = payload["findings"] if report["evaluation"]["status"] == "scored" else []
    fp = sum(index not in matched_indices and item["type"] == kind for index, item in enumerate(predictions))
    fn = sum(issue["type"] == kind for issue in bundle.truth) - tp
    return tp, fp, fn


def _aggregate(reports: list[dict], bundles: dict, kind: str | None = None) -> dict:
    if not reports:
        return {"episodes": 0, "tp": 0, "fp": 0, "fn": 0, "precision": None,
                "recall": None, "f1": None, "return": None, "steps": None,
                "inspection_steps": None, "elapsed_seconds": None, "valid_submit_rate": None,
                "invalid_submit_rate": None, "truncated_rate": None, "clean_false_accusation_rate": None}
    evaluations = [report["evaluation"] for report in reports]
    counts = [(_typed_counts(report, bundles[report["case"]], kind) if kind else
               tuple(report["evaluation"][name] for name in ("tp", "fp", "fn"))) for report in reports]
    tp, fp, fn = (sum(values[index] for values in counts) for index in range(3))
    eligible = [(report, values) for report, values in zip(reports, counts, strict=True)
                if report["evaluation"]["f1"] is not None]
    macros = [(_metrics(*values) if kind else report["evaluation"]) for report, values in eligible]
    clean = [report for report in reports if report["cohort"] == "clean"]
    return {
        "episodes": len(reports), "tp": tp, "fp": fp, "fn": fn,
        **{name: mean(row[name] for row in macros) if macros else None for name in ("precision", "recall", "f1")},
        "micro": _metrics(tp, fp, fn), "metric_eligible_episodes": len(eligible),
        "return": mean(row["reward"] for row in evaluations),
        "steps": mean(row["total_steps"] for row in evaluations),
        "inspection_steps": mean(row["inspection_steps"] for row in evaluations),
        "elapsed_seconds": mean(report["elapsed_seconds"] for report in reports),
        "total_elapsed_seconds": sum(report["elapsed_seconds"] for report in reports),
        "valid_submit_rate": mean(row["status"] == "scored" and row["termination_reason"] == "submitted" for row in evaluations),
        "invalid_submit_rate": mean(row["status"] == "invalid_submit" for row in evaluations),
        "truncated_rate": mean(row["truncated"] for row in evaluations),
        "clean_false_accusation_rate": mean(
            (_typed_counts(report, bundles[report["case"]], kind)[1] if kind else report["evaluation"]["fp"]) > 0
            for report in clean) if clean else None,
    }


def _aggregates(reports: list[dict], bundles: dict) -> dict:
    by_policy = {policy: _aggregate([row for row in reports if row["policy"] == policy], bundles) for policy in POLICIES}
    by_cohort = {
        cohort: {policy: _aggregate([row for row in reports if row["policy"] == policy and row["cohort"] == cohort], bundles)
                 for policy in POLICIES} for cohort in ("clean", "interacting", "other")
    }
    by_type = {}
    for kind in ISSUE_TYPES:
        by_type[kind] = {}
        for policy in POLICIES:
            selected = [row for row in reports if row["policy"] == policy and
                        any(_typed_counts(row, bundles[row["case"]], kind))]
            group = _aggregate(selected, bundles, kind)
            clean = [row for row in reports if row["policy"] == policy and row["cohort"] == "clean"]
            group["clean_episodes"] = len(clean)
            group["clean_false_accusation_rate"] = mean(
                _typed_counts(row, bundles[row["case"]], kind)[1] > 0 for row in clean
            ) if clean else None
            by_type[kind][policy] = group
    return {"by_policy": by_policy, "by_cohort": by_cohort, "by_type": by_type,
            "metric_scope": "Macro P/R/F1 over eligible episodes; micro counts also shown. Per-type groups include truth or predictions of that type.",
            "return_scope": "Always the original whole-episode common return; no additional per-type reward is defined."}


def _rss() -> dict:
    multiplier = 1 if sys.platform == "darwin" else 1024
    return {"self_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * multiplier,
            "children_peak_rss_bytes": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * multiplier,
            "scope": "Process-lifetime high-water RSS; children is the largest reaped child, not a sum or whole-tree/container peak."}


def _failure_receipt(parent: Path, name: str, master_seed: int, profile: str, error: BaseException) -> None:
    descriptor, path = tempfile.mkstemp(prefix=f".{name}-FAILED-", suffix=".json", dir=parent)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(canonical_json({"status": "failed", "completed": False, "profile": profile,
                                     "master_seed": master_seed, "error_type": type(error).__name__,
                                     "error": str(error)[:1000], "version": __version__}) + "\n")
    os.chmod(path, 0o600)


def run_protocol(*, profile: str = "cooperative", output: Path, master_seed: int) -> dict:
    """Run the complete frozen local rehearsal and atomically publish a new leaf.

    No reports are written to the final output before all cohort runs, pairs,
    and replays finish. A protected sibling FAILED receipt records exceptions;
    no successful completion receipt is created for an interrupted batch.
    """
    if profile not in {"cooperative", "secure"}:
        raise ProtocolError("invalid_profile")
    if type(master_seed) is not int or not 0 <= master_seed < 2**256:
        raise ProtocolError("invalid_master_seed")
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("protocol_output_already_exists")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}-staging-", dir=output.parent))
    stage.chmod(0o700)
    started = time.monotonic()
    reserved_inode = None
    published = False
    try:
        source_hashes = _source_hashes()
        specs, opened = generate_holdout_specs(master_seed), corpus_specs()
        _validate_specs(specs, opened)
        bundles, manifest_episodes = {}, []
        for spec in specs:
            bundle = generate(spec["seed"], spec["config"])
            if reconcile(bundle.public) != bundle.truth:
                raise ProtocolError("independent_truth_preflight_failed")
            if spec["cohort"] == "clean" and bundle.truth:
                raise ProtocolError("clean_cohort_contains_errors")
            if spec["cohort"] == "interacting" and not _has_interacting_payment(bundle):
                raise ProtocolError("interacting_cohort_requires_shared_payment_errors")
            destination = stage / "episodes" / spec["key"]
            export_episode(bundle, destination)
            restored = load_episode(destination)
            if restored != bundle:
                raise ProtocolError("episode_roundtrip_preflight_failed")
            upper = evaluate(oracle_action(bundle)["args"]["findings"], bundle.truth,
                             evidence_registry(bundle.public), 0, bundle.public["manifest"]["episode_id"], _tolerances(bundle.public))
            if upper.reward != 1 or upper.tp != len(bundle.truth):
                raise ProtocolError("proof_preflight_failed")
            bundles[spec["key"]] = restored
            manifest_episodes.append({**copy.deepcopy(spec), "episode_id": bundle.public["manifest"]["episode_id"],
                                      "public_sha256": hashlib.sha256(canonical_json(bundle.public).encode()).hexdigest(),
                                      "truth_sha256": hashlib.sha256(canonical_json(bundle.truth).encode()).hexdigest()})
        if {issue["type"] for bundle in bundles.values() for issue in bundle.truth} != set(ISSUE_TYPES):
            raise ProtocolError("holdout_must_cover_all_known_families")

        reports, inspections = [], []
        for spec in specs:
            for policy in POLICIES:
                report = run_episode(bundles[spec["key"]], policy=policy, mode="closed", profile=profile)
                report.update(case=spec["key"], cohort=spec["cohort"])
                inspections.append(_inspect_report(report, bundles[spec["key"]]))
                reports.append(report)

        paired_reports, pair_checks, paired_manifests, direct_results = [], [], [], []
        budget = 7
        for index, spec in enumerate(specs):
            base = bundles[spec["key"]]
            presentation_seed = _presentation_seed(master_seed, index)
            transformed = transform_episode(base, presentation_seed)
            changed = transformed.bundle
            export_episode(changed, stage / "paired_episodes" / spec["key"])
            if load_episode(stage / "paired_episodes" / spec["key"]) != changed:
                raise ProtocolError("paired_roundtrip_failed")
            base_oracle = run_episode(base, policy="oracle", mode="closed", profile=profile)
            changed_oracle = run_episode(changed, policy="oracle", mode="closed", profile=profile)
            for label, report, bundle in (("base", base_oracle, base), ("transformed", changed_oracle, changed)):
                report.update(case=spec["key"], cohort=spec["cohort"], pair_role=label)
                inspections.append(_inspect_report(report, bundle))
                paired_reports.append(report)
            equivalent = []
            for name, payload in _equivalent_answers(base).items():
                mapped = remap_findings(payload, transformed.mapping, changed.public["manifest"]["episode_id"])
                first = evaluate(payload, base.truth, evidence_registry(base.public), budget,
                                 base.public["manifest"]["episode_id"], _tolerances(base.public)).to_dict()
                second = evaluate(mapped, changed.truth, evidence_registry(changed.public), budget,
                                  changed.public["manifest"]["episode_id"], _tolerances(changed.public)).to_dict()
                equal = _score_vector(first) == _score_vector(second)
                equivalent.append(equal)
                direct_results.append({"case": spec["key"], "variant": name, "inspection_steps": budget,
                                       "base": first, "transformed": second, "passed": equal})
            oracle_equal = _score_vector(base_oracle["evaluation"]) == _score_vector(changed_oracle["evaluation"])
            pair_checks.append({"case": spec["key"], "oracle_equal": oracle_equal,
                                "equivalent_cases": len(equivalent), "equivalent_answers_equal": all(equivalent),
                                "passed": oracle_equal and all(equivalent)})
            paired_manifests.append({"case": spec["key"], "presentation_seed": presentation_seed,
                                     "episode_id": changed.public["manifest"]["episode_id"],
                                     "public_sha256": hashlib.sha256(canonical_json(changed.public).encode()).hexdigest()})

        replays = []
        for spec in specs:
            regenerated = generate(spec["seed"], spec["config"])
            world_equal = regenerated == bundles[spec["key"]]
            replays.extend(_replay(report, regenerated, world_equal) for report in reports if report["case"] == spec["key"])
        reference = next(report for report in reports if report["policy"] == "simple")
        repeat = run_episode(bundles[reference["case"]], policy="simple", mode="closed", profile=profile)
        repeat.update(case=reference["case"], cohort=reference["cohort"])
        inspections.append(_inspect_report(repeat, bundles[repeat["case"]]))
        repeat_equal = canonical_json(repeat["trajectory"]) == canonical_json(reference["trajectory"]) and repeat["evaluation"] == reference["evaluation"]
        checks = {
            "cohort_twenty_with_five_clean_five_interacting": True,
            "split_seeds_and_configs_disjoint": True, "independent_preflight_roundtrips_and_proofs": True,
            "known_families_covered": True, "eighty_required_policy_runs": len(reports) == 80,
            "closed_wire_masked": all(item["closed_wire_masked"] for item in inspections),
            "observations_public_only": all(item["observations_public_only"] for item in inspections),
            "trusted_scores_reproduced": all(item["trusted_score_reproduced"] for item in inspections),
            "terminal_outcomes_recorded": all(item["terminal_outcome_recorded"] for item in inspections),
            "reference_workers_exit_cleanly": all(item["worker_exited"] for item in inspections),
            "elapsed_measurements_finite": all(item["elapsed_time_valid"] for item in inspections),
            "required_adapters_submit_validly": all(report["evaluation"]["status"] == "scored" and report["evaluation"]["termination_reason"] == "submitted" for report in reports),
            "oracle_upper_bound": all(report["evaluation"]["reward"] == 1 for report in reports if report["policy"] == "oracle"),
            "mark_all_has_bad_return": all(report["evaluation"]["reward"] == -1 for report in reports if report["policy"] == "mark_all"),
            "twenty_pair_checks": len(pair_checks) == 20 and all(item["passed"] for item in pair_checks),
            "eighty_fixed_action_replays": len(replays) == 80 and all(item["passed"] for item in replays),
            "fresh_policy_repeat_exact": repeat_equal,
            "source_snapshot_unchanged": _source_hashes() == source_hashes,
        }
        all_reports = reports + paired_reports + [repeat]
        with (stage / "trajectories.jsonl").open("x", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o644)
            for index, report in enumerate(all_reports):
                for row in report["trajectory"]:
                    handle.write(canonical_json({"run_id": f"protocol-{index:03}", "policy": report["policy"], **row}) + "\n")
        def compact(report: dict, index: int) -> dict:
            # Keep one canonical trajectory copy. Private scores join it through
            # run_id instead of duplicating every observed financial record.
            return {**{key: value for key, value in report.items() if key != "trajectory"},
                    "trajectory_run_id": f"protocol-{index:03}"}

        _json_file(stage / "evaluation.json", {
            "cohort": [compact(report, index) for index, report in enumerate(reports)],
            "paired_oracles": [compact(report, 80 + index) for index, report in enumerate(paired_reports)],
            "direct_equivalence": direct_results, "policy_repeat": compact(repeat, 120),
        }, private=True)
        counts = {"cohort_policy_runs": 80, "paired_checks": 20, "paired_oracle_runs": 40,
                  "policy_repeat_runs": 1, "total_run_episode_calls": 121,
                  "ordinary_worker_processes": 61, "trusted_oracle_executions": 60,
                  "fixed_action_replays": len(replays), "world_regenerations": 20,
                  "direct_equivalence_cases": len(direct_results), "direct_evaluations": 2 * len(direct_results)}
        summary = {
            "stage": 3, "version": __version__, "schema_version": SCHEMA_VERSION,
            "rehearsal_version": REHEARSAL_VERSION,
            "scope": "Frozen local rehearsal, not organizer hidden checks or an official score.",
            "profile": profile, "checks": checks, "passed": all(checks.values()),
            "runs": [{key: value for key, value in report.items() if key not in {"trajectory", "stderr"}} for report in reports],
            "aggregates": _aggregates(reports, bundles), "pair_checks": pair_checks,
            "replay_checks": replays, "policy_repeat_exact": repeat_equal, "execution_counts": counts,
            "elapsed_seconds": time.monotonic() - started, "rss": _rss(),
            "elapsed_scope": "Preflight, all runs/pairs/replays, and main artifact serialization; final manifest and atomic publication excluded.",
            "remaining_competition_work": ["scorer mutation report", "final release rehearsal"],
        }
        _json_file(stage / "summary.json", summary)
        manifest = {
            "version": __version__, "schema_version": SCHEMA_VERSION, "protocol_version": PROTOCOL_VERSION,
            "rehearsal_version": REHEARSAL_VERSION, "master_seed": master_seed, "profile": profile,
            "episodes": manifest_episodes, "pairs": paired_manifests, "source_sha256": source_hashes,
            "open_plan_sha256": hashlib.sha256(canonical_json(opened).encode()).hexdigest(),
            "holdout_specs_sha256": hashlib.sha256(canonical_json(specs).encode()).hexdigest(),
            "runtime": {"python": sys.version, "platform": platform.platform(), "architecture": platform.machine()},
            "elapsed_seconds": summary["elapsed_seconds"], "rss": summary["rss"], "execution_counts": counts,
        }
        _json_file(stage / "run-manifest.json", manifest, private=True)
        _json_file(stage / "COMPLETE.json", {"completed": True, "passed": summary["passed"],
                                             "rehearsal_version": REHEARSAL_VERSION,
                                             "sha256": {name: _digest(stage / name) for name in
                                                        ("summary.json", "evaluation.json", "run-manifest.json", "trajectories.jsonl")}})
        # Exclusive claim prevents replacement of user data or another run.
        # Renaming onto our own empty claim atomically publishes the full tree.
        output.mkdir(mode=0o700)
        output.chmod(0o700)
        reserved_inode = output.stat().st_ino
        os.rename(stage, output)
        published = True
        return summary
    except BaseException as error:
        if reserved_inode is not None and output.is_dir() and output.stat().st_ino == reserved_inode:
            try:
                output.rmdir()
            except OSError:
                pass
        try:
            _failure_receipt(output.parent, output.name, master_seed, profile, error)
        except OSError:
            # A full/unwritable disk must not replace the original failure.
            pass
        raise
    finally:
        if not published:
            shutil.rmtree(stage, ignore_errors=True)
