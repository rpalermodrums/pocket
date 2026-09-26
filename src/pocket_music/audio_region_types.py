# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit independently sourced local PCM region capture input."""
from typing import Literal

from typing_extensions import TypedDict

from .artifact_store import ArtifactHandle
from .audio_hypothesis_types import AudioHypothesisSettings
from .audio_model_types import AudioPulseModel, AudioPulseSettings
from .audio_note_types import AudioNoteModel, AudioNoteSettings


class AudioRegionSource(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    path: str
    expected_sha256: str
    start_frame: int
    frames: int
    source_origin: Literal['independently_acquired', 'user_recording']


class InlineAudioRegion(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    kind: Literal['inline']
    source: AudioRegionSource


class CapturedAudioRegion(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    kind: Literal['captured']
    region: ArtifactHandle


AudioRegionInput = InlineAudioRegion | CapturedAudioRegion


class PeekRegionAnalysis(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    kind: Literal['peek']
    settings: AudioHypothesisSettings


class LearnedPulseRegionAnalysis(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    kind: Literal['learned_pulse']
    model: AudioPulseModel
    settings: AudioPulseSettings


class LearnedNoteRegionAnalysis(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    kind: Literal['learned_notes']
    model: AudioNoteModel
    settings: AudioNoteSettings


AudioRegionAnalysis = PeekRegionAnalysis | LearnedPulseRegionAnalysis | LearnedNoteRegionAnalysis
