#!/usr/bin/env python3
"""Run the complete predeclared test-only scorer mutation suite."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from mutation_support import MutationHarnessError, run_mutation_suite


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New output leaf; existing paths are never replaced.")
    parser.add_argument("--timeout", type=float, default=10, help="Per-mutant wall-time limit in seconds.")
    parser.add_argument("--baseline-timeout", type=float, default=30)
    args = parser.parse_args()
    try:
        summary = run_mutation_suite(ROOT, args.output, timeout_seconds=args.timeout,
                                     baseline_timeout_seconds=args.baseline_timeout)
    except (MutationHarnessError, OSError, ValueError) as error:
        parser.exit(2, f"Mutation setup failed: {error}\n")
    counts = summary["counts"]
    print(f"Scorer mutations: {'PASS' if summary['passed'] else 'FAIL'}; "
          f"baseline={'PASS' if summary['baseline']['passed'] else 'FAIL'}; "
          f"killed={counts['killed']}/{counts['declared']}, survived={counts['survived']}, "
          f"invalid={counts['invalid']}, timeouts={counts['timeouts']}, not_run={counts['not_run']}")
    print(f"Report: {args.output.resolve() / 'summary.json'}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
