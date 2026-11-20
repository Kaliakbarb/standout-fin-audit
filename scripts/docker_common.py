"""Host-side Docker helpers; no global context changes or implicit pulls."""

from __future__ import annotations

import os
import json
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "genesis-fin-audit:release-" + tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
SUPPORTED_PLATFORMS = ("linux/amd64", "linux/arm64")


def normalize_platform(os_name: str, architecture: str) -> str:
    """Docker and Linux report different aliases for the same instruction set."""
    aliases = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
    platform = f"{os_name}/{aliases.get(architecture, architecture)}"
    if platform not in SUPPORTED_PLATFORMS:
        raise RuntimeError(f"Unsupported Docker platform: {os_name}/{architecture}")
    return platform


def daemon_platform(docker: list[str]) -> str:
    """Inspect the chosen daemon, without changing the caller's Docker context."""
    value = subprocess.check_output(
        docker + ["info", "--format", "{{.OSType}}/{{.Architecture}}"], text=True,
    ).strip().split("/")
    if len(value) != 2:
        raise RuntimeError("Docker returned an invalid platform.")
    return normalize_platform(*value)


def inspect_image_platform(docker: list[str], image: str, expected_platform: str) -> dict:
    """A successful build alone is not evidence of the requested target ISA."""
    inspected = json.loads(subprocess.check_output(
        docker + ["image", "inspect", image, "--format", "{{json .}}"], text=True,
    ))
    if (not isinstance(inspected, dict)
            or normalize_platform(inspected.get("Os", ""), inspected.get("Architecture", "")) != expected_platform):
        raise RuntimeError("Built image does not match the requested platform.")
    return inspected


def load_or_create_master_seed(path: Path) -> int:
    """Persist one private control-cohort seed without publishing a partial file.

    The hard-link installation is atomic and never overwrites a concurrent winner.
    The file belongs to the trusted host; it is never mounted in the worker jail.
    """
    path = path.absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() and not path.is_symlink():
        descriptor, temporary = tempfile.mkstemp(prefix=".cohort-seed-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "master_seed": secrets.randbits(128)}, handle)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Control seed file must be a private regular file owned by this user.")
        if info.st_size > 1024:
            raise ValueError("Invalid control seed receipt.")
        receipt = json.load(handle)
    if not isinstance(receipt, dict) or set(receipt) != {"version", "master_seed"}:
        raise ValueError("Invalid control seed receipt.")
    seed = receipt["master_seed"]
    if type(receipt["version"]) is not int or receipt["version"] != 1 or type(seed) is not int or not 0 <= seed < 2**128:
        raise ValueError("Invalid control seed receipt.")
    return seed


def docker_command(context: str | None = None) -> list[str]:
    if shutil.which("docker") is None:
        raise RuntimeError("Docker is required. See README preparation instructions.")
    context = context or os.environ.get("FIN_AUDIT_DOCKER_CONTEXT")
    if context is None:
        current = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if current.returncode != 0:
            if os.environ.get("DOCKER_HOST"):
                raise RuntimeError("The explicit DOCKER_HOST is unavailable; no fallback context is used.")
            available = subprocess.run(
                ["docker", "--context", "colima-genesis-fin-audit", "info"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if available.returncode == 0:
                context = "colima-genesis-fin-audit"
            else:
                raise RuntimeError("No running Docker engine. Start Docker/Colima; no cooperative fallback is used.")
    return ["docker", "--context", context] if context else ["docker"]


def secure_create_flags(platform: str | None = None) -> list[str]:
    if platform is not None and platform not in SUPPORTED_PLATFORMS:
        raise ValueError("Unsupported verification platform.")
    return [
        "create", "--pull=never", "--network=none", "--read-only",
        "--cpus=2", "--memory=4g", "--memory-swap=4g", "--pids-limit=64",
        "--cap-drop=ALL", "--cap-add=SYS_CHROOT", "--cap-add=SETUID",
        "--cap-add=SETGID", "--cap-add=KILL", "--security-opt=no-new-privileges=true",
        "--tmpfs=/tmp:rw,noexec,nosuid,size=256m",
        "--env=PYTHONDONTWRITEBYTECODE=1", "--env=HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hypothesis",
        # Root inside a Linux VM need not own files shared by its host user.
        # A private anonymous volume avoids host UID/GID and filesystem mapping.
        "--mount", "type=volume,target=/output,volume-nocopy",
    ] + (["--platform=" + platform] if platform is not None else [])


_VERIFICATION_LABEL = "org.genesis.fin-audit.verification"
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")


def _owned_container(docker: list[str], name: str, token: str, expected: str | None) -> str | None:
    """Resolve only this invocation's container, including a partial create."""
    listed = subprocess.run(
        docker + ["ps", "--all", "--quiet", "--no-trunc", "--filter", f"name=^/{name}$",
                  "--filter", f"label={_VERIFICATION_LABEL}={token}"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=30,
    )
    if listed.returncode:
        raise RuntimeError("Cannot establish owned verification container state.")
    identifiers = listed.stdout.splitlines()
    if not identifiers and expected is None:
        return None
    if (len(identifiers) != 1 or not _CONTAINER_ID.fullmatch(identifiers[0])
            or expected is not None and identifiers[0] != expected):
        raise RuntimeError("Owned verification container identity is unavailable or inconsistent.")
    identifier = identifiers[0]
    inspected = subprocess.run(docker + ["inspect", "--type", "container", identifier],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=30)
    if inspected.returncode or len(inspected.stdout) > 1024 * 1024:
        raise RuntimeError("Cannot inspect owned verification container.")
    try:
        values = json.loads(inspected.stdout)
        value = values[0] if isinstance(values, list) and len(values) == 1 else {}
        valid = (value.get("Id") == identifier and value.get("Name") == "/" + name
                 and value.get("Config", {}).get("Labels", {}).get(_VERIFICATION_LABEL) == token)
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise RuntimeError("Verification container ownership check failed.")
    return identifier


def run_secure_container(docker: list[str], image: str, command: list[str], output: Path,
                         *, cwd: Path, platform: str | None = None) -> int:
    """Run one secure container, copy its volume to the host, and remove both.

    Output is a new empty private host directory. Docker cp without archive mode
    gives copied files to the invoking host user while preserving private modes.
    The anonymous volume is retained until copying finishes, including failures.
    """
    output = output.absolute()
    information = output.lstat()
    if (not stat.S_ISDIR(information.st_mode) or stat.S_IMODE(information.st_mode) != 0o700
            or information.st_uid != os.getuid() or any(output.iterdir())):
        raise ValueError("Verification output must be an empty private directory owned by this user.")
    token = secrets.token_hex(16)
    name = "fin-audit-verify-" + token
    identifier = None
    creation_attempted = False
    with tempfile.TemporaryDirectory(prefix=".verification-control-", dir=output) as temporary:
        control = Path(temporary)
        cidfile = control / "container-id"
        staging = control / "artifacts"
        staging.mkdir(mode=0o700)
        try:
            create = docker + secure_create_flags(platform) + [
                "--name", name, "--label", f"{_VERIFICATION_LABEL}={token}", "--cidfile", str(cidfile),
                image, *command,
            ]
            creation_attempted = True
            created = subprocess.run(create, cwd=cwd, stdout=subprocess.DEVNULL, timeout=120)
            if created.returncode:
                raise RuntimeError(f"Secure verification container creation failed (exit {created.returncode}).")
            if cidfile.is_symlink() or not cidfile.is_file() or cidfile.stat().st_size > 65:
                raise RuntimeError("Docker did not provide a valid verification container ID.")
            candidate = cidfile.read_text(encoding="ascii").strip()
            if not _CONTAINER_ID.fullmatch(candidate):
                raise RuntimeError("Docker did not provide a valid verification container ID.")
            identifier = _owned_container(docker, name, token, candidate)
            attached = subprocess.run(docker + ["start", "--attach", identifier], cwd=cwd)
            inspected = subprocess.run(docker + ["inspect", "--type", "container", "--format", "{{json .State}}", identifier],
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=30)
            if inspected.returncode or len(inspected.stdout) > 65536:
                raise RuntimeError("Cannot inspect verification exit status.")
            try:
                state = json.loads(inspected.stdout)
            except ValueError:
                state = None
            if (not isinstance(state, dict) or state.get("Running") is not False
                    or state.get("Status") != "exited" or type(state.get("ExitCode")) is not int
                    or not 0 <= state["ExitCode"] <= 255):
                raise RuntimeError("Verification exit status is invalid or container has not exited.")
            copied = subprocess.run(docker + ["cp", identifier + ":/output/.", str(staging)], cwd=cwd, timeout=300)
            if copied.returncode:
                raise RuntimeError("Could not copy verification artifacts to the host.")
            # Docker may copy the volume root's mode. The containing output stays
            # 0700 throughout; publish only its children, after transfer succeeds.
            for child in staging.iterdir():
                child.rename(output / child.name)
            if attached.returncode and state["ExitCode"] == 0:
                raise RuntimeError("Docker attach failed despite a successful container exit.")
            return state["ExitCode"]
        finally:
            if creation_attempted:
                owned = _owned_container(docker, name, token, identifier)
                if owned is not None:
                    removed = subprocess.run(docker + ["rm", "--force", "--volumes", owned],
                                             stdout=subprocess.DEVNULL, timeout=30)
                    if removed.returncode:
                        raise RuntimeError("Could not remove owned verification container and anonymous volume.")
