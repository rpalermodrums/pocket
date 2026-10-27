# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded exact coordinate values, independent of material and host adapters."""
from fractions import Fraction

from .errors import PocketError


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise PocketError("Unexpected or missing musical-time fields")


def integer(value, name, low=-(2**53), high=2**53):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise PocketError(f"{name} must be a bounded integer")
    return value


def fraction(value, name="time"):
    fields(value, {"n", "d"})
    numerator = integer(value["n"], name + " numerator")
    denominator = integer(value["d"], name + " denominator", 1)
    result = Fraction(numerator, denominator)
    if result.numerator != numerator or result.denominator != denominator:
        raise PocketError(f"{name} requires a reduced rational")
    return result


def rational_json(value):
    value = Fraction(value)
    integer(value.numerator, "converted numerator")
    integer(value.denominator, "converted denominator", 1)
    return {"n": value.numerator, "d": value.denominator}


def text(value, name, limit=500):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise PocketError(f"{name} must be nonempty bounded text")


def bounded_list(value, name, minimum=0, maximum=4096):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise PocketError(f"{name} exceeds its list bound")
