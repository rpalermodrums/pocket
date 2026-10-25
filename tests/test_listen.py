# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import ast
import asyncio
import copy
import importlib.util
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
import typing_extensions
from conftest import UNRECORDED_BUILD_ID
from listen_fake_helper import FIXTURE_VERSION, write_fake_build, write_launcher

from pocket_music import capabilities, listen, listen_host, listen_native, listen_types
from pocket_music.artifact_store import canonical_bytes, digest, put_record, read_record
from pocket_music.error_contracts import CODES, JSON_SCHEMA_V3, error_envelope
from pocket_music.errors import PocketError, valid_hint

RATE = 48000
UID = "synthetic:duo-a"
SRC = Path(listen.__file__).resolve().parents[1]
PACKAGE = SRC / "pocket_music"
REPO = SRC.parent
AUDIO_SUFFIXES = (".wav", ".aif", ".aiff", ".caf", ".flac", ".mp3")
OTHER_EPOCH = "rt-" + "0" * 16
COVERAGE = {"physical_headphones": "not_observed", "headphone_cue": "not_observed",
            "human_listening": "not_performed", "route_semantics": "not_verified",
            "control_issued": False, "device_settings_changed": False, "audio_written_to_disk": False,
            "native_qualification": "not_established"}
MASTER_CUE_UNLABELED = ({"route_id": "master", "role": "master", "channels": [0, 1]},
                        {"route_id": "cue", "role": "cue", "channels": [2]},
                        {"route_id": "booth", "role": "unlabeled", "channels": [3]})
LISTEN_PROVIDERS = ("listen_build_helper", "listen_devices", "listen_prepare", "listen_start",
                    "listen_observe", "listen_events", "listen_stop", "listen_request_status")
LISTEN_GATES = ("listen_calibrate", "listen_capture_window", "listen_analyze", "listen_record_start",
                "listen_record_stop")
RUNTIME_ROWS = ("listen_devices", "listen_start", "listen_observe", "listen_events", "listen_stop")


def tone(frames, tones=((0, -12.0), (1, -12.0))):
    return {"kind": "tone", "frames": frames,
            "tones": [{"channel": c, "hz": 480.0, "dbfs": dbfs} for c, dbfs in tones]}


def script(*segments, rate=RATE, channels=4):
    return {"block_frames": 480,
            "devices": [{"uid": UID, "name": "Practice Duo", "input_channels": channels, "sample_rate": rate},
                        {"uid": "synthetic:duo-b", "name": "Practice Duo", "input_channels": 2,
                         "sample_rate": RATE}],
            "segments": list(segments) or [tone(RATE * 3)]}


def make_config(spec=None, routes=MASTER_CUE_UNLABELED[:2], **sections):
    return {"adapter": {"kind": "synthetic", "script": spec or script()},
            "device": {"uid": UID, "expected_sample_rate": RATE},
            "routes": [copy.deepcopy(r) for r in routes],
            "measurement": {"clip_threshold_dbfs": -0.1, "near_silence": {"level_dbfs": -60.0,
                                                                          "min_duration_ms": 2000}},
            "limits": {"max_runtime_seconds": 60}, **sections}


def raised(call):
    with pytest.raises(PocketError) as caught:
        call()
    return caught.value


def files_under(root):
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*") if p.is_file())


def wait_until(predicate, seconds=20.0, interval=0.05):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("timed out waiting for the Listen runtime")


def forbid_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("this provider must not run a process")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def stop_running(session_dir, timeout_s=60.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        current = listen_host.current_runtime(session_dir)
        if current["state"] not in ("running", "unreachable"):
            return
        try:
            stopped = listen.listen_stop(session_dir, "cleanup-" + secrets.token_hex(4), current["runtime_epoch"])
        except PocketError:
            stopped = {}
        if not stopped.get("lease_released"):
            time.sleep(0.5)
    current = listen_host.current_runtime(session_dir)
    if current["state"] in ("running", "unreachable"):
        newest = listen_host.newest_runtime(session_dir)
        stderr = None if newest is None else Path(newest[2]) / listen_host.STDERR_NAME
        tail = (stderr.read_bytes()[-2000:].decode(errors="replace") if stderr is not None and stderr.exists()
                else "(none)")
        pytest.fail(f"the Listen runtime in {session_dir} is still running after {timeout_s:.0f} s: {current}\n"
                    f"host.stderr tail:\n{tail}")


def sealed_stop_status_allowing_journal_close_partial(stopped):
    assert stopped["terminal_sealed"] is True and stopped["stop_outcome"] == "stopped", stopped
    if stopped["status"] != "ok":
        journal = stopped["terminal"]["journal"]
        assert stopped["status"] == "partial" and len(stopped["next_actions"]) == 2, stopped
        assert stopped["next_actions"][0].startswith("The runtime journal didn't keep every line"), stopped
        assert journal["closed"] is False and journal["lines_unwritten_at_close"] >= 1, journal
    return stopped["status"]


def lease_released_soon(session_dir, stopped, seconds=60.0):
    if not stopped["lease_released"]:
        assert "still holds its lease" in stopped["next_actions"][-1], stopped
        wait_until(lambda: not listen_host.lease_held(session_dir), seconds)
    return True


def test_teardown_fails_loudly_when_a_runtime_cant_be_stopped(tmp_path, monkeypatch):
    session_dir = listen.listen_prepare(str(tmp_path / "session"), make_config())["session"]["session_dir"]
    monkeypatch.setattr(listen_host, "current_runtime",
                        lambda root: {"state": "unreachable", "runtime_epoch": OTHER_EPOCH})

    def unconfirmed(*args):
        raise PocketError("The stop got no reply")
    monkeypatch.setattr(listen, "listen_stop", unconfirmed)
    with pytest.raises(pytest.fail.Exception, match="is still running after 1 s"):
        stop_running(session_dir, timeout_s=1.0)


class Runtimes:
    def __init__(self, base):
        self.base = base
        self.sessions = []

    def prepare(self, config=None, name="session"):
        result = listen.listen_prepare(str(self.base / name), config or make_config())
        session_dir = result["session"]["session_dir"]
        self.sessions.append(session_dir)
        return session_dir, result["session"]["config_sha256"]

    def start(self, config=None, name="session", request_id="start-1"):
        session_dir, sha = self.prepare(config, name)
        return session_dir, sha, listen.listen_start(session_dir, request_id, sha)

    def cleanup(self):
        for session_dir in self.sessions:
            stop_running(session_dir)


@pytest.fixture
def runtimes(tmp_path):
    r = Runtimes(tmp_path)
    yield r
    r.cleanup()


def _signals(events, route_id):
    return [e for e in events if e["route_id"] == route_id and e["evidence"]["type"] == "signal_level"]


@pytest.fixture(scope="module")
def walkthrough(tmp_path_factory):
    base = tmp_path_factory.mktemp("listen-walkthrough")
    spec = script(tone(RATE * 4, tones=((0, -12.0), (1, -12.0), (2, -6.0), (3, -30.0))))
    config = make_config(spec, MASTER_CUE_UNLABELED, delivery={"meter_coalesce_windows": 2})
    receipts, errors = {}, {}
    receipts["devices"] = listen.listen_devices(config["adapter"])
    receipts["prepare"] = listen.listen_prepare(str(base / "session"), config)
    session_dir = receipts["prepare"]["session"]["session_dir"]
    sha = receipts["prepare"]["session"]["config_sha256"]
    receipts["observe_before"] = listen.listen_observe(session_dir)
    receipts["start"] = listen.listen_start(session_dir, "start-1", sha)
    events = []
    try:
        assert receipts["start"].get("start_outcome") == "capturing", receipts["start"]
        epoch = receipts["start"]["runtime"]["runtime_epoch"]
        runtime_dir = Path(session_dir) / receipts["start"]["runtime"]["runtime_dir"]

        def metered():
            observed = listen.listen_observe(session_dir, expected_runtime_epoch=epoch)
            rows = {r["route_id"]: r for r in observed["observation"]["routes"]}
            return observed if rows["master"]["signal"] and rows["booth"]["signal"] else None
        receipts["observe"] = wait_until(metered)
        receipts["observe_other_epoch"] = listen.listen_observe(session_dir,
                                                                expected_runtime_epoch=OTHER_EPOCH)
        cursor = receipts["start"]["cursor"]
        deadline = time.monotonic() + 20
        while not (_signals(events, "master") and _signals(events, "booth")):
            assert time.monotonic() < deadline, "no meters for both metered routes"
            page = listen.listen_events(session_dir, cursor, max_events=32, wait_ms=200)
            receipts.setdefault("events", page)
            events += page["events"]
            cursor = page["next_after"]
        receipts["events_health_only"] = listen.listen_events(session_dir, receipts["start"]["cursor"],
                                                              routes=["cue"], kinds=["health"])
        receipts["events_aged"] = listen.listen_events(session_dir, receipts["start"]["cursor"],
                                                       max_evidence_age_ms=1)
        errors["cursor_ahead"] = raised(lambda: listen.listen_events(
            session_dir, {"runtime_epoch": epoch, "sequence": 10 ** 9}))
        errors["control_op"] = raised(lambda: listen_host.call(runtime_dir, "act"))
        errors["stale_stop"] = raised(lambda: listen.listen_stop(session_dir, "stop-stale", OTHER_EPOCH))
        receipts["observe_after_stale_stop"] = listen.listen_observe(session_dir)
    finally:
        if receipts["start"].get("start_outcome") == "capturing":
            receipts["stop"] = listen.listen_stop(session_dir, "stop-1", epoch)
        else:
            stop_running(session_dir)
    receipts["stop_replay"] = listen.listen_stop(session_dir, "stop-1", epoch)
    receipts["status_start"] = listen.listen_request_status(session_dir, "start-1")
    receipts["status_stop"] = listen.listen_request_status(session_dir, "stop-1")
    receipts["observe_after"] = listen.listen_observe(session_dir)
    receipts["events_after"] = listen.listen_events(session_dir, receipts["start"]["cursor"])
    return {"receipts": receipts, "errors": errors, "events": events, "session_dir": session_dir, "sha": sha,
            "epoch": epoch, "runtime_dir": runtime_dir}


def test_prepare_seals_resolved_config_and_memory_cost_without_any_subprocess(tmp_path, monkeypatch):
    forbid_processes(monkeypatch)
    config = make_config()
    config["measurement"] = {"clip_threshold_dbfs": -1,
                             "near_silence": {"level_dbfs": -60, "min_duration_ms": 2000}}
    del config["limits"]
    result = listen.listen_prepare(str(tmp_path / "session"), config)
    root = tmp_path / "session"
    assert result["status"] == "ok"
    handle = result["artifacts"]["session"]
    record = read_record(handle, root, "pocket.listen-session/v1")
    resolved = record["config"]
    assert result["session"]["settings"] == {k: resolved[k] for k in ("device", "measurement", "buffering",
                                                                      "health", "delivery", "limits")}
    assert resolved["measurement"] == {"clip_threshold_dbfs": -1.0, "window_frames": RATE // 10,
                                       "near_silence": {"level_dbfs": -60.0, "min_duration_ms": 2000}}
    assert isinstance(resolved["measurement"]["clip_threshold_dbfs"], float)
    assert resolved["buffering"] == {"ring_seconds": 10} and resolved["health"] == {"stale_after_ms": 500}
    assert resolved["delivery"] == {"meter_coalesce_windows": 5, "normal_retention_events": 8192,
                                    "normal_retention_bytes": 4194304, "health_retention_events": 1024,
                                    "health_retention_bytes": 524288}
    assert resolved["limits"] == {"max_runtime_seconds": 14400}
    assert record["config_sha256"] == digest(resolved) == result["session"]["config_sha256"]
    floats = make_config()
    floats["measurement"]["clip_threshold_dbfs"] = -1.0
    del floats["limits"]
    again = listen.listen_prepare(str(tmp_path / "again"), floats)
    assert again["session"]["config_sha256"] == record["config_sha256"]
    assert record["memory"] == {"pcm_ring_bytes": 10 * RATE * 2 * 4, "helper_ring_bytes": None,
                                "adapter_queue_max_bytes": None, "descriptor_ring_bytes": 4096 * 64,
                                "measurement_scratch_bytes": RATE // 10 * 2 * 4,
                                "broker_max_bytes": 4194304 + 524288,
                                "total_declared_bytes": 10 * RATE * 2 * 4 + 4096 * 64 + RATE // 10 * 2 * 4
                                + 4194304 + 524288,
                                "not_in_total": {"journal_queue_lines": 4096, "journal_line_max_bytes": 9216,
                                                 "facts_queue_items": 1024,
                                                 "python_object_overhead": "not_counted"}}
    assert record["disk"] == {"journal_budget_bytes": 16777216, "host_stderr_cap_bytes": 1048576,
                              "scope": "per_runtime",
                              "per_runtime_total_declared_bytes": 16777216 + 1048576, "runtimes_max": 9999,
                              "not_in_total": {"runtime_directory_records": [
                                  "launch-request.json", "startup-failure.json", "launch-request.json.sha256",
                                  "startup-failure.json.sha256", "connection.json", "terminal-handle.json"],
                                  "session_records": ["terminal_record", "start_and_stop_request_journals"]}}
    assert result["session"]["memory"] == record["memory"] and result["session"]["disk"] == record["disk"]
    assert record["metered_channels"] == [0, 1] and record["build"] is None
    assert record["retention"] == result["session"]["retention"] == "in_memory_bounded_window_no_file"
    assert record["adapter_profile"] == "listen-synthetic/v1"
    assert files_under(root) == sorted(["listen-session.json", "runtime.lock", handle["artifact_uri"]])
    assert os.stat(root / "runtime.lock").st_mode & 0o777 == 0o600
    assert listen_host.load_session(root)[2] == record
    assert [n["code"] for n in result["notices"]] == ["master_label_untested", "cue_not_established",
                                                      "synthetic_audio"]
    assert result["coverage"]["capture"] == "not_started" and result["coverage"]["device_opened"] is False
    assert result["next_actions"] == [("listen_start with session_dir, a new request_id and "
                                       f"expected_config_sha256 {record['config_sha256']}")]


@pytest.mark.parametrize("path", [("clip_threshold_dbfs",), ("near_silence",), ("near_silence", "level_dbfs"),
                                  ("near_silence", "min_duration_ms")])
def test_prepare_requires_explicit_clip_threshold_and_near_silence_level_and_duration(tmp_path, path):
    config = make_config()
    parent = config["measurement"]
    for key in path[:-1]:
        parent = parent[key]
    del parent[path[-1]]
    error = raised(lambda: listen.listen_prepare(str(tmp_path / "session"), config))
    assert error.code == "invalid_request"
    assert "is required" in error.hint and "no default" in error.hint
    assert not (tmp_path / "session").exists()


@pytest.mark.parametrize("routes", [
    [{"route_id": "master", "role": "master", "channels": [0, 1]},
     {"route_id": "booth", "role": "unlabeled", "channels": [1, 2]}],
    [{"route_id": "master", "role": "master", "channels": [0, 1]},
     {"route_id": "cue", "role": "cue", "channels": [1]}],
])
def test_prepare_refuses_overlapping_channels_one_channel_one_role(tmp_path, routes):
    error = raised(lambda: listen.listen_prepare(str(tmp_path / "session"), make_config(routes=routes)))
    assert error.code == "invalid_request"
    assert error.hint == listen._OVERLAP_HINT
    assert not (tmp_path / "session").exists()


def _route(route_id, role, channels):
    return {"route_id": route_id, "role": role, "channels": channels}


@pytest.mark.parametrize("routes,needle", [
    ([_route("master", "master", [0]), _route("main", "master", [1])], "at most one master"),
    ([_route("master", "master", [0]), _route("cue", "cue", [1]), _route("cue-b", "cue", [2])],
     "at most one master"),
    ([_route("a", "unlabeled", [0]), _route("b", "unlabeled", [1]), _route("c", "unlabeled", [2])],
     "two unlabeled"),
    ([_route("cue", "cue", [0, 1])], "master or unlabeled"),
    ([_route("cue-left", "unlabeled", [0])], "only for a route with that role"),
    ([_route("master-bus", "unlabeled", [0])], "only for a route with that role"),
    ([_route("booth", "master", [0]), _route("cue-master", "cue", [1])], "only for a route with that role"),
    ([_route("cue1", "unlabeled", [0])], "only for a route with that role"),
    ([_route("deck-cuemix", "unlabeled", [0])], "only for a route with that role"),
    ([_route("cue2", "master", [0])], "only for a route with that role"),
    ([_route("master2", "unlabeled", [0])], "only for a route with that role"),
    ([_route("Master", "master", [0])], "lowercase"),
    ([_route("9th", "master", [0])], "lowercase"),
    ([_route("master", "master", [0]), _route("booth", "unlabeled", [1]), _route("booth", "unlabeled", [2])],
     "own route_id"),
    ([_route("master", "master", [0]), _route("a", "unlabeled", [1]), _route("b", "unlabeled", [2]),
      _route("cue", "cue", [3]), _route("c", "unlabeled", [4])], "1 to 4 audio routes"),
    ([_route("master", "master", [0, 1]), _route("a", "unlabeled", [2, 3]), _route("b", "unlabeled", [4])],
     "at most 4 channels"),
    ([_route("master", "master", [0, 1, 2])], "1 or 2 distinct"),
    ([_route("master", "master", [0, 0])], "1 or 2 distinct"),
    ([_route("master", "master", [64])], "from 0 to 63"),
    ([_route("master", "master", [True])], "1 or 2 distinct"),
    ([_route("master", "master", [[0]])], "1 or 2 distinct"),
    ([_route("master", "master", [0, "a"])], "1 or 2 distinct"),
    ([{"route_id": "master", "role": ["master"], "channels": [0]}], "master, cue or unlabeled"),
    ([_route("master", "deck", [0])], "master, cue or unlabeled"),
    ([{"route_id": "master", "role": "master", "channels": [0], "label": "main"}], "unknown label"),
    ([], "1 to 4 audio routes"),
])
def test_prepare_route_rules_roles_counts_and_role_words_in_route_ids(tmp_path, routes, needle):
    error = raised(lambda: listen.listen_prepare(str(tmp_path / "session"), make_config(routes=routes)))
    assert error.code == "invalid_request" and needle in error.hint
    assert not (tmp_path / "session").exists()


def test_prepare_allows_four_single_channel_routes_across_every_role(tmp_path):
    routes = [_route("master", "master", [0]), _route("cue", "cue", [1]), _route("booth", "unlabeled", [2]),
              _route("room", "unlabeled", [3])]
    result = listen.listen_prepare(str(tmp_path / "session"), make_config(routes=routes))
    assert result["session"]["metered_channels"] == [0, 2, 3]
    assert [r["metering"] for r in result["session"]["routes"]] == ["metered", "withheld_until_routing_test",
                                                                    "metered", "metered"]
    named = [_route("master2", "master", [0]), _route("cue1", "cue", [1]), _route("rescue", "unlabeled", [2])]
    assert listen.listen_prepare(str(tmp_path / "named"), make_config(routes=named))["status"] == "ok"


@pytest.mark.parametrize("device", [{"name": "Practice Duo", "expected_sample_rate": RATE},
                                    {"uid": UID, "name": "Practice Duo", "expected_sample_rate": RATE}])
def test_prepare_has_no_name_based_device_selection(tmp_path, device):
    config = {**make_config(), "device": device}
    error = raised(lambda: listen.listen_prepare(str(tmp_path / "session"), config))
    assert error.code == "invalid_request" and error.hint == listen._NAME_HINT
    assert not (tmp_path / "session").exists()


def _set(path, value):
    def change(config):
        parent = config
        for key in path[:-1]:
            parent = parent.setdefault(key, {})
        parent[path[-1]] = value
    return change


@pytest.mark.parametrize("change,needle", [
    (_set(("measurement", "clip_threshold_dbfs"), True), "measurement.clip_threshold_dbfs"),
    (_set(("measurement", "clip_threshold_dbfs"), 0.5), "-20 to 0 dBFS"),
    (_set(("measurement", "clip_threshold_dbfs"), -21), "-20 to 0 dBFS"),
    (_set(("measurement", "clip_threshold_dbfs"), float("nan")), "finite number"),
    (_set(("measurement", "clip_threshold_dbfs"), float("-inf")), "finite number"),
    (_set(("measurement", "clip_threshold_dbfs"), 10 ** 400), "-20 to 0 dBFS"),
    (_set(("measurement", "clip_threshold_dbfs"), -10 ** 400), "-20 to 0 dBFS"),
    (_set(("measurement", "near_silence", "level_dbfs"), 10 ** 400), "-120 to -20 dBFS"),
    (_set(("measurement", "near_silence", "level_dbfs"), -10.0), "-120 to -20 dBFS"),
    (_set(("measurement", "near_silence", "min_duration_ms"), 99), "100 to 600000"),
    (_set(("measurement", "near_silence", "min_duration_ms"), 2000.0), "100 to 600000"),
    (_set(("measurement", "near_silence", "extra"), 1), "unknown extra"),
    (_set(("measurement", "window_frames"), 31), "32 to 48000"),
    (_set(("measurement", "window_frames"), RATE + 1), "32 to 48000"),
    (lambda c: (_set(("buffering", "ring_seconds"), 1)(c), _set(("measurement", "window_frames"), RATE)(c)),
     "quarter of the ring"),
    (_set(("buffering", "ring_seconds"), 0), "1 to 60"),
    (_set(("buffering", "ring_seconds"), True), "1 to 60"),
    (_set(("health", "stale_after_ms"), 99), "100 to 5000"),
    (_set(("delivery", "normal_retention_events"), 255), "256 to 65536"),
    (_set(("delivery", "health_retention_bytes"), 32767), "32768 to 4194304"),
    (_set(("delivery", "meter_coalesce_windows"), 51), "1 to 50"),
    (lambda c: (_set(("measurement", "window_frames"), 64)(c),
                _set(("delivery", "meter_coalesce_windows"), 7)(c)),
     "at least 10 ms of audio (480 frames at 48000 Hz)"),
    (lambda c: (_set(("measurement", "window_frames"), RATE)(c),
                _set(("delivery", "meter_coalesce_windows"), 50)(c)),
     "at most health.stale_after_ms of audio (24000 frames at 48000 Hz)"),
    (_set(("delivery", "meter_coalesce_windows"), 6), "at most health.stale_after_ms of audio"),
    (_set(("health", "stale_after_ms"), 499), "at most health.stale_after_ms of audio (23952 frames"),
    (_set(("limits", "max_runtime_seconds"), 9), "10 to 43200"),
    (_set(("limits", "max_runtime_seconds"), 43201), "10 to 43200"),
    (_set(("recording",), {}), "unknown recording"),
    (_set(("device", "expected_sample_rate"), 22050), "44100, 48000"),
    (_set(("device", "expected_sample_rate"), "48000"), "44100, 48000"),
    (_set(("device", "uid"), "synthetic:\x07bell"), "printable"),
    (_set(("adapter", "kind"), "avfoundation"), "synthetic or coreaudio"),
    (_set(("adapter", "kind"), ["synthetic"]), "synthetic or coreaudio"),
    (_set(("adapter",), {"kind": "coreaudio", "build_id": 7}), "adapter.build_id must be the build_id"),
    (_set(("adapter",), {"kind": "coreaudio", "build_id": "../builds"}), "adapter.build_id must be the build_id"),
    (_set(("adapter",), {"kind": "coreaudio", "build_dir": "/Users/me/build"}), "missing build_id"),
    (_set(("adapter",), {"kind": "coreaudio", "build_id": UNRECORDED_BUILD_ID,
                         "build_dir": "/Users/me/build"}),
     "unknown build_dir; it takes kind, build_id"),
    (lambda c: (c["adapter"]["script"].update(block_frames=8192), _set(("health", "stale_after_ms"), 300)(c)),
     "more than twice the block period: 8192-frame blocks at 48000 Hz arrive 170.7 ms apart"),
])
def test_prepare_refuses_existing_destination_bounds_bools_and_nonfinite_values(tmp_path, change, needle):
    config = make_config()
    change(config)
    error = raised(lambda: listen.listen_prepare(str(tmp_path / "session"), config))
    assert error.code == "invalid_request" and needle in error.hint and valid_hint(error.hint)
    assert not (tmp_path / "session").exists()


def test_prepare_is_refused_off_posix_before_any_directory_is_made(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(listen.os, "name", "nt")
        error = raised(lambda: listen.listen_prepare(str(tmp_path / "session"), make_config()))
    assert error.code == "unsupported_profile" and error.hint == listen._POSIX_HINT
    assert not (tmp_path / "session").exists()


def test_prepare_never_reuses_or_overwrites_a_directory(tmp_path):
    existing = tmp_path / "session"
    existing.mkdir()
    (existing / "notes.txt").write_text("keep")
    error = raised(lambda: listen.listen_prepare(str(existing), make_config()))
    assert error.code == "io_error" and error.hint == listen._DIRECTORY_HINT
    assert files_under(existing) == ["notes.txt"]
    session = listen.listen_prepare(str(tmp_path / "new"), make_config())["session"]["session_dir"]
    error = raised(lambda: listen.listen_prepare(session, make_config()))
    assert error.code == "io_error"


def test_synthetic_uid_namespace_is_exclusive_to_the_synthetic_adapter(tmp_path):
    config = make_config()
    config["device"]["uid"] = "BlackHole2ch_UID"
    error = raised(lambda: listen.listen_prepare(str(tmp_path / "a"), config))
    assert error.hint == listen._NAMESPACE_HINT
    config = {**make_config(), "adapter": {"kind": "coreaudio", "build_id": UNRECORDED_BUILD_ID}}
    error = raised(lambda: listen.listen_prepare(str(tmp_path / "b"), config))
    assert error.hint == listen._NAMESPACE_HINT
    spec = script()
    spec["devices"][0]["uid"] = "duo-a"
    error = raised(lambda: listen.listen_prepare(str(tmp_path / "c"), make_config(spec)))
    assert "synthetic:" in error.hint
    config = make_config()
    config["device"]["uid"] = "synthetic:not-in-script"
    error = raised(lambda: listen.listen_prepare(str(tmp_path / "d"), config))
    assert "adapter.script.devices" in error.hint
    assert not any((tmp_path / name).exists() for name in "abcd")


def test_coreaudio_adapter_is_refused_off_macos_before_any_build_is_read(tmp_path, monkeypatch):
    monkeypatch.setattr(listen.sys, "platform", "linux")
    config = {**make_config(), "adapter": {"kind": "coreaudio", "build_id": UNRECORDED_BUILD_ID},
              "device": {"uid": "BlackHole2ch_UID", "expected_sample_rate": RATE}}
    error = raised(lambda: listen.listen_prepare(str(tmp_path / "session"), config))
    assert error.code == "unsupported_profile" and "synthetic adapter" in error.hint
    error = raised(lambda: listen.listen_devices(config["adapter"]))
    assert error.code == "unsupported_profile"
    assert not (tmp_path / "session").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="the CoreAudio adapter is macOS only")
def test_coreaudio_prepare_and_start_refuse_a_changed_helper_build(tmp_path, monkeypatch):
    build_id, build = write_fake_build()
    config = {**make_config(), "adapter": {"kind": "coreaudio", "build_id": build_id},
              "device": {"uid": "fake:duo-a", "expected_sample_rate": RATE}}
    prepared = listen.listen_prepare(str(tmp_path / "session"), config)
    session = prepared["session"]
    assert session["adapter_profile"] == "coreaudio-hal-input/v1"
    verified = listen_native.verify_build(build_id)
    assert session["build"] == {k: verified[k] for k in ("build_id", "build_json_sha256", "helper_sha256")}
    assert session["memory"]["helper_ring_bytes"] == listen_native.helper_ring_frames(RATE) * 2 * 4
    assert session["memory"]["adapter_queue_max_bytes"] == 256 * 8192 * 2 * 4
    assert session["disk"]["per_runtime_total_declared_bytes"] == 16777216 + 1048576
    assert prepared["coverage"]["audio_source"] == "none"
    helper = build / listen_native.HELPER_NAME
    helper.write_bytes(helper.read_bytes() + b"#\n")
    error = raised(lambda: listen.listen_prepare(str(tmp_path / "again"), config))
    assert error.code == "evidence_mismatch" and "listen_build_helper" in error.hint
    forbid_processes(monkeypatch)
    error = raised(lambda: listen.listen_start(session["session_dir"], "start-1", session["config_sha256"]))
    assert error.code == "evidence_mismatch"
    assert not (Path(session["session_dir"]) / "requests").exists()
    assert not (Path(session["session_dir"]) / "runtimes").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="the CoreAudio adapter is macOS only")
def test_coreaudio_providers_run_only_helper_builds_listen_recorded(tmp_path, monkeypatch):
    marker = tmp_path / "helper-ran"
    stray_id, _ = write_fake_build(register=False, marker=marker)
    device = {"uid": "fake:duo-a", "expected_sample_rate": RATE}
    for build_id in (stray_id, UNRECORDED_BUILD_ID):
        adapter = {"kind": "coreaudio", "build_id": build_id}
        for call in (lambda adapter=adapter: listen.listen_devices(adapter),
                     lambda adapter=adapter: listen.listen_prepare(
                         str(tmp_path / "s"), {**make_config(), "adapter": adapter, "device": device})):
            error = raised(call)
            assert error.code == "evidence_mismatch" and "has no helper build" in str(error)
            assert "build_id" in error.hint and valid_hint(error.hint)
    build_id, build = write_fake_build()
    named_by_path = {"kind": "coreaudio", "build_dir": str(build)}
    for call in (lambda: listen.listen_devices(named_by_path),
                 lambda: listen.listen_prepare(str(tmp_path / "s"), {**make_config(), "adapter": named_by_path,
                                                                     "device": device})):
        error = raised(call)
        assert error.code == "invalid_request" and "build_id" in error.hint
    assert not (tmp_path / "s").exists() and not marker.exists()
    config = {**make_config(), "adapter": {"kind": "coreaudio", "build_id": build_id}, "device": device}
    session = listen.listen_prepare(str(tmp_path / "session"), config)["session"]
    assert session["build"]["build_id"] == build_id and "build_dir" not in session["build"]
    write_launcher(build / listen_native.HELPER_NAME, marker=marker)
    (build / "build.json").unlink()
    (build / "build.json.sha256").unlink()
    listen_native._seal_build(build, FIXTURE_VERSION)
    assert listen_native.file_sha256(build / listen_native.HELPER_NAME) != session["build"]["helper_sha256"]
    error = raised(lambda: listen.listen_start(session["session_dir"], "start-1", session["config_sha256"]))
    assert error.code == "evidence_mismatch"
    assert not (Path(session["session_dir"]) / "requests").exists()
    assert not (Path(session["session_dir"]) / "runtimes").exists() and not marker.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="the CoreAudio adapter is macOS only")
def test_a_relative_registry_folder_is_the_same_registry_for_the_runtime(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("POCKET_LISTEN_DIR", "relative-registry")
    build_id, _ = write_fake_build()
    assert (work / "relative-registry" / listen_native.REGISTRY_KEY_NAME).is_file()
    config = {**make_config(), "adapter": {"kind": "coreaudio", "build_id": build_id},
              "device": {"uid": "fake:duo-a", "expected_sample_rate": RATE}}
    session = listen.listen_prepare(str(tmp_path / "session"), config)["session"]
    started = listen.listen_start(session["session_dir"], "start-1", session["config_sha256"])
    epoch = (started.get("runtime") or {}).get("runtime_epoch")
    try:
        assert started["start_outcome"] in ("capturing", "started_no_audio_yet"), started.get("startup_failure")
    finally:
        if epoch is not None:
            assert listen.listen_stop(session["session_dir"], "stop-1", epoch)["terminal_sealed"] is True


@pytest.mark.skipif(sys.platform != "darwin", reason="the CoreAudio adapter is macOS only")
def test_a_completed_start_replays_after_its_helper_build_changed(tmp_path):
    build_id, build = write_fake_build()
    config = {**make_config(), "adapter": {"kind": "coreaudio", "build_id": build_id},
              "device": {"uid": "fake:duo-a", "expected_sample_rate": RATE}}
    session = listen.listen_prepare(str(tmp_path / "session"), config)["session"]
    session_dir, sha = session["session_dir"], session["config_sha256"]
    started = listen.listen_start(session_dir, "start-1", sha)
    epoch = (started.get("runtime") or {}).get("runtime_epoch")
    try:
        assert started["start_outcome"] in ("capturing", "started_no_audio_yet"), started.get("startup_failure")
        helper = build / listen_native.HELPER_NAME
        helper.write_bytes(helper.read_bytes() + b"#\n")
        replay = listen.listen_start(session_dir, "start-1", sha)
        assert replay["replayed"] is True and replay["runtime"] == started["runtime"]
        assert replay["current_runtime"] == {"state": "running", "runtime_epoch": epoch}
        assert raised(lambda: listen.listen_start(session_dir, "start-2", sha)).code == "evidence_mismatch"
    finally:
        if epoch is not None:
            assert listen.listen_stop(session_dir, "stop-1", epoch)["terminal_sealed"] is True


def test_start_refuses_a_changed_session_or_a_stale_config_sha_before_journaling(tmp_path, monkeypatch):
    prepared = listen.listen_prepare(str(tmp_path / "session"), make_config())
    root = Path(prepared["session"]["session_dir"])
    forbid_processes(monkeypatch)
    error = raised(lambda: listen.listen_start(str(root), "start-1", "0" * 64))
    assert error.code == "stale_revision" and error.hint == listen._SHA_HINT
    record = root / prepared["artifacts"]["session"]["artifact_uri"]
    record.write_bytes(record.read_bytes().replace(b'"master"', b'"masters"', 1))
    error = raised(lambda: listen.listen_start(str(root), "start-2", prepared["session"]["config_sha256"]))
    assert error.code == "evidence_mismatch" and "listen_prepare created" in error.hint
    error = raised(lambda: listen.listen_start(str(tmp_path / "nowhere"), "start-3", "0" * 64))
    assert error.code == "evidence_mismatch"
    assert not (root / "requests").exists() and not (root / "runtimes").exists()


def test_devices_lists_synthetic_inventory_by_uid_and_flags_shared_names_without_a_stream(monkeypatch):
    forbid_processes(monkeypatch)
    result = listen.listen_devices({"kind": "synthetic", "script": script()})
    assert result["status"] == "ok" and result["adapter_profile"] == "listen-synthetic/v1"
    assert [row["uid"] for row in result["devices"]] == [UID, "synthetic:duo-b"]
    assert all(row["display_name_shared"] and row["name_shared_with"] == 1 for row in result["devices"])
    assert {row["profile_fit"] for row in result["devices"]} == {"supported"}
    assert result["microphone_authorization"] == "not_applicable" and result["devices_omitted"] == 0
    assert [n["code"] for n in result["notices"]] == ["display_name_shared", "synthetic_audio"]
    assert result["coverage"]["stream_opened"] is False and result["coverage"]["helper_executed"] is False
    assert result["coverage"]["audio_source"] == "synthetic_script"
    error = raised(lambda: listen.listen_devices({"kind": "synthetic", "script": script(), "name": "Duo"}))
    assert error.code == "invalid_request"


@pytest.mark.skipif(sys.platform != "darwin", reason="the CoreAudio adapter is macOS only")
def test_coreaudio_devices_run_only_the_sealed_list_mode_and_flag_missing_permission(tmp_path):
    build_id, _ = write_fake_build("--authorization", "denied")
    result = listen.listen_devices({"kind": "coreaudio", "build_id": build_id})
    assert result["status"] == "ok" and result["adapter_profile"] == "coreaudio-hal-input/v1"
    rows = {row["uid"]: row for row in result["devices"]}
    assert rows["fake:duo-a"]["display_name_shared"] and rows["fake:duo-a"]["profile_fit"] == "supported"
    assert rows["fake:int"]["profile_fit"] == "unsupported:not_float32"
    assert rows["fake:duo-a"]["supported_channels"] == [0, 1, 2, 3]
    assert rows["fake:int"]["supported_channels"] == []
    assert result["microphone_authorization"] == "denied"
    assert {n["code"] for n in result["notices"]} == {"display_name_shared", "microphone_permission_needed"}
    (permission,) = [n for n in result["notices"] if n["code"] == "microphone_permission_needed"]
    assert "Turn microphone access on" in permission["next_step"]
    assert result["coverage"]["helper_executed"] is True and result["coverage"]["stream_opened"] is False
    assert result["coverage"]["audio_source"] == "none"
    assert result["build"] == {k: listen_native.verify_build(build_id)[k]
                               for k in ("build_id", "build_json_sha256", "helper_sha256")}
    assert result["devices_unreadable"] == 0


@pytest.mark.skipif(sys.platform != "darwin", reason="the CoreAudio adapter is macOS only")
def test_devices_whose_stream_configuration_cant_be_read_are_counted_not_silently_dropped(tmp_path):
    build_id, _ = write_fake_build("--list-unreadable", "2")
    result = listen.listen_devices({"kind": "coreaudio", "build_id": build_id})
    assert result["status"] == "ok" and result["devices_unreadable"] == 2 and result["devices_omitted"] == 0
    (unreadable,) = [n for n in result["notices"] if n["code"] == "devices_unreadable"]
    assert unreadable["severity"] == "warning" and "of 2 devices" in unreadable["text"]


def test_full_synthetic_walkthrough_seals_a_terminal_and_writes_no_audio(walkthrough):
    receipts = walkthrough["receipts"]
    assert receipts["observe_before"]["status"] == "needs_input"
    assert receipts["observe_before"]["runtime"] == {"state": "never_started", "runtime_epoch": None}
    start = receipts["start"]
    assert start["status"] == "ok" and start["start_outcome"] == "capturing" and start["replayed"] is False
    assert start["cursor"] == {"runtime_epoch": walkthrough["epoch"], "sequence": 0}
    assert start["runtime"]["schema"] == "pocket.listen-runtime/v1" and start["runtime"]["pid"] > 0
    assert start["runtime"]["runtime_dir"].startswith("runtimes/0001-")
    assert start["capture"]["state"] == "running" and start["capture"]["device"]["uid"] == UID
    assert start["capture"]["clocks"]["drift"] == "not_estimated"
    assert start["artifacts"] == {} and start["previous_runtime"] is None
    assert start["device_instance_changed_since_previous_runtime"] == "unknown"
    stop = receipts["stop"]
    status = sealed_stop_status_allowing_journal_close_partial(stop)
    assert lease_released_soon(walkthrough["session_dir"], stop)
    terminal = read_record(stop["artifacts"]["terminal"], walkthrough["session_dir"],
                           "pocket.listen-terminal/v1")
    assert terminal["cause"] == "listen_stop" and terminal["stop_request_id"] == "stop-1"
    assert terminal["stop"]["elapsed_ms"] <= terminal["stop"]["bound_ms"]
    assert stop["terminal"]["cause"] == "listen_stop"
    journal = listen_host.verify_journal(walkthrough["runtime_dir"] / "journal.ndjson")
    assert journal["chain_ok"] and journal["head_sha256"] == terminal["journal"]["head_sha256"]
    assert not listen_host.lease_held(walkthrough["session_dir"])
    assert not (walkthrough["runtime_dir"] / "connection.json").exists()
    assert not [p for p in Path(walkthrough["session_dir"]).rglob("*") if p.suffix.lower() in AUDIO_SUFFIXES]
    replay = receipts["stop_replay"]
    assert replay["replayed"] is True
    assert replay["current_runtime"] == {"state": "not_running", "runtime_epoch": None}
    assert {k: v for k, v in replay.items() if k not in ("replayed", "current_runtime")} == \
        {k: v for k, v in stop.items() if k != "replayed"}
    after = receipts["observe_after"]
    assert after["status"] == "needs_input" and after["runtime"]["state"] == "not_running"
    assert after["previous_runtime"]["terminal"] == "sealed"
    assert after["previous_runtime"]["cause"] == "listen_stop"
    assert receipts["events_after"]["events_status"] == "runtime_not_running"
    assert receipts["events_after"]["status"] == "needs_input"
    last = terminal["broker"]["last_sequence"]
    assert after["previous_runtime"]["broker"]["last_sequence"] == last
    assert receipts["events_after"]["missing"] == [{
        "from_sequence": 1, "through_sequence": last, "through_is_lower_bound": False,
        "delivery_class": "all", "cause": "runtime_stopped",
        "health_in_range": "runtime_journal" if status == "ok" else "runtime_journal_incomplete"}]
    assert f"Sequences 1 to {last}" in receipts["events_after"]["next_actions"][0]
    caught_up = listen.listen_events(walkthrough["session_dir"], {"runtime_epoch": walkthrough["epoch"],
                                                                  "sequence": last})
    assert caught_up["missing"] == [] and caught_up["status"] == "needs_input"
    assert caught_up["cursor_runtime"] == {"runtime_epoch": walkthrough["epoch"], "terminal": "sealed",
                                           "last_sequence": last, "journal": terminal["journal"]}
    unknown = listen.listen_events(walkthrough["session_dir"], {"runtime_epoch": OTHER_EPOCH, "sequence": 0})
    assert unknown["missing"] == [] and unknown["cursor_runtime"]["terminal"] == "unknown"
    assert "No runtime of this session" in unknown["next_actions"][0] and "lost" in unknown["next_actions"][0]


def test_routes_report_declared_unverified_master_unlabeled_no_claim_and_unavailable_cue(walkthrough):
    receipts = walkthrough["receipts"]
    prepared = {r["route_id"]: r for r in receipts["prepare"]["session"]["routes"]}
    assert prepared["master"] == {"route_id": "master", "role": "master", "channels": [0, 1],
                                  "metering": "metered", "semantic_status": "declared_unverified",
                                  "route_calibration_id": None}
    assert prepared["cue"] == {"route_id": "cue", "role": "cue", "channels": [2],
                               "metering": "withheld_until_routing_test",
                               "semantic_status": "not_established", "route_calibration_id": None,
                               "isolated_cue": "unavailable"}
    assert prepared["booth"]["semantic_status"] == "no_semantic_claim"
    observed = {r["route_id"]: r for r in receipts["observe"]["observation"]["routes"]}
    assert observed["cue"]["availability"] == "unavailable" and observed["cue"]["signal"] is None
    assert observed["cue"]["reason"] == "routing_test_not_available"
    assert observed["cue"]["channels_exposed"] is True
    assert observed["master"]["availability"] == observed["booth"]["availability"] == "available"
    master_peaks = observed["master"]["signal"]["evidence"]["sample_peak_dbfs"]
    assert master_peaks == pytest.approx([-12.0, -12.0], abs=0.05)
    started = {r["route_id"]: r for r in receipts["start"]["capture"]["routes"]}
    assert started["cue"]["metering"] == "withheld_until_routing_test"
    events = walkthrough["events"]
    measurements = [e for e in events if e["evidence"]["kind"] == "measurement"]
    assert measurements and {e["route_role"] for e in measurements} == {"master", "unlabeled"}
    assert not [e for e in events if e["route_id"] == "cue" or e["route_role"] == "cue"]
    assert {e["semantic_status"] for e in _signals(events, "master")} == {"declared_unverified"}
    assert {e["semantic_status"] for e in _signals(events, "booth")} == {"no_semantic_claim"}
    assert {e["route_calibration_id"] for e in events} == {None}
    assert _signals(events, "booth")[0]["evidence"]["channels"] == [3]
    assert _signals(events, "booth")[0]["evidence"]["sample_peak_dbfs"] == pytest.approx([-30.0], abs=0.05)
    health_only = receipts["events_health_only"]
    assert health_only["status"] == "ok" and health_only["filtered"]["kind"] > 0
    assert health_only["events"] and {e["delivery_class"] for e in health_only["events"]} == {"health"}
    aged = receipts["events_aged"]
    assert aged["filtered"]["stale_measurements"] > 0
    assert [e["evidence"]["type"] for e in aged["events"]][:2] == ["runtime_started", "capture_opened"]


def test_every_receipt_and_event_carries_the_coverage_block(walkthrough):
    receipts = walkthrough["receipts"]
    for name, item in receipts.items():
        assert {k: item["coverage"][k] for k in COVERAGE} == COVERAGE, name
        assert item["coverage"]["audio_source"] in ("synthetic_script", "none"), name
    for page in (receipts["events"], receipts["events_health_only"], receipts["events_aged"]):
        for event in page["events"]:
            assert event["coverage"] == {**COVERAGE, "audio_source": "synthetic_script"}
            assert not {"authority", "grant", "owner", "lease", "permission"} & set(event)
    for event in walkthrough["events"]:
        assert event["coverage"]["control_issued"] is False


def test_observe_reports_epoch_mismatch_as_a_field_not_an_error(walkthrough):
    receipts = walkthrough["receipts"]
    matching, other = receipts["observe"], receipts["observe_other_epoch"]
    assert matching["status"] == other["status"] == "ok"
    assert matching["expected_runtime_epoch_matches"] is True
    assert "runtime_epoch_changed" not in {n["code"] for n in matching["notices"]}
    assert other["expected_runtime_epoch_matches"] is False
    assert "runtime_epoch_changed" in {n["code"] for n in other["notices"]}
    assert other["cursor"]["runtime_epoch"] == walkthrough["epoch"]
    assert matching["cursor"]["sequence"] == matching["observation"]["snapshot_sequence"]
    assert "expected_runtime_epoch_matches" not in receipts["observe_after_stale_stop"]


def test_stop_with_a_stale_runtime_epoch_is_stale_revision_and_stops_nothing(walkthrough, tmp_path):
    error = walkthrough["errors"]["stale_stop"]
    assert error.code == "stale_revision" and error.hint == listen._STALE_EPOCH_HINT
    assert not (Path(walkthrough["session_dir"]) / "requests" / "stop-stale").exists()
    after = walkthrough["receipts"]["observe_after_stale_stop"]
    assert after["status"] == "ok" and after["observation"]["runtime"]["state"] == "running"
    error = raised(lambda: listen.listen_stop(walkthrough["session_dir"], "stop-bad", "runtime-4"))
    assert error.code == "invalid_request" and error.hint == listen._EPOCH_HINT
    gone = listen.listen_stop(walkthrough["session_dir"], "stop-gone", OTHER_EPOCH)
    assert gone["status"] == "needs_input" and gone["stop_outcome"] == "not_running"
    assert gone["runtime_known"] is False and gone["previous_runtime"]["terminal"] == "sealed"


def test_request_status_finds_the_runtime_a_request_launched_and_the_terminal_it_stopped(walkthrough):
    receipts = walkthrough["receipts"]
    start, stop = receipts["status_start"], receipts["status_stop"]
    assert start["journal_state"] == "complete" and start["receipt_status"] == "ok"
    assert start["request_runtime"] == {"runtime_epoch": walkthrough["epoch"], "ordinal": 1,
                                        "terminal": "sealed", "current": False}
    assert start["runtime"] == {"state": "not_running", "runtime_epoch": None}
    assert stop["stop_terminal"]["stop_request_id"] == "stop-1"
    assert stop["stop_terminal"]["cause"] == "listen_stop"
    assert stop["coverage"]["redispatched"] is False and stop["coverage"]["runtime_contacted"] is False
    unknown = listen.listen_request_status(walkthrough["session_dir"], "never-sent")
    assert unknown["status"] == "needs_input" and unknown["journal_state"] == "not_found"
    assert unknown["request_runtime"] is None and unknown["stop_terminal"] is None


def _without_live_summaries(value):
    if isinstance(value, dict):
        return {k: _without_live_summaries(v) for k, v in value.items() if k != "summary"}
    if isinstance(value, list):
        return [_without_live_summaries(v) for v in value]
    return value


def test_listen_docs_and_records_state_no_unmeasured_targets(walkthrough):
    targets = re.compile(r"p99\.9|(?<![\d.])(?:200|600|2) ms\b|(?<![\d.])10 ?%|10 percent")
    texts = [canonical_bytes(_without_live_summaries(r)).decode() for r in walkthrough["receipts"].values()]
    texts += [canonical_bytes(e).decode() for e in walkthrough["events"]]
    observed = walkthrough["receipts"]["observe"]["observation"]["summary"]
    assert not targets.search(re.sub(r"last block \d+ ms ago", "last block N ms ago", observed))
    for path in (REPO / "docs" / "listen.md", REPO / "CHANGELOG.md"):
        if path.exists():
            text = path.read_text()
            texts.append(text if path.name != "CHANGELOG.md" else
                         "\n".join(line for line in text.splitlines() if "Listen" in line))
    for text in texts:
        assert not targets.search(text), targets.search(text)


def test_completed_start_replays_without_spawning_and_is_labeled_replayed(runtimes, monkeypatch):
    session_dir, sha, first = runtimes.start()
    assert first["status"] == "ok"
    epoch = first["runtime"]["runtime_epoch"]

    def no_spawn(*args, **kwargs):
        pytest.fail("a replayed start must not spawn a runtime")
    with monkeypatch.context() as patch:
        patch.setattr(listen_host, "spawn_host", no_spawn)
        replay = listen.listen_start(session_dir, "start-1", sha)
        conflict = listen.listen_start(session_dir, "start-2", sha)
        conflict_replay = listen.listen_start(session_dir, "start-2", sha)
    assert replay["replayed"] is True
    assert replay["current_runtime"] == {"state": "running", "runtime_epoch": epoch}
    assert {k: v for k, v in replay.items() if k not in ("replayed", "current_runtime")} == \
        {k: v for k, v in first.items() if k != "replayed"}
    assert conflict["status"] == "conflict" and conflict["start_outcome"] == "already_running"
    assert conflict["runtime_conflict"] == "already_running" and conflict["running_runtime_epoch"] == epoch
    assert conflict_replay["replayed"] is True and conflict_replay["status"] == "conflict"
    assert len(listen_host.runtime_dirs(session_dir)) == 1
    stopped = listen.listen_stop(session_dir, "stop-1", epoch)
    sealed_stop_status_allowing_journal_close_partial(stopped)
    later = listen.listen_start(session_dir, "start-2", sha)
    assert later["status"] == "conflict" and later["replayed"] is True
    assert later["current_runtime"]["state"] == "not_running"


_LOST_START = r"""
import sys, time
from pathlib import Path
from pocket_music import listen, listen_host
flag = Path(sys.argv[3])

def lost(*args, **kwargs):
    flag.write_text("launched")
    time.sleep(600)

listen_host.wait_for_runtime = lost
listen.listen_start(sys.argv[1], "start-1", sys.argv[2])
"""


def test_lost_start_response_is_resolved_by_request_status_without_redispatch(runtimes, tmp_path,
                                                                              monkeypatch):
    session_dir, sha = runtimes.prepare()
    flag = tmp_path / "launched"
    child = subprocess.Popen([sys.executable, "-c", _LOST_START, session_dir, sha, str(flag)],
                             env={**os.environ, "PYTHONPATH": str(SRC)}, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_until(flag.exists, 60)
        wait_until(lambda: listen_host.current_runtime(session_dir)["state"] == "running", 30)
    finally:
        child.kill()
        child.wait(10)
    epoch = listen_host.current_runtime(session_dir)["runtime_epoch"]
    with monkeypatch.context() as patch:
        patch.setattr(listen_host, "call",
                      lambda *a, **k: pytest.fail("request_status never contacts the runtime"))
        status = listen.listen_request_status(session_dir, "start-1")
    assert status["status"] == "outcome_unknown" and status["journal_state"] == "started"
    assert status["coverage"]["lock_present"] is True
    assert status["request_runtime"] == {"runtime_epoch": epoch, "ordinal": 1, "terminal": "missing",
                                         "current": True}
    assert status["runtime"] == {"state": "running", "runtime_epoch": epoch}
    assert "won't run again" in status["next_actions"][0]
    error = raised(lambda: listen.listen_start(session_dir, "start-1", sha))
    assert error.code == "request_not_complete"
    assert len(listen_host.runtime_dirs(session_dir)) == 1
    observed = listen.listen_observe(session_dir)
    assert observed["status"] == "ok" and observed["cursor"]["runtime_epoch"] == epoch
    stopped = listen.listen_stop(session_dir, "stop-1", epoch)
    sealed_stop_status_allowing_journal_close_partial(stopped)
    after = listen.listen_request_status(session_dir, "start-1")
    assert after["request_runtime"]["current"] is False and after["request_runtime"]["terminal"] == "sealed"
    assert "isn't running now" in after["next_actions"][0]


def test_request_status_says_when_its_bounded_scan_left_older_runtimes_unread(tmp_path):
    session_dir = listen.listen_prepare(str(tmp_path / "session"), make_config())["session"]["session_dir"]
    for ordinal in range(listen.SCAN_RUNTIMES + 1):
        listen_host.prepare_launch(session_dir, request_id=f"start-{ordinal}", launch="provider")
    (Path(session_dir) / "requests" / "lost-1" / "lock").mkdir(parents=True)
    status = listen.listen_request_status(session_dir, "lost-1")
    assert (status["runtimes_scanned"], status["runtimes_omitted"]) == (listen.SCAN_RUNTIMES, 1)
    assert "1 older runtime wasn't read" in status["next_actions"][0]
    assert listen.listen_request_status(session_dir, "start-0")["request_runtime"] is None


def test_lost_stop_response_replays_the_terminal_receipt_and_request_status_finds_it(runtimes, monkeypatch):
    session_dir, _, started = runtimes.start()
    epoch = started["runtime"]["runtime_epoch"]
    real = listen_host.call

    def lose_the_stop_reply(runtime_dir, op, args=None, **kwargs):
        result = real(runtime_dir, op, args, **kwargs)
        if op == "stop":
            raise listen_host.RuntimeUnreachable("socket_unreachable")
        return result
    with monkeypatch.context() as patch:
        patch.setattr(listen_host, "call", lose_the_stop_reply)
        stopped = listen.listen_stop(session_dir, "stop-1", epoch)
    sealed_stop_status_allowing_journal_close_partial(stopped)
    assert stopped["terminal"]["stop_request_id"] == "stop-1"
    replay = listen.listen_stop(session_dir, "stop-1", epoch)
    assert replay["replayed"] is True and replay["artifacts"] == stopped["artifacts"]
    assert replay["current_runtime"]["state"] == "not_running"
    status = listen.listen_request_status(session_dir, "stop-1")
    assert status["journal_state"] == "complete" and status["stop_terminal"]["stop_request_id"] == "stop-1"
    terminal = read_record(stopped["artifacts"]["terminal"], session_dir, "pocket.listen-terminal/v1")
    assert terminal["cause"] == "listen_stop"


def test_a_stop_with_no_reply_whose_runtime_then_exits_without_a_terminal_is_not_running(tmp_path,
                                                                                         monkeypatch):
    session_dir = listen.listen_prepare(str(tmp_path / "session"), make_config())["session"]["session_dir"]
    held = iter([True])
    monkeypatch.setattr(listen_host, "lease_held", lambda root: next(held, False))
    monkeypatch.setattr(listen_host, "current_runtime",
                        lambda root: {"state": "running", "runtime_epoch": OTHER_EPOCH, "runtime_dir": str(tmp_path)})
    monkeypatch.setattr(listen, "_runtime_row", lambda root, epoch: (1, epoch, tmp_path))
    monkeypatch.setattr(listen, "_runtime_record", lambda root, row: {"runtime_epoch": OTHER_EPOCH, "ordinal": 1,
                                                                       "terminal": "missing"})

    def unreachable(*args, **kwargs):
        raise listen_host.RuntimeUnreachable("connection_missing")
    monkeypatch.setattr(listen_host, "call", unreachable)
    monkeypatch.setattr(listen_host, "read_terminal", lambda root, path: None)
    stopped = listen.listen_stop(session_dir, "stop-1", OTHER_EPOCH)
    assert (stopped["status"], stopped["stop_outcome"]) == ("needs_input", "not_running")
    assert stopped["reason"] == "runtime_exited_without_terminal" and stopped["lease_released"] is True
    assert stopped["previous_runtime"]["terminal"] == "missing" and "terminal_sealed" not in stopped
    assert "can't say whether this request reached it" in stopped["next_actions"][0]


def test_a_completed_stop_replays_even_while_a_newer_runtime_runs(runtimes):
    session_dir, sha, first = runtimes.start()
    old = first["runtime"]["runtime_epoch"]
    stopped = listen.listen_stop(session_dir, "stop-1", old)
    sealed_stop_status_allowing_journal_close_partial(stopped)
    assert stopped["replayed"] is False and lease_released_soon(session_dir, stopped)
    newer = listen.listen_start(session_dir, "start-2", sha)
    assert newer["status"] == "ok" and newer["runtime"]["runtime_epoch"] != old
    replay = listen.listen_stop(session_dir, "stop-1", old)
    assert replay["replayed"] is True and replay["artifacts"] == stopped["artifacts"]
    assert replay["current_runtime"] == {"state": "running",
                                         "runtime_epoch": newer["runtime"]["runtime_epoch"]}
    assert listen.listen_observe(session_dir)["observation"]["runtime"]["state"] == "running"
    error = raised(lambda: listen.listen_stop(session_dir, "stop-2", old))
    assert error.code == "stale_revision"


def test_unconfirmed_stop_is_outcome_unknown_and_a_new_request_id_stops_the_runtime(runtimes, monkeypatch):
    session_dir, _, started = runtimes.start()
    epoch = started["runtime"]["runtime_epoch"]
    real = listen_host.call

    def drop_the_stop(runtime_dir, op, args=None, **kwargs):
        if op == "stop":
            raise listen_host.RuntimeUnreachable("socket_unreachable")
        return real(runtime_dir, op, args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(listen_host, "call", drop_the_stop)
        unconfirmed = listen.listen_stop(session_dir, "stop-1", epoch)
    assert unconfirmed["status"] == "outcome_unknown" and unconfirmed["stop_outcome"] == "stop_unconfirmed"
    assert unconfirmed["lease_state"] == "held"
    assert not any("kill" in action or "pid" in action for action in unconfirmed["next_actions"])
    assert listen.listen_observe(session_dir)["status"] == "ok"
    stopped = listen.listen_stop(session_dir, "stop-2", epoch)
    sealed_stop_status_allowing_journal_close_partial(stopped)
    assert stopped["terminal"]["stop_request_id"] == "stop-2"


def test_a_changed_terminal_record_is_reported_and_never_trusted(runtimes, monkeypatch):
    session_dir, _, started = runtimes.start()
    epoch = started["runtime"]["runtime_epoch"]
    runtime_dir = Path(session_dir) / started["runtime"]["runtime_dir"]
    real = listen_host.call

    def change_the_terminal(runtime_dir_, op, args=None, **kwargs):
        result = real(runtime_dir_, op, args, **kwargs)
        if op == "stop":
            handle = json.loads((runtime_dir / "terminal-handle.json").read_bytes())["terminal"]
            record = Path(session_dir) / handle["artifact_uri"]
            record.write_bytes(record.read_bytes().replace(b'"listen_stop"', b'"max_runtime_reached"', 1))
        return result
    with monkeypatch.context() as patch:
        patch.setattr(listen_host, "call", change_the_terminal)
        stopped = listen.listen_stop(session_dir, "stop-1", epoch)
    assert stopped["status"] == "partial" and stopped["terminal_sealed"] is False
    assert stopped["reason"] == "terminal_integrity_mismatch" and stopped["artifacts"] == {}
    after = listen.listen_observe(session_dir)
    assert after["previous_runtime"] == {"runtime_epoch": epoch, "ordinal": 1,
                                         "terminal": "integrity_mismatch"}
    status = listen.listen_request_status(session_dir, "stop-1")
    assert status["stop_terminal"] is None and status["runtimes_unverified"] == 1
    assert listen_host.previous_runtime(session_dir)["terminal"] == "integrity_mismatch"


def test_capture_refusal_is_a_receipt_not_an_error(runtimes):
    spec = script(tone(RATE * 2), rate=44100)
    session_dir, sha, refused = runtimes.start(make_config(spec))
    assert refused["status"] == "unsupported" and refused["start_outcome"] == "capture_refused"
    assert refused["capture_refusal"]["reasons"] == ["sample_rate_mismatch"]
    assert refused["artifacts"] == {} and refused["coverage"]["capture"] == "refused"
    assert "expected_sample_rate" in refused["next_actions"][0]
    terminal = read_record(refused["refused_runtime"]["terminal"], session_dir, "pocket.listen-terminal/v1")
    assert terminal["state"] == "refused" and terminal["cause"] == "startup_refused"
    assert refused["refused_runtime"]["exit_code"] == listen_host.EXIT_REFUSED
    observed = listen.listen_observe(session_dir)
    assert observed["previous_runtime"]["terminal_handle"]["artifact_schema"] == "pocket.listen-terminal/v1"
    assert {r["availability"] for r in observed["previous_runtime"]["routes"]} == {"unavailable"}
    assert not listen_host.lease_held(session_dir)
    status = listen.listen_request_status(session_dir, "start-1")
    assert status["journal_state"] == "complete" and status["receipt_status"] == "unsupported"
    replay = listen.listen_start(session_dir, "start-1", sha)
    assert replay["replayed"] is True and replay["status"] == "unsupported"


def _outcome_terminal(tmp_path, reasons):
    record = {"schema": "pocket.listen-terminal/v1", "runtime_epoch": OTHER_EPOCH, "state": "refused",
              "cause": "startup_refused",
              "capture": {"refusal": {"reasons": reasons, "inventory": [], "detail": {}}}}
    return put_record(record, tmp_path), record


@pytest.mark.parametrize("reasons,status,notices", [
    (["microphone_not_authorized"], "needs_input", ["microphone_permission_needed"]),
    (["microphone_not_authorized", "device_hogged"], "unsupported", ["microphone_permission_needed"]),
    (["helper_start_timeout"], "unsupported", []),
])
def test_permission_refusal_alone_is_needs_input_with_the_permission_notice(tmp_path, reasons, status,
                                                                           notices):
    session = {"config": {"adapter": {"kind": "coreaudio"}, "routes": []}}
    runtime = {"runtime_epoch": OTHER_EPOCH, "ordinal": 1, "runtime_dir": "runtimes/0001-" + OTHER_EPOCH}
    outcome = {"outcome": "capture_refused", "terminal": _outcome_terminal(tmp_path, reasons), "exit_code": 3}
    result = listen._refused_receipt(tmp_path, session, "start-1", tmp_path, runtime, outcome,
                                     {"start_outcome": "capture_refused", "replayed": False,
                                      "previous_runtime": None})
    assert result["status"] == status and [n["code"] for n in result["notices"]] == notices
    assert result["capture_refusal"]["reasons"] == reasons
    assert result["coverage"]["audio_source"] == "none"
    assert result["next_actions"]


@pytest.mark.parametrize("authorization, status, step", [
    ("denied", "needs_input", "Turn microphone access on for that app in System Settings"),
    ("not_determined", "unsupported", "This version has no way to request access"),
    ("restricted", "unsupported", "System Settings can't lift a restriction"),
    (None, "needs_input", "If it isn't listed, run Pocket from an app that already has microphone access"),
])
def test_permission_guidance_follows_the_state_macos_reported(tmp_path, authorization, status, step):
    detail = {} if authorization is None else {"microphone_authorization": authorization}
    record = {"schema": "pocket.listen-terminal/v1", "runtime_epoch": OTHER_EPOCH, "state": "refused",
              "cause": "startup_refused",
              "capture": {"refusal": {"reasons": ["microphone_not_authorized"], "refused_at": "start",
                                      "inventory": None, "detail": detail,
                                      "open_detail": {"microphone_authorization": "authorized"}}}}
    session = {"config": {"adapter": {"kind": "coreaudio"}, "routes": []}}
    runtime = {"runtime_epoch": OTHER_EPOCH, "ordinal": 1, "runtime_dir": "runtimes/0001-" + OTHER_EPOCH}
    outcome = {"outcome": "capture_refused", "terminal": (put_record(record, tmp_path), record),
               "exit_code": 3}
    result = listen._refused_receipt(tmp_path, session, "start-1", tmp_path, runtime, outcome,
                                     {"start_outcome": "capture_refused", "replayed": False,
                                      "previous_runtime": None})
    (permission,) = result["notices"]
    assert result["status"] == status and step in permission["next_step"]
    assert result["next_actions"] == [permission["next_step"]]
    assert result["capture_refusal"]["refused_at"] == "start"
    assert result["capture_refusal"]["open_detail"] == {"microphone_authorization": "authorized"}
    (listed,) = listen._device_notices([], authorization or "unknown")
    assert listed == permission


def test_a_start_with_no_frames_yet_words_its_next_step_from_the_observed_state(tmp_path, monkeypatch):
    session = {"config": make_config()}
    runtime = {"runtime_epoch": OTHER_EPOCH, "ordinal": 1, "runtime_dir": "runtimes/0001-" + OTHER_EPOCH}
    ping = {"capture_state": "stale", "capture_cause": None, "frames_received": 0}
    for state, words in (("stale", "capture is stale (capture_no_frames)"),
                         ("running", "no frame had arrived when this receipt was written")):
        observed = {**_RUNNING, "capture": {"state": state, "frames_received": 0}}
        monkeypatch.setattr(listen, "_observe_once", lambda runtime_dir, observed=observed: observed)
        result = listen._running_receipt(tmp_path, session, "start-1", tmp_path, runtime,
                                         {"outcome": "started_no_audio_yet", "ping": ping},
                                         {"start_outcome": "started_no_audio_yet", "replayed": False,
                                          "previous_runtime": None})
        assert (result["status"], result["start_outcome"]) == ("partial", "started_no_audio_yet")
        assert words in result["next_actions"][0] and "capture is live" not in result["next_actions"][0]


def test_a_capture_that_ended_during_start_says_so_and_how_to_start_again(runtimes):
    session_dir, _, started = runtimes.start(make_config(script({"kind": "device_lost"})))
    assert started["status"] == "partial" and started["start_outcome"] == "capture_ended_during_start"
    assert started["capture"]["state"] == "ended" and started["capture"]["ended_cause"] == "device_lost"
    assert "listen_stop" in started["next_actions"][0] and "won't resume" in started["next_actions"][0]
    assert not any("no audio arrived yet" in action for action in started["next_actions"])
    assert "device_lost" in [n["code"] for n in started["notices"]]
    assert listen_host.lease_held(session_dir)
    (lost,) = [n for n in started["notices"] if n["code"] == "device_lost"]
    assert lost["next_step"].startswith("Stop this runtime with listen_stop")


_RUNNING = {"runtime": {"pid": 7, "started_monotonic_ns": 1, "scheduled_stop_monotonic_ns": 2},
            "routes": [], "notices": []}


@pytest.mark.parametrize("observed, outcome, status, start_outcome, snapshot", [
    ({**_RUNNING, "capture": {"state": "ended", "ended_cause": "device_lost", "frames_received": 480}},
     "capturing", "partial", "capture_ended_during_start", "observed"),
    ({**_RUNNING, "capture": {"state": "running", "frames_received": 480}}, "capturing", "ok", "capturing",
     "observed"),
    (None, "capturing", "partial", "capturing", "unavailable"),
    ({**_RUNNING, "runtime": {**_RUNNING["runtime"], "state": "stopping", "stop": {"cause": "listen_stop"}},
      "capture": {"state": "running", "frames_received": 480}},
     "capturing", "partial", "stopped_during_start", None),
    ({**_RUNNING, "runtime": {**_RUNNING["runtime"], "state": "stopped", "stop": {"cause": "fatal_error"}},
      "capture": {"state": "stopped", "frames_received": 960}},
     "capturing", "partial", "stopped_during_start", None),
    ({**_RUNNING, "capture": {"state": "running", "frames_received": 480}}, "started_no_audio_yet", "ok",
     "capturing", "observed"),
])
def test_start_receipt_follows_the_snapshot_it_reports(tmp_path, monkeypatch, observed, outcome, status,
                                                       start_outcome, snapshot):
    monkeypatch.setattr(listen, "_observe_once", lambda runtime_dir: observed)
    session = {"config": make_config()}
    runtime = {"runtime_epoch": OTHER_EPOCH, "ordinal": 1, "runtime_dir": "runtimes/0001-" + OTHER_EPOCH}
    ping = {"capture_state": "running", "capture_cause": None, "frames_received": 480}
    result = listen._running_receipt(tmp_path, session, "start-1", tmp_path, runtime,
                                     {"outcome": outcome, "ping": ping},
                                     {"start_outcome": outcome, "replayed": False, "previous_runtime": None})
    outcomes = (result["status"], result["start_outcome"], result.get("capture_snapshot"))
    assert outcomes == (status, start_outcome, snapshot)
    if snapshot == "unavailable":
        assert "listen_observe" in result["next_actions"][0] and result["capture"]["routes"] == []
    if start_outcome == "stopped_during_start":
        (step,) = result["next_actions"]
        assert "began stopping" in step and "listen_events" not in step
        assert "capture" not in result and result["stopped_runtime"]["terminal"] is None
        assert result["coverage"]["capture"] == "stopped"
    if start_outcome == "capturing" and status == "ok":
        assert result["next_actions"] == listen._CAPTURING_ACTIONS
        assert result["capture"]["frames_received"] == 480


def test_a_start_that_sees_its_runtime_stopping_reports_the_terminal_once_it_is_sealed(tmp_path, monkeypatch):
    stopped = {**_RUNNING["runtime"], "state": "stopped", "stop": {"cause": "fatal_error"}}
    observed = {**_RUNNING, "runtime": stopped, "capture": {"state": "running", "frames_received": 960}}
    record = {"schema": "pocket.listen-terminal/v1", "runtime_epoch": OTHER_EPOCH, "state": "failed",
              "cause": "fatal_error", "stop_request_id": None, "capture": {"final_state": "stopped"},
              "worker": {"drain": "complete", "state": "running"}, "stop": {"adapter_close": "clean"}}
    terminal = (put_record(record, tmp_path), record)
    monkeypatch.setattr(listen, "_observe_once", lambda runtime_dir: observed)
    monkeypatch.setattr(listen_host, "read_terminal", lambda root, path: terminal)
    runtime = {"runtime_epoch": OTHER_EPOCH, "ordinal": 1, "runtime_dir": "runtimes/0001-" + OTHER_EPOCH}
    result = listen._running_receipt(tmp_path, {"config": make_config()}, "start-1", tmp_path, runtime,
                                     {"outcome": "capturing", "ping": {"capture_state": "running",
                                                                      "frames_received": 480}},
                                     {"start_outcome": "capturing", "replayed": False,
                                      "previous_runtime": None})
    assert (result["status"], result["start_outcome"]) == ("partial", "stopped_during_start")
    assert result["stopped_runtime"]["terminal"] == terminal[0]
    assert result["terminal"]["cause"] == "fatal_error"
    assert "capture" not in result and result["coverage"]["capture"] == "stopped"


def test_a_start_whose_runtime_was_still_stopping_at_the_deadline_is_partial_without_a_terminal(tmp_path):
    session = {"config": make_config()}
    runtime = {"runtime_epoch": OTHER_EPOCH, "ordinal": 1, "runtime_dir": "runtimes/0001-" + OTHER_EPOCH}
    outcome = {"outcome": "stopped_during_start", "exit_code": None, "terminal": None,
               "ping": {"runtime_state": "stopping", "capture_state": "running", "frames_received": 480}}
    result = listen._stopped_receipt(tmp_path, session, "start-1", tmp_path, runtime, outcome,
                                     {"start_outcome": "stopped_during_start", "replayed": False,
                                      "previous_runtime": None})
    assert (result["status"], result["start_outcome"]) == ("partial", "stopped_during_start")
    assert result["stopped_runtime"]["terminal"] is None and result["runtime_state"] == "stopping"
    assert "listen_request_status" in result["next_actions"][0]


def _sealed(tmp_path, record, request_id="stop-1"):
    return listen._sealed_stop(request_id, "synthetic_script", (put_record(record, tmp_path), record),
                               {"lease_released": True, "superseded_by_runtime_epoch": None}, OTHER_EPOCH)


def test_a_sealed_stop_whose_measurement_didnt_drain_is_partial(tmp_path):
    record = {"schema": "pocket.listen-terminal/v1", "runtime_epoch": OTHER_EPOCH, "state": "stopped",
              "cause": "listen_stop", "stop_request_id": "stop-1", "capture": {"final_state": "stopped"},
              "worker": {"drain": "incomplete", "state": "running", "lag_frames": 96000},
              "stop": {"adapter_close": "clean"}}
    stopped = _sealed(tmp_path, record)
    assert (stopped["status"], stopped["stop_outcome"]) == ("partial", "stopped")
    assert stopped["terminal_sealed"]
    assert stopped["measurement_drain"] == "incomplete" and "lag_frames" in stopped["next_actions"][0]
    complete = {**record, "worker": {"drain": "complete", "state": "running", "lag_frames": 0}}
    assert _sealed(tmp_path, complete)["status"] == "ok"
    bounded = {**record, "worker": {"drain": "incomplete", "state": "running", "overruns": 1,
                                    "unmeasured_frames": 4800, "unmeasured_at_stop_frames": 4800}}
    assert not any("fell behind" in action for action in _sealed(tmp_path, bounded)["next_actions"])
    lost = {**record, "worker": {"drain": "complete", "state": "running", "overruns": 0,
                                 "unmeasured_frames": 0, "unaccounted_frames": 624, "facts_dropped": 3}}
    steps = _sealed(tmp_path, lost)["next_actions"]
    assert not any("fell behind" in action for action in steps)
    assert any("lost track of 624 frames" in action and "frames_missing" in action for action in steps)


@pytest.mark.parametrize("change, step", [
    ({"worker": {"drain": "complete", "state": "failed", "unmeasured_after_failure_frames": 95520}},
     "unmeasured_after_failure_frames"),
    ({"worker": {"drain": "complete", "state": "running", "overruns": 3, "unmeasured_frames": 480000,
                 "unmeasured_after_failure_frames": 0, "unmeasured_at_stop_frames": 0}},
     "480,000 received frames it skipped"),
    ({"worker": {"drain": "complete", "state": "running", "unaccounted_frames": 96000, "facts_dropped": 12}},
     "lost track of 96,000 frames"),
    ({"stop": {"adapter_close": "timeout"}}, "adapter_close is timeout"),
    ({"stop": {"adapter_close": "clean",
               "adapter_exit": {"counters": {"undelivered_blocks": 140, "undelivered_frames": 71680}}}},
     "140 blocks (71,680 frames) of audio it had captured but not sent"),
    ({"stop": {"adapter_close": "clean", "adapter_exit": {"counters": {"undelivered_blocks": 3}}}},
     "3 blocks (an unknown number of frames)"),
    ({"stop": {"adapter_close": "clean", **listen_host._close_report(
        {"exit_counters": {"dropped_frames": 6144}, "audio_frames_left_unread": 20480}, 2048)}},
     "4,096 frames dropped after the last block capture took"),
    ({"stop": {"adapter_close": "exited_with_error"}}, "adapter_close is exited_with_error"),
    ({"state": "failed", "error": "capture: boom"}, "terminal.error"),
    ({"journal": {"state": "writing", "dropped_lines": 0, "closed": False, "lines_unwritten_at_close": 8}},
     "8 unwritten at close"),
    ({"journal": {"state": "exhausted", "dropped_lines": 23, "closed": True, "lines_unwritten_at_close": 0}},
     "state exhausted, 23 lines dropped"),
    ({"journal": {"state": "writing", "dropped_lines": 0, "closed": True, "lines_unwritten_at_close": 0},
      "broker": {"publish_hook_errors": 6}}, "publish_hook_errors: 6 events never reached it"),
])
def test_a_sealed_stop_with_a_failed_worker_adapter_or_runtime_is_partial(tmp_path, change, step):
    record = {"schema": "pocket.listen-terminal/v1", "runtime_epoch": OTHER_EPOCH, "state": "stopped",
              "cause": "listen_stop", "stop_request_id": "stop-1", "capture": {"final_state": "stopped"},
              "worker": {"drain": "complete", "state": "running"}, "stop": {"adapter_close": "clean"},
              **change}
    stopped = _sealed(tmp_path, record)
    assert (stopped["status"], stopped["stop_outcome"], stopped["terminal_sealed"]) == (
        "partial", "stopped", True)
    assert any(step in action for action in stopped["next_actions"])


def test_the_daemon_stop_check_allows_only_the_journal_caveat_a_loaded_machine_can_cause(tmp_path):
    record = {"schema": "pocket.listen-terminal/v1", "runtime_epoch": OTHER_EPOCH, "state": "stopped",
              "cause": "listen_stop", "stop_request_id": "stop-1", "capture": {"final_state": "stopped"},
              "worker": {"drain": "complete", "state": "running"}, "stop": {"adapter_close": "clean"},
              "journal": {"state": "writing", "dropped_lines": 0, "closed": False, "lines_unwritten_at_close": 1}}
    assert sealed_stop_status_allowing_journal_close_partial(_sealed(tmp_path, record)) == "partial"
    for other in ({"worker": {"drain": "incomplete", "state": "running"}}, {"stop": {"adapter_close": "timeout"}}):
        with pytest.raises(AssertionError):
            sealed_stop_status_allowing_journal_close_partial(_sealed(tmp_path, {**record, **other}))


def test_audio_the_helper_sent_after_the_last_block_capture_took_is_no_caveat_at_a_stop(tmp_path):
    stop = {"adapter_close": "clean", **listen_host._close_report(
        {"exit_counters": {"dropped_frames": 2048}, "items_left_unread": 40, "blocks_left_unread": 40,
         "audio_frames_left_unread": 20480, "audio_frames_discarded_while_closing": 4096}, 2048)}
    record = {"schema": "pocket.listen-terminal/v1", "runtime_epoch": OTHER_EPOCH, "state": "stopped",
              "cause": "listen_stop", "stop_request_id": "stop-1", "capture": {"final_state": "stopped"},
              "worker": {"drain": "complete", "state": "running"}, "stop": stop}
    stopped = _sealed(tmp_path, record)
    assert stopped["status"] == "ok" and stopped["terminal"]["stop"]["helper_drops_after_last_block_frames"] == 0


def test_device_receipts_say_device_input_only_for_a_runtime_that_received_audio(tmp_path, monkeypatch):
    session = {"config": {"adapter": {"kind": "coreaudio"}, "routes": []}}
    runtime = {"runtime_epoch": OTHER_EPOCH, "ordinal": 1, "runtime_dir": "runtimes/0001-" + OTHER_EPOCH}
    common = {"start_outcome": "started_no_audio_yet", "replayed": False, "previous_runtime": None}
    monkeypatch.setattr(listen, "_observe_once", lambda runtime_dir: {
        "runtime": {"state": "running"}, "capture": {"state": "stale", "frames_received": 0}, "routes": [],
        "notices": []})
    started = listen._running_receipt(tmp_path, session, "start-1", tmp_path, runtime,
                                      {"outcome": "started_no_audio_yet", "ping": {}}, common)
    assert started["start_outcome"] == "started_no_audio_yet" and started["coverage"]["audio_source"] == "none"
    record = {"schema": "pocket.listen-terminal/v1", "runtime_epoch": OTHER_EPOCH, "state": "stopped",
              "cause": "listen_stop", "stop_request_id": "stop-1", "capture": {"final_state": "stopped"},
              "worker": {"drain": "complete", "state": "running"}, "stop": {"adapter_close": "clean"},
              "coverage": {**COVERAGE, "audio_source": "none"}}
    lease = {"lease_released": True, "superseded_by_runtime_epoch": None}
    stopped = listen._sealed_stop("stop-1", "device_input", (put_record(record, tmp_path), record), lease,
                                  OTHER_EPOCH)
    assert stopped["coverage"]["audio_source"] == "none"
    heard = {**record, "coverage": {**COVERAGE, "audio_source": "device_input"}}
    stopped = listen._sealed_stop("stop-1", "device_input", (put_record(heard, tmp_path), heard), lease,
                                  OTHER_EPOCH)
    assert stopped["coverage"]["audio_source"] == "device_input"


def test_a_stop_that_finds_another_stop_sealed_the_terminal_says_who_stopped_it(tmp_path):
    record = {"schema": "pocket.listen-terminal/v1", "runtime_epoch": OTHER_EPOCH, "state": "stopped",
              "cause": "max_runtime_reached", "stop_request_id": None, "capture": {"final_state": "stopped"},
              "worker": {"drain": "complete", "state": "running"}, "stop": {"adapter_close": "clean"}}
    stopped = _sealed(tmp_path, record, "stop-2")
    assert (stopped["status"], stopped["stop_outcome"]) == ("ok", "already_stopped")
    assert stopped["stopped_by"] == {"cause": "max_runtime_reached", "stop_request_id": None}
    assert "already stopped (max_runtime_reached)" in stopped["next_actions"][0]


def test_a_stop_reply_still_in_progress_defers_to_the_terminal_sealed_just_after(tmp_path, monkeypatch):
    record = {"schema": "pocket.listen-terminal/v1", "runtime_epoch": OTHER_EPOCH, "state": "stopped",
              "cause": "max_runtime_reached", "stop_request_id": None, "capture": {"final_state": "stopped"},
              "worker": {"drain": "complete", "state": "running"}, "stop": {"adapter_close": "clean"}}
    terminal = (put_record(record, tmp_path), record)
    row = (1, OTHER_EPOCH, tmp_path / f"0001-{OTHER_EPOCH}")
    monkeypatch.setattr(listen, "_runtime_row", lambda root, epoch: row)
    monkeypatch.setattr(listen_host, "lease_held", lambda root: True)
    monkeypatch.setattr(listen_host, "current_runtime", lambda root: {"runtime_epoch": OTHER_EPOCH})
    monkeypatch.setattr(listen_host, "call", lambda *a, **k: {"status": "ok", "terminal_sealed": False,
                                                              "reason": "stop_in_progress"})
    monkeypatch.setattr(listen, "_wait_released", lambda root, seconds, epoch=None: True)
    monkeypatch.setattr(listen_host, "read_terminal", lambda root, path: terminal)
    session = {"config": make_config()}
    result = listen._stop(tmp_path, session, "stop-2", OTHER_EPOCH)
    assert (result["status"], result["terminal_sealed"], result["stop_outcome"]) == (
        "ok", True, "already_stopped")
    assert result["stop_reply_reason"] == "stop_in_progress"
    assert result["artifacts"]["terminal"] == terminal[0]


def _terminal_row(tmp_path, monkeypatch, record):
    terminal = (put_record(record, tmp_path), record)
    monkeypatch.setattr(listen, "_runtime_row", lambda root, epoch: (1, OTHER_EPOCH, tmp_path / "runtime"))
    monkeypatch.setattr(listen_host, "read_terminal", lambda root, path: terminal)
    return terminal


@pytest.mark.parametrize("state, cause, outcome", [("stopped", "max_runtime_reached", "already_stopped"),
                                                   ("failed", "fatal_error", "already_stopped"),
                                                   ("refused", "startup_refused", "not_running")])
def test_a_stop_after_the_runtime_already_stopped_on_its_own_returns_its_sealed_terminal(
        tmp_path, monkeypatch, state, cause, outcome):
    record = {"schema": "pocket.listen-terminal/v1", "runtime_epoch": OTHER_EPOCH, "state": state,
              "cause": cause, "stop_request_id": None, "capture": {"final_state": "stopped"},
              "worker": {"drain": "complete", "state": "running"}, "stop": {"adapter_close": "clean"}}
    terminal = _terminal_row(tmp_path, monkeypatch, record)
    monkeypatch.setattr(listen_host, "lease_held", lambda root: False)
    result = listen._stop(tmp_path, {"config": make_config()}, "stop-late", OTHER_EPOCH)
    assert result["stop_outcome"] == outcome
    if outcome == "already_stopped":
        assert result["artifacts"]["terminal"] == terminal[0] and result["lease_released"] is True
        assert result["stopped_by"] == {"cause": cause, "stop_request_id": None}
    else:
        assert result["status"] == "needs_input" and result["previous_runtime"]["terminal"] == "sealed"


def test_a_stop_that_finds_a_refused_runtime_is_not_running_even_when_its_reply_was_lost(tmp_path,
                                                                                         monkeypatch):
    record = {"schema": "pocket.listen-terminal/v1", "runtime_epoch": OTHER_EPOCH, "state": "refused",
              "cause": "startup_refused", "stop_request_id": None, "capture": {"final_state": "refused"},
              "worker": {"drain": "complete", "state": "running"}, "stop": {"adapter_close": "clean"}}
    _terminal_row(tmp_path, monkeypatch, record)
    monkeypatch.setattr(listen_host, "lease_held", lambda root: True)
    monkeypatch.setattr(listen_host, "current_runtime", lambda root: {"runtime_epoch": OTHER_EPOCH})
    monkeypatch.setattr(listen, "_wait_released", lambda root, seconds, epoch: True)

    def unreachable(*args, **kwargs):
        raise listen_host.RuntimeUnreachable("lease_held_no_connection")

    monkeypatch.setattr(listen_host, "call", unreachable)
    result = listen._stop(tmp_path, {"config": make_config()}, "stop-late", OTHER_EPOCH)
    assert (result["status"], result["stop_outcome"]) == ("needs_input", "not_running")
    assert "stopped_by" not in result and "reason" not in result
    assert result["previous_runtime"]["terminal"] == "sealed"


def test_a_lost_stop_reply_while_a_newer_runtime_holds_the_lease_still_finds_the_named_terminal(tmp_path,
                                                                                               monkeypatch):
    record = {"schema": "pocket.listen-terminal/v1", "runtime_epoch": OTHER_EPOCH, "state": "stopped",
              "cause": "max_runtime_reached", "stop_request_id": None, "capture": {"final_state": "stopped"},
              "worker": {"drain": "complete", "state": "running"}, "stop": {"adapter_close": "clean"}}
    terminal = _terminal_row(tmp_path, monkeypatch, record)
    newer = "rt-1111111111111111"
    monkeypatch.setattr(listen_host, "lease_held", lambda root: True)
    monkeypatch.setattr(listen_host, "current_runtime", lambda root: {"runtime_epoch": OTHER_EPOCH})

    def unreachable(*args, **kwargs):
        raise listen_host.RuntimeUnreachable("socket_unreachable")

    monkeypatch.setattr(listen_host, "call", unreachable)
    monkeypatch.setattr(listen_host, "newest_runtime", lambda root: (2, newer, tmp_path / "newer"))
    released, checks = listen._released, []

    def checked(root, epoch):
        checks.append(released(root, epoch))
        return checks[-1]

    monkeypatch.setattr(listen, "_released", checked)
    result = listen._stop(tmp_path, {"config": make_config()}, "stop-1", OTHER_EPOCH)
    assert checks and all(checks)
    assert (result["status"], result["stop_outcome"]) == ("ok", "already_stopped")
    assert result["artifacts"]["terminal"] == terminal[0] and result["lease_released"] is False
    assert result["superseded_by_runtime_epoch"] == newer
    assert f"Runtime {newer} now holds this session" in result["next_actions"][-1]
    assert not any(action.startswith("listen_start") for action in result["next_actions"])


def test_notices_keep_every_fault_and_warning_and_cap_only_information():
    items = [{"code": code, "severity": notice.severity, "text": code, "next_step": None}
             for code, notice in listen._NOTICES.items()]
    urgent = [item for item in items if item["severity"] != "info"]
    assert len(urgent) > 8
    ordered = listen.order_notices(list(reversed(items)))
    assert [n["code"] for n in ordered] == [n["code"] for n in sorted(
        urgent, key=lambda n: ({"fault": 0, "warning": 1}[n["severity"]], list(listen._NOTICES).index(n["code"])))]
    info = [item for item in items if item["severity"] == "info"]
    few = [item for item in items if item["code"] in ("runtime_expiring", "capture_stale")] + info
    kept = listen.order_notices(few)
    assert [n["code"] for n in kept][:2] == ["capture_stale", "runtime_expiring"] and len(kept) == 8
    assert [n["code"] for n in kept][2:] == [n["code"] for n in info][:6]


def test_request_status_never_rules_out_a_runtime_it_couldnt_verify():
    base = {"status": "outcome_unknown", "journal_state": "started"}
    current = {"state": "running", "runtime_epoch": OTHER_EPOCH}
    (action,) = listen._status_actions(base, None, None, current, 0, 1)
    assert "1 runtime's records don't verify" in action and "No runtime records" not in action
    assert listen._status_actions(base, None, None, current, 0, 0) == [
        "No runtime records this request; call listen_observe, then use a new request_id."]


def test_start_outcomes_without_a_runtime_map_to_failed_and_outcome_unknown(tmp_path):
    session = {"config": {"adapter": {"kind": "synthetic"}, "routes": []}}
    runtime = {"runtime_epoch": OTHER_EPOCH, "ordinal": 1, "runtime_dir": "runtimes/0001-" + OTHER_EPOCH}
    common = {"start_outcome": "host_failed", "replayed": False, "previous_runtime": None}
    failed = listen._failed_receipt(tmp_path, session, "start-1", tmp_path, runtime,
                                    {"startup_failure": {"stage": "socket", "error": "bind failed"},
                                     "terminal": None, "exit_code": 1, "stderr_tail": "x" * 5000}, common)
    assert failed["status"] == "failed" and failed["startup_failure"]["stage"] == "socket"
    assert len(failed["startup_failure"]["stderr_tail"]) == 2048
    unconfirmed = listen._unconfirmed_receipt(tmp_path, session, "start-1", tmp_path, runtime, {},
                                              {**common, "start_outcome": "start_unconfirmed"})
    assert unconfirmed["status"] == "outcome_unknown" and unconfirmed["lease_state"] == "held"
    assert unconfirmed["reason"] == "lease_held_no_connection"
    assert failed["coverage"]["capture"] == "not_started" and "during startup" in failed["next_actions"][0]
    missing = listen._failed_receipt(tmp_path, session, "start-1", tmp_path, runtime,
                                     {"startup_failure": None, "startup_failure_record": "missing",
                                      "terminal": None, "exit_code": 1, "stderr_tail": "ImportError: numpy"},
                                     common)
    assert missing["startup_failure"] == {"stage": None, "error": None, "record": "missing", "exit_code": 1,
                                          "stderr_tail": "ImportError: numpy"}
    assert "startup_failure says why" not in missing["next_actions"][0]
    assert "startup_failure.exit_code and startup_failure.stderr_tail" in missing["next_actions"][0]
    crash_record = {"schema": "pocket.listen-terminal/v1", "runtime_epoch": OTHER_EPOCH, "state": "failed",
                    "error": "capture: boom", "capture": {"final_state": "stopped", "streams": [{}]}}
    terminal = put_record(crash_record, tmp_path)
    crashed = listen._failed_receipt(tmp_path, session, "start-1", tmp_path, runtime,
                                     {"startup_failure": None, "exit_code": 1, "stderr_tail": None,
                                      "terminal": (terminal, crash_record)}, common)
    assert crashed["startup_failure"]["error"] == "capture: boom"
    assert crashed["coverage"]["capture"] == "stopped" and crashed["terminal"]["state"] == "failed"
    assert "started, then failed" in crashed["next_actions"][0]
    stopped = listen._stopped_receipt(tmp_path, session, "start-1", tmp_path, runtime,
                                      {"terminal": (terminal, {"state": "stopped", "cause": "listen_stop"}),
                                       "exit_code": 0}, {**common, "start_outcome": "stopped_during_start"})
    assert stopped["status"] == "partial" and stopped["start_outcome"] == "stopped_during_start"
    assert "listen_stop" in stopped["next_actions"][0] and stopped["stopped_runtime"]["terminal"] == terminal


def test_events_receipts_report_loss_as_conflict_or_partial_never_ok(runtimes):
    config = make_config(script(tone(RATE * 4)), MASTER_CUE_UNLABELED[:1],
                         measurement={"clip_threshold_dbfs": -0.1, "window_frames": 960,
                                      "near_silence": {"level_dbfs": -60.0, "min_duration_ms": 2000}},
                         delivery={"meter_coalesce_windows": 1, "normal_retention_bytes": 65536})
    session_dir, _, started = runtimes.start(config)
    start = started["cursor"]

    def evicted():
        observed = listen.listen_observe(session_dir)
        return observed if observed["observation"]["broker"]["normal"]["evicted_through"] > 3 else None
    wait_until(evicted)
    expired = listen.listen_events(session_dir, start)
    assert expired["status"] == "conflict" and expired["events_status"] == "cursor_expired"
    assert "events" not in expired and "resume_from_earliest" in expired["next_actions"][0]
    resumed = listen.listen_events(session_dir, start, on_expired="resume_from_earliest", max_events=8)
    assert resumed["status"] == "partial" and resumed["events_status"] == "page"
    assert resumed["missing"][0]["from_sequence"] == 1
    assert resumed["missing"][0]["cause"] == "retention_evicted"
    assert resumed["events"][0]["evidence"]["type"] == "runtime_started"
    current = listen.listen_events(session_dir, listen.listen_observe(session_dir)["cursor"], wait_ms=200)
    assert current["status"] == "ok" and current["missing"] == []
    mismatch = listen.listen_events(session_dir, {"runtime_epoch": OTHER_EPOCH, "sequence": 0})
    assert mismatch["status"] == "conflict" and mismatch["events_status"] == "runtime_epoch_mismatch"
    assert "No runtime of this session" in mismatch["next_actions"][0]
    assert started["runtime"]["runtime_epoch"] in mismatch["next_actions"][1]


def test_a_cursor_from_a_stopped_runtime_names_what_it_missed_while_a_newer_runtime_runs(runtimes):
    session_dir, sha, started = runtimes.start(make_config(script(tone(RATE * 3))))
    first = started["cursor"]
    stopped = listen.listen_stop(session_dir, "stop-a", first["runtime_epoch"])
    assert stopped["terminal_sealed"] is True
    last = stopped["terminal"]["broker"]["last_sequence"]
    alone = listen.listen_events(session_dir, first)
    assert alone["events_status"] == "runtime_not_running" and alone["missing"][0]["through_sequence"] == last
    later = listen.listen_start(session_dir, "start-b", sha)
    assert later["start_outcome"] == "capturing"
    page = listen.listen_events(session_dir, first)
    assert (page["status"], page["events_status"]) == ("conflict", "runtime_epoch_mismatch")
    assert "events" not in page
    assert page["missing"] == alone["missing"] == [{
        "from_sequence": 1, "through_sequence": last, "through_is_lower_bound": False, "delivery_class": "all",
        "cause": "runtime_stopped", "health_in_range": "runtime_journal"}]
    assert page["cursor_runtime"] == alone["cursor_runtime"]
    assert page["cursor_runtime"]["terminal"] == "sealed"
    assert page["next_actions"][0].startswith(f"Sequences 1 to {last} of the cursor's runtime")
    assert later["runtime"]["runtime_epoch"] in page["next_actions"][1]
    unknown = listen.listen_events(session_dir, {"runtime_epoch": OTHER_EPOCH, "sequence": 0})
    assert unknown["missing"] == [] and unknown["cursor_runtime"]["terminal"] == "unknown"
    assert "No runtime of this session" in unknown["next_actions"][0]
    assert later["runtime"]["runtime_epoch"] in unknown["next_actions"][1]


def _journal_record(terminal, broker=None, **journal):
    if terminal == "sealed":
        base = {"state": "writing", "lines": 40, "dropped_lines": 0, "closed": True, "lines_unwritten_at_close": 0}
        return {"runtime_epoch": OTHER_EPOCH, "terminal": "sealed",
                "broker": {"last_sequence": 12, "publish_hook_errors": 0, **(broker or {})},
                "journal": {**base, **journal}}
    base = {"present": True, "verified_lines": 9, "last_sequence": 12, "chain_ok": True, "torn_tail_bytes": 0,
            "dropped_lines_marked": 0, "stopped_writing": None}
    return {"runtime_epoch": OTHER_EPOCH, "terminal": "missing", "journal": {**base, **journal}}


@pytest.mark.parametrize("record, kept", [
    (_journal_record("sealed"), "runtime_journal"),
    (_journal_record("sealed", state="exhausted", dropped_lines=10), "runtime_journal_incomplete"),
    (_journal_record("sealed", dropped_lines=3), "runtime_journal_incomplete"),
    (_journal_record("sealed", closed=False, lines_unwritten_at_close=4), "runtime_journal_incomplete"),
    (_journal_record("sealed", broker={"publish_hook_errors": 1}), "runtime_journal_incomplete"),
    (_journal_record("missing"), "runtime_journal"),
    (_journal_record("missing", stopped_writing="failed"), "runtime_journal_incomplete"),
    (_journal_record("missing", dropped_lines_marked=2), "runtime_journal_incomplete"),
    (_journal_record("missing", torn_tail_bytes=31), "runtime_journal_incomplete"),
    (_journal_record("missing", chain_ok=False), "runtime_journal_incomplete"),
])
def test_an_unread_range_says_the_journal_keeps_its_health_events_only_when_it_kept_every_line(
        tmp_path, monkeypatch, record, kept):
    monkeypatch.setattr(listen, "_runtime_row", lambda root, epoch: (1, epoch, tmp_path))
    monkeypatch.setattr(listen, "_runtime_record", lambda root, row: record)
    found, missing = listen._unread_after_stop(tmp_path, {"runtime_epoch": OTHER_EPOCH, "sequence": 4})
    assert missing[0]["health_in_range"] == kept
    assert (missing[0]["from_sequence"], missing[0]["through_sequence"]) == (5, 12)
    assert found["journal"] == record["journal"]
    (step,) = listen._unread_steps(found, missing)
    assert ("didn't keep every line" in step) is (kept != "runtime_journal")
    sealed = record["terminal"] == "sealed"
    assert missing[0]["through_is_lower_bound"] is not sealed
    assert ("its terminal summarizes them" in step) is sealed
    assert ("5 to at least 12" in step) is not sealed and ("sealed no terminal" in step) is not sealed
    assert ("keeps its health events" in step) is (sealed and kept == "runtime_journal")


@pytest.mark.parametrize("record, warned", [
    (_journal_record("sealed", broker={"last_sequence": 3}), None),
    (_journal_record("missing", last_sequence=3), "sealed no terminal"),
    ({"runtime_epoch": OTHER_EPOCH, "terminal": "integrity_mismatch"}, "doesn't verify"),
])
def test_a_cursor_past_what_a_stopped_runtime_recorded_is_told_loss_is_ruled_out_only_by_a_sealed_terminal(
        tmp_path, monkeypatch, record, warned):
    session_dir = listen.listen_prepare(str(tmp_path / "session"), make_config())["session"]["session_dir"]
    monkeypatch.setattr(listen, "_runtime_row", lambda root, epoch: (1, epoch, tmp_path))
    monkeypatch.setattr(listen, "_runtime_record", lambda root, row: record)
    page = listen.listen_events(session_dir, {"runtime_epoch": OTHER_EPOCH, "sequence": 4})
    assert page["events_status"] == "runtime_not_running" and page["missing"] == []
    assert page["cursor_runtime"]["terminal"] == record["terminal"]
    if warned is None:
        assert page["next_actions"] == [listen._RESTART_STEP]
    else:
        assert warned in page["next_actions"][0] and "lost" in page["next_actions"][0]
        assert page["next_actions"][1:] == [listen._RESTART_STEP]


def test_a_runtime_that_stops_between_the_lease_probe_and_the_call_reads_as_not_running(tmp_path, monkeypatch):
    prepared = listen.listen_prepare(str(tmp_path / "session"), make_config())
    session_dir = prepared["session"]["session_dir"]
    probes = []

    def current(root):
        probes.append(root)
        return ({"state": "running", "runtime_epoch": OTHER_EPOCH, "runtime_dir": str(tmp_path)} if len(probes) == 1
                else {"state": "not_running", "runtime_epoch": OTHER_EPOCH, "runtime_dir": str(tmp_path)})

    def gone(*args, **kwargs):
        raise listen_host.RuntimeUnreachable("lease_held_no_connection")

    monkeypatch.setattr(listen_host, "current_runtime", current)
    monkeypatch.setattr(listen_host, "call", gone)
    events = listen.listen_events(session_dir, {"runtime_epoch": OTHER_EPOCH, "sequence": 0})
    assert (events["status"], events["events_status"]) == ("needs_input", "runtime_not_running")
    assert "reason" not in events and len(probes) == 2
    probes.clear()
    observed = listen.listen_observe(session_dir)
    assert observed["status"] == "needs_input" and observed["runtime"]["state"] == "not_running"
    monkeypatch.setattr(listen_host, "current_runtime",
                        lambda root: {"state": "running", "runtime_epoch": OTHER_EPOCH, "runtime_dir": str(tmp_path)})
    held = listen.listen_events(session_dir, {"runtime_epoch": OTHER_EPOCH, "sequence": 0})
    assert (held["status"], held["reason"]) == ("outcome_unknown", "lease_held_no_connection")
    assert listen.listen_observe(session_dir)["status"] == "outcome_unknown"


def test_a_session_directory_is_one_only_you_can_open_whatever_the_umask(tmp_path):
    previous = os.umask(0o002)
    try:
        session_dir = listen.listen_prepare(str(tmp_path / "session"), make_config())["session"][
            "session_dir"]
    finally:
        os.umask(previous)
    assert stat.S_IMODE(os.lstat(session_dir).st_mode) == 0o700
    previous = os.umask(0o300)
    try:
        created = listen._new_directory(str(tmp_path / "narrow"))
    finally:
        os.umask(previous)
    assert stat.S_IMODE(os.lstat(created).st_mode) == 0o700


@pytest.mark.skipif(sys.platform != "darwin", reason="extended access lists are a macOS check")
def test_a_session_directory_keeps_no_access_list_its_parent_would_pass_on(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o700)
    subprocess.run(["chmod", "+a", "user:nobody allow list,search,read,file_inherit,directory_inherit",
                    str(shared)], check=True, timeout=30)
    (shared / "plain").mkdir(mode=0o700)
    assert listen_native.acl_grants_access(shared / "plain")
    session_dir = listen.listen_prepare(str(shared / "session"), make_config())["session"]["session_dir"]
    assert not listen_native.acl_grants_access(session_dir)


def test_a_session_directory_on_a_volume_that_ignores_ownership_is_refused(tmp_path, monkeypatch):
    parent = tmp_path / "external"
    monkeypatch.setattr(listen_native, "volume_ignores_ownership", lambda where: where == str(parent))
    error = raised(lambda: listen.listen_prepare(str(parent / "session"), make_config()))
    assert error.code == "io_error" and "ignores ownership" in str(error)
    assert parent.is_dir() and not (parent / "session").exists()


def test_a_session_directory_swapped_for_a_symlink_as_it_is_created_is_refused_never_followed(
        tmp_path, monkeypatch):
    theirs = tmp_path / "theirs"
    theirs.mkdir(mode=0o755)
    named = tmp_path / "session"
    real_mkdir = Path.mkdir

    def swapped(self, *args, **kwargs):
        real_mkdir(self, *args, **kwargs)
        if self == named:
            self.rename(tmp_path / "moved")
            self.symlink_to(theirs)
    monkeypatch.setattr(Path, "mkdir", swapped)
    error = raised(lambda: listen.listen_prepare(str(named), make_config()))
    assert error.code == "io_error"
    assert stat.S_IMODE(os.stat(theirs).st_mode) == 0o755 and list(theirs.iterdir()) == []


def test_a_name_that_is_a_dangling_symlink_is_refused_never_followed(tmp_path):
    target = tmp_path / "theirs" / "session"
    (tmp_path / "theirs").mkdir()
    link = tmp_path / "named"
    link.symlink_to(target)
    error = raised(lambda: listen.listen_prepare(str(link), make_config()))
    assert error.code == "io_error" and "already exists" in str(error)
    assert not target.exists() and link.is_symlink()


def test_events_validation_runs_before_any_runtime_and_a_stopped_session_is_needs_input(tmp_path,
                                                                                         monkeypatch):
    prepared = listen.listen_prepare(str(tmp_path / "session"), make_config())
    session_dir = prepared["session"]["session_dir"]
    monkeypatch.setattr(listen_host, "call", lambda *a, **k: pytest.fail("nothing is running"))
    cursor = {"runtime_epoch": OTHER_EPOCH, "sequence": 0}
    for args, hint in [({"max_events": 257}, listen._PAGE_HINT), ({"max_bytes": 16383}, listen._PAGE_HINT),
                       ({"wait_ms": 1001}, listen._PAGE_HINT),
                       ({"kinds": ["measurement"]}, listen._KINDS_HINT),
                       ({"routes": ["booth"]}, "listen_observe lists route_ids."),
                       ({"kinds": [["health"]]}, listen._KINDS_HINT),
                       ({"max_events": True}, listen._PAGE_HINT)]:
        assert raised(lambda args=args: listen.listen_events(session_dir, cursor, **args)).hint == hint
    assert raised(lambda: listen.listen_events(session_dir, {"sequence": 0})).hint == listen._CURSOR_HINT
    result = listen.listen_events(session_dir, cursor, routes=["cue"], kinds=["health", "measurement"])
    assert result["status"] == "needs_input" and result["events_status"] == "runtime_not_running"
    assert result["previous_runtime"] is None
    observed = listen.listen_observe(session_dir, expected_runtime_epoch=OTHER_EPOCH)
    assert observed["status"] == "needs_input" and observed["expected_runtime_epoch_matches"] is False


def _code_literals(path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "code" and isinstance(keyword.value, ast.Constant):
                    yield keyword.value.value


def test_listen_raises_only_existing_error_codes_with_valid_hints(walkthrough, tmp_path, monkeypatch):
    for path in sorted(PACKAGE.glob("listen*.py")):
        for code in _code_literals(path):
            assert code in CODES, (path.name, code)
    prepared = listen.listen_prepare(str(tmp_path / "session"), make_config())
    session_dir = prepared["session"]["session_dir"]
    tampered = listen.listen_prepare(str(tmp_path / "tampered"), make_config())
    record = Path(tampered["session"]["session_dir"]) / tampered["artifacts"]["session"]["artifact_uri"]
    record.write_bytes(record.read_bytes() + b" ")
    locked = listen.listen_prepare(str(tmp_path / "locked"), make_config())
    (Path(locked["session"]["session_dir"]) / "requests" / "start-1" / "lock").mkdir(parents=True)
    listen.listen_stop(session_dir, "stop-1", OTHER_EPOCH)
    cursor = {"runtime_epoch": OTHER_EPOCH, "sequence": 0}

    def config_with(**changes):
        return {**make_config(), **changes}

    cases = {
        "E1": ("invalid_request", lambda: listen.listen_prepare(str(tmp_path / "e1"), config_with(
            measurement={"near_silence": {"level_dbfs": -60.0, "min_duration_ms": 2000}}))),
        "E2": ("invalid_request", lambda: listen.listen_prepare(str(tmp_path / "e2"), config_with(
            device={"name": "Practice Duo", "expected_sample_rate": RATE}))),
        "E3": ("invalid_request", lambda: listen.listen_prepare(str(tmp_path / "e3"), config_with(
            routes=[_route("master", "master", [0, 1]), _route("booth", "unlabeled", [1])]))),
        "E4": ("invalid_request", lambda: listen.listen_prepare(str(tmp_path / "e4"), config_with(
            routes=[_route("cue-left", "unlabeled", [0])]))),
        "E5": ("invalid_request", lambda: listen.listen_prepare(str(tmp_path / "e5"), config_with(
            device={"uid": "BlackHole2ch_UID", "expected_sample_rate": RATE}))),
        "E6": ("io_error", lambda: listen.listen_prepare(session_dir, make_config())),
        "E8": ("evidence_mismatch",
               lambda: listen.listen_start(tampered["session"]["session_dir"], "s", "0" * 64)),
        "E9": ("stale_revision", lambda: listen.listen_start(session_dir, "start-1", "0" * 64)),
        "E10": ("idempotency_conflict", lambda: listen.listen_stop(session_dir, "stop-1", "rt-" + "1" * 16)),
        "E11": ("request_not_complete",
                lambda: listen.listen_start(locked["session"]["session_dir"], "start-1",
                                            locked["session"]["config_sha256"])),
        "E12": ("invalid_request", lambda: listen.listen_events(session_dir, cursor, max_events=257)),
        "E13": ("invalid_request", lambda: listen.listen_events(session_dir, cursor, kinds=["measurement"])),
        "E14": ("invalid_request", lambda: listen.listen_events(session_dir, cursor, routes=["nope"])),
    }
    errors = {name: (code, raised(call)) for name, (code, call) in cases.items()}
    with monkeypatch.context() as patch:
        patch.setattr(listen_native.sys, "platform", "linux")
        errors["E7"] = ("unsupported_profile", raised(listen.listen_build_helper))
    errors["E15"] = ("invalid_request", walkthrough["errors"]["cursor_ahead"])
    errors["E16"] = ("invalid_request", walkthrough["errors"]["control_op"])
    assert "never clamps" in errors["E15"][1].hint
    assert errors["E16"][1].hint.startswith("Listen exposes no control operations")
    errors["E9-stop"] = ("stale_revision", walkthrough["errors"]["stale_stop"])
    validator = None
    if importlib.util.find_spec("jsonschema") is not None:
        from jsonschema import Draft202012Validator
        validator = Draft202012Validator(JSON_SCHEMA_V3)
    for name, (code, error) in errors.items():
        envelope = error_envelope(error, version="v3")
        if validator is not None:
            validator.validate(envelope)
        assert envelope["code"] == code, name
        assert valid_hint(envelope.get("hint")), name


def test_build_helper_discovery_needs_a_toolchain_behind_the_xcode_select_shim(monkeypatch, tmp_path):
    monkeypatch.delenv("DEVELOPER_DIR", raising=False)
    monkeypatch.setattr(listen_native, "XCODE_SELECT_LINK", tmp_path / "no-link")
    monkeypatch.setattr(listen_native, "DEFAULT_DEVELOPER_DIRS", (tmp_path / "Xcode",))
    with monkeypatch.context() as patch:
        patch.setattr(capabilities.sys, "platform", "darwin")
        patch.setattr(capabilities.platform, "mac_ver", lambda: ("15.1", ("", "", ""), ""))
        patch.setattr(capabilities.shutil, "which", lambda name: listen_native.SWIFTC_SHIM)
        rows = capabilities.capabilities_list(domain="listen", limit=50)["capabilities"]
    (build,) = [row for row in rows if row["public_tool"] == "listen_build_helper"]
    assert build["status"] == "requires_optional_setup"


def _discovery(monkeypatch, *, platform, os_name, mac="15.1", swiftc=True):
    with monkeypatch.context() as patch:
        patch.setattr(capabilities.sys, "platform", platform)
        version = mac if platform == "darwin" else ""
        patch.setattr(capabilities.platform, "mac_ver", lambda: (version, ("", "", ""), ""))
        patch.setattr(capabilities.shutil, "which", lambda name: f"/tools/{name}" if swiftc else None)
        patch.setattr(capabilities.os, "name", os_name)
        result = capabilities.capabilities_list(domain="listen", limit=50)
    return {row["public_tool"] or row["proposed_tool"]: row for row in result["capabilities"]}


@pytest.mark.parametrize("platform,os_name,mac,swiftc,build,runtime", [
    ("darwin", "posix", "15.1", True, "available", "requires_native_setup"),
    ("darwin", "posix", "26.0", False, "requires_optional_setup", "requires_native_setup"),
    ("darwin", "posix", "14.6", True, "unsupported", "requires_native_setup"),
    ("linux", "posix", "", True, "unsupported", "requires_native_setup"),
    ("win32", "nt", "", True, "unsupported", "unsupported"),
])
def test_listen_discovery_rows_families_gates_status_and_side_effects(monkeypatch, platform, os_name, mac,
                                                                    swiftc, build, runtime):
    rows = _discovery(monkeypatch, platform=platform, os_name=os_name, mac=mac, swiftc=swiftc)
    assert set(rows) == {*LISTEN_PROVIDERS, *LISTEN_GATES}
    assert rows["listen_build_helper"]["status"] == build
    assert {rows[name]["status"] for name in RUNTIME_ROWS} == {runtime}
    assert rows["listen_prepare"]["status"] == ("unsupported" if os_name != "posix" else "available")
    assert rows["listen_request_status"]["status"] == "available"
    for name in LISTEN_PROVIDERS:
        row = rows[name]
        assert row["native_verified"] is False and row["implementation_status"] == "implemented"
        assert row["accepted_artifact_schemas"] == []
        assert row["provider"] == f"pocket_music.listen.{name}"
        assert row["required_profile"] == ("coreaudio-hal-input/v1" if name == "listen_build_helper"
                                           else "listen-runtime/v1")
    assert rows["listen_prepare"]["returned_artifact_schemas"] == ["pocket.listen-session/v1"]
    terminal_rows = {"listen_start", "listen_observe", "listen_events", "listen_stop",
                     "listen_request_status"}
    assert all(rows[name]["returned_artifact_schemas"] == ["pocket.listen-terminal/v1"]
               for name in terminal_rows)
    returning = {name for name in LISTEN_PROVIDERS if rows[name]["returned_artifact_schemas"]}
    assert returning == {"listen_prepare", *terminal_rows}
    assert any("Synthetic adapter" in text for text in rows["listen_start"]["prerequisites"])
    assert any("swiftc" in text for text in rows["listen_build_helper"]["prerequisites"])
    assert rows["listen_devices"]["side_effects"] == ["conditional_owned_short_lived_probe_process",
                                                      "conditional_temporary_helper_copy"]
    assert rows["listen_build_helper"]["side_effects"] == ["new_local_artifacts",
                                                           "local_build_registry_entry"]
    assert {"owned_local_background_process", "local_unix_socket", "local_runtime_journal",
            "audio_input_copy_only"} <= set(rows["listen_start"]["side_effects"])
    assert {"conditional_owned_helper_process", "conditional_temporary_helper_copy"} <= set(
        rows["listen_start"]["side_effects"])
    copying = {name for name in LISTEN_PROVIDERS
               if "conditional_temporary_helper_copy" in rows[name]["side_effects"]}
    assert copying == {"listen_devices", "listen_start"}
    assert {"owned_runtime_stop", "local_runtime_journal"} <= set(rows["listen_stop"]["side_effects"])
    assert rows["listen_observe"]["side_effects"] == []
    assert all("not yet qualified" in rows[name]["conditional_dependencies"][0]
               for name in LISTEN_PROVIDERS if name not in ("listen_build_helper", "listen_request_status"))
    builds_in = "build_id of a helper build listen_build_helper compiled into Listen's own folder for this user"
    for name in LISTEN_PROVIDERS:
        if name in ("listen_build_helper", "listen_request_status"):
            continue
        dependency = rows[name]["conditional_dependencies"][0]
        assert builds_in in dependency and "POCKET_LISTEN_DIR" in dependency
        assert any("listen_build_helper compiled into Listen's own folder" in text
                   and "copied or moved without its record and that key, is refused" in text
                   for text in rows[name]["prerequisites"])
    (build_prerequisite,) = rows["listen_build_helper"]["prerequisites"]
    assert "returns its build_id" in build_prerequisite and "output" not in build_prerequisite
    assert "microphone access already granted" in rows["listen_start"]["conditional_dependencies"][0]
    assert any("microphone access already granted" in text for text in rows["listen_start"]["prerequisites"])
    for name in ("listen_observe", "listen_events", "listen_stop"):
        assert "checks no microphone access itself" in rows[name]["conditional_dependencies"][0]
        assert not any("already granted" in text for text in
                       rows[name]["prerequisites"] + rows[name]["conditional_dependencies"])
    for name, says in (("listen_devices", "reports microphone access rather than needing it"),
                       ("listen_prepare", "microphone access is checked when listen_start runs")):
        assert says in rows[name]["conditional_dependencies"][0]
        assert not any("already granted" in text for text in
                       rows[name]["prerequisites"] + rows[name]["conditional_dependencies"])
    assert rows["listen_request_status"]["conditional_dependencies"] == []
    assert rows["listen_request_status"]["prerequisites"] == [
        "File reads only, on any system; it never contacts a runtime or a device"]
    for name in LISTEN_GATES:
        gate = rows[name]
        assert gate["public_tool"] is None and gate["implementation_status"] == "not_implemented"
        assert gate["status"] == "documented_unprobed" and gate["native_verified"] is False
    assert "would establish which channels carry cue (until one is measured, cue stays unavailable" in rows[
        "listen_calibrate"]["summary"]
    assert "headphone" in rows["listen_calibrate"]["summary"]
    assert "clock test" in rows["listen_calibrate"]["summary"] and "drift" in rows["listen_calibrate"]["summary"]


def test_listen_discovery_never_executes_tools_and_pages_stay_bounded(monkeypatch):
    forbid_processes(monkeypatch)
    cursor, sizes = None, []
    while True:
        page = capabilities.capabilities_list(limit=1, cursor=cursor)
        sizes.append(len(canonical_bytes(page)))
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert max(sizes) <= 4096
    cursor = None
    while True:
        page = capabilities.capabilities_list(limit=50, cursor=cursor)
        assert len(canonical_bytes(page)) <= 65536
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert page["coverage"]["discovery_executes_tools"] is False


def test_build_helper_runs_only_swiftc_and_never_the_helper(tmp_path, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        assert argv[0] == "swiftc", argv
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, "Apple Swift version 6.3 (fixture)\nTarget: test\n",
                                               "")
        Path(argv[argv.index("-o") + 1]).write_bytes(b"compiled fixture")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def forbidden(*args, **kwargs):
        pytest.fail("building must never run the helper")
    monkeypatch.setattr(listen_native.sys, "platform", "darwin")
    monkeypatch.setattr(listen_native.platform, "mac_ver", lambda: ("15.1", ("", "", ""), ""))
    monkeypatch.setattr(listen_native.shutil, "which", lambda name: "/tools/swiftc")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    result = listen.listen_build_helper()
    compile_call, version_call = calls
    build = result["build"]
    assert build["build_dir"] == str(listen_native.registry_dir() / "builds" / build["build_id"])
    assert compile_call[1:5] == ["-O", "-enforce-exclusivity=unchecked", "-swift-version", "5"]
    assert "-module-cache-path" in compile_call
    assert compile_call[-2:] == ["-o", str(Path(build["build_dir"]) / "listen-capture")]
    assert version_call == ["swiftc", "--version"]
    assert build["build_status"] == "built_not_started" and build["schema"] == "pocket.listen-build/v1"
    assert build["swiftc_version"] == "Apple Swift version 6.3 (fixture)"
    assert build["swift_language_mode"] == "5"
    assert build["native_acceptance"] == "separate_from_build" and build["wire"] == "PLH1"
    assert listen_native.verify_build(build["build_id"])["helper_sha256"] == build["files"]["listen-capture"]
    key = (listen_native.registry_dir() / listen_native.REGISTRY_KEY_NAME).read_bytes()
    assert len(key) == 32 and key.hex() not in json.dumps(result) and repr(key) not in json.dumps(result)
    assert result["coverage"]["helper_executed"] is False and result["coverage"]["device_opened"] is False
    adapter = json.dumps({"build_id": build["build_id"], "kind": "coreaudio"}, separators=(",", ":"))
    assert result["next_actions"][0] == f"listen_devices with {adapter} lists inputs by uid; it opens no stream"
    def slow_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
    monkeypatch.setattr(subprocess, "run", slow_run)
    timed_out = raised(listen.listen_build_helper)
    assert timed_out.code == "unsupported_profile" and "time limit" in timed_out.hint
    assert "leaves nothing behind" in timed_out.hint and "swiftc)" not in timed_out.hint
    assert [p.name for p in Path(build["build_dir"]).parent.iterdir() if p.is_dir()] == [build["build_id"]]
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(listen_native.platform, "mac_ver", lambda: ("14.6", ("", "", ""), ""))
    assert raised(listen.listen_build_helper).code == "unsupported_profile"
    assert len(calls) == 2


def _cli(tmp_path, *args):
    return subprocess.run([sys.executable, "-m", "pocket_music.cli", "--error-format", "v3", *map(str, args)],
                          env={**os.environ, "PYTHONPATH": str(SRC)}, capture_output=True, text=True,
                          timeout=60, check=False)


def _spec(tmp_path, name, value):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(value))
    return path


def _portable(prepared):
    session = {k: v for k, v in prepared["session"].items() if k not in ("session_dir", "session_id")}
    return {**{k: v for k, v in prepared.items() if k != "artifacts"}, "session": session}


def test_listen_cli_group_flat_commands_and_python_share_one_contract(tmp_path):
    adapter = {"kind": "synthetic", "script": script()}
    devices = _spec(tmp_path, "devices", {"adapter": adapter})
    for command in (("listen", "devices"), ("listen-devices",)):
        result = _cli(tmp_path, *command, "--spec", devices)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == listen.listen_devices(adapter)
    config = make_config()
    result = _cli(tmp_path, "listen", "prepare", "--spec", _spec(tmp_path, "prepare", {"config": config}),
                  "--output", tmp_path / "cli-session")
    assert result.returncode == 0, result.stderr
    from_cli = json.loads(result.stdout)
    direct = listen.listen_prepare(str(tmp_path / "py-session"), config)
    assert _portable(from_cli) == _portable(direct)
    assert from_cli["session"]["session_dir"] == str((tmp_path / "cli-session").resolve())
    observe = _spec(tmp_path, "observe", {"session_dir": from_cli["session"]["session_dir"]})
    result = _cli(tmp_path, "listen-observe", "--spec", observe)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == listen.listen_observe(from_cli["session"]["session_dir"])
    assert json.loads(result.stdout)["status"] == "needs_input"
    named = _spec(tmp_path, "named", {"config": {**config, "device": {"name": "Practice Duo",
                                                                      "expected_sample_rate": RATE}}})
    result = _cli(tmp_path, "listen", "prepare", "--spec", named, "--output", tmp_path / "named-session")
    assert result.returncode == 2 and "Traceback" not in result.stderr
    envelope = json.loads(result.stderr)
    assert envelope["code"] == "invalid_request" and envelope["hint"] == listen._NAME_HINT
    assert not (tmp_path / "named-session").exists()
    huge = copy.deepcopy(config)
    huge["measurement"]["clip_threshold_dbfs"] = 10 ** 400
    result = _cli(tmp_path, "listen", "prepare", "--spec", _spec(tmp_path, "huge", {"config": huge}),
                  "--output", tmp_path / "huge-session")
    assert result.returncode == 2 and "Traceback" not in result.stderr
    assert json.loads(result.stderr)["code"] == "invalid_request"
    help_text = _cli(tmp_path, "listen", "--help")
    assert help_text.returncode == 0 and "start" in help_text.stdout and "request-status" in help_text.stdout
    build_help = _cli(tmp_path, "listen", "build-helper", "--help")
    assert build_help.returncode == 0 and "New response JSON file; otherwise stdout" in build_help.stdout


def _tool_error(call):
    from mcp.server.fastmcp.exceptions import ToolError
    with pytest.raises(ToolError) as caught:
        asyncio.run(call)
    return json.loads(str(caught.value))


def _definition(schema, title):
    return next(d for d in schema["$defs"].values() if d.get("title") == title)


def _literal(prop):
    return prop.get("enum", [prop.get("const")])


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="Optional agent extra")
def test_listen_mcp_schemas_annotations_and_strict_validation(tmp_path):
    from pocket_music.mcp_server import build_server

    server = build_server(error_format="v3")
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert set(LISTEN_PROVIDERS) <= set(tools)
    prepare = tools["listen_prepare"].inputSchema
    roles = _literal(_definition(prepare, "ListenRoute")["properties"]["role"])
    assert set(roles) == {"master", "cue", "unlabeled"}
    assert _literal(_definition(prepare, "ListenSyntheticAdapter")["properties"]["kind"]) == ["synthetic"]
    assert _literal(_definition(prepare, "ListenCoreAudioAdapter")["properties"]["kind"]) == ["coreaudio"]
    assert set(_definition(prepare, "ListenCoreAudioAdapter")["properties"]) == {"kind", "build_id"}
    assert not tools["listen_build_helper"].inputSchema.get("properties")
    segment_kinds = _literal(_definition(prepare, "ListenSyntheticSegment")["properties"]["kind"])
    assert {"tone", "gap", "stall", "device_lost", "rate_change", "channel_reorder", "hogged"} <= \
        set(segment_kinds)
    rates = _definition(prepare, "ListenDevice")["properties"]["expected_sample_rate"]
    assert _literal(rates) == [44100, 48000, 88200, 96000]
    assert all(d.get("additionalProperties") is False
               for d in prepare["$defs"].values() if d.get("type") == "object")
    events = tools["listen_events"].inputSchema
    kinds = events["properties"]["kinds"]["anyOf"][0]["items"]
    assert set(_literal(events["$defs"][kinds["$ref"].rsplit("/", 1)[1]] if "$ref" in kinds else kinds)) == \
        {"measurement", "health"}
    assert set(_literal(events["properties"]["on_expired"])) == {"refuse", "resume_from_earliest"}
    for name in LISTEN_PROVIDERS:
        annotations = tools[name].annotations
        read_only = name in ("listen_observe", "listen_events", "listen_request_status")
        assert annotations.readOnlyHint is read_only, name
        assert annotations.idempotentHint is (read_only or name in ("listen_devices", "listen_start",
                                                                    "listen_stop")), name
        assert annotations.destructiveHint is False and annotations.openWorldHint is False
    adapter = {"kind": "synthetic", "script": script()}
    result = asyncio.run(server.call_tool("listen_devices", {"adapter": adapter}))
    assert json.loads(result[0].text) == listen.listen_devices(adapter)
    cursor = {"runtime_epoch": OTHER_EPOCH, "sequence": True}
    error = _tool_error(server.call_tool("listen_events", {"session_dir": str(tmp_path), "after": cursor}))
    assert error["code"] == "invalid_arguments"
    error = _tool_error(server.call_tool("listen_prepare", {"output_dir": str(tmp_path / "x"),
                                                            "config": {**make_config(), "extra": 1}}))
    assert error["code"] == "invalid_arguments" and not (tmp_path / "x").exists()
    named = {**make_config(), "device": {"uid": UID, "name": "Practice Duo", "expected_sample_rate": RATE}}
    error = _tool_error(server.call_tool("listen_prepare", {"output_dir": str(tmp_path / "y"),
                                                            "config": named}))
    assert error["code"] == "invalid_arguments"
    for mixed in ({"kind": "coreaudio", "script": script()}, {"kind": "coreaudio", "build_dir": str(tmp_path)}):
        error = _tool_error(server.call_tool("listen_devices", {"adapter": mixed}))
        assert error["code"] == "invalid_arguments"
    error = _tool_error(server.call_tool("listen_observe", {"session_dir": str(tmp_path / "missing")}))
    assert error["code"] == "evidence_mismatch" and "listen_prepare created" in error["hint"]


def test_every_listen_input_type_refuses_unknown_keys():
    closed = [value for value in vars(listen_types).values() if typing_extensions.is_typeddict(value)]
    assert len(closed) == 17 and all(vars(cls).get("__pydantic_config__") == {"extra": "forbid"} for cls in closed)


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="Optional agent extra")
def test_shuttle_mcp_annotations_are_unchanged():
    from pocket_music.mcp_server import build_server

    tools = {tool.name: tool for tool in asyncio.run(build_server().list_tools())}
    for name, module, _, _, read_only, _ in capabilities.PUBLIC_CAPABILITIES:
        if module == "listen":
            continue
        expected = (read_only or name == "shuttle_act") if module == "shuttle" else True
        assert tools[name].annotations.idempotentHint is expected, name
        assert tools[name].annotations.readOnlyHint is read_only, name
    assert tools["shuttle_build_bridge"].annotations.idempotentHint is False
    assert tools["shuttle_act"].annotations.idempotentHint is True


def _pocket_imports(module):
    tree = ast.parse((PACKAGE / f"{module}.py").read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1:
                found.update([node.module.split(".")[0]] if node.module else [a.name for a in node.names])
            elif node.module and node.module.startswith("pocket_music"):
                found.add(node.module.split(".")[1] if "." in node.module else node.names[0].name)
        elif isinstance(node, ast.Import):
            found.update(a.name.split(".")[1] for a in node.names if a.name.startswith("pocket_music."))
    return {name for name in found if (PACKAGE / f"{name}.py").exists()}


def test_listen_imports_nothing_from_shuttle():
    pending = [path.stem for path in PACKAGE.glob("listen*.py")]
    modules = {"listen", "listen_types", "listen_host", "listen_runtime", "listen_adapters", "listen_native"}
    assert modules <= set(pending)
    seen = set()
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        pending.extend(_pocket_imports(module) - seen)
    assert not {name for name in seen if name.startswith("shuttle")}, seen
    assert seen <= modules | {"artifact_store", "errors", "error_contracts"}, seen


def _slug(heading):
    text = re.sub(r"[^\w\s-]", "", heading).strip().lower()
    return re.sub(r"[-\s]+", "-", text)


def test_listen_doc_links_resolve_in_the_public_site_manifest():
    manifest = json.loads((REPO / "site" / "public-docs.json").read_text())
    pages = {page["source"] for page in manifest["pages"]}
    assert "docs/listen.md" in pages
    groups = {name: entries for group in manifest["nav"] for name, entries in group.items()}
    assert {"Listen: watch live audio": "docs/listen.md"} in groups["Live performance"]
    assert all("docs/listen.md" not in entry.values() for entry in groups["Records and sets"])
    guide = (REPO / "docs" / "listen.md").read_text()
    links = re.findall(r"\]\(([^)\s]+)\)", guide)
    assert links
    for link in links:
        target, _, fragment = link.partition("#")
        assert not target.startswith(("http:", "/")) and "shuttle" not in target, link
        source = (Path("docs") / target).as_posix() if target else "docs/listen.md"
        assert source in pages, link
        if fragment:
            headings = re.findall(r"^#+ (.+)$", (REPO / source).read_text(), flags=re.MULTILINE)
            assert fragment in {_slug(h) for h in headings}, link
    assert "(listen.md)" in (REPO / "docs" / "README.md").read_text()
    assert "[Listen](docs/listen.md)" in (REPO / "README.md").read_text()
