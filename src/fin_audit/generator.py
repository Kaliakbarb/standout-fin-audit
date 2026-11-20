"""Choose the native teaching slice or the explicit multi-currency v2 model."""

from __future__ import annotations

from typing import Any

from .contracts import EpisodeBundle
from .native_generator import GenerationError, generate as _native_generate

STAGE2_CONFIG_KEYS = frozenset({
    "fx_count", "cutoff_count", "foreign_invoice_count", "foreign_currency",
    "foreign_exponent", "refund_count", "interaction_allocation_fx",
    "interaction_allocation_cutoff", "fx_mode", "fx_target",
})


def generate(seed: int, config: dict[str, Any] | None = None) -> EpisodeBundle:
    """Preserve native defaults; explicit v2 configuration selects the full model."""
    if isinstance(config, dict) and STAGE2_CONFIG_KEYS.intersection(config):
        from .multi_generator import generate_multi

        return generate_multi(seed, config)
    return _native_generate(seed, config)
