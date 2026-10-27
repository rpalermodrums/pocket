# SPDX-License-Identifier: AGPL-3.0-only
"""Strict inputs for explicit file-only whole-clip sequence construction."""
from __future__ import annotations

from typing import Literal

from typing_extensions import TypedDict

from .artifact_store import ArtifactHandle
from .material_types import MaterialRecord, Position, Rational
from .time_types import TimeContext


class SequenceMaterial(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    key: str
    material: ArtifactHandle | MaterialRecord


class SequenceOccurrence(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    occurrence_id: str
    material_key: str
    material_revision: str
    clip_id: str
    at_qn: Rational | int


class SequenceDefinition(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    label: str
    materials: list[SequenceMaterial]
    clock: TimeContext
    origin: Position
    length_qn: Rational | int
    occurrences: list[SequenceOccurrence]
    controller_policy: Literal['reject_present']
    expression_policy: Literal['reject_present']
    overlap_policy: Literal['reject_same_channel_pitch']
