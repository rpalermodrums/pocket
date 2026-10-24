# SPDX-License-Identifier: AGPL-3.0-only

from typing import Final, Literal, NotRequired

from typing_extensions import TypedDict

_CLOSED: Final = {"extra": "forbid"}
SampleRate = Literal[44100, 48000, 88200, 96000]
SegmentKind = Literal["tone", "ramp", "silence", "impulse", "nonfinite", "gap", "counter_reset",
                      "counter_backward", "stall", "clocks", "device_lost", "rate_change", "channel_reorder",
                      "hogged"]


class ListenTone(TypedDict):
    __pydantic_config__ = _CLOSED
    channel: int
    hz: float
    dbfs: float


class ListenImpulse(TypedDict):
    __pydantic_config__ = _CLOSED
    channel: int
    offset_frames: int
    dbfs: float


class ListenSyntheticSegment(TypedDict):
    """One scripted stretch of synthetic input; each kind takes its own exact fields."""
    __pydantic_config__ = _CLOSED
    kind: SegmentKind
    frames: NotRequired[int]
    tones: NotRequired[list[ListenTone]]
    impulses: NotRequired[list[ListenImpulse]]
    channels: NotRequired[list[int]]
    offsets: NotRequired[list[int]]
    period_frames: NotRequired[int]
    dbfs: NotRequired[float]
    value: NotRequired[Literal["nan", "inf", "-inf"]]
    via: NotRequired[Literal["device_gap", "helper_ring_overflow", "processor_overload"]]
    sample_time: NotRequired[int]
    delta_frames: NotRequired[int]
    ms: NotRequired[int]
    counter: NotRequired[Literal["advances", "pauses"]]
    sample_time_valid: NotRequired[bool]
    host_time_valid: NotRequired[bool]
    sample_rate: NotRequired[SampleRate]
    order: NotRequired[list[int]]


class ListenSyntheticDevice(TypedDict):
    __pydantic_config__ = _CLOSED
    uid: str
    name: str
    input_channels: int
    sample_rate: SampleRate
    transport: NotRequired[Literal["synthetic", "virtual", "usb", "builtin"]]


class ListenSyntheticScript(TypedDict):
    __pydantic_config__ = _CLOSED
    block_frames: int
    devices: list[ListenSyntheticDevice]
    segments: list[ListenSyntheticSegment]
    host_drift_ppm: NotRequired[float]


class ListenSyntheticAdapter(TypedDict):
    """Scripted, deterministic input; never describes a real device."""
    __pydantic_config__ = _CLOSED
    kind: Literal["synthetic"]
    script: ListenSyntheticScript


class ListenCoreAudioAdapter(TypedDict):
    """The optional macOS helper, named by the build_id listen_build_helper returned."""
    __pydantic_config__ = _CLOSED
    kind: Literal["coreaudio"]
    build_id: str


ListenAdapter = ListenSyntheticAdapter | ListenCoreAudioAdapter


class ListenDevice(TypedDict):
    """Selects one input by its stable UID; display names never select a device."""
    __pydantic_config__ = _CLOSED
    uid: str
    expected_sample_rate: SampleRate


class ListenRoute(TypedDict):
    """A declared audio route: 0-based input channels with one role each."""
    __pydantic_config__ = _CLOSED
    route_id: str
    role: Literal["master", "cue", "unlabeled"]
    channels: list[int]


class ListenNearSilence(TypedDict):
    __pydantic_config__ = _CLOSED
    level_dbfs: float
    min_duration_ms: int


class ListenMeasurement(TypedDict):
    __pydantic_config__ = _CLOSED
    clip_threshold_dbfs: float
    near_silence: ListenNearSilence
    window_frames: NotRequired[int]


class ListenBuffering(TypedDict):
    __pydantic_config__ = _CLOSED
    ring_seconds: NotRequired[int]


class ListenHealth(TypedDict):
    __pydantic_config__ = _CLOSED
    stale_after_ms: NotRequired[int]


class ListenDelivery(TypedDict):
    __pydantic_config__ = _CLOSED
    meter_coalesce_windows: NotRequired[int]
    normal_retention_events: NotRequired[int]
    normal_retention_bytes: NotRequired[int]
    health_retention_events: NotRequired[int]
    health_retention_bytes: NotRequired[int]


class ListenLimits(TypedDict):
    __pydantic_config__ = _CLOSED
    max_runtime_seconds: NotRequired[int]


class ListenConfig(TypedDict):
    """One input, its audio routes and bounded settings; sealed unchanged for the session."""
    __pydantic_config__ = _CLOSED
    adapter: ListenAdapter
    device: ListenDevice
    routes: list[ListenRoute]
    measurement: ListenMeasurement
    buffering: NotRequired[ListenBuffering]
    health: NotRequired[ListenHealth]
    delivery: NotRequired[ListenDelivery]
    limits: NotRequired[ListenLimits]


class ListenCursor(TypedDict):
    """A place in one runtime's event sequence, as listen_start or listen_observe returned it."""
    __pydantic_config__ = _CLOSED
    runtime_epoch: str
    sequence: int


ListenEventKind = Literal["measurement", "health"]
ListenOnExpired = Literal["refuse", "resume_from_earliest"]
