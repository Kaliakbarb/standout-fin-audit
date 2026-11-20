"""Standard-library release inventory, safe extraction and pinned offline loading.

Checksums prove consistency against the manifest. The separately distributed
archive SHA-256 is the trust anchor; this module does not claim signatures.
"""

from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import json
import io
import os
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath


MAX_BYTES = 1_000_000_000
MAX_FILES = 12_000
MANIFEST = "release-manifest.json"
GENERATED = {"docker/requirements.lock.txt", "docker/sandbox-build.json"}
ROOT_FILES = {"README.md", "LICENSE", "pyproject.toml", "uv.lock", ".python-version", ".dockerignore", ".gitignore"}
RUNTIME_ROOTS = {"src", "tests", "scripts", "docker", "schemas", "configs", "datasets"}
EXTENSIONS = {
    "src": {".py", ".html", ".css", ".js", ".mjs"}, "tests": {".py", ".mjs"}, "scripts": {".py", ""},
    "docker": {".py", ""}, "schemas": {".json"}, "configs": {".json"},
    "datasets": {".json", ".jsonl", ".csv", ".md"},
    "docs": {".md", ".txt", ".png", ".json", ".xml", ".html", ".py", ".diff"},
}
SAMPLE_FILES = {"analysis.json", "evaluation.json", "run-manifest.json", "trajectories.jsonl", "COMPLETE.json", "summary.json"}
PUBLIC_FILES = {"manifest.json", "policy.json", "ledger.csv", "invoices.jsonl", "payments.jsonl", "fx.csv"}
PRIVATE_FILES = {"truth.json", "provenance.json"}
EPISODE_FILES = {"public/" + name for name in PUBLIC_FILES} | {"private/" + name for name in PRIVATE_FILES}
LEDGER_COLUMNS = ("row_id", "timestamp", "account", "debit_minor", "credit_minor", "currency", "entity_id", "invoice_id", "payment_id")
FX_COLUMNS = ("date", "source_currency", "target_currency", "rate")
SHOWCASE_CASES = {"clean", "duplicate_only", "fx_only", "masked_both", "transformed_masked", "allocation_fx",
                  "allocation_fx_repaired", "allocation_cutoff", "allocation_cutoff_repaired"}
REQUIRED_DOCS = {"docs/DESIGN_NOTE.md", "docs/DEMO_GUIDE.md", "docs/THIRD_PARTY_NOTICES.md", "docs/RELEASE_GUIDE.md",
                 "docs/REQUIREMENTS.md", "docs/IMPLEMENTATION_PLAN.md", "docs/CONFORMANCE.md", "docs/STAGE2_CONTRACT.md",
                 "docs/STAGE3_PROTOCOL.md", "docs/STAGE4_CONTRACT.md"} | {f"docs/STAGE{stage}_STATUS.md" for stage in range(1, 5)}
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
WEB_ASSETS = {"src/fin_audit/web_assets/" + name for name in
              ("index.html", "style.css", "app.js", "lesson.mjs", "replay.mjs")}
WEB_REQUIRED = WEB_ASSETS | {"src/fin_audit/web.py", "src/fin_audit/web_readiness.py",
                            "scripts/demo.py", "docs/WEB_DEMO.md"}


class ReleaseError(ValueError):
    pass


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _bytes_sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json(path: Path, maximum: int = 16 * 1024 * 1024):
    if path.stat().st_size > maximum:
        raise ReleaseError("oversized_json")
    return _json_bytes(path.read_bytes())


def _json_bytes(data: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ReleaseError("duplicate_json_key")
            result[key] = value
        return result

    def nonfinite(value):
        raise ReleaseError("nonfinite_json")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=nonfinite)


def _write_json(path: Path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n", encoding="utf-8")


def _name(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value or ":" in value:
        raise ReleaseError("unsafe_member_name")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {".", "..", ""} for part in value.split("/")) or str(path) != value:
        raise ReleaseError("unsafe_member_name")
    return value


def _new_leaf(path: Path) -> Path:
    path = path.absolute()
    if path.exists() or path.is_symlink():
        raise FileExistsError("release_output_already_exists")
    # Parent directories belong to the caller. Do not silently follow a
    # dangling or existing symlink anywhere in a requested destination.
    for parent in path.parents:
        if parent.is_symlink():
            raise ReleaseError("symlink_output_parent")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _regular(path: Path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ReleaseError("release_requires_regular_unlinked_files")
    return info


def _walk(root: Path):
    files, directories = [], []
    for current, names, leaves in os.walk(root, followlinks=False):
        for name in sorted(names):
            path = Path(current) / name
            if path.is_symlink():
                raise ReleaseError("symlink_directory")
            directories.append(path)
        for name in sorted(leaves):
            path = Path(current) / name
            _regular(path)
            files.append(path)
    return sorted(files), sorted(directories)


def _source_paths(root: Path) -> list[Path]:
    paths = [root / name for name in ROOT_FILES if (root / name).is_file()]
    for directory, extensions in EXTENSIONS.items():
        for current, names, files in os.walk(root / directory, followlinks=False):
            names[:] = sorted(name for name in names if name != "__pycache__" and not name.startswith("."))
            if any((Path(current) / name).is_symlink() for name in names):
                raise ReleaseError("symlink_source_directory")
            for name in sorted(files):
                path = Path(current) / name
                if name.startswith(".") or path.suffix not in extensions:
                    continue
                _regular(path)
                paths.append(path)
    required = {"README.md", "LICENSE", "pyproject.toml", "uv.lock", ".python-version", "scripts/verify", "scripts/release.py", "scripts/release_support.py"} | REQUIRED_DOCS
    available = {path.relative_to(root).as_posix() for path in paths}
    if not required.issubset(available):
        raise ReleaseError("missing_required_release_source")
    version = _version(root, "pyproject.toml", r'^version\s*=\s*"([^"]+)"')
    if tuple(map(int, version.split("."))) >= (0, 6) and not WEB_REQUIRED.issubset(available):
        raise ReleaseError("missing_required_web_release_source")
    return sorted(paths)


def _runtime_hashes(root: Path, paths: list[Path]) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): _sha(path) for path in paths
            if path.relative_to(root).parts[0] in RUNTIME_ROOTS
            or path.relative_to(root).as_posix() in {"pyproject.toml", "uv.lock", ".python-version", "LICENSE"}}


def inspect_image_archive(path: Path, expected_image_id: str) -> dict:
    """Read Docker save metadata without extracting or trusting cached tags."""
    _regular(path)
    if not isinstance(expected_image_id, str) or not _IMAGE.fullmatch(expected_image_id) or path.stat().st_size >= MAX_BYTES:
        raise ReleaseError("invalid_image_id_or_archive_size")
    with tarfile.open(path, "r:*") as archive:
        members = _tar_members(archive)
        if "manifest.json" not in members or not members["manifest.json"].isfile():
            raise ReleaseError("missing_docker_save_manifest")
        manifest = _json_bytes(_tar_read(archive, members["manifest.json"], 1024 * 1024))
        if not isinstance(manifest, list) or len(manifest) != 1 or not isinstance(manifest[0], dict):
            raise ReleaseError("image_archive_must_contain_one_image")
        config_name = _name(manifest[0].get("Config"))
        config_member = members.get(config_name)
        if config_member is None or not config_member.isfile():
            raise ReleaseError("missing_image_config")
        config_bytes = _tar_read(archive, config_member, 1024 * 1024)
        config_id = "sha256:" + _bytes_sha(config_bytes)
        config = _json_bytes(config_bytes)
        if config.get("os") != "linux" or config.get("architecture") not in {"amd64", "arm64"}:
            raise ReleaseError("unsupported_image_platform")
        layers = manifest[0].get("Layers")
        if not isinstance(layers, list) or not layers or any(_name(layer) not in members or not members[layer].isfile() for layer in layers):
            raise ReleaseError("broken_image_layers")
        rootfs = config.get("rootfs", {})
        diff_ids = rootfs.get("diff_ids")
        if (rootfs.get("type") != "layers" or not isinstance(diff_ids, list) or len(diff_ids) != len(layers)
                or any(not isinstance(value, str) or not _IMAGE.fullmatch(value) for value in diff_ids)):
            raise ReleaseError("invalid_image_rootfs")
        descriptors = None
        if expected_image_id != config_id:
            # Docker 29's containerd store exposes an OCI manifest digest;
            # legacy stores expose the config digest. Bind both to sealed
            # bytes instead of assuming they are interchangeable identifiers.
            preferred_name = "blobs/sha256/" + expected_image_id.split(":", 1)[1]
            preferred = members.get(preferred_name)
            if preferred is None or not preferred.isfile():
                raise ReleaseError("image_config_id_mismatch")
            preferred_bytes = _tar_read(archive, preferred, 1024 * 1024)
            if "sha256:" + _bytes_sha(preferred_bytes) != expected_image_id:
                raise ReleaseError("image_manifest_digest_mismatch")
            preferred_json = _json_bytes(preferred_bytes)
            descriptor = preferred_json.get("config", {})
            descriptors = preferred_json.get("layers")
            if (preferred_json.get("schemaVersion") != 2 or preferred_json.get("mediaType") not in {
                    "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"}
                    or descriptor.get("digest") != config_id or descriptor.get("size") != len(config_bytes)
                    or not isinstance(descriptors, list) or len(descriptors) != len(layers)):
                raise ReleaseError("image_manifest_config_binding_mismatch")
        uncompressed_bytes = 0
        for index, name in enumerate(layers):
            member = members[name]
            raw_digest, _ = _tar_digest(archive, member)
            if descriptors is not None:
                descriptor = descriptors[index]
                if (not isinstance(descriptor, dict) or descriptor.get("digest") != raw_digest
                        or descriptor.get("size") != member.size or descriptor.get("mediaType") not in {
                            "application/vnd.oci.image.layer.v1.tar", "application/vnd.oci.image.layer.v1.tar+gzip",
                            "application/vnd.docker.image.rootfs.diff.tar", "application/vnd.docker.image.rootfs.diff.tar.gzip"}):
                    raise ReleaseError("image_manifest_layer_binding_mismatch")
            digest, size = _tar_digest(archive, member, uncompressed=True)
            uncompressed_bytes += size
            if digest != diff_ids[index] or uncompressed_bytes >= MAX_BYTES:
                raise ReleaseError("image_rootfs_diff_mismatch_or_limit")
        return {"image_id": expected_image_id, "config_id": config_id, "rootfs_diff_ids": diff_ids,
                "os": "linux", "architecture": config["architecture"],
                "variant": config.get("variant", "")}


def _tar_members(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    result, total = {}, 0
    for member in archive:
        name = _name(member.name.rstrip("/") if member.isdir() else member.name)
        if member.name not in {name, name + "/"}:
            raise ReleaseError("noncanonical_tar_member_name")
        if name in result or not (member.isfile() or member.isdir()) or getattr(member, "sparse", None):
            raise ReleaseError("duplicate_or_unsafe_tar_member")
        if member.size < 0 or member.mode & 0o7000:
            raise ReleaseError("unsafe_tar_size_or_mode")
        total += member.size
        if len(result) >= MAX_FILES or total >= MAX_BYTES:
            raise ReleaseError("archive_payload_limit")
        result[name] = member
    return result


def _tar_read(archive, member, maximum):
    if member.size > maximum:
        raise ReleaseError("oversized_tar_metadata")
    handle = archive.extractfile(member)
    if handle is None:
        raise ReleaseError("unreadable_tar_member")
    with handle:
        data = handle.read(maximum + 1)
    if len(data) != member.size:
        raise ReleaseError("truncated_tar_member")
    return data


def _tar_digest(archive, member, *, uncompressed=False):
    handle = archive.extractfile(member)
    if handle is None:
        raise ReleaseError("unreadable_image_layer")
    digest, size = hashlib.sha256(), 0
    with handle:
        compressed = handle.read(2) == b"\x1f\x8b"
        handle.seek(0)
        stream = gzip.GzipFile(fileobj=handle) if uncompressed and compressed else handle
        try:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(block)
                if size >= MAX_BYTES:
                    raise ReleaseError("image_layer_size_limit")
                digest.update(block)
        finally:
            if stream is not handle:
                stream.close()
    return "sha256:" + digest.hexdigest(), size


def _no_closed_keys(value):
    if isinstance(value, dict):
        if {"master_seed", "closed_seed", "holdout_seeds"} & value.keys():
            raise ReleaseError("closed_seed_in_public_document")
        for child in value.values():
            _no_closed_keys(child)
    elif isinstance(value, list):
        for child in value:
            _no_closed_keys(child)


def _copy(path: Path, destination: Path, *, private=False):
    _regular(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, destination)
    mode = 0o600 if private else (0o755 if path.stat().st_mode & 0o111 else 0o644)
    destination.chmod(mode)


def _canonical_digest(value):
    return _bytes_sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def _sample_csv(path: Path, mandatory: tuple[str, ...]):
    reader = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"), newline=""), strict=True)
    fields = reader.fieldnames
    if not fields or len(set(fields)) != len(fields) or not set(mandatory).issubset(fields):
        raise ReleaseError("invalid_showcase_csv_header")
    rows = []
    for raw in reader:
        if None in raw or any(value is None for value in raw.values()):
            raise ReleaseError("invalid_showcase_csv_width")
        row = {}
        for key, value in raw.items():
            if key in mandatory:
                if key in {"debit_minor", "credit_minor"}:
                    if not re.fullmatch(r"0|[1-9][0-9]*", value):
                        raise ReleaseError("invalid_showcase_csv_integer")
                    row[key] = int(value)
                elif key in {"invoice_id", "payment_id"}:
                    row[key] = None if value == "null" else value
                else:
                    row[key] = value
            elif value != "":
                row[key] = _json_bytes(value.encode("utf-8"))
        rows.append(row)
    return rows


def _sample_public(directory: Path):
    return {"manifest": _json(directory / "manifest.json"), "policy": _json(directory / "policy.json"),
            "ledger": _sample_csv(directory / "ledger.csv", LEDGER_COLUMNS), "fx": _sample_csv(directory / "fx.csv", FX_COLUMNS),
            **{table: [_json_bytes(line) for line in (directory / (table + ".jsonl")).read_bytes().split(b"\n") if line]
               for table in ("invoices", "payments")}}


def _copy_sample(source: Path, destination: Path, version: str, source_inventory: dict):
    """Preserve the complete OPEN demonstration receipt, including gold.

    Its documented seed 19 and presentation seed are reviewer material, like
    the open train/dev gold. These files stay outside the agent jail. Closed
    G3 control cohorts and master-seed receipts are excluded from this scope.
    """
    files, _ = _walk(source)
    if not SAMPLE_FILES.issubset({path.relative_to(source).as_posix() for path in files}):
        raise ReleaseError("incomplete_open_showcase")
    complete = _json(source / "COMPLETE.json")
    if complete.get("showcase_version") != "1.0" or complete.get("completed") is not True or complete.get("passed") is not True:
        raise ReleaseError("showcase_not_successfully_completed")
    for name in SAMPLE_FILES - {"COMPLETE.json"}:
        if complete.get("sha256", {}).get(name) != _sha(source / name):
            raise ReleaseError("showcase_receipt_hash_mismatch")
    summary, evaluation = _json(source / "summary.json"), _json(source / "evaluation.json")
    provenance, analysis = _json(source / "run-manifest.json"), _json(source / "analysis.json")
    if (summary.get("showcase_version") != "1.0" or summary.get("software_version") != version
            or summary.get("passed") is not True or summary.get("scope") != "post_run_only" or summary.get("profile") != "secure"
            or summary.get("case_count") != 9 or not isinstance(evaluation, list) or len(evaluation) != 27
            or summary.get("runs") != evaluation or summary.get("execution_counts") != {"ordinary_workers": 18, "run_episode_calls": 27, "trusted_oracles": 9}
            or not summary.get("checks") or any(value is not True for value in summary["checks"].values())
            or provenance.get("source_sha256") != source_inventory or provenance.get("software_version") != version
            or analysis.get("showcase_version") != "1.0" or analysis.get("scope") != "post_run_only"):
        raise ReleaseError("invalid_or_stale_open_showcase")
    entries = provenance.get("episodes")
    if not isinstance(entries, list) or len(entries) != 9 or {entry.get("case") for entry in entries} != SHOWCASE_CASES:
        raise ReleaseError("unexpected_showcase_cases")
    expected_runs = {(case, policy) for case in SHOWCASE_CASES for policy in ("simple", "silent", "oracle")}
    if {(run.get("case"), run.get("policy")) for run in evaluation} != expected_runs:
        raise ReleaseError("unexpected_showcase_runs")
    for entry in entries:
        directory = source / "episodes" / entry["case"]
        if set(entry.get("files", {})) != EPISODE_FILES:
            raise ReleaseError("incomplete_showcase_episode_manifest")
        for name in EPISODE_FILES:
            if entry["files"][name] != _sha(directory / name):
                raise ReleaseError("showcase_episode_file_hash_mismatch")
        public = _sample_public(directory / "public")
        truth, private_provenance = (_json(directory / "private" / name) for name in ("truth.json", "provenance.json"))
        public_digest = _canonical_digest(public)
        if (public_digest != entry.get("public_sha256") or _canonical_digest(truth) != entry.get("truth_sha256")
                or public["manifest"].get("episode_id") != entry.get("episode_id")
                or not isinstance(private_provenance, dict) or private_provenance.get("public_sha256", public_digest) != public_digest):
            raise ReleaseError("showcase_canonical_episode_digest_mismatch")
        _no_closed_keys(public)
        _no_closed_keys(truth)
        _no_closed_keys(private_provenance)
    run_ids = {run.get("trajectory_run_id") for run in evaluation}
    if len(run_ids) != 27 or any(not isinstance(run_id, str) for run_id in run_ids):
        raise ReleaseError("invalid_showcase_run_ids")
    seen = set()
    with (source / "trajectories.jsonl").open("rb") as stream:
        for line in stream:
            if len(line) > 2 * 1024 * 1024:
                raise ReleaseError("oversized_showcase_trajectory_row")
            row = _json_bytes(line)
            _no_closed_keys(row)
            if row.get("run_id") not in run_ids or row.get("reward") != 0 or row.get("info") != {}:
                raise ReleaseError("invalid_or_unmasked_showcase_trajectory")
            seen.add(row["run_id"])
    if seen != run_ids:
        raise ReleaseError("missing_showcase_trajectory")
    for path in files:
        relative = path.relative_to(source)
        name = relative.as_posix()
        allowed = name in SAMPLE_FILES or (len(relative.parts) == 4 and relative.parts[0] == "episodes"
                  and relative.parts[1] in SHOWCASE_CASES and ((relative.parts[2] == "public" and relative.parts[3] in PUBLIC_FILES)
                  or (relative.parts[2] == "private" and relative.parts[3] in PRIVATE_FILES)))
        if allowed:
            if path.suffix == ".json":
                _no_closed_keys(_json(path))
            _copy(path, destination / relative, private="private" in relative.parts)
    viewer = source.parent / "showcase.html"
    if viewer.is_file():
        _copy(viewer, destination / "showcase.html")


def _inventory(root: Path):
    files, directories = _walk(root)
    return ([{"path": path.relative_to(root).as_posix(), "size": path.stat().st_size,
              "sha256": _sha(path), "mode": stat.S_IMODE(path.stat().st_mode)} for path in files if path.relative_to(root).as_posix() != MANIFEST],
            [{"path": path.relative_to(root).as_posix(), "mode": stat.S_IMODE(path.stat().st_mode)} for path in directories])


def verify_bundle(bundle: Path) -> dict:
    """Fail closed on file/hash/mode/inventory mismatch, before Docker runs."""
    bundle = bundle.absolute()
    if bundle.is_symlink() or not bundle.is_dir():
        raise ReleaseError("invalid_bundle_directory")
    _regular(bundle / MANIFEST)
    manifest = _json(bundle / MANIFEST)
    if not isinstance(manifest, dict) or type(manifest.get("format_version")) is not int or manifest.get("format_version") != 1:
        raise ReleaseError("unsupported_release_manifest")
    expected_keys = {"format_version", "software_version", "schema_version", "corpus_version", "protocol_version", "scope",
                     "runtime", "licenses", "files", "directories", "source_inventory", "sample", "reproducibility"}
    if set(manifest) != expected_keys or any(not isinstance(manifest.get(name), str) or not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", manifest[name])
            for name in ("software_version", "schema_version", "corpus_version", "protocol_version")):
        raise ReleaseError("invalid_release_manifest_contract")
    if (manifest["scope"] != "public-source-open-corpus-and-open-showcase"
            or manifest["sample"] != {"scope": "open-showcase", "included": True}
            or manifest["reproducibility"] != {"mtime": 0, "uid": 0, "gid": 0, "archive_format": "tar.gz", "closed_holdout_seeds_included": False}):
        raise ReleaseError("invalid_release_scope_or_reproducibility")
    expected_files, expected_dirs = manifest.get("files"), manifest.get("directories")
    if not isinstance(expected_files, list) or not isinstance(expected_dirs, list):
        raise ReleaseError("invalid_release_inventory")
    paths = []
    for entry in expected_files:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256", "mode"}:
            raise ReleaseError("invalid_file_inventory_entry")
        paths.append(_name(entry["path"]))
        if type(entry["size"]) is not int or entry["size"] < 0 or not isinstance(entry["sha256"], str) or not _SHA.fullmatch(entry["sha256"]):
            raise ReleaseError("invalid_file_size_or_hash")
        if type(entry["mode"]) is not int or entry["mode"] not in {0o600, 0o644, 0o755}:
            raise ReleaseError("invalid_file_mode")
    dir_paths = []
    for entry in expected_dirs:
        if not isinstance(entry, dict) or set(entry) != {"path", "mode"} or type(entry["mode"]) is not int or entry["mode"] not in {0o700, 0o755}:
            raise ReleaseError("invalid_directory_inventory_entry")
        dir_paths.append(_name(entry["path"]))
    if len(set(paths)) != len(paths) or len(set(dir_paths)) != len(dir_paths) or set(paths) & set(dir_paths) or MANIFEST in paths:
        raise ReleaseError("duplicate_inventory_path")
    files, directories = _inventory(bundle)
    if files != expected_files or directories != expected_dirs or stat.S_IMODE((bundle / MANIFEST).stat().st_mode) != 0o644:
        raise ReleaseError("release_inventory_mismatch")
    if len(files) > MAX_FILES or sum(entry["size"] for entry in files) + (bundle / MANIFEST).stat().st_size >= MAX_BYTES:
        raise ReleaseError("release_size_limit")
    runtime = manifest.get("runtime", {})
    if not isinstance(runtime, dict) or set(runtime) != {"image_id", "config_id", "rootfs_diff_ids", "archive", "archive_sha256", "os", "architecture", "variant"} or runtime.get("archive") != "runtime/image.tar":
        raise ReleaseError("unexpected_runtime_archive_path")
    if _sha(bundle / "runtime/image.tar") != runtime.get("archive_sha256"):
        raise ReleaseError("runtime_archive_hash_mismatch")
    actual = inspect_image_archive(bundle / "runtime/image.tar", runtime.get("image_id", ""))
    if any(runtime.get(key) != value for key, value in actual.items()):
        raise ReleaseError("runtime_archive_metadata_mismatch")
    hashes = {entry["path"]: entry["sha256"] for entry in files}
    source_inventory = manifest.get("source_inventory")
    if not isinstance(source_inventory, dict) or not source_inventory or any(hashes.get(name) != digest for name, digest in source_inventory.items()):
        raise ReleaseError("source_inventory_mismatch")
    if source_inventory != _runtime_hashes(bundle, _source_paths(bundle)):
        raise ReleaseError("incomplete_source_inventory")
    return manifest


def _docker_result(command, *, maximum=16 * 1024 * 1024, timeout=120, input_data: bytes | None = None):
    # File-backed output avoids an unbounded communicate buffer on the host.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as input_file:
        if input_data is not None:
            if not isinstance(input_data, bytes) or len(input_data) > 100_000:
                raise ReleaseError("docker_input_limit")
            input_file.write(input_data)
            input_file.seek(0)
        process = subprocess.Popen(command, stdin=input_file if input_data is not None else subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True, close_fds=True)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ReleaseError("docker_command_timeout")
                if output.tell() > maximum:
                    raise ReleaseError("docker_output_limit")
                try:
                    process.wait(timeout=min(remaining, 0.05))
                except subprocess.TimeoutExpired:
                    pass
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                if process.poll() is None:
                    raise
            process.wait(timeout=3)
        length = output.tell()
        output.seek(0)
        data = output.read(maximum + 1)
    if length > maximum:
        raise ReleaseError("docker_output_limit")
    if process.returncode:
        raise ReleaseError("docker_command_failed:" + data[-2000:].decode("utf-8", "replace"))
    return data


def load_verified_image(bundle: Path, docker: list[str]) -> dict:
    """Always load the verified archive; execute by ID rather than a cached tag."""
    manifest = verify_bundle(bundle)
    runtime = manifest["runtime"]
    _docker_result(docker + ["image", "load", "-i", str((bundle / runtime["archive"]).absolute())])
    for identifier in dict.fromkeys((runtime["image_id"], runtime["config_id"])):
        try:
            inspected = _json_bytes(_docker_result(docker + ["image", "inspect", identifier]))
        except ReleaseError as error:
            # A legacy store may not resolve the OCI manifest ID. Only a
            # failed exact lookup permits the second sealed identifier.
            if str(error).startswith("docker_command_failed:"):
                continue
            raise
        if not isinstance(inspected, list) or len(inspected) != 1 or not isinstance(inspected[0], dict):
            raise ReleaseError("loaded_image_identity_mismatch")
        actual = inspected[0]
        if (actual.get("Id") not in {runtime["image_id"], runtime["config_id"]} or actual.get("Os") != "linux"
                or actual.get("Architecture") != runtime["architecture"]
                or actual.get("RootFS", {}).get("Layers") != runtime["rootfs_diff_ids"]):
            raise ReleaseError("loaded_image_identity_mismatch")
        return {**manifest, "loaded_image_id": actual["Id"]}
    raise ReleaseError("loaded_image_not_found_by_sealed_ids")


_INSPECTOR = r'''import base64, hashlib, importlib.metadata, json, re, subprocess, sys, sysconfig
from pathlib import Path
root = Path('/app')
files = [root / name for name in ('pyproject.toml', 'uv.lock', '.python-version', 'LICENSE')]
for directory in ('src', 'tests', 'scripts', 'docker', 'schemas', 'configs', 'datasets'):
    files.extend(path for path in (root / directory).rglob('*') if '__pycache__' not in path.parts and path.is_file() and path.suffix not in {'.pyc', '.pyo'})
if any(path.is_symlink() for path in files): raise ValueError('image_source_symlink')
result = {'source_inventory': {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(files)}, 'licenses': [], 'license_files': {}, 'build_evidence': {}}
for name in ('docker/requirements.lock.txt', 'docker/sandbox-build.json'):
    path=root/name
    if path.is_file(): result['build_evidence'][name]=base64.b64encode(path.read_bytes()).decode()
def safe(value): return re.sub('[^a-zA-Z0-9_.-]', '_', value)
def add(path, name):
    if path.is_file():
        data=path.read_bytes()
        if len(data)>2*1024*1024: raise ValueError('license_text_limit')
        result['license_files'][name]=base64.b64encode(data).decode()
        return {'path': 'runtime/licenses/'+name, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
    return None
for dist in sorted(importlib.metadata.distributions(), key=lambda item: item.metadata.get('Name','')):
    name=dist.metadata.get('Name','unknown'); evidence=[]
    for index,item in enumerate(dist.files or []):
        if any(word in Path(str(item)).name.lower() for word in ('license','copying','notice')):
            entry=add(Path(dist.locate_file(item)), 'python/'+safe(name)+'/'+str(index)+'-'+safe(Path(str(item)).name))
            if entry: evidence.append(entry)
    result['licenses'].append({'kind':'python-distribution','name':name,'version':dist.version,'declared_license':dist.metadata.get('License-Expression') or dist.metadata.get('License'),'texts':evidence})
cp=add(Path(sysconfig.get_path('stdlib'))/'LICENSE.txt', 'cpython/LICENSE.txt')
if cp is None: raise ValueError('missing_cpython_license_text')
result['licenses'].append({'kind':'runtime','name':'CPython','version':sys.version.split()[0],'texts':[cp] if cp else []})
common=[]
for path in sorted(Path('/usr/share/common-licenses').iterdir()):
    entry=add(path, 'debian-common/'+safe(path.name))
    if entry: common.append(entry)
result['licenses'].append({'kind':'license-collection','name':'Debian common licenses','version':'from-pinned-image','texts':common})
packages=subprocess.check_output(['dpkg-query','-W','-f=${binary:Package}\t${Version}\n'], text=True)
for line in sorted(packages.splitlines()):
    name,version=line.split('\t',1); path=Path('/usr/share/doc')/name.split(':')[0]/'copyright'
    entry=add(path, 'debian/'+safe(name)+'/copyright')
    result['licenses'].append({'kind':'debian-package','name':name,'version':version,'texts':[entry] if entry else []})
print(json.dumps(result, sort_keys=True))
'''


def collect_image_metadata(docker: list[str], image_id: str) -> dict:
    if not isinstance(image_id, str) or not _IMAGE.fullmatch(image_id):
        raise ReleaseError("expected_exact_image_id")
    inspected = _json_bytes(_docker_result(docker + ["image", "inspect", image_id]))
    if not isinstance(inspected, list) or len(inspected) != 1 or inspected[0].get("Id") != image_id or inspected[0].get("Os") != "linux":
        raise ReleaseError("unexpected_image_inspection")
    data = _json_bytes(_docker_result(docker + ["run", "--rm", "--pull=never", "--network=none", "--read-only",
        "--cap-drop=ALL", "--pids-limit=16", "--memory=256m", "--cpus=1", image_id, "python", "-I", "-B", "-c", _INSPECTOR],
        maximum=32 * 1024 * 1024))
    data.update(image_id=image_id, os="linux", architecture=inspected[0]["Architecture"], variant=inspected[0].get("Variant", ""))
    return data


def _version(root: Path, filename: str, pattern: str):
    match = re.search(pattern, (root / filename).read_text(encoding="utf-8"), re.MULTILINE)
    if match is None:
        raise ReleaseError("missing_version_declaration")
    return match.group(1)


def build_release(root: Path, output: Path, image_archive: Path, image_id: str,
                  image_metadata: dict, sample_dir: Path | None = None) -> dict:
    """Create an exclusive deterministic public bundle and tar.gz after freeze."""
    output = _new_leaf(output)
    root = root.resolve()
    if sample_dir is None:
        raise ReleaseError("approved_open_showcase_required")
    image = inspect_image_archive(image_archive, image_id)
    if any(image_metadata.get(key) != image[key] for key in ("image_id", "os", "architecture", "variant")):
        raise ReleaseError("image_metadata_disagrees_with_archive")
    paths = _source_paths(root)
    source_inventory = _runtime_hashes(root, paths)
    recorded = image_metadata.get("source_inventory", {})
    if not isinstance(recorded, dict) or {key: value for key, value in recorded.items() if key not in GENERATED} != source_inventory:
        raise ReleaseError("image_and_package_source_mismatch")
    stage = Path(tempfile.mkdtemp(prefix=".release-staging-", dir=output.parent))
    try:
        stage.chmod(0o700)
        bundle = stage / "bundle"
        bundle.mkdir(mode=0o755)
        for path in paths:
            relative = path.relative_to(root)
            if relative.parts[0] == "docs" and path.suffix == ".json":
                _no_closed_keys(_json(path))
            _copy(path, bundle / relative, private="private" in relative.parts)
        for directory in [path for path in bundle.rglob("*") if path.is_dir()]:
            directory.chmod(0o700 if "private" in directory.relative_to(bundle).parts else 0o755)
        launcher = bundle / "verify"
        launcher.write_text("#!/usr/bin/env python3\nimport os, sys\nfrom pathlib import Path\nos.execv(sys.executable, [sys.executable, '-B', str(Path(__file__).resolve().parent / 'scripts' / 'verify'), '--release-bundle', *sys.argv[1:]])\n", encoding="utf-8")
        launcher.chmod(0o755)
        if (bundle / "scripts/demo.py").is_file():
            demo = bundle / "demo"
            demo.write_text("#!/usr/bin/env python3\nimport os, sys\nfrom pathlib import Path\nos.execv(sys.executable, [sys.executable, '-B', str(Path(__file__).resolve().parent / 'scripts' / 'demo.py'), '--release-bundle', *sys.argv[1:]])\n", encoding="utf-8")
            demo.chmod(0o755)
        _copy(image_archive, bundle / "runtime/image.tar")
        (bundle / "runtime/image.tar").chmod(0o644)
        texts = image_metadata.get("license_files", {})
        if not isinstance(texts, dict) or not texts:
            raise ReleaseError("missing_runtime_license_texts")
        for name, encoded in sorted(texts.items()):
            name = _name(name)
            data = base64.b64decode(encoded, validate=True)
            if len(data) > 2 * 1024 * 1024:
                raise ReleaseError("license_text_limit")
            destination = bundle / "runtime/licenses" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            destination.chmod(0o644)
        _write_json(bundle / "runtime/licenses.json", {"image_id": image_id, "os": image["os"], "architecture": image["architecture"],
                    "scope": "Installed dependency declared licenses and shipped license/copyright texts; not a legal audit.",
                    "packages": image_metadata.get("licenses", [])})
        for name, encoded in image_metadata.get("build_evidence", {}).items():
            if name not in GENERATED or recorded.get(name) != _bytes_sha(base64.b64decode(encoded, validate=True)):
                raise ReleaseError("invalid_image_build_evidence")
            destination = bundle / "runtime/build-evidence" / Path(name).name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(base64.b64decode(encoded, validate=True))
            destination.chmod(0o644)
        version = _version(root, "pyproject.toml", r'^version\s*=\s*"([^"]+)"')
        _copy_sample(sample_dir, bundle / "sample", version, recorded)
        # New parent directories must be deterministic despite caller umask.
        for directory in [path for path in bundle.rglob("*") if path.is_dir()]:
            directory.chmod(0o700 if "private" in directory.relative_to(bundle).parts else 0o755)
        files, directories = _inventory(bundle)
        manifest = {
            "format_version": 1, "software_version": version,
            "schema_version": _version(root, "src/fin_audit/contracts.py", r'^SCHEMA_VERSION\s*=\s*"([^"]+)"'),
            "corpus_version": _json(root / "datasets/manifest.json")["corpus_version"],
            "protocol_version": _version(root, "src/fin_audit/contracts.py", r'^PROTOCOL_VERSION\s*=\s*"([^"]+)"'),
            "scope": "public-source-open-corpus-and-open-showcase",
            "runtime": {**image, "archive": "runtime/image.tar", "archive_sha256": _sha(image_archive)},
            "licenses": {"project": {"path": "LICENSE", "spdx": "MIT"}, "runtime_inventory": "runtime/licenses.json"},
            "files": files, "directories": directories, "source_inventory": source_inventory,
            "sample": {"scope": "open-showcase", "included": sample_dir is not None},
            "reproducibility": {"mtime": 0, "uid": 0, "gid": 0, "archive_format": "tar.gz", "closed_holdout_seeds_included": False},
        }
        _write_json(bundle / MANIFEST, manifest)
        (bundle / MANIFEST).chmod(0o644)
        verify_bundle(bundle)
        if _runtime_hashes(root, _source_paths(root)) != source_inventory:
            raise ReleaseError("source_changed_during_release_build")
        archive = stage / f"genesis-fin-audit-{version}-linux-{image['architecture']}.tar.gz"
        _tar_bundle(bundle, archive)
        if archive.stat().st_size >= MAX_BYTES:
            raise ReleaseError("release_archive_size_limit")
        archive_sha = _sha(archive)
        (stage / (archive.name + ".sha256")).write_text(f"{archive_sha}  {archive.name}\n", encoding="ascii")
        receipt = {"passed": True, "archive": archive.name, "archive_sha256": archive_sha,
                   "archive_bytes": archive.stat().st_size, "payload_bytes": sum(entry["size"] for entry in files),
                   "image_id": image_id, "manifest_sha256": _sha(bundle / MANIFEST)}
        _write_json(stage / "release-receipt.json", receipt)
        # Exclusive reservation prevents replacing a concurrently created path.
        output.mkdir(mode=0o700)
        try:
            os.rename(stage, output)
        except BaseException:
            output.rmdir()
            raise
        return {**receipt, "output": str(output), "bundle": str(output / "bundle")}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _tar_bundle(bundle: Path, archive: Path):
    files, directories = _walk(bundle)
    with archive.open("xb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as output:
            for path in sorted(files + directories, key=lambda item: item.relative_to(bundle).as_posix()):
                name = path.relative_to(bundle).as_posix()
                info = tarfile.TarInfo(name)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                info.mode = stat.S_IMODE(path.stat().st_mode)
                if path.is_dir():
                    info.type = tarfile.DIRTYPE
                    output.addfile(info)
                else:
                    info.size = path.stat().st_size
                    with path.open("rb") as stream:
                        output.addfile(info, stream)


def extract_release(archive_path: Path, output: Path, expected_sha256: str | None = None) -> Path:
    """Preflight every header and manually extract into an atomic new leaf."""
    output = _new_leaf(output)
    _regular(archive_path)
    if archive_path.stat().st_size >= MAX_BYTES:
        raise ReleaseError("release_archive_size_limit")
    if expected_sha256 is not None and (not _SHA.fullmatch(expected_sha256) or _sha(archive_path) != expected_sha256):
        raise ReleaseError("release_archive_checksum_mismatch")
    stage = Path(tempfile.mkdtemp(prefix=".release-extract-", dir=output.parent))
    try:
        with tarfile.open(archive_path, "r:*") as archive:
            members = _tar_members(archive)
            for name, member in members.items():
                parent_names = list(PurePosixPath(name).parents)[:-1]
                if any(str(parent) in members and not members[str(parent)].isdir() for parent in parent_names):
                    raise ReleaseError("file_directory_collision")
                if member.mode not in ({0o700, 0o755} if member.isdir() else {0o600, 0o644, 0o755}):
                    raise ReleaseError("unexpected_release_member_mode")
            for name, member in sorted(members.items()):
                destination = stage / name
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    destination.chmod(member.mode)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise ReleaseError("unreadable_tar_member")
                    with source, destination.open("xb") as target:
                        shutil.copyfileobj(source, target, 1024 * 1024)
                    destination.chmod(member.mode)
        verify_bundle(stage)
        output.mkdir(mode=0o700)
        try:
            os.rename(stage, output)
        except BaseException:
            output.rmdir()
            raise
        return output
    finally:
        if stage.exists():
            shutil.rmtree(stage)
