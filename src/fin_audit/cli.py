"""Local demonstrations, generation and executable milestone acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import __version__
from .checker import reconcile
from .contracts import SCHEMA_VERSION, canonical_json
from .environment import AuditEnv
from .generator import generate
from .profiles import stage2_cases
from .runner import run_episode


def _output_directory(requested: str | None) -> Path:
    if requested is not None:
        directory = Path(requested).absolute()
        directory.mkdir(parents=True, exist_ok=False)
        return directory
    parent = Path("artifacts").resolve()
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="run-", dir=parent))


def _write_json(path: Path, payload: Any, *, private: bool = False) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n")
    if private:
        path.chmod(0o600)


def _has_private_keys(value: Any) -> bool:
    forbidden = {"seed", "truth", "provenance", "issue_id", "evidence_sets"}
    if isinstance(value, dict):
        return bool(forbidden & value.keys()) or any(_has_private_keys(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_private_keys(item) for item in value)
    return False


def execute_batch(*, profile: str, output: Path, seed: int, verify: bool) -> dict[str, Any]:
    """Preserved native stage-one regression batch."""
    from .storage import export_episode

    cases = {
        "clean": generate(seed, {"duplicate_count": 0, "allocation_count": 0}),
        "issues": generate(seed, {}),
    }
    (output / "episodes").mkdir()
    for name, bundle in cases.items():
        export_episode(bundle, output / "episodes" / name)
    policies = ("simple", "silent", "mark_all", "random", "oracle")
    reports = []
    for case, bundle in cases.items():
        for policy in policies:
            report = run_episode(bundle, policy=policy, profile=profile, mode="closed")
            report["case"] = case
            reports.append(report)
    by_case = {(report["case"], report["policy"]): report for report in reports}
    clean_simple = by_case["clean", "simple"]
    dirty_simple = by_case["issues", "simple"]
    replay = run_episode(cases["issues"], policy="simple", profile=profile, mode="closed")
    checks = {
        "clean_has_no_issues": not cases["clean"].truth,
        "both_stage1_types_present": {item["type"] for item in cases["issues"].truth} == {"duplicate_invoice", "payment_allocation"},
        "independent_reconciliation_matches": all(reconcile(bundle.public) == bundle.truth for bundle in cases.values()),
        "simple_clean_correct": clean_simple["evaluation"]["f1"] == 1 and clean_simple["evaluation"]["fp"] == 0,
        "simple_errors_proved": dirty_simple["evaluation"]["f1"] == 1 and dirty_simple["evaluation"]["tp"] == 2,
        "supported_findings_better_than_silence": dirty_simple["evaluation"]["reward"] > by_case["issues", "silent"]["evaluation"]["reward"],
        "clean_silence_better_than_accusations": by_case["clean", "silent"]["evaluation"]["reward"] > by_case["clean", "mark_all"]["evaluation"]["reward"],
        "mark_all_penalized": all(by_case[name, "mark_all"]["evaluation"]["reward"] == -1 for name in cases),
        "oracle_upper_bound": all(by_case[name, "oracle"]["evaluation"]["reward"] == 1 for name in cases),
        "closed_feedback_masked": all(
            item["reward"] == 0 and item["info"] == {}
            for report in reports for item in report["trajectory"]
        ),
        "no_private_fields_in_observations": not any(
            _has_private_keys(item["observation"]) for report in reports for item in report["trajectory"]
        ),
        "repeat_actions_observations_reward_exact": canonical_json(dirty_simple["trajectory"]) == canonical_json(replay["trajectory"]),
        "workers_exit_cleanly": all(report["agent_exitcode"] in (None, 0) for report in reports),
    }
    env = AuditEnv()
    env.load_bundle(cases["issues"])
    env.step({"action": "submit", "args": {"findings": {"episode_id": cases["issues"].public["manifest"]["episode_id"], "findings": False}}})
    invalid = env.evaluation_result()
    checks["malformed_submit_minus_one"] = invalid is not None and invalid["reward"] == -1 and invalid["status"] == "invalid_submit"

    probes = None
    if profile == "secure":
        probe = run_episode(cases["clean"], policy="probe", profile="secure", mode="closed")
        receipts = [line[len("ISOLATION_PROBE="):] for line in probe["stderr"].splitlines() if line.startswith("ISOLATION_PROBE=")]
        probes = json.loads(receipts[-1]) if receipts else {}
        checks["secure_isolation_probes"] = bool(probes) and all(value is True for value in probes.values())
        checks["probe_worker_submitted"] = probe["evaluation"]["termination_reason"] == "submitted" and probe["agent_exitcode"] == 0
    summary = {
        "stage": 1, "version": __version__, "schema_version": SCHEMA_VERSION,
        "scope": "G0/G1 native-currency vertical slice; two of four competition issue types",
        "profile": profile, "secure_isolation_verified": profile == "secure" and checks.get("secure_isolation_probes", False),
        "python": platform.python_version(), "platform": platform.system(), "architecture": platform.machine(),
        "checks": checks, "passed": all(checks.values()), "isolation_probes": probes,
        "runs": [{key: value for key, value in report.items() if key not in {"trajectory", "stderr"}} for report in reports],
        "remaining_competition_work": ["FX and cutoff families", "two interacting scenarios", "documented refunds", "10 train + 10 dev exports", "20-episode and 20-pair evaluation", "scorer mutation report and final release rehearsal"],
    }
    # Publish only after the complete closed batch has ended.
    with (output / "trajectories.jsonl").open("w", encoding="utf-8") as handle:
        for index, report in enumerate(reports):
            for item in report["trajectory"]:
                handle.write(canonical_json({"run_id": f"run-{index:02}", "policy": report["policy"], **item}) + "\n")
    _write_json(output / "evaluation.json", reports, private=True)
    _write_json(output / "summary.json", summary)
    if verify and not summary["passed"]:
        failed = [name for name, passed in checks.items() if not passed]
        raise RuntimeError("stage1_checks_failed:" + ",".join(failed))
    return summary


def execute_stage2_batch(*, profile: str, output: Path, seed: int, verify: bool) -> dict[str, Any]:
    """Verify four families, dependent pairs and legitimate foreign/refund cases."""
    from .money import convert_minor
    from .storage import export_episode, load_episode
    from datetime import datetime
    from zoneinfo import ZoneInfo

    cases = {name: generate(seed, config) for name, config in stage2_cases().items()}
    (output / "episodes").mkdir()
    roundtrips = True
    for name, bundle in cases.items():
        folder = output / "episodes" / name
        export_episode(bundle, folder)
        roundtrips &= load_episode(folder) == bundle
    reports = []
    for name, bundle in cases.items():
        for policy in ("simple", "silent", "mark_all", "random", "oracle"):
            report = run_episode(bundle, policy=policy, profile=profile, mode="closed")
            report["case"] = name
            reports.append(report)
    indexed = {(report["case"], report["policy"]): report for report in reports}
    expected = {
        "clean_multicurrency": set(), "duplicate_only": {"duplicate_invoice"},
        "allocation_only": {"payment_allocation"}, "fx_only": {"fx_conversion"},
        "cutoff_only": {"period_cutoff"},
        "all_four": {"duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"},
        "allocation_fx": {"payment_allocation", "fx_conversion"},
        "allocation_cutoff": {"payment_allocation", "period_cutoff"},
        "fx_wrong_date": {"fx_conversion"},
    }

    def shared_payment(name, other_type):
        bundle = cases[name]
        allocation = next(issue for issue in bundle.truth if issue["type"] == "payment_allocation")
        other = next(issue for issue in bundle.truth if issue["type"] == other_type)
        row = next(row for row in bundle.public["ledger"] if row["row_id"] == other["affected_ids"][0])
        return row["payment_id"] in allocation["affected_ids"]

    fx_book = cases["allocation_fx"].public
    allocation_issue = next(issue for issue in cases["allocation_fx"].truth if issue["type"] == "payment_allocation")
    payment = next(doc for doc in fx_book["payments"] if doc["payment_id"] in allocation_issue["affected_ids"])
    receipt = next(row for row in fx_book["ledger"] if row["payment_id"] == payment["payment_id"])
    rate_date = datetime.fromisoformat(payment["timestamp"]).astimezone(ZoneInfo(fx_book["manifest"]["timezone"])).date().isoformat()
    correct_rate = next(row["rate"] for row in fx_book["fx"] if row["date"] == rate_date and row["source_currency"] == payment["currency"] and row["target_currency"] == fx_book["manifest"]["base_currency"])
    remainder = payment["amount_minor"] - receipt["allocated_minor"]
    corrected_unallocated = convert_minor(remainder, correct_rate,
        fx_book["manifest"]["currency_exponents"][payment["currency"]],
        fx_book["manifest"]["currency_exponents"][fx_book["manifest"]["base_currency"]],
        rounding=fx_book["policy"]["rounding"])
    repeat = run_episode(cases["all_four"], policy="simple", profile=profile, mode="closed")
    checks = {
        "all_episode_shapes_and_family_counts": all({issue["type"] for issue in bundle.truth} == expected[name] and len(bundle.truth) == len(expected[name]) for name, bundle in cases.items()),
        "truth_blind_reconciliation_complete": all(reconcile(bundle.public) == bundle.truth for bundle in cases.values()),
        "public_private_roundtrips_exact": roundtrips,
        "each_family_independently_proved": all(indexed[name, "simple"]["evaluation"]["tp"] == 1 and indexed[name, "simple"]["evaluation"]["fp"] == 0 and indexed[name, "simple"]["evaluation"]["fn"] == 0 for name in ("duplicate_only", "allocation_only", "fx_only", "cutoff_only")),
        "all_four_proved_together": indexed["all_four", "simple"]["evaluation"]["tp"] == 4 and indexed["all_four", "simple"]["evaluation"]["f1"] == 1,
        "all_simple_answers_exact": all(indexed[name, "simple"]["evaluation"]["f1"] == 1 for name in cases),
        "clean_foreign_refund_has_no_accusations": indexed["clean_multicurrency", "simple"]["evaluation"]["fp"] == 0 and not cases["clean_multicurrency"].truth,
        "clean_has_foreign_documents": any(doc["currency"] != cases["clean_multicurrency"].public["manifest"]["base_currency"] for doc in cases["clean_multicurrency"].public["invoices"]),
        "clean_has_documented_refund": any(doc["total_minor"] < 0 and doc.get("original_invoice_id") for doc in cases["clean_multicurrency"].public["invoices"]),
        "fx_interaction_is_same_payment": shared_payment("allocation_fx", "fx_conversion"),
        "cutoff_interaction_is_same_payment": shared_payment("allocation_cutoff", "period_cutoff"),
        "allocation_amount_depends_on_fx_rate": corrected_unallocated != allocation_issue["expected_amount_minor"],
        "wrong_date_fx_is_proved": indexed["fx_wrong_date", "simple"]["evaluation"]["f1"] == 1,
        "all_evidence_sets_within_ten": all(1 <= len(proof) <= 10 for bundle in cases.values() for issue in bundle.truth for proof in issue["evidence_sets"]),
        "full_scan_within_fifty_inspections": all(indexed[name, "simple"]["evaluation"]["inspection_steps"] <= 50 for name in cases),
        "oracle_upper_bound": all(indexed[name, "oracle"]["evaluation"]["reward"] == 1 for name in cases),
        "mark_all_penalized": all(indexed[name, "mark_all"]["evaluation"]["reward"] == -1 for name in cases),
        "closed_feedback_masked": all(item["reward"] == 0 and item["info"] == {} for report in reports for item in report["trajectory"]),
        "no_private_observation_fields": not any(_has_private_keys(item["observation"]) for report in reports for item in report["trajectory"]),
        "repeat_actions_observations_exact": canonical_json(indexed["all_four", "simple"]["trajectory"]) == canonical_json(repeat["trajectory"]),
        "workers_exit_cleanly": all(report["agent_exitcode"] in (None, 0) for report in reports),
    }
    probes = None
    if profile == "secure":
        probe = run_episode(cases["clean_multicurrency"], policy="probe", profile=profile, mode="closed")
        receipts = [line.removeprefix("ISOLATION_PROBE=") for line in probe["stderr"].splitlines() if line.startswith("ISOLATION_PROBE=")]
        probes = json.loads(receipts[-1]) if receipts else {}
        checks["secure_isolation_probes"] = bool(probes) and all(value is True for value in probes.values())
        checks["probe_worker_submitted"] = probe["evaluation"]["termination_reason"] == "submitted" and probe["agent_exitcode"] == 0
    summary = {
        "stage": 2, "version": __version__, "schema_version": SCHEMA_VERSION,
        "scope": "G2: four families, dependent allocation+FX and allocation+cutoff, clean foreign/refund controls",
        "profile": profile, "secure_isolation_verified": profile == "secure" and checks.get("secure_isolation_probes", False),
        "python": platform.python_version(), "platform": platform.system(), "architecture": platform.machine(),
        "checks": checks, "passed": all(checks.values()), "isolation_probes": probes,
        "runs": [{key: value for key, value in report.items() if key not in {"trajectory", "stderr"}} for report in reports],
        "remaining_competition_work": ["10 train + 10 dev exports", "20-episode and 20-pair evaluation", "scorer mutation report and final release rehearsal"],
    }
    with (output / "trajectories.jsonl").open("w", encoding="utf-8") as handle:
        for index, report in enumerate(reports):
            for item in report["trajectory"]:
                handle.write(canonical_json({"run_id": f"run-{index:02}", "policy": report["policy"], **item}) + "\n")
    _write_json(output / "evaluation.json", reports, private=True)
    _write_json(output / "summary.json", summary)
    if verify and not summary["passed"]:
        raise RuntimeError("stage2_checks_failed:" + ",".join(name for name, passed in checks.items() if not passed))
    return summary


def execute_stage3_batch(*, profile: str, output: Path, master_seed: int | None = None, verify: bool = True) -> dict[str, Any]:
    """Frozen open corpus plus the complete local 20-episode/20-pair rehearsal."""
    from .corpus import export_corpus, verify_corpus
    from .protocol import run_protocol
    from .reporting import runtime_manifest, source_hashes

    started = time.monotonic()
    output.chmod(0o700)
    root = Path(__file__).resolve().parents[2]
    hashes = source_hashes(root)
    packaged = verify_corpus(root / "datasets")
    export_corpus(output / "datasets")
    exported = verify_corpus(output / "datasets")
    summary = run_protocol(profile=profile, output=output / "protocol",
                           master_seed=secrets.randbits(128) if master_seed is None else master_seed)
    checks = dict(summary["checks"])
    checks.update({
        "packaged_train_dev_verified": packaged["passed"] and packaged["split_counts"] == {"train": 10, "dev": 10},
        "exported_train_dev_verified": exported["passed"] and exported["split_counts"] == {"train": 10, "dev": 10},
        "packaged_exported_plans_identical": packaged["plan_sha256"] == exported["plan_sha256"],
    })
    probes = None
    if profile == "secure":
        probe = run_episode(generate(19, stage2_cases()["clean_multicurrency"]), policy="probe", profile="secure", mode="closed")
        receipts = [line.removeprefix("ISOLATION_PROBE=") for line in probe["stderr"].splitlines() if line.startswith("ISOLATION_PROBE=")]
        probes = json.loads(receipts[-1]) if receipts else {}
        checks["secure_isolation_probes"] = bool(probes) and all(value is True for value in probes.values())
        checks["probe_worker_submitted"] = probe["evaluation"]["termination_reason"] == "submitted" and probe["agent_exitcode"] == 0
    checks["source_unchanged_during_verification"] = source_hashes(root) == hashes
    summary.update({
        "version": __version__, "schema_version": SCHEMA_VERSION, "profile": profile,
        "scope": "G3 local rehearsal; frozen 10 train + 10 dev, same 20 cases for four policies, 20 ID/order pairs and exact action replay",
        "checks": checks, "passed": all(checks.values()),
        "secure_isolation_verified": profile == "secure" and checks.get("secure_isolation_probes", False),
        "isolation_probes": probes, "open_corpus": exported,
        "total_verification_seconds": time.monotonic() - started,
        "remaining_competition_work": ["test-only scorer mutation report", "showcase and design note", "final offline release transfer rehearsal"],
    })
    _write_json(output / "run-manifest.json", runtime_manifest(hashes=hashes, profile=profile), private=True)
    _write_json(output / "summary.json", summary, private=True)
    if verify and not summary["passed"]:
        raise RuntimeError("stage3_checks_failed:" + ",".join(name for name, passed in checks.items() if not passed))
    return summary


def _print_summary(summary: dict, output: Path) -> None:
    print(f"Stage {summary.get('stage', 1)}: {'PASS' if summary['passed'] else 'FAIL'} ({summary['profile']})")
    if summary.get("stage", 1) >= 3:
        print("Open corpus: 10 train + 10 dev")
        print(f"Control runs: {len(summary['runs'])}; paired cases: {len(summary['pair_checks'])}; exact replays: {len(summary['replay_checks'])}")
        if summary["stage"] >= 4:
            print(f"Showcase: {summary['showcase']['case_count']} cases, {len(summary['showcase']['runs'])} executions")
            counts = summary["mutations"]["counts"]
            print(f"Scorer mutations: {'PASS' if summary['mutations']['passed'] else 'FAIL'} ({counts['killed']}/{counts['declared']} detected)")
            print(f"Offline evidence report: {output / 'showcase.html'}")
    else:
        print("case                   policy     TP FP FN   F1   return  steps status")
        for run in summary["runs"]:
            result = run["evaluation"]
            f1 = "N/A" if result["f1"] is None else f"{result['f1']:.2f}"
            print(f"{run['case']:<22} {run['policy']:<10} {result['tp']:>2} {result['fp']:>2} {result['fn']:>2} {f1:>5} {result['reward']:>7.3f} {result['total_steps']:>5} {result['status']}")
    print(f"Checks: {sum(summary['checks'].values())}/{len(summary['checks'])}")
    print(f"Artifacts: {output}")
    if not summary["secure_isolation_verified"]:
        print("Cooperative development run: OS isolation is not verified.")


def execute_stage4_batch(*, profile: str, output: Path, master_seed: int | None = None, verify: bool = True) -> dict[str, Any]:
    """Retain the full G3 regression and add real showcase/mutation evidence."""
    from .reporting import process_memory, runtime_manifest, source_hashes
    from .showcase_report import render_showcase_report
    from .stage4 import run_mutation_checks, run_showcase

    started = time.monotonic()
    output.chmod(0o700)
    root = Path(__file__).resolve().parents[2]
    hashes = source_hashes(root)
    regression = output / "regression"
    regression.mkdir(mode=0o700)
    summary = execute_stage3_batch(profile=profile, output=regression, master_seed=master_seed, verify=True)
    showcase = run_showcase(profile=profile, output=output / "showcase")
    mutations = run_mutation_checks(output=output / "mutations")
    render_showcase_report(output / "showcase", output / "showcase.html")
    checks = {f"g3_{name}": passed for name, passed in summary["checks"].items()}
    checks.update({f"showcase_{name}": passed for name, passed in showcase["checks"].items()})
    checks.update({
        "mutation_baseline_and_declared_mutants_passed": mutations.get("passed") is True,
        "completed_evidence_report_created": (output / "showcase.html").is_file(),
        "source_unchanged_during_stage4": source_hashes(root) == hashes,
    })
    summary.update({
        "stage": 4, "scope": "G4: full G3 regression, real masking/repair showcase and isolated scorer mutation checks",
        "checks": checks, "passed": all(checks.values()), "showcase": showcase, "mutations": mutations,
        "total_verification_seconds": time.monotonic() - started,
        "stage4_resource_measurements": process_memory(),
        "remaining_competition_work": ["submission design note and release inventory", "offline package transfer rehearsal and final demonstration"],
    })
    _write_json(output / "run-manifest.json", runtime_manifest(hashes=hashes, profile=profile), private=True)
    _write_json(output / "summary.json", summary, private=True)
    _write_json(output / "COMPLETE.json", {
        "completed": True, "passed": summary["passed"], "stage": 4,
        "sha256": {name: hashlib.sha256((output / name).read_bytes()).hexdigest()
                   for name in ("summary.json", "run-manifest.json", "showcase.html", "showcase/COMPLETE.json", "mutations/summary.json", "regression/summary.json")},
    }, private=True)
    if verify and not summary["passed"]:
        raise RuntimeError("stage4_checks_failed:" + ",".join(name for name, passed in checks.items() if not passed))
    return summary


def execute_stage5_batch(*, profile: str, output: Path, master_seed: int | None = None, verify: bool = True) -> dict[str, Any]:
    """Application/material acceptance; external transfer has its own receipt."""
    from .reporting import process_memory, runtime_manifest, source_hashes
    from .stage5 import run_release_readiness

    started = time.monotonic()
    root = Path(__file__).resolve().parents[2]
    hashes = source_hashes(root)
    output.chmod(0o700)
    application = output / "application"
    application.mkdir(mode=0o700)
    summary = execute_stage4_batch(profile=profile, output=application, master_seed=master_seed, verify=True)
    readiness = run_release_readiness(profile=profile, output=output / "readiness")
    checks = dict(summary["checks"])
    checks.update({f"release_{key}": value for key, value in readiness["checks"].items()})
    checks["release_source_unchanged"] = source_hashes(root) == hashes
    summary.update(stage=5, scope="G5 application and submission-material acceptance; immutable archive transfer attested separately",
                   checks=checks, passed=all(checks.values()), release_readiness=readiness,
                   total_verification_seconds=time.monotonic() - started,
                   stage5_resource_measurements=process_memory(),
                   application_artifacts="application", transfer_attestation="CLEAN_ROOM_REPORT.json beside the immutable release archive; not inferred by in-container tests",
                   remaining_competition_work=["external archive verification and clean offline transfer attestation"])
    # Keep the sealed G4 subtree intact; never rename files referenced by a
    # completed phase's relative checksum manifest.
    shutil.copyfile(application / "showcase.html", output / "showcase.html")
    (output / "showcase.html").chmod(0o600)
    _write_json(output / "summary.json", summary, private=True)
    _write_json(output / "run-manifest.json", runtime_manifest(hashes=hashes, profile=profile), private=True)
    _write_json(output / "COMPLETE.json", {
        "stage": 5, "completed": True, "passed": summary["passed"],
        "sha256": {name: hashlib.sha256((output / name).read_bytes()).hexdigest() for name in
                   ("summary.json", "run-manifest.json", "application/summary.json", "application/COMPLETE.json",
                    "readiness/summary.json", "readiness/trajectories.jsonl", "showcase.html")},
    }, private=True)
    if verify and not summary["passed"]:
        raise RuntimeError("stage5_application_checks_failed:" + ",".join(key for key, value in checks.items() if not value))
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Synthetic financial audit trainer")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("demo", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--profile", choices=["secure", "cooperative"], default="cooperative")
        command.add_argument("--seed", type=int, default=19, help="Legacy stage1/2 generation seed; stages3/4/5 use --master-seed.")
        command.add_argument("--output")
        command.add_argument("--stage", type=int, choices=[1, 2, 3, 4, 5], default=5)
        command.add_argument("--master-seed", type=int, help="Trusted stage3/4/5 cohort seed; otherwise newly generated and saved in the private protocol receipt.")
    command = commands.add_parser("showcase", help="Run the masking/repair demonstration and build its completed offline evidence report")
    command.add_argument("--profile", choices=["secure", "cooperative"], default="cooperative")
    command.add_argument("--output")
    command = commands.add_parser("serve", help="Open the local interactive audit workbench (educational mode)")
    command.add_argument("--port", type=int, default=8765)
    command = commands.add_parser("datasets", help="Export or verify the frozen open 10 train + 10 dev corpus")
    command.add_argument("operation", choices=["export", "verify"])
    command.add_argument("--directory", required=True)
    command = commands.add_parser("generate")
    command.add_argument("--seed", type=int, default=19)
    command.add_argument("--clean", action="store_true")
    command.add_argument("--output", required=True)
    command.add_argument("--stage", type=int, choices=[1, 2], default=2)
    command.add_argument("--scenario", choices=list(stage2_cases()), default="all_four")
    command.add_argument("--presentation-seed", type=int, help="Optional synthetic company/customer names; financial facts and frozen corpus stay unchanged.")
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            from .web import serve
            serve(args.port)
            return
        if args.command == "datasets":
            from .corpus import export_corpus, verify_corpus
            directory = Path(args.directory).absolute()
            if args.operation == "export":
                export_corpus(directory)
            print(json.dumps(verify_corpus(directory), ensure_ascii=False, indent=2, allow_nan=False))
            return
        if args.command == "generate":
            from .storage import export_episode
            if args.stage == 2:
                config = stage2_cases()["clean_multicurrency" if args.clean else args.scenario]
            else:
                config = {"duplicate_count": 0, "allocation_count": 0} if args.clean else {}
            bundle = generate(args.seed, config)
            if args.presentation_seed is not None:
                from .presentation import generate_named
                bundle = generate_named(args.seed, config, presentation_seed=args.presentation_seed)
            export_episode(bundle, Path(args.output))
            print(f"Episode exported: {Path(args.output).resolve()}")
            return
        output = _output_directory(args.output)
        if args.command == "showcase":
            from .showcase_report import render_showcase_report
            from .stage4 import run_showcase
            summary = run_showcase(profile=args.profile, output=output / "showcase")
            report = render_showcase_report(output / "showcase", output / "showcase.html")
            if not summary["passed"]:
                raise RuntimeError("showcase_checks_failed")
            print(f"Showcase: PASS ({args.profile}); {summary['case_count']} cases / {len(summary['runs'])} executions")
            print(f"Offline evidence report: {report}")
            return
        if args.stage == 5:
            summary = execute_stage5_batch(profile=args.profile, output=output, master_seed=args.master_seed, verify=args.command == "verify")
        elif args.stage == 4:
            summary = execute_stage4_batch(profile=args.profile, output=output, master_seed=args.master_seed, verify=args.command == "verify")
        elif args.stage == 3:
            summary = execute_stage3_batch(profile=args.profile, output=output, master_seed=args.master_seed, verify=args.command == "verify")
        else:
            execute = execute_stage2_batch if args.stage == 2 else execute_batch
            summary = execute(profile=args.profile, output=output, seed=args.seed, verify=args.command == "verify")
        _print_summary(summary, output)
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
