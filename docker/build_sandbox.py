"""Build the minimal agent jail; never run this during an audit episode.

The trusted application and its test dependencies stay outside the jail. Only a
standard-library Python runtime and the standalone public policy worker enter it.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tomllib
from pathlib import Path


def install_test_dependencies() -> None:
    lock = tomllib.loads(Path("/app/uv.lock").read_text())
    lines = ["# Generated from uv.lock; registry packages only, no editable project."]
    for package in lock["package"]:
        if "registry" not in package.get("source", {}):
            continue
        hashes = {
            item["hash"]
            for item in [package.get("sdist", {}), *package.get("wheels", [])]
            if "hash" in item
        }
        if not hashes or not all(value.startswith("sha256:") for value in hashes):
            raise RuntimeError("locked_package_has_no_sha256")
        lines.append(
            f"{package['name']}=={package['version']} "
            + " ".join(f"--hash={value}" for value in sorted(hashes))
        )
    requirements = Path("/app/docker/requirements.lock.txt")
    requirements.write_text("\n".join(lines) + "\n")
    subprocess.run(
        [
            sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
            "--no-cache-dir", "--require-hashes", "-r", str(requirements),
        ],
        check=True,
    )


def dynamic_dependencies(binary: Path) -> set[Path]:
    completed = subprocess.run(
        ["ldd", str(binary)], text=True, capture_output=True, check=False
    )
    output = completed.stdout + completed.stderr
    if "not found" in output:
        raise RuntimeError(f"missing_dynamic_dependency:{binary.name}")
    if completed.returncode and not any(
        marker in output for marker in ("not a dynamic executable", "statically linked")
    ):
        raise RuntimeError(f"ldd_failed:{binary.name}")
    return {Path(value) for value in re.findall(r"(/[^\s()]+)", output)}


def copy_absolute(source: Path, root: Path) -> Path:
    # Keep the loader's original absolute pathname; resolving a usr-merge symlink
    # would lose paths such as /lib/ld-linux-aarch64.so.1 from PT_INTERP.
    source = Path(os.path.abspath(source))
    target = root / source.relative_to("/")
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=True)
    return target


def build(root: Path, worker: Path) -> None:
    if sys.platform != "linux" or sys.version_info[:2] != (3, 11):
        raise RuntimeError("sandbox_build_requires_linux_python311")
    if root.exists() and any(root.iterdir()):
        raise RuntimeError("sandbox_root_must_be_empty")
    root.mkdir(parents=True, exist_ok=True)
    stdlib = Path(sysconfig.get_path("stdlib"))
    target_stdlib = root / stdlib.relative_to("/")
    shutil.copytree(
        stdlib,
        target_stdlib,
        ignore=shutil.ignore_patterns(
            "site-packages", "__pycache__", "test", "tests", "idlelib", "tkinter",
            "turtledemo", "ensurepip", "_test*", "_tkinter*", "turtle.py",
        ),
    )
    executable = Path(sys.executable).resolve()
    copy_absolute(executable, root)
    expected_executable = root / "usr/local/bin/python3.11"
    if not expected_executable.is_file():
        raise RuntimeError("unexpected_python_binary_path")

    pending = [executable, *stdlib.rglob("*.so")]
    checked: set[Path] = set()
    libraries: set[Path] = set()
    while pending:
        binary = pending.pop()
        if binary in checked:
            continue
        checked.add(binary)
        # Excluded stdlib extension modules are not part of the jail.
        if binary != executable:
            candidate = root / binary.relative_to("/")
            if binary.is_relative_to(stdlib) and not candidate.exists():
                continue
        for dependency in dynamic_dependencies(binary):
            copy_absolute(dependency, root)
            libraries.add(dependency)
            if dependency not in checked:
                pending.append(dependency)

    # With no /proc, the dynamic loader cannot rely on resolving $ORIGIN via
    # /proc/self/exe. Preserve the public base image's library cache so libpython
    # in /usr/local/lib is found after the fresh chroot exec as well.
    copy_absolute(Path("/etc/ld.so.cache"), root)

    # Public timezone data is useful to future policy adapters; no host config,
    # secrets, package manager, shell, /proc or writable scratch is copied.
    zoneinfo = Path("/usr/share/zoneinfo")
    if zoneinfo.is_dir():
        shutil.copytree(zoneinfo, root / "usr/share/zoneinfo")
    shutil.copy2(worker, root / "agent_worker.py")
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            raise RuntimeError("unexpected_agent_symlink")
        path.chmod(path.stat().st_mode & ~0o222)
    root.chmod(0o555)

    private = Path("/private")
    private.mkdir(mode=0o700, exist_ok=True)
    private.chmod(0o700)
    sentinel = private / "fin_audit_sentinel.json"
    sentinel.write_text(json.dumps({"sentinel": "private_fin_audit_spike"}) + "\n")
    sentinel.chmod(0o600)

    seccomp_version = subprocess.check_output(
        ["dpkg-query", "--show", "--showformat=${Version}", "libseccomp2"], text=True
    )
    metadata = {
        "python": sys.version,
        "platform_machine": platform.machine(),
        "python_elf_machine": int.from_bytes(executable.read_bytes()[18:20], "little"),
        "executable": str(executable),
        "stdlib": str(stdlib),
        "seccomp_version": seccomp_version,
        "libraries": sorted(str(path) for path in libraries),
        "agent_files": sum(path.is_file() for path in root.rglob("*")),
        "agent_bytes": sum(path.stat().st_size for path in root.rglob("*") if path.is_file()),
        "trusted_application_copied": False,
    }
    Path("/app/docker/sandbox-build.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(metadata, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-test-dependencies", action="store_true")
    parser.add_argument("--root", type=Path, default=Path("/sandbox"))
    parser.add_argument("--worker", type=Path, default=Path("/app/src/fin_audit/agent_worker.py"))
    arguments = parser.parse_args()
    if arguments.install_test_dependencies:
        install_test_dependencies()
    else:
        build(arguments.root, arguments.worker)


if __name__ == "__main__":
    main()
