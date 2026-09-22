"""Strict deterministic motif-development input records."""
from __future__ import annotations

from typing_extensions import TypedDict

from .artifact_store import ArtifactHandle
from .material_types import MaterialRecord, Position, Rational
from .time_types import TimeContext


class DevelopmentSeed(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    material: ArtifactHandle | MaterialRecord
    material_revision: str
    clip_id: str


class DevelopmentOccurrence(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    occurrence_id: str
    at_qn: Rational | int


class DevelopmentVariation(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    pitch_offsets_semitones: list[int]
    timing_offsets_qn: list[Rational | int]
    max_changed_notes: int
    pitch_min: int
    pitch_max: int


class DevelopmentSeeds(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    structure: int
    pitch: int
    timing: int


class DevelopmentDefinition(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    label: str
    seed: DevelopmentSeed
    clock: TimeContext
    origin: Position
    length_qn: Rational | int
    occurrences: list[DevelopmentOccurrence]
    locked_occurrence_id: str
    endpoint_note_id: str
    eligible_occurrence_ids: list[str]
    variation: DevelopmentVariation
    seeds: DevelopmentSeeds
