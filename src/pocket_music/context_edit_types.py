# SPDX-License-Identifier: AGPL-3.0-only
"""Literal context edits and explicit protected fields; no implicit linked selection."""
from typing import Literal

from typing_extensions import TypedDict

from .artifact_store import ArtifactHandle
from .music_types import Rational


class OccurrenceSlip(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    kind: Literal["occurrence_slip_source"]
    occurrence_ids: list[str]
    delta_frames: int


class OccurrenceShift(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    kind: Literal["occurrence_shift_timeline"]
    occurrence_ids: list[str]
    delta_qn: Rational


class AnchorRebind(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    kind: Literal["anchor_rebind"]
    anchor_id: str
    interpretation: ArtifactHandle
    binding_id: str


ContextEdit = OccurrenceSlip | OccurrenceShift | AnchorRebind


class ContextLock(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    section: Literal["occurrences", "anchors"]
    object_id: str
    fields: list[str]
