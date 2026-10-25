# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import bisect
import json
import math
import os
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from functools import partial
from typing import NamedTuple, Protocol

import numpy as np

from .artifact_store import canonical_bytes, digest
from .errors import PocketError, contained
from .listen import (
    DESCRIPTOR_CAPACITY,
    EVENT_MAX_BYTES,
    FACTS_CAPACITY,
    SEMANTIC_STATUS,
    WORKER_KEYS,
    WORKER_TERMINAL_KEYS,
    coverage,
    cue_notice,
    notice,
    order_notices,
    resolve_routes,
    selected_channels,
    validate_poll_args,
)
from .listen_adapters import ENDING_EVENTS, AdapterBlock, AdapterEvent

EVENT_SCHEMA = "pocket.listen-event/v1"
OBSERVATION_SCHEMA = "pocket.listen-observation/v1"
POLL_SCAN_LIMIT = 4096
MS_NS = 1_000_000
LEVEL_PROFILE = "sample-peak-rms/v1"
SILENCE_PROFILE = "near-silence/v1"
RECEIVED_CLOCK_ID = "listen-host-monotonic"
STREAM_RECORDS = 64
STREAM_END_RECORDS = 4096
HEARTBEAT_LIMIT_NS = 2_000_000_000
EXPIRING_LEAD_NS = 300_000_000_000
_FAILURES = ("helper_exited", "helper_protocol_error")
_FINAL_STATES = ("ended", "failed", "stopped")
_INTEGRAL_TOLERANCE = 1e-6
_PLAYBACK_CAVEAT = ("playback_stopped",)
_SILENCE_CAVEATS = ("stopped_transport", "dropout", "intended_ending")
_PUBLISHED_PLACEHOLDER = {"sequence": 2 ** 53, "published_monotonic_ns": 2 ** 63 - 1}
_CURSOR_AHEAD_HINT = "Use the cursor listen_start or listen_observe returned; Listen never clamps a cursor."
_ADAPTER_STAT_KEYS = ("helper_dropped_frames", "helper_dropped_blocks", "oversize_blocks", "layout_mismatch_blocks",
                      "scripted_dropped_frames", "blocks_generated", "device_period_ns", "callback_ns",
                      "blocks", "helper_blocks_written", "queued_items", "queued_audio_frames",
                      "unwatched_properties", "reader_backpressured")
_ACCOUNTED_FACTS = ("frames_missing", "stream_started", "stream_ended")


class Clock(Protocol):
    def monotonic_ns(self) -> int: ...
    def sleep_ns(self, ns: int) -> None: ...


class SystemClock:
    def monotonic_ns(self):
        return time.monotonic_ns()

    def sleep_ns(self, ns):
        if ns > 0:
            time.sleep(ns / 1e9)


class FakeClock:
    def __init__(self, start_ns=1_000_000_000_000):
        self._lock = threading.Lock()
        self._now = int(start_ns)

    def monotonic_ns(self):
        with self._lock:
            return self._now

    def sleep_ns(self, ns):
        self.advance(max(0, int(ns)))

    def advance(self, ns):
        if ns < 0:
            raise ValueError("a monotonic clock never goes backward")
        with self._lock:
            self._now += int(ns)
            return self._now


def stream_epoch(runtime_epoch, ordinal):
    return f"{runtime_epoch}.s{ordinal}"


def stream_clock_id(epoch):
    return f"stream:{epoch}"


def device_instance_epoch(uid, instance, runtime_epoch):
    return "dev-" + digest([uid, instance, runtime_epoch])[:16]


def topology_epoch(device_epoch, negotiated_format, selected_channels, config_sha256):
    fmt = asdict(negotiated_format) if hasattr(negotiated_format, "__dataclass_fields__") else negotiated_format
    return "topo-" + digest([device_epoch, fmt, list(selected_channels), config_sha256])[:16]


def same_host_clock(host_time_scope):
    if host_time_scope == "synthetic_clock_ns":
        return True
    return (host_time_scope == "mach_absolute_time_ns"
            and time.get_clock_info("monotonic").implementation == "mach_absolute_time()")


def _clock_state(value):
    return value if isinstance(value, str) else "available" if value else "unavailable"


def clock_report(epoch, *, sample_time_available, host_time_available, host_time_scope, same_clock):
    return {"stream_clock_id": stream_clock_id(epoch) if epoch else None,
            "device_sample_time": _clock_state(sample_time_available),
            "device_host_time": _clock_state(host_time_available),
            "host_time_scope": host_time_scope, "received_clock": "python_monotonic_ns",
            "received_clock_id": RECEIVED_CLOCK_ID, "same_host_clock": bool(same_clock),
            "domains_compared": False, "drift": "not_estimated", "device_latency": "not_measured"}


@dataclass(frozen=True, slots=True)
class Fact:
    type: str
    received_monotonic_ns: int
    cause: str | None = None
    detail: dict = field(default_factory=dict)
    stream_ordinal: int | None = None
    interval: tuple[int, int] | None = None
    state_transition: tuple[str, str] | None = None
    affected_routes: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class Placement:
    stream_ordinal: int
    start_frame: int
    frames: int
    sample_time: int
    host_ns: int | None


@dataclass(frozen=True, slots=True)
class Descriptor:
    ingest_index: int
    stream_ordinal: int
    start_frame: int
    frames: int
    ring_pos: int
    received_ns: int
    sample_time: int
    host_ns: int | None
    adapter_dropped_total: int


class PcmRing:
    def __init__(self, capacity_frames, channels):
        if type(capacity_frames) is not int or capacity_frames < 1 or type(channels) is not int \
                or not 1 <= channels <= 64:
            raise ValueError("a PCM ring needs a positive frame capacity and 1 to 64 channels")
        self._data = np.zeros((capacity_frames, channels), dtype=np.float32)
        self._lock = threading.Lock()
        self._written = 0

    @property
    def capacity_frames(self):
        return self._data.shape[0]

    @property
    def channels(self):
        return self._data.shape[1]

    @property
    def nbytes(self):
        return self._data.nbytes

    @property
    def write_counter(self):
        with self._lock:
            return self._written

    def retained(self):
        with self._lock:
            start = max(0, self._written - self.capacity_frames)
            return start, self._written - start

    def write(self, pcm):
        capacity = self.capacity_frames
        frames = pcm.shape[0]
        tail = pcm[frames - capacity:] if frames > capacity else pcm
        with self._lock:
            position = self._written
            first = (position + frames - len(tail)) % capacity
            head = min(len(tail), capacity - first)
            self._data[first:first + head] = tail[:head]
            self._data[:len(tail) - head] = tail[head:]
            self._written = position + frames
        return position

    def read(self, position, frames, out=None):
        capacity = self.capacity_frames
        with self._lock:
            if (position < 0 or frames < 1 or frames > capacity or position < self._written - capacity
                    or position + frames > self._written):
                return None
            target = np.empty((frames, self.channels), dtype=np.float32) if out is None else out
            first = position % capacity
            head = min(frames, capacity - first)
            target[:head] = self._data[first:first + head]
            target[head:frames] = self._data[:frames - head]
        return target


_DESCRIPTOR_DTYPE = np.dtype([("ingest_index", "<u8"), ("stream_ordinal", "<u4"), ("start_frame", "<u8"),
                              ("frames", "<u4"), ("ring_pos", "<u8"), ("received_ns", "<i8"),
                              ("sample_time", "<i8"), ("host_ns", "<i8"), ("adapter_dropped_total", "<u8")])
_SAMPLE_TIMES = np.iinfo(_DESCRIPTOR_DTYPE["sample_time"])


class DescriptorRing:
    def __init__(self, capacity=DESCRIPTOR_CAPACITY):
        self._entries = np.zeros(capacity, dtype=_DESCRIPTOR_DTYPE)
        self._lock = threading.Lock()
        self._pushed = 0
        self._taken = 0
        self._overwritten = 0

    @property
    def capacity(self):
        return len(self._entries)

    @property
    def nbytes(self):
        return self._entries.nbytes

    @property
    def overwritten(self):
        with self._lock:
            return self._overwritten

    def __len__(self):
        with self._lock:
            return self._pushed - self._taken

    def push(self, d):
        with self._lock:
            if self._pushed - self._taken == self.capacity:
                self._taken += 1
                self._overwritten += 1
            self._entries[self._pushed % self.capacity] = (
                d.ingest_index, d.stream_ordinal, d.start_frame, d.frames, d.ring_pos, d.received_ns,
                d.sample_time, -1 if d.host_ns is None else d.host_ns,
                d.adapter_dropped_total)
            self._pushed += 1

    def peek_index(self):
        with self._lock:
            if self._taken == self._pushed:
                return None
            return int(self._entries[self._taken % self.capacity]["ingest_index"])

    def position_half_behind(self):
        with self._lock:
            index = self._pushed - self.capacity // 2
            if index < self._taken:
                return None
            return int(self._entries[index % self.capacity]["ring_pos"])

    def pop_if(self, ingest_index):
        with self._lock:
            if self._taken == self._pushed:
                return None
            row = self._entries[self._taken % self.capacity]
            if int(row["ingest_index"]) != ingest_index:
                return None
            self._taken += 1
            return Descriptor(int(row["ingest_index"]), int(row["stream_ordinal"]), int(row["start_frame"]),
                              int(row["frames"]), int(row["ring_pos"]), int(row["received_ns"]),
                              int(row["sample_time"]),
                              None if int(row["host_ns"]) < 0 else int(row["host_ns"]),
                              int(row["adapter_dropped_total"]))


@dataclass(frozen=True, slots=True)
class DroppedFacts:
    first_index: int
    last_index: int


class FactsQueue:
    def __init__(self, capacity=FACTS_CAPACITY):
        self._capacity = capacity
        self._items = deque()
        self._lock = threading.Lock()
        self._runs = deque()
        self._run_open = False
        self._dropped_total = 0

    def __len__(self):
        with self._lock:
            return len(self._items)

    @property
    def capacity(self):
        return self._capacity

    @property
    def dropped_total(self):
        with self._lock:
            return self._dropped_total

    def push(self, ingest_index, fact):
        with self._lock:
            if len(self._items) >= self._capacity:
                self._dropped_total += 1
                if self._run_open:
                    self._runs[-1][1] = ingest_index
                else:
                    self._runs.append([ingest_index, ingest_index])
                    self._run_open = True
                return False
            self._items.append((ingest_index, fact))
            self._run_open = False
            return True

    def peek_index(self):
        with self._lock:
            fact = self._items[0][0] if self._items else None
            run = self._runs[0][1] if self._runs else None
            return run if fact is None or (run is not None and run < fact) else fact

    def pop(self):
        with self._lock:
            run = self._runs[0] if self._runs else None
            if run is not None and (not self._items or run[1] < self._items[0][0]):
                self._runs.popleft()
                if not self._runs:
                    self._run_open = False
                return DroppedFacts(*run)
            return self._items.popleft()[1] if self._items else None

    def dropped_before(self, ingest_index):
        with self._lock:
            return bool(self._runs) and self._runs[0][0] < ingest_index

    def pop_run_before(self, ingest_index):
        with self._lock:
            if not self._runs or self._runs[0][0] >= ingest_index:
                return None
            run = self._runs.popleft()
            if not self._runs:
                self._run_open = False
            return DroppedFacts(*run)


def _unusable_sample_time(value):
    if value is None:
        return "missing"
    if not math.isfinite(value):
        return "nonfinite"
    if abs(value - round(value)) > _INTEGRAL_TOLERANCE:
        return "fractional"
    if not _SAMPLE_TIMES.min <= round(value) <= _SAMPLE_TIMES.max:
        return "out_of_range"
    return None


def _block_shape_ok(block, channels):
    pcm = block.pcm
    return (isinstance(pcm, np.ndarray) and pcm.dtype == np.float32 and pcm.ndim == 2
            and pcm.shape == (block.frames, channels) and block.frames >= 1)


class StreamTracker:
    def __init__(self, runtime_epoch, sample_rate, *, topology_epoch=None):
        self.runtime_epoch = runtime_epoch
        self.sample_rate = sample_rate
        self.topology_epoch = topology_epoch
        self._lock = threading.Lock()
        self._state = "starting"
        self._ended_cause = None
        self._ordinal = 0
        self._end = 0
        self._expected = None
        self._dropped = 0
        self._overload = False
        self._overload_host_ns = None
        self._last = None
        self._frames_received = 0
        self._missing = {"events": 0, "exact_frames": 0}
        self._last_exact = None
        self._record = None
        self._streams = deque(maxlen=STREAM_RECORDS)
        self._streams_omitted = 0
        self._counts = {"blocks": 0, "blocks_after_end": 0, "events_after_end": 0, "processor_overloads": 0,
                        "devices_changed": 0}

    @property
    def capture_state(self):
        with self._lock:
            return self._state

    @property
    def capture_active(self):
        with self._lock:
            return self._state not in _FINAL_STATES

    @property
    def stream_ordinal(self):
        with self._lock:
            return self._ordinal or None

    @property
    def frames_received(self):
        with self._lock:
            return self._frames_received

    def _close_record(self, end_cause):
        if self._record is None:
            return
        if len(self._streams) == self._streams.maxlen:
            self._streams_omitted += 1
        self._streams.append({**self._record, "end_cause": end_cause})
        self._record = None

    def _open_stream(self, cause, sample_time, received_ns, dropped):
        previous = None
        first = self._ordinal == 0
        if not first:
            previous = {"stream_epoch": stream_epoch(self.runtime_epoch, self._ordinal), "end_frame": self._end}
            self._close_record(cause)
        self._ordinal += 1
        self._end, self._expected = 0, sample_time
        self._record = {"stream_epoch": stream_epoch(self.runtime_epoch, self._ordinal), "start_cause": cause,
                        "frames_received": 0, "missing_exact_frames": 0}
        return Fact("stream_started", received_ns, cause,
                    {"origin_device_sample_time": float(sample_time), "topology_epoch": self.topology_epoch,
                     "previous_stream": previous, "adapter_dropped_frames": dropped},
                    stream_ordinal=self._ordinal, state_transition=("starting", "running") if first else None)

    def _missing_fact(self, received_ns, cause, frames, dropped):
        at = self._end
        self._missing["events"] += 1
        self._missing["exact_frames"] += frames
        self._record["missing_exact_frames"] += frames
        self._last_exact = {"start_frame": at, "frames": frames,
                            "stream_epoch": stream_epoch(self.runtime_epoch, self._ordinal)}
        return Fact("frames_missing", received_ns, cause,
                    {"missing": {"start_frame": at, "frames": frames}, "adapter_dropped_frames": dropped},
                    stream_ordinal=self._ordinal, interval=(at, frames))

    def _end_capture(self, cause, received_ns, detail):
        facts = []
        if self._ordinal:
            facts.append(Fact("stream_ended", received_ns, cause,
                              {**detail, "end_frame": self._end,
                               "frames_received": self._record["frames_received"]},
                              stream_ordinal=self._ordinal))
        self._close_record(cause)
        final = "failed" if cause in _FAILURES else "ended"
        facts.append(Fact("capture_failed" if final == "failed" else "capture_ended", received_ns, cause,
                          detail, state_transition=(self._state, final)))
        self._state, self._ended_cause = final, cause
        return tuple(facts)

    def observe_block(self, block):
        with self._lock:
            if self._state in _FINAL_STATES:
                self._counts["blocks_after_end"] += 1
                return None, ()
            received = block.received_monotonic_ns
            dropped = max(0, block.adapter_dropped_frames_total - self._dropped)
            self._dropped = max(self._dropped, block.adapter_dropped_frames_total)
            unusable = _unusable_sample_time(block.device_sample_time)
            if unusable is not None:
                return None, self._end_capture("device_sample_time_unavailable", received,
                                               {"sample_time": unusable, "block_frames": block.frames,
                                                "adapter_dropped_frames": dropped})
            sample_time = round(block.device_sample_time)
            facts = []
            if self._ordinal == 0:
                facts.append(self._open_stream("capture_opened", sample_time, received, dropped))
            else:
                gap = sample_time - self._expected
                if dropped > gap:
                    cause = "counter_reset" if 0 <= sample_time < self.sample_rate else "counter_backward"
                    facts.append(self._open_stream(cause, sample_time, received, dropped))
                elif gap > 0:
                    cause = ("helper_ring_overflow" if dropped == gap else
                             "processor_overload" if self._overload else "device_gap")
                    facts.append(self._missing_fact(received, cause, gap, dropped))
                    self._end += gap
            placement = Placement(self._ordinal, self._end, block.frames, sample_time, block.device_host_time_ns)
            self._end += block.frames
            self._expected = sample_time + block.frames
            self._frames_received += block.frames
            self._record["frames_received"] += block.frames
            self._counts["blocks"] += 1
            host = block.device_host_time_ns
            if self._overload_host_ns is None or host is None or host >= self._overload_host_ns:
                self._overload, self._overload_host_ns = False, None
            self._state = "running"
            self._last = {"stream_frame_end": self._end,
                          "device_sample_time": float(block.device_sample_time),
                          "device_host_time_ns": block.device_host_time_ns, "received_monotonic_ns": received}
            return placement, tuple(facts)

    def observe_event(self, event):
        with self._lock:
            if self._state in _FINAL_STATES:
                self._counts["events_after_end"] += 1
                return ()
            received = event.received_monotonic_ns
            if event.type == "processor_overload":
                self._overload = True
                reported = event.detail.get("host_time_ns") if isinstance(event.detail, dict) else None
                self._overload_host_ns = reported if type(reported) is int else None
                self._counts["processor_overloads"] += 1
                reported_by = event.detail.get("reported_by", "hal") if isinstance(event.detail, dict) else "hal"
                return (Fact("processor_overload_reported", received, "processor_overload",
                             {"reported_by": reported_by}),)
            if event.type == "devices_changed":
                self._counts["devices_changed"] += 1
                return ()
            cause = event.type if event.type in ENDING_EVENTS else "helper_protocol_error"
            return self._end_capture(cause, received,
                                     {"adapter_event": event.type, "adapter_detail": _finite(event.detail)})

    def close(self, cause="stop"):
        with self._lock:
            self._close_record(cause)
            if self._state not in _FINAL_STATES:
                self._state = "stopped"
                self._ended_cause = cause

    def snapshot(self):
        with self._lock:
            streams = list(self._streams) + ([{**self._record, "end_cause": None}] if self._record else [])
            omitted = self._streams_omitted + max(0, len(streams) - STREAM_RECORDS)
            return {"state": self._state, "ended_cause": self._ended_cause,
                    "stream_ordinal": self._ordinal or None,
                    "stream_epoch": stream_epoch(self.runtime_epoch, self._ordinal) if self._ordinal else None,
                    "frames_received": self._frames_received,
                    "stream_frames_received": self._record["frames_received"] if self._record else 0,
                    "last_block": dict(self._last) if self._last else None, "missing": dict(self._missing),
                    "last_exact_missing": dict(self._last_exact) if self._last_exact else None,
                    "adapter_dropped_frames_total": self._dropped,
                    "streams": streams[-STREAM_RECORDS:], "streams_omitted": omitted, **self._counts}


class Watchdog:
    def __init__(self, stale_after_ms, started_ns, *, max_runtime_seconds=None,
                 heartbeat_limit_ns=HEARTBEAT_LIMIT_NS):
        self._stale_after_ms = stale_after_ms
        self._stale_after_ns = stale_after_ms * 1_000_000
        self._started_ns = started_ns
        self._max_ns = None if max_runtime_seconds is None else max_runtime_seconds * 1_000_000_000
        self._heartbeat_limit_ns = heartbeat_limit_ns
        self._lock = threading.Lock()
        self._io_started = started_ns
        self._last_block = None
        self._stale_since = None
        self._episodes = 0
        self._helper_late = False
        self._expiring_sent = False
        self._stop_sent = False

    @property
    def scheduled_stop_ns(self):
        return None if self._max_ns is None else self._started_ns + self._max_ns

    def arm(self, io_started_ns):
        with self._lock:
            self._io_started = io_started_ns

    def block_received(self, received_ns):
        with self._lock:
            previous = self._last_block
            resumed = None
            if self._stale_since is not None:
                stale_for = received_ns - self._stale_since
                resumed = Fact("capture_resumed", received_ns, None,
                               {"last_block_received_monotonic_ns": received_ns,
                                "previous_block_received_monotonic_ns": previous,
                                "stale_for_ms": max(0, stale_for) // 1_000_000,
                                "received_before_stale_decision": stale_for < 0,
                                "stale_after_ms": self._stale_after_ms,
                                "does_not_establish": list(_PLAYBACK_CAVEAT)},
                               state_transition=("stale", "running"))
                self._stale_since = None
            self._last_block = received_ns
            return resumed

    def tick(self, now_ns, *, capture_active=True, heartbeat_ns=None, reader_backpressured=None):
        facts = []
        stop = None
        with self._lock:
            reference = self._io_started if self._last_block is None else self._last_block
            age = now_ns - reference
            if capture_active and self._stale_since is None and age >= self._stale_after_ns:
                self._stale_since = now_ns
                self._episodes += 1
                facts.append(Fact("capture_stale", now_ns, None,
                                  {"last_block_received_monotonic_ns": self._last_block, "age_ms": age // 1_000_000,
                                   "stale_after_ms": self._stale_after_ms,
                                   "does_not_establish": list(_PLAYBACK_CAVEAT)},
                                  state_transition=("starting" if self._last_block is None else "running", "stale")))
            if capture_active and heartbeat_ns is not None:
                late = now_ns - heartbeat_ns > self._heartbeat_limit_ns
                if late != self._helper_late:
                    facts.append(Fact("helper_unresponsive" if late else "helper_responsive", now_ns, None,
                                      {"last_heartbeat_monotonic_ns": heartbeat_ns,
                                       "age_ms": (now_ns - heartbeat_ns) // 1_000_000,
                                       "limit_ms": self._heartbeat_limit_ns // 1_000_000,
                                       "reader_backpressured": reader_backpressured},
                                      state_transition=("responsive", "unresponsive") if late
                                      else ("unresponsive", "responsive")))
                self._helper_late = late
            if self._max_ns is not None:
                deadline = self._started_ns + self._max_ns
                lead = min(EXPIRING_LEAD_NS, self._max_ns // 10)
                if not self._expiring_sent and deadline - lead <= now_ns < deadline:
                    self._expiring_sent = True
                    facts.append(Fact("runtime_expiring", now_ns, "max_runtime_seconds",
                                      {"seconds_remaining": (deadline - now_ns) // 1_000_000_000,
                                       "scheduled_stop_monotonic_ns": deadline}))
                if now_ns >= deadline and not self._stop_sent:
                    self._stop_sent = True
                    stop = "max_runtime_reached"
        return tuple(facts), stop

    def snapshot(self, now_ns):
        with self._lock:
            reference = self._io_started if self._last_block is None else self._last_block
            return {"stale": self._stale_since is not None, "last_block_received_monotonic_ns": self._last_block,
                    "age_ms": max(0, now_ns - reference) // 1_000_000, "stale_after_ms": self._stale_after_ms,
                    "stale_episodes": self._episodes, "helper_unresponsive": self._helper_late,
                    "scheduled_stop_monotonic_ns": self.scheduled_stop_ns}


class CaptureFeed:
    def __init__(self, tracker, ring, watchdog, *, publish_now, descriptors=None, facts=None):
        self.tracker = tracker
        self.ring = ring
        self.watchdog = watchdog
        self.descriptors = descriptors if descriptors is not None else DescriptorRing()
        self.facts = facts if facts is not None else FactsQueue()
        self._publish_now = publish_now
        self._lock = threading.Lock()
        self._next = 0
        self._committed = 0
        self._ends_lock = threading.Lock()
        self._stream_ends = {}
        self._ends_evicted_through = 0

    @property
    def committed(self):
        return self._committed

    def _publish_locked(self, facts, ordinal):
        for fact in facts:
            self._publish_now(fact if fact.stream_ordinal is not None
                              else replace(fact, stream_ordinal=ordinal))
            if fact.type in _ACCOUNTED_FACTS:
                if not self.facts.push(self._next, fact):
                    self._keep_stream_end(fact)
                self._next += 1

    def _keep_stream_end(self, fact):
        if fact.type == "stream_started":
            previous = fact.detail.get("previous_stream")
            ordinal, end = (fact.stream_ordinal - 1, previous.get("end_frame")) if previous else (None, None)
        elif fact.type == "stream_ended":
            ordinal, end = fact.stream_ordinal, fact.detail.get("end_frame")
        else:
            return
        if type(ordinal) is not int or type(end) is not int:
            return
        with self._ends_lock:
            if len(self._stream_ends) >= STREAM_END_RECORDS:
                oldest = next(iter(self._stream_ends))
                del self._stream_ends[oldest]
                self._ends_evicted_through = max(self._ends_evicted_through, oldest)
            self._stream_ends[ordinal] = end

    def take_stream_ends(self, through):
        with self._ends_lock:
            taken = {o: e for o, e in self._stream_ends.items() if o <= through}
            for ordinal in taken:
                del self._stream_ends[ordinal]
            return taken, self._ends_evicted_through

    def ingest(self, item):
        if item is None:
            return None
        with self._lock:
            if not isinstance(item, AdapterEvent) and (not isinstance(item, AdapterBlock)
                                                       or not _block_shape_ok(item, self.ring.channels)):
                item = AdapterEvent("helper_protocol_error", {"reason": "block_layout"},
                                    getattr(item, "received_monotonic_ns", 0))
            if isinstance(item, AdapterEvent):
                self._publish_locked(self.tracker.observe_event(item), self.tracker.stream_ordinal)
                self._committed = self._next
                return None
            before = self.tracker.stream_ordinal
            placement, facts = self.tracker.observe_block(item)
            if placement is None:
                self._publish_locked(facts, before)
                self._committed = self._next
                return None
            resumed = self.watchdog.block_received(item.received_monotonic_ns)
            ring_pos = self.ring.write(item.pcm)
            self._publish_locked((() if resumed is None else (resumed,)) + facts, before)
            self.descriptors.push(Descriptor(self._next, placement.stream_ordinal, placement.start_frame,
                                             placement.frames, ring_pos, item.received_monotonic_ns,
                                             placement.sample_time, placement.host_ns,
                                             item.adapter_dropped_frames_total))
            self._next += 1
            self._committed = self._next
        return placement

    def watchdog_tick(self, now_ns, *, heartbeat_ns=None, reader_backpressured=None):
        with self._lock:
            facts, stop = self.watchdog.tick(now_ns, capture_active=self.tracker.capture_active,
                                             heartbeat_ns=heartbeat_ns, reader_backpressured=reader_backpressured)
            self._publish_locked(facts, self.tracker.stream_ordinal)
        return stop

    def close(self, cause="stop"):
        with self._lock:
            self.tracker.close(cause)

    def take(self, committed):
        while True:
            d_index = self.descriptors.peek_index()
            f_index = self.facts.peek_index()
            d_index = d_index if d_index is not None and d_index < committed else None
            f_index = f_index if f_index is not None and f_index < committed else None
            if d_index is None and f_index is None:
                return None
            if f_index is None or (d_index is not None and d_index < f_index):
                descriptor = self.descriptors.pop_if(d_index)
                if descriptor is not None:
                    return descriptor
                continue
            return self.facts.pop()


@dataclass(frozen=True, slots=True)
class MeteredRoute:
    route_id: str
    role: str
    channels: tuple[int, ...]
    columns: tuple[int, ...]

    def __post_init__(self):
        if self.role not in ("master", "unlabeled"):
            raise ValueError("only master and unlabeled routes are metered; cue needs a routing test")
        if not self.channels or len(self.channels) != len(self.columns):
            raise ValueError("a metered route maps each of its channels to one ring column")

    @property
    def semantic_status(self):
        return SEMANTIC_STATUS[self.role]


def metered_routes(resolved_routes):
    metered = [r for r in resolved_routes if r["metering"] == "metered"]
    selected = [c for r in metered for c in r["channels"]]
    return tuple(MeteredRoute(r["route_id"], r["role"], tuple(r["channels"]),
                              tuple(selected.index(c) for c in r["channels"])) for r in metered)


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    session_id: str
    runtime_epoch: str
    audio_source: str
    sample_rate: int
    routes: tuple[MeteredRoute, ...]
    window_frames: int
    clip_threshold_dbfs: float
    near_silence_level_dbfs: float
    near_silence_min_duration_ms: int
    meter_coalesce_windows: int
    stale_after_ms: int
    device_instance_epoch: str
    host_time_scope: str | None = None

    def __post_init__(self):
        columns = sorted(c for r in self.routes for c in r.columns)
        if not self.routes or columns != list(range(len(columns))):
            raise ValueError("metered routes must cover the ring columns exactly once")
        if self.window_frames < 1 or self.meter_coalesce_windows < 1:
            raise ValueError("windows need at least one frame and coalescing at least one window")

    @property
    def channels(self):
        return sum(len(r.columns) for r in self.routes)


def _finite(value):
    if isinstance(value, dict):
        return {str(k): _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _float32_amplitude(dbfs):
    return float(np.float32(10.0 ** (dbfs / 20.0)))


def _dbfs(value):
    return round(20.0 * math.log10(value), 4) if value > 0 and math.isfinite(value) else None


class EventFactory:
    def __init__(self, context):
        self._ctx = context
        self._coverage = coverage(context.audio_source)
        self._route_ids = tuple(r.route_id for r in context.routes)
        self._stale_after_ns = context.stale_after_ms * 1_000_000

    def stream_epoch(self, ordinal):
        return None if ordinal is None else stream_epoch(self._ctx.runtime_epoch, ordinal)

    def _draft(self, *, delivery_class, ordinal, route, interval, device_clock, received_ns, valid_until,
               evidence):
        epoch = self.stream_epoch(ordinal)
        draft = {"schema": EVENT_SCHEMA, "session_id": self._ctx.session_id,
                 "runtime_epoch": self._ctx.runtime_epoch, "stream_epoch": epoch,
                 "delivery_class": delivery_class, "route_id": route.route_id if route else None,
                 "route_role": route.role if route else None,
                 "semantic_status": route.semantic_status if route else None, "route_calibration_id": None,
                 "interval": None if interval is None or epoch is None else
                 {"clock_id": stream_clock_id(epoch), "start_frame": interval[0], "frames": interval[1]},
                 "device_clock": device_clock, "received_monotonic_ns": received_ns,
                 "valid_until_monotonic_ns": valid_until, "evidence": evidence, "coverage": dict(self._coverage)}
        return _finite(draft)

    def device_clock(self, sample_time_start, host_ns, host_frame, host_sample_time):
        timed = host_ns is not None
        return {"clock_id": f"device-host:{self._ctx.device_instance_epoch}",
                "sample_time_start": float(sample_time_start), "host_time_ns": host_ns,
                "host_time_frame": host_frame if timed else None,
                "host_time_sample_time": float(host_sample_time) if timed else None,
                "host_time_scope": self._ctx.host_time_scope}

    def health(self, kind, *, received_ns, cause=None, ordinal=None, detail=None, state_transition=None,
               affected_routes=None, interval=None):
        evidence = {"kind": "health", "type": kind, "cause": cause,
                    "state_transition": None if state_transition is None else
                    {"from": state_transition[0], "to": state_transition[1]},
                    "affected_routes": list(self._route_ids if affected_routes is None else affected_routes),
                    "detail": detail or {}}
        draft = self._draft(delivery_class="health", ordinal=ordinal, route=None, interval=interval,
                            device_clock=None, received_ns=received_ns, valid_until=None, evidence=evidence)
        size = len(canonical_bytes({**draft, **_PUBLISHED_PLACEHOLDER}))
        if size <= EVENT_MAX_BYTES:
            return draft
        return {**draft, "evidence": {**draft["evidence"],
                                      "detail": {"detail_truncated": True, "detail_bytes": size}}}

    def fact(self, fact):
        return self.health(fact.type, received_ns=fact.received_monotonic_ns, cause=fact.cause,
                           ordinal=fact.stream_ordinal, detail=fact.detail,
                           state_transition=fact.state_transition, affected_routes=fact.affected_routes,
                           interval=fact.interval)

    def measurement(self, route, *, ordinal, interval, clocks, received_ns, evidence):
        return self._draft(delivery_class="normal", ordinal=ordinal, route=route, interval=interval,
                           device_clock=self.device_clock(*clocks), received_ns=received_ns,
                           valid_until=received_ns + self._stale_after_ns, evidence=evidence)


class WindowClocks(NamedTuple):
    sample_time_start: int
    host_ns: int | None
    host_frame: int
    host_sample_time: int


@dataclass(frozen=True, slots=True)
class RouteWindow:
    route: MeteredRoute
    stream_ordinal: int
    start_frame: int
    frames: int
    full: bool
    truncated_by: str | None
    n_finite: tuple[int, ...]
    sum_sq: tuple[float, ...]
    peak: tuple[float, ...]
    nonfinite: tuple[int, ...]
    clipped: tuple[int, ...]
    all_zero: tuple[bool, ...]
    clocks: WindowClocks
    received_ns: int

    @property
    def end_frame(self):
        return self.start_frame + self.frames

    @property
    def finite(self):
        return not any(self.nonfinite)


class _Window:
    __slots__ = ("all_zero", "clipped", "clocks", "frames", "n_finite", "nonfinite", "ordinal", "peak",
                 "received_ns", "start", "sum_sq")

    def __init__(self, ordinal, start, channels, clocks):
        self.ordinal, self.start, self.frames, self.clocks, self.received_ns = ordinal, start, 0, clocks, None
        self.n_finite = np.zeros(channels, dtype=np.int64)
        self.sum_sq = np.zeros(channels, dtype=np.float64)
        self.peak = np.zeros(channels, dtype=np.float64)
        self.nonfinite = np.zeros(channels, dtype=np.int64)
        self.clipped = np.zeros(channels, dtype=np.int64)
        self.all_zero = np.ones(channels, dtype=bool)

    @property
    def end(self):
        return self.start + self.frames

    def add(self, pcm, threshold, received_ns):
        samples = pcm.astype(np.float64)
        finite = np.isfinite(samples)
        values = np.where(finite, samples, 0.0)
        magnitude = np.abs(values)
        self.n_finite += finite.sum(axis=0)
        self.sum_sq += (values * values).sum(axis=0)
        np.maximum(self.peak, magnitude.max(axis=0), out=self.peak)
        self.nonfinite += (~finite).sum(axis=0)
        self.clipped += ((magnitude >= threshold) & finite).sum(axis=0)
        self.all_zero &= (samples == 0.0).all(axis=0)
        self.frames += pcm.shape[0]
        self.received_ns = received_ns

    def route_window(self, route, full, truncated_by):
        pick = list(route.columns)
        return RouteWindow(route, self.ordinal, self.start, self.frames, full, truncated_by,
                           tuple(int(v) for v in self.n_finite[pick]), tuple(float(v) for v in self.sum_sq[pick]),
                           tuple(float(v) for v in self.peak[pick]), tuple(int(v) for v in self.nonfinite[pick]),
                           tuple(int(v) for v in self.clipped[pick]), tuple(bool(v) for v in self.all_zero[pick]),
                           self.clocks, self.received_ns)


class MeterCoalescer:
    def __init__(self, route, context, factory):
        self._route = route
        self._limit = context.meter_coalesce_windows
        self._window_frames = context.window_frames
        self._threshold_dbfs = context.clip_threshold_dbfs
        self._factory = factory
        self._pending = []

    @property
    def pending(self):
        return len(self._pending)

    def transition(self, window):
        if self._pending and self._pending[-1].finite != window.finite:
            return self.flush("nonfinite_transition")
        return ()

    def add(self, window):
        events = list(self.transition(window))
        if self._pending and (self._pending[-1].stream_ordinal != window.stream_ordinal
                              or self._pending[-1].end_frame != window.start_frame):
            raise RuntimeError("meter windows must be contiguous within one stream epoch")
        self._pending.append(window)
        if window.truncated_by is not None:
            events += self.flush(window.truncated_by)
        elif len(self._pending) >= self._limit:
            events += self.flush(None)
        return tuple(events)

    def flush(self, truncated_by):
        if not self._pending:
            return ()
        windows = self._pending
        count = len(self._route.channels)
        nonfinite = [sum(w.nonfinite[c] for w in windows) for c in range(count)]
        n_finite = [sum(w.n_finite[c] for w in windows) for c in range(count)]
        sum_sq = [math.fsum(w.sum_sq[c] for w in windows) for c in range(count)]
        peak = [max(w.peak[c] for w in windows) for c in range(count)]
        first = windows[0]
        frames = sum(w.frames for w in windows)
        evidence = {"kind": "measurement", "type": "signal_level", "profile": LEVEL_PROFILE,
                    "channels": list(self._route.channels), "windows": len(windows),
                    "window_frames": self._window_frames, "frames_measured": frames,
                    "complete": all(w.full for w in windows), "truncated_by": truncated_by,
                    "sample_peak_dbfs": [None if nonfinite[c] else _dbfs(peak[c]) for c in range(count)],
                    "rms_dbfs": [None if nonfinite[c] or not n_finite[c] else _dbfs(math.sqrt(sum_sq[c] / n_finite[c]))
                                 for c in range(count)],
                    "digital_zero": [all(w.all_zero[c] for w in windows) for c in range(count)],
                    "nonfinite_count": nonfinite,
                    "clipped_count": [sum(w.clipped[c] for w in windows) for c in range(count)],
                    "clip_threshold_dbfs": self._threshold_dbfs, "finite": not any(nonfinite),
                    "true_peak": "not_measured", "loudness": "not_measured"}
        event = self._factory.measurement(self._route, ordinal=first.stream_ordinal,
                                          interval=(first.start_frame, frames), clocks=first.clocks,
                                          received_ns=windows[-1].received_ns, evidence=evidence)
        self._pending = []
        return (event,)


class NearSilenceTracker:
    def __init__(self, route, context, factory):
        self._route = route
        self._factory = factory
        self._level_dbfs = context.near_silence_level_dbfs
        self._level = _float32_amplitude(context.near_silence_level_dbfs)
        self._min_duration_ms = context.near_silence_min_duration_ms
        self._min_frames = math.ceil(context.near_silence_min_duration_ms * context.sample_rate / 1000)
        self._window_frames = context.window_frames
        self.state = "unknown"
        self._ordinal = None
        self._since = None
        self._end = None
        self._clocks = None

    @property
    def activity(self):
        return {"near_silent": "active", "unknown": "unknown"}.get(self.state, "inactive")

    def _event(self, state, received_ns, interrupted_by=None):
        evidence = {"kind": "measurement", "type": "near_silence", "profile": SILENCE_PROFILE, "state": state,
                    "level_dbfs": self._level_dbfs, "min_duration_ms": self._min_duration_ms,
                    "resolution_frames": self._window_frames, "quiet_since_frame": self._since,
                    "interrupted_by": interrupted_by, "does_not_establish": list(_SILENCE_CAVEATS)}
        return self._factory.measurement(self._route, ordinal=self._ordinal,
                                         interval=(self._since, self._end - self._since), clocks=self._clocks,
                                         received_ns=received_ns, evidence=evidence)

    def _reset(self, state):
        self.state, self._ordinal, self._since, self._end, self._clocks = state, None, None, None, None

    def interrupt(self, cause, received_ns):
        events = (self._event("interrupted", received_ns, cause),) if self.state == "near_silent" else ()
        self._reset("unknown")
        return events

    def interrupt_silently(self):
        self._reset("unknown")

    def update(self, window):
        if window.truncated_by is not None:
            return self.interrupt(window.truncated_by, window.received_ns)
        if not window.finite:
            return self.interrupt("nonfinite", window.received_ns)
        if not window.full:
            return self.interrupt("frames_missing", window.received_ns)
        if all(p < self._level for p in window.peak):
            events = ()
            if self.state in ("pending", "near_silent") and (self._ordinal != window.stream_ordinal
                                                              or self._end != window.start_frame):
                events = self.interrupt("frames_missing", window.received_ns)
            if self.state not in ("pending", "near_silent"):
                self.state, self._ordinal, self._since, self._clocks = ("pending", window.stream_ordinal,
                                                                        window.start_frame, window.clocks)
            self._end = window.end_frame
            if self.state == "pending" and self._end - self._since >= self._min_frames:
                self.state = "near_silent"
                return (*events, self._event("started", window.received_ns))
            return events
        events = (self._event("ended", window.received_ns),) if self.state == "near_silent" else ()
        self._reset("sounding")
        return events


class _UnmeasuredSpan(NamedTuple):
    ordinal: int
    start: int
    frames: int
    received_ns: int
    cause: str


class MeasurementWorker:
    def __init__(self, context, feed, publish, clock, *, factory=None):
        if feed.ring.channels != context.channels:
            raise ValueError("the PCM ring must hold exactly the metered channels of the context")
        self._ctx = context
        self._feed = feed
        self._publish = publish
        self._clock = clock
        self._factory = factory if factory is not None else EventFactory(context)
        self._window_frames = context.window_frames
        self._threshold = _float32_amplitude(context.clip_threshold_dbfs)
        self._scratch = np.empty((context.window_frames, feed.ring.channels), dtype=np.float32)
        self._coalescers = {r.route_id: MeterCoalescer(r, context, self._factory) for r in context.routes}
        self._silence = {r.route_id: NearSilenceTracker(r, context, self._factory) for r in context.routes}
        self._stream = None
        self._stream_open = False
        self._cursor = 0
        self._window = None
        self._unmeasured = None
        self._ring_end = 0
        self._skip_before = 0
        self._seen_overwritten = 0
        self._facts_lost = False
        self._lost_through = -1
        self._stopping = False
        self._inflight = None
        self._consumed = 0
        self._windowed_frames = 0
        self._published_frames = {r.route_id: 0 for r in context.routes}
        self._cancel = threading.Event()
        self._sealed = threading.Event()
        self._lock = threading.Lock()
        self._state = "running"
        self._error = None
        self._overruns = 0
        self._unmeasured_frames = 0
        self._failed_frames = 0
        self._unaccounted_frames = 0
        self._stop_frames = 0
        self._suppressed = 0
        self._nonfinite = {r.route_id: False for r in context.routes}
        self._latest = {r.route_id: None for r in context.routes}

    @property
    def state(self):
        with self._lock:
            return self._state

    def snapshot(self):
        with self._lock:
            routes = {rid: {"nonfinite": self._nonfinite[rid], "near_silence": self._silence[rid].activity,
                            "latest_signal": None if self._latest[rid] is None else dict(self._latest[rid])}
                      for rid in self._latest}
            state, error, overruns, unmeasured = self._state, self._error, self._overruns, self._unmeasured_frames
            failed_frames, suppressed = self._failed_frames, self._suppressed
            unaccounted, stop_frames = self._unaccounted_frames, self._stop_frames
        return {"state": state, "error": error, "lag_frames": max(0, self._feed.ring.write_counter - self._ring_end),
                "overruns": overruns, "unmeasured_frames": unmeasured,
                "unmeasured_after_failure_frames": failed_frames, "unaccounted_frames": unaccounted,
                "facts_dropped": self._feed.facts.dropped_total,
                "unmeasured_at_stop_frames": stop_frames, "suppressed_after_stop": suppressed,
                "routes": routes}

    def cancel(self):
        self._cancel.set()

    def seal(self):
        self._sealed.set()

    def _emit(self, event):
        if self._sealed.is_set():
            with self._lock:
                self._suppressed += 1
            return None
        sequence = self._publish(event)
        if sequence is None:
            with self._lock:
                self._suppressed += 1
            return None
        evidence = event["evidence"]
        if evidence.get("type") == "signal_level":
            with self._lock:
                self._latest[event["route_id"]] = {"evidence": evidence, "interval": event["interval"],
                                                   "received_monotonic_ns": event["received_monotonic_ns"],
                                                   "sequence": sequence}
                self._published_frames[event["route_id"]] += evidence["frames_measured"]
        return sequence

    def _read_without_a_level(self):
        window = self._window
        with self._lock:
            behind = max(self._windowed_frames - published for published in self._published_frames.values())
        return behind + (window.frames if window is not None else 0)

    def account_held(self):
        d = self._inflight
        held = self._read_without_a_level()
        held = max(0, held - (max(0, self._consumed - d.start_frame) if d is not None else 0))
        with self._lock:
            self._unmeasured_frames += held
            self._stop_frames += held
        return held

    def _emit_all(self, events):
        for event in events:
            self._emit(event)

    def _fail(self, error):
        with self._lock:
            if self._state == "failed":
                return
            self._state = "failed"
            self._error = f"{type(error).__name__}: {error}"[:300]
        window, d = self._window, self._inflight
        self._window = self._inflight = None
        lost = window.frames if window is not None else 0
        if d is not None:
            lost += max(0, d.start_frame + d.frames - self._consumed)
            self._cursor = max(self._cursor, d.start_frame + d.frames)
            self._ring_end = d.ring_pos + d.frames
        with suppress(Exception):
            self._close_unmeasured()
        now = self._clock.monotonic_ns()
        self._emit(self._factory.health("measurement_worker_failed", received_ns=now,
                                        cause="internal_error", ordinal=self._stream,
                                        detail={"error_type": type(error).__name__, "error": str(error)[:300]}))
        for route in self._ctx.routes:
            rid = route.route_id
            with suppress(Exception):
                self._emit_all(self._coalescers[rid].flush("measurement_worker_failed"))
                self._emit_all(self._silence[rid].interrupt("measurement_worker_failed", now))
            self._silence[rid].interrupt_silently()
            with self._lock:
                self._nonfinite[rid] = False
        lost += self._read_without_a_level()
        with self._lock:
            self._unmeasured_frames += lost
            self._failed_frames += lost

    def _guarded(self, function, *args):
        if self.state != "failed":
            contained(partial(function, *args), on_error=self._fail)

    def run_pending(self, *, deadline_ns=None):
        committed = self._feed.committed
        taken = 0
        while not self._cancel.is_set():
            if deadline_ns is not None and self._clock.monotonic_ns() >= deadline_ns:
                break
            item = self._feed.take(committed)
            if item is None:
                break
            taken += 1
            self._take(item)
        if self._ring_end >= self._skip_before:
            self._guarded(self._close_unmeasured)
        return taken

    def account_rest(self):
        committed = self._feed.committed
        self._stopping = True
        skipped = 0
        while True:
            item = self._feed.take(committed)
            if item is None:
                break
            skipped += item.frames if isinstance(item, Descriptor) and self.state != "failed" else 0
            self._take(item)
        self._guarded(self._close_unmeasured)
        return skipped

    def _take(self, item):
        if isinstance(item, DroppedFacts):
            self._facts_dropped(item)
            return
        if isinstance(item, Fact):
            self._on_fact(item)
            return
        run = self._feed.facts.pop_run_before(item.ingest_index)
        if run is not None:
            self._facts_dropped(run)
        if self.state == "failed":
            start = self._cursor if item.stream_ordinal == self._stream else 0
            skipped = max(0, item.start_frame - start)
            unaccounted = skipped if self._gap_cause(item.ingest_index) == "facts_queue_full" else 0
            with self._lock:
                self._unmeasured_frames += item.frames + skipped - unaccounted
                self._failed_frames += item.frames + skipped - unaccounted
                self._unaccounted_frames += unaccounted
            self._stream, self._cursor = item.stream_ordinal, max(start, item.start_frame + item.frames)
            self._ring_end = item.ring_pos + item.frames
        else:
            self._guarded(self._measure, item)

    def _facts_dropped(self, run):
        self._facts_lost = True
        self._lost_through = max(self._lost_through, run.last_index)

    def flush(self, cause="stop"):
        self._guarded(self._close_unmeasured)
        self._guarded(self._truncate, cause, self._clock.monotonic_ns())

    def _on_fact(self, fact):
        self._guarded(self._reach, fact)
        self._guarded(self._account, fact)
        if self.state == "failed" and fact.type == "frames_missing" and fact.stream_ordinal == self._stream:
            self._cursor = max(self._cursor, sum(fact.interval))
        if fact.type == "stream_started":
            self._stream, self._stream_open, self._cursor = fact.stream_ordinal, True, 0

    def _reach(self, fact):
        if fact.stream_ordinal is None:
            return
        target = fact.stream_ordinal - 1 if fact.type == "stream_started" else fact.stream_ordinal
        if target < 1 or (self._stream is not None and target <= self._stream):
            return
        self._skip_to_stream(target, fact.received_monotonic_ns)
        self._stream, self._stream_open, self._cursor = target, True, 0

    def _gap_cause(self, ingest_index=None):
        lost = self._facts_lost or (ingest_index is not None
                                    and self._feed.facts.dropped_before(ingest_index))
        return "facts_queue_full" if lost else "measurement_lag"

    def _account(self, fact):
        received = fact.received_monotonic_ns
        if fact.type == "frames_missing":
            at, frames = fact.interval
            if self._stream_open and at > self._cursor:
                self._unmeasured_span(self._cursor, at - self._cursor, received, cause=self._gap_cause())
            self._close_unmeasured()
            self._truncate("frames_missing", received)
            self._cursor = at + frames
        elif fact.type == "stream_started":
            previous = fact.detail.get("previous_stream")
            self._end_stream(previous["end_frame"] if previous else None, received)
        else:
            self._end_stream(fact.detail.get("end_frame"), received)
        self._facts_lost = False

    def _end_stream(self, end_frame, received_ns):
        if not self._stream_open:
            return
        if end_frame is not None and end_frame > self._cursor:
            self._unmeasured_span(self._cursor, end_frame - self._cursor, received_ns,
                                  cause=self._gap_cause())
        self._close_unmeasured()
        self._truncate("stream_ended", received_ns)
        self._stream_open = False

    def _measure(self, d):
        self._inflight, self._consumed = d, d.start_frame
        if d.stream_ordinal != self._stream or not self._stream_open:
            self._skip_to_stream(d.stream_ordinal, d.received_ns)
            self._stream, self._stream_open, self._cursor = d.stream_ordinal, True, 0
        self._consumed = min(self._cursor, d.start_frame)
        if d.start_frame > self._cursor:
            overwritten = self._feed.descriptors.overwritten
            if overwritten != self._seen_overwritten:
                self._seen_overwritten = overwritten
                target = self._feed.descriptors.position_half_behind()
                if target is not None:
                    self._skip_before = max(self._skip_before, target)
            self._unmeasured_span(self._cursor, d.start_frame - self._cursor, d.received_ns,
                                  cause=self._gap_cause(d.ingest_index))
            self._consumed = d.start_frame
        if d.ingest_index > self._lost_through:
            self._facts_lost = False
        ring = self._feed.ring
        size = self._window_frames
        offset = 0
        while offset < d.frames:
            frame = d.start_frame + offset
            count = min(d.frames - offset, (frame // size + 1) * size - frame)
            position = d.ring_pos + offset
            if self._stopping:
                held = position >= max(self._skip_before, ring.write_counter - ring.capacity_frames)
                self._unmeasured_span(frame, count, d.received_ns,
                                      cause="stop_drain_bound" if held else "measurement_lag")
                self._consumed = frame + count
                offset += count
                continue
            pcm = (None if position < self._skip_before
                   else ring.read(position, count, out=self._scratch[:count]))
            if pcm is None:
                if position >= self._skip_before:
                    self._skip_before = ring.write_counter - ring.capacity_frames // 2
                self._unmeasured_span(frame, count, d.received_ns, cause="measurement_lag")
                self._consumed = frame + count
            else:
                self._close_unmeasured()
                self._add(d, frame, pcm)
            offset += count
        self._cursor = max(self._cursor, d.start_frame + d.frames)
        self._ring_end = d.ring_pos + d.frames
        self._inflight = None

    def _skip_to_stream(self, ordinal, received_ns):
        ends, evicted_through = self._feed.take_stream_ends(ordinal - 1)
        if self._stream is not None:
            if self._stream_open and self._stream not in ends and self._stream <= evicted_through:
                self._unknown_extent(self._stream, self._cursor, received_ns)
            self._end_stream(ends.get(self._stream), received_ns)
        for skipped in range(1 if self._stream is None else self._stream + 1, ordinal):
            end = ends.get(skipped)
            if end is not None and end > 0:
                self._close_unmeasured()
                self._unmeasured = _UnmeasuredSpan(skipped, 0, end, received_ns, "facts_queue_full")
                self._close_unmeasured()
            elif end is None and skipped <= evicted_through:
                self._unknown_extent(skipped, 0, received_ns)

    def _unknown_extent(self, ordinal, start_frame, received_ns):
        self._close_unmeasured()
        self._emit(self._factory.health(
            "frames_unaccounted", received_ns=received_ns, cause="facts_queue_full", ordinal=ordinal,
            detail={"unaccounted": None, "from_frame": start_frame, "extent": "unknown",
                    "may_be": ["frames_missing", "measurement_overrun"]}))

    def _add(self, d, frame, pcm):
        window = self._window
        if window is not None and (window.ordinal != self._stream or window.end != frame):
            raise RuntimeError("a window must stay contiguous; a fact should have truncated it")
        if window is None:
            clocks = WindowClocks(d.sample_time + (frame - d.start_frame), d.host_ns, d.start_frame,
                                  d.sample_time)
            window = self._window = _Window(self._stream, frame, self._ctx.channels, clocks)
        window.add(pcm, self._threshold, d.received_ns)
        self._consumed = frame + pcm.shape[0]
        if window.end % self._window_frames == 0:
            self._close_window(None)

    def _close_window(self, truncated_by):
        window, self._window = self._window, None
        if window is None or not window.frames:
            return False
        full = truncated_by is None and window.frames == self._window_frames
        with self._lock:
            self._windowed_frames += window.frames
        for route in self._ctx.routes:
            self._route_window(window.route_window(route, full, truncated_by))
        return True

    def _route_window(self, window):
        route_id = window.route.route_id
        coalescer = self._coalescers[route_id]
        self._emit_all(coalescer.transition(window))
        if window.finite == self._nonfinite[route_id]:
            state = "ended" if window.finite else "started"
            with self._lock:
                self._nonfinite[route_id] = not window.finite
            self._emit(self._factory.health(
                "nonfinite_input", received_ns=window.received_ns, cause="nonfinite_samples",
                ordinal=window.stream_ordinal, affected_routes=(route_id,),
                interval=(window.start_frame, window.frames),
                state_transition=("available", "nonfinite") if state == "started" else ("nonfinite", "available"),
                detail={"state": state, "channels": list(window.route.channels), "counts": list(window.nonfinite)}))
        self._emit_all(self._silence[route_id].update(window))
        self._emit_all(coalescer.add(window))

    def _truncate(self, cause, received_ns):
        if self._close_window(cause):
            return
        for route in self._ctx.routes:
            self._emit_all(self._coalescers[route.route_id].flush(cause))
            self._emit_all(self._silence[route.route_id].interrupt(cause, received_ns))

    def _unmeasured_span(self, start, frames, received_ns, *, cause):
        run = self._unmeasured
        if (run is not None and run.ordinal == self._stream and run.start + run.frames == start
                and run.cause == cause):
            self._unmeasured = _UnmeasuredSpan(run.ordinal, run.start, run.frames + frames, received_ns,
                                               cause)
            return
        self._close_unmeasured()
        self._truncate("frames_unaccounted" if cause == "facts_queue_full" else "measurement_overrun",
                       received_ns)
        self._unmeasured = _UnmeasuredSpan(self._stream, start, frames, received_ns, cause)

    def _close_unmeasured(self):
        run, self._unmeasured = self._unmeasured, None
        if run is None:
            return
        ordinal, start, frames, received, cause = run
        interval = {"start_frame": start, "frames": frames}
        if cause == "facts_queue_full":
            with self._lock:
                self._unaccounted_frames += frames
            self._emit(self._factory.health("frames_unaccounted", received_ns=received, cause=cause,
                                            ordinal=ordinal, interval=(start, frames),
                                            detail={"unaccounted": interval,
                                                    "may_be": ["frames_missing", "measurement_overrun"]}))
            return
        with self._lock:
            self._overruns += 1
            self._unmeasured_frames += frames
            self._stop_frames += frames if cause == "stop_drain_bound" else 0
        self._emit(self._factory.health("measurement_overrun", received_ns=received, cause=cause,
                                        ordinal=ordinal, interval=(start, frames),
                                        detail={"unmeasured": interval, "exact": True}))


class _RetainedEvent(NamedTuple):
    data: bytes
    size: int
    route_id: str | None
    received_ns: int


class _Retention:
    def __init__(self, max_events, max_bytes):
        if max_events < 1 or max_bytes < EVENT_MAX_BYTES:
            raise ValueError("a retention class must hold at least one event of the largest size")
        self.max_events = max_events
        self.max_bytes = max_bytes
        self._seqs = []
        self._entries = []
        self._start = 0
        self.bytes = 0
        self.evicted_through = 0
        self.evicted = 0

    def __len__(self):
        return len(self._seqs) - self._start

    @property
    def end(self):
        return len(self._seqs)

    def first(self):
        return self._seqs[self._start] if len(self) else None

    def last(self):
        return self._seqs[-1] if len(self) else None

    def sequence_at(self, index):
        return self._seqs[index]

    def entry_at(self, index):
        return self._entries[index]

    def index_after(self, sequence):
        return bisect.bisect_right(self._seqs, sequence, lo=self._start)

    def tail(self, count):
        return [(s, e.data) for s, e in zip(self._seqs[max(self._start, self.end - count):],
                                            self._entries[max(self._start, self.end - count):], strict=True)]

    def append(self, sequence, entry):
        self._seqs.append(sequence)
        self._entries.append(entry)
        self.bytes += entry.size
        while len(self) > self.max_events or self.bytes > self.max_bytes:
            self.bytes -= self._entries[self._start].size
            self.evicted_through = self._seqs[self._start]
            self._entries[self._start] = None
            self._start += 1
            self.evicted += 1
        if self._start >= 1024 and 2 * self._start >= len(self._seqs):
            del self._seqs[:self._start]
            del self._entries[:self._start]
            self._start = 0

    def summary(self):
        return {"first_sequence": self.first(), "last_sequence": self.last(), "events": len(self),
                "bytes": self.bytes, "evicted_through": self.evicted_through}


class EventBroker:
    def __init__(self, runtime_epoch, clock, *, normal_events, normal_bytes, health_events, health_bytes,
                 on_publish=None):
        self.runtime_epoch = runtime_epoch
        self._clock = clock
        self._on_publish = on_publish
        self._cond = threading.Condition(threading.Lock())
        self._normal = _Retention(normal_events, normal_bytes)
        self._health = _Retention(health_events, health_bytes)
        self._head = 0
        self._health_lost_through = 0
        self._latest = {}
        self._hook_errors = 0
        self._closed = False
        self._final = False
        self._refused_after_final = 0

    @property
    def head(self):
        with self._cond:
            return self._head

    def publish(self, draft, *, final=False):
        delivery = draft.get("delivery_class")
        if delivery not in ("normal", "health"):
            raise ValueError("an event is either normal or health")
        evidence = draft["evidence"]
        with self._cond:
            if self._final:
                self._refused_after_final += 1
                return None
            sequence = self._head + 1
            event = {**draft, "sequence": sequence, "published_monotonic_ns": self._clock.monotonic_ns()}
            data = canonical_bytes(event)
            if len(data) > EVENT_MAX_BYTES:
                raise ValueError(f"a Listen event is at most {EVENT_MAX_BYTES} bytes")
            self._head = sequence
            entry = _RetainedEvent(data, len(data), event.get("route_id"), event["received_monotonic_ns"])
            if delivery == "health":
                self._health.append(sequence, entry)
                self._health_lost_through = max(self._health_lost_through, self._health.evicted_through)
            else:
                self._normal.append(sequence, entry)
                if evidence.get("type") == "signal_level":
                    self._latest[event["route_id"]] = (sequence, data)
            if self._on_publish is not None:
                contained(partial(self._on_publish, event), on_error=self._count_hook_error_locked)
            if final:
                self._final = True
            self._cond.notify_all()
        return sequence

    def close(self):
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def _count_hook_error_locked(self, error):
        self._hook_errors += 1

    def _counters_locked(self):
        return {"head_sequence": self._head, "normal": self._normal.summary(),
                "health": self._health.summary(), "health_lost_through": self._health_lost_through,
                "evicted": {"normal": self._normal.evicted, "health": self._health.evicted},
                "publish_hook_errors": self._hook_errors, "refused_after_final": self._refused_after_final}

    def counters(self):
        with self._cond:
            return self._counters_locked()

    def view(self, recent_health=16):
        with self._cond:
            head = self._head
            counters = self._counters_locked()
            latest = dict(self._latest)
            recent = self._health.tail(recent_health)
        return {"head": head, "counters": counters,
                "latest": {route: json.loads(data) for route, (_, data) in latest.items()},
                "recent_health": [json.loads(data) for _, data in recent]}

    def _earliest_locked(self):
        return {"health": self._health.first(), "normal": self._normal.first()}

    def poll(self, after, *, routes=None, kinds=None, max_events=64, max_bytes=65536, wait_ms=0,
             max_age_ms=None, on_expired="refuse"):
        if max_events < 1 or max_bytes < EVENT_MAX_BYTES:
            raise ValueError("a page must hold at least one event of the largest size")
        cursor = after["sequence"]
        waited_from = time.monotonic_ns()
        with self._cond:
            base = {"runtime_epoch": self.runtime_epoch, "after": dict(after)}
            if after["runtime_epoch"] != self.runtime_epoch:
                return {**base, "events_status": "runtime_epoch_mismatch",
                        "current_runtime_epoch": self.runtime_epoch, "head_sequence": self._head,
                        "now_monotonic_ns": self._clock.monotonic_ns(), "waited_ms": 0}
            if cursor > self._head:
                raise PocketError(f"after.sequence {cursor} is ahead of this runtime's newest event "
                                  f"{self._head}", hint=_CURSOR_AHEAD_HINT)
            if wait_ms > 0 and self._head == cursor and not self._closed:
                self._cond.wait_for(lambda: self._head > cursor or self._closed, timeout=wait_ms / 1000)
            now = self._clock.monotonic_ns()
            base.update(head_sequence=self._head, now_monotonic_ns=now,
                        waited_ms=(time.monotonic_ns() - waited_from) // 1_000_000)
            if cursor < self._health_lost_through:
                return {**base, "events_status": "snapshot_required",
                        "health_lost": {"through_sequence": self._health_lost_through},
                        "earliest_retained": self._earliest_locked(),
                        "resume": {"call": "listen_observe", "use": "cursor"}}
            expired_through = self._normal.evicted_through if cursor < self._normal.evicted_through else None
            health_only = kinds == ("health",)
            evicted_unsubscribed = expired_through if health_only else None
            expired_through = None if health_only else expired_through
            if expired_through is not None and on_expired == "refuse":
                return {**base, "events_status": "cursor_expired",
                        "expired": {"delivery_class": "normal", "evicted_through": expired_through},
                        "earliest_retained": self._earliest_locked(),
                        "resume": {"call": "listen_observe", "use": "cursor",
                                   "or": {"on_expired": "resume_from_earliest"}}}
            page = self._walk_locked(cursor, routes, kinds, max_events, max_bytes, max_age_ms, now)
            through = None if evicted_unsubscribed is None else min(evicted_unsubscribed, page["next_after"])
            evicted_skipped = (0 if through is None or through <= cursor else
                               through - cursor - (self._health.index_after(through)
                                                   - self._health.index_after(cursor)))
            retained = {"health": self._health.summary(), "normal": self._normal.summary()}
        page = {**page, "filtered": {**page["filtered"], "kind": page["filtered"]["kind"] + evicted_skipped}}
        next_after = page["next_after"]
        missing = []
        if expired_through is not None and min(expired_through, next_after) > cursor:
            missing.append({"from_sequence": cursor + 1, "through_sequence": min(expired_through, next_after),
                            "delivery_class": "normal", "cause": "retention_evicted",
                            "health_in_range": "delivered"})
        return {**base, "events_status": "page", "events": [json.loads(data) for data in page["events"]],
                "next_after": {"runtime_epoch": self.runtime_epoch, "sequence": next_after},
                "missing": missing, "filtered": page["filtered"],
                "truncated": page["truncated_by"] is not None, "truncated_by": page["truncated_by"],
                "more_available": base["head_sequence"] > next_after,
                "retained": retained}

    def _walk_locked(self, cursor, routes, kinds, max_events, max_bytes, max_age_ms, now):
        health, normal = self._health, self._normal
        h, n = health.index_after(cursor), normal.index_after(cursor)
        events, used, examined = [], 0, 0
        filtered = {"kind": 0, "routes": 0, "stale_measurements": 0}
        measurements = kinds is None or "measurement" in kinds
        max_age_ns = None if max_age_ms is None else max_age_ms * 1_000_000
        next_after, truncated_by = self._head, None
        while h < health.end or n < normal.end:
            from_health = n >= normal.end or (h < health.end
                                              and health.sequence_at(h) < normal.sequence_at(n))
            store, index = (health, h) if from_health else (normal, n)
            sequence = store.sequence_at(index)
            data, size, route_id, received_ns = store.entry_at(index)
            if examined >= POLL_SCAN_LIMIT:
                next_after, truncated_by = sequence - 1, "scan_limit"
                break
            skip = None
            if not from_health:
                if not measurements:
                    skip = "kind"
                elif routes is not None and route_id not in routes:
                    skip = "routes"
                elif max_age_ns is not None and now - received_ns > max_age_ns:
                    skip = "stale_measurements"
            if skip is None:
                if len(events) >= max_events or used + size > max_bytes:
                    next_after = sequence - 1
                    truncated_by = "max_events" if len(events) >= max_events else "max_bytes"
                    break
                events.append(data)
                used += size
            else:
                filtered[skip] += 1
            examined += 1
            if from_health:
                h += 1
            else:
                n += 1
        return {"events": events, "filtered": filtered, "next_after": next_after,
                "truncated_by": truncated_by}


def _setting(config, *path):
    value = config
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise ValueError(f"the resolved Listen config has no {'.'.join(path)}")
        value = value[key]
    return value


def _route_names(route_ids):
    names = list(route_ids)
    text = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return text[:1].upper() + text[1:]


def _thousands(value):
    return f"{int(value):,}"


@dataclass(slots=True)
class _RingRun:
    ring_start: int
    stream_ordinal: int
    start_frame: int
    frames: int


class ListenCore:
    def __init__(self, config, opened, *, session_id, runtime_epoch, audio_source, adapter_profile,
                 config_sha256, clock, ordinal=1, pid=None, started_ns=None, adapter_stats=None,
                 journal_status=None, security=None, on_publish=None, descriptor_capacity=DESCRIPTOR_CAPACITY,
                 facts_capacity=FACTS_CAPACITY, provenance=None):
        if opened.refusal or opened.identity is None or opened.format is None:
            raise ValueError("a refused open is sealed by the host; it never builds a core")
        fmt, identity = opened.format, opened.identity
        resolved, reasons = resolve_routes(_setting(config, "routes"), fmt.device_input_channels)
        if reasons:
            raise ValueError(f"route resolution refused capture: {', '.join(reasons)}")
        self.session_id, self.runtime_epoch, self.ordinal = session_id, runtime_epoch, ordinal
        self.audio_source, self.adapter_profile = audio_source, adapter_profile
        self.config_sha256 = config_sha256
        self.provenance = dict(provenance) if provenance else None
        self.identity, self.format = identity, fmt
        self.resolved_routes = resolved
        self.selected_channels = selected_channels(resolved)
        self.pid = os.getpid() if pid is None else pid
        self._clock = clock
        self._adapter_stats = adapter_stats
        self._journal_status = journal_status
        self._security = security
        self._route_ids = tuple(r["route_id"] for r in resolved)
        self._name_shared_with = (sum(1 for row in opened.inventory
                                      if row.get("name") == identity.name and row.get("uid") != identity.uid)
                                  if opened.inventory else None)
        self.device_instance_epoch = device_instance_epoch(identity.uid, identity.instance, runtime_epoch)
        self.topology_epoch = topology_epoch(self.device_instance_epoch, fmt, self.selected_channels,
                                             config_sha256)
        self.started_ns = clock.monotonic_ns() if started_ns is None else started_ns
        rate = fmt.sample_rate
        self.context = RuntimeContext(
            session_id, runtime_epoch, audio_source, rate, metered_routes(resolved),
            _setting(config, "measurement", "window_frames"),
            _setting(config, "measurement", "clip_threshold_dbfs"),
            _setting(config, "measurement", "near_silence", "level_dbfs"),
            _setting(config, "measurement", "near_silence", "min_duration_ms"),
            _setting(config, "delivery", "meter_coalesce_windows"),
            _setting(config, "health", "stale_after_ms"),
            self.device_instance_epoch, fmt.host_time_scope)
        self._max_runtime_seconds = _setting(config, "limits", "max_runtime_seconds")
        self.tracker = StreamTracker(runtime_epoch, rate, topology_epoch=self.topology_epoch)
        self.ring = PcmRing(_setting(config, "buffering", "ring_seconds") * rate, len(self.selected_channels))
        self.watchdog = Watchdog(self.context.stale_after_ms, self.started_ns,
                                 max_runtime_seconds=self._max_runtime_seconds)
        self.feed = CaptureFeed(self.tracker, self.ring, self.watchdog,
                                descriptors=DescriptorRing(descriptor_capacity),
                                facts=FactsQueue(facts_capacity), publish_now=self._publish_fact)
        self.broker = EventBroker(runtime_epoch, clock,
                                  normal_events=_setting(config, "delivery", "normal_retention_events"),
                                  normal_bytes=_setting(config, "delivery", "normal_retention_bytes"),
                                  health_events=_setting(config, "delivery", "health_retention_events"),
                                  health_bytes=_setting(config, "delivery", "health_retention_bytes"),
                                  on_publish=on_publish)
        self.factory = EventFactory(self.context)
        self.worker = MeasurementWorker(self.context, self.feed, self._publish, clock, factory=self.factory)
        self._measure_lock = threading.Lock()
        self._lock = threading.Lock()
        self._work = threading.Condition(threading.Lock())
        self._runs_lock = threading.Lock()
        self._totals_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._state = "ready"
        self._stop = None
        self._stopped_ns = None
        self._drain = None
        self._work_pending = False
        self._closed = False
        self._runs = deque(maxlen=256)
        self._stats_errors = 0
        self._stats_failed = False
        self._totals = {rid: {"frames_measured": 0, "max_sample_peak_dbfs": None, "clipped_total": 0,
                              "nonfinite_total": 0, "near_silence_frames": 0}
                        for rid in (r.route_id for r in self.context.routes)}

    @property
    def state(self):
        with self._lock:
            return self._state

    def begin(self, now_ns=None):
        with self._measure_lock, self._lock:
            if self._state != "ready":
                raise RuntimeError("a Listen runtime begins once")
            now = self._now(now_ns)
            self._state = "running"
            self.watchdog.arm(now)
            self._publish(self.factory.health(
                "runtime_started", received_ns=now, state_transition=("launching", "running"),
                affected_routes=(),
                detail={"ordinal": self.ordinal, "pid": self.pid, "adapter": self.adapter_profile,
                        "started_monotonic_ns": self.started_ns,
                        "scheduled_stop_monotonic_ns": self.watchdog.scheduled_stop_ns,
                        "max_runtime_seconds": self._max_runtime_seconds}))
            self._publish(self.factory.health(
                "capture_opened", received_ns=now,
                detail={"device": self._device(), "device_instance_epoch": self.device_instance_epoch,
                        "topology_epoch": self.topology_epoch, "format": self._format(),
                        "clocks": self._clocks(None),
                        "unwatched_properties": self._stats().get("unwatched_properties"),
                        "provenance": self.provenance, "routes": [
                            {k: r[k] for k in ("route_id", "role", "channels", "channels_exposed",
                                               "metering")}
                            for r in self.resolved_routes]}))

    def begin_stop(self, cause, *, request_id=None, now_ns=None):
        with self._lock:
            if self._state != "running":
                return False
            now = self._now(now_ns)
            self._state = "stopping"
            self._stop = {"cause": cause, "request_id": request_id, "requested_monotonic_ns": now}
            self._publish(self.factory.health("runtime_stopping", received_ns=now, cause=cause,
                                              affected_routes=(), state_transition=("running", "stopping"),
                                              detail={"request_id": request_id}))
        self._signal_work()
        return True

    def finish_stop(self, *, now_ns=None, drain_ns=500_000_000):
        with self._lock:
            if self._state != "stopping":
                raise RuntimeError("call begin_stop before finish_stop")
        self.feed.close("stop")
        waits_end = time.monotonic() + 2 * drain_ns / 1e9
        drained = self._measure_lock.acquire(timeout=drain_ns / 1e9)
        cancelled = not drained
        if cancelled:
            self.worker.cancel()
            drained = self._measure_lock.acquire(timeout=max(0.0, waits_end - time.monotonic()))
        try:
            drain = "incomplete"
            if drained:
                if not cancelled:
                    self.worker.run_pending(deadline_ns=self._clock.monotonic_ns() + drain_ns)
                drain = "incomplete" if self.worker.account_rest() else "complete"
                self.worker.flush("stop")
            self.worker.seal()
            if not drained:
                self.worker.account_held()
            snapshot = self.tracker.snapshot()
            with self._lock:
                now = self._now(now_ns)
                self._state, self._stopped_ns, self._drain = "stopped", now, drain
                self._publish(self.factory.health(
                    "runtime_stopped", received_ns=now, cause=self._stop["cause"], affected_routes=(),
                    state_transition=("stopping", "stopped"),
                    detail={"request_id": self._stop["request_id"], "worker_drain": drain,
                            "capture": {"final_state": snapshot["state"],
                                        "ended_cause": snapshot["ended_cause"]}}), final=True)
            self._closed = True
        finally:
            if drained:
                self._measure_lock.release()
        self.broker.close()
        self._signal_work()
        return self.terminal_summary()

    def _publish_fact(self, fact):
        return self._publish(self.factory.fact(fact))

    def publish_health(self, kind, *, cause=None, detail=None, state_transition=None, affected_routes=(),
                       now_ns=None):
        return self._publish(self.factory.health(kind, received_ns=self._now(now_ns), cause=cause,
                                                 detail=detail, state_transition=state_transition,
                                                 affected_routes=affected_routes))

    def ingest(self, item):
        if self._state == "ready":
            raise RuntimeError("call begin before ingesting capture")
        if self._closed or item is None:
            return None
        placement = self.feed.ingest(item)
        if placement is not None:
            self._note_run(placement)
        self._signal_work()
        return placement

    def wait_for_work(self, timeout_ns):
        with self._work:
            ready = self._work.wait_for(lambda: self._work_pending or self._closed, timeout=timeout_ns / 1e9)
            self._work_pending = False
            return bool(ready) and not self._closed

    def measure_pending(self):
        with self._measure_lock:
            if self._closed or self._state == "ready":
                return 0
            return self.worker.run_pending()

    def watchdog_tick(self, now_ns=None, *, heartbeat_ns=None):
        if self.state != "running":
            return None
        stats = self._stats() if self._adapter_stats is not None else {}
        heartbeat = stats.get("last_heartbeat_monotonic_ns") if heartbeat_ns is None else heartbeat_ns
        cause = self.feed.watchdog_tick(self._now(now_ns), heartbeat_ns=heartbeat,
                                        reader_backpressured=stats.get("reader_backpressured"))
        self._signal_work()
        return cause

    def observe(self, now_ns=None):
        with self._lock:
            state, stop = self._state, dict(self._stop) if self._stop else None
            view = self.broker.view()
        now = self._now(now_ns)
        capture = self.tracker.snapshot()
        watch = self.watchdog.snapshot(now)
        worker = self.worker.snapshot()
        capture_state = self._capture_state(capture, watch)
        routes = self._route_rows(capture_state, worker, view["latest"], now)
        journal = self._journal_status() if self._journal_status is not None else None
        counters = view["counters"]
        adapter = self._adapter_report(now)
        checked = (capture["state"] not in _FINAL_STATES and not self._stats_unreadable()
                   and adapter is not None and adapter.get("last_heartbeat_age_ms") is not None)
        unresponsive = watch["helper_unresponsive"] if checked else "unknown"
        observation = {
            "schema": OBSERVATION_SCHEMA,
            "summary": self._summary(routes, capture, watch, self._lag_behind_s(worker)),
            "runtime": {"state": state, "runtime_epoch": self.runtime_epoch, "ordinal": self.ordinal,
                        "pid": self.pid, "started_monotonic_ns": self.started_ns,
                        "scheduled_stop_monotonic_ns": watch["scheduled_stop_monotonic_ns"],
                        "now_monotonic_ns": now, "stop": stop},
            "capture": {"state": capture_state, "adapter": self.adapter_profile, "device": self._device(),
                        "device_instance_epoch": self.device_instance_epoch,
                        "topology_epoch": self.topology_epoch,
                        "format": self._format(), "clocks": self._clocks(capture),
                        "stream_epoch": capture["stream_epoch"], "stream_ordinal": capture["stream_ordinal"],
                        "frames_received": capture["frames_received"],
                        "last_block": capture["last_block"], "age_ms": watch["age_ms"],
                        "stale_after_ms": watch["stale_after_ms"], "stale_episodes": watch["stale_episodes"],
                        "helper_unresponsive": unresponsive,
                        "missing": capture["missing"], "ended_cause": capture["ended_cause"],
                        "adapter_stats": adapter},
            "ring": {"capacity_frames": self.ring.capacity_frames, "bytes": self.ring.nbytes,
                     "retained": self._retained(capture), "retention": "in_memory_bounded_window_no_file"},
            "worker": {k: worker[k] for k in WORKER_KEYS},
            "broker": {"head_sequence": counters["head_sequence"], "normal": counters["normal"],
                       "health": counters["health"], "health_lost_through": counters["health_lost_through"],
                       "publish_hook_errors": counters["publish_hook_errors"]},
            "journal": journal,
            "security": self._security,
            "routes": routes,
            "recent_health": [{"sequence": e["sequence"], "type": e["evidence"]["type"],
                               "cause": e["evidence"]["cause"],
                               "affected_routes": e["evidence"]["affected_routes"],
                               "published_monotonic_ns": e["published_monotonic_ns"]}
                              for e in view["recent_health"]],
            "notices": self._notices(state, capture_state, capture, watch, worker, routes, journal, now,
                                     adapter),
            "snapshot_sequence": view["head"],
            "coverage": self._coverage(capture["frames_received"])}
        return {"observation": _finite(observation),
                "cursor": {"runtime_epoch": self.runtime_epoch, "sequence": view["head"]}}

    def poll(self, after, *, routes=None, kinds=None, max_events=64, max_bytes=65536, wait_ms=0,
             max_evidence_age_ms=None, on_expired="refuse"):
        args = validate_poll_args(after, self._route_ids, routes=routes, kinds=kinds, max_events=max_events,
                                  max_bytes=max_bytes, wait_ms=wait_ms,
                                  max_evidence_age_ms=max_evidence_age_ms, on_expired=on_expired)
        page = self.broker.poll(args["after"], routes=args["routes"], kinds=args["kinds"],
                                max_events=args["max_events"], max_bytes=args["max_bytes"],
                                wait_ms=args["wait_ms"], max_age_ms=args["max_evidence_age_ms"],
                                on_expired=args["on_expired"])
        return {**page, "health_summary": self.health_summary(),
                "coverage": self._coverage(self.tracker.frames_received)}

    def health_summary(self, now_ns=None):
        now = self._now(now_ns)
        capture = self.tracker.snapshot()
        watch = self.watchdog.snapshot(now)
        worker = self.worker.snapshot()
        capture_state = self._capture_state(capture, watch)
        last = capture["last_block"]
        return {"capture_state": capture_state, "capture_cause": capture["ended_cause"],
                "worker_state": worker["state"], "stream_epoch": capture["stream_epoch"],
                "last_block_age_ms": None if last is None
                else max(0, now - last["received_monotonic_ns"]) // MS_NS,
                "routes": [{k: row[k] for k in ("route_id", "availability", "health", "near_silence")}
                           for row in self._route_rows(capture_state, worker, {}, now)]}

    def terminal_summary(self):
        with self._lock:
            state, stop, stopped = self._state, dict(self._stop) if self._stop else {}, self._stopped_ns
            drain = self._drain
        now = self._now(None)
        capture = self.tracker.snapshot()
        watch = self.watchdog.snapshot(now)
        worker = self.worker.snapshot()
        counters = self.broker.counters()
        with self._totals_lock:
            totals = {rid: dict(values) for rid, values in self._totals.items()}
        stats = self._stats()
        callback = stats.get("callback_ns")
        final = {row["route_id"]: row for row in self._route_rows(self._capture_state(capture, watch), worker,
                                                                   {}, now)}
        routes = []
        for r in self.resolved_routes:
            measured = totals.get(r["route_id"])
            row = final[r["route_id"]]
            failed = worker["state"] == "failed" and r["metering"] == "metered"
            routes.append({"route_id": r["route_id"], "role": r["role"], "metering": r["metering"],
                           "availability": row["availability"],
                           "reason": "measurement_worker_failed" if failed else row["reason"],
                           "availability_at_start": r["availability"],
                           **(measured if measured is not None else
                              {"frames_measured": None, "max_sample_peak_dbfs": None, "clipped_total": None,
                               "nonfinite_total": None, "near_silence_frames": None})})
        return _finite({
            "session_id": self.session_id, "config_sha256": self.config_sha256,
            "runtime_epoch": self.runtime_epoch,
            "ordinal": self.ordinal, "state": state, "cause": stop.get("cause"),
            "stop_request_id": stop.get("request_id"), "started_monotonic_ns": self.started_ns,
            "stopped_monotonic_ns": stopped,
            "capture": {"final_state": capture["state"], "ended_cause": capture["ended_cause"],
                        "refusal": None, "adapter": self.adapter_profile, "device": self._device(),
                        "device_instance": self.identity.instance,
                        "device_instance_epoch": self.device_instance_epoch,
                        "topology_epoch": self.topology_epoch,
                        "streams": capture["streams"], "streams_omitted": capture["streams_omitted"],
                        "missing": capture["missing"], "stale_episodes": watch["stale_episodes"],
                        "adapter_dropped_frames_total": capture["adapter_dropped_frames_total"],
                        "nonfinite_samples": sum(t["nonfinite_total"] for t in totals.values()),
                        "callback_ns": dict(callback) if isinstance(callback, dict) else None,
                        "unwatched_properties": stats.get("unwatched_properties"),
                        "adapter_stats_errors": self._stats_error_count(), "provenance": self.provenance},
            "routes": routes,
            "broker": {"published": counters["head_sequence"], "last_sequence": counters["head_sequence"],
                       "evicted": counters["evicted"], "health_lost_through": counters["health_lost_through"],
                       "publish_hook_errors": counters["publish_hook_errors"],
                       "refused_after_final": counters["refused_after_final"]},
            "worker": {"drain": drain, **{k: worker[k] for k in WORKER_TERMINAL_KEYS}},
            "coverage": self._coverage(capture["frames_received"])})

    def _now(self, now_ns):
        return self._clock.monotonic_ns() if now_ns is None else now_ns

    def _coverage(self, frames_received):
        received = self.audio_source != "device_input" or frames_received > 0
        return coverage(self.audio_source if received else "none")

    def _signal_work(self):
        with self._work:
            self._work_pending = True
            self._work.notify_all()

    def _publish(self, draft, *, final=False):
        if self.audio_source == "device_input" and not self.tracker.frames_received:
            draft = {**draft, "coverage": self._coverage(0)}
        evidence = draft["evidence"]
        kind = evidence.get("type")
        closes_quiet_span = kind == "near_silence" and evidence["state"] in ("ended", "interrupted")
        if kind != "signal_level" and not closes_quiet_span:
            return self.broker.publish(draft, final=final)
        with self._totals_lock:
            sequence = self.broker.publish(draft, final=final)
            if sequence is None:
                return None
            totals = self._totals[draft["route_id"]]
            if kind == "near_silence":
                totals["near_silence_frames"] += draft["interval"]["frames"]
            else:
                totals["frames_measured"] += evidence["frames_measured"]
                peaks = [p for p in evidence["sample_peak_dbfs"] if p is not None]
                if peaks:
                    current = totals["max_sample_peak_dbfs"]
                    totals["max_sample_peak_dbfs"] = max(peaks if current is None else [current, *peaks])
                totals["clipped_total"] += sum(evidence["clipped_count"])
                totals["nonfinite_total"] += sum(evidence["nonfinite_count"])
        return sequence

    def _note_run(self, placement):
        ring_start = self.ring.write_counter - placement.frames
        with self._runs_lock:
            last = self._runs[-1] if self._runs else None
            if (last is not None and last.stream_ordinal == placement.stream_ordinal
                    and last.start_frame + last.frames == placement.start_frame
                    and last.ring_start + last.frames == ring_start):
                last.frames += placement.frames
            else:
                self._runs.append(_RingRun(ring_start, placement.stream_ordinal, placement.start_frame,
                                           placement.frames))

    def _retained(self, capture):
        with self._runs_lock:
            runs = [replace(run) for run in self._runs]
        if not runs or capture["stream_ordinal"] is None:
            return None
        start_pos, _ = self.ring.retained()
        ordinal = runs[-1].stream_ordinal
        current = [r for r in runs if r.stream_ordinal == ordinal and r.ring_start + r.frames > start_pos]
        if not current:
            return None
        first = current[0]
        start = first.start_frame + max(0, start_pos - first.ring_start)
        end = current[-1].start_frame + current[-1].frames
        held = sum(r.frames - max(0, start_pos - r.ring_start) for r in current)
        return {"stream_epoch": stream_epoch(self.runtime_epoch, ordinal), "start_frame": start,
                "frames": end - start, "frames_held": held, "contains_gaps": held < end - start}

    def _stats(self):
        if self._adapter_stats is None:
            return {}
        stats = contained(self._adapter_stats, on_error=lambda error: None)
        if not isinstance(stats, dict):
            with self._stats_lock:
                self._stats_errors += 1
                self._stats_failed = True
            return {}
        with self._stats_lock:
            self._stats_failed = False
        return stats

    def _stats_unreadable(self):
        with self._stats_lock:
            return self._adapter_stats is not None and self._stats_failed

    def _stats_error_count(self):
        with self._stats_lock:
            return self._stats_errors

    def _adapter_report(self, now):
        if self._adapter_stats is None:
            return None
        stats = self._stats()
        report = {k: stats[k] for k in _ADAPTER_STAT_KEYS if k in stats}
        heartbeat = stats.get("last_heartbeat_monotonic_ns")
        report["last_heartbeat_age_ms"] = None if heartbeat is None else max(0, now - heartbeat) // MS_NS
        report["stats_errors"] = self._stats_error_count()
        written, handed = stats.get("helper_blocks_written"), stats.get("blocks")
        if type(written) is int and type(handed) is int:
            report["backlog_blocks_lower_bound"] = max(0, written - handed)
        if isinstance(report.get("callback_ns"), dict):
            report["callback_ns"] = {**report["callback_ns"], "scope": "this_runtime_measured"}
        return _finite(report)

    def _device(self):
        i = self.identity
        return {"uid": i.uid, "name": i.name, "manufacturer": i.manufacturer, "model_uid": i.model_uid,
                "transport": i.transport, "device_instance": i.instance,
                "name_shared_with": self._name_shared_with}

    def _format(self):
        f = self.format
        return {"sample_rate": f.sample_rate, "sample_format": f.sample_format,
                "device_input_channels": f.device_input_channels,
                "selected_channels": list(self.selected_channels),
                "buffer_frame_size": f.buffer_frame_size, "device_latency_frames": f.device_latency_frames,
                "safety_offset_frames": f.safety_offset_frames}

    def _clocks(self, capture):
        last = capture["last_block"] if capture else None
        sample = (False if capture and capture["ended_cause"] == "device_sample_time_unavailable"
                  else ("expected" if self.format.has_sample_time else False) if last is None else True)
        host = (("expected" if self.format.host_time else False) if last is None
                else last["device_host_time_ns"] is not None)
        scope = self.format.host_time_scope
        return clock_report(capture["stream_epoch"] if capture else None, sample_time_available=sample,
                            host_time_available=host, host_time_scope=scope,
                            same_clock=same_host_clock(scope))

    @staticmethod
    def _capture_state(capture, watch):
        state = capture["state"]
        return "stale" if state in ("starting", "running") and watch["stale"] else state

    def _route_rows(self, capture_state, worker, latest, now):
        behind = self._lag_behind_s(worker) is not None
        rows = []
        for r in self.resolved_routes:
            row = {"route_id": r["route_id"], "role": r["role"], "channels": list(r["channels"]),
                   "channels_exposed": r["channels_exposed"], "metering": r["metering"],
                   "semantic_status": r["semantic_status"], "route_calibration_id": None}
            if r["role"] == "cue":
                rows.append({**row, "availability": "unavailable", "isolated_cue": "unavailable",
                             "reason": "routing_test_not_available", "signal": None,
                             "near_silence": "unknown", "health": "unavailable"})
                continue
            if r["metering"] != "metered":
                rows.append({**row, "availability": "unavailable", "reason": r["reason"], "signal": None,
                             "near_silence": "unknown", "health": "unavailable"})
                continue
            measured = worker["routes"][r["route_id"]]
            health = ("ended" if capture_state in _FINAL_STATES else "stale" if capture_state == "stale"
                      else "starting" if capture_state == "starting"
                      else "measurement_failed" if worker["state"] == "failed"
                      else "nonfinite" if measured["nonfinite"] else "ok")
            available = health in ("ok", "nonfinite")
            reason = (None if available else "measurement_worker_failed" if health == "measurement_failed"
                      else f"capture_{capture_state}")
            rows.append({**row, "availability": "available" if available else "unavailable", "reason": reason,
                         "signal": self._signal(latest.get(r["route_id"]), now),
                         "near_silence": measured["near_silence"] if available and not behind else "unknown",
                         "health": health})
        return rows

    @staticmethod
    def _signal(event, now):
        if event is None:
            return None
        return {"sequence": event["sequence"], "stream_epoch": event["stream_epoch"],
                "interval": event["interval"], "evidence": event["evidence"],
                "received_monotonic_ns": event["received_monotonic_ns"],
                "published_monotonic_ns": event["published_monotonic_ns"],
                "valid_until_monotonic_ns": event["valid_until_monotonic_ns"],
                "age_ms": max(0, now - event["received_monotonic_ns"]) // MS_NS,
                "fresh": now < event["valid_until_monotonic_ns"]}

    def _lag_behind_s(self, worker):
        lag = worker["lag_frames"]
        behind = lag * 1000 > self.context.stale_after_ms * self.context.sample_rate
        return lag / self.context.sample_rate if behind else None

    def _summary(self, routes, capture, watch, behind_s=None):
        parts = []
        for row in routes:
            if row["role"] == "cue":
                parts.append(f"{row['route_id']}: not monitored (no routing test)")
            elif row["metering"] != "metered":
                parts.append(f"{row['route_id']}: unavailable (channels not exposed)")
            elif row["health"] in ("ok", "nonfinite"):
                signal = row["signal"] if row["signal"] and row["signal"]["fresh"] else None
                levels = signal["evidence"]["sample_peak_dbfs"] if signal else []
                peaks = [p for p in levels if p is not None]
                level = (f", peak {max(peaks):.1f} dBFS" if peaks else ", digital zero"
                         if signal and all(signal["evidence"]["digital_zero"]) else "")
                measured = "" if behind_s is None else f" in audio measured {behind_s:.1f} s behind capture"
                parts.append(f"{row['route_id']}: " + (f"nonfinite input{measured}" if row["health"] == "nonfinite"
                                                       else "receiving" + level
                                                       + (f", finite{measured}" if measured else "")))
            elif row["health"] == "measurement_failed":
                parts.append(f"{row['route_id']}: receiving, not measured (measurement stopped)")
            elif row["health"] == "stale":
                parts.append(f"{row['route_id']}: stale, no frames for {watch['age_ms'] / 1000:.1f} s")
            elif row["health"] == "starting":
                parts.append(f"{row['route_id']}: waiting for the first block")
            else:
                parts.append(f"{row['route_id']}: capture {capture['state']}"
                             + (f" ({capture['ended_cause']})" if capture["ended_cause"] else ""))
        stream = (f"stream {capture['stream_ordinal']}, {_thousands(capture['missing']['exact_frames'])} "
                  "missing frames" if capture["stream_ordinal"] else "no stream yet")
        last = capture["last_block"]
        age = "no block yet" if last is None else f"last block {watch['age_ms']} ms ago"
        return " · ".join([*parts, stream, age])

    def _notices(self, state, capture_state, capture, watch, worker, routes, journal, now, adapter=None):
        metered = [r for r in routes if r["metering"] == "metered"]
        items = []
        if capture_state == "stale":
            items.append(notice("capture_stale" if capture["last_block"] else "capture_no_frames",
                                routes=_route_names(r["route_id"] for r in metered),
                                age_s=f"{watch['age_ms'] / 1000:.1f}"))
        last_exact = capture["last_exact_missing"]
        missing = capture["missing"]
        if missing["exact_frames"] and last_exact:
            items.append(notice("frames_missing_exact", total=_thousands(missing["exact_frames"]),
                                gaps=_thousands(missing["events"]),
                                frames=_thousands(last_exact["frames"]),
                                start_frame=_thousands(last_exact["start_frame"]),
                                stream_epoch=last_exact["stream_epoch"]))
        if worker["facts_dropped"]:
            items.append(notice("measurement_facts_dropped", count=_thousands(worker["facts_dropped"]),
                                frames=_thousands(worker["unaccounted_frames"])))
        if (capture["stream_ordinal"] or 0) > 1 and capture["streams"]:
            items.append(notice("new_stream_epoch", stream_epoch=capture["stream_epoch"],
                                cause=capture["streams"][-1]["start_cause"]))
        ended = capture["ended_cause"]
        if capture["state"] in ("ended", "failed") and ended:
            code = {"device_lost": "device_lost", "sample_rate_changed": "format_changed",
                    "format_changed": "format_changed", "channel_map_changed": "channel_map_changed"}.get(
                        ended, "capture_ended")
            items.append(notice(code, cause=ended))
        nonfinite = [r["route_id"] for r in metered if r["health"] == "nonfinite"]
        if nonfinite:
            item = notice("nonfinite_input", routes=_route_names(nonfinite))
            behind_s = self._lag_behind_s(worker)
            if behind_s is not None:
                item["text"] += (f" That describes the last audio measured, which trails capture by "
                                 f"{behind_s:.1f} s, so the input may have changed since.")
            items.append(item)
        if worker["state"] == "failed":
            items.append(notice("measurement_worker_failed",
                                frames=_thousands(worker["unmeasured_after_failure_frames"])))
        lagged = worker["unmeasured_frames"] - worker["unmeasured_after_failure_frames"]
        if lagged:
            items.append(notice("measurement_overrun", frames=_thousands(lagged)))
        descriptors = self.feed.descriptors
        behind = (worker["lag_frames"] > self.ring.capacity_frames // 2
                  or len(descriptors) > descriptors.capacity // 2)
        if worker["state"] != "failed" and behind:
            items.append(notice("measurement_lagging",
                                seconds=f"{worker['lag_frames'] / self.context.sample_rate:.1f}"))
        if journal and journal.get("state") not in (None, "writing"):
            items.append(notice("journal_degraded", state=journal["state"]))
        elif journal and journal.get("dropped_lines"):
            items.append(notice("journal_degraded", state="dropping lines"))
        elif journal and self.broker.counters()["publish_hook_errors"]:
            items.append(notice("journal_degraded", state="incomplete"))
        if self._stats_unreadable():
            items.append(notice("adapter_stats_unavailable", count=_thousands(self._stats_error_count())))
        unwatched = (adapter or {}).get("unwatched_properties")
        if unwatched:
            items.append(notice("device_changes_unwatched", properties=", ".join(unwatched)))
        scheduled = watch["scheduled_stop_monotonic_ns"]
        if state == "running" and scheduled is not None:
            lead = min(EXPIRING_LEAD_NS, self._max_runtime_seconds * 1_000_000_000 // 10)
            if scheduled - now <= lead:
                items.append(notice("runtime_expiring", seconds=max(0, scheduled - now) // 1_000_000_000))
        quiet = [r["route_id"] for r in metered if r["near_silence"] == "active"]
        if quiet:
            items.append(notice("near_silence", routes=_route_names(quiet),
                                level_dbfs=f"{self.context.near_silence_level_dbfs:g}",
                                seconds=f"{self.context.near_silence_min_duration_ms / 1000:g}"))
        if any(r["role"] == "master" for r in routes):
            items.append(notice("master_label_untested"))
        cue = next((r for r in routes if r["role"] == "cue"), None)
        if cue is not None:
            items.append(cue_notice(cue["channels"], cue["channels_exposed"]))
        if self.audio_source == "synthetic_script":
            items.append(notice("synthetic_audio"))
        return order_notices(items)
