"""Strict declared inputs for offline symbolic note/sustain reservations."""
from __future__ import annotations

from typing import Literal

from typing_extensions import TypedDict


class InitialSustain(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    channel: int
    value: int


class LifecycleInitialState(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    active_notes: Literal['none']
    sustain: list[InitialSustain]
