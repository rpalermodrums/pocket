# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit coordinate declarations for immutable region corrections."""
from typing import Literal

from typing_extensions import TypedDict

from .audio_hypothesis_types import (
    AttackHypothesis,
    AudioEvidenceSupport,
    AudioHypothesisCorrection,
    NoteHypothesis,
    PhraseAnchorHypothesis,
)
from .material_types import Rational


class OriginalPulseLatticeOrigin(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    local_estimate_seconds: float | int
    original_offset_seconds_q: Rational


class OriginalPulseHypothesis(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    kind: Literal['pulse_candidate']
    start_frame: int
    end_frame_exclusive: int
    bpm: float | int
    lattice_origin: OriginalPulseLatticeOrigin


class OriginalRegionCorrection(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    correction_id: str
    supersedes: list[str]
    annotation: AttackHypothesis | OriginalPulseHypothesis | PhraseAnchorHypothesis | NoteHypothesis
    support: list[AudioEvidenceSupport]
    uncertainty: list[str]


class LocalRegionCorrections(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    coordinate_space: Literal['local_crop_frame']
    corrections: list[AudioHypothesisCorrection]


class OriginalRegionCorrections(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    coordinate_space: Literal['original_source_frame']
    corrections: list[OriginalRegionCorrection]


AudioRegionCorrectionBatch = LocalRegionCorrections | OriginalRegionCorrections
