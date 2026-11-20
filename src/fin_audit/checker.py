"""Versioned dispatch to truth-blind public accounting reconciliation.

This module never imports the generator, mutation code, provenance, or truth.
Historical native books remain checkable after the public schema version bump.
"""

from __future__ import annotations

from typing import Any

from .checker_errors import DataValidationError
from .contracts import SUPPORTED_SCHEMA_VERSIONS, validate_public_privacy


def reconcile(
    public: dict[str, Any], policy: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Derive the complete supported issue set from public records alone."""
    try:
        if not isinstance(public, dict):
            raise DataValidationError("public_book_requires_object")
        try:
            validate_public_privacy(public)
        except ValueError as error:
            raise DataValidationError(str(error)) from error
        if public["manifest"]["schema_version"] not in SUPPORTED_SCHEMA_VERSIONS:
            raise DataValidationError("unsupported_schema_version")
        if policy is not None and policy != public["policy"]:
            raise DataValidationError("policy_does_not_match_public_book")
        model = public["policy"].get("accounting_model")
        if model == "receivables_native_v1":
            from .native_checker import reconcile as reconcile_native

            return reconcile_native(public, policy)
        if model == "receivables_multi_v2":
            from .multicurrency_checker import reconcile as reconcile_multi

            return reconcile_multi(public, policy)
        raise DataValidationError("unsupported_accounting_model")
    except DataValidationError:
        raise
    except (KeyError, TypeError, IndexError, AttributeError) as error:
        raise DataValidationError("malformed_public_book") from error
