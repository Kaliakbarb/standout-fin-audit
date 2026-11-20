"""Public proof packaging must not publish trusted seeds or stale evidence."""

import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import submission


def test_host_config_redacts_actual_private_command_without_mutating_input():
    containers = [{"Image": "sha256:abc", "Config": {"Cmd": ["verify", "--master-seed", "secret-control-value"], "Env": ["SECRET=value"]},
                   "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True}}]
    public = submission.redacted_containers(containers, "a" * 64)
    assert "secret-control-value" not in str(public)
    assert "SECRET=value" not in str(public)
    assert public["containers"][0]["HostConfig"]["NetworkMode"] == "none"
    assert containers[0]["Config"]["Cmd"][-1] == "secret-control-value"


@pytest.mark.parametrize("value", [{"master_seed": 123}, {"nested": [{"seed": 123}]}, {"hidden_seed": "123"}])
def test_public_receipt_refuses_private_control_seed_fields(value):
    with pytest.raises(ValueError, match="private_control_seed"):
        submission.no_control_seeds(value)


def test_legitimate_disjoint_seed_check_and_checksums_are_not_private_seed_values():
    submission.no_control_seeds({"checks": {"split_seeds_and_configs_disjoint": True}, "sha256": {"control-seed.json": "a" * 64}})


def test_raw_evidence_requires_the_exact_completed_receipt_hash(tmp_path):
    path = tmp_path / "verification" / "tests.xml"
    path.parent.mkdir()
    path.write_text("<testsuite tests='1' failures='0'/>")
    receipt = {"sha256": {"verification/tests.xml": hashlib.sha256(path.read_bytes()).hexdigest()}}
    assert submission.checked_file(tmp_path, receipt, "verification/tests.xml") == path
    path.write_text("<testsuite tests='1' failures='1'/>")
    with pytest.raises(ValueError, match="clean_artifact_hash_mismatch"):
        submission.checked_file(tmp_path, receipt, "verification/tests.xml")


def test_raw_evidence_does_not_follow_a_symlink(tmp_path):
    target = tmp_path / "original.json"
    target.write_text("{}")
    link = tmp_path / "linked.json"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="clean_artifact_hash_mismatch"):
        submission.checked_file(tmp_path, {"sha256": {"linked.json": submission.digest(target)}}, "linked.json")


def test_seed_value_scan_detects_secret_in_a_string_without_echoing_it(tmp_path):
    secret = 987654321012345678901234567890
    path = tmp_path / "report.json"
    path.write_text('{"command": "verify --master-seed ' + str(secret) + '"}')
    with pytest.raises(ValueError, match="private_seed_value") as caught:
        submission.check_seed_values([path], [secret])
    assert str(secret) not in str(caught.value)


def test_seed_value_scan_allows_public_checksum_keys_but_checks_every_sidecar(tmp_path):
    path = tmp_path / "public.json"
    path.write_text('{"sha256": {"control-seed.json": "abcdef"}}')
    assert submission.check_seed_values([path], [987654321012345678901234567890])["matches"] == 0


@pytest.mark.parametrize("change", [
    {"before_verification": ["unrelated"]},
    {"after_verification": ["a" * 64]},
    {"observed_output_volumes": []},
    {"observed_output_volumes": ["a" * 64, "b" * 64]},
    {"observed_output_volumes": ["not-an-anonymous-volume"]},
    {"passed": False},
])
def test_submission_refuses_missing_or_leaked_output_volume_evidence(change):
    evidence = {"before_verification": [], "after_verification": [],
                "observed_output_volumes": ["a" * 64], "passed": True, **change}
    with pytest.raises(ValueError, match="output_volume_lifecycle_not_clean"):
        submission.require_clean_volume_inventory(evidence)


def test_final_zip_keeps_exact_archive_and_only_public_evidence(tmp_path, monkeypatch):
    release, clean = tmp_path / "release", tmp_path / "clean"
    (release / "bundle").mkdir(parents=True)
    clean.mkdir()
    master, episode = 987654321012345678901234567890, 876543210987654321098765432109
    runtime = {"os": "linux", "architecture": "amd64", "image_id": "sha256:image", "config_id": "sha256:config"}
    manifest = {"software_version": "0.6.0", "runtime": runtime}
    (release / "bundle/release-manifest.json").write_text(json.dumps(manifest))
    archive = release / "genesis-fin-audit-0.6.0-linux-amd64.tar.gz"
    archive.write_bytes(b"exact sealed archive bytes")
    build = {"archive_sha256": submission.digest(archive), "manifest_sha256": submission.digest(release / "bundle/release-manifest.json")}
    (release / "release-receipt.json").write_text(json.dumps(build))
    platform = {"target_platform": "linux/amd64", "application": {"architecture": "x86_64"},
                "client": {"machine": "arm64"}, "cross_architecture_client_to_vm": True}
    entries = {
        "verification/tests.xml": "<testsuite tests='1' failures='0'/>",
        "verification/run/summary.json": {"passed": True, "profile": "secure", "checks": {"web": True}, "isolation_probes": {"probe": True}},
        "verification/cgroup-metrics.json": {"passed": True, "values": {"memory.peak": 123}},
        "verification/run/showcase.html": "<html>Open synthetic demonstration</html>",
        "initial-daemon.json": {"info": {"Images": 0, "Containers": 0}, "isolation": {"routes": []}},
        "application-containers.json": [{"Config": {"Cmd": ["verify", "--master-seed", str(master)]}, "HostConfig": {"NetworkMode": "none"}}],
        "daemon-cleanup.json": {"existing_engine_preserved": True, "stopped_pid": 123, "removed_owned_roots": ["/owned/test"]},
        "execution-platform.json": platform,
        "loaded-image.json": {"Id": "sha256:image", "Architecture": "amd64"},
        "volume-inventory.json": {"scope": "private clean daemon anonymous output volumes",
                                  "before_verification": [], "observed_output_volumes": ["a" * 64],
                                  "after_verification": [], "passed": True},
        "verification/run/readiness/web-summary.json": {"passed": True},
        "verification/run/run-manifest.json": {"software_version": "0.6.0", "architecture": "x86_64", "source_sha256": {}},
        "verification/run/readiness/summary.json": {"passed": True, "web_readiness": {"sha256": hashlib.sha256(json.dumps({"passed": True}).encode()).hexdigest()}},
        "control-seed.json": {"master_seed": master},
        "verification/run/application/regression/protocol/run-manifest.json": {"master_seed": master, "episodes": [{"seed": episode}]},
    }
    for name, value in entries.items():
        path = clean / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))
    receipt = {"completed": True, "passed": True, "software_version": "0.6.0", "runtime": runtime,
               "execution_platform": platform, "junit": {"tests": 1, "errors": 0, "failures": 0, "skipped": 0},
               "release_archive": {"filename": archive.name, "sha256": submission.digest(archive), "bytes": archive.stat().st_size},
               "peak_cgroup_memory_bytes": 123, "elapsed_seconds": 1,
               "sha256": {name: submission.digest(clean / name) for name in entries}}
    (clean / "COMPLETE.json").write_text(json.dumps(receipt))
    monkeypatch.setattr(submission, "verify_bundle", lambda _: manifest)
    result = submission.build_submission(release, clean, tmp_path / "submission")
    with zipfile.ZipFile(Path(result["output"]) / result["zip"]) as delivered:
        assert delivered.testzip() is None
        assert delivered.read(archive.name) == archive.read_bytes()
        assert "control-seed.json" not in delivered.namelist()
        assert "run-manifest.json" not in delivered.namelist()
        for name in delivered.namelist():
            assert str(master).encode() not in delivered.read(name)
            assert str(episode).encode() not in delivered.read(name)
        public = json.loads(delivered.read("DELIVERY_REPORT.json"))
        assert public["privacy_scan"]["long_decimal_tokens_checked"] == 2
        assert public["no_official_submission_performed"] is True
        assert delivered.read("CLEAN_ROOM_REPORT.json") == (clean / "COMPLETE.json").read_bytes()
        assert delivered.read("VOLUME_INVENTORY.json") == (clean / "volume-inventory.json").read_bytes()
        assert hashlib.sha256(delivered.read("CLEAN_ROOM_REPORT.json")).hexdigest() == public["raw_clean_receipt_sha256"]
        readiness = json.loads(delivered.read("READINESS_REPORT.json"))
        assert hashlib.sha256(delivered.read("web-summary.json")).hexdigest() == readiness["web_readiness"]["sha256"]
