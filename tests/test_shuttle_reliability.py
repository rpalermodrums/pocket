# SPDX-License-Identifier: AGPL-3.0-only
"""Offline regressions: no controller, app, audio device or host is started."""
from __future__ import annotations

import io
import json
import os
import shutil
import struct
import subprocess
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from pocket_music import shuttle, shuttle_host
from pocket_music.errors import PocketError
from pocket_music.shuttle_host import _master_stream_header
from test_shuttle import runtime


def wav_stream(samples, *, channels=2, rate=48000):
    x = np.asarray(samples, dtype="<f4")
    fmt = struct.pack("<HHIIHH", 3, channels, rate, rate * channels * 4, channels * 4, 32)
    # Nonseekable ffmpeg-shaped RIFF/data lengths, with an odd padded metadata chunk.
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVEfmt " + struct.pack("<I", 16)
            + fmt + b"JUNK" + struct.pack("<I", 3) + b"abc\0data"
            + struct.pack("<I", 0xFFFFFFFF) + x.tobytes())


def capture_runtime(tmp_path, data):
    r = runtime(tmp_path)
    r.capture = SimpleNamespace(stdout=io.BytesIO(data), poll=lambda: None)
    r.capture_file = str(tmp_path / "capture.wav")
    r.capture_status = {"state": "starting", "frames_written": 0,
                        "maximum_frames": shuttle_host.CAPTURE_MAX_FRAMES, "format_verified": False}
    r.audio_error = None
    return r


def test_future_audio_is_not_a_fresh_monitor(tmp_path):
    r = runtime(tmp_path)
    r.audio["at_ns"] = time.monotonic_ns() + 10_000_000_000
    with pytest.raises(PocketError, match="stale"):
        r._guard_monitor()


@pytest.mark.parametrize("owner", [None, "s"])
def test_topology_change_invalidates_even_an_unowned_runtime(tmp_path, owner):
    r = runtime(tmp_path)
    r.owner = owner
    r.binding = {"left": "old-source"}
    r.condition = threading.Condition(r.lock)
    r.midi = SimpleNamespace(stdout=[json.dumps({"event": "topology"})], poll=lambda: None)
    r._midi_reader()
    assert r.topology_changed and not r.binding
    with pytest.raises(PocketError, match="stale"):
        r.act({"session_id": "s", "request_id": "reclaim", "expected_revision": r.revision,
               "action": {"kind": "claim"}})


@pytest.mark.parametrize("after_baseline", [False, True])
def test_only_first_inventory_can_establish_topology_baseline(tmp_path, after_baseline):
    r = runtime(tmp_path)
    r.owner = None
    r.ready = False
    r.initial_inventory_seen = False
    r.condition = threading.Condition(r.lock)
    ready = {"event": "ready", "sources": [{"name": "DDJ-FLX4"}]}
    topology = {"event": "topology"}
    events = [ready, topology, ready] if after_baseline else [topology, ready]
    r.midi = SimpleNamespace(stdout=[json.dumps(e) for e in events], poll=lambda: None)
    r._midi_reader()
    assert r.ready and r.initial_inventory_seen
    assert r.topology_changed is after_baseline
    if after_baseline:
        with pytest.raises(PocketError, match="stale"):
            r._guard_monitor()
    else:
        assert r._guard_monitor()["finite"]


@pytest.mark.parametrize("message", [[182, 23, 40], [182, 55, 80], [176, 33, 1], [144, 10, 127]])
def test_all_controller_channel_controls_still_yield_and_invalidate_source_binding(tmp_path, message):
    r = runtime(tmp_path)
    r.binding = {"left": "fullpath-confirmed-source"}
    r.condition = threading.Condition(r.lock)
    r.physical_events = 0
    r.midi = SimpleNamespace(stdout=[json.dumps({"origin": "flx4", "bytes": message})])
    r._midi_reader()
    assert r.owner is None and not r.binding and r.cancel.exists()
    assert r.gains == {"left": None, "right": None}


@pytest.mark.parametrize("change", ["old", "future", "other-image", "missing"])
def test_observer_rejects_nonfresh_or_unmatched_captures(tmp_path, monkeypatch, change):
    r = runtime(tmp_path)
    r.build = tmp_path
    r._guard_monitor = lambda **kwargs: None

    def run(args, **kwargs):
        now = time.monotonic_ns()
        value = {"status": "ok", "started_ns": now, "completed_ns": now, "image_path": args[1]}
        if change == "old":
            value["started_ns"] = 1
        elif change == "future":
            value["completed_ns"] = now + 1_000_000_000
        elif change == "other-image":
            value["image_path"] = "a-prior-frame.png"
        else:
            del value["started_ns"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(value))

    monkeypatch.setattr(shuttle_host.subprocess, "run", run)
    with pytest.raises(PocketError, match="observation is stale"):
        r._raw_observe()


def loop_runtime(tmp_path, current=8):
    r = runtime(tmp_path)
    r.observe = lambda _: {"decks": {"left": {"track_id": "a", "loop_beats": current,
                                               "loop_state_known": True}}}
    r.feedback = {(144, 30): 127 if current == 8 else 0, (144, 31): 127 if current == 16 else 0}
    return r


def test_loop_cannot_verify_a_new_toggle_with_old_feedback(tmp_path):
    r = loop_runtime(tmp_path)
    r.feedback[(144, 31)] = 127
    r.feedback_ns[(144, 31)] = time.monotonic_ns() - 1_000_000_000
    sent = []
    r._pulse = lambda note, **kwargs: sent.append(note)
    # Isolate freshness of feedback from monitor timing.
    r._owned = lambda *args: None
    with pytest.raises(PocketError, match="post-dispatch"):
        r._execute("s", {"kind": "loop", "deck": "left", "beats": 16}, "lease")
    assert sent == [31]


def test_loop_requires_post_dispatch_feedback_and_preserves_noop_scope(tmp_path):
    r = loop_runtime(tmp_path)
    r._pulse = lambda _, **kwargs: pytest.fail("unchanged state must not toggle")
    unchanged = r._execute("s", {"kind": "loop", "deck": "left", "beats": 8}, "lease")
    assert unchanged["status"] == "unchanged" and "cached" in unchanged["verification"]

    def pulse(note, **kwargs):
        assert note == 30
        r.feedback[(144, 30)] = 0
        r.feedback_ns[(144, 30)] = time.monotonic_ns()

    r._pulse = pulse
    assert r._execute("s", {"kind": "loop", "deck": "left", "beats": None}, "lease")["status"] == "verified"


@pytest.mark.parametrize("channels,rate", [(4, 48000), (2, 44100)])
def test_capture_refuses_unqualified_negotiated_format(tmp_path, channels, rate):
    r = capture_runtime(tmp_path, wav_stream(np.zeros(80), channels=channels, rate=rate))
    r._audio_reader()
    assert r.capture_status["state"] == "failed" and r.audio is None
    assert "no implicit conversion" in r.audio_error
    assert not (tmp_path / "capture.wav").exists()


def test_partial_final_block_is_saved_with_exact_frame_count(tmp_path):
    x = np.arange(34, dtype="float32").reshape(-1, 2) / 100
    r = capture_runtime(tmp_path, wav_stream(x))
    r._audio_reader()
    assert r.capture_status["state"] == "finalized"
    assert r.capture_status["frames_written"] == 17
    assert r.capture_status["format_verified"]
    saved, rate = sf.read(r.capture_file, dtype="float32", always_2d=True)
    assert rate == 48000 and np.array_equal(saved, x)


def test_capture_exhaustion_is_explicit_and_blocks_control_while_meter_can_continue(tmp_path, monkeypatch):
    monkeypatch.setattr(shuttle_host, "CAPTURE_MAX_FRAMES", 10)
    r = capture_runtime(tmp_path, wav_stream(np.ones((15, 2), dtype="float32") / 10))
    r._audio_reader()
    assert r.capture_status["state"] == "exhausted" and r.capture_status["frames_written"] == 10
    assert len(sf.read(r.capture_file)[0]) == 10
    assert r._guard_monitor(require_capture=False)["frames"] == 15
    with pytest.raises(PocketError, match="exhausted"):
        r._guard_monitor()
    assert any(e.get("event") == "capture_exhausted" for e in r.events)


def test_capture_never_overwrites_and_never_claims_nonfinite_audio(tmp_path):
    r = capture_runtime(tmp_path, wav_stream(np.ones((2, 2))))
    (tmp_path / "capture.wav").write_bytes(b"keep-me")
    r._audio_reader()
    assert r.capture_status["state"] == "failed" and (tmp_path / "capture.wav").read_bytes() == b"keep-me"
    other = tmp_path / "other"
    other.mkdir()
    r = capture_runtime(other, wav_stream(np.full((2, 2), np.nan)))
    r._audio_reader()
    assert r.capture_status["state"] == "failed" and r.capture_status["frames_written"] == 0
    assert r.audio is None


def test_capture_refuses_truncated_frame(tmp_path):
    r = capture_runtime(tmp_path, wav_stream(np.zeros((1, 2))) + b"x")
    r._audio_reader()
    assert r.capture_status["state"] == "failed" and "incomplete stereo frame" in r.audio_error


def test_capture_finalization_error_is_failed_not_a_saved_receipt(tmp_path, monkeypatch):
    r = capture_runtime(tmp_path, wav_stream(np.zeros((2, 2))))

    class FailingWriter:
        def __init__(self, *args, **kwargs):
            pass

        def write(self, value):
            pass

        def flush(self):
            pass

        def close(self):
            raise OSError("disk finalization failed")

    monkeypatch.setattr(shuttle_host.sf, "SoundFile", FailingWriter)
    r._audio_reader()
    assert r.capture_status["state"] == "failed"
    assert any(e.get("event") == "capture_failed" for e in r.events)
    assert not any(e.get("state") == "finalized" for e in r.events)


def test_actual_offline_ffmpeg_nonseekable_float_wav_header():
    binary = shutil.which("ffmpeg")
    if not binary:
        pytest.skip("optional ffmpeg not installed")
    data = subprocess.run([binary, "-v", "error", "-f", "f32le", "-ar", "48000", "-ac", "2",
                           "-i", "pipe:0", "-c:a", "pcm_f32le", "-f", "wav", "pipe:1"],
                          input=np.zeros((17, 2), dtype="<f4").tobytes(), capture_output=True,
                          check=True, timeout=10).stdout
    stream = io.BytesIO(data)
    _master_stream_header(stream)
    assert np.array_equal(np.frombuffer(stream.read(), dtype="<f4"), np.zeros(34, dtype="float32"))


def test_replacing_same_size_same_mtime_source_invalidates_pcm_cache(tmp_path, monkeypatch):
    r = runtime(tmp_path)
    r.build = tmp_path
    source = tmp_path / "source.wav"
    sf.write(source, np.ones(480) / 10, 48000, subtype="PCM_24")
    original_stat = source.stat()
    target = {"track_id": "a", "native_path": str(source), "pcm": shuttle._pcm_identity(source), "index": 0}
    r.sessions = {"s": {"assets": [target]}}
    r.verified_pcm = {}
    r.observe = lambda _: {"decks": {"left": {"playing": False, "track_id": "a"}}}
    log = tmp_path / "DJ.INFO"
    log.write_text("")
    from pathlib import Path
    original_expand = Path.expanduser
    monkeypatch.setattr(Path, "expanduser", lambda p: log if str(p).endswith("/Logs/DJ.INFO") else original_expand(p))
    monkeypatch.setattr(shuttle_host.subprocess, "run", lambda *args, **kwargs: None)
    r._pulse = lambda _, **kwargs: log.write_text(log.read_text() + "[View] The cache `" + str(source) + "` was requested\n")
    assert r._execute("s", {"kind": "load", "deck": "left", "track_id": "a"}, "lease")["status"] == "verified"
    replacement = tmp_path / "replacement.wav"
    sf.write(replacement, np.ones(480) / 5, 48000, subtype="PCM_24")
    os.replace(replacement, source)
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert source.stat().st_size == original_stat.st_size
    r._pulse = lambda _, **kwargs: pytest.fail("changed source cannot load")
    with pytest.raises(PocketError, match="Native source audio differs"):
        r._execute("s", {"kind": "load", "deck": "left", "track_id": "a"}, "lease")


def send_runtime(tmp_path):
    r = runtime(tmp_path)
    r.send_lock = threading.Lock()
    r.condition = threading.Condition(r.lock)
    r.serial = 0
    r.acks = {}
    r.writes = []

    def write(payload):
        r.writes.extend(json.loads(line) for line in payload.splitlines())
        return len(payload)

    def flush():
        r.acks[str(r.serial)] = [0] * len([v for v in r.writes if v["request_id"] == str(r.serial)])
        r.condition.notify_all()

    r.midi = SimpleNamespace(poll=lambda: None, stdin=SimpleNamespace(write=write, flush=flush))
    return r


def test_revocation_between_action_guard_and_dispatch_cannot_write_or_restore_gain(tmp_path):
    r = send_runtime(tmp_path)
    r.observe = lambda _: {"decks": {"left": {"track_id": "a", "playing": True}}}

    class RevokeBeforeAdmission:
        def __enter__(self):
            r._yield("human reclaim before final dispatch")

        def __exit__(self, *args):
            pass

    r.send_lock = RevokeBeforeAdmission()
    with pytest.raises(PocketError, match="yielded"):
        r._execute("s", {"kind": "gain", "deck": "left", "value": 20}, "lease")
    assert r.writes == [] and r.gains["left"] is None


def test_ack_wait_allows_release_and_cannot_restore_a_revoked_gain(tmp_path):
    r = send_runtime(tmp_path)
    r.observe = lambda _: {"decks": {"left": {"track_id": "a", "playing": True}}}
    released = threading.Event()
    worker = None

    def flush():
        def revoke_and_ack():
            r._yield("human reclaim while native acknowledgement is pending")
            released.set()
            with r.condition:
                r.acks[str(r.serial)] = [0]
                r.condition.notify_all()

        nonlocal worker
        worker = threading.Thread(target=revoke_and_ack)
        worker.start()

    r.midi.stdin.flush = flush
    with pytest.raises(PocketError, match="yielded"):
        r._execute("s", {"kind": "gain", "deck": "left", "value": 20}, "lease")
    worker.join(1)
    assert released.is_set() and r.gains["left"] is None
    assert [v["send"] for v in r.writes] == [[176, 23, 20]]


def test_pulse_submits_note_off_with_admitted_note_on(tmp_path):
    r = send_runtime(tmp_path)
    r._pulse(20, session_id="s", lease="lease")
    assert [v["send"] for v in r.writes] == [[144, 20, 127], [128, 20, 0]]
    r.binding = {"left": "source-a"}
    r.binding_pid = {"left": 123}
    r._yield("explicit release")
    assert not r.binding and not r.binding_pid
    r.owner, r.lease = "s", "a-new-lease"
    assert not r.binding  # Same artist/duration after a human load cannot restore the old full-path proof.


@pytest.mark.parametrize("subtype", ["FLOAT", "DOUBLE"])
def test_fractional_float_identity_is_exact_and_old_float_identity_requires_prepare(tmp_path, subtype):
    import hashlib

    source = tmp_path / "float.wav"
    sf.write(source, np.full((20, 2), 0.1), 48000, subtype=subtype)
    identity = shuttle._pcm_identity(source)
    assert identity["representation"].endswith("/v2")
    with sf.SoundFile(source) as audio:
        legacy_header = [audio.samplerate, audio.channels, len(audio), audio.subtype]
        legacy_hash = hashlib.sha256(json.dumps(legacy_header).encode())
        legacy_hash.update(audio.read(dtype="int32", always_2d=True).astype("<i4").tobytes())
    legacy = {k: v for k, v in identity.items() if k != "representation"}
    legacy["sha256"] = legacy_hash.hexdigest()
    r = runtime(tmp_path)
    r.verified_pcm = {}
    with pytest.raises(PocketError, match="prepare again"):
        r._verified_source({"native_path": str(source), "pcm": legacy})
    sf.write(source, np.full((20, 2), 0.2), 48000, subtype=subtype)
    assert shuttle._pcm_identity(source)["sha256"] != identity["sha256"]


@pytest.mark.parametrize("subtype", ["FLOAT", "DOUBLE"])
def test_float_source_replacement_with_same_size_and_mtime_cannot_reuse_verification(tmp_path, subtype):
    source = tmp_path / "float.wav"
    sf.write(source, np.full((20, 2), 0.1), 48000, subtype=subtype)
    before = source.stat()
    target = {"native_path": str(source), "pcm": shuttle._pcm_identity(source)}
    r = runtime(tmp_path)
    r.verified_pcm = {}
    r._verified_source(target)
    replacement = tmp_path / "replacement.wav"
    sf.write(replacement, np.full((20, 2), 0.2), 48000, subtype=subtype)
    os.replace(replacement, source)
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert source.stat().st_size == before.st_size
    with pytest.raises(PocketError, match="Native source audio differs"):
        r._verified_source(target)


@pytest.mark.parametrize("subtype", ["FLOAT", "DOUBLE"])
def test_nonfinite_float_source_has_no_accepted_identity(tmp_path, subtype):
    source = tmp_path / "invalid.wav"
    sf.write(source, np.array([[0.0, np.nan], [np.inf, 0.1]]), 48000, subtype=subtype)
    with pytest.raises(PocketError, match="nonfinite"):
        shuttle._pcm_identity(source)


@pytest.mark.parametrize("when", ["navigation", "dispatch"])
def test_source_change_during_load_preparation_or_confirmation_cannot_bind(tmp_path, monkeypatch, when):
    from pathlib import Path

    r = runtime(tmp_path)
    r.build = tmp_path
    source = tmp_path / "source.wav"
    sf.write(source, np.ones(480) / 10, 48000, subtype="PCM_24")
    target = {"track_id": "a", "native_path": str(source), "pcm": shuttle._pcm_identity(source), "index": 0}
    r.sessions = {"s": {"assets": [target]}}
    r.verified_pcm = {}
    r.observe = lambda _: {"decks": {"left": {"playing": False, "track_id": "a"}}}
    log = tmp_path / "DJ.INFO"
    log.write_text("")
    original_expand = Path.expanduser
    monkeypatch.setattr(Path, "expanduser", lambda p: log if str(p).endswith("/Logs/DJ.INFO") else original_expand(p))

    def mutate():
        sf.write(source, np.ones(480) / 5, 48000, subtype="PCM_24")

    def navigate(*args, **kwargs):
        if when == "navigation":
            mutate()

    def pulse(note, **kwargs):
        if when == "navigation":
            pytest.fail("changed source must not reach load dispatch")
        mutate()
        log.write_text("[View] The cache `" + str(source) + "` was requested\n")

    monkeypatch.setattr(shuttle_host.subprocess, "run", navigate)
    r._pulse = pulse
    with pytest.raises(PocketError, match="Native source audio differs"):
        r._execute("s", {"kind": "load", "deck": "left", "track_id": "a"}, "lease")
    assert not r.binding


def test_parallel_observations_refuse_instead_of_overlapping_or_reusing_a_frame(tmp_path):
    r = runtime(tmp_path)
    began, release = threading.Event(), threading.Event()
    calls = []
    results = []

    def capture():
        calls.append("capture")
        began.set()
        assert release.wait(1)
        return {"frame": len(calls)}

    r._capture_observation = capture
    worker = threading.Thread(target=lambda: results.append(r._raw_observe()))
    worker.start()
    assert began.wait(1)
    with pytest.raises(PocketError, match="busy; no cached"):
        r._raw_observe()
    release.set()
    worker.join(1)
    assert results == [{"frame": 1}] and calls == ["capture"]
    assert r._raw_observe() == {"frame": 2}


def test_failed_observer_releases_gate_for_a_new_request(tmp_path):
    r = runtime(tmp_path)
    r._capture_observation = lambda: (_ for _ in ()).throw(TimeoutError("native capture timed out"))
    with pytest.raises(TimeoutError):
        r._raw_observe()
    r._capture_observation = lambda: {"new": True}
    assert r._raw_observe() == {"new": True}


def test_source_cache_must_match_each_prepared_sessions_expected_identity(tmp_path):
    source = tmp_path / "source.wav"
    sf.write(source, np.ones(480) / 5, 48000, subtype="PCM_24")
    stale_identity = shuttle._pcm_identity(source)
    sf.write(source, np.ones(480) / 10, 48000, subtype="PCM_24")
    current_identity = shuttle._pcm_identity(source)
    r = runtime(tmp_path)
    r.verified_pcm = {}
    current = {"native_path": str(source), "pcm": current_identity}
    stale = {"native_path": str(source), "pcm": stale_identity}
    r._verified_source(current)
    with pytest.raises(PocketError, match="Native source audio differs"):
        r._verified_source(stale)
    r._verified_source(current)
