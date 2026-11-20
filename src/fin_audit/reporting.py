"""Trusted run provenance; measurements are separate from enforced limits."""

from __future__ import annotations

import hashlib
import importlib.metadata
import platform
import resource
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .contracts import PROTOCOL_VERSION, SCHEMA_VERSION


def process_memory() -> dict[str, Any]:
    """Observed process-lifetime RSS; never present this as container peak."""
    multiplier = 1 if sys.platform == "darwin" else 1024
    return {
        "self_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * multiplier,
        "children_peak_rss_bytes": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * multiplier,
        "scope": "Process-lifetime high-water; children is the largest reaped child including test harnesses, not a sum or container peak.",
    }


def source_hashes(root: Path) -> dict[str, str]:
    """Hash executable sources, tests, frozen plans and data, excluding outputs."""
    files = [root / name for name in ("pyproject.toml", "uv.lock", ".python-version", "LICENSE")]
    for directory in ("src", "tests", "scripts", "docker", "schemas", "configs", "datasets"):
        for path in (root / directory).rglob("*"):
            if "__pycache__" not in path.parts and path.is_file() and path.suffix not in {".pyc", ".pyo"}:
                if path.is_symlink():
                    raise ValueError("Source manifest does not accept symlinks.")
                files.append(path)
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(files)}


def runtime_manifest(*, hashes: dict[str, str], profile: str) -> dict[str, Any]:
    dependencies = {}
    for name in ("pytest", "hypothesis", "jsonschema"):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = None
    return {
        "software_version": __version__, "schema_version": SCHEMA_VERSION,
        "wire_protocol_version": PROTOCOL_VERSION, "profile": profile,
        "python": platform.python_version(), "platform": platform.system(),
        "architecture": platform.machine(), "dependencies": dependencies,
        "source_sha256": hashes,
        "source_scope": "src/tests/scripts/docker/schemas/configs/datasets and pyproject/lock/python-version/LICENSE; docs and run outputs excluded",
        "resource_limits": {
            "enforced_by_secure_wrapper_only": True, "container_cpus": 2,
            "container_memory_bytes": 4 * 1024**3, "container_pids": 64,
            "agent_address_space_bytes": 512 * 1024**2,
            "episode_timeout_seconds": 60, "network": "none", "gpu": False,
        },
        "measurement_note": "Limits are configuration, not observed consumption. Protocol report records wall time and process lifetime high-water RSS separately.",
    }
