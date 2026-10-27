# SPDX-License-Identifier: AGPL-3.0-only
"""Typed transport records for explicit instrument observations and offline plans."""
from __future__ import annotations

from typing import Literal, NotRequired

from typing_extensions import TypedDict

from .artifact_store import ArtifactHandle


class InstrumentIdentity(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    manufacturer: str
    product: str
    build: str
    format: Literal["VST3", "AU", "AAX", "VST2", "native", "unknown"]
    class_id: str
    instance_id: str
    host_build: str
    os_arch: str
    binding: str


class ParameterDescriptor(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    parameter_id: str
    name: str
    identity_quality: Literal["native_id", "host_slot_bound"]
    native_min: float
    native_max: float
    value: float
    unit: str
    quantized: bool
    values: list[float]
    writable: bool
    automation_owner: Literal["none", "host_automation", "macro", "remote", "unknown"]
    semantic_key: str | None
    mapping_evidence: str | None
    display_value: NotRequired[str | None]


class InstrumentObservation(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    identity: InstrumentIdentity
    topology_token: str
    parameters: list[ParameterDescriptor]
    attribution: str
    observed_at: str
    source_kind: Literal["saved", "live", "manual"]
    opaque_state: NotRequired[ArtifactHandle | None]
    dependencies: NotRequired[list[ArtifactHandle]]


class ParameterChange(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    parameter_id: str
    value: float


class SoundAlternative(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    label: str
    hypothesis: str
    changes: list[ParameterChange]


class RationalTime(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    n: int
    d: int


class CurveTarget(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    kind: Literal["cc", "pitch_bend", "channel_pressure", "per_note_pitch",
                  "per_note_pressure", "per_note_slide", "host_parameter", "macro"]
    target_id: str
    scope: Literal["channel", "note", "instance"]
    unit: str
    value_min: float
    value_max: float
    quantized: bool
    values: list[float]
    ownership: Literal["none", "host_automation", "macro", "remote", "unknown"]
    value_mode: Literal["absolute", "additive", "multiplicative"]
    channel: NotRequired[int]
    note_id: NotRequired[str]
    resize_policy: NotRequired[Literal["stretch_with_gate", "preserve_fraction", "crop", "preserve_ms"]]
    parameter_layout_sha256: NotRequired[str]


class CurvePoint(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    time: RationalTime
    value: float
    order: int


class CurveOperation(TypedDict, total=False):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    op: Literal["create", "shift", "scale_time", "scale_value", "offset", "smooth", "simplify", "splice"]
    points: list[CurvePoint]
    space: Literal["arrangement_qn", "clip_qn", "phrase_qn", "note_relative_qn"]
    interpolation: Literal["linear", "step"]
    amount: RationalTime | float
    factor: RationalTime | float
    origin: RationalTime
    window: int
    tolerance: float
    start: RationalTime
    end: RationalTime
