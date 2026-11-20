#!/usr/bin/env python3
"""Container entry point: tests, acceptance, and final cgroup measurements."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def cgroup_metrics(directory: Path = Path("/sys/fs/cgroup")) -> dict:
    """Read kernel counters; unsupported files stay explicitly unavailable."""
    names = ("memory.peak", "memory.current", "memory.max", "memory.swap.max",
             "memory.events", "cpu.max", "cpu.stat", "pids.current", "pids.peak", "pids.max", "pids.events")
    values, unavailable = {}, []
    for name in names:
        try:
            raw = (directory / name).read_text(encoding="ascii").strip()
        except OSError:
            unavailable.append(name)
            continue
        if name.endswith((".events", ".stat")):
            values[name] = {key: int(value) for key, value in (line.split() for line in raw.splitlines())}
        elif raw.isdecimal():
            values[name] = int(raw)
        else:
            values[name] = raw
    return {"scope": "Current container cgroup, all test/acceptance child processes included; daemon/VM excluded.",
            "source": str(directory), "cgroup_v2": (directory / "cgroup.controllers").is_file(),
            "values": values, "unavailable": unavailable}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=int, choices=[1, 2, 3, 4, 5], default=5)
    parser.add_argument("--output", type=Path, default=Path("/output"))
    parser.add_argument("--master-seed", type=int)
    args = parser.parse_args(argv)
    output = args.output.absolute()
    started = time.monotonic()
    tests_code, app_code = None, None
    result_code = 1
    try:
        tests = [sys.executable, "-m", "pytest", "-q", "-o", "cache_dir=/tmp/pytest-cache",
                 "--basetemp=/tmp/pytest", f"--junitxml={output / 'tests.xml'}"]
        tests_code = subprocess.run(tests, check=False).returncode
        if tests_code:
            result_code = tests_code
        else:
            command = [sys.executable, "-m", "fin_audit.cli", "verify", "--stage", str(args.stage),
                       "--profile", "secure", "--output", str(output / "run")]
            if args.master_seed is not None:
                command.extend(["--master-seed", str(args.master_seed)])
            app_code = subprocess.run(command, check=False).returncode
            result_code = app_code
    finally:
        metrics = cgroup_metrics()
        metrics.update(stage=args.stage, pytest_exitcode=tests_code, application_exitcode=app_code,
                       passed=tests_code == 0 and app_code == 0, elapsed_seconds=time.monotonic() - started)
        descriptor = os.open(output / "cgroup-metrics.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(metrics, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
    return result_code


if __name__ == "__main__":
    raise SystemExit(main())
