"""Project-local development process configuration."""

import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def development_environment() -> dict[str, str]:
    environment = os.environ.copy()
    if sys.platform == "darwin" and "UV_PROJECT_ENVIRONMENT" not in environment:
        # Editable hooks inside synced Documents repeatedly become UF_HIDDEN.
        # Keep only this project's generated environment in a non-synced cache.
        key = hashlib.sha256(str(ROOT).encode()).hexdigest()[:12]
        environment["UV_PROJECT_ENVIRONMENT"] = str(
            Path.home() / "Library/Caches/genesis-fin-audit" / key / "venv"
        )
    return environment
