"""Strict portable audio-evidence and caller-authored correction inputs."""
from __future__ import annotations

from typing import Literal

from typing_extensions import TypedDict


class AudioHypothesisSource(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    path: str
    expected_sha256: str
    start_frame: int
    frames: int
    source_origin: Literal['independently_acquired', 'user_recording']


class AudioHypothesisSettings(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    bpm_hint: float | int | None
    beats_per_bar: int


class AudioHypothesisAttribution(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    actor: str
    actor_kind: Literal['human', 'agent']
    statement: str
    uncertainty: list[str]


class AudioEvidenceSupport(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    kind: Literal['analysis_pointer', 'annotation_id']
    reference: str


class AttackHypothesis(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    kind: Literal['attack']
    source_frame: int
    strength_relative: float | int | None


class PulseHypothesis(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    kind: Literal['pulse_candidate']
    start_frame: int
    end_frame_exclusive: int
    bpm: float | int
    source_lattice_origin_seconds: float | int


class PhraseAnchorHypothesis(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    kind: Literal['phrase_anchor']
    start_frame: int
    end_frame_exclusive: int
    label: str


class NoteHypothesis(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    kind: Literal['note_hypothesis']
    start_frame: int
    end_frame_exclusive: int
    midi_note: int
    cents: float | int
    tuning_ref: str


class AudioHypothesisCorrection(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    correction_id: str
    supersedes: list[str]
    annotation: AttackHypothesis | PulseHypothesis | PhraseAnchorHypothesis | NoteHypothesis
    support: list[AudioEvidenceSupport]
    uncertainty: list[str]
