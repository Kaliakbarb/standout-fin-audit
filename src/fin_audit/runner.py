"""Bounded JSONL broker between the trusted environment and a fresh worker."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .contracts import PROTOCOL_VERSION, EpisodeBundle, canonical_json, strict_json_loads
from .environment import AuditEnv

MAX_FRAME_BYTES = 256 * 1024
MAX_WIRE_BYTES = 8 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024


def _check_json_tree(value: Any) -> None:
    queue = [(value, 0)]
    while queue:
        item, depth = queue.pop()
        if depth > 32:
            raise ValueError("json_too_deep")
        if isinstance(item, str):
            item.encode("utf-8", errors="strict")
        elif isinstance(item, dict):
            for key, child in item.items():
                key.encode("utf-8", errors="strict")
                queue.append((child, depth + 1))
        elif isinstance(item, list):
            queue.extend((child, depth + 1) for child in item)


def oracle_action(bundle: EpisodeBundle) -> dict[str, Any]:
    """Evaluator-only reference. Never send these private inputs to a worker."""
    return {
        "action": "submit",
        "args": {"findings": {
            "episode_id": bundle.public["manifest"]["episode_id"],
            "findings": [{
                "type": issue["type"], "affected_ids": list(issue["affected_ids"]),
                "claimed_amount_minor": issue["expected_amount_minor"],
                "currency": issue["currency"], "evidence_ids": list(issue["evidence_sets"][0]),
                "explanation": "Evaluator-only upper-bound reference with a complete proof.",
            } for issue in bundle.truth],
        }},
    }


def _wire(transition: tuple) -> dict[str, Any]:
    obs, reward, terminated, truncated, info = transition
    return {"observation": obs, "reward": reward, "terminated": terminated,
            "truncated": truncated, "info": info}


def _record(episode_id: str, step: int, action: Any, response: dict) -> dict:
    return {"episode_id": episode_id, "step": step, "action": action, **response}


def _stop(process: subprocess.Popen) -> None:
    # A cooperative leader may have exited while descendants still hold pipes.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=3)


def run_episode(
    bundle: EpisodeBundle,
    *,
    policy: str = "simple",
    mode: str = "closed",
    profile: str = "cooperative",
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Run one policy. The returned report is trusted, post-run information.

    Cooperative workers are separate processes but have normal host filesystem
    access; only the prepared Linux secure profile claims filesystem isolation.
    """
    if policy not in {"simple", "silent", "random", "mark_all", "oracle", "probe"}:
        raise ValueError("invalid_policy")
    if profile not in {"secure", "cooperative"}:
        raise ValueError("invalid_profile")
    if profile == "secure" and (
        sys.platform != "linux" or os.geteuid() != 0
        or not Path("/sandbox/agent_worker.py").is_file()
    ):
        raise RuntimeError("secure_runtime_unavailable")
    if policy == "probe" and profile != "secure":
        raise RuntimeError("isolation_probe_requires_secure_runtime")
    started = time.monotonic()
    env = AuditEnv(mode=mode, time_limit_seconds=timeout)
    initial, info = env.load_bundle(bundle)
    # Terminal environments expose zero remaining time. Capture the fixed
    # episode deadline now so delivery/finalization cannot extend its budget.
    episode_deadline = time.monotonic() + env.remaining_time_seconds
    episode_id = initial["manifest"]["episode_id"]
    trajectory = [_record(episode_id, 0, None, {
        "observation": initial, "reward": 0.0, "terminated": False,
        "truncated": False, "info": info,
    })]
    if policy == "oracle":
        action = oracle_action(bundle)
        trajectory.append(_record(episode_id, 1, action, _wire(env.step(action))))
        return {"episode_id": episode_id, "policy": policy, "profile": "trusted_oracle",
                "mode": mode, "trajectory": trajectory, "evaluation": env.evaluation_result(),
                "stderr": "", "agent_exitcode": None, "elapsed_seconds": time.monotonic() - started}

    folder = Path(__file__).resolve().parent
    if profile == "secure":
        command = [sys.executable, "-I", "-S", str(folder / "sandbox_launcher.py"), policy]
    else:
        command = [sys.executable, "-I", "-S", str(folder / "agent_worker.py"), "--policy", policy]
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        close_fds=True, start_new_session=True, cwd="/" if profile == "secure" else folder,
        env={"LANG": "C.UTF-8", "TZ": "UTC", "PYTHONHASHSEED": "0"},
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    for stream in (process.stdin, process.stdout, process.stderr):
        os.set_blocking(stream.fileno(), False)
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    pending = bytearray((canonical_json({
        "protocol_version": PROTOCOL_VERSION, "observation": initial, "info": info,
    }) + "\n").encode())
    stdout = bytearray()
    stderr = bytearray()
    wire_bytes = 0
    terminal = False
    writing = False
    terminal_grace: float | None = None

    def abort(reason: str) -> None:
        nonlocal terminal, terminal_grace, pending
        if not terminal:
            response = _wire(env.abort(reason))
            trajectory.append(_record(episode_id, env.total_steps, None, response))
            terminal = True
        pending = bytearray()
        terminal_grace = time.monotonic()

    try:
        while True:
            if time.monotonic() >= episode_deadline:
                if not terminal:
                    abort("agent_timeout")
                break
            if terminal and not pending:
                if process.poll() is not None:
                    # Drain already produced stderr, including the probe receipt.
                    try:
                        chunk = os.read(process.stderr.fileno(), MAX_STDERR_BYTES + 1)
                    except BlockingIOError:
                        chunk = b""
                    stderr.extend(chunk)
                    break
                if terminal_grace is None:
                    terminal_grace = min(time.monotonic() + 0.5, episode_deadline)
                if time.monotonic() >= terminal_grace:
                    break
            if not terminal and env.remaining_time_seconds <= 0:
                abort("agent_timeout")
                break
            if pending and not writing:
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                writing = True
            if not pending and writing:
                selector.unregister(process.stdin)
                writing = False
            wait = 0.05 if terminal else min(0.2, env.remaining_time_seconds)
            next_deadline = min(episode_deadline, terminal_grace) if terminal_grace is not None else episode_deadline
            wait = min(wait, max(0.0, next_deadline - time.monotonic()))
            events = selector.select(wait)
            for key, _ in events:
                if key.data == "stdin":
                    try:
                        count = os.write(process.stdin.fileno(), pending[:65536])
                        del pending[:count]
                        if terminal and not pending and count > 0:
                            terminal_grace = min(time.monotonic() + 0.5, episode_deadline)
                    except (BrokenPipeError, OSError):
                        abort("agent_failed")
                    continue
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    if key.data == "stdout" and not terminal:
                        abort("agent_failed")
                    continue
                if key.data == "stderr":
                    stderr.extend(chunk)
                    if len(stderr) > MAX_STDERR_BYTES:
                        stderr = stderr[:MAX_STDERR_BYTES]
                        abort("protocol_error")
                    continue
                wire_bytes += len(chunk)
                stdout.extend(chunk)
                if wire_bytes > MAX_WIRE_BYTES or (b"\n" not in stdout and len(stdout) > MAX_FRAME_BYTES):
                    abort("protocol_error")
                    continue
                while b"\n" in stdout and not terminal:
                    line, _, remaining = stdout.partition(b"\n")
                    stdout = bytearray(remaining)
                    if len(line) > MAX_FRAME_BYTES:
                        abort("protocol_error")
                        break
                    try:
                        action = strict_json_loads(line.decode("utf-8", errors="strict"))
                        _check_json_tree(action)
                    except (ValueError, UnicodeError, RecursionError):
                        abort("protocol_error")
                        break
                    response = _wire(env.step(action))
                    trajectory.append(_record(episode_id, env.total_steps, action, response))
                    pending.extend((canonical_json(response) + "\n").encode())
                    terminal = response["terminated"] or response["truncated"]
            if terminal and terminal_grace is not None and terminal_grace <= time.monotonic():
                break
    finally:
        selector.close()
        _stop(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
    evaluation = env.evaluation_result()
    if evaluation is None:
        raise RuntimeError("missing_terminal_evaluation")
    return {"episode_id": episode_id, "policy": policy, "profile": profile, "mode": mode,
            "trajectory": trajectory, "evaluation": evaluation,
            "stderr": stderr[:MAX_STDERR_BYTES].decode("utf-8", errors="replace"),
            "agent_exitcode": process.returncode, "elapsed_seconds": time.monotonic() - started}
