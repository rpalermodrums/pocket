"""Explicit authored evidence matching; no inferred pulse-to-note association."""
from typing import Literal

from typing_extensions import TypedDict

from .artifact_store import ArtifactHandle
from .material_types import Rational


class TimingAlignment(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    kind: Literal['declared_unwarped_source_clock']
    original_sha256: str
    sample_rate: int
    source_anchor_frame_q: Rational
    host_anchor_seconds_q: Rational
    time_map: ArtifactHandle
    clip_id: str


class AttackPoint(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    kind: Literal['annotation_attack']


class PulsePoint(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    kind: Literal['declared_pulse_point']
    original_source_frame_q: Rational
    statement: str


class TimingMatch(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    note_id: str
    annotation_id: str
    point: AttackPoint | PulsePoint


class TimingAlternative(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    alternative_id: str
    statement: str
    uncertainty: list[str]
    strength: Rational
    maximum_shift_qn: Rational
    matches: list[TimingMatch]
