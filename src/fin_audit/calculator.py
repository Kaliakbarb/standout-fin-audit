"""Bounded decimal arithmetic for the public calculator tool.

This parser deliberately accepts no Python expressions, names, functions, powers,
scientific notation, or filesystem operations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import (
    Decimal,
    DecimalException,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    ROUND_HALF_EVEN,
    Underflow,
    localcontext,
)

MAX_EXPRESSION_LENGTH = 512
MAX_OPERATIONS = 64
MAX_DEPTH = 16
MAX_LITERAL_DIGITS = 40
MAX_RESULT_LENGTH = 256
DECIMAL_PRECISION = 80
MAX_ADJUSTED_EXPONENT = 100

_NUMBER = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")


class CalculationError(ValueError):
    """A bounded, public error code; never include the expression in the error."""

    def __init__(self, code: str = "invalid_expression") -> None:
        self.code = code
        super().__init__(code)


def _bounded(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise CalculationError("number_out_of_range")
    if value and abs(value.adjusted()) > MAX_ADJUSTED_EXPONENT:
        raise CalculationError("number_out_of_range")
    return value


def _tokens(expression: str) -> list[str]:
    tokens: list[str] = []
    position = 0
    while position < len(expression):
        char = expression[position]
        if char in " \t\r\n":
            position += 1
            continue
        if char in "+-*/()":
            tokens.append(char)
            position += 1
            continue
        match = _NUMBER.match(expression, position)
        if match is None:
            raise CalculationError()
        token = match.group()
        if sum(char.isdigit() for char in token) > MAX_LITERAL_DIGITS:
            raise CalculationError("number_out_of_range")
        tokens.append(token)
        position = match.end()
    return tokens


@dataclass
class _Parser:
    tokens: list[str]
    position: int = 0
    operations: int = 0

    def peek(self) -> str | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def operation(self) -> None:
        self.operations += 1
        if self.operations > MAX_OPERATIONS:
            raise CalculationError("expression_too_complex")

    def expression(self, depth: int = 0) -> Decimal:
        value = self.product(depth)
        while self.peek() in ("+", "-"):
            operator = self.peek()
            self.position += 1
            self.operation()
            right = self.product(depth)
            value = _bounded(value + right if operator == "+" else value - right)
        return value

    def product(self, depth: int) -> Decimal:
        value = self.factor(depth)
        while self.peek() in ("*", "/"):
            operator = self.peek()
            self.position += 1
            self.operation()
            right = self.factor(depth)
            value = _bounded(value * right if operator == "*" else value / right)
        return value

    def factor(self, depth: int) -> Decimal:
        if depth > MAX_DEPTH:
            raise CalculationError("expression_too_complex")
        token = self.peek()
        if token is None:
            raise CalculationError()
        self.position += 1
        if token in ("+", "-"):
            self.operation()
            value = self.factor(depth + 1)
            return _bounded(value if token == "+" else -value)
        if token == "(":
            value = self.expression(depth + 1)
            if self.peek() != ")":
                raise CalculationError()
            self.position += 1
            return value
        if _NUMBER.fullmatch(token) is None:
            raise CalculationError()
        return _bounded(Decimal(token))


def calculate(expression: str) -> str:
    """Evaluate arithmetic as a finite decimal string with deterministic rounding."""
    if not isinstance(expression, str):
        raise CalculationError()
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise CalculationError("expression_too_large")
    tokens = _tokens(expression)
    if not tokens:
        raise CalculationError()
    try:
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            context.rounding = ROUND_HALF_EVEN
            context.Emax = MAX_ADJUSTED_EXPONENT
            context.Emin = -MAX_ADJUSTED_EXPONENT
            context.clamp = 0
            context.clear_flags()
            for signal in context.traps:
                context.traps[signal] = False
            for signal in (DivisionByZero, InvalidOperation, Overflow, Underflow):
                context.traps[signal] = True
            parser = _Parser(tokens)
            value = _bounded(parser.expression())
            if parser.peek() is not None:
                raise CalculationError()
            rendered = format(value, "f")
    except DivisionByZero:
        raise CalculationError("division_by_zero") from None
    except DecimalException:
        raise CalculationError("number_out_of_range") from None
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if rendered in ("-0", ""):
        rendered = "0"
    if len(rendered) > MAX_RESULT_LENGTH:
        raise CalculationError("number_out_of_range")
    return rendered
