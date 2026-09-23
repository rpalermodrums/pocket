"""Explicit clocks and authored musical anchors; none imply native execution."""
from typing import Literal

from typing_extensions import TypedDict

from .artifact_store import ArtifactHandle
from .music_types import Rational


class Attribution(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    actor: str
    actor_kind: Literal["human", "agent"]
    statement: str
    uncertainty: list[str]


class SourceClock(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    clock_id: str
    region: ArtifactHandle


class TimelineClock(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    clock_id: str
    time_map: ArtifactHandle


class ClockPosition(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    clock_id: str
    space: Literal["source_frame", "arrangement_qn", "host_seconds", "render_frame"]
    value: int | Rational


class Occurrence(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    occurrence_id: str
    source_clock_id: str
    timeline_clock_id: str
    source_span_frames: list[int]
    timeline_span_qn: list[Rational]


class MusicalAnchor(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    anchor_id: str
    kind: Literal["pulse", "bar_one", "phrase_start", "onset", "other"]
    label: str
    position: ClockPosition
    attribution: Attribution


class MusicalContextDefinition(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    context_id: str
    title: str
    attribution: Attribution
    sources: list[SourceClock]
    timelines: list[TimelineClock]
    occurrences: list[Occurrence]
    anchors: list[MusicalAnchor]
    materials: list[ArtifactHandle]
