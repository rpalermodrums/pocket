# SPDX-License-Identifier: AGPL-3.0-only
"""Strict offline member-channel expression planning inputs; no native authority."""
from __future__ import annotations

from typing import Literal

from typing_extensions import TypedDict

from .artifact_store import ArtifactHandle
from .lifecycle_types import LifecycleInitialState
from .material_types import Rational


class ExpressionLifecycleRequest(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    clip_id: str
    initial_state: LifecycleInitialState
    horizon_qn: Rational | int
    release_tail_qn: Rational | int
    equal_time_order: Literal['note_off_cc_note_on', 'cc_note_off_note_on']
    source_basis: Literal['canonical_notes_retained_cc64']


class ExpressionAttribution(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    actor: str
    actor_kind: Literal['human', 'agent']
    statement: str
    evidence: list[ArtifactHandle]


class ReceiverAssumption(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    label: str
    zone: Literal['lower', 'upper']
    manager_channel: int
    member_channels: list[int]
    member_bend_range_semitones: int
    manager_pitch_policy: Literal['neutral_no_pitch_messages']
    configuration_policy: Literal['assume_preconfigured_no_setup_messages']
    configuration_status: Literal['declared_unverified']
    attribution: ExpressionAttribution
    instrument_state: ArtifactHandle | None


class ExpressionNeutral(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    pitch_cents: Literal[0]
    pressure_7bit: int
    slide_7bit: int


class ExpressionEncoding(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    source_channel_policy: Literal['single_source_channel_to_zone']
    sustain_route: Literal['manager_cc64']
    allocation: Literal['lowest_available_zone_order']
    exhaustion: Literal['reject_no_stealing']
    reservation: Literal['through_symbolic_release_plus_declared_tail']
    same_pitch_policy: Literal['new_per_note_channel_realization']
    tuning: Literal['tuning:12tet-a440']
    neutral: ExpressionNeutral
    curve_mode: Literal['step_only']
    rounding: Literal['nearest_ties_even']
    max_pitch_error_cents: Rational | int
    max_control_error_normalized: Rational | int
    reuse_order: Literal['old_expression_then_declared_off_cc_then_reset_setup_on']


class ExpressionConfiguration(TypedDict):
    __pydantic_config__ = {'extra': 'forbid', 'strict': True}  # noqa: RUF012
    lifecycle: ArtifactHandle | ExpressionLifecycleRequest
    receiver_assumption: ReceiverAssumption
    encoding: ExpressionEncoding
