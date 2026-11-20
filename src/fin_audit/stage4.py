"""Trusted showcase execution and isolated scorer mutation acceptance.

Evidence cards and mathematical diagnostics are post-run artifacts. Ordinary
policies receive only the existing public tool protocol and no presentation map.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import __version__
from .checker import reconcile
from .contracts import SCHEMA_VERSION, canonical_json, strict_json_loads
from .reporting import runtime_manifest, source_hashes
from .runner import run_episode
from .storage import export_episode, load_episode
from .transforms import transform_episode

SHOWCASE_VERSION = "1.0"
_FILES = ("public/manifest.json", "public/policy.json", "public/ledger.csv",
          "public/invoices.jsonl", "public/payments.jsonl", "public/fx.csv",
          "private/truth.json", "private/provenance.json")


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(canonical_json(value) + "\n")


def _quality(report: dict) -> tuple:
    result = report["evaluation"]
    return tuple(result[key] for key in ("tp", "fp", "fn", "precision", "recall", "f1"))


def run_showcase(*, profile: str, output: Path) -> dict[str, Any]:
    """Execute real policies on all counterfactuals, then publish a new receipt."""
    from .showcase import showcase_analysis, showcase_cases

    if profile not in {"cooperative", "secure"}:
        raise ValueError("invalid_showcase_profile")
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("showcase_output_already_exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}-staging-", dir=output.parent))
    root = Path(__file__).resolve().parents[2]
    hashes = source_hashes(root)
    started = time.monotonic()
    published = False
    reserved_inode = None
    try:
        cases = showcase_cases()
        expected = {"clean", "duplicate_only", "fx_only", "masked_both", "allocation_fx",
                    "allocation_fx_repaired", "allocation_cutoff", "allocation_cutoff_repaired"}
        if set(cases) != expected:
            raise ValueError("showcase_case_contract_mismatch")
        # The fixed presentation seed is public showcase metadata, never a
        # substitute for the private holdout seed used by the G3 regression.
        cases["transformed_masked"] = transform_episode(cases["masked_both"], 2026112204).bundle
        manifest_episodes = []
        for key, bundle in cases.items():
            if reconcile(bundle.public) != bundle.truth:
                raise ValueError("showcase_independent_reconciliation_failed")
            destination = stage / "episodes" / key
            export_episode(bundle, destination)
            if load_episode(destination) != bundle:
                raise ValueError("showcase_storage_roundtrip_failed")
            manifest_episodes.append({
                "case": key, "episode_id": bundle.public["manifest"]["episode_id"],
                "public_sha256": _digest(bundle.public), "truth_sha256": _digest(bundle.truth),
                "files": {name: hashlib.sha256((destination / name).read_bytes()).hexdigest() for name in _FILES},
            })
        reports = []
        for key, bundle in cases.items():
            for policy in ("simple", "silent", "oracle"):
                report = run_episode(bundle, policy=policy, profile=profile, mode="closed")
                report["case"] = key
                report["trajectory_run_id"] = f"showcase-{len(reports):03}"
                reports.append(report)
        index = {(report["case"], report["policy"]): report for report in reports}
        # This analysis first becomes an output after every ordinary worker has
        # ended. It contains evidence and repairs for the human reviewer only.
        analysis = showcase_analysis({key: cases[key] for key in cases if key != "transformed_masked"})
        if analysis.get("showcase_version") != SHOWCASE_VERSION or analysis.get("scope") != "post_run_only":
            raise ValueError("showcase_analysis_contract_mismatch")
        analysis_checks = analysis.get("checks")
        if type(analysis_checks) is not dict or not analysis_checks or any(type(value) is not bool for value in analysis_checks.values()):
            raise ValueError("invalid_showcase_analysis_checks")
        checks = {
            "all_independent_books_and_storage_verified": True,
            "financial_counterfactual_checks": all(analysis_checks.values()),
            "nine_cases_twenty_seven_executions": len(cases) == 9 and len(reports) == 27,
            "simple_all_findings_exact": all(index[key, "simple"]["evaluation"]["tp"] == len(bundle.truth)
                and index[key, "simple"]["evaluation"]["fp"] == 0
                and index[key, "simple"]["evaluation"]["fn"] == 0
                and index[key, "simple"]["evaluation"]["f1"] == 1 for key, bundle in cases.items()),
            "oracle_common_upper_bound": all(index[key, "oracle"]["evaluation"]["reward"] == 1 for key in cases),
            "proved_dirty_answers_better_than_silence": all(index[key, "simple"]["evaluation"]["reward"] > index[key, "silent"]["evaluation"]["reward"] for key, bundle in cases.items() if bundle.truth),
            "clean_silence_is_correct": index["clean", "silent"]["evaluation"]["f1"] == 1,
            "all_adapters_submit_and_exit": all(report["evaluation"]["termination_reason"] == "submitted"
                and report["evaluation"]["status"] == "scored" and report["agent_exitcode"] in (0, None) for report in reports),
            "closed_wire_feedback_masked": all(row["reward"] == 0 and row["info"] == {} for report in reports for row in report["trajectory"]),
            "masked_pair_simple_quality_equal": _quality(index["masked_both", "simple"]) == _quality(index["transformed_masked", "simple"]),
            "masked_pair_oracle_quality_equal": _quality(index["masked_both", "oracle"]) == _quality(index["transformed_masked", "oracle"]),
            "source_unchanged_during_showcase": source_hashes(root) == hashes,
        }
        with (stage / "trajectories.jsonl").open("x", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            for report in reports:
                for row in report["trajectory"]:
                    handle.write(canonical_json({"run_id": report["trajectory_run_id"], "policy": report["policy"], **row}) + "\n")
        compact = [{key: value for key, value in report.items() if key not in {"trajectory", "stderr"}} for report in reports]
        summary = {
            "showcase_version": SHOWCASE_VERSION, "software_version": __version__, "schema_version": SCHEMA_VERSION,
            "scope": "post_run_only", "profile": profile, "passed": all(checks.values()), "checks": checks,
            "case_count": len(cases), "runs": compact, "elapsed_seconds": time.monotonic() - started,
            "execution_counts": {"run_episode_calls": 27, "ordinary_workers": 18, "trusted_oracles": 9},
        }
        _write_json(stage / "analysis.json", analysis)
        _write_json(stage / "summary.json", summary)
        _write_json(stage / "evaluation.json", compact)
        _write_json(stage / "run-manifest.json", {
            **runtime_manifest(hashes=hashes, profile=profile), "showcase_version": SHOWCASE_VERSION,
            "episodes": manifest_episodes,
        })
        _write_json(stage / "COMPLETE.json", {
            "showcase_version": SHOWCASE_VERSION, "completed": True, "passed": summary["passed"],
            "sha256": {name: hashlib.sha256((stage / name).read_bytes()).hexdigest() for name in
                       ("analysis.json", "summary.json", "evaluation.json", "run-manifest.json", "trajectories.jsonl")},
        })
        output.mkdir(mode=0o700)
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
        descriptor, failure = tempfile.mkstemp(prefix=f".{output.name}-FAILED-", suffix=".json", dir=output.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(canonical_json({"completed": False, "passed": False, "error_type": type(error).__name__,
                                         "error": str(error)[:1000], "software_version": __version__}) + "\n")
        raise
    finally:
        if not published:
            shutil.rmtree(stage, ignore_errors=True)


def run_mutation_checks(*, output: Path) -> dict[str, Any]:
    """Run trusted mutation acceptance with 60-second detector budgets.

    The complete harness remains bounded to 300 seconds. Standalone developer
    CLI defaults are separate; timeouts still invalidate an experiment.
    """
    root = Path(__file__).resolve().parents[2]
    command = [sys.executable, str(root / "scripts/mutation_check.py"),
               "--timeout", "60", "--baseline-timeout", "60", "--output", str(Path(output).absolute())]
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            try:
                returncode = process.wait(timeout=300)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError("mutation_harness_timeout") from error
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        if returncode:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - 4096))
            diagnostic = log.read(4096).decode("utf-8", errors="replace")
            raise RuntimeError(f"mutation_harness_failed:{returncode}:{diagnostic}")
    summary = strict_json_loads((Path(output) / "summary.json").read_text(encoding="utf-8"))
    if type(summary) is not dict or summary.get("passed") is not True:
        raise RuntimeError("mutation_harness_did_not_pass")
    return summary
