from decimal import Inexact, ROUND_DOWN, Rounded, localcontext

import pytest
from hypothesis import given, strategies as st

from fin_audit.calculator import CalculationError, calculate


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("0.1 + 0.2", "0.3"),
        ("100000 - 90000", "10000"),
        ("(600.00 - 500) * 495", "49500"),
        ("2+3*4", "14"),
        ("(2+3)*4", "20"),
        ("-(-2.50) + +.5", "3"),
        ("1 / 8", "0.125"),
        ("12. / 4", "3"),
        ("-0.000", "0"),
        ("  42 \n", "42"),
    ],
)
def test_decimal_arithmetic(expression, expected):
    assert calculate(expression) == expected


def test_repeating_decimal_is_bounded_and_independent_of_caller_context():
    expected = "0." + "3" * 80
    assert calculate("1 / 3") == expected
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_DOWN
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        assert calculate("1 / 3") == expected
        assert calculate("0.1 + 0.2") == "0.3"
        assert context.prec == 2


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('echo escaped')",
        "open('/private/truth.json').read()",
        "(1).__class__",
        "[1][0]",
        "sum((1,2))",
        "2**1000000",
        "1//2",
        "1e100",
        "NaN",
        "Infinity",
        "True",
        "1;2",
        "1 2",
        "1.2.3",
        "(1+2",
        "1+2)",
        "",
        " ",
        "١+٢",
        None,
        2,
    ],
)
def test_forbidden_code_and_invalid_syntax_are_bounded(expression):
    with pytest.raises(CalculationError) as caught:
        calculate(expression)
    assert str(caught.value) in {
        "invalid_expression", "expression_too_complex", "number_out_of_range"
    }


def test_division_by_zero_is_a_bounded_error():
    with pytest.raises(CalculationError, match="^division_by_zero$"):
        calculate("100 / 0")


@pytest.mark.parametrize(
    "expression",
    [
        "1" * 513,
        "9" * 41,
        "(" * 17 + "1" + ")" * 17,
        "+".join(["1"] * 66),
        "*".join(["9" * 40] * 4),
    ],
)
def test_resource_limits_reject_large_or_complex_input(expression):
    with pytest.raises(CalculationError):
        calculate(expression)


def test_exact_supported_boundaries():
    assert calculate("+".join(["1"] * 65)) == "65"
    assert calculate("(" * 16 + "1" + ")" * 16) == "1"
    assert calculate("9" * 40) == "9" * 40


@given(st.integers(-10**12, 10**12), st.integers(-10**12, 10**12))
def test_integer_arithmetic_matches_exact_reference(left, right):
    assert calculate(f"({left})+({right})") == str(left + right)
    assert calculate(f"({left})*({right})") == str(left * right)
