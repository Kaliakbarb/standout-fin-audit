"""Fail-closed checks for the host-only clean offline rehearsal utility."""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("clean_rehearsal", ROOT / "scripts/clean_rehearsal.py")
rehearsal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rehearsal)


def test_environment_never_inherits_context_auth_or_tls(monkeypatch, tmp_path):
    for name in ("DOCKER_CONTEXT", "FIN_AUDIT_DOCKER_CONTEXT", "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_API_VERSION"):
        monkeypatch.setenv(name, "wrong-existing-engine")
    env = rehearsal.isolated_environment(tmp_path / "socket", tmp_path / "config")
    assert env["DOCKER_HOST"] == f"unix://{tmp_path}/socket"
    assert env["DOCKER_CONFIG"] == str(tmp_path / "config")
    assert all(name not in env for name in ("DOCKER_CONTEXT", "FIN_AUDIT_DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_API_VERSION"))


def isolated_daemon(storage_driver="vfs"):
    daemon = rehearsal.IsolatedDaemon(storage_driver=storage_driver)
    daemon.initial_volumes = []
    daemon.paths = {"root": "/var/lib/fin-audit-clean-own", "run": "/run/fin-audit-clean-own"}
    daemon.initial = {"DockerRootDir": daemon.paths["root"] + "/data", "Images": 0, "Containers": 0,
                      "Driver": storage_driver, "Containerd": {"Address": daemon.paths["run"] + "/containerd.sock"}}
    daemon.isolation = {"netns": "net:[2]", "host_netns": "net:[1]", "addresses": [{"ifname": "lo"}], "routes": [],
                        "containerd": [{"root": daemon.paths["root"] + "/containerd", "state": daemon.paths["run"] + "/containerd",
                                        "grpc_address": daemon.paths["run"] + "/containerd.sock", "config_path": daemon.paths["root"] + "/containerd.toml",
                                        "netns": "net:[2]"}]}
    return daemon


@pytest.mark.parametrize("case", ["warm", "container", "volume", "existing_root", "host_network", "interface", "route", "global_containerd", "global_state", "address", "child_network"])
def test_daemon_rejects_unsafe_or_warm_state(case):
    daemon = isolated_daemon()
    daemon.validate_isolation()
    if case == "warm": daemon.initial["Images"] = 1
    elif case == "container": daemon.initial["Containers"] = 1
    elif case == "volume": daemon.initial_volumes = ["b" * 64]
    elif case == "existing_root": daemon.initial["DockerRootDir"] = "/var/lib/docker"
    elif case == "host_network": daemon.isolation["netns"] = "net:[1]"
    elif case == "interface": daemon.isolation["addresses"].append({"ifname": "eth0"})
    elif case == "route": daemon.isolation["routes"] = [{"dst": "default"}]
    elif case == "global_containerd": daemon.isolation["containerd"][0]["root"] = "/var/lib/containerd"
    elif case == "global_state": daemon.isolation["containerd"][0]["state"] = "/run/containerd"
    elif case == "address": daemon.initial["Containerd"]["Address"] = "/run/containerd/containerd.sock"
    else: daemon.isolation["containerd"][0]["netns"] = "net:[1]"
    with pytest.raises(rehearsal.RehearsalError):
        daemon.validate_isolation()


def readiness_setup(monkeypatch, tmp_path, storage_driver="vfs"):
    """Run the real enter/exit lifecycle with no external commands or sleeps."""
    daemon = isolated_daemon(storage_driver)
    daemon.paths["socket"] = daemon.paths["run"] + "/docker.sock"
    initial, isolation, paths = daemon.initial, daemon.isolation, daemon.paths
    baseline = {"daemon_id": "original", "data_root": "/original", "containerd_address": "/original.sock",
                "global_containerd": [], "images": ["original-image"], "volumes": ["original-volume"]}
    remote_calls = []

    def remote(source, *args, **kwargs):
        remote_calls.append(source)
        if source == rehearsal._REMOTE_BASELINE: return copy.deepcopy(baseline)
        if source == rehearsal._REMOTE_START:
            assert args[0] == {"tag": daemon.tag, "storage_driver": storage_driver}
            return paths
        if source == rehearsal._REMOTE_INSPECT: return isolation
        if source == rehearsal._REMOTE_STOP: return {"removed_owned_roots": [paths["root"], paths["run"]]}
        pytest.fail("unexpected remote command")

    class Forward:
        status = None
        terminated = False

        def poll(self): return self.status
        def terminate(self): self.terminated = True
        def wait(self, **kwargs): return 0

    forward, clock = Forward(), [0.0]
    real_mkdtemp = rehearsal.tempfile.mkdtemp
    monkeypatch.setattr(daemon, "remote", remote)
    monkeypatch.setattr(rehearsal.tempfile, "mkdtemp", lambda **kwargs: real_mkdtemp(prefix=kwargs["prefix"], dir=tmp_path))
    monkeypatch.setattr(rehearsal.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="Host private-test\n", returncode=0))
    monkeypatch.setattr(rehearsal.subprocess, "Popen", lambda *args, **kwargs: forward)
    monkeypatch.setattr(rehearsal.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(rehearsal.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    return daemon, initial, forward, clock, remote_calls


def assert_readiness_cleanup(daemon, forward, remote_calls):
    assert remote_calls.count(rehearsal._REMOTE_STOP) == 1
    assert daemon.cleanup["existing_engine_preserved"] is True
    assert forward.terminated and daemon.forward_log.closed
    assert not daemon.temporary.exists()


@pytest.mark.parametrize("storage_driver", ["vfs", "overlay2"])
def test_readiness_retries_one_docker_info_timeout_then_succeeds(monkeypatch, tmp_path, storage_driver):
    daemon, initial, forward, clock, remote_calls = readiness_setup(monkeypatch, tmp_path, storage_driver)
    attempts = []

    def docker(*args, timeout=30):
        if args == ("volume", "ls", "--quiet"):
            return SimpleNamespace(returncode=0, stdout="")
        assert args == ("info", "--format", "{{json .}}")
        attempts.append(timeout)
        if len(attempts) == 1:
            clock[0] += timeout
            raise rehearsal.subprocess.TimeoutExpired(["docker", "info"], timeout)
        return SimpleNamespace(returncode=0, stdout=json.dumps(initial))

    monkeypatch.setattr(daemon, "docker", docker)
    with daemon as ready:
        assert ready.initial == initial and attempts == [5, 5]
        assert clock[0] == pytest.approx(5.2) and not forward.terminated
    assert_readiness_cleanup(daemon, forward, remote_calls)


@pytest.mark.parametrize("storage_driver", ["vfs", "overlay2"])
def test_private_daemon_requires_the_requested_storage_driver(storage_driver):
    daemon = isolated_daemon(storage_driver)
    daemon.validate_isolation()
    daemon.initial["Driver"] = "overlay2" if storage_driver == "vfs" else "vfs"
    with pytest.raises(rehearsal.RehearsalError, match="daemon_storage_driver_mismatch"):
        daemon.validate_isolation()


@pytest.mark.parametrize("storage_driver", ["overlay", "overlay2; touch /tmp/unowned", None])
def test_unknown_storage_driver_is_rejected_before_daemon_creation(storage_driver):
    with pytest.raises(ValueError, match="unsupported_storage_driver"):
        rehearsal.IsolatedDaemon(storage_driver=storage_driver)


@pytest.mark.parametrize("storage_driver", ["vfs", "overlay2", "overlay2; touch /tmp/unowned"])
def test_remote_start_revalidates_storage_driver_before_filesystem_access(storage_driver):
    # Execute the actual remote validation prefix, stopping before root/run
    # paths are constructed. An altered remote argument must fail independently
    # of the host constructor, without touching any VM or host paths.
    source = ast.parse(rehearsal._REMOTE_START)
    prefix = []
    for node in source.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "root" for target in node.targets):
            break
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            prefix.append(node)
    namespace = {"json": json, "sys": SimpleNamespace(argv=["remote-start", json.dumps({"tag": "a" * 32,
                                                                                       "storage_driver": storage_driver})])}
    validation = compile(ast.Module(body=prefix, type_ignores=[]), "remote-start-validation", "exec")
    if storage_driver in {"vfs", "overlay2"}:
        exec(validation, namespace)
        assert namespace["storage_driver"] == storage_driver
    else:
        with pytest.raises(RuntimeError, match="unsupported_storage_driver"):
            exec(validation, namespace)


@pytest.mark.parametrize("option,expected", [([], "vfs"), (["--storage-driver", "overlay2"], "overlay2")])
def test_clean_cli_keeps_vfs_default_and_passes_explicit_storage_driver(monkeypatch, tmp_path, option, expected):
    calls = []
    def run(bundle, output, **kwargs):
        calls.append(kwargs)
        return output
    monkeypatch.setattr(rehearsal, "run_clean_rehearsal", run)
    assert rehearsal.main([str(tmp_path / "bundle"), "--output", str(tmp_path / "output"), *option]) == 0
    assert calls[0]["storage_driver"] == expected


def test_clean_cli_rejects_unknown_storage_driver_before_execution(monkeypatch, tmp_path):
    monkeypatch.setattr(rehearsal, "run_clean_rehearsal", lambda *args, **kwargs: pytest.fail("must not execute"))
    with pytest.raises(SystemExit) as error:
        rehearsal.main([str(tmp_path / "bundle"), "--output", str(tmp_path / "output"), "--storage-driver", "overlay"])
    assert error.value.code == 2


def test_readiness_timeouts_exhaust_only_the_remaining_budget_and_cleanup(monkeypatch, tmp_path):
    daemon, _, forward, clock, remote_calls = readiness_setup(monkeypatch, tmp_path)
    attempts = []

    def docker(*args, timeout):
        assert args == ("info", "--format", "{{json .}}")
        attempts.append(timeout)
        clock[0] += timeout
        raise rehearsal.subprocess.TimeoutExpired(["docker", "info"], timeout)

    monkeypatch.setattr(daemon, "docker", docker)
    with pytest.raises(rehearsal.RehearsalError, match="isolated_daemon_start_timeout"):
        daemon.__enter__()
    assert len(attempts) == 8 and all(0 < timeout <= 5 for timeout in attempts)
    assert attempts[-1] == pytest.approx(3.6) and clock[0] == pytest.approx(40)
    assert rehearsal._REMOTE_INSPECT not in remote_calls
    assert_readiness_cleanup(daemon, forward, remote_calls)


def test_readiness_docker_timeout_does_not_hide_a_failed_ssh_forward(monkeypatch, tmp_path):
    daemon, _, forward, clock, remote_calls = readiness_setup(monkeypatch, tmp_path)
    attempts = []

    def docker(*args, timeout):
        attempts.append(timeout)
        clock[0] += timeout
        forward.status = 255
        raise rehearsal.subprocess.TimeoutExpired(["docker", "info"], timeout)

    monkeypatch.setattr(daemon, "docker", docker)
    with pytest.raises(rehearsal.RehearsalError, match="ssh_forward_failed"):
        daemon.__enter__()
    assert attempts == [5] and clock[0] == 5
    assert_readiness_cleanup(daemon, forward, remote_calls)


def container():
    return {"Id": "own-container", "Image": "sha256:exact", "HostConfig": {
        "NetworkMode": "none", "ReadonlyRootfs": True, "Memory": 4 * 1024**3, "MemorySwap": 4 * 1024**3,
        "NanoCpus": 2_000_000_000, "PidsLimit": 64, "CapDrop": ["ALL"],
        "CapAdd": ["SYS_CHROOT", "SETUID", "SETGID", "KILL"], "SecurityOpt": ["no-new-privileges=true"],
        "Mounts": [{"Type": "volume", "Target": "/output", "VolumeOptions": {"NoCopy": True}}],
        "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=256m"}},
        "Mounts": [{"Type": "volume", "Name": "b" * 64, "Destination": "/output", "Driver": "local", "RW": True,
                    "Source": "/private-daemon/data/volumes/" + "b" * 64 + "/_data"}]}


@pytest.mark.parametrize("prefix", ["", "CAP_"])
def test_docker_capability_prefix_preserves_exact_semantic_set(prefix):
    actual = container()
    actual["HostConfig"]["CapAdd"] = [prefix + name for name in ("KILL", "SETGID", "SETUID", "SYS_CHROOT")]
    assert rehearsal.validate_container(actual, "sha256:exact")["Id"] == "own-container"
    actual["HostConfig"]["CapAdd"].append(prefix + "SYS_ADMIN")
    with pytest.raises(rehearsal.RehearsalError, match="capabilities"):
        rehearsal.validate_container(actual, "sha256:exact")


@pytest.mark.parametrize("field,value", [("NetworkMode", "bridge"), ("ReadonlyRootfs", False), ("Memory", 8 * 1024**3),
                                        ("NanoCpus", 0), ("PidsLimit", -1), ("CapAdd", ["SYS_ADMIN"]),
                                        ("SecurityOpt", []), ("Privileged", True)])
def test_actual_container_must_have_exact_supported_boundaries(field, value):
    actual = container()
    assert rehearsal.validate_container(actual, "sha256:exact")["Id"] == "own-container"
    actual["HostConfig"][field] = value
    with pytest.raises(rehearsal.RehearsalError):
        rehearsal.validate_container(actual, "sha256:exact")


@pytest.mark.parametrize("case", ["bind", "named_source", "volumes_from", "extra_declaration", "readonly_declaration",
                                  "driver_options", "copy_from_image", "non_boolean_nocopy", "unbounded_tmpfs", "extra_mount", "host_mount", "wrong_destination",
                                  "readonly_output", "named_output", "remote_driver", "missing_mounts"])
def test_only_anonymous_local_output_volume_and_bounded_tmpfs_are_allowed(case):
    actual = container()
    config = actual["HostConfig"]
    if case == "bind": config["Binds"] = ["/host:/output:rw"]
    elif case == "named_source": config["Mounts"][0]["Source"] = "existing-volume"
    elif case == "volumes_from": config["VolumesFrom"] = ["another-container"]
    elif case == "extra_declaration": config["Mounts"].append({"Type": "volume", "Target": "/extra"})
    elif case == "readonly_declaration": config["Mounts"][0]["ReadOnly"] = True
    elif case == "driver_options": config["Mounts"][0]["VolumeOptions"] = {"DriverConfig": {"Name": "local", "Options": {"device": "/host"}}}
    elif case == "copy_from_image": config["Mounts"][0]["VolumeOptions"] = {"NoCopy": False}
    elif case == "non_boolean_nocopy": config["Mounts"][0]["VolumeOptions"] = {"NoCopy": 1}
    elif case == "unbounded_tmpfs": config["Tmpfs"] = {"/tmp": "rw"}
    elif case == "extra_mount": actual["Mounts"].append({"Type": "volume", "Destination": "/extra"})
    elif case == "host_mount": actual["Mounts"][0]["Type"] = "bind"
    elif case == "wrong_destination": actual["Mounts"][0]["Destination"] = "/app"
    elif case == "readonly_output": actual["Mounts"][0]["RW"] = False
    elif case == "named_output": actual["Mounts"][0]["Name"] = "existing-volume"
    elif case == "remote_driver": actual["Mounts"][0]["Driver"] = "remote"
    else: actual.pop("Mounts")
    with pytest.raises(rehearsal.RehearsalError, match="mount|volume"):
        rehearsal.validate_container(actual, "sha256:exact")


def test_observed_volume_receipt_omits_host_source_path():
    actual = container()
    observed = rehearsal.validate_container(actual, "sha256:exact")
    assert observed["Mounts"] == [{"Type": "volume", "Name": "b" * 64, "Destination": "/output", "Driver": "local", "RW": True}]
    assert "/private-daemon" not in json.dumps(observed)
    assert rehearsal.volume_inventory([], [observed], []) == {
        "scope": "private clean daemon anonymous output volumes", "before_verification": [],
        "observed_output_volumes": ["b" * 64], "after_verification": [], "passed": True}


@pytest.mark.parametrize("case", ["warm", "leak", "unobserved", "extra_output"])
def test_rescue_cleanup_cannot_turn_a_leaked_volume_into_success(case):
    observed = [rehearsal.validate_container(container(), "sha256:exact")]
    before, after = [], []
    if case == "warm": before = ["c" * 64]
    elif case == "leak": after = ["b" * 64]
    elif case == "unobserved": observed = []
    else: observed.append({"Mounts": [{"Name": "c" * 64}]})
    with pytest.raises(rehearsal.RehearsalError, match="volume_not_cleaned"):
        rehearsal.volume_inventory(before, observed, after)


def remote_cleanup_function(run):
    source = ast.parse(rehearsal._REMOTE_STOP)
    helper = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "cleanup_owned_docker")
    namespace = {"subprocess": SimpleNamespace(run=run)}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), "remote-cleanup-helper", "exec"), namespace)
    return namespace["cleanup_owned_docker"]


def test_private_daemon_cleanup_removes_container_volume_and_detached_orphan_only():
    command = ["docker", "--host", "unix:///run/private/docker.sock"]
    replies = iter(["b" * 64 + "\n" + "c" * 64 + "\n", "a" * 64 + "\n", "", "c" * 64 + "\n", "", ""])
    calls = []
    def run(arguments, **kwargs):
        assert arguments[:3] == command
        assert kwargs["check"] is True
        calls.append(arguments[3:])
        return SimpleNamespace(stdout=next(replies))
    result = remote_cleanup_function(run)(command)
    assert calls == [["volume", "ls", "--quiet"], ["ps", "-aq", "--no-trunc"], ["rm", "-f", "-v", "a" * 64],
                     ["volume", "ls", "--quiet"], ["volume", "rm", "c" * 64], ["volume", "ls", "--quiet"]]
    assert result["before_container_removal"] == ["b" * 64, "c" * 64]
    assert result["explicitly_removed"] == ["c" * 64]
    assert result["remaining_after_docker_cleanup"] == [] and result["docker_cleanup_completed"]


@pytest.mark.parametrize("case", ["malformed_volume", "remaining_volume"])
def test_private_daemon_cleanup_does_not_claim_an_unknown_or_remaining_volume_was_removed(case):
    replies = iter(["existing-volume\n"] if case == "malformed_volume" else ["b" * 64 + "\n", "", "", "b" * 64 + "\n"])
    def run(*args, **kwargs):
        return SimpleNamespace(stdout=next(replies))
    with pytest.raises(RuntimeError, match="unexpected_owned_volume_name|owned_volumes_remain"):
        remote_cleanup_function(run)(["docker", "--host", "unix:///run/private/docker.sock"])


@pytest.mark.parametrize("volumes", [["existing-volume"], ["existing-volume", "unrelated-new-volume"], []])
def test_private_cleanup_must_preserve_original_engine_volumes(volumes):
    daemon = rehearsal.IsolatedDaemon()
    daemon.paths = {"root": "/owned", "run": "/owned-run"}
    daemon.existing_before = {"daemon_id": "original", "data_root": "/original-data", "containerd_address": "/original.sock",
                              "global_containerd": [], "images": ["existing-image"], "volumes": ["existing-volume"]}
    after = {**daemon.existing_before, "volumes": volumes}
    daemon.remote = lambda source, *args, **kwargs: after if source == rehearsal._REMOTE_BASELINE else {}
    if "existing-volume" in volumes:
        daemon.__exit__(None, None, None)
        assert daemon.cleanup["existing_engine_preserved"] is True
    else:
        with pytest.raises(rehearsal.RehearsalError, match="existing_engine_changed"):
            daemon.__exit__(None, None, None)
        assert daemon.cleanup["existing_engine_preserved"] is False


@pytest.mark.parametrize("identifier", ["sha256:oci", "sha256:config"])
def test_manifest_and_config_image_stores_resolve_same_sealed_contents(identifier):
    runtime = {"image_id": "sha256:oci", "config_id": "sha256:config", "os": "linux", "architecture": "arm64",
               "rootfs_diff_ids": ["sha256:layer1", "sha256:layer2"]}
    installed = {"Id": identifier, "Os": "linux", "Architecture": "arm64", "RootFS": {"Layers": runtime["rootfs_diff_ids"]}}
    assert rehearsal.validate_loaded_image(installed, runtime) == identifier
    installed["RootFS"]["Layers"] = ["sha256:other"]
    with pytest.raises(rehearsal.RehearsalError, match="contents_mismatch"):
        rehearsal.validate_loaded_image(installed, runtime)


def test_unrelated_tagged_image_cannot_replace_sealed_id():
    with pytest.raises(rehearsal.RehearsalError, match="identity_mismatch"):
        rehearsal.validate_loaded_image({"Id": "sha256:wrong"}, {"image_id": "sha256:oci", "config_id": "sha256:config"})


def run_receipts(tmp_path):
    run = tmp_path / "run"
    for name, value in {"summary.json": {"stage": 5, "profile": "secure", "passed": True, "checks": {"real_workers": True}},
                        "run-manifest.json": {}, "application/summary.json": {}, "application/COMPLETE.json": {},
                        "readiness/summary.json": {}, "readiness/trajectories.jsonl": {}, "showcase.html": {}}.items():
        path = run / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    complete = {"stage": 5, "completed": True, "passed": True,
                "sha256": {path.relative_to(run).as_posix(): rehearsal.digest(path) for path in run.rglob("*") if path.is_file()}}
    (run / "COMPLETE.json").write_text(json.dumps(complete))
    metrics = {"stage": 5, "passed": True, "cgroup_v2": True, "values": {
        "memory.max": 4 * 1024**3, "memory.peak": 123456789, "pids.max": 64, "cpu.max": "200000 100000"}}
    (tmp_path / "cgroup-metrics.json").write_text(json.dumps(metrics))
    (tmp_path / "tests.xml").write_text('<testsuites><testsuite tests="123" errors="0" failures="0" skipped="0"/></testsuites>')
    return run, complete, metrics


def test_completed_receipts_include_kernel_peak_and_zero_skips(tmp_path):
    run_receipts(tmp_path)
    result = rehearsal.verify_run_receipts(tmp_path)
    assert result["junit"] == {"tests": 123, "errors": 0, "failures": 0, "skipped": 0}
    assert result["metrics"]["values"]["memory.peak"] == 123456789


@pytest.mark.parametrize("case", ["incomplete", "tamper", "traversal", "no_peak", "excess_peak", "wrong_cpu", "skipped", "failed_checks"])
def test_incomplete_corrupt_or_unmeasured_runs_never_attest_success(tmp_path, case):
    run, complete, metrics = run_receipts(tmp_path)
    if case == "incomplete":
        complete["completed"] = False
    elif case == "tamper":
        (run / "showcase.html").write_text("changed")
    elif case == "traversal":
        complete["sha256"]["../outside"] = "0" * 64
    elif case == "no_peak":
        metrics["values"].pop("memory.peak")
    elif case == "excess_peak":
        metrics["values"]["memory.peak"] = 5 * 1024**3
    elif case == "wrong_cpu":
        metrics["values"]["cpu.max"] = "max 100000"
    elif case == "skipped":
        (tmp_path / "tests.xml").write_text('<testsuites><testsuite tests="123" skipped="5"/></testsuites>')
    else:
        (run / "summary.json").write_text(json.dumps({"stage": 5, "profile": "secure", "passed": True, "checks": {"real_workers": False}}))
        complete["sha256"]["summary.json"] = rehearsal.digest(run / "summary.json")
    (run / "COMPLETE.json").write_text(json.dumps(complete))
    (tmp_path / "cgroup-metrics.json").write_text(json.dumps(metrics))
    with pytest.raises(rehearsal.RehearsalError):
        rehearsal.verify_run_receipts(tmp_path)


def test_bad_archive_is_rejected_before_vm_provision(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = {"software_version": "0.5.0", "runtime": {"architecture": "arm64"}}
    monkeypatch.setitem(sys.modules, "release_support", SimpleNamespace(verify_bundle=lambda path: copy.deepcopy(manifest), extract_release=lambda *args: None))
    archive = tmp_path / "genesis-fin-audit-0.5.0-linux-arm64.tar.gz"
    archive.write_bytes(b"bad archive")
    monkeypatch.setattr(rehearsal.IsolatedDaemon, "__enter__", lambda self: pytest.fail("must not provision a daemon"))
    with pytest.raises(rehearsal.RehearsalError, match="archive_checksum_mismatch"):
        rehearsal.run_clean_rehearsal(bundle, tmp_path / "receipt", expected_sha256="0" * 64)
    assert not (tmp_path / "receipt").exists()


def test_profile_is_an_argument_not_a_shell_command():
    with pytest.raises(ValueError, match="invalid_colima_profile"):
        rehearsal.IsolatedDaemon("profile; shutdown -h now")


@pytest.mark.parametrize("reported,canonical", [("x86_64", "amd64"), ("amd64", "amd64"),
                                                ("AMD64", "amd64"), ("aarch64", "arm64"), ("arm64", "arm64")])
def test_architecture_aliases_do_not_change_instruction_set(reported, canonical):
    assert rehearsal.canonical_architecture(reported) == canonical


def platform_facts():
    runtime = {"os": "linux", "architecture": "amd64"}
    application = {"platform": "Linux", "architecture": "x86_64", "python": "3.11.15", "software_version": "0.6.0"}
    daemon = {"OSType": "linux", "Architecture": "x86_64"}
    vm = {"system": "Linux", "machine": "x86_64", "kernel": {"system": "Linux", "machine": "x86_64", "release": "observed-kernel"}}
    client = {"system": "Darwin", "machine": "arm64"}
    return runtime, application, daemon, vm, client


def test_full_system_cross_arch_guest_is_reported_without_native_host_claim():
    proof = rehearsal.execution_platform(*platform_facts(), version="0.6.0")
    assert proof["target_platform"] == "linux/amd64"
    assert proof["cross_architecture_client_to_vm"] is True
    assert proof["cross_architecture_vm_to_application"] is False
    assert proof["client"]["machine"] == "arm64"
    assert proof["vm"]["kernel"]["machine"] == "x86_64"
    assert "not native bare-metal" in proof["scope"]


def test_user_mode_cross_arch_execution_is_not_mislabelled_as_matching_vm():
    runtime, application, daemon, vm, client = platform_facts()
    daemon["Architecture"] = "aarch64"
    vm["machine"] = vm["kernel"]["machine"] = "aarch64"
    proof = rehearsal.execution_platform(runtime, application, daemon, vm, client, version="0.6.0")
    assert proof["cross_architecture_client_to_vm"] is False
    assert proof["cross_architecture_vm_to_application"] is True


@pytest.mark.parametrize("case", ["wrong_app_arch", "wrong_app_os", "wrong_version", "wrong_python",
                                  "missing_vm", "contradictory_kernel", "contradictory_daemon"])
def test_platform_attestation_rejects_missing_or_contradictory_observations(case):
    runtime, application, daemon, vm, client = platform_facts()
    if case == "wrong_app_arch": application["architecture"] = "aarch64"
    elif case == "wrong_app_os": application["platform"] = "Darwin"
    elif case == "wrong_version": application["software_version"] = "0.5.0"
    elif case == "wrong_python": application["python"] = "3.12.0"
    elif case == "missing_vm": vm = {}
    elif case == "contradictory_kernel": vm["kernel"]["machine"] = "aarch64"
    else: daemon["Architecture"] = "aarch64"
    with pytest.raises(rehearsal.RehearsalError):
        rehearsal.execution_platform(runtime, application, daemon, vm, client, version="0.6.0")


def test_unknown_client_cpu_is_left_unknown_instead_of_claiming_native_execution():
    runtime, application, daemon, vm, client = platform_facts()
    client["machine"] = "unknown"
    proof = rehearsal.execution_platform(runtime, application, daemon, vm, client, version="0.6.0")
    assert proof["cross_architecture_client_to_vm"] is None


def test_wrong_requested_target_is_rejected_before_vm_provision(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = {"software_version": "0.6.0", "runtime": {"os": "linux", "architecture": "arm64"}}
    monkeypatch.setitem(sys.modules, "release_support", SimpleNamespace(verify_bundle=lambda path: manifest, extract_release=lambda *args: None))
    monkeypatch.setattr(rehearsal.IsolatedDaemon, "__enter__", lambda self: pytest.fail("must not provision a daemon"))
    with pytest.raises(rehearsal.RehearsalError, match="target_platform_mismatch"):
        rehearsal.run_clean_rehearsal(bundle, tmp_path / "receipt", expected_platform="linux/amd64")
    assert not (tmp_path / "receipt").exists()
