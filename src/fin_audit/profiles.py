"""Public, versioned demonstration configurations for the second milestone."""

from __future__ import annotations


def stage2_cases() -> dict[str, dict]:
    base = {
        "foreign_invoice_count": 6,
        "foreign_currency": "USD",
        "foreign_exponent": 2,
        "refund_count": 1,
        "duplicate_count": 0,
        "allocation_count": 0,
        "fx_count": 0,
        "cutoff_count": 0,
    }
    settings = {
        "clean_multicurrency": {},
        "duplicate_only": {"duplicate_count": 1},
        "allocation_only": {"allocation_count": 1},
        "fx_only": {"fx_count": 1, "fx_target": "payment"},
        "cutoff_only": {"cutoff_count": 1},
        "all_four": {"duplicate_count": 1, "allocation_count": 1, "fx_count": 1, "cutoff_count": 1},
        "allocation_fx": {"allocation_count": 1, "fx_count": 1, "interaction_allocation_fx": True, "fx_target": "payment"},
        "allocation_cutoff": {"allocation_count": 1, "cutoff_count": 1, "interaction_allocation_cutoff": True},
        "fx_wrong_date": {"fx_count": 1, "fx_target": "invoice", "fx_mode": "date"},
    }
    return {name: {**base, **extra} for name, extra in settings.items()}
