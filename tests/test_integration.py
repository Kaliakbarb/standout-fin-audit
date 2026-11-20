"""First vertical-slice integration, not full competition compliance.

These tests use cooperative subprocesses. They test wire/lifecycle behavior and
do not claim OS isolation; the Linux secure profile needs its separate probes.
"""

from __future__ import annotations

import copy
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from fin_audit import runner
from fin_audit.contracts import EpisodeBundle, canonical_json, strict_json_loads
from fin_audit.generator import generate
from fin_audit.runner import run_episode


@pytest.mark.parametrize("clean", [False, True])
def test_public_only_simple_worker_finds_stage_one_errors_and_respects_clean_books(clean):
    config = {"duplicate_count": 0, "allocation_count": 0} if clean else {}
    bundle = generate(37, config)
    report = run_episode(bundle, policy="simple", mode="closed", timeout=5)
    result = report["evaluation"]
    assert report["profile"] == "cooperative"
    assert report["agent_exitcode"] == 0
    assert result["status"] == "scored"
    assert (result["tp"], result["fp"], result["fn"]) == (len(bundle.truth), 0, 0)
    assert result["f1"] == 1.0
    assert result["total_steps"] == (8 if clean else 9)
    assert result["inspection_steps"] == result["total_steps"] - 1
    assert result["reward"] == (0.986 if clean else 0.984)
    assert all(row["reward"] == 0 for row in report["trajectory"])


@pytest.mark.parametrize("clean", [False, True])
def test_mark_all_is_a_valid_submission_with_thirty_false_accusations(clean):
    config = {"duplicate_count": 0, "allocation_count": 0} if clean else {}
    bundle = generate(37, config)
    report = run_episode(bundle, policy="mark_all", mode="closed", timeout=5)
    result = report["evaluation"]
    assert result["status"] == "scored"
    assert (result["tp"], result["fp"], result["fn"]) == (0, 30, len(bundle.truth))
    assert result["f1"] == 0.0
    assert result["reward"] == -1.0
    assert report["trajectory"][-1]["terminated"] is True
    assert report["trajectory"][-1]["truncated"] is False
    assert all(row["reward"] == 0 for row in report["trajectory"])


@pytest.mark.parametrize("clean", [False, True])
def test_evaluator_only_oracle_checks_upper_bound_without_inspections(clean):
    config = {"duplicate_count": 0, "allocation_count": 0} if clean else {}
    bundle = generate(38, config)
    report = run_episode(bundle, policy="oracle", mode="closed", timeout=5)
    assert report["profile"] == "trusted_oracle"
    assert report["agent_exitcode"] is None
    assert report["evaluation"]["reward"] == 1.0
    assert report["evaluation"]["inspection_steps"] == 0
    assert report["evaluation"]["total_steps"] == 1
    assert report["evaluation"]["tp"] == len(bundle.truth)
    assert len(report["trajectory"]) == 2
    assert all(row["reward"] == 0 for row in report["trajectory"])


@pytest.mark.parametrize("policy", ["simple", "silent", "random", "mark_all", "oracle"])
def test_train_and_closed_share_true_score_with_only_terminal_train_feedback(policy):
    bundle = generate(39)
    train = run_episode(bundle, policy=policy, mode="train", timeout=5)
    closed = run_episode(bundle, policy=policy, mode="closed", timeout=5)
    assert train["evaluation"] == closed["evaluation"]
    assert all(row["reward"] == 0 for row in train["trajectory"][:-1])
    assert train["trajectory"][-1]["reward"] == train["evaluation"]["reward"]
    assert all(row["reward"] == 0 for row in closed["trajectory"])
    assert train["trajectory"][-1]["terminated"] is True
    assert all(row["info"] == {} for row in train["trajectory"] + closed["trajectory"])


@pytest.mark.parametrize("policy", ["simple", "random", "mark_all", "oracle"])
def test_fresh_runs_have_exact_canonical_transcripts_and_scores(policy):
    first = run_episode(generate(40), policy=policy, mode="closed", timeout=5)
    second = run_episode(generate(40), policy=policy, mode="closed", timeout=5)
    assert canonical_json(first["trajectory"]) == canonical_json(second["trajectory"])
    assert first["evaluation"] == second["evaluation"]
    # Operational elapsed time is intentionally outside the replay comparison.
    assert "elapsed_seconds" in first


def _keys(value):
    result = set()
    if isinstance(value, dict):
        result.update(value)
        for child in value.values():
            result.update(_keys(child))
    elif isinstance(value, list):
        for child in value:
            result.update(_keys(child))
    return result


def test_no_private_truth_or_provenance_reaches_observation_wire():
    generated = generate(41)
    sentinel = "private-evaluator-sentinel-must-never-enter-wire"
    truth = copy.deepcopy(generated.truth)
    truth[0]["private_test_note"] = sentinel
    bundle = EpisodeBundle(generated.public, truth, {**generated.provenance, "sentinel": sentinel})
    report = run_episode(bundle, policy="simple", mode="closed", timeout=5)
    forbidden = {
        "seed", "truth", "provenance", "issue_id", "evidence_sets",
        "expected_amount_minor", "private_test_note", "sentinel",
    }
    for row in report["trajectory"]:
        observation = row["observation"]
        assert not forbidden.intersection(_keys(observation))
        encoded = canonical_json(observation)
        assert sentinel not in encoded
        assert all(issue["issue_id"] not in encoded for issue in truth)
        assert row["info"] == {}
    assert report["evaluation"]["f1"] == 1.0


@pytest.mark.parametrize(
    "text",
    [
        '{"action":"list_records","action":"submit"}',
        '{"amount":NaN}',
        '{"amount":Infinity}',
        '{"amount":-Infinity}',
        '{"amount":1e999}',
        '{"amount":-1e999}',
    ],
)
def test_wire_json_rejects_duplicate_keys_and_nonfinite_numbers(text):
    with pytest.raises(ValueError):
        strict_json_loads(text)


def test_json_tree_rejects_unpaired_surrogates_and_excessive_depth():
    for value in ({"id": "\ud800"}, {"\udfff": "value"}):
        with pytest.raises(UnicodeError):
            runner._check_json_tree(value)
    value = "leaf"
    for _ in range(34):
        value = [value]
    with pytest.raises(ValueError, match="json_too_deep"):
        runner._check_json_tree(value)
    runner._check_json_tree({"message": "Корректный текст", "values": [None, True, 1]})


def _replace_worker(monkeypatch, tmp_path: Path, body: str, *, read_initial: bool = True,
                    prelaunch: bool = False, request=None):
    """Use a real adversarial subprocess without altering production adapters."""
    if prelaunch and request is None:
        raise ValueError("Prelaunched fixtures require an owner finalizer")
    script = tmp_path / "wire_fixture.py"
    ready = tmp_path / "wire_fixture.ready"
    preparing = tmp_path / "wire_fixture.preparing"
    readiness = (
        f"with open({str(preparing)!r}, 'w') as receipt:\n    receipt.write('READY')\n"
        f"os.replace({str(preparing)!r}, {str(ready)!r})\n"
    ) if prelaunch else ""
    script.write_text(
        "import sys, os, time, json\n"
        + readiness
        + ("start = json.loads(sys.stdin.readline())\n" if read_initial else "")
        + body,
        encoding="utf-8",
    )
    original_popen = runner.subprocess.Popen
    launched = []

    if prelaunch:
        process_kwargs = {
            "stdin": runner.subprocess.PIPE, "stdout": runner.subprocess.PIPE, "stderr": runner.subprocess.PIPE,
            "close_fds": True, "start_new_session": True, "cwd": Path(runner.__file__).resolve().parent,
            "env": {"LANG": "C.UTF-8", "TZ": "UTC", "PYTHONHASHSEED": "0"},
        }
        process = original_popen([sys.executable, "-I", "-S", str(script)], **process_kwargs)
        launched.append(process)
        cleaned = False

        def cleanup():
            nonlocal cleaned
            if cleaned:
                return
            try:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
            cleaned = True

        # Own the child before waiting for readiness, including setup failure
        # and tests which never call the broker. Do not depend on runner._stop.
        try:
            request.addfinalizer(cleanup)
            deadline = time.monotonic() + 5
            while not ready.is_file():
                assert process.poll() is None, "Fixture exited before READY"
                assert time.monotonic() < deadline, "Fixture did not become READY"
                time.sleep(0.01)
            assert ready.read_text() == "READY"
            assert process.poll() is None, "Fixture exited before broker launch"
        except BaseException:
            cleanup()
            raise
        used = False

        def launch(_command, **kwargs):
            nonlocal used
            assert not used, "Prelaunched fixture permits only one broker launch"
            assert kwargs == process_kwargs, "Broker changed fixture pipe/session parameters"
            used = True
            return process
    else:
        def launch(_command, **kwargs):
            process = original_popen([sys.executable, "-I", "-S", str(script)], **kwargs)
            launched.append(process)
            return process

    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    return launched, original_popen


def test_prelaunched_fixture_is_cleaned_even_when_broker_is_never_called(monkeypatch, tmp_path, request):
    finalizers = []

    class Owner:
        def addfinalizer(self, callback):
            finalizers.append(callback)
            request.addfinalizer(callback)

    monkeypatch.setattr(runner, "_stop", lambda process: pytest.fail("Fixture cleanup must be independent"))
    launched, _ = _replace_worker(monkeypatch, tmp_path, "time.sleep(10)\n", prelaunch=True, request=Owner())
    process = launched[0]
    assert process.poll() is None and len(finalizers) == 1
    finalizers[0]()
    assert process.poll() is not None
    assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))
    finalizers[0]()  # Idempotent teardown must not signal a reused process ID.


def test_prelaunched_fixture_cleans_setup_failure_before_ready(monkeypatch, tmp_path, request):
    original_popen = runner.subprocess.Popen
    launched = []

    def early_exit(_command, **kwargs):
        process = original_popen([sys.executable, "-I", "-S", "-c", "import os; os._exit(17)"], **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(runner.subprocess, "Popen", early_exit)
    with pytest.raises(AssertionError, match="before READY"):
        _replace_worker(monkeypatch, tmp_path, "time.sleep(10)\n", prelaunch=True, request=request)
    assert len(launched) == 1 and launched[0].returncode is not None
    assert all(stream.closed for stream in (launched[0].stdin, launched[0].stdout, launched[0].stderr))


def test_prelaunched_fixture_rejects_a_second_broker_launch(monkeypatch, tmp_path, request):
    launched, _ = _replace_worker(monkeypatch, tmp_path, "print('not-json', flush=True)\n",
                                   prelaunch=True, request=request)
    bundle = generate(42)
    assert run_episode(bundle)["evaluation"]["termination_reason"] == "protocol_error"
    with pytest.raises(AssertionError, match="only one broker launch"):
        run_episode(bundle)
    assert len(launched) == 1


def test_prelaunched_fixture_rejects_changed_session_parameters(monkeypatch, tmp_path, request):
    launched, _ = _replace_worker(monkeypatch, tmp_path, "time.sleep(10)\n", prelaunch=True, request=request)
    with pytest.raises(AssertionError, match="pipe/session parameters"):
        runner.subprocess.Popen(["unused"], stdin=runner.subprocess.PIPE, stdout=runner.subprocess.PIPE,
                                stderr=runner.subprocess.PIPE, close_fds=True, start_new_session=False,
                                cwd=Path(runner.__file__).resolve().parent,
                                env={"LANG": "C.UTF-8", "TZ": "UTC", "PYTHONHASHSEED": "0"})
    assert len(launched) == 1 and launched[0].poll() is None


@pytest.mark.parametrize(
    "line",
    [
        "not-json\n",
        '{"action":"list_records","action":"submit"}\n',
        '{"action":"calculator","args":{"expression":1e999}}\n',
        '{"action":"get_document","args":{"doc_id":"\\ud800"}}\n',
    ],
)
def test_real_broker_bounds_malformed_frames(monkeypatch, tmp_path, line, request):
    _replace_worker(
        monkeypatch, tmp_path,
        f"sys.stdout.write({line!r})\nsys.stdout.flush()\ntime.sleep(10)\n",
        prelaunch=True, request=request,
    )
    report = run_episode(generate(42), timeout=1)
    assert report["evaluation"]["termination_reason"] == "protocol_error"
    assert report["evaluation"]["truncated"] is True
    assert report["evaluation"]["reward"] == -1.0
    assert report["trajectory"][-1]["observation"]["final_findings"] == []
    assert report["elapsed_seconds"] < 3


@pytest.mark.parametrize("stream,limit", [("stdout", runner.MAX_FRAME_BYTES), ("stderr", runner.MAX_STDERR_BYTES)])
def test_real_broker_bounds_output_flooding(monkeypatch, tmp_path, stream, limit, request):
    _replace_worker(
        monkeypatch, tmp_path,
        f"sys.{stream}.write('x' * {limit + 65536})\nsys.{stream}.flush()\ntime.sleep(10)\n",
        prelaunch=True, request=request,
    )
    report = run_episode(generate(43), timeout=1)
    assert report["evaluation"]["termination_reason"] == "protocol_error"
    assert report["evaluation"]["truncated"] is True
    assert len(report["stderr"].encode("utf-8")) <= runner.MAX_STDERR_BYTES
    assert report["elapsed_seconds"] < 3


def test_real_broker_stops_an_unresponsive_worker(monkeypatch, tmp_path):
    _replace_worker(monkeypatch, tmp_path, "time.sleep(10)\n")
    report = run_episode(generate(44), timeout=0.1)
    assert report["evaluation"]["termination_reason"] == "agent_timeout"
    assert report["evaluation"]["truncated"] is True
    assert report["trajectory"][-1]["observation"]["final_findings"] == []
    assert report["agent_exitcode"] is not None
    assert report["elapsed_seconds"] < 3


def test_real_broker_times_out_an_unfinished_partial_frame(monkeypatch, tmp_path):
    _replace_worker(
        monkeypatch, tmp_path,
        "sys.stdout.write('{')\nsys.stdout.flush()\ntime.sleep(10)\n",
    )
    report = run_episode(generate(45), timeout=0.1)
    assert report["evaluation"]["termination_reason"] == "agent_timeout"
    assert report["evaluation"]["truncated"] is True
    assert report["evaluation"]["total_steps"] == 0
    assert report["elapsed_seconds"] < 3


def test_recognized_invalid_submit_is_not_reclassified_as_a_transport_failure(monkeypatch, tmp_path, request):
    _replace_worker(
        monkeypatch, tmp_path,
        "payload = {'episode_id': start['observation']['manifest']['episode_id']}\n"
        "print(json.dumps({'action': 'submit', 'args': {'findings': payload}}), flush=True)\n"
        "time.sleep(10)\n",
        prelaunch=True, request=request,
    )
    report = run_episode(generate(46), timeout=2)
    assert report["evaluation"]["termination_reason"] == "invalid_submit"
    assert report["evaluation"]["status"] == "invalid_submit"
    assert report["evaluation"]["reward"] == -1.0
    assert report["evaluation"]["terminated"] is True
    assert report["evaluation"]["truncated"] is False
    assert report["elapsed_seconds"] < 2


def test_terminal_ack_grace_is_bounded_when_worker_refuses_to_exit(monkeypatch, tmp_path):
    _replace_worker(
        monkeypatch, tmp_path,
        "payload = {'episode_id': start['observation']['manifest']['episode_id'], 'findings': []}\n"
        "print(json.dumps({'action': 'submit', 'args': {'findings': payload}}), flush=True)\n"
        "time.sleep(10)\n",
    )
    bundle = generate(47, {"duplicate_count": 0, "allocation_count": 0})
    report = run_episode(bundle, timeout=2)
    assert report["evaluation"]["termination_reason"] == "submitted"
    assert report["evaluation"]["reward"] == 1.0
    assert report["evaluation"]["total_steps"] == 1
    assert report["agent_exitcode"] == -signal.SIGKILL
    assert report["elapsed_seconds"] < 2


@pytest.mark.parametrize("ack_delay", [0.3, 0.6])
def test_worker_gets_exit_grace_after_terminal_ack_is_fully_written(monkeypatch, tmp_path, ack_delay):
    launched, _ = _replace_worker(
        monkeypatch, tmp_path,
        "payload = {'episode_id': start['observation']['manifest']['episode_id'], 'findings': []}\n"
        "print(json.dumps({'action': 'submit', 'args': {'findings': payload}}), flush=True)\n"
        "ack = json.loads(sys.stdin.readline())\n"
        "assert ack['terminated'] is True\n"
        "time.sleep(0.3)\nos._exit(0)\n",
    )
    original_write = os.write
    delayed = []

    def delayed_terminal_write(fd, data):
        if launched and fd == launched[0].stdin.fileno() and b'"terminated":true' in data:
            delayed.append(True)
            time.sleep(ack_delay)
        return original_write(fd, data)

    monkeypatch.setattr(runner.os, "write", delayed_terminal_write)
    report = run_episode(generate(47, {"duplicate_count": 0, "allocation_count": 0}))
    assert delayed == [True]
    assert report["agent_exitcode"] == 0
    assert report["evaluation"]["termination_reason"] == "submitted"
    assert report["evaluation"]["reward"] == 1.0


def test_nonreading_terminal_worker_is_still_bounded_by_episode_deadline(monkeypatch, tmp_path):
    _replace_worker(
        monkeypatch, tmp_path,
        "payload = {'episode_id': start['observation']['manifest']['episode_id'], 'findings': []}\n"
        "print(json.dumps({'action': 'submit', 'args': {'findings': payload}}), flush=True)\n"
        "time.sleep(10)\n",
    )
    original_wire = runner._wire

    def large_terminal_response(transition):
        response = original_wire(transition)
        if response["terminated"] or response["truncated"]:
            # Exceed pipe capacity so not reading really prevents ACK flush.
            response["observation"]["padding"] = "x" * (runner.MAX_FRAME_BYTES // 2)
        return response

    monkeypatch.setattr(runner, "_wire", large_terminal_response)
    report = run_episode(generate(47, {"duplicate_count": 0, "allocation_count": 0}), timeout=2)
    assert report["evaluation"]["termination_reason"] == "submitted"
    assert report["evaluation"]["reward"] == 1.0
    assert report["agent_exitcode"] == -signal.SIGKILL
    assert report["elapsed_seconds"] < 3


@pytest.mark.parametrize("exit_code", [7, None])
def test_worker_exit_status_after_reading_terminal_ack_is_preserved(monkeypatch, tmp_path, exit_code):
    _replace_worker(
        monkeypatch, tmp_path,
        "payload = {'episode_id': start['observation']['manifest']['episode_id'], 'findings': []}\n"
        "print(json.dumps({'action': 'submit', 'args': {'findings': payload}}), flush=True)\n"
        "ack = json.loads(sys.stdin.readline())\n"
        "assert ack['terminated'] is True\n"
        + ("time.sleep(10)\n" if exit_code is None else f"os._exit({exit_code})\n"),
    )
    report = run_episode(generate(47, {"duplicate_count": 0, "allocation_count": 0}))
    assert report["evaluation"]["termination_reason"] == "submitted"
    assert report["agent_exitcode"] == (-signal.SIGKILL if exit_code is None else exit_code)


def test_terminal_exit_grace_cannot_extend_the_fixed_episode_deadline(monkeypatch, tmp_path):
    clock = [0.0]
    original_env = runner.AuditEnv
    original_selector = runner.selectors.DefaultSelector

    class ClockedEnv(original_env):
        def __init__(self, **kwargs):
            super().__init__(clock=lambda: clock[0], **kwargs)

    class AdvancingSelector:
        def __init__(self): self.selector = original_selector()
        def register(self, *args): return self.selector.register(*args)
        def unregister(self, *args): return self.selector.unregister(*args)
        def close(self): self.selector.close()
        def select(self, wait):
            events = self.selector.select(wait)
            if delivered:
                clock[0] += wait
            return events

    launched, _ = _replace_worker(
        monkeypatch, tmp_path,
        "payload = {'episode_id': start['observation']['manifest']['episode_id'], 'findings': []}\n"
        "print(json.dumps({'action': 'submit', 'args': {'findings': payload}}), flush=True)\n"
        "json.loads(sys.stdin.readline())\ntime.sleep(10)\n",
    )
    original_write = os.write
    bundle = generate(47, {"duplicate_count": 0, "allocation_count": 0})
    delivered = []

    def late_terminal_write(fd, data):
        if launched and fd == launched[0].stdin.fileno() and b'"terminated":true' in data:
            clock[0] = 4.9
            delivered.append(True)
        return original_write(fd, data)

    monkeypatch.setattr(runner.os, "write", late_terminal_write)
    monkeypatch.setattr(runner, "AuditEnv", ClockedEnv)
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runner.selectors, "DefaultSelector", AdvancingSelector)
    report = run_episode(bundle, timeout=5)
    assert delivered == [True]
    assert report["evaluation"]["termination_reason"] == "submitted"
    assert report["agent_exitcode"] == -signal.SIGKILL
    assert report["elapsed_seconds"] == pytest.approx(5)


@pytest.mark.parametrize("reply,fast_exit", [
    ({"terminated": True}, True),
    ({"truncated": True}, True),
    ({"observation": {"result": {}}}, False),
    (None, False),
    ("malformed", False),
    ({"observation": {"error": {"code": "fixture_error"}}}, False),
])
def test_reference_worker_fast_exit_requires_a_terminal_ack(tmp_path, reply, fast_exit):
    worker = Path(runner.__file__).with_name("agent_worker.py")
    code = (
        "import atexit, runpy, sys\n"
        "atexit.register(lambda: print('PYTHON_FINALIZER', file=sys.stderr, flush=True))\n"
        "worker = runpy.run_path(sys.argv[1])\n"
        "worker['send']('submit', {})\n"
        "print('AFTER_SEND', flush=True)\n"
    )
    data = "" if reply is None else (reply if isinstance(reply, str) else json.dumps(reply)) + "\n"
    completed = runner.subprocess.run([sys.executable, "-I", "-S", "-c", code, str(worker)],
                                      input=data, text=True, capture_output=True, timeout=60)
    assert json.loads(completed.stdout.splitlines()[0])["action"] == "submit"
    assert ("PYTHON_FINALIZER" not in completed.stderr) is fast_exit
    if fast_exit:
        assert completed.returncode == 0
        assert "AFTER_SEND" not in completed.stdout
    elif reply == {"observation": {"result": {}}}:
        assert completed.returncode == 0 and "AFTER_SEND" in completed.stdout
    else:
        assert completed.returncode != 0


def test_reference_worker_does_not_report_fast_success_when_terminal_flush_fails():
    worker = Path(runner.__file__).with_name("agent_worker.py")
    code = (
        "import runpy, sys\n"
        "class FailingFlush:\n"
        "    def write(self, value): return sys.__stderr__.write(value)\n"
        "    def flush(self): raise OSError('terminal_flush_failed')\n"
        "sys.stderr = FailingFlush()\n"
        "worker = runpy.run_path(sys.argv[1])\n"
        "worker['send']('submit', {})\n"
    )
    completed = runner.subprocess.run([sys.executable, "-I", "-S", "-c", code, str(worker)],
                                      input='{"terminated":true}\n', text=True, capture_output=True, timeout=60)
    assert completed.returncode != 0
    assert "terminal_flush_failed" in completed.stderr


@pytest.mark.skipif(not hasattr(os, "fork"), reason="supported runner requires POSIX process groups")
@pytest.mark.parametrize("startup_delay", [0.0, 0.15])
def test_cleanup_kills_descendant_even_when_worker_leader_already_exited(monkeypatch, tmp_path, startup_delay):
    ready = tmp_path / "descendant-ready"
    pending = tmp_path / "descendant-ready.pending"
    launched, original_popen = _replace_worker(
        monkeypatch, tmp_path,
        "child = os.fork()\n"
        "if child == 0:\n    while True:\n        time.sleep(60)\n"
        f"with open({str(pending)!r}, 'w') as receipt:\n    receipt.write(str(child))\n"
        f"os.replace({str(pending)!r}, {str(ready)!r})\n"
        "os._exit(0)\n",
        read_initial=False,
    )

    def still_running(pid):
        if sys.platform == "linux":
            try:
                stat = Path(f"/proc/{pid}/stat").read_text()
            except FileNotFoundError:
                return False
            return stat[stat.rfind(")") + 2:].split()[0] != "Z"
        process = original_popen(
            ["ps", "-o", "stat=", "-p", str(pid)],
            stdout=runner.subprocess.PIPE, stderr=runner.subprocess.DEVNULL,
        )
        output, _ = process.communicate(timeout=1)
        state = output.decode().strip()
        return bool(state) and not state.startswith("Z")

    fixture_popen = runner.subprocess.Popen

    def launch_after_leader_exit(command, **kwargs):
        if startup_delay:
            time.sleep(startup_delay)
        process = fixture_popen(command, **kwargs)
        deadline = time.monotonic() + 5
        while (not ready.is_file() or process.poll() is None) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.is_file(), "fixture did not publish its descendant PID"
        assert process.poll() == 0, "fixture leader did not exit successfully"
        child_pid = int(ready.read_text())
        assert os.getpgid(child_pid) == process.pid
        assert still_running(child_pid), "fixture descendant was not alive before cleanup"
        return process

    # Establish the lifecycle precondition before the broker receives Popen.
    # Even when startup exhausts its short test budget, cleanup must kill the
    # live process group after its leader has exited. The child cannot time out
    # naturally and make a broken cleanup appear successful.
    monkeypatch.setattr(runner.subprocess, "Popen", launch_after_leader_exit)

    try:
        report = run_episode(generate(48), timeout=0.1)
        child_pid = int(ready.read_text())
        deadline = time.monotonic() + 0.5
        while still_running(child_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not still_running(child_pid)
        assert report["evaluation"]["truncated"] is True
        assert report["agent_exitcode"] == 0
    finally:
        # Also clean up the fixture if a regression leaves its descendant alive.
        for process in launched:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_cli_renders_real_invalid_submit_metrics_without_crashing(capsys, tmp_path):
    from fin_audit.cli import _print_summary
    from fin_audit.environment import AuditEnv

    env = AuditEnv()
    env.load_bundle(generate(49))
    env.step({"action": "submit", "args": {"findings": None}})
    result = env.evaluation_result()
    assert result["status"] == "invalid_submit"
    assert result["f1"] is None
    summary = {
        "passed": False, "profile": "cooperative",
        "runs": [{"case": "issues", "policy": "invalid", "evaluation": result}],
        "checks": {"valid_submission": False}, "secure_isolation_verified": False,
    }
    _print_summary(summary, tmp_path)
    printed = capsys.readouterr().out
    assert "N/A" in printed
    assert "invalid_submit" in printed
    assert "FAIL" in printed
