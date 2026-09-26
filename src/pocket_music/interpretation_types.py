# SPDX-License-Identifier: AGPL-3.0-only
"""Closed musical claims; observations, choices and coordinate clocks stay distinct."""
from typing import Literal

from typing_extensions import TypedDict

from .artifact_store import ArtifactHandle
from .music_types import Rational


class PointClaim(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    kind: Literal["onset", "bar_one", "phrase_start"]
    status: Literal["selected", "authored"]
    source_frame_q: Rational


class PulseClaim(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    kind: Literal["pulse"]
    status: Literal["selected", "authored"]
    bpm: float
    interval_frames: list[int]


class UnresolvedClaim(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    kind: Literal["onset", "pulse", "bar_one", "phrase_start"]
    status: Literal["unresolved"]
    interval_frames: list[int]


InterpretationClaim = PointClaim | PulseClaim | UnresolvedClaim


class InterpretationEvidence(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    hypotheses: ArtifactHandle
    expected_revision: str
    annotation_id: str


class SelectionBinding(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    kind: Literal["selection"]
    binding_id: str


class AnchorBinding(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    kind: Literal["anchor"]
    binding_id: str
    anchor_id: str
    label: str


InterpretationBinding = SelectionBinding | AnchorBinding
