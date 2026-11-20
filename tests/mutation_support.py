"""Test-only seeded faults and an isolated, assertion-aware pytest runner.

This module is never imported by the production scorer or agent worker. The
frozen fault plan is intentionally finite, not exhaustive mutation coverage.
"""

from __future__ import annotations

import ast
import copy
import difflib
import hashlib
import json
import math
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PLAN_PATH = "configs/scorer-mutations.json"
SCORER_PATH = "src/fin_audit/evaluation.py"
TEST_PATH = "tests/test_evaluation.py"
MAX_CAPTURE_BYTES = 32 * 1024
MAX_STREAM_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024
OPERATORS = frozenset({
    "ignore_currency", "evidence_overlap", "reuse_truth", "omit_fp", "omit_fn",
    "wrong_clean_f1", "wrong_step_cost", "greedy_matching", "ignore_amount",
    "exclusive_tolerance", "affected_superset", "positive_invalid_reward",
})
_F1 = "Fraction(2 * tp, 2 * tp + fp + fn) if issues or normalized else Fraction(1)"
_AMOUNT = 'abs(finding["claimed_amount_minor"] - issue.expected_amount_minor) <= configured_tolerances.get(issue.type, 0)'


class MutationHarnessError(ValueError):
    """An invalid frozen plan, target, or publication request."""


@dataclass(frozen=True)
class MutationPatch:
    source: str
    diff: str
    source_sha256: str
    mutated_sha256: str
    patch_sha256: str
    ast_before_sha256: str
    ast_after_sha256: str
    target_line: int
    target_end_line: int
    target_matches: int


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _dump(node: ast.AST) -> str:
    return ast.dump(node, include_attributes=False)


def _expr(text: str) -> ast.expr:
    return ast.parse(text, mode="eval").body


def load_plan(root: Path) -> dict:
    plan = json.loads((root / PLAN_PATH).read_text(encoding="utf-8"))
    if plan.get("source") != SCORER_PATH or plan.get("baseline_tests") != [TEST_PATH]:
        raise MutationHarnessError("unexpected_mutation_source_or_baseline")
    mutants = plan.get("mutants")
    if not isinstance(mutants, list) or len(mutants) != 12:
        raise MutationHarnessError("frozen_plan_requires_twelve_faults")
    if {item.get("operator") for item in mutants} != OPERATORS:
        raise MutationHarnessError("frozen_operator_set_mismatch")
    if len({item.get("id") for item in mutants}) != len(mutants):
        raise MutationHarnessError("duplicate_mutant_id")
    for item in mutants:
        if not isinstance(item.get("id"), str) or not item["id"].replace("_", "").isalnum():
            raise MutationHarnessError("unsafe_mutant_id")
        if item.get("equivalent") is not False or not item.get("description"):
            raise MutationHarnessError("equivalent_or_undescribed_fault_not_allowed")
        tests = item.get("detection_tests")
        if not isinstance(tests, list) or not tests or any(
            not isinstance(test, str) or not test.startswith(TEST_PATH + "::test_")
            or not test.removeprefix(TEST_PATH + "::").replace("_", "").isalnum()
            for test in tests
        ):
            raise MutationHarnessError("invalid_detection_selectors")
    return plan


def _target_expression(tree: ast.Module, original: str) -> ast.expr:
    expected = _dump(_expr(original))
    targets = [node for node in ast.walk(tree) if isinstance(node, ast.expr) and _dump(node) == expected]
    if len(targets) != 1:
        raise MutationHarnessError(f"mutation_target_not_unique:{len(targets)}")
    return targets[0]


def apply_mutation(source: str, operator: str) -> MutationPatch:
    """Change exactly one predeclared AST target, retaining unrelated source."""
    tree = ast.parse(source)
    replacements = {
        "ignore_currency": ('finding["currency"] == issue.currency', "True"),
        "evidence_overlap": ("any(proof.issubset(evidence) for proof in issue.evidence_sets)",
                             "any(bool(proof & evidence) for proof in issue.evidence_sets)"),
        "omit_fp": (_F1, "Fraction(2 * tp, 2 * tp + fn) if issues or normalized else Fraction(1)"),
        "omit_fn": (_F1, "Fraction(2 * tp, 2 * tp + fp) if issues or normalized else Fraction(1)"),
        "wrong_clean_f1": (_F1, "Fraction(2 * tp, 2 * tp + fp + fn) if issues or normalized else Fraction(0)"),
        "wrong_step_cost": ("Fraction(inspection_steps, 500)", "Fraction(inspection_steps, 50)"),
        "ignore_amount": (_AMOUNT, "True"),
        "exclusive_tolerance": (_AMOUNT, _AMOUNT.replace(" <= ", " < ")),
        "affected_superset": ("affected == issue.affected_ids", "issue.affected_ids.issubset(affected)"),
    }
    if operator in replacements:
        original, replacement = replacements[operator]
        target = _target_expression(tree, original)
        new_node: ast.AST = _expr(replacement)
    elif operator in {"reuse_truth", "greedy_matching"}:
        targets = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_maximum_matching"]
        if len(targets) != 1:
            raise MutationHarnessError("matching_function_target_not_unique")
        target = targets[0]
        new_node = copy.deepcopy(target)
        body = (
            "def replacement():\n    return tuple((index, candidates[0]) for index, candidates in enumerate(adjacency) if candidates)\n"
            if operator == "reuse_truth" else
            "def replacement():\n    used = set()\n    pairs = []\n    for index, candidates in enumerate(adjacency):\n        for candidate in candidates:\n            if candidate not in used:\n                used.add(candidate)\n                pairs.append((index, candidate))\n                break\n    return tuple(pairs)\n"
        )
        new_node.body = ast.parse(body).body[0].body
    elif operator == "positive_invalid_reward":
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "EvaluationResult"
                 and any(keyword.arg == "status" and isinstance(keyword.value, ast.Constant)
                         and keyword.value.value == "invalid_submit" for keyword in node.keywords)]
        values = [keyword.value for call in calls for keyword in call.keywords if keyword.arg == "reward"]
        if len(values) != 1 or _dump(values[0]) != _dump(_expr("-1.0")):
            raise MutationHarnessError("invalid_reward_target_not_unique")
        target, new_node = values[0], _expr("1.0")
    else:
        raise MutationHarnessError("unknown_mutation_operator")

    # CPython AST columns are UTF-8 byte offsets, not Unicode character offsets.
    encoded = source.encode("utf-8")
    lines = encoded.splitlines(keepends=True)
    start = sum(map(len, lines[:target.lineno - 1])) + target.col_offset
    end = sum(map(len, lines[:target.end_lineno - 1])) + target.end_col_offset
    updated = (encoded[:start] + ast.unparse(new_node).encode("utf-8") + encoded[end:]).decode("utf-8")
    after = ast.parse(updated)
    compile(after, "mutated-evaluation.py", "exec")
    if _dump(tree) == _dump(after):
        raise MutationHarnessError("mutation_is_ast_noop")
    diff = "".join(difflib.unified_diff(source.splitlines(keepends=True), updated.splitlines(keepends=True),
                                      fromfile="original/evaluation.py", tofile=f"mutants/{operator}/evaluation.py"))
    return MutationPatch(updated, diff, _sha(encoded), _sha(updated.encode()), _sha(diff.encode()),
                         _sha(_dump(tree).encode()), _sha(_dump(after).encode()),
                         target.lineno, target.end_lineno, 1)


_CHILD = r'''import hashlib, json, math, os, resource, sys
from pathlib import Path

src, report_path, expected_sha, expected_contract_sha = sys.argv[1:5]
tests = sys.argv[5:]
sys.path.insert(0, src)
result = {"origin_verified": False, "records": [], "collection_errors": []}

def save():
    Path(report_path).write_text(json.dumps(result, ensure_ascii=True, allow_nan=False), encoding="utf-8")

try:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * 1024, 2 * 1024 * 1024))
    import fin_audit.evaluation as scorer
    import fin_audit.contracts as contracts
    origins = {}
    for name, module, filename, expected in (
        ("evaluation", scorer, "evaluation.py", expected_sha),
        ("contracts", contracts, "contracts.py", expected_contract_sha),
    ):
        path = Path(module.__file__).resolve()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if path != (Path(src) / "fin_audit" / filename).resolve() or digest != expected:
            raise RuntimeError("isolated_module_origin_mismatch")
        origins[name] = {"path": str(path), "sha256": digest}
    result.update(origin_verified=True, origins=origins)
    import pytest

    class Capture:
        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_makereport(self, item, call):
            outcome = yield
            report = outcome.get_result()
            kind = None if call.excinfo is None else call.excinfo.type.__module__ + "." + call.excinfo.type.__qualname__
            assertion = call.excinfo is not None and issubclass(call.excinfo.type, AssertionError)
            result["records"].append({
                "nodeid": report.nodeid, "when": report.when, "outcome": report.outcome,
                "exception_type": kind, "semantic_assertion": bool(assertion and report.when == "call" and report.failed),
            })

        def pytest_collectreport(self, report):
            if report.failed:
                result["collection_errors"].append(str(report.longrepr)[:2000])

    code = int(pytest.main(["-q", "--tb=short", "-p", "no:cacheprovider", "-c", "pytest.ini", *tests], plugins=[Capture()]))
    result["pytest_exitcode"] = code
    save()
    raise SystemExit(code)
except Exception as error:
    result.update(launcher_error=type(error).__name__ + ":" + str(error)[:1000])
    save()
    raise SystemExit(70)
'''


def classify_execution(exitcode: int | None, events: Any, *, timed_out: bool = False,
                       output_limit: bool = False, baseline: bool = False) -> dict:
    """A test assertion is a kill; test infrastructure/runtime failures are not."""
    invalid = {"status": "invalid", "passed": False, "detected": False, "killed_by": []}
    if timed_out or output_limit:
        return {**invalid, "reason": "timeout" if timed_out else "output_limit"}
    if not isinstance(events, dict):
        return {**invalid, "reason": "missing_or_invalid_child_report"}
    if not events.get("origin_verified"):
        return {**invalid, "reason": "origin_or_import_error"}
    if events.get("launcher_error") or events.get("collection_errors"):
        return {**invalid, "reason": "launcher_or_collection_error"}
    records = events.get("records", [])
    calls = [record for record in records if record["when"] == "call"]
    failed = [record for record in records if record["outcome"] == "failed"]
    if any(not record.get("semantic_assertion") for record in failed):
        return {**invalid, "reason": "runtime_or_fixture_error"}
    if not calls or all(record["outcome"] == "skipped" for record in calls):
        return {**invalid, "reason": "no_executed_detection_tests"}
    if exitcode == 0 and not failed:
        return {"status": "passed" if baseline else "survived", "passed": baseline,
                "detected": False, "reason": None if baseline else "all_detection_tests_passed", "killed_by": []}
    if exitcode == 1 and failed:
        return {"status": "failed" if baseline else "killed", "passed": False,
                "detected": not baseline, "reason": "baseline_assertion_failure" if baseline else "assertion_failure",
                "killed_by": [record["nodeid"] for record in failed]}
    return {**invalid, "reason": "unexpected_exitcode"}


def _stop(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Some macOS hosts deny a signal to a group whose leader has already
        # been reaped. Never mask a failure to stop a still-running child.
        if process.poll() is None:
            raise
    process.wait(timeout=3)


def run_detection(root: Path, source: str, tests: list[str], *, timeout_seconds: float = 10,
                  baseline: bool = False) -> dict:
    """Run the actual copied detector tests against a genuinely isolated scorer."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise MutationHarnessError("invalid_timeout")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="fin-audit-mutation-") as temporary:
        work = Path(temporary)
        package = work / "src" / "fin_audit"
        package.mkdir(parents=True)
        for filename in ("__init__.py", "contracts.py"):
            shutil.copyfile(root / "src" / "fin_audit" / filename, package / filename)
        (package / "evaluation.py").write_text(source, encoding="utf-8")
        (work / "tests").mkdir()
        shutil.copyfile(root / TEST_PATH, work / TEST_PATH)
        (work / "pytest.ini").write_text("[pytest]\naddopts=\n", encoding="utf-8")
        (work / "detect.py").write_text(_CHILD, encoding="utf-8")
        source_sha = _sha((package / "evaluation.py").read_bytes())
        contract_sha = _sha((package / "contracts.py").read_bytes())
        command = [sys.executable, "-I", "-B", str(work / "detect.py"), str(work / "src"),
                   str(work / "events.json"), source_sha, contract_sha, *tests]
        environment = {key: value for key, value in os.environ.items() if key not in {
            "PYTHONPATH", "PYTHONHOME", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONSTARTUP"}}
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        process = subprocess.Popen(command, cwd=work, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   close_fds=True, start_new_session=True)
        assert process.stdout is not None
        os.set_blocking(process.stdout.fileno(), False)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        captured = bytearray()
        total = 0
        stream_hash = hashlib.sha256()
        timed_out = output_limit = False
        deadline = time.monotonic() + timeout_seconds
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                for key, _ in selector.select(min(remaining, 0.1)):
                    try:
                        chunk = os.read(key.fileobj.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    stream_hash.update(chunk)
                    captured.extend(chunk[:max(0, MAX_CAPTURE_BYTES - len(captured))])
                    if total > MAX_STREAM_BYTES:
                        output_limit = True
                        break
                if output_limit:
                    break
            if not timed_out and not output_limit:
                # stdout EOF is not process exit: pytest may still be writing
                # its report or completing Python teardown. Preserve its real
                # exit status before cleaning up the process group.
                try:
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    timed_out = True
        finally:
            selector.close()
            _stop(process)
            process.stdout.close()
        events = None
        result_path = work / "events.json"
        if result_path.is_file() and result_path.stat().st_size <= MAX_RESULT_BYTES:
            try:
                events = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        classification = classify_execution(process.returncode, events, timed_out=timed_out,
                                            output_limit=output_limit, baseline=baseline)
        records = events.get("records", []) if isinstance(events, dict) else []
        return {**classification, "tests": list(tests), "command": command,
                "exitcode": process.returncode, "timed_out": timed_out, "output_limit": output_limit,
                "elapsed_seconds": time.monotonic() - started, "events": events,
                "source_sha256": source_sha, "contracts_sha256": contract_sha,
                "test_counts": dict(Counter(record["outcome"] for record in records if record["when"] == "call")),
                "stdout": captured.decode("utf-8", errors="replace"), "stdout_bytes_read": total,
                "stdout_truncated": total > len(captured), "read_stream_sha256": stream_hash.hexdigest()}


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def run_mutation_suite(root: Path, output: Path, *, timeout_seconds: float = 10,
                       baseline_timeout_seconds: float = 30) -> dict:
    """Run the full frozen plan; no CLI filtering or production mutation exists."""
    root = root.resolve()
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("mutation_output_already_exists")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    plan = load_plan(root)
    source = (root / SCORER_PATH).read_bytes().decode("utf-8")
    paths = [SCORER_PATH, "src/fin_audit/contracts.py", "src/fin_audit/__init__.py", TEST_PATH,
             PLAN_PATH, "tests/mutation_support.py", "scripts/mutation_check.py"]
    snapshot = {path: _sha((root / path).read_bytes()) for path in paths}
    started = time.monotonic()
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}-mutation-staging-", dir=output.parent))
    stage.chmod(0o700)
    reserved_inode = None
    published = False
    try:
        (stage / "original-evaluation.py").write_text(source, encoding="utf-8")
        shutil.copyfile(root / PLAN_PATH, stage / "frozen-plan.json")
        baseline = run_detection(root, source, plan["baseline_tests"], timeout_seconds=baseline_timeout_seconds, baseline=True)
        _write_json(stage / "baseline.json", baseline)
        (stage / "baseline-stdout.txt").write_text(baseline["stdout"], encoding="utf-8")
        results = []
        for spec in plan["mutants"]:
            entry = {**spec, "status": "not_run", "detected": False, "killed_by": [],
                     "reason": "baseline_failed", "exitcode": None, "elapsed_seconds": 0.0}
            if baseline["passed"]:
                destination = stage / "mutants" / spec["id"]
                destination.mkdir(parents=True)
                try:
                    patch = apply_mutation(source, spec["operator"])
                    (destination / "evaluation.py").write_text(patch.source, encoding="utf-8")
                    (destination / "patch.diff").write_text(patch.diff, encoding="utf-8")
                    run = run_detection(root, patch.source, spec["detection_tests"], timeout_seconds=timeout_seconds)
                    entry.update(run, original_source_sha256=patch.source_sha256,
                                 mutated_source_sha256=patch.mutated_sha256, patch_sha256=patch.patch_sha256,
                                 ast_before_sha256=patch.ast_before_sha256, ast_after_sha256=patch.ast_after_sha256,
                                 target_line=patch.target_line, target_end_line=patch.target_end_line,
                                 target_matches=patch.target_matches,
                                 artifacts={"source": f"mutants/{spec['id']}/evaluation.py",
                                            "patch": f"mutants/{spec['id']}/patch.diff",
                                            "result": f"mutants/{spec['id']}/result.json"})
                except (MutationHarnessError, SyntaxError) as error:
                    entry.update(status="invalid", reason=str(error), detected=False)
                _write_json(destination / "result.json", entry)
                (destination / "stdout.txt").write_text(entry.get("stdout", ""), encoding="utf-8")
            results.append(entry)
        source_unchanged = snapshot == {path: _sha((root / path).read_bytes()) for path in paths}
        counts = {name: sum(result["status"] == name for result in results)
                  for name in ("killed", "survived", "invalid", "not_run")}
        counts.update(declared=len(results), timeouts=sum(result.get("timed_out", False) for result in results))
        version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
        summary = {
            "plan_version": plan["plan_version"], "software_version": version,
            "scope": plan["purpose"], "passed": baseline["passed"] and counts["killed"] == len(results) and source_unchanged,
            "baseline": baseline, "mutants": results, "counts": counts,
            "source_snapshot_unchanged": source_unchanged, "source_sha256": snapshot,
            "elapsed_seconds": time.monotonic() - started,
            "classification": plan["classification"],
        }
        _write_json(stage / "summary.json", summary)
        _write_json(stage / "manifest.json", {
            "plan_version": plan["plan_version"], "software_version": version,
            "declared_ids": [spec["id"] for spec in plan["mutants"]], "source_sha256": snapshot,
            "python": sys.version, "timeouts_seconds": {"baseline": baseline_timeout_seconds, "mutant": timeout_seconds},
            "isolation": "Fresh subprocess and temporary package tree; module path+SHA verified. Not an OS security sandbox claim.",
            "summary_sha256": _sha((stage / "summary.json").read_bytes()),
        })
        _write_json(stage / "COMPLETE.json", {
            "completed": True, "passed": summary["passed"],
            "sha256": {name: _sha((stage / name).read_bytes()) for name in
                       ("summary.json", "manifest.json", "frozen-plan.json", "original-evaluation.py")},
        })
        output.mkdir(mode=0o700)
        reserved_inode = output.stat().st_ino
        os.rename(stage, output)
        published = True
        return summary
    finally:
        if not published:
            if reserved_inode is not None and output.is_dir() and output.stat().st_ino == reserved_inode:
                try:
                    output.rmdir()
                except OSError:
                    pass
            shutil.rmtree(stage, ignore_errors=True)
