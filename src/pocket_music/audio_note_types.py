# SPDX-License-Identifier: AGPL-3.0-only
"""Strict optional ONNX note-hypothesis declarations, independent of MIDI/native work."""
from typing import Literal

from typing_extensions import TypedDict

from .artifact_store import ArtifactHandle
from .audio_model_types import AudioModelFile


class AudioNoteModelDeclaration(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    adapter: Literal['basic_pitch_onnx_cpu_v1']
    executable: AudioModelFile
    weights: AudioModelFile
    expected_profile: str | None


class InlineAudioNoteModel(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    kind: Literal['inline']
    declaration: AudioNoteModelDeclaration
    qualification: Literal['synthetic_onnx_cpu_v1']


class InspectedAudioNoteModel(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    kind: Literal['inspected']
    model: ArtifactHandle


AudioNoteModel = InlineAudioNoteModel | InspectedAudioNoteModel


class AudioNoteSettings(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    device: Literal['cpu']
    dtype: Literal['float32']
    threads: Literal[1]
    downmix: Literal['arithmetic_mean']
    resampler: Literal['soxr_hq']
    decoder: Literal['basic_pitch_0_4_0_false_false_v1']
    onset_threshold: Literal[0.5]
    frame_threshold: Literal[0.3]
    min_note_frames: Literal[11]
    energy_tol: Literal[11]
    infer_onsets: Literal[False]
    melodia_trick: Literal[False]
