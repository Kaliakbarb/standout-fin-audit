#!/usr/bin/env python3
"""Assemble the final ZIP from one sealed release and its clean-run receipt.

Only an explicit public allowlist is copied. Private control seeds, raw docker
commands and the clean runner's working tree are never recursively packaged.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

from release_support import ReleaseError, verify_bundle


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def read_json(path: Path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024**2:
        raise ReleaseError("invalid_submission_receipt_file")
    return json.loads(path.read_text())


def checked_file(clean: Path, receipt: dict, relative: str) -> Path:
    path = clean / relative
    if (path.is_symlink() or any(parent.is_symlink() for parent in path.parents)
            or not path.is_file() or receipt.get("sha256", {}).get(relative) != digest(path)):
        raise ReleaseError("clean_artifact_hash_mismatch:" + relative)
    return path


def no_control_seeds(value) -> None:
    forbidden = {"master_seed", "seed", "episode_seed", "hidden_seed", "holdout_seed"}
    if isinstance(value, dict):
        if forbidden.intersection(value):
            raise ReleaseError("private_control_seed_in_public_receipt")
        for item in value.values():
            no_control_seeds(item)
    elif isinstance(value, list):
        for item in value:
            no_control_seeds(item)


def require_clean_volume_inventory(value: dict) -> None:
    observed = value.get("observed_output_volumes")
    if (value.get("passed") is not True or value.get("before_verification") != []
            or value.get("after_verification") != [] or not isinstance(observed, list)
            or len(observed) != 1 or not isinstance(observed[0], str)
            or re.fullmatch(r"[0-9a-f]{64}", observed[0]) is None):
        raise ReleaseError("output_volume_lifecycle_not_clean")


def redacted_containers(value: list, source_sha256: str) -> dict:
    public = copy.deepcopy(value)
    for container in public:
        config = container.get("Config", {})
        if "Cmd" in config:
            config["Cmd"] = ["[redacted: trusted verification command contains a private control seed]"]
        if "Env" in config:
            config["Env"] = ["[redacted: runtime environment]"]
    return {"scope": "Public redacted view of actual container configuration",
            "source_sha256": source_sha256, "redacted_fields": ["Config.Cmd", "Config.Env if present"],
            "containers": public}


def check_seed_values(files: list[Path], seeds: list[int]) -> dict:
    """Scan long private decimal tokens without logging any token values.

    Short integers are not distinguishable from ordinary counts. Structural
    seed-key rejection and the explicit copy/redaction allowlist cover those.
    """
    tokens = {str(value).encode("ascii") for value in seeds if type(value) is int and len(str(value)) >= 12}
    for path in files:
        if path.suffix not in {".json", ".xml", ".md", ".html"}:
            continue
        data = path.read_bytes()
        if any(token in data for token in tokens):
            raise ReleaseError("private_seed_value_in_public_sidecar")
    return {"long_decimal_tokens_checked": len(tokens), "minimum_digits": 12, "matches": 0}


def write_json(path: Path, value) -> None:
    no_control_seeds(value)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n")
    path.chmod(0o644)


def build_submission(release: Path, clean: Path, output: Path) -> dict:
    release, clean = release.resolve(strict=True), clean.resolve(strict=True)
    bundle = release / "bundle"
    manifest = verify_bundle(bundle)
    receipt = read_json(clean / "COMPLETE.json")
    version = manifest["software_version"]
    runtime = manifest["runtime"]
    if (receipt.get("completed") is not True or receipt.get("passed") is not True
            or receipt.get("software_version") != version or receipt.get("runtime") != runtime):
        raise ReleaseError("clean_receipt_does_not_match_release")
    platform = receipt.get("execution_platform", {})
    if (runtime.get("architecture") != "amd64" or platform.get("target_platform") != "linux/amd64"
            or platform.get("application", {}).get("architecture") not in {"amd64", "x86_64"}):
        raise ReleaseError("submission_requires_verified_linux_amd64")
    junit = receipt.get("junit", {})
    if not junit.get("tests") or any(junit.get(name) != 0 for name in ("errors", "failures", "skipped")):
        raise ReleaseError("submission_requires_all_tests_without_skips")
    archive_info = receipt.get("release_archive", {})
    archive = release / f"genesis-fin-audit-{version}-linux-amd64.tar.gz"
    if (archive_info.get("filename") != archive.name or archive.is_symlink()
            or archive_info.get("sha256") != digest(archive) or archive_info.get("bytes") != archive.stat().st_size):
        raise ReleaseError("submission_archive_does_not_match_clean_run")
    build_receipt = read_json(release / "release-receipt.json")
    if (build_receipt.get("archive_sha256") != archive_info["sha256"]
            or build_receipt.get("manifest_sha256") != digest(bundle / "release-manifest.json")):
        raise ReleaseError("release_build_receipt_mismatch")
    selected = {name: checked_file(clean, receipt, name) for name in (
        "verification/tests.xml", "verification/run/summary.json", "verification/cgroup-metrics.json",
        "verification/run/showcase.html", "initial-daemon.json", "application-containers.json",
        "daemon-cleanup.json", "execution-platform.json", "loaded-image.json",
        "verification/run/readiness/web-summary.json", "control-seed.json",
        "verification/run/application/regression/protocol/run-manifest.json",
        "verification/run/run-manifest.json", "verification/run/readiness/summary.json", "volume-inventory.json")}
    summary = read_json(selected["verification/run/summary.json"])
    metrics = read_json(selected["verification/cgroup-metrics.json"])
    cleanup = read_json(selected["daemon-cleanup.json"])
    initial = read_json(selected["initial-daemon.json"])
    execution_platform = read_json(selected["execution-platform.json"])
    volumes = read_json(selected["volume-inventory.json"])
    require_clean_volume_inventory(volumes)
    control = read_json(selected["control-seed.json"])
    protocol = read_json(selected["verification/run/application/regression/protocol/run-manifest.json"])
    seeds = [control["master_seed"], protocol["master_seed"], *(entry["seed"] for entry in protocol["episodes"])]
    if (summary.get("passed") is not True or summary.get("profile") != "secure"
            or metrics.get("passed") is not True or cleanup.get("existing_engine_preserved") is not True
            or initial.get("info", {}).get("Images") != 0 or initial.get("info", {}).get("Containers") != 0
            or execution_platform != platform):
        raise ReleaseError("incomplete_clean_submission_evidence")
    output = output.absolute()
    if output.exists() or output.is_symlink() or any(parent.is_symlink() for parent in output.parents):
        raise ReleaseError("submission_output_must_be_new")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".submission-", dir=output.parent))
    try:
        for source, name in ((archive, archive.name), (release / "release-receipt.json", "release-receipt.json"),
                             (selected["verification/tests.xml"], "tests.xml"),
                             (selected["verification/run/showcase.html"], "showcase.html")):
            if source.is_symlink() or not source.is_file():
                raise ReleaseError("missing_public_submission_material")
            shutil.copyfile(source, stage / name)
            (stage / name).chmod(0o644)
        if digest(stage / archive.name) != archive_info["sha256"]:
            raise ReleaseError("archive_changed_during_submission_copy")
        raw_public = {
            "CLEAN_ROOM_REPORT.json": clean / "COMPLETE.json",
            "summary.json": selected["verification/run/summary.json"],
            "cgroup-metrics.json": selected["verification/cgroup-metrics.json"],
            "EXECUTION_PLATFORM.json": selected["execution-platform.json"],
            "LOADED_IMAGE.json": selected["loaded-image.json"],
            "web-summary.json": selected["verification/run/readiness/web-summary.json"],
            "APPLICATION_MANIFEST.json": selected["verification/run/run-manifest.json"],
            "READINESS_REPORT.json": selected["verification/run/readiness/summary.json"],
            "VOLUME_INVENTORY.json": selected["volume-inventory.json"],
        }
        for name, source in raw_public.items():
            no_control_seeds(read_json(source))
            shutil.copyfile(source, stage / name)
            (stage / name).chmod(0o644)
        write_json(stage / "HOST_CONFIG.json", redacted_containers(read_json(selected["application-containers.json"]), digest(selected["application-containers.json"])))
        write_json(stage / "INITIAL_ENVIRONMENT.json", {
            "scope": "Public subset of observed fresh daemon and isolated network",
            "source_sha256": digest(selected["initial-daemon.json"]),
            "info": {key: initial["info"].get(key) for key in ("ID", "Architecture", "OSType", "Driver", "Images", "Containers", "DockerRootDir")},
            "isolation": initial["isolation"],
        })
        write_json(stage / "CLEANUP.json", {
            "scope": "Public subset; full owned-daemon logs retained with private run artifacts",
            "source_sha256": digest(selected["daemon-cleanup.json"]),
            **{key: cleanup.get(key) for key in ("stopped_pid", "removed_owned_roots", "existing_engine_preserved", "volume_cleanup")},
        })
        delivery = {"software_version": version, "passed": True, "completed": True,
                    "archive": archive_info, "bundle_inventory_sha256": digest(bundle / "release-manifest.json"),
                    "execution_platform": platform, "tests": junit,
                    "application_checks": len(summary["checks"]), "isolation_probes": len(summary.get("isolation_probes", {})),
                    "mutations": summary.get("mutations"), "whole_container_peak_bytes": receipt["peak_cgroup_memory_bytes"],
                    "clean_seconds": receipt["elapsed_seconds"], "raw_clean_receipt_sha256": digest(clean / "COMPLETE.json"),
                    "public_receipt_sources": {name: {"raw_path": source.relative_to(clean).as_posix(), "sha256": digest(source)}
                                               for name, source in raw_public.items()},
                    "no_official_submission_performed": True}
        write_json(stage / "DELIVERY_REPORT.json", delivery)
        instructions = f"""# Genesis FinAudit {version}: Linux amd64

Все данные синтетические. В комплект входят исходники, актуальный полигон и лаборатория,
10 train + 10 dev, тесты, лицензии и точный Docker image. Обучение весов не выполнялось.

1. Сверьте SHA-256 inner archive из SHA256SUMS с отдельно полученным checksum.
2. Распакуйте {archive.name} в новый каталог и перейдите в него.
3. Полная проверка: `./verify --offline`.
4. Интерактивный полигон: `./demo --offline --port 8765`, затем http://127.0.0.1:8765/.
   Ctrl+C останавливает контейнер своего демо. Выбор engine: `--context NAME`.

На хосте нужны Python 3.11+ и Docker CLI/engine для Linux amd64.
uv, Node, pip/apt, GPU, внешняя сеть и платные API для этих команд не нужны.
Приложение исполняется с network=none. Демо передаёт HTTP через localhost proxy и docker exec.

CLEAN_ROOM_REPORT.json относится к SHA-256 ровно этого inner archive; публичные
HOST_CONFIG/INITIAL_ENVIRONMENT/CLEANUP — явно отмеченные redacted/subset views.
Закрытый контрольный seed и raw trusted command в ZIP не включены.
EXECUTION_PLATFORM.json фиксирует наблюдаемые архитектуры и cross-architecture границы.
QEMU на ARM-хосте не выдаётся за физический x86-компьютер.

Официальные fixed tests/adapters и канал сдачи организаторы не предоставили.
Этот комплект подготовлен для передачи, но автоматически никуда не отправлялся.
"""
        (stage / "SUBMISSION.md").write_text(instructions)
        public_files = sorted(path for path in stage.iterdir() if path.is_file())
        privacy = check_seed_values(public_files, seeds)
        delivery["privacy_scan"] = privacy
        write_json(stage / "DELIVERY_REPORT.json", delivery)
        (stage / "SHA256SUMS").write_text("".join(f"{digest(path)}  {path.name}\n" for path in public_files), encoding="ascii")
        zip_name = f"genesis-fin-audit-{version}-submission.zip"
        with zipfile.ZipFile(stage / zip_name, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive_zip:
            for path in sorted(stage.iterdir()):
                if path.name == zip_name:
                    continue
                info = zipfile.ZipInfo(path.name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive_zip.writestr(info, path.read_bytes())
        with zipfile.ZipFile(stage / zip_name) as archive_zip:
            if (archive_zip.testzip() is not None
                    or hashlib.sha256(archive_zip.read(archive.name)).hexdigest() != archive_info["sha256"]):
                raise ReleaseError("submission_zip_integrity_failed")
        if (stage / zip_name).stat().st_size >= 1_000_000_000:
            raise ReleaseError("submission_zip_size_limit")
        zip_sha = digest(stage / zip_name)
        (stage / (zip_name + ".sha256")).write_text(f"{zip_sha}  {zip_name}\n", encoding="ascii")
        output.mkdir(mode=0o755)
        try:
            os.rename(stage, output)
        except BaseException:
            output.rmdir()
            raise
        return {"output": str(output), "zip": zip_name, "zip_sha256": zip_sha,
                "zip_bytes": (output / zip_name).stat().st_size, "archive_sha256": archive_info["sha256"]}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--clean", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = build_submission(args.release, args.clean, args.output)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Submission assembly failed: {error}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
