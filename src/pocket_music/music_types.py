# SPDX-License-Identifier: AGPL-3.0-only
"""Small, host-independent value types shared by audio, symbolic material and time."""
from typing_extensions import TypedDict


class Rational(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    n: int
    d: int
