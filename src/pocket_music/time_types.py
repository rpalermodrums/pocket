# SPDX-License-Identifier: AGPL-3.0-only
"""Strict transport schemas for exact, declared musical clock mappings."""
from __future__ import annotations

from typing import Literal, NotRequired

from typing_extensions import TypedDict

from .artifact_store import ArtifactHandle
from .music_types import Rational


class TimeContext(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    schema: Literal["pocket.time-context/v1"]
    context_id: str
    attribution: str
    source: NotRequired[ArtifactHandle]


class TimeDomain(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    start: Rational
    end: Rational


class TempoStep(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    at_qn: Rational
    bpm: Rational
    interpolation: Literal["step"]


class HostOrigin(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    arrangement_qn: Rational
    host_seconds: Rational


class RenderOrigin(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    audio: ArtifactHandle
    host_seconds: Rational
    frame: int
    sample_rate: int


class MeterSegment(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    at_qn: Rational
    numerator: int
    denominator: int
    bar_number: int
    partial_previous_bar: bool


class LocalCycle(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    cycle_id: str
    origin_qn: Rational
    length_qn: Rational
    annotation: str


class TimeMapDefinition(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    source_context: ArtifactHandle | TimeContext
    domain_qn: TimeDomain
    tempo: list[TempoStep]
    host_origin: HostOrigin
    render_origin: NotRequired[RenderOrigin | None]
    meter: NotRequired[list[MeterSegment]]
    bar_one_qn: NotRequired[Rational | None]
    cycles: NotRequired[list[LocalCycle]]


class TimePosition(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    space: Literal["arrangement_qn", "host_seconds", "render_frame"]
    value: Rational | int


class FrameQuantization(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    policy: Literal["nearest_half_away_from_zero"]
    max_error_frames: Rational
