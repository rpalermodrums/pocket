# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import dataclasses
import io
import json
import math
import random
import threading
import time
from dataclasses import dataclass

import numpy as np
import pytest

from pocket_music import listen, listen_adapters, listen_runtime
from pocket_music.artifact_store import canonical_bytes
from pocket_music.errors import PocketError
from pocket_music.listen import resolve_routes, selected_channels
from pocket_music.listen_adapters import (
    ENDING_EVENTS,
    AdapterBlock,
    AdapterEvent,
    Decoder,
    ProtocolError,
    SyntheticAdapter,
    decode_frames,
    encode_block,
    validate_synthetic_script,
)
from pocket_music.listen_native import (
    BLOCK,
    BLOCK_HEADER,
    EVENT,
    EXIT,
    HEADER,
    HEARTBEAT,
    HEARTBEAT_TYPE,
    HELLO,
    MAGIC,
    STARTED,
    encode_frame,
)
from pocket_music.listen_runtime import (
    EVENT_MAX_BYTES,
    CaptureFeed,
    Descriptor,
    DescriptorRing,
    DroppedFacts,
    EventFactory,
    FactsQueue,
    FakeClock,
    ListenCore,
    MeasurementWorker,
    MeteredRoute,
    PcmRing,
    RuntimeContext,
    StreamTracker,
    SystemClock,
    Watchdog,
    clock_report,
    device_instance_epoch,
    metered_routes,
    same_host_clock,
    stream_epoch,
    topology_epoch,
)

RATE = 48000
UID = "synthetic:duo-a"
RUNTIME = "rt-0123456789abcdef"
MS = 1_000_000
MASTER = ({"route_id": "master", "role": "master", "channels": [0, 1]},)
AUTHORITY_KEYS = {"grant", "grants", "authority", "owner", "ownership", "lease", "permission", "permissions"}


def device(uid=UID, name="Practice Duo", channels=4, rate=RATE):
    return {"uid": uid, "name": name, "input_channels": channels, "sample_rate": rate}


def tone(frames, *tones):
    return {"kind": "tone", "frames": frames,
            "tones": [{"channel": c, "hz": hz, "dbfs": db} for c, hz, db in tones]}


def stereo_tone(frames, dbfs=-12.0, hz=480.0):
    return tone(frames, (0, hz, dbfs), (1, hz, dbfs))


def silence(frames):
    return {"kind": "silence", "frames": frames}


def gap(frames, via="device_gap"):
    return {"kind": "gap", "frames": frames, "via": via}


def stall(ms, counter):
    return {"kind": "stall", "ms": ms, "counter": counter}


def clocks(sample_time_valid, host_time_valid=True):
    return {"kind": "clocks", "sample_time_valid": sample_time_valid, "host_time_valid": host_time_valid}


RESET = {"kind": "counter_reset", "sample_time": 0}


def nonfinite(frames, channels, offsets, value="nan"):
    return {"kind": "nonfinite", "frames": frames, "channels": channels, "offsets": offsets, "value": value}


def script(*segments, devices=None, block=480, **extra):
    return {"block_frames": block, "devices": devices or [device()], "segments": list(segments), **extra}


class Sink:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        canonical_bytes(event)
        self.events.append({**event, "sequence": len(self.events) + 1, "published_monotonic_ns": 0})
        return len(self.events)


@dataclass
class Run:
    clock: FakeClock
    adapter: SyntheticAdapter
    tracker: StreamTracker
    ring: PcmRing
    watchdog: Watchdog
    feed: CaptureFeed
    worker: MeasurementWorker
    sink: Sink

    def events(self, kind=None, route=None):
        return [e for e in self.sink.events if (kind is None or e["evidence"]["type"] == kind)
                and (route is None or e["route_id"] == route)]

    def drive(self, reads=1_000_000, *, measure_every=1, timeout_ns=50 * MS):
        for count in range(1, reads + 1):
            item = self.adapter.read(timeout_ns)
            self.feed.ingest(item)
            self.feed.watchdog_tick(self.clock.monotonic_ns())
            if measure_every and count % measure_every == 0:
                self.worker.run_pending()
            if isinstance(item, AdapterEvent) and item.type in ENDING_EVENTS:
                break
        self.worker.run_pending()
        return self


def build(spec, routes=MASTER, *, uid=UID, rate=RATE, ring_frames=None, window_frames=4800, coalesce=5,
          stale_after_ms=500, clip=-0.1, level=-60.0, min_ms=2000, descriptors=None, facts=None):
    clock = FakeClock()
    adapter = SyntheticAdapter(spec, uid, rate, clock)
    opened = adapter.open()
    assert opened.refusal == ()
    resolved, refusal = resolve_routes(list(routes), opened.format.device_input_channels)
    assert refusal == ()
    selected = selected_channels(resolved)
    assert adapter.start(selected).started
    dev = device_instance_epoch(opened.identity.uid, opened.identity.instance, RUNTIME)
    context = RuntimeContext("session-test", RUNTIME, "synthetic_script", opened.format.sample_rate,
                             metered_routes(resolved), window_frames, clip, level, min_ms, coalesce, stale_after_ms,
                             dev, opened.format.host_time_scope)
    tracker = StreamTracker(RUNTIME, opened.format.sample_rate,
                            topology_epoch=topology_epoch(dev, opened.format, selected, "0" * 64))
    ring = PcmRing(ring_frames or 10 * rate, len(selected))
    watchdog = Watchdog(stale_after_ms, clock.monotonic_ns())
    sink, factory = Sink(), EventFactory(context)
    feed = CaptureFeed(tracker, ring, watchdog, descriptors=descriptors, facts=facts,
                       publish_now=lambda fact: sink(factory.fact(fact)))
    worker = MeasurementWorker(context, feed, sink, clock, factory=factory)
    return Run(clock, adapter, tracker, ring, watchdog, feed, worker, sink)


def run(spec, routes=MASTER, **options):
    return build(spec, routes, **options).drive()


def spans(events):
    return [(e["interval"]["start_frame"], e["interval"]["frames"]) for e in events]


def evidence(event):
    return event["evidence"]


def expected_tone(start, frames, hz, dbfs, rate=RATE):
    n = np.arange(start, start + frames, dtype=np.float64)
    return (10.0 ** (dbfs / 20.0) * np.sin(2.0 * math.pi * hz * n / rate)).astype(np.float32).astype(np.float64)


def dbfs(value):
    return 20.0 * math.log10(value)


def test_tone_peak_and_rms_are_exact_sample_measurements():
    r = run(script(stereo_tone(48000)))
    meters = r.events("signal_level", "master")
    assert spans(meters) == [(0, 24000), (24000, 24000)]
    for event in meters:
        ev = evidence(event)
        assert ev["profile"] == "sample-peak-rms/v1" and ev["channels"] == [0, 1]
        assert ev["windows"] == 5 and ev["window_frames"] == 4800 and ev["frames_measured"] == 24000
        assert ev["complete"] is True and ev["truncated_by"] is None and ev["finite"] is True
        assert all(abs(p + 12.0) < 0.001 for p in ev["sample_peak_dbfs"])
        assert all(abs(x + 15.01) < 0.01 for x in ev["rms_dbfs"])
        assert ev["true_peak"] == "not_measured" and ev["loudness"] == "not_measured"
        assert event["route_role"] == "master" and event["semantic_status"] == "declared_unverified"
        assert event["route_calibration_id"] is None


def test_digital_zero_reports_null_dbfs_and_flag_never_a_floor():
    r = run(script(silence(9600)))
    (meter,) = r.events("signal_level", "master")
    ev = evidence(meter)
    assert ev["sample_peak_dbfs"] == [None, None] and ev["rms_dbfs"] == [None, None]
    assert ev["digital_zero"] == [True, True] and ev["finite"] is True
    text = json.dumps(meter, allow_nan=False)
    assert "Infinity" not in text and "-1000" not in text


def test_clipped_count_uses_the_declared_threshold_inclusively():
    impulses = [{"channel": 0, "offset_frames": 10, "dbfs": 0.0}, {"channel": 0, "offset_frames": 20, "dbfs": -0.2},
                {"channel": 0, "offset_frames": 30, "dbfs": -0.1}, {"channel": 1, "offset_frames": 40, "dbfs": 3.0}]
    r = run(script({"kind": "impulse", "frames": 4800, "impulses": impulses}), clip=-0.1)
    (meter,) = r.events("signal_level", "master")
    ev = evidence(meter)
    assert ev["clipped_count"] == [2, 1] and ev["clip_threshold_dbfs"] == -0.1
    assert abs(ev["sample_peak_dbfs"][0]) < 0.001 and abs(ev["sample_peak_dbfs"][1] - 3.0) < 0.001


def test_nonfinite_samples_null_levels_publish_health_and_keep_json_finite():
    three = [device(channels=3)]
    routes = MASTER + ({"route_id": "aux", "role": "unlabeled", "channels": [2]},)
    all_three = ((0, 480.0, -12.0), (1, 480.0, -12.0), (2, 480.0, -12.0))
    r = run(script(tone(9600, *all_three), nonfinite(4800, [0], [100], "nan"), nonfinite(4800, [1], [200], "inf"),
                   nonfinite(4800, [2], [300], "-inf"), tone(9600, *all_three), devices=three), routes)
    health = r.events("nonfinite_input")
    rows = [(e["evidence"]["affected_routes"], e["evidence"]["detail"]["state"], e["interval"]["start_frame"],
             e["evidence"]["detail"]["counts"]) for e in health]
    assert rows == [(["master"], "started", 9600, [1, 0]), (["master"], "ended", 19200, [0, 0]),
                    (["aux"], "started", 19200, [1]), (["aux"], "ended", 24000, [0])]
    master = [evidence(e) for e in r.events("signal_level", "master")]
    bad = [ev for ev in master if not ev["finite"]]
    assert len(bad) == 1 and bad[0]["nonfinite_count"] == [1, 1]
    assert bad[0]["sample_peak_dbfs"] == [None, None] and bad[0]["rms_dbfs"] == [None, None]
    assert master[0]["truncated_by"] == "nonfinite_transition" and bad[0]["truncated_by"] == "nonfinite_transition"
    last = r.events("signal_level", "master")[-1]
    assert evidence(last)["finite"] and last["interval"]["start_frame"] + last["interval"]["frames"] == 33600
    assert r.tracker.capture_state == "ended" and r.tracker.snapshot()["ended_cause"] == "end_of_stream"


def test_near_silence_needs_the_declared_level_and_duration():
    r = run(script(stereo_tone(48000), silence(144000), stereo_tone(48000)), level=-60.0, min_ms=2000)
    events = r.events("near_silence", "master")
    assert [evidence(e)["state"] for e in events] == ["started", "ended"]
    started, ended = (evidence(e) for e in events)
    assert started["quiet_since_frame"] == 48000 and spans(events) == [(48000, 96000), (48000, 144000)]
    assert started["level_dbfs"] == -60.0 and started["min_duration_ms"] == 2000
    assert started["resolution_frames"] == 4800 and started["profile"] == "near-silence/v1"
    assert started["does_not_establish"] == ["stopped_transport", "dropout", "intended_ending"]
    assert ended["interrupted_by"] is None
    assert not run(script(stereo_tone(48000), stereo_tone(144000, -50.0)), min_ms=2000).events("near_silence")
    soft = run(script(stereo_tone(48000), stereo_tone(144000, -70.0)), min_ms=2000).events("near_silence")
    assert [evidence(e)["state"] for e in soft] == ["started", "interrupted"]


@pytest.mark.parametrize("break_with, cause", [
    (gap(4800), "frames_missing"),
    (nonfinite(4800, [0], [10]), "nonfinite"),
    ({"kind": "counter_reset"}, "stream_ended"),
])
def test_near_silence_is_interrupted_by_missing_frames_nonfinite_and_epoch_end(break_with, cause):
    r = run(script(stereo_tone(48000), silence(144000), break_with, silence(48000)), min_ms=2000)
    events = r.events("near_silence", "master")
    assert [evidence(e)["state"] for e in events] == ["started", "interrupted"]
    assert evidence(events[1])["interrupted_by"] == cause and spans(events)[1] == (48000, 144000)


def test_ramp_survives_ring_wrap_exactly():
    r = build(script({"kind": "ramp", "frames": 48000, "channels": [0, 1], "period_frames": 1000, "dbfs": 0.0}),
              ring_frames=4800, window_frames=1200)
    checked = 0
    while True:
        item = r.adapter.read(50 * MS)
        placement = r.feed.ingest(item)
        if placement is not None:
            n = np.arange(placement.start_frame, placement.start_frame + placement.frames, dtype=np.float64)
            expected = (2.0 * np.mod(n, 1000) / 999 - 1.0).astype(np.float32)
            pcm = r.ring.read(r.ring.write_counter - placement.frames, placement.frames)
            assert np.array_equal(pcm[:, 0], expected) and np.array_equal(pcm[:, 1], expected)
            checked += placement.frames
        r.worker.run_pending()
        if isinstance(item, AdapterEvent):
            break
    assert checked == 48000 and r.ring.write_counter // r.ring.capacity_frames == 10
    meters = r.events("signal_level", "master")
    assert sum(evidence(e)["frames_measured"] for e in meters) == 48000
    assert all(evidence(e)["sample_peak_dbfs"] == [0.0, 0.0] for e in meters)


def test_forward_counter_jump_is_an_exact_missing_interval_in_the_same_epoch():
    r = run(script(stereo_tone(9600), gap(1000), stereo_tone(9600)))
    (missing,) = r.events("frames_missing")
    detail = evidence(missing)["detail"]
    assert evidence(missing)["cause"] == "device_gap" and detail["adapter_dropped_frames"] == 0
    assert detail["missing"] == {"start_frame": 9600, "frames": 1000} and spans([missing]) == [(9600, 1000)]
    assert len(r.events("stream_started")) == 1 and missing["stream_epoch"] == stream_epoch(RUNTIME, 1)
    assert r.tracker.snapshot()["missing"] == {"events": 1, "exact_frames": 1000}
    starts = [s for s, _ in spans(r.events("signal_level", "master"))]
    assert 10600 in starts and not any(9600 <= s < 10600 for s in starts)


@pytest.mark.parametrize("via", ["device_gap", "helper_ring_overflow", "processor_overload"])
def test_helper_ring_overflow_and_processor_overload_attribute_the_gap_cause(via):
    r = run(script(stereo_tone(9600), gap(960, via), stereo_tone(9600)))
    (missing,) = r.events("frames_missing")
    assert evidence(missing)["cause"] == via and spans([missing]) == [(9600, 960)]
    overload = r.events("processor_overload_reported")
    assert len(overload) == (via == "processor_overload")
    if overload:
        assert overload[0]["sequence"] < missing["sequence"]
    assert evidence(missing)["detail"]["adapter_dropped_frames"] == (960 if via == "helper_ring_overflow" else 0)


def test_an_overload_report_explains_the_gap_after_blocks_captured_before_it():
    tracker = StreamTracker(RUNTIME, RATE)
    pcm = np.zeros((480, 2), dtype=np.float32)

    def block(sample_time, host_ns):
        return tracker.observe_block(AdapterBlock(480, pcm, float(sample_time), host_ns, host_ns, 0))

    block(0, 1_000)
    tracker.observe_event(AdapterEvent("processor_overload", {"reported_by": "hal", "host_time_ns": 25_000},
                                       30_000))
    assert block(480, 11_000)[1] == ()
    (missing,) = block(1440, 21_000)[1]
    assert missing.cause == "processor_overload" and missing.interval == (960, 480)
    assert block(1920, 31_000)[1] == ()
    (later,) = block(2880, 41_000)[1]
    assert later.cause == "device_gap"


@pytest.mark.parametrize("segments, cause, previous_end", [
    ((stereo_tone(96000), {"kind": "counter_reset"}, stereo_tone(9600)), "counter_reset", 96000),
    ((stereo_tone(144000), {"kind": "counter_backward", "delta_frames": -48000}, stereo_tone(9600)),
     "counter_backward", 144000),
])
def test_counter_reset_and_backward_jump_start_a_new_stream_epoch_at_frame_zero(segments, cause, previous_end):
    r = run(script(*segments))
    first, second = r.events("stream_started")
    assert evidence(first)["cause"] == "capture_opened" and evidence(second)["cause"] == cause
    assert second["stream_epoch"] == stream_epoch(RUNTIME, 2)
    assert evidence(second)["detail"]["previous_stream"] == {"stream_epoch": stream_epoch(RUNTIME, 1),
                                                             "end_frame": previous_end}
    later = [e for e in r.events("signal_level", "master") if e["stream_epoch"] == stream_epoch(RUNTIME, 2)]
    assert later[0]["interval"]["start_frame"] == 0
    assert later[0]["interval"]["clock_id"] == f"stream:{RUNTIME}.s2"
    assert not r.events("frames_missing")


def test_missing_frames_are_published_before_later_measurements():
    r = run(script(stereo_tone(9600), gap(4800), stereo_tone(9600)), coalesce=1)
    (missing,) = r.events("frames_missing")
    meters = r.events("signal_level", "master")
    assert all(e["sequence"] > missing["sequence"] for e in meters if e["interval"]["start_frame"] >= 14400)
    assert any(e["sequence"] < missing["sequence"] for e in meters)


def test_meter_windows_truncate_at_gaps_and_never_count_missing_frames_as_measured():
    r = run(script(stereo_tone(7000), gap(1000), stereo_tone(9000)), coalesce=1)
    rows = [(e["interval"]["start_frame"], e["interval"]["frames"], evidence(e)["truncated_by"],
             evidence(e)["complete"]) for e in r.events("signal_level", "master")]
    assert rows == [(0, 4800, None, True), (4800, 2200, "frames_missing", False), (8000, 1600, None, False),
                    (9600, 4800, None, True), (14400, 2600, "stream_ended", False)]
    assert all(evidence(e)["frames_measured"] == e["interval"]["frames"] for e in r.events("signal_level"))
    assert not any(s < 8000 and s + n > 7000 for s, n, _, _ in rows)


def test_meter_coalescing_is_exact_per_route_and_leaves_no_sequence_holes():
    routes = MASTER + ({"route_id": "aux", "role": "unlabeled", "channels": [2, 3]},)
    tones = ((0, 480.0, -12.0), (1, 480.0, -12.0), (2, 1000.0, -6.0), (3, 250.0, -20.0))
    r = run(script(tone(72000, *tones)), routes)
    assert [e["sequence"] for e in r.sink.events] == list(range(1, len(r.sink.events) + 1))
    by_channel = {c: (hz, db) for c, hz, db in tones}
    for route_id, channels in (("master", [0, 1]), ("aux", [2, 3])):
        meters = r.events("signal_level", route_id)
        assert spans(meters) == [(0, 24000), (24000, 24000), (48000, 24000)]
        for event in meters:
            start, frames = event["interval"]["start_frame"], event["interval"]["frames"]
            for index, channel in enumerate(channels):
                x = expected_tone(start, frames, *by_channel[channel])
                assert abs(evidence(event)["sample_peak_dbfs"][index] - dbfs(np.max(np.abs(x)))) < 0.001
                assert abs(evidence(event)["rms_dbfs"][index] - dbfs(math.sqrt(np.mean(x * x)))) < 0.001


@pytest.mark.parametrize("cause", ["frames_missing", "stream_ended", "nonfinite_transition", "stop"])
def test_coalesced_meters_close_early_at_gap_epoch_end_nonfinite_transition_and_stop(cause):
    follow = {"frames_missing": gap(480), "stream_ended": {"kind": "counter_reset"},
              "nonfinite_transition": nonfinite(4800, [0], [5]), "stop": stereo_tone(48000)}[cause]
    r = build(script(stereo_tone(9600), follow, stereo_tone(9600)))
    if cause == "stop":
        r.drive(reads=20)
        r.worker.flush("stop")
    else:
        r.drive()
    first = r.events("signal_level", "master")[0]
    assert spans([first]) == [(0, 9600)] and evidence(first)["windows"] == 2
    assert evidence(first)["truncated_by"] == cause and evidence(first)["complete"] is True


@pytest.mark.parametrize("ring_frames", [4800, 5000])
def test_ring_overwrite_reports_measurement_overrun_with_the_exact_unmeasured_interval(ring_frames):
    r = build(script(stereo_tone(48000)), ring_frames=ring_frames, window_frames=1200, coalesce=1)
    r.drive(reads=30, measure_every=0)
    (overrun,) = r.events("measurement_overrun")
    assert evidence(overrun)["detail"] == {"unmeasured": {"start_frame": 0, "frames": 12000}, "exact": True}
    assert spans([overrun]) == [(0, 12000)]
    meters = r.events("signal_level", "master")
    assert meters[0]["interval"]["start_frame"] == 12000 and meters[0]["sequence"] > overrun["sequence"]
    assert sum(evidence(e)["frames_measured"] for e in meters) == 14400 - 12000
    snapshot = r.worker.snapshot()
    assert (snapshot["overruns"], snapshot["unmeasured_frames"], snapshot["lag_frames"]) == (1, 12000, 0)


def test_a_worker_slower_than_capture_skips_ahead_in_spans_of_half_a_ring(monkeypatch):
    r = build(script(stereo_tone(480 * 400)), ring_frames=4800, window_frames=480, coalesce=1)
    original, measured = r.worker._add, []

    def add_while_capture_runs(*args):
        original(*args)
        measured.append(1)
        if len(measured) % 2 == 0:
            for _ in range(3):
                r.feed.ingest(r.adapter.read(50 * MS))

    monkeypatch.setattr(r.worker, "_add", add_while_capture_runs)
    while r.tracker.capture_active:
        r.feed.ingest(r.adapter.read(50 * MS))
        r.worker.run_pending()
    r.worker.run_pending()
    overruns = r.events("measurement_overrun")
    assert len(overruns) >= 3
    assert all(e["interval"]["frames"] >= 4800 // 2 for e in overruns[:-1])
    meters = r.events("signal_level", "master")
    tiles = sorted(spans(meters) + spans(overruns))
    assert [s for s, _ in tiles] == list(np.cumsum([0] + [n for _, n in tiles[:-1]]))
    assert sum(n for _, n in tiles) == 480 * 400


def test_a_worker_slower_than_capture_skips_ahead_when_block_records_are_overwritten_first(monkeypatch):
    r = build(script(stereo_tone(480 * 1200)), window_frames=480, coalesce=1, descriptors=DescriptorRing(64))
    original, measured = r.worker._add, []

    def add_while_capture_runs(*args):
        original(*args)
        measured.append(1)
        if len(measured) % 2 == 0:
            for _ in range(3):
                r.feed.ingest(r.adapter.read(50 * MS))

    monkeypatch.setattr(r.worker, "_add", add_while_capture_runs)
    while r.tracker.capture_active:
        r.feed.ingest(r.adapter.read(50 * MS))
        r.worker.run_pending()
    r.worker.run_pending()
    overruns = r.events("measurement_overrun")
    assert r.feed.descriptors.overwritten and len(overruns) >= 2
    assert all(e["interval"]["frames"] >= 64 // 2 * 480 for e in overruns[:-1])
    assert len(overruns) <= 1200 // (64 // 2) + 1
    meters = r.events("signal_level", "master")
    tiles = sorted(spans(meters) + spans(overruns))
    assert [s for s, _ in tiles] == list(np.cumsum([0] + [n for _, n in tiles[:-1]]))
    assert sum(n for _, n in tiles) == 480 * 1200


def test_overwritten_descriptors_are_reported_as_an_exact_unmeasured_interval():
    r = build(script(stereo_tone(48000)), window_frames=1200, coalesce=1, descriptors=DescriptorRing(8))
    r.drive(reads=30, measure_every=0)
    assert r.feed.descriptors.overwritten == 22
    (overrun,) = r.events("measurement_overrun")
    assert evidence(overrun)["detail"] == {"unmeasured": {"start_frame": 0, "frames": 26 * 480},
                                           "exact": True}
    first = r.events("signal_level", "master")[0]
    assert first["interval"] == {"clock_id": f"stream:{RUNTIME}.s1", "start_frame": 26 * 480, "frames": 720}


def test_measurement_worker_crash_keeps_capture_and_watchdog_health_live(monkeypatch):
    r = build(script(stereo_tone(9600), stall(1000, "pauses"), stereo_tone(9600)), coalesce=1)
    r.drive(reads=12)

    def broken(*args):
        raise RuntimeError("injected measurement fault")

    monkeypatch.setattr(r.worker, "_add", broken)
    r.drive()
    (failed,) = r.events("measurement_worker_failed")
    assert evidence(failed)["detail"]["error_type"] == "RuntimeError" and r.worker.state == "failed"
    assert not [e for e in r.events("signal_level") if e["sequence"] > failed["sequence"]]
    later = [evidence(e)["type"] for e in r.sink.events if e["sequence"] > failed["sequence"]]
    assert {"capture_stale", "capture_resumed", "stream_ended", "capture_ended"} <= set(later)
    assert r.tracker.snapshot()["frames_received"] == 19200
    assert r.worker.snapshot()["unmeasured_frames"] > 0


def test_pcm_ring_and_descriptor_ring_are_preallocated_and_bounded():
    ring = PcmRing(4800, 2)
    storage, size = id(ring._data), ring.nbytes
    for index in range(100):
        ring.write(np.full((480, 2), index, dtype=np.float32))
    assert id(ring._data) == storage and ring.nbytes == size == 4800 * 2 * 4
    assert ring.write_counter == 48000 and ring.retained() == (43200, 4800)
    assert ring.read(43199, 2) is None and ring.read(47520, 480)[0, 0] == 99.0
    assert DescriptorRing().nbytes == listen.DESCRIPTOR_CAPACITY * listen.DESCRIPTOR_BYTES
    descriptors = DescriptorRing(64)
    entries, entry_bytes = id(descriptors._entries), descriptors.nbytes
    for index in range(640):
        descriptors.push(_descriptor(index))
    assert id(descriptors._entries) == entries and descriptors.nbytes == entry_bytes
    assert len(descriptors) == 64 and descriptors.overwritten == 576 and descriptors.peek_index() == 576
    facts = FactsQueue(16)
    for index in range(160):
        facts.push(index, None)
    assert len(facts) == 16 and facts.dropped_total == 144
    assert facts.dropped_before(17) and not facts.dropped_before(16)
    assert [facts.pop() for _ in range(16)] == [None] * 16 and facts.peek_index() == 159
    assert facts.pop() == DroppedFacts(16, 159) and facts.pop() is None and not facts.dropped_before(200)
    facts = FactsQueue(1)
    facts.push(0, None)
    facts.push(1, None)
    assert facts.pop_run_before(1) is None
    assert facts.pop_run_before(2) == DroppedFacts(1, 1) and len(facts) == 1
    facts.push(3, None)
    assert facts.pop_run_before(9) == DroppedFacts(3, 3) and facts.dropped_total == 2


def _descriptor(index):
    return Descriptor(index, 1, index * 480, 480, index * 480, index, index * 480, None, 0)


def test_oversized_block_keeps_only_the_newest_ring_frames():
    ring = PcmRing(1000, 1)
    position = ring.write(np.arange(2500, dtype=np.float32).reshape(-1, 1))
    assert position == 0 and ring.retained() == (1500, 1000)
    assert ring.read(1499, 1) is None and ring.read(1500, 1000)[:, 0].tolist() == list(range(1500, 2500))


def test_watchdog_reports_stale_after_the_declared_age_and_resumes_without_a_false_gap():
    t0 = 5_000_000_000
    watchdog = Watchdog(500, t0)
    assert watchdog.block_received(t0) is None
    assert watchdog.tick(t0 + 499 * MS) == ((), None)
    (stale,), stop = watchdog.tick(t0 + 500 * MS)
    assert stop is None and stale.type == "capture_stale" and stale.state_transition == ("running", "stale")
    assert stale.detail["age_ms"] == 500 and stale.detail["stale_after_ms"] == 500
    assert stale.detail["does_not_establish"] == ["playback_stopped"]
    assert watchdog.tick(t0 + 900 * MS) == ((), None)
    resumed = watchdog.block_received(t0 + 1000 * MS)
    assert resumed.type == "capture_resumed" and resumed.detail["stale_for_ms"] == 500
    assert watchdog.snapshot(t0 + 1100 * MS)["stale"] is False
    r = run(script(stereo_tone(9600), stall(1000, "pauses"), stereo_tone(9600)))
    assert len(r.events("capture_stale")) == 1 and len(r.events("capture_resumed")) == 1
    assert not r.events("frames_missing") and len(r.events("stream_started")) == 1


def test_watchdog_runtime_bound_and_helper_heartbeat():
    t0 = 1_000_000_000
    watchdog = Watchdog(500, t0, max_runtime_seconds=100)

    def tick(at_ms, heartbeat_ms=None):
        watchdog.block_received(t0 + at_ms * MS)
        return watchdog.tick(t0 + at_ms * MS, heartbeat_ns=None if heartbeat_ms is None else t0 + heartbeat_ms * MS)

    assert tick(89_000, 89_000) == ((), None)
    facts, stop = tick(90_000, 90_000)
    assert [f.type for f in facts] == ["runtime_expiring"] and stop is None
    assert facts[0].detail == {"seconds_remaining": 10, "scheduled_stop_monotonic_ns": t0 + 100_000 * MS}
    facts, _ = tick(92_001, 90_000)
    assert [f.type for f in facts] == ["helper_unresponsive"] and facts[0].detail["age_ms"] == 2001
    assert facts[0].detail["reader_backpressured"] is None
    assert tick(100_000) == ((), "max_runtime_reached")
    assert tick(100_400) == ((), None)
    fresh = Watchdog(500, t0)
    (stale,), _ = fresh.tick(t0 + 500 * MS)
    assert stale.detail["last_block_received_monotonic_ns"] is None and stale.state_transition[0] == "starting"
    assert Watchdog(500, t0).tick(t0 + 900 * MS, capture_active=False) == ((), None)


@pytest.mark.parametrize("counter", ["advances", "pauses"])
def test_stall_resume_is_an_exact_gap_or_contiguous_by_counter(counter):
    r = run(script(stereo_tone(9600), stall(1000, counter), stereo_tone(9600)))
    (stale,), (resumed,) = r.events("capture_stale"), r.events("capture_resumed")
    assert stale["sequence"] < resumed["sequence"] and len(r.events("stream_started")) == 1
    missing = r.events("frames_missing")
    if counter == "advances":
        (gap_event,) = missing
        assert evidence(gap_event)["cause"] == "device_gap" and spans(missing) == [(9600, 48000)]
        assert resumed["sequence"] < gap_event["sequence"]
        assert r.tracker.snapshot()["missing"] == {"events": 1, "exact_frames": 48000}
        assert r.tracker.snapshot()["last_block"]["stream_frame_end"] == 9600 + 48000 + 9600
    else:
        assert not missing and r.tracker.snapshot()["missing"] == {"events": 0, "exact_frames": 0}
        assert r.tracker.snapshot()["last_block"]["stream_frame_end"] == 19200


def test_a_helper_ring_overflow_longer_than_stale_after_is_an_exact_gap_with_its_cause():
    r = build_core(script(stereo_tone(9600), gap(48000, "helper_ring_overflow"), stereo_tone(9600)),
                   stale_after_ms=500)
    r.drive()
    (missing,) = r.events("frames_missing")
    assert evidence(missing)["cause"] == "helper_ring_overflow" and spans([missing]) == [(9600, 48000)]
    assert evidence(missing)["detail"] == {"missing": {"start_frame": 9600, "frames": 48000},
                                           "adapter_dropped_frames": 48000}
    assert len(r.events("capture_stale")) == len(r.events("capture_resumed")) == 1
    assert len(r.events("stream_started")) == 1
    observation = r.observe()
    assert observation["capture"]["missing"] == {"events": 1, "exact_frames": 48000}
    assert "stream 1, 48,000 missing frames" in observation["summary"]
    (exact,) = [n for n in observation["notices"] if n["code"] == "frames_missing_exact"]
    assert "missing 48,000 frames in 1 exact gaps" in exact["text"]


@pytest.mark.parametrize("segments, stream", [
    ((gap(480, "helper_ring_overflow"), stereo_tone(9600)), 1),
    ((stereo_tone(9600), gap(480, "helper_ring_overflow"), RESET, stereo_tone(9600)), 2),
])
def test_frames_the_helper_dropped_before_a_stream_began_are_counted_on_its_stream_started(segments, stream):
    r = run(script(*segments))
    assert not r.events("frames_missing")
    starts = r.events("stream_started")
    assert [evidence(e)["detail"]["adapter_dropped_frames"] for e in starts] == [0] * (stream - 1) + [480]
    assert starts[-1]["stream_epoch"] == stream_epoch(RUNTIME, stream)
    snapshot = r.tracker.snapshot()
    assert snapshot["missing"] == {"events": 0, "exact_frames": 0}
    assert snapshot["adapter_dropped_frames_total"] == 480


def test_a_block_without_a_device_sample_time_ends_the_stream_and_capture_and_nothing_measured_follows():
    r = build_core(script(stereo_tone(9600), clocks(False), stereo_tone(9600)), coalesce=1)
    r.drive()
    (ended,) = r.events("stream_ended")
    (capture,) = r.events("capture_ended")
    assert evidence(ended)["cause"] == evidence(capture)["cause"] == "device_sample_time_unavailable"
    assert evidence(capture)["state_transition"] == {"from": "running", "to": "ended"}
    detail = evidence(ended)["detail"]
    assert detail == {"sample_time": "missing", "block_frames": 480, "adapter_dropped_frames": 0,
                      "end_frame": 9600, "frames_received": 9600}
    measured = [e for e in r.published if e["delivery_class"] == "normal"]
    assert measured and {e["stream_epoch"] for e in measured} == {stream_epoch(RUNTIME, 1)}
    assert max(e["interval"]["start_frame"] + e["interval"]["frames"] for e in measured) == 9600
    assert not r.events("frames_missing") and len(r.events("stream_started")) == 1
    snapshot = r.core.tracker.snapshot()
    assert snapshot["frames_received"] == 9600 and snapshot["blocks_after_end"] == 19
    observation = r.observe()
    capture_block = observation["capture"]
    assert capture_block["state"] == "ended"
    assert capture_block["ended_cause"] == "device_sample_time_unavailable"
    assert capture_block["clocks"]["device_sample_time"] == "unavailable"
    (ended_notice,) = [n for n in observation["notices"] if n["code"] == "capture_ended"]
    assert "device_sample_time_unavailable" in ended_notice["text"]
    assert ended_notice["next_step"].startswith("Stop this runtime with listen_stop and its runtime_epoch")


@pytest.mark.parametrize("sample_time, reason", [(None, "missing"), (math.nan, "nonfinite"),
                                                 (math.inf, "nonfinite"), (480.5, "fractional"),
                                                 (2.0 ** 63, "out_of_range"), (1e19, "out_of_range"),
                                                 (1e300, "out_of_range"), (-2.0 ** 64, "out_of_range")])
def test_a_block_without_a_usable_sample_time_is_never_placed_and_ends_capture(sample_time, reason):
    tracker = StreamTracker(RUNTIME, RATE)
    pcm = np.zeros((480, 2), dtype=np.float32)
    placement, facts = tracker.observe_block(AdapterBlock(480, pcm, 0.0, None, 1, 0))
    assert placement.start_frame == 0 and [f.cause for f in facts] == ["capture_opened"]
    placement, facts = tracker.observe_block(AdapterBlock(480, pcm, sample_time, None, 2, 240))
    assert placement is None
    assert [(f.type, f.cause) for f in facts] == [("stream_ended", "device_sample_time_unavailable"),
                                                  ("capture_ended", "device_sample_time_unavailable")]
    assert facts[0].detail == {"sample_time": reason, "block_frames": 480, "adapter_dropped_frames": 240,
                               "end_frame": 480, "frames_received": 480}
    assert facts[1].state_transition == ("running", "ended")
    assert tracker.observe_block(AdapterBlock(480, pcm, 960.0, None, 3, 240)) == (None, ())
    snapshot = tracker.snapshot()
    assert (snapshot["state"], snapshot["frames_received"], snapshot["blocks_after_end"]) == ("ended", 480, 1)
    first = StreamTracker(RUNTIME, RATE)
    placement, (only,) = first.observe_block(AdapterBlock(480, pcm, sample_time, None, 1, 0))
    assert placement is None and only.type == "capture_ended"
    assert only.state_transition == ("starting", "ended")
    assert first.snapshot()["stream_ordinal"] is None and first.snapshot()["streams"] == []


@pytest.mark.parametrize("first", [True, False])
@pytest.mark.parametrize("sample_time", [2.0 ** 63, 1e19, 1e300])
def test_a_whole_sample_time_too_large_for_the_descriptor_ring_ends_capture_before_any_fact_of_its_block(
        first, sample_time):
    r = build(script(stereo_tone(4800)), window_frames=480, coalesce=1)
    start = r.clock.monotonic_ns()
    pcm = np.zeros((480, 2), dtype=np.float32)
    if not first:
        r.feed.ingest(AdapterBlock(480, pcm, 0.0, None, start, 0, None))
    r.feed.ingest(AdapterBlock(480, pcm, sample_time, None, start + 10_000_000, 0, None))
    r.worker.run_pending()
    facts = [evidence(e)["type"] for e in r.sink.events if e["delivery_class"] == "health"]
    assert facts == (["capture_ended"] if first else ["stream_started", "stream_ended", "capture_ended"])
    assert spans(r.events("signal_level")) == ([] if first else [(0, 480)])
    assert {evidence(e)["cause"] for e in r.events("capture_ended") + r.events("stream_ended")} == {
        "device_sample_time_unavailable"}
    assert evidence(r.events("capture_ended")[0])["detail"]["sample_time"] == "out_of_range"
    assert len(r.feed.descriptors) == 0 and r.tracker.snapshot()["frames_received"] == (0 if first else 480)


def test_device_clock_and_host_clock_ids_are_never_compared():
    r = run(script(stereo_tone(9600), gap(480), clocks(True, False), stereo_tone(9600)))
    measured = [e for e in r.sink.events if e["delivery_class"] == "normal"]
    assert measured
    for event in r.sink.events:
        if event["interval"] is not None:
            assert event["interval"]["clock_id"] == f"stream:{event['stream_epoch']}"
        clock = event["device_clock"]
        if clock is not None:
            assert set(clock) == {"clock_id", "sample_time_start", "host_time_ns", "host_time_frame",
                                  "host_time_sample_time", "host_time_scope"}
            assert clock["clock_id"].startswith("device-host:dev-") and clock["host_time_scope"] == "synthetic_clock_ns"
        assert not [k for k in _keys(event) if any(w in k for w in ("latency", "drift", "skew", "delta"))]
    late = [e for e in measured if e["interval"]["start_frame"] >= 10080]
    assert late and all(e["device_clock"]["host_time_ns"] is None for e in late)
    assert all(e["device_clock"]["sample_time_start"] == float(e["interval"]["start_frame"]) for e in measured)


def test_a_window_inside_a_block_pairs_its_host_time_with_that_block_s_frame():
    r = run(script(stereo_tone(48000), block=4096), coalesce=1)
    clocks = [(e["interval"]["start_frame"], e["device_clock"]) for e in r.events("signal_level", "master")]
    assert any(clock["host_time_frame"] != start for start, clock in clocks)
    for start, clock in clocks:
        assert clock["sample_time_start"] - clock["host_time_sample_time"] == start - clock["host_time_frame"]
        assert clock["host_time_frame"] <= start < clock["host_time_frame"] + 4096


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def test_host_drift_is_reported_never_subtracted_into_latency_or_drift():
    clock = FakeClock()
    adapter = SyntheticAdapter(script(stereo_tone(48000), host_drift_ppm=50.0), UID, RATE, clock)
    adapter.open()
    adapter.start((0, 1))
    origin = clock.monotonic_ns()
    blocks = []
    while not isinstance(item := adapter.read(50 * MS), AdapterEvent):
        if item is not None:
            blocks.append(item)
    last = blocks[-1]
    drifted = 1.0 + 50.0 * 1e-6
    assert last.device_sample_time == 47520.0
    assert last.device_host_time_ns == origin + round(47520 * 1e9 * drifted / RATE)
    assert last.device_host_time_ns - (origin + round(47520 * 1e9 / RATE)) == 49500
    assert last.received_monotonic_ns == origin + round(48000 * 1e9 * drifted / RATE)
    report = clock_report(stream_epoch(RUNTIME, 1), sample_time_available=True, host_time_available=True,
                          host_time_scope="synthetic_clock_ns", same_clock=same_host_clock("synthetic_clock_ns"))
    assert report["drift"] == "not_estimated" and report["domains_compared"] is False
    assert report["device_latency"] == "not_measured" and report["same_host_clock"] is True
    assert same_host_clock("unknown_scope") is False
    r = run(script(stereo_tone(48000), host_drift_ppm=50.0))
    assert not [k for e in r.sink.events for k in _keys(e) if "drift" in k or "latency" in k]


@pytest.mark.parametrize("ending, cause", [
    ({"kind": "rate_change", "sample_rate": 44100}, "sample_rate_changed"),
    ({"kind": "channel_reorder", "order": [1, 0, 3, 2]}, "channel_map_changed"),
    ({"kind": "hogged"}, "device_hogged"),
])
def test_format_rate_or_channel_map_change_ends_the_stream_and_never_relabels(ending, cause):
    r = run(script(stereo_tone(9600), ending))
    (ended,) = r.events("stream_ended")
    (capture,) = r.events("capture_ended")
    assert evidence(ended)["cause"] == evidence(capture)["cause"] == cause
    assert evidence(capture)["state_transition"] == {"from": "running", "to": "ended"}
    meters = r.events("signal_level")
    assert {e["stream_epoch"] for e in meters} == {stream_epoch(RUNTIME, 1)}
    assert max(e["interval"]["start_frame"] + e["interval"]["frames"] for e in meters) == 9600
    assert evidence(ended)["detail"]["end_frame"] == 9600
    assert evidence(meters[-1])["truncated_by"] == "stream_ended"
    assert r.tracker.capture_state == "ended"
    block = AdapterBlock(480, np.zeros((480, 2), dtype=np.float32), 9600.0, None, r.clock.monotonic_ns(), 0)
    assert r.feed.ingest(block) is None and r.tracker.snapshot()["blocks_after_end"] == 1
    assert r.adapter.read(50 * MS) is None and len(r.events("stream_started")) == 1


def test_device_loss_ends_capture_without_reconnect_and_the_latest_meter_ages():
    r = run(script(stereo_tone(9600), {"kind": "device_lost"}))
    (capture,) = r.events("capture_ended")
    assert evidence(capture)["cause"] == "device_lost"
    latest = r.worker.snapshot()["routes"]["master"]["latest_signal"]
    for _ in range(100):
        assert r.adapter.read(50 * MS) is None
        r.feed.watchdog_tick(r.clock.monotonic_ns())
        r.worker.run_pending()
    assert r.worker.snapshot()["routes"]["master"]["latest_signal"] == latest
    assert r.clock.monotonic_ns() - latest["received_monotonic_ns"] >= 5_000 * MS
    assert not r.events("capture_stale") and len(r.events("stream_started")) == 1
    assert r.tracker.snapshot()["state"] == "ended"


def test_end_of_script_ends_capture_and_snapshots_keep_serving():
    r = run(script(stereo_tone(4800)))
    (ended,) = r.events("stream_ended")
    assert evidence(ended)["cause"] == "end_of_stream" and evidence(ended)["detail"]["end_frame"] == 4800
    snapshot = r.tracker.snapshot()
    assert snapshot["state"] == "ended" and snapshot["ended_cause"] == "end_of_stream"
    assert snapshot["streams"] == [{"stream_epoch": stream_epoch(RUNTIME, 1), "start_cause": "capture_opened",
                                    "frames_received": 4800, "missing_exact_frames": 0,
                                    "end_cause": "end_of_stream"}]
    assert r.worker.snapshot()["state"] == "running" and r.watchdog.snapshot(r.clock.monotonic_ns())


def test_duplicate_display_names_select_by_uid_and_report_the_shared_name():
    devices = [device("synthetic:duo-a", "Practice Duo", 4), device("synthetic:duo-b", "Practice Duo", 2)]
    adapter = SyntheticAdapter(script(stereo_tone(4800), devices=devices), "synthetic:duo-b", RATE, FakeClock())
    opened = adapter.open()
    assert opened.identity.uid == "synthetic:duo-b" and opened.identity.name == "Practice Duo"
    assert opened.identity.instance == "synthetic-instance-2" and opened.format.device_input_channels == 2
    assert [(row["uid"], row["display_name_shared"], row["name_shared_with"]) for row in opened.inventory] == [
        ("synthetic:duo-a", True, 1), ("synthetic:duo-b", True, 1)]
    missing = SyntheticAdapter(script(stereo_tone(4800)), "synthetic:other", RATE, FakeClock()).open()
    assert missing.refusal == ("device_not_found",) and missing.identity is None
    r = run(script(stereo_tone(4800), devices=devices), uid="synthetic:duo-b")
    assert r.events("signal_level", "master")
    core = build_core(script(stereo_tone(4800), devices=devices), uid="synthetic:duo-b").core
    assert core.observe()["observation"]["capture"]["device"]["name_shared_with"] == 1


def test_name_sharing_is_null_when_the_adapter_listed_no_other_inputs():
    clock = FakeClock()
    adapter = SyntheticAdapter(script(stereo_tone(4800)), UID, RATE, clock)
    opened = dataclasses.replace(adapter.open(), inventory=())
    core = ListenCore(core_config(), opened, session_id="s", runtime_epoch=RUNTIME, audio_source="device_input",
                      adapter_profile="coreaudio-hal-input/v1", config_sha256="0" * 64, clock=clock)
    core.begin()
    assert core.observe()["observation"]["capture"]["device"]["name_shared_with"] is None
    assert core.terminal_summary()["capture"]["device"]["name_shared_with"] is None


def test_a_device_runtime_says_device_input_only_once_a_block_has_arrived():
    clock = FakeClock()
    adapter = SyntheticAdapter(script(stereo_tone(4800)), UID, RATE, clock)
    opened = adapter.open()
    published = []
    core = ListenCore(core_config(), opened, session_id="s", runtime_epoch=RUNTIME, audio_source="device_input",
                      adapter_profile="coreaudio-hal-input/v1", config_sha256="0" * 64, clock=clock,
                      on_publish=published.append)
    assert adapter.start(core.selected_channels).started
    core.begin()
    assert core.observe()["observation"]["coverage"]["audio_source"] == "none"
    assert core.poll({"runtime_epoch": RUNTIME, "sequence": 0})["coverage"]["audio_source"] == "none"
    assert core.terminal_summary()["coverage"]["audio_source"] == "none"
    assert [(e["evidence"]["type"], e["coverage"]["audio_source"]) for e in published] == [
        ("runtime_started", "none"), ("capture_opened", "none")]
    core.ingest(adapter.read(50 * MS))
    core.measure_pending()
    assert core.observe()["observation"]["coverage"]["audio_source"] == "device_input"
    assert core.terminal_summary()["coverage"]["audio_source"] == "device_input"
    assert published[-1]["evidence"]["type"] == "stream_started"
    assert published[-1]["coverage"]["audio_source"] == "device_input"


def test_cue_route_is_declared_unmetered_and_reports_channels_exposed_separately():
    routes = MASTER + ({"route_id": "cue", "role": "cue", "channels": [2, 3]},)
    four, _ = resolve_routes(list(routes), 4)
    two, _ = resolve_routes(list(routes), 2)
    assert [(r["route_id"], r["channels_exposed"], r["metering"]) for r in four] == [
        ("master", True, "metered"), ("cue", True, "withheld_until_routing_test")]
    assert two[1]["channels_exposed"] is False and two[1]["availability"] == "unavailable"
    assert selected_channels(four) == selected_channels(two) == (0, 1)
    assert [r.route_id for r in metered_routes(four)] == ["master"]
    r = run(script(tone(48000, (0, 480.0, -12.0), (2, 480.0, -6.0), (3, 480.0, -6.0))), routes)
    assert r.sink.events and not [e for e in r.sink.events if e["route_role"] == "cue" or e["route_id"] == "cue"]
    assert all("cue" not in e["evidence"]["affected_routes"] for e in r.events() if e["delivery_class"] == "health")
    with pytest.raises(ValueError, match="cue"):
        MeteredRoute("cue", "cue", (2,), (0,))


def test_blocked_adapter_reports_stale_never_the_last_success():
    r = build(script(stereo_tone(9600), stall(3000, "pauses"), stereo_tone(4800)), coalesce=1)
    r.drive(reads=30)
    latest = r.worker.snapshot()["routes"]["master"]["latest_signal"]
    ages = []
    for _ in range(30):
        assert r.adapter.read(50 * MS) is None
        r.feed.watchdog_tick(r.clock.monotonic_ns())
        r.worker.run_pending()
        ages.append(r.watchdog.snapshot(r.clock.monotonic_ns())["age_ms"])
    assert r.watchdog.snapshot(r.clock.monotonic_ns())["stale"] is True and len(r.events("capture_stale")) == 1
    assert ages == sorted(ages) and ages[-1] > ages[0] >= 500
    assert r.worker.snapshot()["routes"]["master"]["latest_signal"] == latest
    assert r.clock.monotonic_ns() > latest["received_monotonic_ns"] + 1_500 * MS


ALTERNATING_TONES_AND_GAPS = script(*[stereo_tone(480) if i % 2 == 0 else gap(48) for i in range(40)])


def test_a_full_facts_queue_loses_measurement_accounting_never_health():
    r = build(ALTERNATING_TONES_AND_GAPS, facts=FactsQueue(4))
    r.drive(measure_every=0)
    assert r.feed.facts.dropped_total == r.worker.snapshot()["facts_dropped"] == 17
    published = r.events("frames_missing")
    assert len(published) == r.tracker.snapshot()["missing"]["events"] == 19
    assert spans(published) == [(480 + i * 528, 48) for i in range(19)]
    assert len(r.events("stream_ended")) == len(r.events("capture_ended")) == 1
    assert not r.events("health_facts_dropped")
    assert not r.events("measurement_overrun") and r.worker.snapshot()["unmeasured_frames"] == 0
    unaccounted = r.events("frames_unaccounted")
    assert spans(unaccounted) == spans(published[3:])
    assert {evidence(e)["cause"] for e in unaccounted} == {"facts_queue_full"}
    assert evidence(unaccounted[0])["detail"]["may_be"] == ["frames_missing", "measurement_overrun"]
    assert r.worker.snapshot()["unaccounted_frames"] == 16 * 48
    r.worker.flush("stop")
    meters = r.events("signal_level")
    assert sum(evidence(e)["frames_measured"] for e in meters) == 20 * 480
    gaps = spans(published)
    assert not any(s < gs + gn and gs < s + n for s, n in spans(meters) for gs, gn in gaps)


def test_streams_whose_facts_and_blocks_were_both_lost_are_still_accounted_for():
    r = build(script(stereo_tone(9600), RESET, stereo_tone(4800), RESET, stereo_tone(9600)),
              descriptors=DescriptorRing(4), facts=FactsQueue(1))
    r.drive(measure_every=0)
    r.worker.flush("stop")
    snapshot, received = r.worker.snapshot(), r.tracker.snapshot()["frames_received"]
    assert received == 24000 and snapshot["facts_dropped"] == 3 and snapshot["lag_frames"] == 0
    measured = sum(evidence(e)["frames_measured"] for e in r.events("signal_level"))
    assert measured + snapshot["unmeasured_frames"] + snapshot["unaccounted_frames"] == received
    unaccounted = r.events("frames_unaccounted")
    assert [(e["stream_epoch"][-2:], e["interval"]["start_frame"], e["interval"]["frames"]) for e in unaccounted] \
        == [("s1", 0, 9600), ("s2", 0, 4800), ("s3", 0, 7680)]
    assert {evidence(e)["cause"] for e in unaccounted} == {"facts_queue_full"}
    assert r.feed.take_stream_ends(10 ** 6) == ({3: 9600}, 0)


def test_past_its_bound_capture_says_a_lost_streams_extent_is_unknown(monkeypatch):
    monkeypatch.setattr(listen_runtime, "STREAM_END_RECORDS", 1)
    r = build(script(stereo_tone(9600), RESET, stereo_tone(4800), RESET, stereo_tone(9600)),
              descriptors=DescriptorRing(4), facts=FactsQueue(1))
    r.drive(measure_every=0)
    r.worker.flush("stop")
    unaccounted = r.events("frames_unaccounted")
    assert [(e["stream_epoch"][-2:], e["interval"]) for e in unaccounted][:1] == [("s1", None)]
    assert evidence(unaccounted[0])["detail"]["extent"] == "unknown"
    assert [e["stream_epoch"][-2:] for e in unaccounted] == ["s1", "s2", "s3"]


def _ingest_blocks(r, count):
    taken = 0
    while taken < count:
        item = r.adapter.read(50 * MS)
        r.feed.ingest(item)
        taken += 1 if isinstance(item, AdapterBlock) else 0


def _take_through_first_fact(r):
    while True:
        item = r.feed.take(r.feed.committed)
        r.worker._take(item)
        if isinstance(item, listen_runtime.Fact):
            return


def _drain_capture(r):
    while True:
        item = r.adapter.read(50 * MS)
        r.feed.ingest(item)
        if isinstance(item, AdapterEvent) and item.type in ENDING_EVENTS:
            return


def _unaccounted_rows(r):
    return [(e["stream_epoch"][-2:], e["interval"]["start_frame"], e["interval"]["frames"])
            for e in r.events("frames_unaccounted")]


def test_a_queued_stream_start_for_a_later_stream_accounts_for_the_stream_measurement_never_saw():
    r = build(script(stereo_tone(960), gap(48), stereo_tone(1440), RESET, stereo_tone(1920), RESET,
                     stereo_tone(1920)), descriptors=DescriptorRing(4), facts=FactsQueue(1))
    _ingest_blocks(r, 2)
    r.worker.run_pending()
    _ingest_blocks(r, 3 + 4)
    _take_through_first_fact(r)
    _ingest_blocks(r, 4)
    _drain_capture(r)
    r.worker.run_pending()
    r.worker.flush("stop")
    snapshot, received = r.worker.snapshot(), r.tracker.snapshot()["frames_received"]
    measured = sum(evidence(e)["frames_measured"] for e in r.events("signal_level"))
    assert received == 6240
    assert measured + snapshot["unmeasured_frames"] + snapshot["unaccounted_frames"] == received
    assert _unaccounted_rows(r) == [("s1", 1008, 1440), ("s2", 0, 1920)]
    assert {evidence(e)["cause"] for e in r.events("frames_unaccounted")} == {"facts_queue_full"}
    assert r.feed.take_stream_ends(10 ** 6) == ({3: 1920}, 0)


def test_a_queued_gap_on_a_later_stream_is_applied_on_that_stream():
    r = build(script(stereo_tone(960), gap(48), stereo_tone(1440), RESET, stereo_tone(1920), gap(48),
                     stereo_tone(1920)), descriptors=DescriptorRing(4), facts=FactsQueue(1))
    _ingest_blocks(r, 2)
    r.worker.run_pending()
    _ingest_blocks(r, 3 + 4)
    _take_through_first_fact(r)
    _ingest_blocks(r, 4)
    _drain_capture(r)
    r.worker.run_pending()
    r.worker.flush("stop")
    snapshot, received = r.worker.snapshot(), r.tracker.snapshot()["frames_received"]
    measured = sum(evidence(e)["frames_measured"] for e in r.events("signal_level"))
    assert measured + snapshot["unmeasured_frames"] + snapshot["unaccounted_frames"] == received == 6240
    assert _unaccounted_rows(r) == [("s1", 1008, 1440), ("s2", 0, 1920)]
    assert [(e["stream_epoch"][-2:], e["interval"]["start_frame"]) for e in r.events("signal_level")] \
        == [("s1", 0), ("s2", 1968)]
    assert r.feed.take_stream_ends(10 ** 6) == ({2: 3888}, 0)


def test_a_block_with_the_wrong_layout_ends_the_stream_as_a_protocol_error():
    r = build(script(stereo_tone(4800)))
    r.feed.ingest(AdapterBlock(480, np.zeros((480, 3), dtype=np.float32), 0.0, None, 1, 0))
    r.worker.run_pending()
    assert [evidence(e)["type"] for e in r.sink.events] == ["capture_failed"]
    assert evidence(r.sink.events[0])["cause"] == "helper_protocol_error"
    assert r.tracker.capture_state == "failed"


def test_health_detail_is_truncated_to_the_event_cap():
    routes = metered_routes(resolve_routes(list(MASTER), 2)[0])
    context = RuntimeContext("s", RUNTIME, "synthetic_script", RATE, routes, 4800, -0.1, -60.0, 2000, 5, 500,
                             "dev-0000000000000000", "synthetic_clock_ns")
    factory = EventFactory(context)
    event = factory.health("capture_failed", received_ns=1, cause="helper_exited",
                           detail={"stderr_tail": "x" * 20000, "value": math.nan})
    assert evidence(event)["detail"]["detail_truncated"] is True
    assert len(canonical_bytes({**event, "sequence": 2 ** 53, "published_monotonic_ns": 2 ** 62})) <= EVENT_MAX_BYTES
    small = factory.health("capture_stale", received_ns=1, detail={"value": math.inf, "items": (1, 2)})
    assert evidence(small)["detail"] == {"value": None, "items": [1, 2]}


def test_epoch_helpers_are_stable_and_scoped_to_one_runtime():
    a = device_instance_epoch(UID, "synthetic-instance-1", "rt-a")
    assert a == device_instance_epoch(UID, "synthetic-instance-1", "rt-a") and a.startswith("dev-") and len(a) == 20
    assert a != device_instance_epoch(UID, "synthetic-instance-1", "rt-b")
    opened = SyntheticAdapter(script(stereo_tone(480)), UID, RATE, FakeClock()).open()
    topology = topology_epoch(a, opened.format, (0, 1), "f" * 64)
    assert topology.startswith("topo-") and topology != topology_epoch(a, opened.format, (1, 0), "f" * 64)
    assert stream_epoch("rt-a", 3) == "rt-a.s3"


def test_events_never_carry_route_role_cue_authority_fields_or_nonfinite_json():
    for seed in range(50):
        rng = random.Random(seed)
        spec, routes, options, measure_every = _random_case(rng)
        r = build(spec, routes, **options).drive(measure_every=measure_every)
        _check_invariants(r, seed)


def _random_case(rng):
    channels = rng.choice([2, 3, 4])
    block = rng.choice([32, 256, 480, 1000, 4096])
    segments, counter = [], 0
    for _ in range(rng.randint(3, 12)):
        kind = rng.choice(["tone", "ramp", "silence", "impulse", "nonfinite", "gap", "counter_reset",
                           "counter_backward", "stall", "clocks"])
        frames = rng.randint(1, 12000)
        if kind == "tone":
            segment = tone(frames, (rng.randrange(channels), rng.uniform(20.0, 20000.0), rng.uniform(-90.0, 0.0)))
        elif kind == "ramp":
            segment = {"kind": "ramp", "frames": frames, "channels": [0], "period_frames": rng.randint(2, 2000),
                       "dbfs": rng.uniform(-40.0, 3.0)}
        elif kind == "silence":
            segment = silence(frames)
        elif kind == "impulse":
            segment = {"kind": "impulse", "frames": frames, "impulses": [
                {"channel": rng.randrange(channels), "offset_frames": rng.randrange(frames),
                 "dbfs": rng.uniform(-20.0, 12.0)}]}
        elif kind == "nonfinite":
            segment = nonfinite(frames, [rng.randrange(channels)], [rng.randrange(frames)],
                                rng.choice(["nan", "inf", "-inf"]))
        elif kind == "gap":
            segment = gap(rng.randint(1, 5000), rng.choice(["device_gap", "helper_ring_overflow",
                                                            "processor_overload"]))
            frames = segment["frames"]
        elif kind == "counter_reset":
            if counter < 1:
                continue
            segment = {"kind": "counter_reset", "sample_time": rng.randint(0, min(counter, RATE) - 1)}
            counter, frames = segment["sample_time"], 0
        elif kind == "counter_backward":
            if counter < 2:
                continue
            segment = {"kind": "counter_backward", "delta_frames": -rng.randint(1, counter)}
            counter, frames = counter + segment["delta_frames"], 0
        elif kind == "stall":
            segment = stall(rng.randint(50, 900), rng.choice(["advances", "pauses"]))
            frames = segment["ms"] * RATE // 1000 if segment["counter"] == "advances" else 0
        else:
            segment, frames = clocks(rng.random() >= 0.05, rng.random() < 0.8), 0
        counter += frames if kind not in ("counter_reset", "counter_backward") else 0
        segments.append(segment)
    if rng.random() < 0.4:
        segments.append(rng.choice([{"kind": "device_lost"}, {"kind": "hogged"},
                                    {"kind": "rate_change", "sample_rate": 44100},
                                    {"kind": "channel_reorder", "order": list(reversed(range(channels)))}]))
    routes = list(MASTER)
    if channels >= 3:
        routes.append({"route_id": "aux", "role": "unlabeled", "channels": [2]})
    if channels == 4 and rng.random() < 0.5:
        routes.append({"route_id": "cue", "role": "cue", "channels": [3]})
    window = rng.choice([480, 1200, 4800])
    options = {"window_frames": window, "coalesce": rng.randint(1, 6), "min_ms": rng.choice([100, 500, 2000]),
               "ring_frames": rng.choice([4 * window, 10 * RATE]),
               "descriptors": DescriptorRing(rng.choice([8, 4096]))}
    return script(*segments, devices=[device(channels=channels)], block=block), routes, options, rng.choice([1, 3, 50])


def _check_invariants(r, seed):
    events = r.sink.events
    assert r.worker.state == "running", seed
    for event in events:
        assert len(canonical_bytes(event)) <= EVENT_MAX_BYTES, seed
        assert event["route_role"] in (None, "master", "unlabeled") and event["route_id"] != "cue", seed
        assert not AUTHORITY_KEYS & set(_keys(event)), seed
    route_ids = [route.route_id for route in r.worker._ctx.routes]
    streams = {s["stream_epoch"]: s for s in r.tracker.snapshot()["streams"]}
    for epoch, record in streams.items():
        end = record["frames_received"] + record["missing_exact_frames"]
        missing = [(e["interval"]["start_frame"], e["interval"]["frames"], e["sequence"])
                   for e in r.events("frames_missing") if e["stream_epoch"] == epoch]
        unmeasured = [(e["interval"]["start_frame"], e["interval"]["frames"]) for e in r.events("measurement_overrun")
                      if e["stream_epoch"] == epoch]
        for route in route_ids:
            meters = [e for e in r.events("signal_level", route) if e["stream_epoch"] == epoch]
            tiles = sorted([(e["interval"]["start_frame"], e["interval"]["frames"]) for e in meters]
                           + [(s, n) for s, n, _ in missing] + unmeasured)
            position = 0
            for start, frames in tiles:
                assert start == position and frames > 0, (seed, epoch, route, tiles)
                position += frames
            assert position == end, (seed, epoch, route, position, end)
            for start, frames, sequence in missing:
                assert all(e["sequence"] > sequence for e in meters
                           if e["interval"]["start_frame"] >= start + frames), seed
        for event in r.events("near_silence"):
            if event["stream_epoch"] == epoch:
                s, n = event["interval"]["start_frame"], event["interval"]["frames"]
                assert not any(s < ms + mn and ms < s + n for ms, mn, _ in missing), seed
    for route in route_ids:
        states = [evidence(e)["detail"]["state"] for e in r.events("nonfinite_input")
                  if evidence(e)["affected_routes"] == [route]]
        assert states == ["started", "ended"] * (len(states) // 2) + ["started"] * (len(states) % 2), seed


@pytest.mark.parametrize("change, field", [
    ({"block_frames": 16}, "script.block_frames"),
    ({"devices": [device(uid="duo-a")]}, "script.devices[0].uid"),
    ({"devices": [device(rate=22050)]}, "script.devices[0].sample_rate"),
    ({"segments": [{"kind": "tone", "frames": 480, "tones": [{"channel": 0, "hz": 24000.0, "dbfs": -6}]}]},
     "script.segments[0].tones[0].hz"),
    ({"segments": [{"kind": "silence", "frames": 480, "dbfs": -6}]}, "script.segments[0]"),
    ({"segments": [{"kind": "gap", "frames": 480, "via": "network"}]}, "script.segments[0].via"),
    ({"segments": [{"kind": "counter_backward", "delta_frames": -1}]}, "script.segments[0].delta_frames"),
    ({"segments": [silence(4800), {"kind": "counter_reset", "sample_time": 4800}]},
     "script.segments[1].sample_time"),
    ({"segments": [silence(96000), {"kind": "counter_reset", "sample_time": 48000}]},
     "script.segments[1].sample_time"),
    ({"segments": [{"kind": "counter_reset"}]}, "script.segments[0]"),
    ({"segments": [{"kind": "device_lost"}, silence(480)]}, "script.segments[1]"),
    ({"segments": [{"kind": "stall", "ms": 10, "counter": "pauses"}]}, "script.segments[0].ms"),
    ({"segments": [{"kind": "silence", "frames": True}]}, "script.segments[0].frames"),
    ({"host_drift_ppm": math.inf}, "script.host_drift_ppm"),
    ({"host_drift_ppm": 10 ** 400}, "script.host_drift_ppm"),
    ({"segments": [{"kind": "tone", "frames": 480, "tones": [{"channel": 0, "hz": 10 ** 400, "dbfs": -6}]}]},
     "script.segments[0].tones[0].hz"),
    ({"segments": [{"kind": "channel_reorder", "order": [0, "a"]}]}, "script.segments[0].order"),
    ({"segments": [nonfinite(480, [0], [[1]])]}, "script.segments[0].offsets"),
    ({"segments": [nonfinite(480, [[0]], [1])]}, "script.segments[0].channels"),
    ({"segments": [{"kind": "ramp", "frames": 480, "channels": [[0]], "period_frames": 100, "dbfs": 0.0}]},
     "script.segments[0].channels"),
    ({"segments": [{"kind": ["tone"], "frames": 480}]}, "script.segments[0].kind"),
    ({"segments": [{"kind": "gap", "frames": 480, "via": ["device_gap"]}]}, "script.segments[0].via"),
])
def test_synthetic_script_bounds_are_refused_with_a_hint_naming_the_field(change, field):
    with pytest.raises(PocketError) as raised:
        validate_synthetic_script({**script(silence(480)), **change})
    assert raised.value.code == "invalid_request" and raised.value.hint.startswith(field)


def test_rate_mismatch_is_an_open_refusal_not_an_error():
    adapter = SyntheticAdapter(script(silence(480), devices=[device(rate=44100)]), UID, RATE, FakeClock())
    opened = adapter.open()
    assert opened.refusal == ("sample_rate_mismatch",) and opened.format.sample_rate == 44100
    assert opened.detail == {"nominal_sample_rate": 44100, "expected_sample_rate": RATE}


def test_stepped_reads_never_wait_past_their_timeout():
    clock = FakeClock()
    adapter = SyntheticAdapter(script(stereo_tone(480), stall(1000, "pauses"), stereo_tone(480)), UID, RATE, clock)
    adapter.open()
    adapter.start((0, 1))
    start = clock.monotonic_ns()
    assert isinstance(adapter.read(50 * MS), AdapterBlock) and clock.monotonic_ns() - start == 10 * MS
    before = clock.monotonic_ns()
    assert adapter.read(50 * MS) is None and clock.monotonic_ns() - before == 50 * MS
    assert adapter.close(0).clean and adapter.read(50 * MS) is None


def test_realtime_pacing_sleeps_until_blocks_are_due():
    adapter = SyntheticAdapter(script(stereo_tone(1920)), UID, RATE, SystemClock(), pacing="realtime")
    adapter.open()
    adapter.start((0, 1))
    began = time.monotonic()
    items = [adapter.read(200 * MS) for _ in range(5)]
    elapsed = time.monotonic() - began
    assert [type(i).__name__ for i in items] == ["AdapterBlock"] * 4 + ["AdapterEvent"]
    assert elapsed >= 0.035
    with pytest.raises(ValueError, match="stepped"):
        SyntheticAdapter(script(silence(480)), UID, RATE, SystemClock(), pacing="stepped")


def _hello():
    return {"schema": "pocket.listen-helper-hello/v1", "mode": "capture"}


def _started(channels=(0, 1)):
    return {"schema": "pocket.listen-helper-started/v1", "selected_channels": list(channels), "ring_frames": 4096,
            "ring_bytes": 32768, "max_block_frames": 8192, "host_now_ticks": 1}


def _exit():
    return {"schema": "pocket.listen-helper-exit/v1", "reason": "quit"}


def _block(index=0, *, channels=2, frames=480, dropped=(0, 0), ticks=(10, 20), sample=None):
    pcm = np.arange(frames * channels, dtype=np.float32).reshape(frames, channels) if sample is None else sample
    return encode_block(index, float(index * frames), 1000 + index, 3, pcm, dropped[0], dropped[1], *ticks)


def _stream(*frames, serials=None):
    return b"".join(encode_frame(kind, payload, serial if serials is None else serials[serial])
                    for serial, (kind, payload) in enumerate(frames))


def _heartbeat():
    return HEARTBEAT.pack(1, 2, 0, 5, 9)


def test_plh1_frames_round_trip_and_decode_incrementally():
    pcm = np.array([[0.5, -0.5], [math.nan, math.inf]], dtype=np.float32)
    data = _stream((HELLO, _hello()), (STARTED, _started()), (BLOCK, _block(0, frames=2, sample=pcm)),
                   (HEARTBEAT_TYPE, _heartbeat()), (EVENT, {"schema": "pocket.listen-helper-event/v1",
                                                           "type": "processor_overload"}),
                   (BLOCK, _block(1, dropped=(64, 1))), (EXIT, _exit()))
    frames = decode_frames(data)
    assert [kind for kind, _ in frames] == ["hello", "started", "block", "heartbeat", "event", "block", "exit"]
    block = frames[2][1]
    assert block.index == 0 and block.frames == 2 and block.channels == 2 and block.flags == 3
    assert block.pcm.dtype == np.float32 and np.array_equal(block.pcm, pcm, equal_nan=True)
    assert frames[3][1] == (1, 2, 0, 5, 9) and frames[5][1].dropped_frames == 64
    decoder = Decoder()
    pieces = [frame for byte in data for frame in decoder.feed(bytes([byte]))]
    decoder.finish()
    assert [kind for kind, _ in pieces] == [kind for kind, _ in frames] and decoder.exited


def _header(kind, payload, serial=0, *, magic=MAGIC, reserved=0):
    return HEADER.pack(magic, kind, reserved, len(payload), serial) + payload


def _cases():
    hello = encode_frame(HELLO, _hello(), 0)
    started = encode_frame(STARTED, _started(), 1)
    opened = hello + started
    block = _block(0)
    bad_block_reserved = bytearray(block)
    bad_block_reserved[34] = 1
    return {
        "bad_magic": _header(HELLO, b"{}", magic=b"PLH2"),
        "unknown_type": _header(9, b"{}"),
        "reserved_nonzero": _header(HELLO, b"{}", reserved=1),
        "payload_length": opened + _header(HEARTBEAT_TYPE, b"\0" * 39, 2),
        "serial_not_contiguous": hello + encode_frame(STARTED, _started(), 2),
        "hello_not_first": encode_frame(STARTED, _started(), 0),
        "frame_before_started": hello + encode_frame(BLOCK, block, 1),
        "duplicate_hello": hello + encode_frame(HELLO, _hello(), 1),
        "duplicate_started": opened + encode_frame(STARTED, _started(), 2),
        "started_channels": hello + encode_frame(STARTED, _started((0, 0)), 1),
        "started_channels_nested": hello + encode_frame(STARTED, _started(([0],)), 1),
        "block_index_not_contiguous": opened + encode_frame(BLOCK, _block(1), 2),
        "block_channels": opened + encode_frame(BLOCK, _block(0, channels=3), 2),
        "block_length": opened + encode_frame(BLOCK, block + b"\0", 2),
        "block_frames": opened + encode_frame(BLOCK, _block(0, frames=0, sample=np.zeros((0, 2), np.float32)), 2),
        "dropped_total_decreased": opened + encode_frame(BLOCK, _block(0, dropped=(10, 1)), 2)
        + encode_frame(BLOCK, _block(1, dropped=(5, 1)), 3),
        "callback_ticks": opened + encode_frame(BLOCK, _block(0, ticks=(20, 10)), 2),
        "block_reserved": opened + encode_frame(BLOCK, bytes(bad_block_reserved), 2),
        "invalid_json": encode_frame(HELLO, b"{not json", 0),
        "nonfinite_json": encode_frame(HELLO, b'{"schema": "pocket.listen-helper-hello/v1", "x": NaN}', 0),
        "unexpected_schema": encode_frame(HELLO, {"schema": "pocket.listen-helper-exit/v1"}, 0),
        "frame_after_exit": opened + encode_frame(EXIT, _exit(), 2) + encode_frame(HEARTBEAT_TYPE, _heartbeat(), 3),
        "truncated_frame": opened[:-1],
    }


_REASONS = {"block_reserved": "reserved_nonzero", "nonfinite_json": "invalid_json",
            "started_channels_nested": "started_channels"}


@pytest.mark.parametrize("case", sorted(_cases()))
def test_plh1_decoder_rejects_bad_magic_type_length_serial_reserved_order_and_tails(case):
    with pytest.raises(ProtocolError) as raised:
        decode_frames(_cases()[case])
    assert raised.value.reason == _REASONS.get(case, case)


def test_a_decoder_fault_reaches_the_adapter_as_a_protocol_error_never_a_dead_reader(monkeypatch):
    items = []

    class Reader:
        _clock = FakeClock()

        def _put(self, item):
            items.append(item)

        def _protocol_error(self, stream, reason):
            listen_adapters.CoreAudioHelperAdapter._protocol_error(self, stream, reason)

    def faulty(self, chunk):
        raise TypeError("unhashable type: 'list'")

    monkeypatch.setattr(listen_adapters.Decoder, "decode", faulty)
    listen_adapters.CoreAudioHelperAdapter._read_stdout(Reader(), io.BytesIO(b"PLH1" * 8))
    assert [item[:2] for item in items] == [("protocol_error", "decoder_fault")]


@pytest.mark.parametrize("arm", ["protocol_error", "eof"])
def test_a_reader_whose_error_arm_raises_still_reports_a_decoder_fault(arm):
    items, refused = [], []

    class Reader:
        _clock = FakeClock()

        def _put(self, item):
            if item[0] == arm and not refused:
                refused.append(item[:2])
                raise RuntimeError("the reader queue broke")
            items.append(item)

        def _protocol_error(self, stream, reason):
            listen_adapters.CoreAudioHelperAdapter._protocol_error(self, stream, reason)

    class Unreadable(io.RawIOBase):
        def read(self, size=-1):
            raise OSError("helper stdout broke")

    stream = io.BytesIO(b"XXXX" * 8) if arm == "protocol_error" else Unreadable()
    listen_adapters.CoreAudioHelperAdapter._read_stdout(Reader(), stream)
    assert refused == [("protocol_error", "bad_magic") if arm == "protocol_error" else ("eof", None)]
    assert [item[:2] for item in items] == [("protocol_error", "decoder_fault")]


def test_a_property_the_helper_couldnt_read_ends_capture_under_its_own_cause():
    class Adapter:
        _ended = False

        def _signal_child(self):
            raise AssertionError("an unreadable property is a known event, not a protocol error")

    event = listen_adapters.CoreAudioHelperAdapter._event(
        Adapter(), {"type": "device_property_unreadable", "value": {"property": "nominal_sample_rate"}}, 7)
    assert event.type == "device_property_unreadable"
    assert event.detail == {"value": {"property": "nominal_sample_rate"}}
    tracker = StreamTracker(RUNTIME, RATE)
    tracker.observe_block(AdapterBlock(480, np.zeros((480, 2), dtype=np.float32), 0.0, None, 1, 0))
    facts = tracker.observe_event(event)
    assert [(f.type, f.cause) for f in facts] == [("stream_ended", "device_property_unreadable"),
                                                  ("capture_ended", "device_property_unreadable")]
    assert tracker.capture_state == "ended"


def test_plh1_block_limits_follow_the_negotiated_maximum():
    data = _stream((HELLO, _hello()), (STARTED, _started()), (BLOCK, _block(0, frames=480)))
    assert len(decode_frames(data)) == 3
    with pytest.raises(ProtocolError, match="block_frames"):
        decode_frames(data, max_block_frames=256)
    assert BLOCK_HEADER.size == 72 and HEADER.size == 16 and HEARTBEAT.size == 40
    oversized = HEADER.pack(MAGIC, BLOCK, 0, 1024 * 1024 + 1, 2)
    with pytest.raises(ProtocolError, match="payload_length"):
        Decoder().feed(_stream((HELLO, _hello()), (STARTED, _started())) + oversized)


def core_config(routes=MASTER, *, rate=RATE, window_frames=4800, coalesce=5, clip=-0.1, level=-60.0,
                min_ms=2000, ring_seconds=10, stale_after_ms=500, normal_events=8192, normal_bytes=4194304,
                health_events=1024, health_bytes=524288, max_runtime_seconds=14400):
    return {"device": {"uid": UID, "expected_sample_rate": rate}, "routes": [dict(r) for r in routes],
            "measurement": {"clip_threshold_dbfs": clip, "window_frames": window_frames,
                            "near_silence": {"level_dbfs": level, "min_duration_ms": min_ms}},
            "buffering": {"ring_seconds": ring_seconds}, "health": {"stale_after_ms": stale_after_ms},
            "delivery": {"meter_coalesce_windows": coalesce, "normal_retention_events": normal_events,
                         "normal_retention_bytes": normal_bytes, "health_retention_events": health_events,
                         "health_retention_bytes": health_bytes},
            "limits": {"max_runtime_seconds": max_runtime_seconds}}


@dataclass
class CoreRun:
    clock: FakeClock
    adapter: SyntheticAdapter
    core: ListenCore
    published: list

    def events(self, kind=None, route=None):
        return [e for e in self.published if (kind is None or e["evidence"]["type"] == kind)
                and (route is None or e["route_id"] == route)]

    def drive(self, reads=1_000_000, *, measure_every=1, timeout_ns=50 * MS):
        stop = None
        for count in range(1, reads + 1):
            item = self.adapter.read(timeout_ns)
            self.core.ingest(item)
            stop = self.core.watchdog_tick()
            if measure_every and count % measure_every == 0:
                self.core.measure_pending()
            if stop or (isinstance(item, AdapterEvent) and item.type in ENDING_EVENTS):
                break
        self.core.measure_pending()
        return stop

    def stop(self, cause="listen_stop", request_id="stop-1"):
        assert self.core.begin_stop(cause, request_id=request_id)
        self.adapter.close(0)
        return self.core.finish_stop()

    def observe(self):
        return self.core.observe()["observation"]

    def route(self, route_id):
        return next(r for r in self.observe()["routes"] if r["route_id"] == route_id)


def build_core(spec, routes=MASTER, *, uid=UID, rate=RATE, begin=True, facts_capacity=1024,
               descriptor_capacity=4096, journal_status=None, security=None, **settings):
    clock = FakeClock()
    adapter = SyntheticAdapter(spec, uid, rate, clock)
    opened = adapter.open()
    assert opened.refusal == ()
    published = []
    core = ListenCore(core_config(routes, rate=rate, **settings), opened, session_id="session-test",
                      runtime_epoch=RUNTIME, audio_source="synthetic_script", adapter_profile=adapter.profile,
                      config_sha256="0" * 64, clock=clock, adapter_stats=adapter.stats,
                      journal_status=journal_status, security=security, on_publish=published.append,
                      facts_capacity=facts_capacity, descriptor_capacity=descriptor_capacity)
    assert adapter.start(core.selected_channels).started
    if begin:
        core.begin()
    return CoreRun(clock, adapter, core, published)


def notice_codes(observation):
    return [n["code"] for n in observation["notices"]]


def test_core_publishes_lifecycle_in_ingest_order_and_stop_closes_meters_and_seals_a_summary():
    r = build_core(script(stereo_tone(12000), silence(2400)), coalesce=5)
    r.drive(reads=20)
    types = [evidence(e)["type"] for e in r.published]
    assert types[:3] == ["runtime_started", "capture_opened", "stream_started"]
    assert [e["sequence"] for e in r.published] == list(range(1, len(r.published) + 1))
    opened = evidence(r.published[1])["detail"]
    assert opened["device"]["uid"] == UID and opened["format"]["selected_channels"] == [0, 1]
    assert opened["clocks"]["domains_compared"] is False and opened["topology_epoch"] == r.core.topology_epoch
    assert (opened["clocks"]["device_sample_time"], opened["clocks"]["device_host_time"]) == ("expected",) * 2
    assert r.observe()["capture"]["clocks"]["device_sample_time"] == "available"
    terminal = r.stop(request_id="stop-7")
    tail = [evidence(e)["type"] for e in r.published[-4:]]
    assert tail[-1] == "runtime_stopped" and "runtime_stopping" in [evidence(e)["type"] for e in r.published]
    stopped = evidence(r.published[-1])
    assert stopped["cause"] == "listen_stop" and stopped["detail"]["request_id"] == "stop-7"
    assert stopped["state_transition"] == {"from": "stopping", "to": "stopped"}
    assert stopped["detail"]["worker_drain"] == "complete"
    last_meter = r.events("signal_level", "master")[-1]
    assert evidence(last_meter)["truncated_by"] == "stop"
    assert terminal["state"] == "stopped" and terminal["cause"] == "listen_stop"
    assert terminal["stop_request_id"] == "stop-7" and terminal["capture"]["final_state"] == "stopped"
    assert terminal["broker"]["last_sequence"] == r.published[-1]["sequence"] == r.core.broker.head
    assert terminal["capture"]["streams"][0]["end_cause"] == "stop"
    assert terminal["worker"] == {"drain": "complete", "state": "running", "error": None, "lag_frames": 0,
                                  "overruns": 0, "unmeasured_frames": 0, "unmeasured_after_failure_frames": 0,
                                  "unaccounted_frames": 0, "facts_dropped": 0, "unmeasured_at_stop_frames": 0,
                                  "suppressed_after_stop": 0}
    assert terminal["routes"][0]["frames_measured"] == r.core.tracker.snapshot()["frames_received"]
    assert terminal["coverage"]["control_issued"] is False
    assert terminal["coverage"]["audio_written_to_disk"] is False
    head = r.core.broker.head
    assert r.core.begin_stop("listen_stop") is False
    assert r.core.ingest(r.adapter.read(50 * MS)) is None and r.core.measure_pending() == 0
    assert r.core.watchdog_tick() is None and r.core.broker.head == head
    assert r.observe()["runtime"]["state"] == "stopped"
    with pytest.raises(RuntimeError):
        r.core.finish_stop()


def test_core_refuses_ingest_before_begin_and_a_refused_open_or_route_never_builds_a_core():
    r = build_core(script(stereo_tone(4800)), begin=False)
    with pytest.raises(RuntimeError, match="begin"):
        r.core.ingest(r.adapter.read(50 * MS))
    r.core.begin()
    with pytest.raises(RuntimeError, match="once"):
        r.core.begin()
    spec = script(stereo_tone(480), devices=[device(rate=44100)])
    refused = SyntheticAdapter(spec, UID, RATE, FakeClock()).open()
    with pytest.raises(ValueError, match="refused open"):
        ListenCore(core_config(), refused, session_id="s", runtime_epoch=RUNTIME,
                   audio_source="synthetic_script", adapter_profile="listen-synthetic/v1",
                   config_sha256="0" * 64, clock=FakeClock())


def test_end_of_script_ends_capture_and_the_runtime_keeps_serving_observe():
    r = build_core(script(stereo_tone(9600)))
    r.drive()
    for _ in range(40):
        r.core.ingest(r.adapter.read(50 * MS))
        r.core.watchdog_tick()
        r.core.measure_pending()
    observation = r.observe()
    assert observation["runtime"]["state"] == "running"
    assert observation["capture"]["state"] == "ended"
    assert observation["capture"]["ended_cause"] == "end_of_stream"
    master = next(row for row in observation["routes"] if row["route_id"] == "master")
    assert master["health"] == "ended" and master["availability"] == "unavailable"
    assert master["reason"] == "capture_ended" and master["signal"]["fresh"] is False
    assert master["signal"]["age_ms"] >= 2000
    assert notice_codes(observation)[0] == "capture_ended" and not r.events("capture_stale")
    page = r.core.poll(r.core.observe()["cursor"], wait_ms=0)
    assert page["events_status"] == "page" and page["health_summary"]["capture_state"] == "ended"


def test_cue_route_is_declared_unmetered_and_reports_channels_exposed_separately_in_observe():
    routes = MASTER + ({"route_id": "cue", "role": "cue", "channels": [2, 3]},)
    for channels, exposed in ((4, True), (2, False)):
        tones = [(0, 480.0, -12.0)] + [(c, 480.0, -6.0) for c in (2, 3) if c < channels]
        r = build_core(script(tone(9600, *tones), devices=[device(channels=channels)]), routes)
        r.drive()
        cue = r.route("cue")
        assert cue["channels_exposed"] is exposed and cue["metering"] == "withheld_until_routing_test"
        assert cue["availability"] == "unavailable" and cue["isolated_cue"] == "unavailable"
        assert cue["semantic_status"] == "not_established" and cue["signal"] is None
        assert cue["reason"] == "routing_test_not_available" and cue["route_calibration_id"] is None
        assert r.core.selected_channels == (0, 1)
        assert not [e for e in r.published if e["route_role"] == "cue" or e["route_id"] == "cue"]
        (cue_notice,) = [n for n in r.observe()["notices"] if n["code"] == "cue_not_established"]
        assert ("unlabeled route in a new session" in cue_notice["next_step"]) is exposed
        assert ("This input has no channels 2 and 3" in cue_notice["next_step"]) is not exposed
        assert r.route("master")["semantic_status"] == "declared_unverified"


def test_unexposed_unlabeled_route_is_unavailable_and_unexposed_master_refuses_capture():
    routes = MASTER + ({"route_id": "aux", "role": "unlabeled", "channels": [2, 3]},)
    r = build_core(script(stereo_tone(9600), devices=[device(channels=2)]), routes)
    r.drive(reads=12)
    assert r.route("master")["availability"] == "available" and r.route("master")["health"] == "ok"
    r.drive()
    aux = r.route("aux")
    assert aux["availability"] == "unavailable" and aux["reason"] == "channels_not_exposed"
    assert aux["health"] == "unavailable" and aux["signal"] is None and aux["channels_exposed"] is False
    assert r.core.selected_channels == (0, 1) and not r.events("signal_level", "aux")
    assert [row["route_id"] for row in r.core.terminal_summary()["routes"]] == ["master", "aux"]
    assert r.core.terminal_summary()["routes"][1]["clipped_total"] is None
    two_channels = script(stereo_tone(480), devices=[device(channels=2)])
    opened = SyntheticAdapter(two_channels, UID, RATE, FakeClock()).open()
    unexposed = ({"route_id": "master", "role": "master", "channels": [2, 3]},)
    assert resolve_routes(list(unexposed), 2)[1] == ("master_channels_not_exposed",)
    only_aux = ({"route_id": "aux", "role": "unlabeled", "channels": [3]},)
    assert resolve_routes(list(only_aux), 2)[1] == ("no_metered_channels_exposed",)
    for declared in (unexposed, only_aux):
        with pytest.raises(ValueError, match="route resolution refused"):
            ListenCore(core_config(declared), opened, session_id="s", runtime_epoch=RUNTIME,
                       audio_source="synthetic_script", adapter_profile="listen-synthetic/v1",
                       config_sha256="0" * 64, clock=FakeClock())


def test_blocked_adapter_observation_is_stale_with_an_aging_meter_never_the_last_success():
    r = build_core(script(stereo_tone(9600), stall(3000, "pauses"), stereo_tone(4800)), coalesce=1)
    r.drive(reads=30)
    ages = []
    for _ in range(20):
        r.core.ingest(r.adapter.read(50 * MS))
        r.core.watchdog_tick()
        r.core.measure_pending()
        master = r.route("master")
        ages.append(master["signal"]["age_ms"])
    observation = r.observe()
    assert observation["capture"]["state"] == "stale" and notice_codes(observation)[0] == "capture_stale"
    assert master["health"] == "stale" and master["availability"] == "unavailable"
    assert master["reason"] == "capture_stale" and master["near_silence"] == "unknown"
    assert master["signal"]["fresh"] is False and ages == sorted(ages) and ages[-1] > ages[0] >= 500
    assert "stale, no frames for" in observation["summary"]
    assert r.core.health_summary()["routes"][0]["health"] == "stale"
    r.drive()
    assert len(r.events("capture_resumed")) == 1 and r.observe()["capture"]["state"] == "ended"


def test_observe_reads_its_cursor_before_the_state_it_reports():
    r = build_core(script(stereo_tone(48000)), coalesce=1)
    r.drive(reads=30)
    original = r.core.tracker.snapshot
    injected = []

    def racing_snapshot():
        injected.append(r.core.publish_health("capture_stale", cause="injected_race"))
        return original()

    r.core.tracker.snapshot = racing_snapshot
    result = r.core.observe()
    assert result["cursor"]["sequence"] < injected[0]
    master = next(row for row in result["observation"]["routes"] if row["route_id"] == "master")
    assert master["signal"]["sequence"] <= result["cursor"]["sequence"]
    assert result["observation"]["snapshot_sequence"] == result["cursor"]["sequence"]
    page = r.core.poll(result["cursor"])
    assert injected[0] in [e["sequence"] for e in page["events"]]


def test_route_totals_retained_ring_span_and_notices_follow_the_published_evidence():
    impulses = {"kind": "impulse", "frames": 4800,
                "impulses": [{"channel": 0, "offset_frames": 10, "dbfs": 3.0},
                             {"channel": 1, "offset_frames": 20, "dbfs": 0.0}]}
    spec = script(stereo_tone(9600), impulses, gap(480), nonfinite(4800, [1], [5]), stereo_tone(9600))
    r = build_core(spec, ring_seconds=1)
    r.drive()
    observation = r.observe()
    retained = observation["ring"]["retained"]
    assert retained["stream_epoch"] == stream_epoch(RUNTIME, 1) and retained["contains_gaps"] is True
    assert retained["start_frame"] == 0 and retained["frames"] == 9600 + 4800 + 480 + 4800 + 9600
    assert retained["frames_held"] == retained["frames"] - 480
    assert observation["ring"]["retention"] == "in_memory_bounded_window_no_file"
    codes = notice_codes(observation)
    assert {"frames_missing_exact", "master_label_untested", "synthetic_audio"} <= set(codes)
    exact = next(n for n in observation["notices"] if n["code"] == "frames_missing_exact")
    assert "480 frames at stream frame 14,400" in exact["text"]
    (master,) = r.core.terminal_summary()["routes"]
    assert master["max_sample_peak_dbfs"] == pytest.approx(3.0, abs=1e-3)
    assert master["clipped_total"] == 2 and master["nonfinite_total"] == 1
    assert r.core.terminal_summary()["capture"]["nonfinite_samples"] == 1


def test_retained_span_reports_only_the_current_stream_epoch():
    r = build_core(script(stereo_tone(9600), {"kind": "counter_reset", "sample_time": 0}, stereo_tone(4800)))
    r.drive()
    retained = r.observe()["ring"]["retained"]
    assert retained == {"stream_epoch": stream_epoch(RUNTIME, 2), "start_frame": 0, "frames": 4800,
                        "frames_held": 4800, "contains_gaps": False}
    assert "new_stream_epoch" in notice_codes(r.observe())


def _next_block(r, sample_time, received_ns):
    return AdapterBlock(480, np.zeros((480, 2), dtype=np.float32), float(sample_time), None, received_ns, 0)


def test_capture_resumed_never_precedes_the_capture_stale_it_ends(monkeypatch):
    r = build_core(script(stereo_tone(48000)))
    r.core.ingest(r.adapter.read(50 * MS))
    r.clock.advance(600 * MS)
    tick, decided, proceed = r.core.watchdog.tick, threading.Event(), threading.Event()

    def paused_tick(*args, **kwargs):
        result = tick(*args, **kwargs)
        decided.set()
        proceed.wait(5)
        return result

    monkeypatch.setattr(r.core.watchdog, "tick", paused_tick)
    watchdog = threading.Thread(target=r.core.watchdog_tick)
    watchdog.start()
    assert decided.wait(5)
    capture = threading.Thread(target=r.core.ingest, args=(_next_block(r, 480, r.clock.monotonic_ns()),))
    capture.start()
    capture.join(0.2)
    proceed.set()
    watchdog.join(5)
    capture.join(5)
    r.core.measure_pending()
    order = [evidence(e)["type"] for e in r.published
             if evidence(e)["type"] in ("capture_stale", "capture_resumed")]
    assert order == ["capture_stale", "capture_resumed"]
    assert evidence(r.events("capture_resumed")[0])["detail"]["stale_for_ms"] == 0
    assert r.observe()["capture"]["state"] == "running"


def test_stale_and_resumed_alternate_however_far_measurement_lags():
    r = build_core(script(stereo_tone(480000)))
    r.core.ingest(r.adapter.read(50 * MS))
    r.core.measure_pending()
    r.clock.advance(600 * MS)
    r.core.watchdog_tick()
    r.core.ingest(_next_block(r, 480, r.clock.monotonic_ns()))
    r.clock.advance(600 * MS)
    r.core.watchdog_tick()
    r.core.measure_pending()
    order = [(evidence(e)["type"], evidence(e)["state_transition"]["to"]) for e in r.published
             if evidence(e)["type"] in ("capture_stale", "capture_resumed")]
    assert order == [("capture_stale", "stale"), ("capture_resumed", "running"), ("capture_stale", "stale")]
    assert r.observe()["capture"]["state"] == "stale"


def test_a_watchdog_event_never_names_a_stream_before_its_stream_started():
    r = build_core(script(stereo_tone(480000)))
    r.core.ingest(r.adapter.read(50 * MS))
    r.core.ingest(_next_block(r, 0, r.clock.monotonic_ns()))
    r.clock.advance(600 * MS)
    r.core.watchdog_tick()
    r.core.ingest(_next_block(r, 480, r.clock.monotonic_ns()))
    r.core.measure_pending()
    announced = set()
    for event in r.published:
        if evidence(event)["type"] == "stream_started":
            announced.add(event["stream_epoch"])
        elif event["stream_epoch"] is not None:
            assert event["stream_epoch"] in announced, evidence(event)["type"]
    assert r.events("capture_stale")[0]["stream_epoch"] == stream_epoch(RUNTIME, 2)
    assert r.events("capture_resumed")[0]["stream_epoch"] == stream_epoch(RUNTIME, 2)


def test_a_block_received_before_the_stale_decision_resumes_with_zero_not_negative_duration():
    r = build_core(script(stereo_tone(48000)))
    r.core.ingest(r.adapter.read(50 * MS))
    arrived = r.clock.monotonic_ns() + 590 * MS
    r.clock.advance(600 * MS)
    r.core.watchdog_tick()
    r.core.ingest(_next_block(r, 480, arrived))
    r.core.measure_pending()
    detail = evidence(r.events("capture_resumed")[0])["detail"]
    assert detail["stale_for_ms"] == 0 and detail["received_before_stale_decision"] is True


def test_a_block_ingested_after_the_stale_decision_resumes_with_no_gap_while_its_counter_follows_on():
    r = build(script(stereo_tone(480 * 4)))
    start = r.clock.monotonic_ns()
    pcm = np.zeros((480, 2), dtype=np.float32)
    r.feed.ingest(AdapterBlock(480, pcm, 0.0, None, start, 0, None))
    r.feed.ingest(AdapterBlock(480, pcm, 480.0, None, start + 10 * MS, 0, None))
    r.feed.watchdog_tick(start + 600 * MS)
    r.feed.ingest(AdapterBlock(480, pcm, 960.0, None, start + 20 * MS, 0, None))
    r.worker.run_pending()
    types = [evidence(e)["type"] for e in r.sink.events if e["delivery_class"] == "health"]
    assert types == ["stream_started", "capture_stale", "capture_resumed"]
    assert evidence(r.events("capture_resumed")[0])["detail"]["received_before_stale_decision"] is True
    assert r.tracker.snapshot()["stream_ordinal"] == 1 and r.tracker.snapshot()["missing"]["events"] == 0
    r.feed.ingest(AdapterBlock(480, pcm, 1440.0 + 28800, None, start + 700 * MS, 0, None))
    (missing,) = r.events("frames_missing")
    assert evidence(missing)["cause"] == "device_gap" and spans([missing]) == [(1440, 28800)]
    assert r.tracker.snapshot()["stream_ordinal"] == 1


def test_a_nonfinite_route_says_how_old_its_measured_state_is_while_measurement_lags():
    r = build_core(script(nonfinite(RATE, [0, 1], list(range(0, RATE, 800))), stereo_tone(4 * RATE)))
    for _ in range(100):
        r.core.ingest(r.adapter.read(50 * MS))
        r.core.measure_pending()
    current = r.observe()
    (fresh,) = [n for n in current["notices"] if n["code"] == "nonfinite_input"]
    assert "trails capture" not in fresh["text"] and "measured" not in current["summary"]
    for _ in range(300):
        r.core.ingest(r.adapter.read(50 * MS))
    lagging = r.observe()
    assert lagging["worker"]["lag_frames"] == 3 * RATE and lagging["routes"][0]["near_silence"] == "unknown"
    (old,) = [n for n in lagging["notices"] if n["code"] == "nonfinite_input"]
    assert "which trails capture by 3.0 s" in old["text"]
    assert "master: nonfinite input in audio measured 3.0 s behind capture" in lagging["summary"]


def test_startup_before_io_begins_doesnt_count_toward_the_first_block_age():
    r = build_core(script(stereo_tone(48000)), begin=False)
    r.clock.advance(700 * MS)
    r.core.begin()
    r.clock.advance(50 * MS)
    r.core.watchdog_tick()
    r.core.measure_pending()
    assert not r.events("capture_stale") and r.observe()["capture"]["age_ms"] == 50
    r.clock.advance(450 * MS)
    r.core.watchdog_tick()
    (stale,) = r.events("capture_stale")
    assert evidence(stale)["detail"]["age_ms"] == 500
    assert evidence(stale)["state_transition"]["from"] == "starting"
    assert notice_codes(r.observe())[0] == "capture_no_frames"
    text = r.observe()["notices"][0]["text"]
    assert "No frames have arrived from Master since capture started 0.5 s ago" in text


def test_watchdog_facts_are_published_when_decided_never_behind_the_measurement_backlog():
    r = build_core(script(stereo_tone(48000), stall(1000, "pauses"), stereo_tone(4800)), coalesce=1)
    for _ in range(112):
        r.core.ingest(r.adapter.read(50 * MS))
        r.core.watchdog_tick()
    assert r.core.worker.snapshot()["lag_frames"] >= 48000 - 480 and not r.events("signal_level")
    (stale,) = r.events("capture_stale")
    assert stale["sequence"] == r.core.broker.head
    assert r.core.health_summary()["capture_state"] == "stale"


def test_capture_health_is_on_the_page_while_measurement_is_held_back():
    r = build_core(script(stereo_tone(48000), gap(512), stereo_tone(48000)))
    for _ in range(101):
        r.core.ingest(r.adapter.read(50 * MS))
        r.core.watchdog_tick()
    assert r.core.worker.snapshot()["lag_frames"] == 101 * 480
    page = r.core.poll({"runtime_epoch": RUNTIME, "sequence": 0})
    assert page["events_status"] == "page"
    assert [evidence(e)["type"] for e in page["events"]] == ["runtime_started", "capture_opened",
                                                            "stream_started", "frames_missing"]
    started, missing = page["events"][2:]
    assert started["stream_epoch"] == missing["stream_epoch"] == stream_epoch(RUNTIME, 1)
    assert evidence(missing)["detail"]["missing"] == {"start_frame": 48000, "frames": 512}
    assert r.observe()["capture"]["missing"]["exact_frames"] == 512
    r.core.measure_pending()
    meters = r.events("signal_level")
    assert spans(meters) == [(0, 24000), (24000, 24000)]
    assert all(e["sequence"] > missing["sequence"] for e in meters)


def test_after_the_measurement_worker_fails_a_later_gap_is_still_published(monkeypatch):
    r = build_core(script(stereo_tone(9600), gap(512), stereo_tone(4800)), coalesce=1)
    r.drive(reads=5)

    def broken(*args):
        raise RuntimeError("injected measurement fault")

    monkeypatch.setattr(r.core.worker, "_add", broken)
    r.drive(reads=1)
    assert r.core.worker.state == "failed"
    (failed,) = r.events("measurement_worker_failed")
    while r.core.tracker.capture_active:
        r.core.ingest(r.adapter.read(50 * MS))
        r.core.watchdog_tick()
    page = r.core.poll({"runtime_epoch": RUNTIME, "sequence": failed["sequence"]})
    assert page["events_status"] == "page"
    assert [evidence(e)["type"] for e in page["events"]] == ["frames_missing", "stream_ended",
                                                            "capture_ended"]
    assert evidence(page["events"][0])["detail"]["missing"] == {"start_frame": 9600, "frames": 512}


def test_near_silence_is_interrupted_at_a_gap_published_while_measurement_lagged():
    quiet_at, quiet, after_gap = 48000, 144000, 48000 + 144000 + 4800
    r = build_core(script(stereo_tone(quiet_at), silence(quiet), gap(4800), silence(quiet)), min_ms=2000)
    r.drive(reads=(quiet_at + 96000) // 480 + 10)
    assert r.route("master")["near_silence"] == "active"
    _ingest_until_the_end(r)
    (missing,) = r.events("frames_missing")
    r.core.measure_pending()
    events = r.events("near_silence", "master")
    assert [(evidence(e)["state"], evidence(e)["interrupted_by"]) for e in events] == [
        ("started", None), ("interrupted", "frames_missing"),
        ("started", None), ("interrupted", "stream_ended")]
    assert spans(events) == [(quiet_at, 96000), (quiet_at, quiet), (after_gap, 96000), (after_gap, quiet)]
    assert events[0]["sequence"] < missing["sequence"] < events[1]["sequence"]


def test_near_silence_reads_unknown_while_measurement_trails_capture_by_more_than_stale_after():
    r = build_core(script(stereo_tone(48000), silence(144000), stereo_tone(96000)), min_ms=1000)
    r.drive(reads=(48000 + 120000) // 480)
    assert r.route("master")["near_silence"] == "active"

    def ingest(blocks):
        for _ in range(blocks):
            r.core.ingest(r.adapter.read(50 * MS))
            r.core.watchdog_tick()

    ingest(10)
    assert r.route("master")["near_silence"] == "active"
    ingest(50)
    observed = r.observe()
    assert r.core.worker.snapshot()["lag_frames"] > 24000
    master = next(row for row in observed["routes"] if row["route_id"] == "master")
    assert master["near_silence"] == "unknown" and "near_silence" not in notice_codes(observed)
    assert {row["route_id"]: row["near_silence"] for row in r.core.health_summary()["routes"]}["master"] == "unknown"
    r.core.measure_pending()
    assert r.route("master")["near_silence"] != "unknown"


def test_capture_records_say_which_software_produced_them():
    r = build_core(script(stereo_tone(4800)))
    r.drive()
    assert evidence(r.events("capture_opened")[0])["detail"]["provenance"] is None
    adapter = SyntheticAdapter(script(stereo_tone(4800)), UID, RATE, FakeClock())
    provenance = {"pocket_version": "0.0.test", "macos_version": "not_applicable"}
    core = ListenCore(core_config(), adapter.open(), session_id="s", runtime_epoch=RUNTIME,
                      audio_source="synthetic_script", adapter_profile=adapter.profile, config_sha256="0" * 64,
                      clock=FakeClock(), provenance=provenance)
    published = []
    core.broker._on_publish = published.append
    core.begin()
    opened = next(e for e in published if evidence(e)["type"] == "capture_opened")
    assert evidence(opened)["detail"]["provenance"] == provenance
    assert core.terminal_summary()["capture"]["provenance"] == provenance


def test_helper_heartbeat_recovery_is_published_and_observed():
    t0 = 1_000_000_000
    watchdog = Watchdog(500, t0)
    watchdog.block_received(t0 + 3000 * MS)
    (late,), _ = watchdog.tick(t0 + 3000 * MS, heartbeat_ns=t0)
    assert late.type == "helper_unresponsive" and late.state_transition == ("responsive", "unresponsive")
    assert watchdog.snapshot(t0 + 3000 * MS)["helper_unresponsive"] is True
    assert watchdog.tick(t0 + 3050 * MS, heartbeat_ns=t0) == ((), None)
    (back,), _ = watchdog.tick(t0 + 3100 * MS, heartbeat_ns=t0 + 3090 * MS)
    assert back.type == "helper_responsive" and back.state_transition == ("unresponsive", "responsive")
    assert watchdog.snapshot(t0 + 3100 * MS)["helper_unresponsive"] is False


def test_after_a_worker_failure_routes_report_no_current_level_or_near_silence(monkeypatch):
    r = build_core(script(stereo_tone(48000), silence(144000), stereo_tone(480000, dbfs=-12.0)), min_ms=1000)
    for _ in range(400):
        r.drive(reads=1)
        if r.route("master")["near_silence"] == "active":
            break
    assert r.route("master")["near_silence"] == "active"

    def broken(*args):
        raise RuntimeError("injected measurement fault")

    monkeypatch.setattr(r.core.worker, "_add", broken)
    r.drive(reads=600)
    assert r.core.tracker.snapshot()["frames_received"] > 192000 + 96000 and r.core.worker.state == "failed"
    observation = r.observe()
    master = next(row for row in observation["routes"] if row["route_id"] == "master")
    assert master["availability"] == "unavailable" and master["health"] == "measurement_failed"
    assert master["reason"] == "measurement_worker_failed" and master["near_silence"] == "unknown"
    assert "master: receiving, not measured" in observation["summary"]
    assert "digital zero" not in observation["summary"]
    codes = notice_codes(observation)
    assert "measurement_worker_failed" in codes and "near_silence" not in codes
    assert "measurement_overrun" not in codes
    worker = observation["worker"]
    assert worker["unmeasured_frames"] == worker["unmeasured_after_failure_frames"] > 0
    summary = r.core.health_summary()
    assert summary["worker_state"] == "failed" and summary["routes"][0]["near_silence"] == "unknown"
    assert summary["routes"][0]["availability"] == "unavailable"
    (interrupted,) = [e for e in r.events("near_silence") if evidence(e)["state"] == "interrupted"]
    assert evidence(interrupted)["interrupted_by"] == "measurement_worker_failed"
    assert interrupted["sequence"] > r.events("measurement_worker_failed")[0]["sequence"]


def test_a_fresh_level_is_named_in_the_summary_and_an_old_one_is_not():
    r = build_core(script(stereo_tone(96000)), coalesce=5, stale_after_ms=100)
    r.drive(reads=50)
    assert "receiving, peak -12.0 dBFS" in r.observe()["summary"]
    r.drive(reads=20)
    observation = r.observe()
    master = next(row for row in observation["routes"] if row["route_id"] == "master")
    assert master["health"] == "ok" and master["signal"]["fresh"] is False
    assert "master: receiving ·" in observation["summary"] and "peak" not in observation["summary"]


def test_an_incomplete_stop_drain_is_sealed_as_incomplete_and_nothing_publishes_after_runtime_stopped(
        monkeypatch):
    r = build_core(script(stereo_tone(96000)), window_frames=480, coalesce=1)
    for _ in range(200):
        r.core.ingest(r.adapter.read(50 * MS))
    inside, release = threading.Event(), threading.Event()
    original = r.core.worker._measure

    def slow(d):
        inside.set()
        release.wait(5)
        original(d)

    monkeypatch.setattr(r.core.worker, "_measure", slow)
    worker = threading.Thread(target=r.core.measure_pending)
    worker.start()
    assert inside.wait(5)
    assert r.core.begin_stop("listen_stop", request_id="stop-1")
    terminal = r.core.finish_stop(drain_ns=20 * MS)
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    stopped = r.events("runtime_stopped")[0]
    assert evidence(stopped)["detail"]["worker_drain"] == "incomplete"
    assert r.published[-1] is stopped and r.core.broker.head == stopped["sequence"]
    assert terminal["broker"]["last_sequence"] == stopped["sequence"]
    assert terminal["worker"]["drain"] == "incomplete" and terminal["worker"]["lag_frames"] >= 96000 - 480
    assert terminal["routes"][0]["frames_measured"] == 0
    assert r.core.worker.snapshot()["suppressed_after_stop"] >= 1


GAP_THEN_LOST = script(stereo_tone(4800), gap(512), stereo_tone(4800), {"kind": "device_lost"})


def _ingest_until_the_end(r):
    while True:
        item = r.adapter.read(50 * MS)
        r.core.ingest(item)
        if isinstance(item, AdapterEvent) and item.type in ENDING_EVENTS:
            return


def _held_worker(r, monkeypatch, *, release_on_cancel):
    inside, release = threading.Event(), threading.Event()
    measure, cancel = r.core.worker._measure, r.core.worker.cancel

    def slow(d):
        inside.set()
        release.wait(5)
        measure(d)

    def cancelled():
        cancel()
        if release_on_cancel:
            release.set()

    monkeypatch.setattr(r.core.worker, "_measure", slow)
    monkeypatch.setattr(r.core.worker, "cancel", cancelled)
    worker = threading.Thread(target=r.core.measure_pending)
    worker.start()
    assert inside.wait(5)
    return worker, release


GAP_THEN_LOST_CAPTURE_HEALTH = ["runtime_started", "capture_opened", "stream_started", "frames_missing",
                                "stream_ended", "capture_ended"]


def test_a_stop_that_cancels_a_lagging_worker_counts_the_blocks_it_left_and_capture_health_is_already_out(
        monkeypatch):
    r = build_core(GAP_THEN_LOST, window_frames=480, coalesce=1)
    _ingest_until_the_end(r)
    assert [evidence(e)["type"] for e in r.published] == GAP_THEN_LOST_CAPTURE_HEALTH
    worker, _ = _held_worker(r, monkeypatch, release_on_cancel=True)
    assert r.core.begin_stop("listen_stop", request_id="stop-1")
    terminal = r.core.finish_stop(drain_ns=2_000 * MS)
    worker.join(10)
    types = [evidence(e)["type"] for e in r.published]
    assert types[:6] == GAP_THEN_LOST_CAPTURE_HEALTH and types.index("runtime_stopped") == len(types) - 1
    (missing,) = r.events("frames_missing")
    assert evidence(missing)["detail"]["missing"] == {"start_frame": 4800, "frames": 512}
    page = r.core.poll({"runtime_epoch": RUNTIME, "sequence": 0}, max_events=256)
    assert page["events_status"] == "page" and page["missing"] == [] and not page["more_available"]
    assert [evidence(e)["type"] for e in page["events"]] == types
    work, (master,) = terminal["worker"], terminal["routes"]
    assert work["drain"] == "incomplete" and work["lag_frames"] == 0
    assert work["unmeasured_at_stop_frames"] == 9600 - 480
    assert master["frames_measured"] + work["unmeasured_frames"] + work["unaccounted_frames"] == 9600
    assert {evidence(e)["cause"] for e in r.events("measurement_overrun")} == {"stop_drain_bound"}
    assert terminal["broker"]["health_lost_through"] == 0


def test_a_stop_the_worker_never_lets_go_of_leaves_every_capture_fact_readable(monkeypatch):
    r = build_core(GAP_THEN_LOST, window_frames=480, coalesce=1)
    _ingest_until_the_end(r)
    worker, release = _held_worker(r, monkeypatch, release_on_cancel=False)
    assert r.core.begin_stop("listen_stop", request_id="stop-1")
    terminal = r.core.finish_stop(drain_ns=20 * MS)
    release.set()
    worker.join(5)
    types = [evidence(e)["type"] for e in r.published]
    assert types == [*GAP_THEN_LOST_CAPTURE_HEALTH, "runtime_stopping", "runtime_stopped"]
    assert "facts_unpublished_at_stop" not in evidence(r.published[-1])["detail"]
    assert terminal["broker"]["health_lost_through"] == 0
    page = r.core.poll({"runtime_epoch": RUNTIME, "sequence": 0})
    assert page["events_status"] == "page" and [evidence(e)["type"] for e in page["events"]] == types
    work, (master,) = terminal["worker"], terminal["routes"]
    assert work["drain"] == "incomplete" and work["lag_frames"] == 9600 and master["frames_measured"] == 0
    assert terminal["capture"]["missing"]["exact_frames"] == 512
    assert terminal["capture"]["ended_cause"] == "device_lost"


@pytest.mark.parametrize("slow_blocks, drain, measured", [("last", "complete", 4800),
                                                          ("every", "incomplete", 480)])
def test_a_stop_drain_is_complete_exactly_when_measurement_reached_every_block(monkeypatch, slow_blocks,
                                                                              drain, measured):
    r = build_core(script(stereo_tone(9600)), window_frames=480, coalesce=1)
    for _ in range(10):
        r.core.ingest(r.adapter.read(50 * MS))
    measure = r.core.worker._measure

    def slow(d):
        measure(d)
        if slow_blocks == "every" or d.ingest_index == r.core.feed.committed - 1:
            r.clock.advance(100 * MS)

    monkeypatch.setattr(r.core.worker, "_measure", slow)
    assert r.core.begin_stop("listen_stop", request_id="stop-1")
    terminal = r.core.finish_stop(drain_ns=20 * MS)
    work, (master,) = terminal["worker"], terminal["routes"]
    assert work["drain"] == drain and master["frames_measured"] == measured
    assert work["unmeasured_at_stop_frames"] == 4800 - measured and work["lag_frames"] == 0


def test_a_full_facts_queue_never_sends_a_reader_to_a_snapshot_and_its_gaps_are_already_published(
        monkeypatch):
    r = build_core(ALTERNATING_TONES_AND_GAPS, facts_capacity=4, window_frames=480, coalesce=1)
    _ingest_until_the_end(r)
    publish, pages = r.core.broker.publish, []

    def publish_then_poll(draft, *, final=False):
        sequence = publish(draft, final=final)
        pages.append(r.core.broker.poll({"runtime_epoch": RUNTIME, "sequence": 0}, max_events=256,
                                        max_bytes=262144))
        return sequence

    monkeypatch.setattr(r.core.broker, "publish", publish_then_poll)
    r.core.measure_pending()
    assert len(pages) > 16 and {page["events_status"] for page in pages} == {"page"}
    assert r.core.broker.counters()["health_lost_through"] == 0
    gaps = {(e["interval"]["start_frame"], e["interval"]["frames"]): e["sequence"]
            for e in r.events("frames_missing")}
    unaccounted = r.events("frames_unaccounted")
    assert len(unaccounted) == 16 and all(gaps[span] < e["sequence"]
                                          for span, e in zip(spans(unaccounted), unaccounted, strict=True))
    observation = r.observe()
    assert observation["worker"]["facts_dropped"] == 17 and observation["worker"]["unaccounted_frames"] == 768
    (lost,) = [n for n in observation["notices"] if n["code"] == "measurement_facts_dropped"]
    assert "lost track of 17 capture facts" in lost["text"] and "768 frames" in lost["text"]
    assert "health_facts_dropped" not in notice_codes(observation)


def test_a_worker_failure_publishes_the_windows_it_had_measured_and_says_why_they_end():
    r = build_core(script(stereo_tone(48000)), coalesce=5)
    r.drive(reads=30)
    assert not r.events("signal_level")

    def broken(*args):
        raise RuntimeError("injected measurement fault")

    r.core.worker._add = broken
    r.drive(reads=5)
    (flushed,) = r.events("signal_level")
    assert evidence(flushed)["truncated_by"] == "measurement_worker_failed"
    assert evidence(flushed)["frames_measured"] == 14400
    assert flushed["sequence"] > r.events("measurement_worker_failed")[0]["sequence"]


@pytest.mark.parametrize("fails, after_reads", [
    pytest.param("_add", 50, id="on_a_window_boundary"),
    pytest.param("_add", 55, id="mid_window"),
    pytest.param("_route_window", 49, id="while_a_window_closes"),
])
def test_a_worker_failure_accounts_for_every_received_frame(monkeypatch, fails, after_reads):
    r = build_core(script(stereo_tone(120000)), coalesce=1)
    r.drive(reads=after_reads)

    def broken(*args):
        raise RuntimeError("injected measurement fault")

    monkeypatch.setattr(r.core.worker, fails, broken)
    r.drive()
    terminal = r.stop()
    worker, (master,) = terminal["worker"], terminal["routes"]
    assert worker["state"] == "failed" and r.core.tracker.snapshot()["frames_received"] == 120000
    assert worker["error"] == "RuntimeError: injected measurement fault"
    assert r.observe()["worker"]["error"] == worker["error"]
    assert master["frames_measured"] + worker["unmeasured_frames"] + worker["unaccounted_frames"] == 120000
    assert worker["unmeasured_frames"] == worker["unmeasured_after_failure_frames"] == 120000 - master[
        "frames_measured"]
    assert master["availability"] == "unavailable" and master["reason"] == "measurement_worker_failed"


def test_nothing_publishes_after_runtime_stopped_whichever_thread_tries():
    r = build_core(script(stereo_tone(48000)))
    r.drive(reads=20)
    terminal = r.stop()
    head = r.core.broker.head
    assert r.core.publish_health("journal_failed", cause="failed") is None
    assert r.core._publish(r.core.factory.health("capture_stale", received_ns=r.clock.monotonic_ns())) is None
    assert r.core.broker.head == head == terminal["broker"]["last_sequence"]
    assert r.core.broker.counters()["refused_after_final"] == 2
    assert evidence(r.published[-1])["type"] == "runtime_stopped"


def test_a_level_the_broker_accepted_before_runtime_stopped_is_in_the_totals_the_stop_reads():
    r = build_core(script(stereo_tone(9600)), window_frames=480, coalesce=1)
    for _ in range(10):
        r.core.ingest(r.adapter.read(50 * MS))
    assert r.core.begin_stop("listen_stop", request_id="stop-1")
    publish, stopping = r.core.broker.publish, {}

    def stop_meanwhile():
        stopped = r.core.factory.health("runtime_stopped", received_ns=r.clock.monotonic_ns(),
                                        cause="listen_stop", affected_routes=(),
                                        state_transition=("stopping", "stopped"))
        stopping["sequence"] = publish(stopped, final=True)
        stopping["summary"] = r.core.terminal_summary()

    def accept_then_stop(draft, *, final=False):
        sequence = publish(draft, final=final)
        if sequence is not None and draft["evidence"]["type"] == "signal_level" and "thread" not in stopping:
            stopping["thread"] = threading.Thread(target=stop_meanwhile, daemon=True)
            stopping["thread"].start()
            stopping["thread"].join(0.5)
        return sequence
    r.core.broker.publish = accept_then_stop
    r.core.measure_pending()
    stopping["thread"].join(30)
    assert not stopping["thread"].is_alive()
    accepted = [e for e in r.events("signal_level") if e["sequence"] < stopping["sequence"]]
    assert accepted and stopping["summary"]["broker"]["last_sequence"] == stopping["sequence"]
    (master,) = stopping["summary"]["routes"]
    assert master["frames_measured"] == sum(evidence(e)["frames_measured"] for e in accepted)


def test_terminal_routes_say_how_each_route_ended_and_how_it_started():
    r = build_core(script(stereo_tone(9600), {"kind": "device_lost"}),
                   MASTER + ({"route_id": "aux", "role": "unlabeled", "channels": [2, 3]},), coalesce=1)
    r.drive()
    master, aux = r.stop()["routes"]
    assert (master["availability"], master["reason"], master["availability_at_start"]) == (
        "unavailable", "capture_ended", "available")
    assert master["frames_measured"] == 9600
    assert (aux["availability"], aux["availability_at_start"]) == ("unavailable", "available")


def test_unwatched_helper_properties_are_reported_at_open_in_observe_and_in_the_terminal():
    r = build_core(script(stereo_tone(48000)), begin=False)
    stats = r.adapter.stats
    r.core._adapter_stats = lambda: {**stats(), "unwatched_properties": ["processor_overload", "devices"]}
    r.core.begin()
    r.drive(reads=5)
    assert evidence(r.events("capture_opened")[0])["detail"]["unwatched_properties"] == [
        "processor_overload", "devices"]
    observation = r.observe()
    reported = observation["capture"]["adapter_stats"]
    assert reported["unwatched_properties"] == ["processor_overload", "devices"]
    (unwatched,) = [n for n in observation["notices"] if n["code"] == "device_changes_unwatched"]
    assert "processor_overload, devices" in unwatched["text"]
    assert r.stop()["capture"]["unwatched_properties"] == ["processor_overload", "devices"]


def test_journal_lines_dropped_from_a_full_queue_raise_the_journal_notice():
    journal = {"state": "writing", "lines": 40, "bytes": 9000, "dropped_lines": 3, "budget_bytes": 16777216}
    r = build_core(script(stereo_tone(48000)), journal_status=lambda: journal)
    r.drive(reads=5)
    (degraded,) = [n for n in r.observe()["notices"] if n["code"] == "journal_degraded"]
    assert "journal is dropping lines" in degraded["text"]


def test_the_exact_gap_notice_states_the_total_and_names_the_latest_gap_and_its_stream():
    r = build_core(script(stereo_tone(9600), gap(4800), stereo_tone(9600), gap(512), stereo_tone(9600)))
    r.drive()
    (exact,) = [n for n in r.observe()["notices"] if n["code"] == "frames_missing_exact"]
    assert "missing 5,312 frames in 2 exact gaps" in exact["text"]
    assert f"the latest is 512 frames at stream frame 24,000 on {stream_epoch(RUNTIME, 1)}" in exact["text"]


def test_the_exact_gap_notice_names_the_newest_gap_capture_found_while_measurement_lags():
    r = build_core(script(stereo_tone(9600), gap(512), stereo_tone(9600), gap(2048), stereo_tone(9600)))
    r.drive(reads=22)
    for _ in range(30):
        r.core.ingest(r.adapter.read(50 * MS))
    second = r.events("frames_missing")[1]
    assert evidence(second)["detail"]["missing"] == {"start_frame": 19712, "frames": 2048}
    assert r.core.worker.snapshot()["lag_frames"] > 0
    (exact,) = [n for n in r.observe()["notices"] if n["code"] == "frames_missing_exact"]
    assert "missing 2,560 frames in 2 exact gaps; the latest is 2,048 frames at stream frame 19,712" in exact[
        "text"]


def test_measurement_that_falls_half_a_ring_behind_says_so_before_any_overrun():
    r = build_core(script(stereo_tone(48000 * 8)))
    for _ in range(600):
        r.core.ingest(r.adapter.read(50 * MS))
    observation = r.observe()
    assert observation["worker"]["lag_frames"] == 288000 and observation["worker"]["overruns"] == 0
    (lagging,) = [n for n in observation["notices"] if n["code"] == "measurement_lagging"]
    assert "6.0 s behind capture" in lagging["text"]
    r.core.measure_pending()
    assert "measurement_lagging" not in notice_codes(r.observe())


def test_failing_adapter_stats_and_journal_hook_are_counted_and_reported():
    journal = {"state": "writing", "lines": 0, "bytes": 0, "dropped_lines": 0, "budget_bytes": 16777216}
    r = build_core(script(stereo_tone(48000)), journal_status=lambda: journal)

    def broken():
        raise RuntimeError("stats fault")

    def refuse(event):
        raise OSError("journal hook fault")

    r.core._adapter_stats = broken
    r.core.broker._on_publish = refuse
    r.drive(reads=10)
    observation = r.observe()
    assert observation["capture"]["adapter_stats"]["stats_errors"] >= 10
    assert observation["capture"]["helper_unresponsive"] == "unknown"
    assert "adapter_stats_unavailable" in notice_codes(observation)
    assert observation["broker"]["publish_hook_errors"] > 0
    assert "journal_degraded" in notice_codes(observation)
    terminal = r.core.terminal_summary()
    assert terminal["capture"]["adapter_stats_errors"] >= 10
    assert terminal["broker"]["publish_hook_errors"] == observation["broker"]["publish_hook_errors"]
    r.core._adapter_stats = lambda: {**r.adapter.stats(), "last_heartbeat_monotonic_ns": r.clock.monotonic_ns()}
    recovered = r.observe()
    assert recovered["capture"]["helper_unresponsive"] is False
    assert "adapter_stats_unavailable" not in notice_codes(recovered)


def test_an_adapter_without_a_heartbeat_reports_the_helper_check_as_unknown_not_passed():
    r = build_core(script(stereo_tone(48000)))
    r.drive(reads=5)
    observation = r.observe()
    assert observation["capture"]["state"] == "running"
    assert observation["capture"]["adapter_stats"]["last_heartbeat_age_ms"] is None
    assert observation["capture"]["helper_unresponsive"] == "unknown"


@pytest.mark.parametrize("ending,code", [
    ({"kind": "device_lost"}, "device_lost"),
    ({"kind": "hogged"}, "capture_ended"),
    ({"kind": "rate_change", "sample_rate": 44100}, "format_changed"),
    ({"kind": "channel_reorder", "order": [1, 0, 3, 2]}, "channel_map_changed"),
    (None, "capture_ended"),
])
def test_an_ended_capture_notice_stops_the_runtime_before_starting_again(ending, code):
    segments = [stereo_tone(4800)] + ([ending] if ending else [])
    r = build_core(script(*segments))
    r.drive()
    (ended,) = [n for n in r.observe()["notices"] if n["code"] == code]
    assert ended["severity"] == "fault"
    assert ended["next_step"].startswith("Stop this runtime with listen_stop and its runtime_epoch")


def test_the_helper_heartbeat_is_unknown_once_capture_has_ended():
    r = build_core(script(stereo_tone(4800), {"kind": "device_lost"}))
    r.core._adapter_stats = lambda: {**r.adapter.stats(), "last_heartbeat_monotonic_ns": r.clock.monotonic_ns()}
    r.core.ingest(r.adapter.read(50 * MS))
    r.core.watchdog_tick()
    assert r.observe()["capture"]["helper_unresponsive"] is False
    r.drive()
    observation = r.observe()
    assert observation["capture"]["state"] == "ended" and observation["capture"]["ended_cause"] == "device_lost"
    assert observation["capture"]["helper_unresponsive"] == "unknown"


def test_a_late_heartbeat_says_whether_the_runtime_itself_was_behind_reading_it():
    r = build_core(script(stereo_tone(48000)))
    start = r.clock.monotonic_ns()
    r.core._adapter_stats = lambda: {**r.adapter.stats(), "last_heartbeat_monotonic_ns": start,
                                     "reader_backpressured": True}
    r.core.ingest(r.adapter.read(50 * MS))
    r.clock.advance(2100 * MS)
    r.core.ingest(r.adapter.read(50 * MS))
    r.core.watchdog_tick()
    (late,) = r.events("helper_unresponsive")
    assert evidence(late)["detail"]["reader_backpressured"] is True and evidence(late)["detail"]["age_ms"] >= 2100


def test_runtime_bound_returns_the_stop_cause_once_after_runtime_expiring():
    r = build_core(script(stereo_tone(12 * RATE)), max_runtime_seconds=10)
    assert r.drive() == "max_runtime_reached"
    expiring = r.events("runtime_expiring")
    assert len(expiring) == 1 and evidence(expiring[0])["detail"]["seconds_remaining"] <= 1
    assert r.core.watchdog_tick() is None
    terminal = r.stop("max_runtime_reached", request_id=None)
    assert terminal["cause"] == "max_runtime_reached" and terminal["stop_request_id"] is None


def test_observation_is_bounded_finite_json_with_host_journal_and_security_blocks():
    segments = [stereo_tone(960) if i % 2 == 0 else gap(48) for i in range(200)]
    journal = {"state": "failed", "lines": 3, "bytes": 900, "dropped_lines": 0, "budget_bytes": 16777216}
    security = {"peer_uid_check": "enforced", "socket": "af_unix_only"}
    r = build_core(script(*segments, nonfinite(4800, [0], [1]), stereo_tone(4800)), coalesce=1,
                   journal_status=lambda: journal, security=security)
    r.drive()
    observation = r.observe()
    size = len(canonical_bytes(observation))
    assert size <= 65536 and len(observation["recent_health"]) == 16 and len(observation["notices"]) <= 8
    assert observation["journal"] == journal and observation["security"] == security
    assert "journal_degraded" in notice_codes(observation)
    assert [n["severity"] for n in observation["notices"]] == sorted(
        (n["severity"] for n in observation["notices"]), key=["fault", "warning", "info"].index)
    assert observation["capture"]["adapter_stats"]["device_period_ns"] == 10_000_000
    assert observation["capture"]["clocks"]["drift"] == "not_estimated"
    assert not AUTHORITY_KEYS & set(_keys(observation))
