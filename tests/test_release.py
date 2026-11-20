"""Offline release format regressions with tiny synthetic image archives."""

from __future__ import annotations

import base64
import gzip
import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_support", ROOT / "scripts/release_support.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    path.chmod(0o644)


def add(archive, name, data=b"", *, kind=tarfile.REGTYPE, mode=0o644, link=""):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = mode
    info.linkname = link
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    archive.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)


def image_archive(path, *, os_name="linux", architecture="arm64", oci=False, binding=None):
    uncompressed = b"fixture layer"
    layer = gzip.compress(uncompressed, mtime=0) if oci else uncompressed
    diff_id = "sha256:" + hashlib.sha256(uncompressed).hexdigest()
    if binding == "rootfs":
        diff_id = "sha256:" + "0" * 64
    config = json.dumps({"os": os_name, "architecture": architecture, "rootfs": {"type": "layers", "diff_ids": [diff_id]}}, sort_keys=True).encode()
    digest = hashlib.sha256(config).hexdigest()
    layer_sha = hashlib.sha256(layer).hexdigest()
    config_name = "blobs/sha256/" + digest if oci else digest + ".json"
    layer_name = "blobs/sha256/" + layer_sha if oci else "layer.tar"
    preferred = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                 "config": {"digest": "sha256:" + ("0" * 64 if binding == "config" else digest), "size": len(config)},
                 "layers": [{"digest": "sha256:" + ("0" * 64 if binding == "layer" else layer_sha), "size": len(layer),
                             "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}]}
    preferred_bytes = json.dumps(preferred, sort_keys=True).encode()
    preferred_sha = hashlib.sha256(preferred_bytes).hexdigest()
    with tarfile.open(path, "w") as archive:
        add(archive, config_name, config)
        add(archive, layer_name, layer)
        if oci:
            add(archive, "blobs/sha256/" + preferred_sha, preferred_bytes if binding != "digest" else b"tampered")
        add(archive, "manifest.json", json.dumps([{"Config": config_name, "Layers": [layer_name], "RepoTags": ["untrusted:tag"]}]).encode())
    return "sha256:" + (preferred_sha if oci else digest)


@pytest.fixture
def inputs(tmp_path):
    root = tmp_path / "source"
    for name, data in {
        "README.md": "fixture documentation\n", "LICENSE": "MIT fixture\n", ".python-version": "3.11\n",
        "pyproject.toml": '[project]\nversion = "0.5.0"\n', "uv.lock": "fixture lock\n",
        "src/fin_audit/contracts.py": 'SCHEMA_VERSION = "0.2.0"\nPROTOCOL_VERSION = "1.0"\n',
        "scripts/verify": "# fixture verifier\n", "scripts/release.py": "# fixture builder\n",
        "scripts/release_support.py": "# fixture release runtime\n", "tests/test_fixture.py": "assert True\n",
        "datasets/manifest.json": '{"corpus_version":"0.3.0"}\n',
        "datasets/train/open/private/truth.json": '[]\n', "docs/CONFORMANCE.md": "fixture conformance\n",
    }.items():
        write(root / name, data)
    for name in release.REQUIRED_DOCS:
        write(root / name, "fixture required release document\n")
    (root / "scripts/verify").chmod(0o755)
    archive = tmp_path / "image.tar"
    image_id = image_archive(archive)
    metadata = {"image_id": image_id, "os": "linux", "architecture": "arm64", "variant": "",
                "source_inventory": release._runtime_hashes(root, release._source_paths(root)),
                "licenses": [{"name": "fixture", "texts": [{"path": "runtime/licenses/cpython/LICENSE.txt"}]}],
                "license_files": {"cpython/LICENSE.txt": base64.b64encode(b"fixture runtime license\n").decode()},
                "build_evidence": {}}
    sample = tmp_path / "receipt"
    episodes, runs, trajectories = [], [], []
    for case in sorted(release.SHOWCASE_CASES):
        hashes = {}
        for name in release.PUBLIC_FILES:
            path = sample / "episodes" / case / "public" / name
            if name == "ledger.csv":
                data = ",".join(release.LEDGER_COLUMNS) + "\n"
            elif name == "fx.csv":
                data = ",".join(release.FX_COLUMNS) + "\n"
            elif name.endswith("jsonl"):
                data = ""
            elif name == "manifest.json":
                data = json.dumps({"episode_id": "demo-" + case}) + "\n"
            else:
                data = '{}\n'
            write(path, data)
            hashes["public/" + name] = release._sha(path)
        public_digest = release._canonical_digest(release._sample_public(sample / "episodes" / case / "public"))
        for name, value in {"truth.json": [], "provenance.json": {"seed": 19, "presentation_seed": 2026112204, "public_sha256": public_digest}}.items():
            path = sample / "episodes" / case / "private" / name
            write(path, json.dumps(value) + "\n")
            hashes["private/" + name] = release._sha(path)
        episodes.append({"case": case, "files": hashes, "episode_id": "demo-" + case,
                         "public_sha256": public_digest, "truth_sha256": release._canonical_digest([])})
        for policy in ("simple", "silent", "oracle"):
            run_id = f"showcase-{len(runs):03}"
            runs.append({"case": case, "policy": policy, "trajectory_run_id": run_id})
            trajectories.append({"run_id": run_id, "reward": 0, "info": {}})
    summary = {"showcase_version": "1.0", "software_version": "0.5.0", "scope": "post_run_only", "profile": "secure",
               "passed": True, "case_count": 9, "runs": runs, "checks": {"unit_fixture": True},
               "execution_counts": {"ordinary_workers": 18, "run_episode_calls": 27, "trusted_oracles": 9}}
    for name, value in {"summary.json": summary, "evaluation.json": runs,
                        "analysis.json": {"showcase_version": "1.0", "scope": "post_run_only"},
                        "run-manifest.json": {"episodes": episodes, "source_sha256": metadata["source_inventory"], "software_version": "0.5.0"}}.items():
        write(sample / name, json.dumps(value))
    write(sample / "trajectories.jsonl", "".join(json.dumps(row) + "\n" for row in trajectories))
    complete = {"showcase_version": "1.0", "completed": True, "passed": True,
                "sha256": {name: release._sha(sample / name) for name in release.SAMPLE_FILES - {"COMPLETE.json"}}}
    write(sample / "COMPLETE.json", json.dumps(complete))
    return root, archive, image_id, metadata, sample


def build(inputs, output):
    root, archive, image_id, metadata, sample = inputs
    return release.build_release(root, output, archive, image_id, metadata, sample)


def test_deterministic_release_roundtrip_source_permissions_and_inventory(inputs, tmp_path):
    first = build(inputs, tmp_path / "first")
    second = build(inputs, tmp_path / "second")
    assert first["archive_sha256"] == second["archive_sha256"]
    assert first["archive_bytes"] < release.MAX_BYTES
    bundle = Path(first["bundle"])
    manifest = release.verify_bundle(bundle)
    assert manifest["software_version"] == "0.5.0"
    assert manifest["schema_version"] == "0.2.0" and manifest["corpus_version"] == "0.3.0"
    assert manifest["runtime"]["image_id"] == inputs[2]
    assert manifest["reproducibility"]["closed_holdout_seeds_included"] is False
    assert manifest["sample"] == {"included": True, "scope": "open-showcase"}
    assert (bundle / "runtime/licenses/cpython/LICENSE.txt").read_text() == "fixture runtime license\n"
    assert len(list((bundle / "sample/episodes").glob("*/private/*.json"))) == 18
    assert len(list((bundle / "sample/episodes").glob("*/*/*"))) == 72
    assert json.loads((bundle / "sample/episodes/clean/private/provenance.json").read_text())["seed"] == 19
    assert stat.S_IMODE((bundle / "sample/episodes/clean/private").stat().st_mode) == 0o700
    assert stat.S_IMODE((bundle / "sample/episodes/clean/private/truth.json").stat().st_mode) == 0o600
    assert (bundle / "sample/COMPLETE.json").read_bytes() == (inputs[4] / "COMPLETE.json").read_bytes()
    assert stat.S_IMODE((bundle / "datasets/train/open/private").stat().st_mode) == 0o700
    assert stat.S_IMODE((bundle / "datasets/train/open/private/truth.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((bundle / "verify").stat().st_mode) == 0o755
    archive = Path(first["output"]) / first["archive"]
    extracted = release.extract_release(archive, tmp_path / "extracted", first["archive_sha256"])
    assert release.verify_bundle(extracted) == manifest
    with tarfile.open(archive) as stream:
        members = stream.getmembers()
    assert [item.name for item in members] == sorted(item.name for item in members)
    assert all(item.uid == item.gid == item.mtime == 0 and not item.uname and not item.gname for item in members)


@pytest.mark.parametrize("tamper", ["content", "extra", "missing", "mode", "symlink", "hardlink", "nested_manifest"])
def test_inventory_rejects_tampering_before_docker(inputs, tmp_path, tamper):
    bundle = Path(build(inputs, tmp_path / "release")["bundle"])
    target = bundle / "README.md"
    if tamper == "content":
        target.write_text("changed")
    elif tamper == "extra":
        write(bundle / "secret.txt", "unexpected")
    elif tamper == "missing":
        target.unlink()
    elif tamper == "mode":
        target.chmod(0o600)
    elif tamper == "symlink":
        target.unlink()
        target.symlink_to(inputs[0] / "README.md")
    elif tamper == "hardlink":
        os.link(target, tmp_path / "linked")
    else:
        write(bundle / "nested/release-manifest.json", "{}")
    with pytest.raises(release.ReleaseError):
        release.verify_bundle(bundle)


@pytest.mark.parametrize("kind", ["traversal", "absolute", "backslash", "duplicate", "symlink", "hardlink", "device", "collision", "setuid"])
def test_unsafe_archives_rejected_atomically_before_writes(tmp_path, kind):
    path = tmp_path / "unsafe.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        if kind == "traversal":
            add(archive, "../escaped", b"bad")
        elif kind == "absolute":
            add(archive, "/escaped", b"bad")
        elif kind == "backslash":
            add(archive, "a\\..\\escaped", b"bad")
        elif kind == "duplicate":
            add(archive, "duplicate", b"a")
            add(archive, "duplicate", b"b")
        elif kind in {"symlink", "hardlink"}:
            add(archive, "linked", kind=tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE, link="../escaped")
        elif kind == "device":
            add(archive, "device", kind=tarfile.CHRTYPE)
        elif kind == "collision":
            add(archive, "parent", b"file")
            add(archive, "parent/child", b"bad")
        else:
            add(archive, "executable", b"bad", mode=0o4755)
    output = tmp_path / "output"
    with pytest.raises((release.ReleaseError, FileExistsError)):
        release.extract_release(path, output)
    assert not output.exists() and not (tmp_path / "escaped").exists()
    assert not list(tmp_path.glob(".release-extract-*"))


def test_wrong_image_id_platform_or_layer_manifest_rejected(inputs, tmp_path):
    _, image, image_id, _, _ = inputs
    with pytest.raises(release.ReleaseError, match="image_config_id_mismatch"):
        release.inspect_image_archive(image, "sha256:" + "0" * 64)
    unsupported = tmp_path / "windows.tar"
    unsupported_id = image_archive(unsupported, os_name="windows")
    with pytest.raises(release.ReleaseError, match="unsupported_image_platform"):
        release.inspect_image_archive(unsupported, unsupported_id)


def test_source_image_drift_and_closed_seed_docs_are_rejected(inputs, tmp_path):
    root, image, image_id, metadata, sample = inputs
    write(root / "tests/test_fixture.py", "assert False\n")
    with pytest.raises(release.ReleaseError, match="image_and_package_source_mismatch"):
        build(inputs, tmp_path / "drift")
    assert not (tmp_path / "drift").exists()
    metadata["source_inventory"] = release._runtime_hashes(root, release._source_paths(root))
    write(root / "docs/leaked.json", '{"master_seed":123}')
    with pytest.raises(release.ReleaseError, match="closed_seed_in_public_document"):
        build(inputs, tmp_path / "leak")
    assert not (tmp_path / "leak").exists()


def test_allowlist_omits_artifacts_caches_git_and_home_credentials(inputs, tmp_path):
    root = inputs[0]
    for name in ("artifacts/private-seed.json", ".git/config", ".env", "tests/__pycache__/cache.pyc", "docs/.credentials", "src/private.key"):
        write(root / name, "should not be included")
    bundle = Path(build(inputs, tmp_path / "release")["bundle"])
    assert not any((bundle / name).exists() for name in ("artifacts", ".git", ".env", "tests/__pycache__", "docs/.credentials", "src/private.key"))


def test_force_load_exact_archive_even_if_tag_cache_would_be_present(inputs, tmp_path, monkeypatch):
    bundle = Path(build(inputs, tmp_path / "release")["bundle"])
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if "load" in command:
            return b"Loaded image: untrusted:tag\n"
        return json.dumps([{"Id": inputs[2], "Os": "linux", "Architecture": "arm64",
                            "RootFS": {"Layers": release.inspect_image_archive(inputs[1], inputs[2])["rootfs_diff_ids"]}}]).encode()

    monkeypatch.setattr(release, "_docker_result", run)
    for _ in range(2):
        assert release.load_verified_image(bundle, ["docker", "--context", "clean"])["runtime"]["image_id"] == inputs[2]
    assert len(commands) == 4
    assert commands[0][3:5] == ["image", "load"]
    assert commands[1][-1] == inputs[2]
    assert all("pull" not in command for command in commands)


def test_tampered_archive_or_wrong_loaded_identity_never_runs_image(inputs, tmp_path, monkeypatch):
    bundle = Path(build(inputs, tmp_path / "release")["bundle"])
    calls = []

    def wrong(command, **kwargs):
        calls.append(command)
        return b"loaded" if "load" in command else b'[{"Id":"wrong","Os":"linux","Architecture":"arm64"}]'

    monkeypatch.setattr(release, "_docker_result", wrong)
    with pytest.raises(release.ReleaseError, match="loaded_image_identity_mismatch"):
        release.load_verified_image(bundle, ["docker"])
    assert len(calls) == 2
    write(bundle / "runtime/image.tar", b"tampered")
    calls.clear()
    with pytest.raises(release.ReleaseError):
        release.load_verified_image(bundle, ["docker"])
    assert calls == []


@pytest.mark.parametrize("kind", ["directory", "dangling_symlink", "parent_symlink"])
def test_new_leaf_requirements(inputs, tmp_path, kind):
    output = tmp_path / "existing"
    if kind == "directory":
        output.mkdir()
    elif kind == "dangling_symlink":
        output.symlink_to(tmp_path / "missing")
    else:
        parent = tmp_path / "linked"
        parent.symlink_to(tmp_path / "target", target_is_directory=True)
        output = parent / "new"
    with pytest.raises((FileExistsError, release.ReleaseError)):
        build(inputs, output)


def test_outer_archive_checksum_is_checked_before_extraction(inputs, tmp_path):
    result = build(inputs, tmp_path / "release")
    output = tmp_path / "wrong-checksum"
    with pytest.raises(release.ReleaseError, match="release_archive_checksum_mismatch"):
        release.extract_release(Path(result["output"]) / result["archive"], output, "0" * 64)
    assert not output.exists()


def test_duplicate_manifest_keys_are_rejected(inputs, tmp_path):
    bundle = Path(build(inputs, tmp_path / "release")["bundle"])
    (bundle / release.MANIFEST).write_text('{"format_version":1,"format_version":1}')
    with pytest.raises(release.ReleaseError, match="duplicate_json_key"):
        release.verify_bundle(bundle)


@pytest.mark.parametrize("change,expected", [
    ("version", "invalid_or_stale_open_showcase"),
    ("cases", "unexpected_showcase_cases"),
    ("runs", "invalid_or_stale_open_showcase"),
    ("unmasked", "invalid_or_unmasked_showcase_trajectory"),
    ("public_hash", "showcase_episode_file_hash_mismatch"),
    ("closed_seed", "closed_seed_in_public_document"),
])
def test_sample_contract_rejects_stale_partial_leaking_or_tampered_receipts(inputs, tmp_path, change, expected):
    sample = inputs[4]
    if change in {"version", "runs"}:
        value = json.loads((sample / "summary.json").read_text())
        if change == "version":
            value["software_version"] = "0.4.0"
        else:
            value["runs"] = value["runs"][:-1]
        write(sample / "summary.json", json.dumps(value))
    elif change == "cases":
        value = json.loads((sample / "run-manifest.json").read_text())
        value["episodes"][0]["case"] = "h00"
        write(sample / "run-manifest.json", json.dumps(value))
    elif change == "unmasked":
        with (sample / "trajectories.jsonl").open("a") as handle:
            handle.write('{"run_id":"showcase-000","reward":1,"info":{}}\n')
    elif change == "public_hash":
        write(sample / "episodes/clean/public/ledger.csv", "modified")
    else:
        value = json.loads((sample / "analysis.json").read_text())
        value["master_seed"] = 123
        write(sample / "analysis.json", json.dumps(value))
    receipt = json.loads((sample / "COMPLETE.json").read_text())
    receipt["sha256"] = {name: release._sha(sample / name) for name in release.SAMPLE_FILES - {"COMPLETE.json"}}
    write(sample / "COMPLETE.json", json.dumps(receipt))
    output = tmp_path / "bad-sample"
    with pytest.raises(release.ReleaseError, match=expected):
        build(inputs, output)
    assert not output.exists()


def test_mandatory_sample_and_required_design_docs_cannot_be_omitted(inputs, tmp_path):
    root, archive, image_id, metadata, _ = inputs
    with pytest.raises(release.ReleaseError, match="approved_open_showcase_required"):
        release.build_release(root, tmp_path / "no-sample", archive, image_id, metadata)
    (root / "docs/DESIGN_NOTE.md").unlink()
    with pytest.raises(release.ReleaseError, match="missing_required_release_source"):
        build(inputs, tmp_path / "no-doc")


def test_launcher_always_forces_release_mode_and_disables_bytecode(inputs, tmp_path):
    root, _, _, metadata, sample = inputs
    write(root / "scripts/verify", "import json,sys; print(json.dumps(sys.argv[1:])); print(sys.dont_write_bytecode)\n")
    metadata["source_inventory"] = release._runtime_hashes(root, release._source_paths(root))
    provenance = json.loads((sample / "run-manifest.json").read_text())
    provenance["source_sha256"] = metadata["source_inventory"]
    write(sample / "run-manifest.json", json.dumps(provenance))
    receipt = json.loads((sample / "COMPLETE.json").read_text())
    receipt["sha256"]["run-manifest.json"] = release._sha(sample / "run-manifest.json")
    write(sample / "COMPLETE.json", json.dumps(receipt))
    bundle = Path(build(inputs, tmp_path / "release")["bundle"])
    result = subprocess.run([sys.executable, "-B", str(bundle / "verify"), "--offline"], capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ['["--release-bundle", "--offline"]', "True"]
    release.verify_bundle(bundle)


def test_docker_commands_have_real_deadlines_and_bounded_output():
    with pytest.raises(release.ReleaseError, match="docker_command_timeout"):
        release._docker_result([sys.executable, "-c", "import time; time.sleep(20)"], timeout=0.1)
    with pytest.raises(release.ReleaseError, match="docker_output_limit"):
        release._docker_result([sys.executable, "-c", "import os; os.write(1,b'x'*100000)"], maximum=1024)


def test_source_inventory_must_cover_all_executable_inputs(inputs, tmp_path):
    bundle = Path(build(inputs, tmp_path / "release")["bundle"])
    manifest = json.loads((bundle / release.MANIFEST).read_text())
    del manifest["source_inventory"]["scripts/verify"]
    write(bundle / release.MANIFEST, json.dumps(manifest))
    with pytest.raises(release.ReleaseError, match="incomplete_source_inventory"):
        release.verify_bundle(bundle)


@pytest.mark.parametrize("binding,reason", [("config", "image_manifest_config_binding_mismatch"),
                                            ("layer", "image_manifest_layer_binding_mismatch"),
                                            ("digest", "image_manifest_digest_mismatch"),
                                            ("rootfs", "image_rootfs_diff_mismatch_or_limit")])
def test_oci_manifest_requires_actual_content_binding_to_config_and_layers(tmp_path, binding, reason):
    path = tmp_path / "oci.tar"
    preferred = image_archive(path, oci=True, binding=binding)
    with pytest.raises(release.ReleaseError, match=reason):
        release.inspect_image_archive(path, preferred)


@pytest.mark.parametrize("store", ["oci", "legacy"])
def test_oci_and_legacy_stores_resolve_only_sealed_ids_and_identical_rootfs(inputs, tmp_path, monkeypatch, store):
    root, archive, _, metadata, sample = inputs
    preferred = image_archive(archive, oci=True)
    metadata["image_id"] = preferred
    runtime = release.inspect_image_archive(archive, preferred)
    assert runtime["image_id"] != runtime["config_id"]
    result = release.build_release(root, tmp_path / "release", archive, preferred, metadata, sample)
    bundle = Path(result["bundle"])
    commands = []

    def docker(command, **kwargs):
        commands.append(command)
        if "load" in command:
            return b"loaded"
        if store == "legacy" and command[-1] == preferred:
            raise release.ReleaseError("docker_command_failed:No such image")
        identifier = preferred if store == "oci" else runtime["config_id"]
        return json.dumps([{"Id": identifier, "Os": "linux", "Architecture": "arm64",
                            "RootFS": {"Layers": runtime["rootfs_diff_ids"]}}]).encode()

    monkeypatch.setattr(release, "_docker_result", docker)
    loaded = release.load_verified_image(bundle, ["docker"])
    assert loaded["loaded_image_id"] == (preferred if store == "oci" else runtime["config_id"])
    assert commands[0][1:3] == ["image", "load"]
    assert commands[1][-1] == preferred
    assert len(commands) == (2 if store == "oci" else 3)
    if store == "legacy":
        assert commands[2][-1] == runtime["config_id"]
    # The transient resolved ID must not alter the sealed disk manifest.
    assert "loaded_image_id" not in release.verify_bundle(bundle)


def test_loaded_image_with_sealed_id_but_wrong_rootfs_is_rejected(inputs, tmp_path, monkeypatch):
    bundle = Path(build(inputs, tmp_path / "release")["bundle"])

    def docker(command, **kwargs):
        if "load" in command:
            return b"loaded"
        return json.dumps([{"Id": inputs[2], "Os": "linux", "Architecture": "arm64",
                            "RootFS": {"Layers": ["sha256:" + "0" * 64]}}]).encode()

    monkeypatch.setattr(release, "_docker_result", docker)
    with pytest.raises(release.ReleaseError, match="loaded_image_identity_mismatch"):
        release.load_verified_image(bundle, ["docker"])


@pytest.mark.parametrize("change,reason", [("truth_digest", "showcase_canonical_episode_digest_mismatch"),
                                          ("public_digest", "showcase_canonical_episode_digest_mismatch"),
                                          ("master_seed", "closed_seed_in_public_document")])
def test_open_private_proofs_are_present_hash_bound_and_do_not_admit_closed_receipts(inputs, tmp_path, change, reason):
    sample = inputs[4]
    manifest = json.loads((sample / "run-manifest.json").read_text())
    entry = next(item for item in manifest["episodes"] if item["case"] == "clean")
    if change == "truth_digest":
        name, value = "private/truth.json", [{"changed": True}]
    elif change == "public_digest":
        name, value = "public/manifest.json", {"episode_id": "changed"}
    else:
        name = "private/provenance.json"
        value = json.loads((sample / "episodes/clean" / name).read_text())
        value["master_seed"] = 123
    write(sample / "episodes/clean" / name, json.dumps(value))
    entry["files"][name] = release._sha(sample / "episodes/clean" / name)
    write(sample / "run-manifest.json", json.dumps(manifest))
    receipt = json.loads((sample / "COMPLETE.json").read_text())
    receipt["sha256"]["run-manifest.json"] = release._sha(sample / "run-manifest.json")
    write(sample / "COMPLETE.json", json.dumps(receipt))
    with pytest.raises(release.ReleaseError, match=reason):
        build(inputs, tmp_path / "modified")


def test_missing_open_gold_file_cannot_publish_completed_sample(inputs, tmp_path):
    (inputs[4] / "episodes/clean/private/truth.json").unlink()
    output = tmp_path / "missing-gold"
    with pytest.raises((release.ReleaseError, OSError)):
        build(inputs, output)
    assert not output.exists()


def web_inputs(inputs):
    root, _, _, metadata, sample = inputs
    write(root / "pyproject.toml", '[project]\nversion = "0.6.0"\n')
    for name in release.WEB_REQUIRED:
        write(root / name, "// frontend fixture\n" if name.endswith((".js", ".mjs")) else "web fixture\n")
    write(root / "tests/web_fixture.test.mjs", "// javascript regression fixture\n")
    metadata["source_inventory"] = release._runtime_hashes(root, release._source_paths(root))
    summary = json.loads((sample / "summary.json").read_text())
    summary["software_version"] = "0.6.0"
    write(sample / "summary.json", json.dumps(summary))
    provenance = json.loads((sample / "run-manifest.json").read_text())
    provenance.update(software_version="0.6.0", source_sha256=metadata["source_inventory"])
    write(sample / "run-manifest.json", json.dumps(provenance))
    receipt = json.loads((sample / "COMPLETE.json").read_text())
    receipt["sha256"] = {name: release._sha(sample / name) for name in release.SAMPLE_FILES - {"COMPLETE.json"}}
    write(sample / "COMPLETE.json", json.dumps(receipt))
    return inputs


def test_new_release_includes_every_frontend_file_and_javascript_test_in_hash_binding(inputs, tmp_path):
    web_inputs(inputs)
    bundle = Path(build(inputs, tmp_path / "web-release")["bundle"])
    manifest = release.verify_bundle(bundle)
    for name in release.WEB_ASSETS | {"tests/web_fixture.test.mjs"}:
        assert (bundle / name).read_bytes() == (inputs[0] / name).read_bytes()
        assert manifest["source_inventory"][name] == release._sha(bundle / name)
    assert (bundle / "demo").stat().st_mode & 0o111
    assert "scripts" in (bundle / "demo").read_text() and "--release-bundle" in (bundle / "demo").read_text()
    write(bundle / "src/fin_audit/web_assets/app.js", "modified frontend")
    with pytest.raises(release.ReleaseError, match="release_inventory_mismatch"):
        release.verify_bundle(bundle)


@pytest.mark.parametrize("missing", ["scripts/demo.py", *sorted(release.WEB_ASSETS)])
def test_new_release_cannot_omit_frontend_or_launcher(inputs, tmp_path, missing):
    web_inputs(inputs)
    (inputs[0] / missing).unlink()
    with pytest.raises(release.ReleaseError, match="missing_required_web_release_source"):
        build(inputs, tmp_path / "missing-web")


def test_changed_frontend_after_image_build_is_not_packaged_as_matching_image(inputs, tmp_path):
    web_inputs(inputs)
    write(inputs[0] / "src/fin_audit/web_assets/style.css", "changed after image freeze")
    with pytest.raises(release.ReleaseError, match="image_and_package_source_mismatch"):
        build(inputs, tmp_path / "stale-image")


def test_amd64_archive_and_loader_preserve_the_actual_platform_identity(inputs, tmp_path, monkeypatch):
    root, archive, _, metadata, sample = inputs
    identifier = image_archive(archive, architecture="amd64")
    metadata.update(image_id=identifier, architecture="amd64")
    result = release.build_release(root, tmp_path / "amd64", archive, identifier, metadata, sample)
    bundle = Path(result["bundle"])
    assert result["archive"].endswith("linux-amd64.tar.gz")
    manifest = release.verify_bundle(bundle)
    assert manifest["runtime"]["architecture"] == "amd64"

    def docker(command, **kwargs):
        if "load" in command:
            return b"loaded"
        return json.dumps([{"Id": identifier, "Os": "linux", "Architecture": "arm64",
                            "RootFS": {"Layers": manifest["runtime"]["rootfs_diff_ids"]}}]).encode()

    monkeypatch.setattr(release, "_docker_result", docker)
    with pytest.raises(release.ReleaseError, match="loaded_image_identity_mismatch"):
        release.load_verified_image(bundle, ["docker"])
