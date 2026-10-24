# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import ast
import errno
import hashlib
import io
import json
import os
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from conftest import UNRECORDED_BUILD_ID
from listen_fake_helper import write_fake_build

from pocket_music import listen, listen_adapters, listen_host, listen_native
from pocket_music.artifact_store import digest, put_record
from pocket_music.errors import PocketError
from pocket_music.listen_adapters import AdapterBlock, StartResult
from pocket_music.listen_host import (
    ListenHost,
    RuntimeJournal,
    acquire_lease,
    call,
    current_runtime,
    lease_held,
    peer_uid,
    prepare_launch,
    read_connection,
    read_launch,
    read_startup_failure,
    read_terminal,
    runtime_dirs,
    spawn_host,
    verify_journal,
    wait_for_runtime,
)
from pocket_music.listen_runtime import FakeClock, Watchdog

RATE = 48000
UID = "synthetic:duo-a"
MASTER = ({"route_id": "master", "role": "master", "channels": [0, 1]},)
AUDIO_SUFFIXES = (".wav", ".aif", ".aiff", ".caf", ".flac", ".mp3")
START_WAIT_TEST_S = 60.0
STOP_REPLY_S = 30.0
STOP_HANG_GUARD_S = 30
OBSERVE_HANG_GUARD_S = 15
SLOW_STARTUP_ALLOWANCE_S = 120
LOADED_FAKE_START_ALLOWANCE_S = 30.0
LOADED_FAKE_CLOSE_ALLOWANCE_NS = 30_000_000_000
LOADED_FAKE_CLOSE_JOIN_ALLOWANCE_S = 45.0
SRC = str(Path(listen_host.__file__).resolve().parents[1])


def tone(frames, dbfs=-12.0, channels=(0, 1)):
    return {"kind": "tone", "frames": frames, "tones": [{"channel": c, "hz": 480.0, "dbfs": dbfs} for c in channels]}


def script(*segments, rate=RATE, channels=2, uid=UID):
    return {"block_frames": 480,
            "devices": [{"uid": uid, "name": "Practice Duo", "input_channels": channels, "sample_rate": rate}],
            "segments": list(segments) or [tone(RATE * 3)]}


def resolved_config(spec=None, routes=MASTER, *, adapter=None, uid=UID, rate=RATE, max_runtime_seconds=14400):
    return {"adapter": adapter or {"kind": "synthetic", "script": spec or script()},
            "device": {"uid": uid, "expected_sample_rate": rate}, "routes": [dict(r) for r in routes],
            "measurement": {"clip_threshold_dbfs": -0.1, "window_frames": rate // 10,
                            "near_silence": {"level_dbfs": -60.0, "min_duration_ms": 2000}},
            "buffering": {"ring_seconds": 10}, "health": {"stale_after_ms": 500},
            "delivery": {"meter_coalesce_windows": 1, "normal_retention_events": 8192,
                         "normal_retention_bytes": 4194304, "health_retention_events": 1024,
                         "health_retention_bytes": 524288},
            "limits": {"max_runtime_seconds": max_runtime_seconds}}


def seal_session(directory, config, *, build=None):
    root = Path(directory)
    root.mkdir()
    os.close(os.open(root / "runtime.lock", os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600))
    coreaudio = config["adapter"]["kind"] == "coreaudio"
    routes, _ = listen.resolve_routes(config["routes"], 64)
    record = {"schema": "pocket.listen-session/v1", "session_id": os.urandom(16).hex(),
              "created_wall_utc": "2026-10-22T00:00:00.000Z", "config": config, "config_sha256": digest(config),
              "adapter_profile": "coreaudio-hal-input/v1" if coreaudio else "listen-synthetic/v1",
              "build": build, "metered_channels": list(listen.selected_channels(routes)),
              "routes": [{k: r[k] for k in ("route_id", "role", "channels", "metering", "semantic_status",
                                            "route_calibration_id")} for r in routes],
              "memory": {"pcm_ring_bytes": 0, "helper_ring_bytes": None, "broker_max_bytes": 0,
                         "journal_budget_bytes": listen_host.JOURNAL_BUDGET_BYTES, "total_declared_bytes": 0},
              "retention": "in_memory_bounded_window_no_file"}
    handle = put_record(record, root)
    with (root / "listen-session.json").open("x") as stream:
        json.dump({"schema": "pocket.listen-session-pointer/v1", "session": handle}, stream)
    return str(root), record


def entries(runtime):
    return [json.loads(line)["entry"] for line in (Path(runtime) / "journal.ndjson").read_bytes().splitlines()]


def journal_types(runtime):
    return [e["event"]["evidence"]["type"] for e in entries(runtime) if "event" in e]


def epoch_of(runtime):
    return Path(runtime).name.split("-", 1)[1]


def stop_args(runtime, request_id="stop-1"):
    return {"expected_runtime_epoch": epoch_of(runtime), "request_id": request_id}


def wait_until(predicate, seconds=30.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the runtime")


class Daemons:
    def __init__(self):
        self.processes = []
        self.socket_dirs = []

    def launch(self, session_dir, request_id="start-1"):
        fd = acquire_lease(session_dir)
        if fd is None:
            return None, None
        try:
            runtime, _ = prepare_launch(session_dir, request_id=request_id, launch="provider")
            process = spawn_host(session_dir, runtime, fd)
        finally:
            os.close(fd)        # without LOCK_UN: the daemon's inherited description keeps the lease
        self.processes.append(process)
        return process, runtime

    def foreground(self, session_dir, config_sha256):
        process = subprocess.Popen(
            [sys.executable, "-m", "pocket_music.listen_host", "--session-dir", session_dir, "--foreground",
             "--expected-config-sha256", config_sha256],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "PYTHONPATH": SRC})
        self.processes.append(process)
        return process

    def remember_socket(self, runtime):
        self.socket_dirs.append(Path(read_connection(runtime)["socket"]).parent)

    def cleanup(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        for directory in self.socket_dirs:
            (directory / "host.sock").unlink(missing_ok=True)
            if directory.exists():
                directory.rmdir()


@pytest.fixture
def daemons():
    d = Daemons()
    yield d
    d.cleanup()


class Hosts:
    def __init__(self):
        self.hosts = []

    def build(self, session_dir, **options):
        fd = acquire_lease(session_dir)
        assert fd is not None
        runtime, _ = prepare_launch(session_dir, request_id=None, launch="foreground")
        host = ListenHost(session_dir, runtime, lease_fd=fd, **options)
        self.hosts.append(host)
        return host

    def started(self, session_dir, **options):
        host = self.build(session_dir, **options)
        assert host.start() is None
        return host

    def cleanup(self):
        for host in self.hosts:
            if host.core is not None and not host._stop_begun:
                host.stop("listen_stop")
            host.shutdown()


@pytest.fixture
def hosts():
    h = Hosts()
    yield h
    h.cleanup()


def frames_received(host):
    return host.core.tracker.snapshot()["frames_received"]


def test_start_observe_events_stop_end_to_end(tmp_path, daemons):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(RATE * 3))))
    process, runtime = daemons.launch(session_dir)
    started = wait_for_runtime(session_dir, runtime, process, timeout_s=START_WAIT_TEST_S)
    assert started["outcome"] == "capturing", started
    assert lease_held(session_dir) and current_runtime(session_dir)["state"] == "running"
    descriptor = read_connection(runtime)
    observed = call(runtime, "observe")
    observation, cursor = observed["observation"], observed["cursor"]
    assert cursor["runtime_epoch"] == epoch_of(runtime) == observation["runtime"]["runtime_epoch"]
    assert observation["runtime"]["state"] == "running" and observation["capture"]["state"] == "running"
    assert observation["runtime"]["pid"] == process.pid == descriptor["pid"]
    assert observation["security"] == {"peer_uid_check": "enforced", "socket": "af_unix_only"}
    assert observation["journal"]["state"] == "writing" and observation["coverage"]["control_issued"] is False
    events, after = [], {"runtime_epoch": cursor["runtime_epoch"], "sequence": 0}
    while not any(e["evidence"]["type"] == "signal_level" for e in events):
        page = call(runtime, "events", {"after": after, "wait_ms": 500})
        assert page["events_status"] == "page" and page["missing"] == [], page
        events, after = events + page["events"], page["next_after"]
    types = [e["evidence"]["type"] for e in events]
    assert types[:3] == ["runtime_started", "capture_opened", "stream_started"]
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    begun = time.monotonic()
    reply = call(runtime, "stop", stop_args(runtime), timeout_s=STOP_REPLY_S)
    assert reply["terminal_sealed"] is True and time.monotonic() - begun < STOP_HANG_GUARD_S
    assert process.wait(timeout=10) == 0
    assert not lease_held(session_dir) and current_runtime(session_dir)["state"] == "not_running"
    assert not (runtime / "connection.json").exists() and not Path(descriptor["socket"]).parent.exists()
    handle, terminal = read_terminal(session_dir, runtime)
    assert reply["terminal_handle"] == handle and reply["cause"] == "listen_stop"
    assert (terminal["state"], terminal["cause"], terminal["stop_request_id"]) == ("stopped", "listen_stop", "stop-1")
    assert terminal["stop"]["adapter_close"] == "clean"
    assert terminal["stop"]["elapsed_ms"] <= terminal["stop"]["bound_ms"] == listen_host.STOP_BOUND_MS
    assert terminal["stop"]["bound_exceeded"] is False
    assert terminal["capture"]["streams"][0]["frames_received"] > 0
    assert terminal["routes"][0]["max_sample_peak_dbfs"] == pytest.approx(-12.0, abs=0.05)
    journal = verify_journal(runtime / "journal.ndjson")
    assert journal["chain_ok"] and journal["torn_tail_bytes"] == 0
    assert journal["head_sha256"] == terminal["journal"]["head_sha256"]
    assert journal["verified_lines"] == terminal["journal"]["lines"] and terminal["journal"]["dropped_lines"] == 0
    assert journal["last_sequence"] == terminal["broker"]["last_sequence"]
    journaled = entries(runtime)
    assert "signal_level" not in journal_types(runtime) and journal_types(runtime)[-1] == "runtime_stopped"
    assert {"op": "stop", "request_id": "stop-1", "expected_runtime_epoch": epoch_of(runtime)} in journaled
    assert [e["phase"] for e in journaled if "phase" in e] == ["launch", "capture_started", "stopping", "stopped"]
    assert not [p for p in Path(session_dir).rglob("*") if p.suffix.lower() in AUDIO_SUFFIXES]
    assert "Traceback" not in (runtime / "host.stderr").read_text()


def test_single_instance_per_session_dir_including_concurrent_starts(tmp_path, daemons):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(RATE * 3))))
    barrier, results = threading.Barrier(2), []

    def start():
        barrier.wait()
        results.append(daemons.launch(session_dir))

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    launched = [r for r in results if r[0] is not None]
    assert len(results) == 2 and len(launched) == 1 and len(daemons.processes) == 1
    process, runtime = launched[0]
    assert [row[2] for row in runtime_dirs(session_dir)] == [runtime]
    assert wait_for_runtime(session_dir, runtime, process, timeout_s=START_WAIT_TEST_S)["outcome"] == "capturing"
    assert acquire_lease(session_dir) is None
    assert call(runtime, "stop", stop_args(runtime), timeout_s=STOP_REPLY_S)["terminal_sealed"]
    assert process.wait(timeout=10) == 0
    fd = acquire_lease(session_dir)
    assert fd is not None
    os.close(fd)


def _raw(socket_path, payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(socket_path)
        connection.sendall(payload)
        return json.loads(connection.makefile("rb").readline())


def test_runtime_dir_0700_socket_0600_and_nonce_required(tmp_path, daemons):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(RATE * 3))))
    process, runtime = daemons.launch(session_dir)
    assert wait_for_runtime(session_dir, runtime, process, timeout_s=START_WAIT_TEST_S)["outcome"] == "capturing"
    descriptor = read_connection(runtime)
    sock = Path(descriptor["socket"])
    assert stat.S_IMODE(os.stat(runtime).st_mode) == 0o700 and stat.S_IMODE(os.stat(sock.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(runtime / "connection.json").st_mode) == 0o600
    assert stat.S_ISSOCK(os.lstat(sock).st_mode) and stat.S_IMODE(os.lstat(sock).st_mode) == 0o600
    assert len(os.fsencode(sock)) < 100 and sock.parent.name.startswith("pocket-listen-")
    for name in ("journal.ndjson", "launch-request.json", "host.stderr"):
        assert os.stat(runtime / name).st_mode & 0o077 == 0, name
    for nonce in ("0" * 48, None, descriptor["nonce"][:-1]):
        reply = _raw(str(sock), json.dumps({"op": "ping", "nonce": nonce, "args": {}}).encode() + b"\n")
        assert reply["status"] == "error" and "nonce" in reply["error"] and reply["code"] == "invalid_request"
        assert reply["hint"] == listen_host._NONCE_HINT
    assert _raw(str(sock), json.dumps({"op": "ping", "nonce": descriptor["nonce"]}).encode() + b"\n")["status"] == "ok"
    for path, mode in ((runtime, 0o755), (runtime / "connection.json", 0o644)):
        os.chmod(path, mode)
        with pytest.raises(PocketError) as caught:
            call(runtime, "ping")
        assert caught.value.code == "evidence_mismatch" and caught.value.hint == listen_host._PRIVATE_HINT
        os.chmod(path, 0o700 if path == runtime else 0o600)
    assert call(runtime, "ping")["runtime_state"] == "running"
    assert call(runtime, "stop", stop_args(runtime), timeout_s=STOP_REPLY_S)["terminal_sealed"]
    assert process.wait(timeout=10) == 0


def test_capture_refusal_is_a_receipt_not_an_error(tmp_path, daemons):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(44100), rate=44100)))
    process, runtime = daemons.launch(session_dir)
    outcome = wait_for_runtime(session_dir, runtime, process, timeout_s=START_WAIT_TEST_S)
    assert outcome["outcome"] == "capture_refused" and outcome["exit_code"] == listen_host.EXIT_REFUSED
    _, terminal = outcome["terminal"]
    assert (terminal["state"], terminal["cause"]) == ("refused", "startup_refused")
    assert terminal["capture"]["refusal"]["reasons"] == ["sample_rate_mismatch"]
    assert [row["uid"] for row in terminal["capture"]["refusal"]["inventory"]] == [UID]
    assert terminal["broker"]["published"] == 0 and terminal["capture"]["streams"] == []
    assert terminal["routes"][0]["max_sample_peak_dbfs"] is None
    assert outcome["startup_failure"] is None and read_startup_failure(runtime) is None
    assert not lease_held(session_dir) and not (runtime / "connection.json").exists()
    assert [e["phase"] for e in entries(runtime)] == ["launch", "refused"]
    assert verify_journal(runtime / "journal.ndjson")["chain_ok"]


def test_a_capture_that_ends_before_any_frame_is_not_reported_as_waiting_for_audio(tmp_path, daemons):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script({"kind": "device_lost"})))
    process, runtime = daemons.launch(session_dir)
    outcome = wait_for_runtime(session_dir, runtime, process, timeout_s=START_WAIT_TEST_S)
    assert outcome["outcome"] == "capture_ended_during_start"
    assert (outcome["ping"]["capture_state"], outcome["ping"]["capture_cause"]) == ("ended", "device_lost")
    assert outcome["ping"]["frames_received"] == 0 and lease_held(session_dir)
    assert call(runtime, "stop", stop_args(runtime), timeout_s=STOP_REPLY_S)["terminal_sealed"]
    assert process.wait(timeout=10) == 0


def test_a_runtime_stopped_while_start_waits_is_stopped_not_a_startup_failure(tmp_path, daemons):
    stall = {"kind": "stall", "ms": 30000, "counter": "pauses"}
    spec = script(stall, stall, stall, tone(RATE))
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(spec))
    process, runtime = daemons.launch(session_dir)

    def stop_when_reachable():
        wait_until(lambda: (runtime / "connection.json").exists(), SLOW_STARTUP_ALLOWANCE_S)
        call(runtime, "stop", stop_args(runtime), timeout_s=STOP_REPLY_S)

    stopper = threading.Thread(target=stop_when_reachable)
    stopper.start()
    outcome = wait_for_runtime(session_dir, runtime, process, timeout_s=SLOW_STARTUP_ALLOWANCE_S)
    stopper.join(30)
    assert outcome["outcome"] == "stopped_during_start" and outcome["exit_code"] == 0
    _, terminal = outcome["terminal"]
    assert (terminal["state"], terminal["cause"]) == ("stopped", "listen_stop")


class _FakePopen:
    def __init__(self, polls):
        self.polls = polls

    def poll(self):
        self.polls -= 1
        return 0 if self.polls < 0 else None


@pytest.mark.parametrize("exits, outcome, sealed", [(True, "stopped_during_start", True),
                                                    (False, "stopped_during_start", False)])
def test_a_start_that_sees_its_runtime_stopping_waits_for_the_stop_never_reports_capturing(
        tmp_path, monkeypatch, exits, outcome, sealed):
    runtime = tmp_path / f"0001-rt-{'0' * 16}"
    runtime.mkdir()
    (runtime / listen_host.CONNECTION_NAME).write_text("{}")
    ping = {"status": "ok", "runtime_state": "stopping", "capture_state": "running", "capture_cause": None,
            "frames_received": 480, "head_sequence": 9}
    terminal = ("handle", {"state": "stopped", "cause": "listen_stop"})
    monkeypatch.setattr(listen_host, "call", lambda *a, **k: ping)
    monkeypatch.setattr(listen_host, "read_terminal", lambda root, path: terminal if exits else None)
    process = _FakePopen(3 if exits else 10 ** 6)
    result = wait_for_runtime(tmp_path, runtime, process, timeout_s=0.5, poll_s=0.01)
    assert result["outcome"] == outcome and (result["terminal"] is not None) == sealed
    assert result["exit_code"] == (0 if exits else None)


def test_sigterm_stops_the_daemon_gracefully_with_a_sealed_terminal(tmp_path, daemons):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(RATE * 3))))
    process, runtime = daemons.launch(session_dir)
    assert wait_for_runtime(session_dir, runtime, process, timeout_s=START_WAIT_TEST_S)["outcome"] == "capturing"
    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=10) == 0
    _, terminal = read_terminal(session_dir, runtime)
    assert (terminal["state"], terminal["cause"], terminal["stop_request_id"]) == (
        "stopped", "signal_terminate", None)
    assert not lease_held(session_dir) and not (runtime / "connection.json").exists()


class _ForeignPeer:
    def __init__(self, uid):
        self.uid, self.sent, self.reads = uid, b"", 0

    def settimeout(self, value):
        pass

    def getsockopt(self, level, option, size=None):
        if sys.platform == "darwin":
            return struct.pack("=II", 0, self.uid) + bytes(68)
        return struct.pack("3i", 4242, self.uid, self.uid)

    def recv(self, size):
        self.reads += 1
        raise AssertionError("the request was read before the peer check")

    def sendall(self, data):
        self.sent += data


@pytest.mark.skipif(sys.platform != "darwin" and not hasattr(socket, "SO_PEERCRED"),
                    reason="this platform reports no peer credentials")
def test_peer_uid_mismatch_is_refused_before_the_nonce(tmp_path, hosts):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config())
    host = hosts.started(session_dir, threads=False, clock=FakeClock())
    peer = _ForeignPeer(os.getuid() + 1)
    host.handle_connection(peer)
    reply = json.loads(peer.sent)
    assert reply["status"] == "error" and "another user" in reply["error"] and peer.reads == 0
    left, right = socket.socketpair(socket.AF_UNIX)
    with left, right:
        assert peer_uid(left) == os.getuid()


_NETWORK_NAMES = {"AF_INET", "AF_INET6", "SOCK_DGRAM", "TCPServer", "UDPServer", "create_server",
                  "create_connection", "HTTPServer"}


def test_host_has_no_network_listener(tmp_path, hosts, monkeypatch):
    package = Path(listen_host.__file__).parent
    for name in ("listen_host.py", "listen.py", "listen_adapters.py", "listen_runtime.py", "listen_native.py"):
        source = (package / name).read_text()
        tree = ast.parse(source)
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        modules = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        modules |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert not names & _NETWORK_NAMES, name
        assert not {m for m in modules if m.split(".")[0] in ("http", "urllib", "xmlrpc", "asyncio", "ssl")}, name
        assert "http.server" not in source and "socketserver.TCPServer" not in source, name
    real = socket.socket

    class UnixOnly(real):
        def __init__(self, family=socket.AF_UNIX, type=socket.SOCK_STREAM, proto=0, fileno=None):
            if fileno is None and family != socket.AF_UNIX:
                raise AssertionError(f"Listen opened a non-AF_UNIX socket: {family}")
            super().__init__(family, type, proto, fileno)

    monkeypatch.setattr(socket, "socket", UnixOnly)
    session_dir, _ = seal_session(tmp_path / "session", resolved_config())
    host = hosts.started(session_dir)
    wait_until(lambda: frames_received(host) > 0)
    assert call(host.runtime_dir, "observe")["observation"]["security"]["socket"] == "af_unix_only"
    assert call(host.runtime_dir, "stop", stop_args(host.runtime_dir), timeout_s=STOP_REPLY_S)["terminal_sealed"]


def test_socket_ops_are_read_only_bounded_and_errors_keep_their_code_and_hint(tmp_path, hosts):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(RATE * 10))))
    host = hosts.started(session_dir)
    runtime = host.runtime_dir
    wait_until(lambda: host.core.broker.head >= 3)
    cursor = call(runtime, "observe")["cursor"]
    refusals = [
        (("act", {"kind": "play"}), "invalid_request", listen_host._CONTROL_HINT),
        (("observe", {"routes": ["master"]}), "invalid_request", listen_host._REQUEST_HINT),
        (("events", {"after": cursor, "max_events": 1000}), "invalid_request", listen._PAGE_HINT),
        (("events", {"after": cursor, "kinds": ["measurement"]}), "invalid_request", listen._KINDS_HINT),
        (("events", {"after": {**cursor, "sequence": 10 ** 6}}), "invalid_request",
         "Use the cursor listen_start or listen_observe returned; Listen never clamps a cursor."),
        (("stop", {"expected_runtime_epoch": "rt-" + "0" * 16, "request_id": "stop-x"}), "stale_revision",
         listen_host._STALE_EPOCH_HINT),
    ]
    for (op, args), code, hint in refusals:
        with pytest.raises(PocketError) as caught:
            call(runtime, op, args)
        assert (caught.value.code, caught.value.hint) == (code, hint), (op, args)
    assert call(runtime, "ping")["runtime_state"] == "running" and not host._stop_begun
    page = call(runtime, "events", {"after": {"runtime_epoch": cursor["runtime_epoch"], "sequence": 0},
                                    "kinds": ["health"], "max_events": 2})
    assert page["events_status"] == "page" and len(page["events"]) == 2 and page["truncated"] is True
    mismatch = call(runtime, "events", {"after": {"runtime_epoch": "rt-" + "1" * 16, "sequence": 0}})
    assert mismatch["events_status"] == "runtime_epoch_mismatch" and "events" not in mismatch
    for _ in range(listen_host.MAX_CONNECTIONS):
        assert host._slots.acquire(blocking=False)
    try:
        with pytest.raises(PocketError) as caught:
            call(runtime, "ping")
        assert "busy" in str(caught.value) and caught.value.hint == listen_host._BUSY_HINT
    finally:
        for _ in range(listen_host.MAX_CONNECTIONS):
            host._slots.release()
    oversized = _raw(read_connection(runtime)["socket"], b"x" * (listen_host.REQUEST_BYTES + 1))
    assert oversized["status"] == "error" and oversized["hint"] == listen_host._REQUEST_HINT
    real_observe, host.core.observe = host.core.observe, lambda: 1 / 0
    try:
        with pytest.raises(PocketError) as caught:
            call(runtime, "observe")
        assert "internal error" in str(caught.value) and caught.value.code == "invalid_request"
    finally:
        host.core.observe = real_observe
    assert call(runtime, "observe")["observation"]["runtime"]["state"] == "running"
    assert call(runtime, "stop", stop_args(runtime), timeout_s=STOP_REPLY_S)["terminal_sealed"]


def test_daemon_crash_leaves_the_previous_runtime_historical(tmp_path, daemons):
    session_dir, record = seal_session(tmp_path / "session", resolved_config(script(tone(RATE * 30))))
    first = daemons.foreground(session_dir, record["config_sha256"])
    line = json.loads(first.stdout.readline())
    assert line["status"] == "running", (line, first.stderr.read())
    old = Path(line["runtime_dir"])
    assert wait_for_runtime(session_dir, old, first, timeout_s=START_WAIT_TEST_S)["outcome"] == "capturing"
    wait_until(lambda: b'"type":"stream_started"' in (old / "journal.ndjson").read_bytes())
    daemons.remember_socket(old)
    first.kill()
    first.wait(timeout=10)
    assert not lease_held(session_dir) and read_terminal(session_dir, old) is None
    before = {p.name: p.read_bytes() for p in old.iterdir()}
    second = daemons.foreground(session_dir, record["config_sha256"])
    line = json.loads(second.stdout.readline())
    assert line["status"] == "running"
    new = Path(line["runtime_dir"])
    launch = read_launch(new)
    assert (launch["ordinal"], launch["launch"], launch["request_id"]) == (2, "foreground", None)
    previous = launch["previous_runtime"]
    assert (previous["runtime_epoch"], previous["terminal"]) == (epoch_of(old), "missing")
    assert previous["journal"]["chain_ok"] and previous["journal"]["verified_lines"] >= 4
    assert previous["journal"]["last_sequence"] >= 3 and previous["journal"]["torn_tail_bytes"] == 0
    assert {p.name: p.read_bytes() for p in old.iterdir()} == before
    second.send_signal(signal.SIGINT)
    assert second.wait(timeout=10) == 0


def test_foreground_host_shares_the_runtime_path_and_stops_on_sigint(tmp_path, daemons):
    session_dir, record = seal_session(tmp_path / "session", resolved_config(script(tone(RATE * 30))))
    process = daemons.foreground(session_dir, record["config_sha256"])
    line = json.loads(process.stdout.readline())
    runtime = Path(line["runtime_dir"])
    assert line == {"status": "running", "runtime_epoch": epoch_of(runtime), "runtime_dir": str(runtime)}
    assert wait_for_runtime(session_dir, runtime, process, timeout_s=START_WAIT_TEST_S)["outcome"] == "capturing"
    assert call(runtime, "observe")["observation"]["runtime"]["pid"] == process.pid
    busy = subprocess.run([sys.executable, "-m", "pocket_music.listen_host", "--session-dir", session_dir,
                           "--foreground", "--expected-config-sha256", record["config_sha256"]],
                          check=False, capture_output=True, text=True, timeout=60,
                          env={**os.environ, "PYTHONPATH": SRC})
    assert busy.returncode == listen_host.EXIT_CONFLICT
    assert json.loads(busy.stdout) == {"status": "conflict", "runtime_conflict": "already_running"}
    stale = subprocess.run([sys.executable, "-m", "pocket_music.listen_host", "--session-dir", session_dir,
                            "--foreground", "--expected-config-sha256", "0" * 64],
                           check=False, capture_output=True, text=True, timeout=60,
                           env={**os.environ, "PYTHONPATH": SRC})
    assert stale.returncode == listen_host.EXIT_SESSION and json.loads(stale.stdout)["code"] == "stale_revision"
    assert len(runtime_dirs(session_dir)) == 1
    process.send_signal(signal.SIGINT)
    assert process.wait(timeout=10) == 0
    final = json.loads(process.stdout.readline())
    assert final == {"status": "stopped", "exit_code": 0, "terminal_sealed": True, "cause": "signal_terminate"}
    _, terminal = read_terminal(session_dir, runtime)
    assert (terminal["state"], terminal["cause"]) == ("stopped", "signal_terminate")
    assert not lease_held(session_dir)


def test_max_runtime_bound_stops_with_a_terminal_cause(tmp_path, hosts):
    config = resolved_config(script(tone(RATE * 12)), max_runtime_seconds=10)
    session_dir, _ = seal_session(tmp_path / "session", config)
    clock = FakeClock()
    host = hosts.started(session_dir, threads=False, clock=clock)
    begun = clock.monotonic_ns()
    result = next(r for r in (host.step() for _ in range(5000)) if r is not None)
    assert result["terminal_sealed"] and result["cause"] == "max_runtime_reached"
    assert clock.monotonic_ns() - begun == pytest.approx(10e9, abs=0.05e9)
    _, terminal = read_terminal(session_dir, host.runtime_dir)
    assert (terminal["state"], terminal["cause"], terminal["stop_request_id"]) == (
        "stopped", "max_runtime_reached", None)
    assert terminal["capture"]["streams"][0]["frames_received"] <= 10 * RATE
    types = journal_types(host.runtime_dir)
    assert types.index("runtime_expiring") < types.index("runtime_stopping") < types.index("runtime_stopped")
    assert host.step() is result


class _FailingStream(io.FileIO):
    def __init__(self, path, good):
        super().__init__(path, "x")
        self.good = good

    def write(self, data):
        if self.good <= 0:
            raise OSError(errno.ENOSPC, "No space left on device")
        self.good -= 1
        return super().write(data)


def test_shutdown_gives_the_journal_its_last_close_before_the_lease_goes(tmp_path, hosts):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(RATE))))
    host = hosts.started(session_dir, threads=False, clock=FakeClock())
    for _ in range(5):
        host.step()
    assert host.stop("listen_stop", request_id="stop-1")["terminal_sealed"]
    held_at_close, close = [], host.journal.close

    def last_close(*args, **kwargs):
        held_at_close.append(lease_held(session_dir))
        return close(*args, **kwargs)
    host.journal.close = last_close
    host.shutdown()
    assert held_at_close == [True] and not lease_held(session_dir)


def test_journal_failure_and_terminal_seal_failure_degrade_honestly(tmp_path, hosts, monkeypatch):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(RATE * 10))))
    host = hosts.started(session_dir, threads=False, clock=FakeClock(),
                         journal_opener=lambda path: _FailingStream(path, 3))
    for _ in range(100):
        host.step()
    assert host.journal.state == "failed"
    observation = host.core.observe()["observation"]
    assert observation["journal"]["state"] == "failed" and observation["capture"]["state"] == "running"
    assert "journal_degraded" in [n["code"] for n in observation["notices"]]
    before = observation["capture"]["frames_received"]
    for _ in range(50):
        host.step()
    assert frames_received(host) == before + 50 * 480
    page = host.core.poll({"runtime_epoch": host.runtime_epoch, "sequence": 0}, kinds=["health"], max_events=256)
    failed = [e["evidence"] for e in page["events"] if e["evidence"]["type"] == "journal_failed"]
    assert len(failed) == 1 and failed[0]["state_transition"] == {"from": "writing", "to": "failed"}
    assert "journal_failed" not in journal_types(host.runtime_dir)

    def no_space(record, root):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(listen_host, "put_record", no_space)
    result = host.stop("listen_stop", request_id="stop-1")
    assert (result["terminal_sealed"], result["reason"], result["terminal_handle"]) == (
        False, "terminal_seal_failed", None)
    assert read_terminal(session_dir, host.runtime_dir) is None
    host._finished.set()
    assert host.run() == listen_host.EXIT_FAILED and host._error == "stop: terminal_seal_failed"
    monkeypatch.undo()
    host.shutdown()
    second = hosts.started(session_dir, threads=False, clock=FakeClock())
    for _ in range(20):
        second.step()
    assert second.stop("listen_stop", request_id="stop-2")["terminal_sealed"]
    handle, terminal = read_terminal(session_dir, second.runtime_dir)
    assert terminal["journal"]["state"] == "writing" and terminal["journal"]["closed"] is True
    record = Path(session_dir) / handle["artifact_uri"]
    record.write_bytes(record.read_bytes().replace(b'"stopped"', b'"stoppee"', 1))
    with pytest.raises(PocketError) as caught:
        read_terminal(session_dir, second.runtime_dir)
    assert caught.value.code == "evidence_mismatch" and caught.value.hint == listen_host._TERMINAL_HINT


class _BlockingStream(io.FileIO):
    def __init__(self, path):
        super().__init__(path, "x")
        self.blocked, self.release = threading.Event(), threading.Event()

    def write(self, data):
        self.blocked.set()
        self.release.wait(30)
        return super().write(data)


def test_slow_consumers_and_the_journal_never_block_capture(tmp_path, hosts):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(RATE * 10))))
    streams = []

    def opener(path):
        streams.append(_BlockingStream(path))
        return streams[-1]

    host = hosts.started(session_dir, journal_opener=opener, journal_queue_lines=2)
    try:
        assert streams[0].blocked.wait(5)
        start = wait_until(lambda: frames_received(host))
        wait_until(lambda: frames_received(host) >= start + RATE // 2)
        begun = time.monotonic()
        observed = call(host.runtime_dir, "observe")["observation"]
        assert time.monotonic() - begun < OBSERVE_HANG_GUARD_S and observed["capture"]["state"] == "running"
        assert observed["journal"]["dropped_lines"] >= 1 and observed["journal"]["lines"] == 0
        assert "journal_degraded" in [n["code"] for n in observed["notices"]]
        page = call(host.runtime_dir, "events", {"after": {"runtime_epoch": host.runtime_epoch, "sequence": 0}})
        assert page["events_status"] == "page" and page["events"][0]["evidence"]["type"] == "runtime_started"
    finally:
        streams[0].release.set()
    result = host.stop("listen_stop", request_id="stop-1")
    assert result["terminal_sealed"]
    _, terminal = read_terminal(session_dir, host.runtime_dir)
    assert terminal["journal"]["dropped_lines"] >= 1 and terminal["worker"]["state"] == "running"
    checked = verify_journal(host.runtime_dir / "journal.ndjson")
    assert checked["chain_ok"] and checked["dropped_lines_marked"] == terminal["journal"]["dropped_lines"]


class _StuckClose:
    def __init__(self, inner):
        self.inner, self.profile = inner, inner.profile
        self.release = threading.Event()

    def open(self):
        return self.inner.open()

    def start(self, channels):
        return self.inner.start(channels)

    def read(self, timeout_ns):
        return self.inner.read(timeout_ns)

    def stats(self):
        return self.inner.stats()

    def close(self, deadline_ns):
        self.release.wait(30)
        return self.inner.close(deadline_ns)


def test_stop_bound_holds_when_the_adapter_ignores_close(tmp_path, hosts):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(RATE * 10))))
    adapters = []

    def factory(session, clock):
        adapters.append(_StuckClose(listen_host._default_adapter(session, clock)))
        return adapters[-1]

    host = hosts.started(session_dir, adapter_factory=factory)
    wait_until(lambda: frames_received(host) > 0)
    begun = time.monotonic()
    try:
        reply = call(host.runtime_dir, "stop", stop_args(host.runtime_dir), timeout_s=STOP_HANG_GUARD_S)
    finally:
        adapters[0].release.set()
    _, terminal = read_terminal(session_dir, host.runtime_dir)
    assert reply["terminal_sealed"] and terminal["stop"]["adapter_close"] == "timeout"
    assert terminal["stop"]["elapsed_ms"] <= terminal["stop"]["bound_ms"] == listen_host.STOP_BOUND_MS
    assert terminal["stop"]["bound_exceeded"] is False
    assert time.monotonic() - begun < STOP_HANG_GUARD_S
    assert (terminal["state"], terminal["cause"]) == ("stopped", "listen_stop")


def test_an_adapter_close_that_raises_any_exception_reads_as_an_error_never_a_timeout(tmp_path, hosts):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(RATE * 3))))

    def factory(session, clock):
        adapter = listen_host._default_adapter(session, clock)
        close = adapter.close

        def exiting(deadline_ns):
            close(deadline_ns)
            raise SystemExit("adapter close exited")
        adapter.close = exiting
        return adapter

    host = hosts.started(session_dir, threads=False, clock=FakeClock(), adapter_factory=factory)
    host.step()
    assert host.stop("listen_stop", request_id="stop-1")["terminal_sealed"]
    _, terminal = read_terminal(session_dir, host.runtime_dir)
    assert (terminal["state"], terminal["stop"]["adapter_close"]) == ("stopped", "error")


def test_a_stuck_close_and_a_lagging_worker_together_still_end_the_ladder_within_the_bound(tmp_path, hosts):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(RATE * 10))))
    adapters, release = [], threading.Event()

    def factory(session, clock):
        adapters.append(_StuckClose(listen_host._default_adapter(session, clock)))
        return adapters[-1]

    host = hosts.started(session_dir, adapter_factory=factory)
    wait_until(lambda: frames_received(host) > 0)
    measure, inside = host.core.worker._measure, threading.Event()

    def slow(d):
        inside.set()
        release.wait(30)
        measure(d)

    host.core.worker._measure = slow
    try:
        assert inside.wait(10)
        reply = host.stop("listen_stop", request_id="stop-1")
    finally:
        adapters[0].release.set()
        release.set()
    _, terminal = read_terminal(session_dir, host.runtime_dir)
    assert reply["terminal_sealed"] and terminal["worker"]["drain"] == "incomplete"
    assert terminal["stop"]["elapsed_ms"] <= terminal["stop"]["bound_ms"]
    assert terminal["stop"]["bound_exceeded"] is False


class _BreaksAfter:
    def __init__(self, inner, blocks):
        self.inner, self.profile, self.blocks, self.count = inner, inner.profile, blocks, 0

    def open(self):
        return self.inner.open()

    def start(self, channels):
        return self.inner.start(channels)

    def read(self, timeout_ns):
        item = self.inner.read(timeout_ns)
        self.count += isinstance(item, AdapterBlock)
        if self.count > self.blocks:
            raise RuntimeError("adapter read broke")
        return item

    def stats(self):
        return self.inner.stats()

    def close(self, deadline_ns):
        return self.inner.close(deadline_ns)


def test_a_host_thread_failure_ends_the_runtime_with_a_failed_terminal(tmp_path, hosts):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(RATE * 10))))

    def factory(session, clock):
        return _BreaksAfter(listen_host._default_adapter(session, clock), 20)

    host = hosts.started(session_dir, adapter_factory=factory)
    codes = []
    runner = threading.Thread(target=lambda: codes.append(host.run()))
    runner.start()
    runner.join(10)
    assert codes == [listen_host.EXIT_FAILED]
    _, terminal = read_terminal(session_dir, host.runtime_dir)
    assert (terminal["state"], terminal["cause"]) == ("failed", "fatal_error")
    assert terminal["error"].startswith("capture: adapter read broke")
    assert terminal["capture"]["streams"][0]["frames_received"] == 20 * 480
    assert not lease_held(session_dir) and not (host.runtime_dir / "connection.json").exists()
    assert {"phase": "stopping", "cause": "fatal_error", "request_id": None} in entries(host.runtime_dir)


def test_daemon_startup_failure_is_reported_as_host_failed_with_its_record(tmp_path, daemons):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config())
    fd = acquire_lease(session_dir)
    try:
        runtime, _ = prepare_launch(session_dir, request_id="start-1", launch="provider")
        pointer = json.loads((Path(session_dir) / "listen-session.json").read_text())
        record = Path(session_dir) / pointer["session"]["artifact_uri"]
        record.write_bytes(record.read_bytes().replace(b'"master"', b'"mastex"', 1))
        process = spawn_host(session_dir, runtime, fd)
    finally:
        os.close(fd)
    daemons.processes.append(process)
    outcome = wait_for_runtime(session_dir, runtime, process, timeout_s=START_WAIT_TEST_S)
    assert outcome["outcome"] == "host_failed" and outcome["exit_code"] == listen_host.EXIT_SESSION
    assert outcome["terminal"] is None and outcome["startup_failure"]["stage"] == "session_verify"
    assert "Can't verify the Listen session" in outcome["stderr_tail"]
    assert not lease_held(session_dir)


def test_daemon_stderr_is_capped_and_counts_what_it_leaves_out():
    sink = io.StringIO()
    capped = listen_host._CappedStream(sink, 100)
    for text in ("a" * 60, "b" * 60, "c"):
        assert capped.write(text) == len(text)
    assert sink.getvalue() == "a" * 60 + "b" * 40 and capped.omitted == 21


def test_route_and_start_refusals_seal_a_refused_terminal_in_process(tmp_path, hosts):
    unexposed = ({"route_id": "master", "role": "master", "channels": [2, 3]},)
    session_dir, _ = seal_session(tmp_path / "routes", resolved_config(routes=unexposed))
    host = hosts.build(session_dir, threads=False, clock=FakeClock())
    assert host.start() == listen_host.EXIT_REFUSED
    _, terminal = read_terminal(session_dir, host.runtime_dir)
    assert terminal["capture"]["refusal"]["reasons"] == ["master_channels_not_exposed"]
    assert terminal["routes"][0]["availability"] == "unavailable" and not lease_held(session_dir)
    assert host.socket_dir is not None and not host.socket_dir.exists()

    def refusing(session, clock):
        adapter = listen_host._default_adapter(session, clock)
        adapter.start = lambda channels: StartResult(False, ("helper_start_timeout",), tuple(channels))
        return adapter

    session_dir, _ = seal_session(tmp_path / "start", resolved_config())
    host = hosts.build(session_dir, threads=False, clock=FakeClock(), adapter_factory=refusing)
    assert host.start() == listen_host.EXIT_REFUSED and host.core is None
    _, terminal = read_terminal(session_dir, host.runtime_dir)
    assert terminal["capture"]["refusal"]["reasons"] == ["helper_start_timeout"]
    assert terminal["capture"]["device"]["uid"] == UID and terminal["stop"]["adapter_close"] == "clean"
    (master,) = terminal["routes"]
    assert (master["availability"], master["reason"], master["availability_at_start"]) == (
        "unavailable", "capture_refused", "available")
    assert master["metering"] == "metered" and master["frames_measured"] is None
    routes = (*unexposed, {"route_id": "deck", "role": "unlabeled", "channels": [0, 1]})
    session_dir, _ = seal_session(tmp_path / "mixed", resolved_config(routes=routes))
    host = hosts.build(session_dir, threads=False, clock=FakeClock())
    assert host.start() == listen_host.EXIT_REFUSED
    _, terminal = read_terminal(session_dir, host.runtime_dir)
    assert [(r["availability"], r["reason"], r["availability_at_start"]) for r in terminal["routes"]] == [
        ("unavailable", "channels_not_exposed", "unavailable"),
        ("unavailable", "capture_refused", "available")]
    assert listen._newest_record(Path(session_dir))["routes"][1]["availability"] == "unavailable"


def test_a_startup_failure_after_capture_began_seals_a_failed_terminal_with_what_it_captured(tmp_path, hosts,
                                                                                            monkeypatch):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(RATE * 5))))
    host = hosts.build(session_dir)
    real_write = listen_host._write_private_json

    def full_disk(path, value):
        if Path(path).name == listen_host.CONNECTION_NAME:
            wait_until(lambda: host.core.tracker.snapshot()["frames_received"] > 0, 30)
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(path, value)
    monkeypatch.setattr(listen_host, "_write_private_json", full_disk)
    assert host.start() == listen_host.EXIT_FAILED
    assert not lease_held(session_dir) and not (host.runtime_dir / "connection.json").exists()
    assert read_startup_failure(host.runtime_dir) is None
    handle, terminal = read_terminal(session_dir, host.runtime_dir)
    assert terminal["state"] == "failed" and terminal["error"].startswith("startup: ")
    assert terminal["cause"] == "fatal_error" and terminal["capture"]["streams"][0]["frames_received"] > 0
    receipt = listen._failed_receipt(Path(session_dir), listen_host.load_session(session_dir)[2], "start-1",
                                     host.runtime_dir, {"runtime_epoch": host.runtime_epoch},
                                     {"startup_failure": None, "startup_failure_record": "missing",
                                      "terminal": (handle, terminal), "exit_code": 1, "stderr_tail": None},
                                     {"start_outcome": "host_failed", "replayed": False})
    assert receipt["coverage"]["capture"] != "not_started" and "started, then failed" in receipt["next_actions"][0]


def test_a_refusal_whose_terminal_cant_be_sealed_records_why_and_exits_as_a_failure(tmp_path, hosts,
                                                                                    monkeypatch):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(44100), rate=44100)))
    host = hosts.build(session_dir, threads=False, clock=FakeClock())

    def no_space(record, root):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(listen_host, "put_record", no_space)
    assert host.start() == listen_host.EXIT_FAILED
    assert read_terminal(session_dir, host.runtime_dir) is None and not lease_held(session_dir)
    record = read_startup_failure(host.runtime_dir)
    assert record["stage"] == "refused" and "sample_rate_mismatch" in record["error"]
    assert record["refusal"] == {"reasons": ["sample_rate_mismatch"], "refused_at": "open"}
    monkeypatch.undo()
    receipt = listen._failed_receipt(Path(session_dir), listen_host.load_session(session_dir)[2], "start-1",
                                     host.runtime_dir, {"runtime_epoch": host.runtime_epoch},
                                     {"startup_failure": record, "startup_failure_record": "sealed",
                                      "terminal": None, "exit_code": 1, "stderr_tail": None},
                                     {"start_outcome": "host_failed", "replayed": False})
    assert receipt["startup_failure"]["refusal"]["reasons"] == ["sample_rate_mismatch"]
    assert receipt["coverage"]["capture"] == "refused" and "refusal.reasons" in receipt["next_actions"][0]


def test_an_open_refusal_that_raises_while_sealing_is_a_startup_failure_never_an_escaped_error(
        tmp_path, hosts, monkeypatch):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(44100), rate=44100)))
    host = hosts.build(session_dir, threads=False, clock=FakeClock())

    def broken(record, root):
        raise RuntimeError("the terminal store broke")
    monkeypatch.setattr(listen_host, "put_record", broken)
    assert host.start() == listen_host.EXIT_FAILED
    monkeypatch.undo()
    record = read_startup_failure(host.runtime_dir)
    assert record["stage"] == "internal" and "the terminal store broke" in record["error"]
    assert read_terminal(session_dir, host.runtime_dir) is None
    assert not lease_held(session_dir) and not host.socket_dir.exists()


def test_an_adapter_open_that_raises_after_opening_is_closed_and_fails_startup_at_adapter_open(
        tmp_path, hosts):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(RATE))))
    closes = []

    def factory(session, clock):
        adapter = listen_host._default_adapter(session, clock)
        opened, close = adapter.open, adapter.close

        def broken():
            opened()
            raise RuntimeError("the open broke")

        def recording(deadline_ns):
            closes.append(deadline_ns)
            return close(deadline_ns)
        adapter.open, adapter.close = broken, recording
        return adapter

    host = hosts.build(session_dir, threads=False, clock=FakeClock(), adapter_factory=factory)
    assert host.start() == listen_host.EXIT_FAILED
    record = read_startup_failure(host.runtime_dir)
    assert record["stage"] == "adapter_open" and "the open broke" in record["error"]
    assert len(closes) == 1 and not lease_held(session_dir) and not host.socket_dir.exists()


@pytest.mark.parametrize("name", ["journal", "capture", "worker", "watchdog", "server"])
def test_a_host_thread_that_cant_start_ends_the_runtime_with_its_record_and_frees_the_lease(tmp_path, hosts,
                                                                                         monkeypatch, name):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(RATE * 5))))
    host = hosts.build(session_dir)
    real_start = threading.Thread.start

    def start(thread):
        if thread.name == f"listen-{name}":
            raise RuntimeError("can't start new thread")
        return real_start(thread)
    monkeypatch.setattr(threading.Thread, "start", start)
    returned = {}
    runner = threading.Thread(target=lambda: returned.update(code=host.start()), daemon=True)
    real_start(runner)
    runner.join(60)
    monkeypatch.undo()
    assert not runner.is_alive(), "start() never returned"
    assert returned["code"] == listen_host.EXIT_FAILED
    assert not lease_held(session_dir) and not host.socket_dir.exists()
    if name == "journal":
        record = read_startup_failure(host.runtime_dir)
        assert record["stage"] == "internal" and "can't start new thread" in record["error"]
        assert read_terminal(session_dir, host.runtime_dir) is None
    else:
        _, terminal = read_terminal(session_dir, host.runtime_dir)
        assert terminal["state"] == "failed" and terminal["cause"] == "fatal_error"
        assert "can't start new thread" in terminal["error"]
        assert read_startup_failure(host.runtime_dir) is None


@pytest.mark.parametrize("ending", ["refused", "startup_failed"])
def test_a_refusal_or_startup_failure_gives_the_journal_its_last_close_before_the_lease_goes(tmp_path, hosts,
                                                                                           monkeypatch, ending):
    rate = 44100 if ending == "refused" else RATE
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(rate), rate=rate)))
    streams, options = [], {}

    def opener(path):
        streams.append(_BlockingStream(path))
        return streams[-1]

    if ending == "startup_failed":
        def failing_adapter(session, clock):
            assert streams[0].blocked.wait(30)
            raise OSError(errno.EIO, "Input/output error")
        options["adapter_factory"] = failing_adapter
    host = hosts.build(session_dir, journal_opener=opener, **options)
    host.journal_close_s, host.shutdown_wait_s = 0.2, 30.0
    closes, real_close = [], RuntimeJournal.close

    def close(journal, thread=None, timeout_s=0.5):
        result = real_close(journal, thread, timeout_s)
        closes.append((lease_held(session_dir), result["closed"]))
        streams[0].release.set()
        return result
    monkeypatch.setattr(RuntimeJournal, "close", close)
    expected = listen_host.EXIT_REFUSED if ending == "refused" else listen_host.EXIT_FAILED
    assert host.start() == expected
    assert closes[0] == (True, False) and closes[-1] == (True, True)
    assert not lease_held(session_dir)


def test_an_input_whose_blocks_arrive_further_apart_than_stale_after_allows_is_refused(tmp_path, hosts):
    config = resolved_config({**script(tone(RATE * 3)), "block_frames": 8192})
    session_dir, _ = seal_session(tmp_path / "session", {**config, "health": {"stale_after_ms": 300}})
    host = hosts.build(session_dir, threads=False, clock=FakeClock())
    assert host.start() == listen_host.EXIT_REFUSED and host.core is None
    _, terminal = read_terminal(session_dir, host.runtime_dir)
    refusal = terminal["capture"]["refusal"]
    assert refusal["reasons"] == ["stale_after_below_block_period"] and refusal["refused_at"] == "open"
    assert refusal["block_period"] == {"buffer_frame_size": 8192, "sample_rate": RATE, "stale_after_ms": 300}
    assert all(r["availability"] == "unavailable" for r in terminal["routes"])
    session_dir, _ = seal_session(tmp_path / "fits", {**config, "health": {"stale_after_ms": 342}})
    assert hosts.build(session_dir, threads=False, clock=FakeClock()).start() is None


def test_a_changed_session_launch_or_lease_fails_startup_with_a_sealed_record(tmp_path, hosts):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config())
    host = hosts.build(session_dir, threads=False, clock=FakeClock())
    pointer = json.loads((Path(session_dir) / "listen-session.json").read_text())
    record = Path(session_dir) / pointer["session"]["artifact_uri"]
    original = record.read_bytes()
    record.write_bytes(original.replace(b'"master"', b'"mastex"', 1))
    assert host.start() == listen_host.EXIT_SESSION
    failure = read_startup_failure(host.runtime_dir)
    assert failure["stage"] == "session_verify" and failure["schema"] == "pocket.listen-startup-failure/v1"
    assert not lease_held(session_dir) and not (host.runtime_dir / "journal.ndjson").exists()
    record.write_bytes(original)
    stale = hosts.build(session_dir, threads=False, clock=FakeClock())
    stale.shutdown()
    newer = hosts.build(session_dir, threads=False, clock=FakeClock())
    fd = os.open(tmp_path / "not-the-lease", os.O_CREAT | os.O_RDWR, 0o600)
    for runtime, lease in ((stale.runtime_dir, newer.lease_fd), (newer.runtime_dir, fd)):
        host = ListenHost(session_dir, runtime, lease_fd=os.dup(lease), threads=False, clock=FakeClock())
        assert host.start() == listen_host.EXIT_SESSION
        assert read_startup_failure(runtime)["stage"] == "session_verify"
    os.close(fd)


def test_runtime_directories_are_new_per_launch_and_never_reused(tmp_path):
    session_dir, record = seal_session(tmp_path / "session", resolved_config())
    fd = acquire_lease(session_dir)
    try:
        first, launch = prepare_launch(session_dir, request_id="start-1", launch="provider")
        assert launch["previous_runtime"] is None and first.name == f"0001-{launch['runtime_epoch']}"
        second, launch = prepare_launch(session_dir, request_id="start-2", launch="provider")
        assert second.name.startswith("0002-rt-") and stat.S_IMODE(os.stat(second).st_mode) == 0o700
        assert launch["previous_runtime"] == {
            "runtime_epoch": epoch_of(first), "terminal": "missing",
            "journal": {"present": False, "verified_lines": 0, "last_sequence": 0, "chain_ok": False,
                        "torn_tail_bytes": 0, "dropped_lines_marked": 0, "stopped_writing": None}}
        assert (launch["session_id"], launch["config_sha256"]) == (record["session_id"], record["config_sha256"])
        assert read_launch(second) == launch
        (Path(session_dir) / "runtimes" / f"9999-rt-{'0' * 16}").mkdir()
        with pytest.raises(PocketError) as caught:
            prepare_launch(session_dir, request_id="start-3", launch="provider")
        assert caught.value.hint == "Prepare a new session directory."
        (second / "launch-request.json").write_text("{}")
        with pytest.raises(PocketError) as caught:
            read_launch(second)
        assert caught.value.code == "evidence_mismatch"
    finally:
        os.close(fd)


def test_lease_probe_is_shared_and_never_takes_the_lease(tmp_path):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config())
    assert not lease_held(session_dir) and current_runtime(session_dir)["state"] == "never_started"
    fd = acquire_lease(session_dir)
    try:
        assert lease_held(session_dir) and lease_held(session_dir)
        assert acquire_lease(session_dir, retry_s=0.05) is None
        assert current_runtime(session_dir)["state"] == "unreachable"
    finally:
        os.close(fd)
    assert not lease_held(session_dir)


def test_verify_journal_checks_the_chain_and_reports_a_torn_tail(tmp_path):
    path = tmp_path / "journal.ndjson"
    journal = RuntimeJournal(path, FakeClock())
    for n in range(4):
        assert journal.offer({"phase": f"p{n}"})
    health = {"delivery_class": "health", "sequence": 7, "evidence": {"type": "capture_stale"}}
    journal.offer_event(health)
    journal.offer_event({"delivery_class": "normal", "sequence": 8, "evidence": {"type": "signal_level"}})
    journal.offer_event({"delivery_class": "normal", "sequence": 9, "evidence": {"type": "near_silence"}})
    journal.offer_event({"delivery_class": "health", "sequence": 10, "evidence": {"type": "journal_failed"}})
    closed = journal.close()
    assert (closed["lines"], closed["dropped_lines"], closed["state"], closed["closed"]) == (6, 0, "writing", True)
    assert journal.offer({"phase": "late"}) is False and journal.status()["dropped_lines"] == 1
    result = verify_journal(path)
    assert result == {"present": True, "verified_lines": 6, "last_sequence": 9, "chain_ok": True,
                      "torn_tail_bytes": 0, "head_sha256": closed["head_sha256"], "dropped_lines_marked": 0,
                      "stopped_writing": None}
    lines = path.read_bytes().splitlines()
    assert json.loads(lines[0])["prev_sha256"] == "0" * 64
    assert json.loads(lines[1])["prev_sha256"] == hashlib.sha256(lines[0]).hexdigest()
    assert closed["head_sha256"] == hashlib.sha256(lines[-1]).hexdigest()
    torn, fragment = tmp_path / "torn.ndjson", b'{"schema":"pocket.listen-jour'
    torn.write_bytes(path.read_bytes() + fragment)
    assert verify_journal(torn)["torn_tail_bytes"] == len(fragment) and verify_journal(torn)["chain_ok"]
    tampered = tmp_path / "tampered.ndjson"
    tampered.write_bytes(b"\n".join([*lines[:2], lines[2].replace(b"p2", b"pX"), *lines[3:]]) + b"\n")
    assert (verify_journal(tampered)["chain_ok"], verify_journal(tampered)["verified_lines"]) == (False, 3)
    assert verify_journal(tmp_path / "missing.ndjson")["present"] is False


def test_a_line_offered_while_the_journal_closes_is_written_or_counted_never_lost(tmp_path):
    journal = RuntimeJournal(tmp_path / "journal.ndjson", FakeClock())
    real_put, closer = journal._queue.put_nowait, []

    def put_while_closing(item):
        thread = threading.Thread(target=lambda: closer.append(journal.close()))
        thread.start()
        thread.join(0.2)
        real_put(item)
        closer.append(thread)

    journal._queue.put_nowait = put_while_closing
    journal.offer({"phase": "racing"})
    closer[-1].join(5)
    status = journal.status()
    assert status["lines"] + status["dropped_lines"] == 1
    assert verify_journal(tmp_path / "journal.ndjson")["verified_lines"] == status["lines"]


def test_lines_a_full_journal_queue_dropped_are_marked_in_the_chain(tmp_path):
    path = tmp_path / "journal.ndjson"
    journal = RuntimeJournal(path, FakeClock(), queue_lines=2)
    assert journal.offer({"phase": "a"}) and journal.offer({"phase": "b"})
    assert [journal.offer({"phase": p}) for p in "cde"] == [False, False, False]
    journal.write_pending()
    assert journal.offer({"phase": "f"})
    journal.write_pending()
    assert journal.offer({"phase": "g"}) and journal.offer({"phase": "h"})
    assert not journal.offer({"phase": "i"})
    closed = journal.close()
    entries = [json.loads(line)["entry"] for line in path.read_bytes().splitlines()]
    assert entries == [{"phase": "a"}, {"phase": "b"}, {"journal": "lines_dropped", "dropped_lines": 3},
                       {"phase": "f"}, {"phase": "g"}, {"phase": "h"},
                       {"journal": "lines_dropped", "dropped_lines": 1}]
    result = verify_journal(path)
    assert result["chain_ok"] and result["dropped_lines_marked"] == closed["dropped_lines"] == 4


def test_journal_budget_exhaustion_is_reported_once_counted_and_marked_in_the_chain(tmp_path):
    degraded, path = [], tmp_path / "journal.ndjson"
    journal = RuntimeJournal(path, FakeClock(), budget_bytes=1400,
                             on_degraded=lambda state, error: degraded.append(state))
    for n in range(10):
        journal.offer({"phase": "x" * 100, "n": n})
    status = journal.close()
    assert degraded == ["exhausted"] and status["state"] == "exhausted"
    assert status["lines"] + status["dropped_lines"] == 10 + 1 and status["bytes"] <= 1400
    entries = [json.loads(line)["entry"] for line in path.read_bytes().splitlines()]
    assert entries[-1] == {"journal": "stopped_writing", "state": "exhausted"}
    result = verify_journal(path)
    assert result["chain_ok"] and result["verified_lines"] == status["lines"]
    assert result["stopped_writing"] == "exhausted"


def test_a_failed_fsync_after_every_line_was_written_drops_no_line(tmp_path, monkeypatch):
    path = tmp_path / "journal.ndjson"
    journal = RuntimeJournal(path, FakeClock())
    assert journal.offer({"phase": "a"}) and journal.offer({"phase": "b"})
    journal.write_pending()

    def fail(fd):
        raise OSError(errno.EIO, "injected fsync error")
    with monkeypatch.context() as patch:
        patch.setattr(listen_host.os, "fsync", fail)
        status = journal.close()
    assert status["state"] == "failed" and status["dropped_lines"] == 0
    entries = [json.loads(line)["entry"] for line in path.read_bytes().splitlines()]
    assert entries == [{"phase": "a"}, {"phase": "b"}, {"journal": "stopped_writing", "state": "failed"}]


def test_a_drop_marker_that_reaches_the_budget_isnt_counted_as_a_dropped_line(tmp_path):
    def feed(journal):
        assert journal.offer({"phase": "a"})
        journal.write_pending()
        assert journal.offer({"phase": "b"}) and not journal.offer({"phase": "c"})
        journal.write_pending()
        assert journal.offer({"phase": "d"})

    probe = RuntimeJournal(tmp_path / "probe.ndjson", FakeClock(), queue_lines=1)
    feed(probe)
    used = probe.status()["bytes"]
    marker = len(probe._encode(probe._clock.monotonic_ns(), listen_host._dropped_marker(1))) + 1
    budget = listen_host.JOURNAL_MARKER_RESERVE_BYTES + used + marker - 1
    journal = RuntimeJournal(tmp_path / "journal.ndjson", FakeClock(), queue_lines=1, budget_bytes=budget)
    feed(journal)
    journal.write_pending()
    status = journal.close()
    assert status["state"] == "exhausted" and status["dropped_lines"] == 2
    entries = [json.loads(line)["entry"] for line in (tmp_path / "journal.ndjson").read_bytes().splitlines()]
    assert entries == [{"phase": "a"}, {"phase": "b"}, {"journal": "stopped_writing", "state": "exhausted"}]


class _BreakingStream:
    def __init__(self, path, fail_at, *, partial=False):
        self.inner, self.fail_at, self.partial, self.calls = path.open("wb", buffering=0), fail_at, partial, 0

    def write(self, data):
        self.calls += 1
        if self.calls == self.fail_at and self.partial:
            return self.inner.write(bytes(data[:10]))
        if self.calls == self.fail_at + (1 if self.partial else 0):
            raise OSError("injected write error")
        return self.inner.write(data)

    def fileno(self):
        return self.inner.fileno()

    def close(self):
        self.inner.close()


@pytest.mark.parametrize("partial, marked", [(False, "failed"), (True, None)])
def test_a_journal_write_error_is_marked_in_the_chain_unless_it_left_a_partial_line(tmp_path, partial,
                                                                                   marked):
    path = tmp_path / "journal.ndjson"
    journal = RuntimeJournal(path, FakeClock(), opener=lambda p: _BreakingStream(p, 4, partial=partial))
    for n in range(10):
        journal.offer({"phase": "p", "n": n})
    status = journal.close()
    assert status["state"] == "failed" and status["lines"] == 3 + (1 if marked else 0)
    result = verify_journal(path)
    assert result["stopped_writing"] == marked and result["verified_lines"] == status["lines"]
    assert result["torn_tail_bytes"] == (10 if partial else 0)


def test_a_journal_close_that_outwaits_a_stuck_writer_counts_the_lines_it_left(tmp_path):
    path, stuck, written = tmp_path / "journal.ndjson", threading.Event(), []

    class Stuck:
        def __init__(self, p):
            self.inner = p.open("wb", buffering=0)

        def write(self, data):
            written.append(len(data))
            if len(written) == 3:
                stuck.wait(10)
            return self.inner.write(data)

        def fileno(self):
            return self.inner.fileno()

        def close(self):
            self.inner.close()

    journal = RuntimeJournal(path, FakeClock(), opener=Stuck)
    writer = journal.start()
    for n in range(10):
        assert journal.offer({"phase": "p", "n": n})
    wait_until(lambda: len(written) == 3)
    first = journal.close(writer, 0.2)
    assert first["closed"] is False and first["lines"] == 2
    assert first["lines"] + first["dropped_lines"] + first["lines_unwritten_at_close"] == 10
    stuck.set()
    final = journal.close(writer, 5)
    assert final["closed"] is True and final["lines_unwritten_at_close"] == 0
    assert verify_journal(path)["verified_lines"] == 10


def _coreaudio_session(tmp_path, *fake_args):
    build_id, _ = write_fake_build(*fake_args)
    sealed = listen_native.verify_build(build_id)
    config = resolved_config(adapter={"kind": "coreaudio", "build_id": build_id}, uid="fake:duo")
    return seal_session(tmp_path / "session", config,
                        build={k: sealed[k] for k in ("build_id", "build_json_sha256", "helper_sha256")})


def _patient(session, clock):
    adapter = listen_host._default_adapter(session, clock)
    adapter.hello_timeout_s = adapter.started_timeout_s = LOADED_FAKE_START_ALLOWANCE_S
    return adapter


@pytest.mark.skipif(sys.platform != "darwin", reason="the CoreAudio adapter runs on macOS only")
def test_the_daemon_runs_a_private_helper_copy_never_the_build_or_session_and_never_with_the_lease(
        tmp_path, hosts, monkeypatch):
    session_dir, _ = _coreaudio_session(tmp_path)
    build_id = listen_host.load_session(session_dir)[2]["build"]["build_id"]
    host = hosts.build(session_dir, adapter_factory=_patient)
    spawned, real = [], subprocess.Popen

    def recording(argv, **kwargs):
        folder = Path(argv[0]).parent
        modes = [stat.S_IMODE(os.lstat(path).st_mode) for path in (folder, argv[0])]
        spawned.append((argv, kwargs, os.get_inheritable(host.lease_fd), modes))
        return real(argv, **kwargs)

    monkeypatch.setattr(listen_adapters.subprocess, "Popen", recording)
    assert host.start() is None
    monkeypatch.undo()
    (argv, kwargs, inheritable, modes), = spawned
    helper = Path(argv[0])
    assert helper.name == "listen-capture" and helper.parent.parent == Path("/private/tmp")
    assert helper.parent.name.startswith("pocket-listen-helper-") and modes == [0o700, 0o700]
    assert listen_native.file_sha256(helper) == listen_native.verify_build(build_id)["helper_sha256"]
    assert argv[1] == "capture" and kwargs["cwd"] == helper.parent
    assert kwargs["close_fds"] is True and "pass_fds" not in kwargs and inheritable is False
    for folder in (Path(session_dir).resolve(), listen_native.registry_dir()):
        assert not helper.is_relative_to(folder)
    assert list(Path(session_dir).rglob("listen-capture")) == []
    assert host.stop("listen_stop", request_id="stop-1")["terminal_sealed"]
    assert not helper.parent.exists()


def test_a_helper_copy_that_cant_be_made_safely_fails_startup_at_adapter_open(tmp_path, hosts, monkeypatch):
    session_dir, _ = _coreaudio_session(tmp_path)
    unsafe = tmp_path / "unsafe-tmp"
    unsafe.mkdir()
    monkeypatch.setattr(listen_native, "_copy_parent", lambda: unsafe)
    spawned = []
    monkeypatch.setattr(listen_adapters.subprocess, "Popen", lambda argv, **kwargs: spawned.append(argv))
    host = hosts.build(session_dir, adapter_factory=_patient)
    assert host.start() == listen_host.EXIT_FAILED
    assert spawned == [] and list(unsafe.iterdir()) == [] and not lease_held(session_dir)
    record = read_startup_failure(host.runtime_dir)
    assert record["stage"] == "adapter_open" and "sticky bit" in record["error"]
    assert read_terminal(session_dir, host.runtime_dir) is None


@pytest.mark.skipif(sys.platform != "darwin", reason="the CoreAudio adapter runs on macOS only")
@pytest.mark.parametrize("fault", ["hello_check", "reader_start"])
def test_an_adapter_open_that_raises_after_the_helper_started_leaves_no_helper_behind(tmp_path, hosts,
                                                                                     monkeypatch, fault):
    session_dir, _ = _coreaudio_session(tmp_path)
    host = hosts.build(session_dir, adapter_factory=_patient)
    host.close_budget_ns = LOADED_FAKE_CLOSE_ALLOWANCE_NS
    host.close_join_s = LOADED_FAKE_CLOSE_JOIN_ALLOWANCE_S
    children, real_popen, real_start = [], subprocess.Popen, threading.Thread.start

    def recording(argv, **kwargs):
        children.append(real_popen(argv, **kwargs))
        return children[-1]

    def broken(hello, uid, rate, pid):
        raise RuntimeError("the open broke")

    def start(thread):
        if thread.name.endswith("(_read_stdout)"):
            raise RuntimeError("the open broke")
        return real_start(thread)

    faults = {"hello_check": (listen_adapters, "validate_hello", broken),
              "reader_start": (threading.Thread, "start", start)}
    monkeypatch.setattr(listen_adapters.subprocess, "Popen", recording)
    monkeypatch.setattr(*faults[fault])
    assert host.start() == listen_host.EXIT_FAILED
    monkeypatch.undo()
    (child,) = children
    assert child.poll() is not None and not Path(child.args[0]).parent.exists()
    assert host.adapter.close(0).cause == "clean"
    record = read_startup_failure(host.runtime_dir)
    assert record["stage"] == "adapter_open" and "the open broke" in record["error"]
    assert read_terminal(session_dir, host.runtime_dir) is None and not lease_held(session_dir)


@pytest.mark.skipif(sys.platform != "darwin", reason="the CoreAudio adapter runs on macOS only")
def test_darwin_fake_build_runs_the_coreaudio_path_through_the_daemon(tmp_path, hosts):
    session_dir, _ = _coreaudio_session(tmp_path)
    host = hosts.started(session_dir, adapter_factory=_patient)
    host.close_budget_ns = LOADED_FAKE_CLOSE_ALLOWANCE_NS
    host.close_join_s = LOADED_FAKE_CLOSE_JOIN_ALLOWANCE_S
    wait_until(lambda: frames_received(host) >= RATE // 2, seconds=30)
    observation = call(host.runtime_dir, "observe")["observation"]
    capture = observation["capture"]
    assert capture["adapter"] == "coreaudio-hal-input/v1" and capture["state"] == "running"
    assert capture["device"]["device_instance"] == "audioobject-4242"
    assert capture["clocks"]["host_time_scope"] == "mach_absolute_time_ns"
    assert capture["clocks"]["device_sample_time"] == "available"
    assert capture["adapter_stats"]["callback_ns"]["scope"] == "this_runtime_measured"
    def helper_stats():
        stats = call(host.runtime_dir, "observe")["observation"]["capture"]["adapter_stats"]
        return stats if "backlog_blocks_lower_bound" in stats else None

    stats = wait_until(helper_stats, seconds=30)
    assert stats["backlog_blocks_lower_bound"] >= 0 and {"queued_items", "queued_audio_frames"} <= set(stats)
    assert observation["coverage"]["audio_source"] == "device_input"
    assert list(Path(session_dir).rglob("listen-capture")) == []
    reply = call(host.runtime_dir, "stop", stop_args(host.runtime_dir), timeout_s=60)
    assert reply["terminal_sealed"]
    _, terminal = read_terminal(session_dir, host.runtime_dir)
    assert terminal["stop"]["adapter_close"] == "clean" and terminal["capture"]["callback_ns"]["samples"] > 0
    provenance = terminal["capture"]["provenance"]
    assert provenance["macos_version"] == "26.0.0" and provenance["driver_version"] == "not_reported"
    assert provenance["build_json_sha256"] == listen_host.load_session(session_dir)[2]["build"]["build_json_sha256"]
    exit_report = terminal["stop"]["adapter_exit"]
    assert exit_report["exit_code"] == 0 and exit_report["exit_reason"] == "quit"
    assert exit_report["counters"]["dropped_frames"] == 0 and exit_report["counters"]["blocks_written"] > 0
    assert terminal["stop"]["helper_drops_after_last_block_frames"] == 0
    assert "capture_failed" not in journal_types(host.runtime_dir)
    assert host.adapter._process.poll() is not None


@pytest.mark.skipif(sys.platform != "darwin", reason="the CoreAudio adapter runs on macOS only")
@pytest.mark.parametrize("fake_args, reasons, why", [
    (("--start-refusal", "microphone_not_authorized"), ["microphone_not_authorized"],
     {"exit_reason": "refused", "exit_code": 3, "microphone_authorization": "denied"}),
    (("--start-failed",), ["helper_exited"],
     {"exit_reason": "start_failed", "exit_code": 5, "exit_stage": "start", "exit_osstatus": -10851}),
    (("--device-changed", "channel_map_changed"), ["device_changed_before_start"],
     {"exit_reason": "device_changed_before_start", "exit_code": 4, "exit_event_type": "channel_map_changed"}),
    (("--device-changed", "device_lost"), ["device_not_alive"],
     {"exit_reason": "device_changed_before_start", "exit_code": 4, "exit_event_type": "device_lost"}),
    (("--hog-pid", "unreadable", "--device-changed", "device_property_unreadable"), ["device_changed_before_start"],
     {"exit_reason": "device_changed_before_start", "exit_code": 4, "exit_event_type": "device_property_unreadable",
      "exit_event_value": {"property": "hog_mode"}}),
])
def test_a_start_refusal_seals_what_the_helper_said_when_io_didnt_start(tmp_path, hosts, fake_args, reasons,
                                                                        why):
    session_dir, _ = _coreaudio_session(tmp_path, *fake_args)
    host = hosts.build(session_dir, adapter_factory=_patient)
    assert host.start() == listen_host.EXIT_REFUSED
    _, terminal = read_terminal(session_dir, host.runtime_dir)
    refusal = terminal["capture"]["refusal"]
    assert refusal["reasons"] == reasons and refusal["refused_at"] == "start"
    assert {k: refusal["detail"].get(k) for k in why} == why and "stderr_tail" not in refusal["detail"]
    assert refusal["open_detail"]["microphone_authorization"] == "authorized"
    exit_report = terminal["stop"]["adapter_exit"]
    assert {k: exit_report.get(k) for k in why} == why
    if why["exit_reason"] == "device_changed_before_start":
        assert terminal["stop"]["adapter_close"] == "clean"
    assert terminal["coverage"]["audio_source"] == "none"
    provenance = terminal["capture"]["provenance"]
    assert provenance["macos_version"] == "26.0.0" and provenance["driver_version"] == "not_reported"
    assert provenance["helper_sha256"] == listen_host.load_session(session_dir)[2]["build"]["helper_sha256"]


def test_closing_the_helper_counts_discarded_audio_frames_apart_from_protocol_items(tmp_path):
    adapter = listen_adapters.CoreAudioHelperAdapter(UNRECORDED_BUILD_ID, UID, RATE, FakeClock())
    adapter._closing.set()
    frames = listen_adapters.BlockFrame(0, 0.0, 0, 3, 512, 2, 0, 0, 0, 0, np.zeros((512, 2), np.float32))
    for item in (("block", frames, 0), ("block", frames, 0), ("event", {}, 0), ("eof", None, 0)):
        adapter._put(item)
    assert adapter._discarded == {"items": 4, "blocks": 2, "audio_frames": 1024}


def test_the_reader_queue_is_reported_in_items_and_in_the_audio_frames_its_blocks_hold(tmp_path):
    adapter = listen_adapters.CoreAudioHelperAdapter(UNRECORDED_BUILD_ID, UID, RATE, FakeClock())
    frames = listen_adapters.BlockFrame(0, 0.0, 0, 3, 512, 2, 0, 0, 0, 0, np.zeros((512, 2), np.float32))
    for item in (("block", frames, 0), ("block", frames, 0), ("event", {}, 0)):
        adapter._put(item)
    stats = adapter.stats()
    assert (stats["queued_items"], stats["queued_audio_frames"]) == (3, 1024) and "queued_frames" not in stats


def test_a_reader_waiting_on_a_full_queue_says_so_so_a_late_heartbeat_isnt_blamed_on_the_helper(tmp_path):
    clock = FakeClock()
    adapter = listen_adapters.CoreAudioHelperAdapter(UNRECORDED_BUILD_ID, UID, RATE, clock)
    adapter._heartbeat((0, 0, 0, 0, 0), clock.monotonic_ns())
    adapter._frames = listen_adapters.queue.Queue(maxsize=1)
    adapter._frames.put(("event", {}, 0))
    assert adapter.stats()["reader_backpressured"] is False
    reader = threading.Thread(target=adapter._put, args=(("event", {}, 1),), daemon=True)
    reader.start()
    try:
        wait_until(lambda: adapter.stats()["reader_backpressured"], seconds=30)
    finally:
        adapter._frames.get()
        reader.join(30)
    assert not reader.is_alive() and adapter.stats()["reader_backpressured"] is True
    adapter._heartbeat((0, 0, 0, 0, 0), clock.advance(1))
    assert adapter.stats()["reader_backpressured"] is False


def test_a_reader_kept_waiting_by_a_slow_steady_drain_reads_backpressured_at_the_late_heartbeat(tmp_path):
    clock = FakeClock()
    adapter = listen_adapters.CoreAudioHelperAdapter(UNRECORDED_BUILD_ID, UID, RATE, clock)
    adapter._heartbeat((0, 0, 0, 0, 0), clock.monotonic_ns())
    adapter._frames = listen_adapters.queue.Queue(maxsize=1)
    adapter._frames.put(("event", {}, 0))
    items = [("event", {}, n) for n in range(1, 6)]
    reader = threading.Thread(target=lambda: [adapter._put(item) for item in items], daemon=True)
    reader.start()
    samples = []
    try:
        wait_until(lambda: adapter.stats()["reader_backpressured"], seconds=30)
        for _ in items:
            adapter._frames.get(timeout=30)
            time.sleep(0.005)
            samples.append(adapter.stats()["reader_backpressured"])
    finally:
        while reader.is_alive() and not adapter._frames.empty():
            adapter._frames.get_nowait()
        reader.join(30)
    assert not reader.is_alive() and samples == [True] * len(items)
    watchdog = Watchdog(500, clock.monotonic_ns())
    stats = adapter.stats()
    facts, _ = watchdog.tick(clock.advance(2_100_000_000), heartbeat_ns=stats["last_heartbeat_monotonic_ns"],
                             reader_backpressured=stats["reader_backpressured"])
    (late,) = [f for f in facts if f.type == "helper_unresponsive"]
    assert late.detail["reader_backpressured"] is True


def test_importing_the_launcher_loads_no_numpy_and_starts_nothing():
    code = ("import sys, threading; import pocket_music.listen, pocket_music.listen_host; "
            "assert 'numpy' not in sys.modules, 'numpy'; assert threading.active_count() == 1")
    subprocess.run([sys.executable, "-c", code], check=True, timeout=60, env={**os.environ, "PYTHONPATH": SRC})


def _connected_runtime(tmp_path):
    runtime = tmp_path / f"0001-rt-{'0' * 16}"
    runtime.mkdir()
    (runtime / listen_host.CONNECTION_NAME).write_text("{}")
    return runtime


def test_a_ping_the_daemon_refuses_is_no_answer_and_the_start_keeps_waiting(tmp_path, monkeypatch):
    runtime = _connected_runtime(tmp_path)
    replies = [PocketError("Listen runtime is busy", hint=listen_host._BUSY_HINT),
               PocketError("Listen runtime internal error; the runtime is still serving"),
               {"status": "ok", "runtime_state": "running", "capture_state": "running", "capture_cause": None,
                "frames_received": 480, "head_sequence": 3}]

    def ping(*args, **kwargs):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(listen_host, "call", ping)
    result = wait_for_runtime(tmp_path, runtime, _FakePopen(10 ** 6), timeout_s=5, poll_s=0.01)
    assert result["outcome"] == "capturing" and result["ping"]["frames_received"] == 480 and not replies

    def busy(*args, **kwargs):
        raise PocketError("Listen runtime is busy", hint=listen_host._BUSY_HINT)

    monkeypatch.setattr(listen_host, "call", busy)
    result = wait_for_runtime(tmp_path, runtime, _FakePopen(10 ** 6), timeout_s=0.2, poll_s=0.01)
    assert result["outcome"] == "start_unconfirmed" and result["ping"] is None


@pytest.mark.parametrize("state, record", [("torn", "unverified"), ("absent", "missing"), ("sealed", "sealed")])
def test_a_startup_failure_record_that_doesnt_verify_is_reported_never_raised(tmp_path, monkeypatch, state,
                                                                            record):
    runtime = tmp_path / f"0001-rt-{'0' * 16}"
    runtime.mkdir()
    failure = {"schema": listen_host.STARTUP_FAILURE_SCHEMA, "runtime_epoch": epoch_of(runtime),
               "stage": "socket", "error": "No space left on device", "wall_utc": "2026-10-23T00:00:00.000Z"}
    if state == "torn":
        (runtime / listen_host.STARTUP_FAILURE_NAME).write_text(json.dumps(failure))
    elif state == "sealed":
        listen_host._seal(runtime / listen_host.STARTUP_FAILURE_NAME, failure)
    (runtime / listen_host.STDERR_NAME).write_text("OSError: [Errno 28] No space left on device\n")
    result = wait_for_runtime(tmp_path, runtime, _FakePopen(0), timeout_s=1, poll_s=0.01)
    assert result["outcome"] == "host_failed" and result["startup_failure_record"] == record
    assert (result["startup_failure"] is None) is (state != "sealed")
    session = {"config": {"adapter": {"kind": "coreaudio"}, "routes": []}}
    receipt = listen._failed_receipt(tmp_path, session, "start-1", runtime,
                                     {"runtime_epoch": epoch_of(runtime), "ordinal": 1}, result,
                                     {"start_outcome": "host_failed", "replayed": False,
                                      "previous_runtime": None})
    assert receipt["status"] == "failed" and receipt["startup_failure"]["record"] == record
    assert "No space left" in receipt["startup_failure"]["stderr_tail"]
    assert ("doesn't verify" in receipt["next_actions"][0]) is (state == "torn")
    assert receipt["coverage"]["audio_source"] == "none"


def test_runtimes_must_be_the_sessions_own_private_directory(tmp_path):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config())
    base = Path(session_dir) / "runtimes"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    base.symlink_to(elsewhere)
    with pytest.raises(PocketError) as caught:
        listen_host.allocate_runtime(session_dir)
    assert caught.value.code == "evidence_mismatch" and list(elsewhere.iterdir()) == []
    base.unlink()
    base.mkdir(mode=0o700)
    os.chmod(base, 0o770)
    with pytest.raises(PocketError):
        listen_host.allocate_runtime(session_dir)
    os.chmod(base, 0o700)
    assert listen_host.allocate_runtime(session_dir)[0] == 1


def test_the_daemon_gets_the_build_registry_as_an_absolute_path(tmp_path, monkeypatch):
    (tmp_path / "caller").mkdir()
    (tmp_path / "runtime").mkdir()
    monkeypatch.chdir(tmp_path / "caller")
    env = listen_host.host_env({"POCKET_LISTEN_DIR": "relative-registry", "PATH": "/bin"})
    expected = str((tmp_path / "caller" / "relative-registry").resolve())
    assert env["POCKET_LISTEN_DIR"] == expected and env["PATH"] == "/bin"
    monkeypatch.chdir(tmp_path / "runtime")
    assert str(listen_native.registry_dir(env)) == expected
    assert listen_host.host_env({})["POCKET_LISTEN_DIR"] == str(Path("~/.pocket/listen").expanduser().resolve())


def test_the_daemon_never_imports_code_from_its_runtime_directory(tmp_path, monkeypatch):
    planted = tmp_path / "runtime" / "pocket_music"
    planted.mkdir(parents=True)
    marker = tmp_path / "planted-ran"
    (planted / "__init__.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    (planted / "listen_host.py").write_text("")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "theirs"))
    argv = listen_host.host_argv(tmp_path, tmp_path / "runtime", 9)
    env = listen_host.host_env()
    assert argv[1] == "-P" and argv[2:4] == ["-m", "pocket_music.listen_host"]
    assert env["PYTHONPATH"].split(os.pathsep) == [SRC, str(tmp_path / "theirs")]
    run = subprocess.run([*argv[:4], "--help"], check=False, cwd=tmp_path / "runtime", env=env,
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0 and "listen_host" in run.stdout and not marker.exists()
    subprocess.run([argv[0], *argv[2:4], "--help"], check=False, cwd=tmp_path / "runtime", env=env,
                   capture_output=True, timeout=60)
    assert marker.exists()


def test_an_installed_package_stays_behind_the_standard_library_in_the_daemon(monkeypatch):
    monkeypatch.setattr(listen_host, "_site_directories", lambda: {SRC})
    env = listen_host.host_env({"PATH": "/bin"})
    assert "PYTHONPATH" not in env and env["PATH"] == "/bin"
    assert listen_host.host_env({"PYTHONPATH": "/theirs"})["PYTHONPATH"] == "/theirs"
    monkeypatch.setattr(listen_host, "_site_directories", set)
    assert listen_host.host_env({"PYTHONPATH": "/theirs"})["PYTHONPATH"].split(os.pathsep) == [SRC, "/theirs"]


class _DyingStream:
    def __init__(self, gate):
        self.gate = gate

    def write(self, data):
        self.gate.wait(5)
        raise RuntimeError("unexpected writer fault")

    def fileno(self):
        return 0

    def close(self):
        pass


def test_a_journal_writer_that_dies_counts_every_line_it_never_wrote(tmp_path):
    gate = threading.Event()
    journal = RuntimeJournal(tmp_path / "journal.ndjson", FakeClock(), opener=lambda path: _DyingStream(gate))
    writer = journal.start()
    assert all(journal.offer({"phase": "test", "index": index}) for index in range(5))
    gate.set()
    writer.join(5)
    assert not writer.is_alive()
    closed = journal.close(writer, 0.5)
    assert (closed["state"], closed["lines"], closed["dropped_lines"]) == ("failed", 0, 5)
    assert closed["closed"] is True and closed["lines_unwritten_at_close"] == 0


class _ExitingStream(io.FileIO):
    def __init__(self, path):
        super().__init__(path, "x")

    def write(self, data):
        raise SystemExit("journal writer exited")


def test_a_journal_writer_that_raises_any_exception_degrades_the_journal_and_the_runtime_still_stops(tmp_path,
                                                                                                    hosts):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(RATE * 3))))
    host = hosts.started(session_dir, journal_opener=_ExitingStream)
    wait_until(lambda: not host._threads["journal"].is_alive())
    assert host.journal.state == "failed"
    assert host.stop("listen_stop", request_id="stop-1")["terminal_sealed"]
    _, terminal = read_terminal(session_dir, host.runtime_dir)
    journal = terminal["journal"]
    assert (terminal["state"], journal["state"], journal["lines"], journal["closed"]) == (
        "stopped", "failed", 0, True)
    assert journal["dropped_lines"] >= 1 and journal["lines_unwritten_at_close"] == 0


def test_a_stop_that_raises_ends_the_runtime_as_failed_without_a_terminal(tmp_path, hosts, monkeypatch):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(RATE * 3))))
    host = hosts.started(session_dir, threads=False, clock=FakeClock())
    host.step()

    def broken(*args):
        raise ValueError("terminal assembly failed")

    monkeypatch.setattr(host, "_stop_ladder", broken)
    reply = host.stop("listen_stop", request_id="stop-1", expected_runtime_epoch=host.runtime_epoch)
    assert reply["terminal_sealed"] is False and reply["reason"] == "stop_failed"
    host._finished.set()
    assert host.run() == listen_host.EXIT_FAILED
    assert not (host.runtime_dir / listen_host.TERMINAL_POINTER_NAME).exists()
    assert "stop: terminal assembly failed" in host._error


def _breaking_stderr_tail(session, clock):
    adapter = listen_host._default_adapter(session, clock)

    def broken(limit):
        raise RuntimeError("the stderr tail broke")
    adapter.stderr_tail = broken
    return adapter


def _refusing_start_with_a_breaking_stderr_tail(session, clock):
    adapter = _breaking_stderr_tail(session, clock)
    adapter.start = lambda channels: StartResult(False, ("helper_start_timeout",), tuple(channels))
    return adapter


def test_a_helper_stderr_tail_that_raises_still_seals_the_stop_without_a_tail(tmp_path, hosts):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(RATE * 3))))
    host = hosts.started(session_dir, threads=False, clock=FakeClock(), adapter_factory=_breaking_stderr_tail)
    host.step()
    assert host.stop("listen_stop", request_id="stop-1")["terminal_sealed"]
    _, terminal = read_terminal(session_dir, host.runtime_dir)
    assert terminal["state"] == "stopped" and terminal["capture"]["helper_stderr_tail"] is None


@pytest.mark.parametrize(("options", "factory", "refused_at"), [
    pytest.param({"spec": script(tone(44100), rate=44100)}, _breaking_stderr_tail, "open", id="open"),
    pytest.param({"routes": ({"route_id": "master", "role": "master", "channels": [2, 3]},)},
                 _breaking_stderr_tail, "routes", id="routes"),
    pytest.param({}, _refusing_start_with_a_breaking_stderr_tail, "start", id="start")])
def test_a_helper_stderr_tail_that_raises_still_seals_the_refusal_without_a_tail(
        tmp_path, hosts, options, factory, refused_at):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(**options))
    host = hosts.build(session_dir, threads=False, clock=FakeClock(), adapter_factory=factory)
    assert host.start() == listen_host.EXIT_REFUSED and host.core is None
    _, terminal = read_terminal(session_dir, host.runtime_dir)
    assert terminal["capture"]["refusal"]["refused_at"] == refused_at
    assert terminal["capture"]["helper_stderr_tail"] is None
    assert read_startup_failure(host.runtime_dir) is None and not lease_held(session_dir)


def test_a_close_thread_that_cant_start_still_seals_the_startup_failure_and_frees_the_lease(
        tmp_path, hosts, monkeypatch):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(RATE))))

    def factory(session, clock):
        adapter = listen_host._default_adapter(session, clock)
        opened = adapter.open

        def broken():
            opened()
            raise RuntimeError("the open broke")
        adapter.open = broken
        return adapter

    host = hosts.build(session_dir, threads=False, clock=FakeClock(), adapter_factory=factory)
    real = listen_host._start_thread

    def no_close_thread(name, body, *, on_error):
        if name == "adapter-close":
            raise RuntimeError("can't start new thread")
        return real(name, body, on_error=on_error)
    monkeypatch.setattr(listen_host, "_start_thread", no_close_thread)
    assert host.start() == listen_host.EXIT_FAILED
    record = read_startup_failure(host.runtime_dir)
    assert record["stage"] == "adapter_open" and "the open broke" in record["error"]
    assert not lease_held(session_dir) and not host.socket_dir.exists()


def test_a_stop_reaches_the_runtime_while_readers_hold_every_slot(tmp_path, hosts):
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(RATE * 10))))
    host = hosts.started(session_dir)
    runtime = host.runtime_dir
    wait_until(lambda: host.core.broker.head >= 3)
    for _ in range(listen_host.MAX_CONNECTIONS):
        assert host._slots.acquire(blocking=False)
    try:
        with pytest.raises(PocketError) as caught:
            call(runtime, "observe")
        assert "busy" in str(caught.value)
        reply = call(runtime, "stop", stop_args(runtime), timeout_s=STOP_REPLY_S)
        assert reply["terminal_sealed"] is True and reply["stop_request_id"] == "stop-1"
    finally:
        for _ in range(listen_host.MAX_CONNECTIONS):
            host._slots.release()


def test_a_synthetic_runtime_records_its_provenance_as_not_applicable(tmp_path, hosts):
    from pocket_music import __version__
    session_dir, _ = seal_session(tmp_path / "session", resolved_config(script(tone(RATE * 3))))
    host = hosts.started(session_dir, threads=False, clock=FakeClock())
    host.step()
    opened = next(e for e in host.core.broker.view(64)["recent_health"]
                  if e["evidence"]["type"] == "capture_opened")
    expected = {"pocket_version": __version__, "macos_version": "not_applicable",
                "helper_sha256": "not_applicable", "build_json_sha256": "not_applicable",
                "driver_version": "not_applicable"}
    assert opened["evidence"]["detail"]["provenance"] == expected
    assert host.stop("listen_stop", request_id="stop-1")["terminal_sealed"]
    _, terminal = read_terminal(session_dir, host.runtime_dir)
    assert terminal["capture"]["provenance"] == expected
