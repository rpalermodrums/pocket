"""Strict domain checks shared by instrument and curve providers."""
from __future__ import annotations

import math

from ..errors import PocketError


def fields(value, required, optional=(), label="record"):
    if not isinstance(value, dict):
        raise PocketError(f"{label} must be an object")
    missing, unknown = set(required) - value.keys(), value.keys() - set(required) - set(optional)
    if missing or unknown:
        raise PocketError(f"{label}: missing fields {sorted(missing)}; unknown fields {sorted(unknown)}")


def text(value, label, maximum=1024):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise PocketError(f"{label} must be nonempty text of at most {maximum} characters")
    return value


def number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PocketError(f"{label} must be a finite number")
    try:
        finite = math.isfinite(value)
    except OverflowError as error:
        raise PocketError(f"{label} exceeds the supported numeric range") from error
    if not finite:
        raise PocketError(f"{label} must be a finite number")
    return value


def integer(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise PocketError(f"{label} must be an integer in {low}..{high}")
    return value


def boolean(value, label):
    if not isinstance(value, bool):
        raise PocketError(f"{label} must be boolean")
    return value


def choice(value, allowed, label):
    if not isinstance(value, str) or value not in allowed:
        raise PocketError(f"Unsupported {label}: {value!r}")
    return value


def page(limit, offset):
    integer(limit, "limit", 1, 100)
    integer(offset, "offset", 0, 1000000)


def domain(value, low, high, quantized, values, label):
    number(low, "minimum")
    number(high, "maximum")
    number(value, label)
    boolean(quantized, "quantized")
    if low > high or not low <= value <= high:
        raise PocketError(f"{label} outside value domain")
    if not isinstance(values, list) or len(values) > 512:
        raise PocketError("enum values must be a list with at most 512 entries")
    for item in values:
        number(item, "enum value")
        if not low <= item <= high:
            raise PocketError("enum value outside value domain")
    if len(values) != len(set(values)):
        raise PocketError("Duplicate enum values")
    if quantized and (value not in values if values else value != int(value)):
        raise PocketError(f"{label} is not a valid quantized value")
    if values and not quantized:
        raise PocketError("Enum values require quantized:true")
