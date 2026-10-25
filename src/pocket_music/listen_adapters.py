# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import json
import math
import queue
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Literal, Protocol

import numpy as np

from .errors import PocketError, contained
from .listen_native import (
    ADAPTER_QUEUE_FRAMES,
    BLOCK,
    BLOCK_HEADER,
    COREAUDIO_PROFILE,
    EVENT,
    EXIT,
    HEADER,
    HEARTBEAT,
    HEARTBEAT_TYPE,
    HELLO,
    HELLO_REFUSALS,
    HELPER_RING_SECONDS,
    HELPER_SCHEMAS,
    MAGIC,
    MAX_BLOCK_FRAMES,
    MICROPHONE_STATES,
    ORDERLY_EXIT_CODES,
    SAMPLE_RATES,
    START_REFUSALS,
    STARTED,
    SYNTHETIC_PROFILE,
    copy_helper,
    has_four_byte_samples,
    is_float32,
    remove_helper_copy,
    verify_build,
)

_CAPS = {HELLO: 65536, BLOCK: 1024 * 1024, EVENT: 16384, HEARTBEAT_TYPE: HEARTBEAT.size, EXIT: 16384,
         STARTED: 16384}
_ENDING_HELPER_EVENTS = ("device_lost", "sample_rate_changed", "format_changed", "channel_map_changed",
                         "device_hogged", "device_property_unreadable")
_ONGOING_HELPER_EVENTS = ("processor_overload", "devices_changed")
HELPER_EVENTS = (*_ENDING_HELPER_EVENTS, *_ONGOING_HELPER_EVENTS)
ENDING_EVENTS = (*_ENDING_HELPER_EVENTS, "end_of_stream", "helper_exited", "helper_protocol_error")


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    uid: str
    name: str
    manufacturer: str | None
    model_uid: str | None
    transport: str
    instance: str


@dataclass(frozen=True, slots=True)
class NegotiatedFormat:
    sample_rate: int
    sample_format: str
    device_input_channels: int
    buffer_frame_size: int | None
    device_latency_frames: int | None
    safety_offset_frames: int | None
    has_sample_time: bool
    host_time: bool
    host_time_scope: str
    stream_configuration: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class OpenResult:
    identity: DeviceIdentity | None
    format: NegotiatedFormat | None
    refusal: tuple[str, ...]
    inventory: tuple[dict, ...] = ()
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StartResult:
    started: bool
    refusal: tuple[str, ...]
    selected_channels: tuple[int, ...]
    ring_frames: int | None = None
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdapterBlock:
    frames: int
    pcm: np.ndarray
    device_sample_time: float | None
    device_host_time_ns: int | None
    received_monotonic_ns: int
    adapter_dropped_frames_total: int
    callback_ns: int | None = None


@dataclass(frozen=True, slots=True)
class AdapterEvent:
    type: Literal[*ENDING_EVENTS, *_ONGOING_HELPER_EVENTS]
    detail: dict
    received_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class CloseResult:
    cause: Literal["clean", "exited_with_error", "terminated", "killed", "timeout", "not_started"]
    clean: bool
    detail: dict = field(default_factory=dict)


class CaptureAdapter(Protocol):
    profile: str

    def open(self) -> OpenResult: ...
    def start(self, selected_channels: tuple[int, ...]) -> StartResult: ...
    def read(self, timeout_ns: int) -> AdapterBlock | AdapterEvent | None: ...
    def close(self, deadline_ns: int) -> CloseResult: ...
    def stats(self) -> dict: ...


def _ns(frames, rate):
    return (int(frames) * 1_000_000_000) // int(rate)


def _amplitude(dbfs):
    return 10.0 ** (float(dbfs) / 20.0)


_SEGMENT_FIELDS = {
    "tone": ({"frames", "tones"}, set()),
    "ramp": ({"frames", "channels", "period_frames", "dbfs"}, set()),
    "silence": ({"frames"}, set()),
    "impulse": ({"frames", "impulses"}, set()),
    "nonfinite": ({"frames", "channels", "offsets", "value"}, set()),
    "gap": ({"frames", "via"}, set()),
    "counter_reset": (set(), {"sample_time"}),
    "counter_backward": ({"delta_frames"}, set()),
    "stall": ({"ms", "counter"}, set()),
    "clocks": ({"sample_time_valid", "host_time_valid"}, set()),
    "device_lost": (set(), set()),
    "hogged": (set(), set()),
    "rate_change": ({"sample_rate"}, set()),
    "channel_reorder": ({"order"}, set()),
}
_AUDIO_KINDS = ("tone", "ramp", "silence", "impulse", "nonfinite")
_FINAL_KINDS = ("device_lost", "hogged", "rate_change", "channel_reorder")
_SCRIPT_HOURS = 4


def _refuse(field_name, rule):
    raise PocketError(f"Synthetic script field {field_name} is invalid", hint=f"{field_name} {rule}"[:400])


def _int(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        _refuse(name, f"must be an integer from {low} to {high}.")
    return value


def _number(value, low, high, name, *, open_low=False, open_high=False):
    # The range comes first: Python compares an int with a float exactly, while isfinite would convert an
    # int too large for a float and raise OverflowError instead of refusing it.
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or value < low or value > high
            or (open_low and value == low) or (open_high and value == high) or not math.isfinite(value)):
        _refuse(name, f"must be a finite number from {low} to {high}.")
    return float(value)


def _channels(value, limit, name, *, maximum=16):
    if (not isinstance(value, list) or not 1 <= len(value) <= maximum
            or not all(type(c) is int and 0 <= c < limit for c in value) or len(set(value)) != len(value)):
        _refuse(name, f"must list 1 to {maximum} distinct 0-based channels below {limit}.")
    return list(value)


def validate_synthetic_script(script):
    if not isinstance(script, dict) or set(script) - {"block_frames", "devices", "segments", "host_drift_ppm"} \
            or not {"block_frames", "devices", "segments"} <= set(script):
        _refuse("script", "takes block_frames, devices and segments, and optionally host_drift_ppm.")
    block = _int(script["block_frames"], 32, MAX_BLOCK_FRAMES, "script.block_frames")
    devices = script["devices"]
    if not isinstance(devices, list) or not 1 <= len(devices) <= 8:
        _refuse("script.devices", "must list 1 to 8 synthetic devices.")
    normalized_devices = []
    for i, device in enumerate(devices):
        name = f"script.devices[{i}]"
        if not isinstance(device, dict) or not {"uid", "name", "input_channels", "sample_rate"} <= set(device) \
                or set(device) - {"uid", "name", "input_channels", "sample_rate", "transport"}:
            _refuse(name, "takes uid, name, input_channels and sample_rate, and optionally transport.")
        uid = device["uid"]
        if (not isinstance(uid, str) or not uid.startswith("synthetic:") or not 1 <= len(uid) <= 256
                or not uid.isprintable()):
            _refuse(f"{name}.uid", "must start with 'synthetic:' and have at most 256 printable characters.")
        if not isinstance(device["name"], str) or not 1 <= len(device["name"]) <= 256 \
                or not device["name"].isprintable():
            _refuse(f"{name}.name", "must be 1 to 256 printable characters; names are display only.")
        _int(device["input_channels"], 1, 16, f"{name}.input_channels")
        if type(device["sample_rate"]) is not int or device["sample_rate"] not in SAMPLE_RATES:
            _refuse(f"{name}.sample_rate", "must be one of 44100, 48000, 88200 or 96000.")
        if not isinstance(device.get("transport", "synthetic"), str) \
                or device.get("transport", "synthetic") not in ("synthetic", "virtual", "usb", "builtin"):
            _refuse(f"{name}.transport", "must be synthetic, virtual, usb or builtin.")
        normalized_devices.append({**device, "transport": device.get("transport", "synthetic")})
    uids = [d["uid"] for d in devices]
    if len(set(uids)) != len(uids):
        _refuse("script.devices", "must give each device its own uid; display names may repeat.")
    min_rate = min(d["sample_rate"] for d in devices)
    max_channels = max(d["input_channels"] for d in devices)
    drift = _number(script.get("host_drift_ppm", 0.0), -1000.0, 1000.0, "script.host_drift_ppm")
    segments = script["segments"]
    if not isinstance(segments, list) or not 1 <= len(segments) <= 256:
        _refuse("script.segments", "must list 1 to 256 segments.")
    limit_frames = _SCRIPT_HOURS * 3600 * min_rate
    total, counter, normalized = 0, 0, []
    for i, segment in enumerate(segments):
        name = f"script.segments[{i}]"
        if not isinstance(segment, dict) or not isinstance(segment.get("kind"), str) \
                or segment["kind"] not in _SEGMENT_FIELDS:
            _refuse(f"{name}.kind", "must be one of " + ", ".join(_SEGMENT_FIELDS) + ".")
        kind = segment["kind"]
        required, optional = _SEGMENT_FIELDS[kind]
        keys = set(segment) - {"kind"}
        if not required <= keys or keys - required - optional:
            fields = ", ".join(sorted(required | optional)) or "no other fields"
            _refuse(name, f"({kind}) takes exactly: {fields}.")
        if i and segments[i - 1].get("kind") in _FINAL_KINDS:
            _refuse(name, f"follows {segments[i - 1]['kind']}, which ends the stream; end the script there.")
        item = dict(segment)
        if "frames" in required:
            frames = _int(segment["frames"], 1, limit_frames, f"{name}.frames")
            total += frames
            counter += frames
        if kind == "tone":
            tones = segment["tones"]
            if not isinstance(tones, list) or not 1 <= len(tones) <= 8:
                _refuse(f"{name}.tones", "must list 1 to 8 tones.")
            normalized_tones = []
            for j, tone in enumerate(tones):
                if not isinstance(tone, dict) or set(tone) != {"channel", "hz", "dbfs"}:
                    _refuse(f"{name}.tones[{j}]", "takes exactly channel, hz and dbfs.")
                _int(tone["channel"], 0, max_channels - 1, f"{name}.tones[{j}].channel")
                normalized_tones.append({"channel": tone["channel"],
                                         "hz": _number(tone["hz"], 0.0, min_rate / 2, f"{name}.tones[{j}].hz",
                                                       open_low=True, open_high=True),
                                         "dbfs": _number(tone["dbfs"], -120.0, 6.0, f"{name}.tones[{j}].dbfs")})
            item["tones"] = normalized_tones
        elif kind == "ramp":
            item["channels"] = _channels(segment["channels"], max_channels, f"{name}.channels")
            _int(segment["period_frames"], 2, min_rate, f"{name}.period_frames")
            item["dbfs"] = _number(segment["dbfs"], -120.0, 6.0, f"{name}.dbfs")
        elif kind == "impulse":
            impulses = segment["impulses"]
            if not isinstance(impulses, list) or not 1 <= len(impulses) <= 64:
                _refuse(f"{name}.impulses", "must list 1 to 64 impulses.")
            normalized_impulses = []
            for j, impulse in enumerate(impulses):
                if not isinstance(impulse, dict) or set(impulse) != {"channel", "offset_frames", "dbfs"}:
                    _refuse(f"{name}.impulses[{j}]", "takes exactly channel, offset_frames and dbfs.")
                _int(impulse["channel"], 0, max_channels - 1, f"{name}.impulses[{j}].channel")
                _int(impulse["offset_frames"], 0, segment["frames"] - 1, f"{name}.impulses[{j}].offset_frames")
                normalized_impulses.append({**impulse, "dbfs": _number(impulse["dbfs"], -120.0, 12.0,
                                                                       f"{name}.impulses[{j}].dbfs")})
            item["impulses"] = normalized_impulses
        elif kind == "nonfinite":
            item["channels"] = _channels(segment["channels"], max_channels, f"{name}.channels")
            offsets = segment["offsets"]
            if (not isinstance(offsets, list) or not 1 <= len(offsets) <= 64
                    or not all(type(o) is int and 0 <= o < segment["frames"] for o in offsets)
                    or len(set(offsets)) != len(offsets)):
                _refuse(f"{name}.offsets", "must list 1 to 64 distinct frame offsets inside the segment.")
            if not isinstance(segment["value"], str) or segment["value"] not in ("nan", "inf", "-inf"):
                _refuse(f"{name}.value", "must be nan, inf or -inf.")
        elif kind == "gap":
            if not isinstance(segment["via"], str) \
                    or segment["via"] not in ("device_gap", "helper_ring_overflow", "processor_overload"):
                _refuse(f"{name}.via", "must be device_gap, helper_ring_overflow or processor_overload.")
        elif kind == "counter_reset":
            if counter < 1:
                _refuse(name, "(counter_reset) needs frames before it; a reset restarts a counter that has "
                              "moved.")
            counter = _int(segment.get("sample_time", 0), 0, min(counter, min_rate) - 1,
                           f"{name}.sample_time")
            item["sample_time"] = counter
        elif kind == "counter_backward":
            _int(segment["delta_frames"], -counter, -1, f"{name}.delta_frames")
            counter += segment["delta_frames"]
        elif kind == "stall":
            _int(segment["ms"], 50, 30000, f"{name}.ms")
            if not isinstance(segment["counter"], str) or segment["counter"] not in ("advances", "pauses"):
                _refuse(f"{name}.counter", "must be advances or pauses.")
            stalled = segment["ms"] * min_rate // 1000
            total += stalled
            counter += stalled if segment["counter"] == "advances" else 0
        elif kind == "clocks":
            if type(segment["sample_time_valid"]) is not bool or type(segment["host_time_valid"]) is not bool:
                _refuse(name, "(clocks) takes booleans sample_time_valid and host_time_valid.")
        elif kind == "rate_change":
            if type(segment["sample_rate"]) is not int or segment["sample_rate"] not in SAMPLE_RATES:
                _refuse(f"{name}.sample_rate", "must be one of 44100, 48000, 88200 or 96000.")
        elif kind == "channel_reorder":
            order = segment["order"]
            if (not isinstance(order, list) or not 2 <= len(order) <= 16
                    or not all(type(o) is int for o in order) or sorted(order) != list(range(len(order)))):
                _refuse(f"{name}.order", "must be a permutation of 0-based device channels, 2 to 16 long.")
        normalized.append(item)
    if total > limit_frames:
        _refuse("script.segments", f"must total at most {_SCRIPT_HOURS} hours of frames at the lowest "
                                   f"sample_rate the script lists, {limit_frames} frames.")
    return {"block_frames": block, "devices": normalized_devices, "segments": normalized, "host_drift_ppm": drift}


def synthetic_inventory(script):
    names = [d["name"] for d in script["devices"]]
    rows = []
    for device in script["devices"][:32]:
        channels, rate = device["input_channels"], device["sample_rate"]
        rows.append({"uid": device["uid"], "name": device["name"], "manufacturer": "Pocket synthetic",
                     "model_uid": "synthetic-script", "transport": device.get("transport", "synthetic"),
                     "audio_object_id": None, "alive": True, "hog_mode_pid": -1, "nominal_sample_rate": rate,
                     "input_channels": channels,
                     "input_streams": [{"starting_channel": 0, "channels": channels,
                                        "virtual_format": {"format_id": "lpcm", "float": True,
                                                           "bits_per_channel": 32,
                                                           "channels_per_frame": channels, "interleaved": True,
                                                           "packed": True, "native_endian": True,
                                                           "sample_rate": rate}}],
                     "channel_names": [""] * channels, "buffer_frame_size": script["block_frames"],
                     "display_name_shared": names.count(device["name"]) > 1,
                     "name_shared_with": names.count(device["name"]) - 1, "profile_fit": "supported",
                     "supported_channels": list(range(channels))})
    return tuple(rows)


class SyntheticAdapter:
    profile = SYNTHETIC_PROFILE

    def __init__(self, script, device_uid, expected_rate, clock, pacing="stepped"):
        if pacing not in ("stepped", "realtime"):
            raise ValueError("pacing must be stepped or realtime")
        if pacing == "stepped" and not hasattr(clock, "advance"):
            raise ValueError("stepped pacing needs a test clock that can advance")
        self._script = validate_synthetic_script(script)
        self._clock = clock
        self._expected_rate = expected_rate
        matches = [i for i, d in enumerate(self._script["devices"]) if d["uid"] == device_uid]
        self._index = matches[0] if matches else None
        self._device = self._script["devices"][self._index] if matches else None
        self._rate = self._device["sample_rate"] if matches else expected_rate
        self._block = self._script["block_frames"]
        self._drift = self._script["host_drift_ppm"]
        self._selected = ()
        self._started = self._closed = self._done = False
        self._segment = 0
        self._segment_offset = 0
        self._elapsed = 0
        self._counter = 0
        self._dropped = 0
        self._sample_valid = self._host_valid = True
        self._pending = deque()
        self._origin_ns = 0
        self._blocks = 0

    def open(self):
        inventory = synthetic_inventory(self._script)
        if self._device is None:
            return OpenResult(None, None, ("device_not_found",), inventory)
        d = self._device
        identity = DeviceIdentity(d["uid"], d["name"], "Pocket synthetic", "synthetic-script", d["transport"],
                                  f"synthetic-instance-{self._index + 1}")
        fmt = NegotiatedFormat(self._rate, "float32", d["input_channels"], self._block, None, None, True, True,
                               "synthetic_clock_ns", (d["input_channels"],))
        refusal = ("sample_rate_mismatch",) if self._rate != self._expected_rate else ()
        return OpenResult(identity, fmt, refusal, inventory,
                          {"nominal_sample_rate": self._rate, "expected_sample_rate": self._expected_rate})

    def start(self, selected_channels):
        selected = tuple(selected_channels)
        if (self._device is None or not selected or len(set(selected)) != len(selected)
                or any(c >= self._device["input_channels"] for c in selected)):
            return StartResult(False, ("no_metered_channels_exposed",), selected)
        self._selected = selected
        self._started = True
        self._origin_ns = self._clock.monotonic_ns()
        return StartResult(True, (), selected, None, {})

    def _wait_until(self, due_ns):
        now = self._clock.monotonic_ns()
        if due_ns > now:
            self._clock.sleep_ns(due_ns - now)

    def _host_time(self, elapsed):
        return self._origin_ns + round(elapsed * 1e9 * (1.0 + self._drift * 1e-6) / self._rate)

    def _event(self, kind, detail):
        return AdapterEvent(kind, detail, self._clock.monotonic_ns())

    def _pcm(self, segment, offset, frames):
        channels = self._device["input_channels"]
        data = np.zeros((frames, channels), dtype=np.float64)
        kind = segment["kind"]
        absolute = np.arange(self._elapsed, self._elapsed + frames, dtype=np.float64)
        if kind == "tone":
            for tone in segment["tones"]:
                if tone["channel"] < channels:
                    data[:, tone["channel"]] += _amplitude(tone["dbfs"]) * np.sin(
                        2.0 * math.pi * tone["hz"] * absolute / self._rate)
        elif kind == "ramp":
            period = segment["period_frames"]
            values = _amplitude(segment["dbfs"]) * (2.0 * np.mod(absolute, period) / (period - 1) - 1.0)
            for c in segment["channels"]:
                if c < channels:
                    data[:, c] = values
        elif kind == "impulse":
            for impulse in segment["impulses"]:
                if impulse["channel"] < channels and offset <= impulse["offset_frames"] < offset + frames:
                    data[impulse["offset_frames"] - offset, impulse["channel"]] += _amplitude(impulse["dbfs"])
        elif kind == "nonfinite":
            value = {"nan": math.nan, "inf": math.inf, "-inf": -math.inf}[segment["value"]]
            for position in segment["offsets"]:
                if offset <= position < offset + frames:
                    for c in segment["channels"]:
                        if c < channels:
                            data[position - offset, c] = value
        return np.ascontiguousarray(data[:, list(self._selected)].astype(np.float32))

    def read(self, timeout_ns):
        deadline = self._clock.monotonic_ns() + max(0, int(timeout_ns))
        while True:
            if self._pending:
                return self._pending.popleft()
            if self._done or not self._started or self._closed:
                self._wait_until(deadline)
                return None
            segments = self._script["segments"]
            if self._segment >= len(segments):
                self._pending.append(self._event("end_of_stream", {"reason": "synthetic_script_complete"}))
                self._done = True
                continue
            segment = segments[self._segment]
            kind = segment["kind"]
            if kind in _AUDIO_KINDS:
                frames = min(self._block, segment["frames"] - self._segment_offset)
                due = self._host_time(self._elapsed + frames)
                if due > deadline:
                    self._wait_until(deadline)
                    return None
                self._wait_until(due)
                block = AdapterBlock(frames, self._pcm(segment, self._segment_offset, frames),
                                     float(self._counter) if self._sample_valid else None,
                                     self._host_time(self._elapsed) if self._host_valid else None,
                                     self._clock.monotonic_ns(), self._dropped, None)
                self._elapsed += frames
                self._counter += frames
                self._segment_offset += frames
                self._blocks += 1
                if self._segment_offset >= segment["frames"]:
                    self._segment, self._segment_offset = self._segment + 1, 0
                return block
            if kind == "stall":
                stalled = segment["ms"] * self._rate // 1000
                due = self._host_time(self._elapsed + stalled)
                if due > deadline:
                    self._wait_until(deadline)
                    return None
                self._wait_until(due)
                self._elapsed += stalled
                if segment["counter"] == "advances":
                    self._counter += stalled
            elif kind == "gap":
                self._elapsed += segment["frames"]
                self._counter += segment["frames"]
                if segment["via"] == "helper_ring_overflow":
                    self._dropped += segment["frames"]
                elif segment["via"] == "processor_overload":
                    self._pending.append(self._event("processor_overload", {"reported_by": "synthetic_script"}))
            elif kind == "counter_reset":
                self._counter = segment.get("sample_time", 0)
            elif kind == "counter_backward":
                self._counter += segment["delta_frames"]
            elif kind == "clocks":
                self._sample_valid = segment["sample_time_valid"]
                self._host_valid = segment["host_time_valid"]
            elif kind in ("device_lost", "hogged"):
                self._pending.append(self._event("device_lost" if kind == "device_lost" else "device_hogged", {}))
                self._done = True
            elif kind == "rate_change":
                self._pending.append(self._event("sample_rate_changed", {"sample_rate": segment["sample_rate"]}))
                self._done = True
            elif kind == "channel_reorder":
                self._pending.append(self._event("channel_map_changed", {"order": list(segment["order"])}))
                self._done = True
            self._segment, self._segment_offset = self._segment + 1, 0

    def close(self, deadline_ns):
        self._closed = True
        return CloseResult("clean", True, {})

    def stats(self):
        return {"profile": self.profile, "blocks_generated": self._blocks, "scripted_dropped_frames": self._dropped,
                "last_heartbeat_monotonic_ns": None, "callback_ns": None,
                "device_period_ns": _ns(self._block, self._rate)}


class ProtocolError(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _reject_constant(name):
    raise ValueError(f"non-finite JSON constant {name}")


def encode_block(index, sample_time, host_ticks, flags, pcm, dropped_frames, dropped_blocks, cb_start, cb_end):
    samples = np.ascontiguousarray(pcm, dtype="<f4")
    frames, channels = samples.shape
    return BLOCK_HEADER.pack(index, sample_time, host_ticks, flags, frames, channels, 0, 0, dropped_frames,
                             dropped_blocks, cb_start, cb_end) + samples.tobytes()


@dataclass(frozen=True, slots=True)
class BlockFrame:
    index: int
    sample_time: float
    host_ticks: int
    flags: int
    frames: int
    channels: int
    dropped_frames: int
    dropped_blocks: int
    callback_start_ticks: int
    callback_end_ticks: int
    pcm: np.ndarray


class _Phase(Enum):
    HELLO = auto()
    AWAIT_START = auto()
    STREAM = auto()
    CLOSED = auto()


class Decoder:
    def __init__(self, max_block_frames=MAX_BLOCK_FRAMES):
        self._buffer = bytearray()
        self._serial = 0
        self._phase = _Phase.HELLO
        self._block_index = 0
        self._channels = None
        self._dropped = (0, 0)
        self._max_frames = max_block_frames

    def feed(self, data):
        return list(self.decode(data))

    def decode(self, data):
        self._buffer.extend(data)
        while len(self._buffer) >= HEADER.size:
            magic, kind, reserved, length, serial = HEADER.unpack_from(self._buffer, 0)
            if magic != MAGIC:
                raise ProtocolError("bad_magic")
            if kind not in _CAPS:
                raise ProtocolError("unknown_type")
            if reserved:
                raise ProtocolError("reserved_nonzero")
            if length > _CAPS[kind] or (kind == HEARTBEAT_TYPE and length != HEARTBEAT.size):
                raise ProtocolError("payload_length")
            if serial != self._serial:
                raise ProtocolError("serial_not_contiguous")
            if len(self._buffer) < HEADER.size + length:
                break
            payload = bytes(self._buffer[HEADER.size:HEADER.size + length])
            del self._buffer[:HEADER.size + length]
            self._serial += 1
            yield self._frame(kind, payload)

    @property
    def exited(self):
        return self._phase is _Phase.CLOSED

    def finish(self):
        if self._buffer:
            raise ProtocolError("truncated_frame")

    def _json(self, kind, payload):
        try:
            value = json.loads(payload.decode("utf-8"), parse_constant=_reject_constant)
        except (ValueError, UnicodeError) as error:
            raise ProtocolError("invalid_json") from error
        if not isinstance(value, dict) or value.get("schema") != HELPER_SCHEMAS[kind]:
            raise ProtocolError("unexpected_schema")
        return value

    def _frame(self, kind, payload):
        phase = self._phase
        if phase is _Phase.CLOSED:
            raise ProtocolError("frame_after_exit")
        if phase is _Phase.HELLO and kind != HELLO:
            raise ProtocolError("hello_not_first")
        if phase is not _Phase.HELLO and kind == HELLO:
            raise ProtocolError("duplicate_hello")
        if phase is _Phase.AWAIT_START and kind not in (STARTED, EXIT):
            raise ProtocolError("frame_before_started")
        if phase is _Phase.STREAM and kind == STARTED:
            raise ProtocolError("duplicate_started")
        if kind == HELLO:
            self._phase = _Phase.AWAIT_START
            return ("hello", self._json(kind, payload))
        if kind == STARTED:
            value = self._json(kind, payload)
            channels = value.get("selected_channels")
            if (not isinstance(channels, list) or not 1 <= len(channels) <= 4
                    or not all(type(c) is int and 0 <= c < 64 for c in channels)
                    or len(set(channels)) != len(channels)):
                raise ProtocolError("started_channels")
            self._channels = len(channels)
            self._phase = _Phase.STREAM
            return ("started", value)
        if kind == EXIT:
            self._phase = _Phase.CLOSED
            return ("exit", self._json(kind, payload))
        if kind == EVENT:
            return ("event", self._json(kind, payload))
        if kind == HEARTBEAT_TYPE:
            return ("heartbeat", HEARTBEAT.unpack(payload))
        return ("block", self._block(payload))

    def _block(self, payload):
        if len(payload) < BLOCK_HEADER.size:
            raise ProtocolError("block_length")
        (index, sample_time, host, flags, frames, channels, reserved0, reserved1, dropped_frames, dropped_blocks,
         cb_start, cb_end) = BLOCK_HEADER.unpack_from(payload, 0)
        if reserved0 or reserved1:
            raise ProtocolError("reserved_nonzero")
        if index != self._block_index:
            raise ProtocolError("block_index_not_contiguous")
        if not 1 <= frames <= self._max_frames:
            raise ProtocolError("block_frames")
        if channels != self._channels:
            raise ProtocolError("block_channels")
        if len(payload) != BLOCK_HEADER.size + frames * channels * 4:
            raise ProtocolError("block_length")
        if dropped_frames < self._dropped[0] or dropped_blocks < self._dropped[1]:
            raise ProtocolError("dropped_total_decreased")
        if cb_end < cb_start:
            raise ProtocolError("callback_ticks")
        self._block_index += 1
        self._dropped = (dropped_frames, dropped_blocks)
        pcm = np.frombuffer(payload, dtype="<f4", offset=BLOCK_HEADER.size).reshape(frames, channels).astype(
            np.float32)
        return BlockFrame(index, sample_time, host, flags, frames, channels, dropped_frames, dropped_blocks,
                          cb_start, cb_end, pcm)


def decode_frames(data, max_block_frames=MAX_BLOCK_FRAMES):
    decoder = Decoder(max_block_frames)
    frames = decoder.feed(data)
    decoder.finish()
    return frames


_HELPER_ARGS = ("--ring-seconds", str(HELPER_RING_SECONDS), "--max-block-frames", str(MAX_BLOCK_FRAMES))
_CHANGED_BEFORE_START = {"device_lost": "device_not_alive", "sample_rate_changed": "sample_rate_mismatch",
                         "device_hogged": "device_hogged"}
_EXIT_COUNTERS = ("callbacks", "blocks_written", "blocks_sent", "dropped_frames", "dropped_blocks",
                  "oversize_blocks", "layout_mismatch_blocks", "undelivered_blocks", "undelivered_frames")
_EXIT_VALUE_BYTES = 2048
_HOG_MODE_FREE, _HOG_MODE_UNREADABLE = -1, None


def _is_int(value, low=None):
    return type(value) is int and (low is None or value >= low)


def _finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _hello_device_ok(device):
    streams, configuration = device.get("input_streams"), device.get("stream_configuration")
    return (isinstance(device.get("uid"), str) and isinstance(device.get("name"), str)
            and type(device.get("alive")) is bool
            and (device.get("hog_mode_pid") is None or _is_int(device.get("hog_mode_pid")))
            and _finite_number(device.get("nominal_sample_rate")) and _is_int(device.get("input_channels"), 0)
            and _is_int(device.get("audio_object_id"), 0) and isinstance(streams, list)
            and all(isinstance(s, dict) and _is_int(s.get("channels"), 0)
                    and (s.get("starting_channel") is None or _is_int(s["starting_channel"], 0)) for s in streams)
            and isinstance(configuration, list) and all(_is_int(n, 0) for n in configuration)
            and sum(configuration) == device["input_channels"])


def validate_hello(hello, device_uid, expected_rate, helper_pid=None):
    if (not isinstance(hello, dict) or hello.get("schema") != HELPER_SCHEMAS[HELLO]
            or hello.get("mode") != "capture"):
        return ("helper_protocol_error",)
    declared = hello.get("refusal")
    if not isinstance(declared, list) or not all(reason in HELLO_REFUSALS for reason in declared):
        return ("helper_protocol_error",)
    if declared:
        return tuple(dict.fromkeys(declared))
    device, pid, timebase = hello.get("device"), hello.get("helper_pid"), hello.get("timebase")
    if (not isinstance(device, dict) or not _hello_device_ok(device) or not _is_int(pid, 1)
            or (helper_pid is not None and pid != helper_pid)
            or not _is_int(hello.get("max_block_frames"), 1) or hello["max_block_frames"] > MAX_BLOCK_FRAMES
            or not isinstance(timebase, dict) or not all(_is_int(timebase.get(k), 1) for k in ("numer", "denom"))
            or hello.get("host_time_scope") != "mach_absolute_time"
            or hello.get("microphone_authorization") not in MICROPHONE_STATES):
        return ("helper_protocol_error",)
    reasons = []
    if device["uid"] != device_uid:
        reasons.append("device_not_found")
    if not device["alive"]:
        reasons.append("device_not_alive")
    if device["hog_mode_pid"] not in (_HOG_MODE_FREE, _HOG_MODE_UNREADABLE, pid):
        reasons.append("device_hogged")
    if hello["microphone_authorization"] != "authorized":
        # Under denied or undetermined permission macOS can deliver zeros that would read as silence.
        reasons.append("microphone_not_authorized")
    if device["nominal_sample_rate"] != expected_rate:
        reasons.append("sample_rate_mismatch")
    return tuple(reasons)


def _optional_int(value):
    return value if _is_int(value, 0) else None


def _exit_why(exit_record):
    why = {}
    if isinstance(exit_record.get("stage"), str):
        why["exit_stage"] = exit_record["stage"][:64]
    if _is_int(exit_record.get("osstatus")):
        why["exit_osstatus"] = exit_record["osstatus"]
    if isinstance(exit_record.get("type"), str):
        why["exit_event_type"] = exit_record["type"][:64]
    if exit_record.get("value") is not None:
        try:
            fits = len(json.dumps(exit_record["value"], sort_keys=True, allow_nan=False)) <= _EXIT_VALUE_BYTES
        except (TypeError, ValueError):
            fits = False
        why["exit_event_value"] = exit_record["value"] if fits else {"value_omitted": True}
    if exit_record.get("microphone_authorization") in MICROPHONE_STATES:
        why["microphone_authorization"] = exit_record["microphone_authorization"]
    return why


def _hello_identity(device):
    return DeviceIdentity(device["uid"], device["name"][:256],
                          device["manufacturer"][:256] if isinstance(device.get("manufacturer"), str) else None,
                          device["model_uid"][:256] if isinstance(device.get("model_uid"), str) else None,
                          device["transport"][:32] if isinstance(device.get("transport"), str) else "other",
                          f"audioobject-{device['audio_object_id']}")


def _hello_format(device):
    return NegotiatedFormat(int(device["nominal_sample_rate"]), "float32", device["input_channels"],
                            _optional_int(device.get("buffer_frame_size")),
                            _optional_int(device.get("device_latency_frames")),
                            _optional_int(device.get("safety_offset_frames")), True, True, "mach_absolute_time_ns",
                            tuple(device["stream_configuration"]))


def _wait(process, seconds):
    try:
        process.wait(timeout=max(0.0, seconds))
        return True
    except subprocess.TimeoutExpired:
        return False


class CoreAudioHelperAdapter:
    profile = COREAUDIO_PROFILE
    hello_timeout_s = 5.0
    started_timeout_s = 3.0
    queue_frames = ADAPTER_QUEUE_FRAMES
    callback_window = 4096

    def __init__(self, build_id, device_uid, expected_rate, clock, *, expected_helper_sha256=None):
        self._build_id = build_id
        self._uid = device_uid
        self._rate = expected_rate
        self._clock = clock
        self._expected_sha = expected_helper_sha256
        self._frames = queue.Queue(maxsize=self.queue_frames)
        self._closing = threading.Event()
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._helper = None
        self._process = None
        self._threads = ()
        self._hello = None
        self._timebase = (1, 1)
        self._started = False
        self._ended = False
        self._exit = None
        self._close_result = None
        self._stderr = bytearray()
        self._discarded = {"items": 0, "blocks": 0, "audio_frames": 0}
        self._stats = {"blocks": 0, "heartbeats": 0, "callbacks": None, "helper_dropped_frames": 0,
                       "helper_dropped_blocks": 0, "oversize_blocks": None, "layout_mismatch_blocks": None,
                       "last_heartbeat_monotonic_ns": None, "unwatched_properties": None}
        self._waited_ns = None
        self._callback_ns = np.zeros(self.callback_window, dtype=np.int64)
        self._callback_count = 0
        self._callback_max = 0

    @property
    def helper_pid(self):
        return None if self._process is None else self._process.pid

    def open(self):
        if self._process is not None or self._closing.is_set():
            raise RuntimeError("open runs once per adapter")
        try:
            build = verify_build(self._build_id)
            if self._expected_sha is not None and build["helper_sha256"] != self._expected_sha:
                raise PocketError("The helper build differs from the one this session sealed")
            self._helper = copy_helper(build)
        except (PocketError, OSError) as error:
            if getattr(error, "code", None) == "io_error":
                raise
            return OpenResult(None, None, ("helper_build_changed",), (), {"error": str(error)[:500]})
        argv = [str(self._helper), "capture", "--device-uid", self._uid, "--expected-sample-rate",
                str(self._rate), *_HELPER_ARGS]
        try:
            self._process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, bufsize=0, close_fds=True,
                                             cwd=self._helper.parent)
        except OSError as error:
            return OpenResult(None, None, ("helper_exited",), (), {"error": str(error)[:500]})
        for thread in (threading.Thread(target=self._read_stdout, args=(self._process.stdout,), daemon=True),
                       threading.Thread(target=self._read_stderr, args=(self._process.stderr,), daemon=True)):
            thread.start()
            self._threads = (*self._threads, thread)
        frame = self._next(self.hello_timeout_s)
        if frame is None or frame[0] != "hello":
            reason = ("helper_start_timeout" if frame is None else
                      "helper_exited" if frame[0] == "eof" else "helper_protocol_error")
            self._stop_child()
            return OpenResult(None, None, (reason,), (), self._detail())
        hello = frame[1]
        refusal = validate_hello(hello, self._uid, self._rate, self._process.pid)
        authorization = hello.get("microphone_authorization")
        macos = hello.get("macos_version")
        detail = {"helper_pid": self._process.pid,
                  "microphone_authorization": authorization if authorization in MICROPHONE_STATES else None,
                  "macos_version": macos[:32] if isinstance(macos, str) else None}
        if refusal:
            self._stop_child()
            return OpenResult(None, None, refusal, (), {**detail, **self._detail()})
        self._hello = hello
        self._timebase = (hello["timebase"]["numer"], hello["timebase"]["denom"])
        device = hello["device"]
        return OpenResult(_hello_identity(device), _hello_format(device), (), (), detail)

    def start(self, selected_channels):
        selected = tuple(selected_channels)
        if self._hello is None or self._started or self._closing.is_set():
            raise RuntimeError("start follows one successful open")
        device = self._hello["device"]
        if (not 1 <= len(selected) <= 4 or len(set(selected)) != len(selected)
                or not all(type(c) is int and 0 <= c < device["input_channels"] for c in selected)):
            self._stop_child()
            return StartResult(False, ("no_metered_channels_exposed",), selected, None, self._detail())
        readable = all(has_four_byte_samples(s.get("virtual_format")) for s in device["input_streams"])
        for channel in selected:
            owners = [s for s in device["input_streams"] if s.get("starting_channel") is not None
                      and s["starting_channel"] <= channel < s["starting_channel"] + s["channels"]]
            if (not readable or len(owners) != 1
                    or not is_float32(owners[0].get("virtual_format"), self._rate)):
                self._stop_child()
                return StartResult(False, ("format_not_float32",), selected, None, self._detail())
        try:
            self._process.stdin.write(("start " + ",".join(map(str, selected)) + "\n").encode())
            self._process.stdin.flush()
        except (OSError, ValueError):
            self._stop_child()
            return StartResult(False, ("helper_exited",), selected, None, self._detail())
        frame = self._next(self.started_timeout_s)
        if frame is not None and frame[0] == "started":
            started = frame[1]
            if (started.get("selected_channels") != list(selected) or not _is_int(started.get("ring_frames"), 1)
                    or not _is_int(started.get("ring_bytes"), 1)):
                self._stop_child()
                return StartResult(False, ("helper_protocol_error",), selected, None, self._detail())
            self._started = True
            unwatched = started.get("unwatched_properties")
            with self._lock:
                self._stats["last_heartbeat_monotonic_ns"] = frame[2]
                names = unwatched[:16] if isinstance(unwatched, list) else None
                self._stats["unwatched_properties"] = (None if names is None else
                                                       [n[:64] for n in names if isinstance(n, str)])
            return StartResult(True, (), selected, started["ring_frames"],
                               {"ring_bytes": started["ring_bytes"], "record_capacity": started.get("record_capacity"),
                                "max_block_frames": started.get("max_block_frames"),
                                "unwatched_properties": self._stats["unwatched_properties"]})
        if frame is None:
            reasons = ("helper_start_timeout",)
        elif frame[0] == "exit" and frame[1].get("reason") == "device_changed_before_start":
            reasons = (_CHANGED_BEFORE_START.get(frame[1].get("type"), "device_changed_before_start"),)
        elif frame[0] == "exit":
            refusal = frame[1].get("refusal")
            reasons = (("helper_exited",) if not refusal else tuple(dict.fromkeys(refusal))
                       if isinstance(refusal, list) and all(r in START_REFUSALS for r in refusal)
                       else ("helper_protocol_error",))
        else:
            reasons = ("helper_exited",) if frame[0] == "eof" else ("helper_protocol_error",)
        self._stop_child()
        return StartResult(False, reasons, selected, None, self._detail())

    def read(self, timeout_ns):
        timeout_s = max(0, int(timeout_ns)) / 1e9
        if self._ended or not self._started or self._closing.is_set():
            time.sleep(timeout_s)
            return None
        frame = self._next(timeout_s)
        if frame is None:
            return None
        kind, value, received = frame
        if kind == "block":
            return self._block(value, received)
        if self._closing.is_set() or kind == "exit":
            return None
        if kind == "event":
            return self._event(value, received)
        self._ended = True
        if kind == "eof":
            return AdapterEvent("helper_exited", {"exit_code": self._process.poll(), **self._exit_detail(512)},
                                received)
        self._signal_child()
        return AdapterEvent("helper_protocol_error", {"reason": value}, received)

    def _block(self, frame, received):
        numer, denom = self._timebase
        callback = (frame.callback_end_ticks - frame.callback_start_ticks) * numer // denom
        with self._lock:
            self._stats["blocks"] += 1
            self._stats["helper_dropped_frames"] = frame.dropped_frames
            self._stats["helper_dropped_blocks"] = frame.dropped_blocks
            self._callback_ns[self._callback_count % self.callback_window] = callback
            self._callback_count += 1
            self._callback_max = max(self._callback_max, callback)
        return AdapterBlock(frame.frames, frame.pcm, frame.sample_time if frame.flags & 1 else None,
                            frame.host_ticks * numer // denom if frame.flags & 2 else None, received,
                            frame.dropped_frames, callback)

    def _event(self, event, received):
        kind = event.get("type")
        if kind not in HELPER_EVENTS:
            self._ended = True
            self._signal_child()
            return AdapterEvent("helper_protocol_error", {"reason": "unknown_event"}, received)
        if kind not in _ONGOING_HELPER_EVENTS:
            self._ended = True
        detail = {"value": event.get("value")}
        if kind == "processor_overload":
            detail["reported_by"] = "hal"
            ticks = event.get("host_now_ticks")
            if _is_int(ticks, 0):
                numer, denom = self._timebase
                detail["host_time_ns"] = ticks * numer // denom
        return AdapterEvent(kind, detail, received)

    def _put(self, item):
        if not self._closing.is_set():
            try:
                self._frames.put_nowait(item)
                return
            except queue.Full:
                with self._lock:
                    self._waited_ns = self._clock.monotonic_ns()
        while not self._closing.is_set():
            try:
                self._frames.put(item, timeout=0.05)
                return
            except queue.Full:
                pass
        with self._lock:
            self._discarded["items"] += 1
            if item[0] == "block":
                self._discarded["blocks"] += 1
                self._discarded["audio_frames"] += item[1].frames

    def _read_stdout(self, stream):
        decoder = Decoder()

        def pump():
            try:
                while True:
                    chunk = stream.read(65536)
                    now = self._clock.monotonic_ns()
                    if not chunk:
                        decoder.finish()
                        self._put(("eof", None, now))
                        return
                    for kind, value in decoder.decode(chunk):
                        if kind == "heartbeat":
                            self._heartbeat(value, now)
                            continue
                        if kind == "exit":
                            with self._lock:
                                self._exit = value
                        self._put((kind, value, now))
            except ProtocolError as error:
                self._protocol_error(stream, error.reason)
            except (OSError, ValueError):
                self._put(("eof", None, self._clock.monotonic_ns()))

        contained(pump, on_error=lambda error: self._protocol_error(stream, "decoder_fault"))

    def _protocol_error(self, stream, reason):
        self._put(("protocol_error", reason, self._clock.monotonic_ns()))
        try:
            while stream.read(65536):
                pass
        except (OSError, ValueError):
            return

    def _heartbeat(self, values, now):
        _host_ticks, blocks_written, dropped_frames, callback_max_ticks, callbacks = values
        numer, denom = self._timebase
        with self._lock:
            self._stats.update(last_heartbeat_monotonic_ns=now, callbacks=callbacks,
                               helper_blocks_written=blocks_written, heartbeat_dropped_frames=dropped_frames,
                               callback_max_ns_since_heartbeat=callback_max_ticks * numer // denom)
            self._stats["heartbeats"] += 1

    def _read_stderr(self, stream):
        try:
            for chunk in iter(lambda: stream.read(4096), b""):
                with self._lock:
                    self._stderr.extend(chunk)
                    del self._stderr[:-65536]
        except (OSError, ValueError):
            return

    def stderr_tail(self, limit=2048):
        with self._lock:
            return bytes(self._stderr[-limit:]).decode("utf-8", "replace") or None

    def _exit_detail(self, stderr_limit=2048):
        with self._lock:
            exit_record = dict(self._exit) if isinstance(self._exit, dict) else None
        counters = {} if exit_record is None else {k: exit_record[k] for k in _EXIT_COUNTERS
                                                   if _is_int(exit_record.get(k), 0)}
        reason = exit_record.get("reason") if exit_record and isinstance(exit_record.get("reason"), str) else None
        return {"exit_reason": reason, "exit_counters": counters, **_exit_why(exit_record or {}),
                "stderr_tail": self.stderr_tail(stderr_limit)}

    def _detail(self):
        code = None if self._process is None else self._process.poll()
        return {"exit_code": code, **self._exit_detail(512)}

    def _next(self, timeout_s):
        try:
            return self._frames.get(timeout=max(0.0, timeout_s))
        except queue.Empty:
            return None

    def _signal_child(self):
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

    def _stop_child(self):
        self.close(self._clock.monotonic_ns() + 3_000_000_000)

    def close(self, deadline_ns):
        with self._close_lock:
            if self._close_result is None:
                self._closing.set()
                try:
                    self._close_result = self._close_child(deadline_ns)
                finally:
                    if self._helper is not None:
                        remove_helper_copy(self._helper)
                        self._helper = None
            return self._close_result

    def _close_child(self, deadline_ns):
        process = self._process
        if process is None:
            return CloseResult("not_started", True, {})
        budget = max(0.0, (int(deadline_ns) - self._clock.monotonic_ns()) / 1e9)
        begun = time.monotonic()
        if process.poll() is None:
            try:
                process.stdin.write(b"quit\n")
                process.stdin.flush()
            except (OSError, ValueError):
                pass
        try:
            process.stdin.close()
        except (OSError, ValueError):
            pass
        cause = "exited"
        if not _wait(process, budget * 2 / 3):
            process.terminate()
            cause = "terminated"
            if not _wait(process, budget - (time.monotonic() - begun)):
                process.kill()
                cause = "killed" if _wait(process, 1.0) else "timeout"
        for thread in self._threads:
            thread.join(timeout=1.0)
        with self._lock:
            if cause == "exited":
                reason = (self._exit or {}).get("reason")
                orderly = ORDERLY_EXIT_CODES.get(reason) if isinstance(reason, str) else None
                cause = "clean" if process.returncode in (0, orderly) else "exited_with_error"
            self._stats["oversize_blocks"] = (self._exit or {}).get("oversize_blocks")
            self._stats["layout_mismatch_blocks"] = (self._exit or {}).get("layout_mismatch_blocks")
            discarded = dict(self._discarded)
        with self._frames.mutex:
            unread = list(self._frames.queue)
        blocks = [item[1] for item in unread if item[0] == "block"]
        return CloseResult(cause, cause == "clean",
                           {"exit_code": process.returncode,
                            "elapsed_ms": int((time.monotonic() - begun) * 1000),
                            "budget_ms": int(budget * 1000),
                            "items_discarded_while_closing": discarded["items"],
                            "blocks_discarded_while_closing": discarded["blocks"],
                            "audio_frames_discarded_while_closing": discarded["audio_frames"],
                            "items_left_unread": len(unread),
                            "blocks_left_unread": len(blocks),
                            "audio_frames_left_unread": sum(b.frames for b in blocks),
                            **self._exit_detail()})

    def stats(self):
        with self._lock:
            count = min(self._callback_count, self.callback_window)
            window = np.sort(self._callback_ns[:count]) if count else None
            callback = None if window is None else {
                "samples": self._callback_count, "max": int(self._callback_max), "p50": int(window[count // 2]),
                "p99": int(window[min(count - 1, (count * 99) // 100)]), "quantile_window": count}
            device = self._hello["device"] if self._hello else {}
            frames = device.get("buffer_frame_size")
            beat, waited = self._stats["last_heartbeat_monotonic_ns"], self._waited_ns
            backpressured = waited is not None and (beat is None or waited >= beat)
            with self._frames.mutex:
                queued = list(self._frames.queue)
            return {**self._stats, "reader_backpressured": backpressured,
                    "profile": self.profile, "helper_pid": self.helper_pid, "callback_ns": callback,
                    "device_period_ns": _ns(frames, self._rate) if _is_int(frames, 1) else None,
                    "queued_items": len(queued),
                    "queued_audio_frames": sum(item[1].frames for item in queued if item[0] == "block")}
