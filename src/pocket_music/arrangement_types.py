"""Declared multi-section development, without inferred arrangement semantics."""
from __future__ import annotations

from typing_extensions import TypedDict

from .artifact_store import ArtifactHandle
from .develop_types import DevelopmentSeeds
from .material_types import Position, Rational
from .structure_types import StructureAttribution
from .time_types import TimeContext


class ArrangementSection(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    section_id: str
    node_id: str
    at_qn: Rational | int


class ArrangementVariation(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    section_id: str
    endpoint_note_id: str
    pitch_offsets_semitones: list[int]
    timing_offsets_qn: list[Rational | int]
    pitch_min: int
    pitch_max: int


class ArrangementDefinition(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    label: str
    structure: ArtifactHandle
    expected_structure_revision: str
    clock: TimeContext
    origin: Position
    length_qn: Rational | int
    sections: list[ArrangementSection]
    locked_section_ids: list[str]
    variations: list[ArrangementVariation]
    seeds: DevelopmentSeeds
    attribution: StructureAttribution
