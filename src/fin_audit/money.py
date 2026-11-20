"""Exact monetary primitives shared by data generation and independent checking."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, localcontext

_DECIMAL = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)\Z")
_ROUNDING_MODES = {"ROUND_HALF_EVEN", "ROUND_HALF_UP", "ROUND_DOWN"}


def minor_integer(value: object, *, nonnegative: bool = False) -> int:
    """Require an actual integer, never a bool or an implicitly rounded float."""
    if type(value) is not int:
        raise ValueError("money_requires_integer_minor_units")
    if nonnegative and value < 0:
        raise ValueError("negative_money_not_allowed")
    return value


def currency_exponent(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 9:
        raise ValueError("invalid_currency_exponent")
    return value


def convert_minor(
    amount_minor: int,
    rate: str,
    source_exponent: int,
    target_exponent: int,
    *,
    rounding: str = "ROUND_HALF_EVEN",
) -> int:
    """Convert source minor units at target-major/source-major decimal rate.

    Signed source documents can use the same primitive. Ledger debit and credit
    positivity is validated separately. Conversion performs exactly one rounding
    at the target minor-unit boundary.
    """
    minor_integer(amount_minor)
    currency_exponent(source_exponent)
    currency_exponent(target_exponent)
    if not isinstance(rate, str) or len(rate) > 100 or not _DECIMAL.fullmatch(rate):
        raise ValueError("fx_rate_requires_decimal_string")
    if not isinstance(rounding, str) or rounding not in _ROUNDING_MODES:
        raise ValueError("unsupported_rounding_mode")
    try:
        parsed_rate = Decimal(rate)
    except InvalidOperation as exc:
        raise ValueError("invalid_fx_rate") from exc
    if not parsed_rate.is_finite() or parsed_rate <= 0:
        raise ValueError("fx_rate_must_be_positive_finite")
    # Enough precision for the complete integer/rate product, not merely a
    # constant context which silently loses large low-order minor units.
    precision = max(80, len(str(abs(amount_minor))) + len(rate) + 30)
    with localcontext() as context:
        context.prec = precision
        context.rounding = rounding
        converted = (
            Decimal(amount_minor)
            * parsed_rate
            * (Decimal(10) ** (target_exponent - source_exponent))
        )
        return int(converted.to_integral_value(rounding=rounding))


def split_minor(total_minor: int, parts: int) -> list[int]:
    """Split positive money into positive integer pieces without losing cents."""
    minor_integer(total_minor, nonnegative=True)
    if type(parts) is not int or parts < 1 or total_minor < parts:
        raise ValueError("invalid_positive_money_split")
    quotient, remainder = divmod(total_minor, parts)
    return [quotient + (index < remainder) for index in range(parts)]
