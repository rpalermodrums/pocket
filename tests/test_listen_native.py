# SPDX-License-Identifier: AGPL-3.0-only

import concurrent.futures
import hashlib
import hmac
import itertools
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import listen_fake_helper
import numpy as np
import pytest
from conftest import UNRECORDED_BUILD_ID
from listen_fake_helper import write_fake_build

from pocket_music import listen_adapters, listen_native
from pocket_music.errors import PocketError, valid_hint
from pocket_music.listen import resolve_routes
from pocket_music.listen_adapters import (
    AdapterBlock,
    AdapterEvent,
    CoreAudioHelperAdapter,
    decode_frames,
    validate_hello,
)
from pocket_music.listen_native import HELLO_REFUSALS
from pocket_music.listen_runtime import HEARTBEAT_LIMIT_NS, FakeClock, ListenCore, StreamTracker, SystemClock

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Listen's helper protocol runs on POSIX")

SWIFT = Path(listen_native.helper_source())
UID = "fake:duo"
RATE = 48000
NS = 1_000_000_000
SELF_TEST_HANG_GUARD_S = 600
LOADED_FAKE_START_ALLOWANCE_S = 30.0
LOADED_FAKE_QUIT_ALLOWANCE_S = 30
CLOSE_HANG_GUARD_S = 30
LOADED_PROBE_ALLOWANCE_S = 30.0
PROBE_HANG_GUARD_S = 30


def _region(name):
    text = SWIFT.read_text()
    assert text.count(f"// BEGIN {name}\n") == 1 and text.count(f"// END {name}\n") == 1, name
    return text.split(f"// BEGIN {name}\n")[1].split(f"// END {name}\n")[0]


def _function(name):
    text = SWIFT.read_text()
    start = text.index(f"\nfunc {name}(")
    return text[start:text.index("\n}\n", start) + 3]


def test_swift_source_sets_no_device_property_and_never_writes_output():
    text = SWIFT.read_text()
    forbidden = ["AudioObjectSetPropertyData", "AudioDeviceSetProperty", "AudioHardwareSetProperty",
                 "AudioStreamSetProperty", "requestAccess", "requestRecordPermission", "AVAudioEngine", "AudioUnit",
                 "AudioQueue", "kAudioDevicePropertyIOProcStreamUsage", "outOutputData", "AudioHardwareCreate",
                 "AudioHardwareDestroy", "kAudioDevicePropertyDefault", "AF_INET", "socket("]
    assert [token for token in forbidden if token in text] == []
    assert re.findall(r"\bAudio\w*Set\w*\s*\(", text) == []
    assert text.splitlines()[0] == "// SPDX-License-Identifier: AGPL-3.0-only"


def test_swift_realtime_section_has_no_allocation_locks_io_or_objc():
    section = _region("REALTIME")
    forbidden = ["print", "String(", "Array(", "[UInt8]", "Data(", "JSONSerialization", "NSLock", "Dispatch",
                 "FileHandle", "malloc", ".allocate(", "Unmanaged", "class ", "@objc", "NSString", "fwrite", "write(",
                 "usleep", "sleep(", "AudioObjectSetPropertyData", "AudioObjectGetPropertyData", "outOutputData",
                 "Thread", "exit(", "append(", "map {", "map(", "\"", "readValue", "readString"]
    assert [token for token in forbidden if token in section] == []
    assert re.findall(r"\b(?:try|throw|throws|await|async)\b", section) == []
    prefix = "let listenIOProc: AudioDeviceIOProc ="
    (declaration,) = [line for line in section.split("\n") if line.startswith(prefix)]
    assert declaration == ("let listenIOProc: AudioDeviceIOProc = { _, _, inInputData, inInputTime, _, _, "
                           "inClientData in")
    assert section.count("AudioDeviceIOProc") == 1 and "mach_absolute_time()" in section


def test_swift_self_test_path_calls_no_coreaudio_api():
    hal = ("AudioObject", "AudioDevice", "AudioHardware", "AVCaptureDevice", "AVFoundation", "kAudio")
    for name in ("SELFTEST", "DEVICE-FREE"):
        assert [token for token in hal if token in _region(name)] == [], name
    text = SWIFT.read_text()
    dispatch = text[text.rindex("\nsignal(SIGPIPE, SIG_IGN)\n"):]
    assert [token for token in hal if token in dispatch] == []
    assert dispatch.index('"self-test"') < dispatch.index('"list-devices"') < dispatch.index('"capture"')
    assert "listenIOProc(0," in _region("SELFTEST")


def test_swift_capture_reads_permission_before_any_io_and_never_requests_it():
    capture = _function("capture")
    first_check = capture.index("microphoneAuthorization()")
    recheck = capture.index("microphoneAuthorization()", first_check + 1)
    assert first_check < capture.index("translateUID(") < capture.index("frameHello")
    assert recheck < capture.index("AudioDeviceCreateIOProcID") < capture.index("AudioDeviceStart(")
    assert 'authorization != "authorized"' in _function("refusals")
    assert "AVCaptureDevice.authorizationStatus(for: .audio)" in _function("microphoneAuthorization")
    for listing in (_function("listDevices"), _function("inputDevices"), _function("readDevice")):
        assert "AudioDeviceCreateIOProcID" not in listing and "AudioDeviceStart" not in listing


def test_adapter_argv_and_plh1_constants_match_the_swift_helper():
    text = SWIFT.read_text()
    allowed = set(re.search(r'options\(args, \["device-uid", ([^\]]*)\]\)', text).group(1).replace('"', "")
                  .replace(" ", "").split(",")) | {"device-uid"}
    argv_options = {a[2:] for a in listen_adapters._HELPER_ARGS[::2]} | {"device-uid", "expected-sample-rate"}
    assert argv_options == allowed
    for schema in (*listen_native.HELPER_SCHEMAS.values(), listen_native.DEVICES_SCHEMA):
        assert f'"{schema}"' in text
    numbers = dict(re.findall(r"frame(\w+): UInt16 = (\d)", text))
    assert {k.lower(): int(v) for k, v in numbers.items()} == {
        "hello": listen_native.HELLO, "block": listen_native.BLOCK, "event": listen_native.EVENT,
        "heartbeat": listen_native.HEARTBEAT_TYPE, "exit": listen_native.EXIT, "started": listen_native.STARTED}
    magic = re.search(r"var frame: \[UInt8\] = \[([^\]]+)\]", text)[1]
    assert bytes(int(byte, 16) for byte in magic.split(",")) == listen_native.MAGIC
    bound = re.search(r'integer\(o, "max-block-frames", (\d+), 32\.\.\.(\d+)\)', _function("capture"))
    assert int(bound[1]) == int(bound[2]) == listen_native.MAX_BLOCK_FRAMES
    events = set(re.findall(r'\("(\w+)", \[', _function("deviceChange")))
    assert events <= set(listen_adapters.HELPER_EVENTS)
    refused = set(re.findall(r'reasons\.append\("(\w+)"\)', _function("refusals")))
    assert refused | {"device_not_found"} == set(HELLO_REFUSALS)


def test_the_orderly_exit_codes_are_the_ones_capture_returns():
    capture = _function("capture")
    pairs = (re.findall(r'\["reason": "(\w+)"[^;]*?\]\)(?:\s*;)?\s*return (\d+)', capture)
             + re.findall(r'reason = "(\w+)"; code = (\d+)', capture))
    codes = {}
    for reason, code in pairs:
        codes.setdefault(reason, set()).add(int(code))
    assert codes and all(len(found) == 1 for found in codes.values()), codes
    orderly = set(listen_native.ORDERLY_EXIT_CODES.values())
    assert {r: c for r, (c,) in codes.items() if c in orderly} == listen_native.ORDERLY_EXIT_CODES


def test_the_helper_exit_counts_the_audio_frames_it_held_back_as_well_as_the_blocks():
    capture = _function("capture")
    assert '"undelivered_blocks": undelivered' in capture
    assert "let undeliveredFrames = value(ctx, .framesWritten) &- value(ctx, .framesRead)" in capture
    assert '"undelivered_frames": undeliveredFrames' in capture
    assert {"undelivered_blocks", "undelivered_frames"} <= set(listen_adapters._EXIT_COUNTERS)


def test_swift_device_reads_never_guess_a_property_they_couldnt_read():
    change = _function("deviceChange")
    for property_name in ("device_is_alive", "nominal_sample_rate", "hog_mode", "stream_configuration",
                          "input_streams"):
        assert f'return ("device_property_unreadable", ["property": "{property_name}"])' in change
    assert "guard let streams = readStreamsChecked(start.id) else {" in change
    assert "readStreams(" not in change
    checked = _function("readStreamsChecked")
    assert "guard let first = readValue(s, address(kAudioStreamPropertyStartingChannel)" in checked
    assert "let format = readValue(s, address(kAudioStreamPropertyVirtualFormat)" in checked
    assert '"devices_unreadable": unreadable' in _function("listDevices")
    assert "configured.filter { $0.1 == nil }.count" in _function("inputDevices")
    assert "guard let hog = readValue(start.id, address(kAudioDevicePropertyHogMode)" in change
    assert "hogPID: readValue(id, address(kAudioDevicePropertyHogMode), pid_t(-1)),\n" in _function("readDevice")
    assert '"hog_mode_pid": json(d.hogPID.map { Int($0) })' in _function("deviceJSON")
    assert "if let hog = d.hogPID, hog != -1 && hog != getpid()" in _function("refusals")
    assert "HogMode), pid_t(-1)) ?? " not in SWIFT.read_text()


def test_the_late_pre_start_check_names_a_property_it_couldnt_read_instead_of_guessing_it():
    capture = _function("capture")
    change = capture.index("let change = deviceChange(info, expectedRate: rate)")
    late = capture.index("refusals(readDevice(info.id)")
    assert change < late
    assert 'let late = change?.0 == "device_property_unreadable" ? []' in capture
    assert "if let (type, detail) = change {" in capture


def test_each_helper_drain_pass_is_bounded_so_its_control_checks_keep_running():
    drain = _function("drainOnce")
    assert "first &+ drainMaxBlocks" in drain and "drainBudgetMs" in drain
    assert drain.count(".blocksWritten") == 1
    capture = _function("capture")
    assert "while drainOnce(ctx, wire) > 0 {}" in capture
    assert "if drained == 0 { usleep(2000) }" in capture


def test_importing_the_fake_helper_loads_no_numpy():
    code = "import sys, listen_fake_helper; assert 'numpy' not in sys.modules"
    src = str(Path(listen_native.__file__).resolve().parents[1])
    tests = str(Path(listen_fake_helper.__file__).resolve().parent)
    subprocess.run([sys.executable, "-c", code], check=True, timeout=60,
                   env={**os.environ, "PYTHONPATH": os.pathsep.join([src, tests])})


def test_helper_ring_frames_matches_the_helper_allocation():
    assert "powerOfTwo(atLeast: max(frames, 1))" in SWIFT.read_text()
    assert [listen_native.helper_ring_frames(rate) for rate in (44100, 48000, 88200, 96000)] == [
        131072, 131072, 262144, 262144]


NATIVE = pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("swiftc") or os.environ.get("POCKET_LISTEN_NATIVE_BUILD") != "1",
    reason="needs macOS, swiftc and POCKET_LISTEN_NATIVE_BUILD=1 (a cold compile takes tens of seconds)")


@NATIVE
def test_swift_helper_compiles_and_self_test_accounts_every_frame():
    built = listen_native.build_helper()
    assert built["build"]["swiftc_flags"] == ["-O", "-enforce-exclusivity=unchecked", "-swift-version", "5"]
    assert listen_native.verify_build(built["build_id"])["helper_sha256"] == built["build"]["files"]["listen-capture"]
    helper = listen_native.copy_helper(listen_native.verify_build(built["build_id"]))
    try:
        result = subprocess.run([str(helper), "self-test", "--blocks", "400", "--block-frames", "480",
                                 "--stall-after-blocks", "20", "--stall-ms", "400", "--ring-frames", "2048"],
                                capture_output=True, timeout=120, check=False, cwd=helper.parent)
    finally:
        listen_native.remove_helper_copy(helper)
    assert result.returncode == 0, result.stderr[-2000:]
    frames = decode_frames(result.stdout)
    kinds = [kind for kind, _ in frames]
    assert kinds[:2] == ["hello", "started"] and kinds[-1] == "exit" and "heartbeat" in kinds
    hello, exit_record = frames[0][1], frames[-1][1]
    assert hello["mode"] == "self-test" and hello["microphone_authorization"] == "not_applicable"
    assert exit_record["reason"] == "self_test_complete" and exit_record["callbacks"] == 400
    blocks = [value for kind, value in frames if kind == "block"]
    assert exit_record["blocks_sent"] == len(blocks) and exit_record["oversize_blocks"] == 0
    assert exit_record["layout_mismatch_blocks"] == 0
    tracker = StreamTracker("rt-selftest", RATE)
    gaps, consumed = [], 0
    for value in blocks:
        start = int(value.sample_time)
        ramp = np.arange(start, start + value.frames, dtype=np.float32) / np.float32(1e7)
        assert np.array_equal(value.pcm[:, 0], ramp) and np.array_equal(value.pcm[:, 1], -ramp)
        block = AdapterBlock(value.frames, value.pcm, value.sample_time, value.host_ticks, 0, value.dropped_frames)
        _, facts = tracker.observe_block(block)
        gaps += [fact for fact in facts if fact.type == "frames_missing"]
        consumed += value.frames
    assert gaps, "the stall should have overflowed the helper ring"
    assert all(g.cause == "helper_ring_overflow" and g.detail["adapter_dropped_frames"] == g.interval[1]
               for g in gaps)
    missing = sum(g.interval[1] for g in gaps)
    assert consumed + missing == exit_record["produced_frames"] == 400 * 480
    assert missing == exit_record["dropped_frames"] == blocks[-1].dropped_frames
    first_gap = next(b for a, b in itertools.pairwise(blocks) if b.sample_time != a.sample_time + a.frames)
    assert gaps[0].interval[0] == int(first_gap.sample_time) - first_gap.dropped_frames


@NATIVE
def test_a_slow_reader_still_gets_helper_heartbeats_on_time():
    built = listen_native.build_helper()
    helper = Path(built["build_dir"]) / "listen-capture"
    process = subprocess.Popen([str(helper), "self-test", "--blocks", "4000", "--block-frames", "480",
                                "--stall-after-blocks", "0", "--stall-ms", "0", "--ring-frames", "4096"],
                               stdout=subprocess.PIPE, bufsize=0)
    decoder, frames, slow = listen_adapters.Decoder(), [], []

    def read_all():
        deadline = time.monotonic() + 60
        while sum(kind == "heartbeat" for kind, _ in frames) < 3 and time.monotonic() < deadline:
            chunk = process.stdout.read(4096)
            if not chunk:
                break
            frames.extend(decoder.feed(chunk))
            time.sleep(0.01)
        slow.extend(value for kind, value in frames if kind == "heartbeat")
        while chunk := process.stdout.read(65536):
            frames.extend(decoder.feed(chunk))

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    reading = pool.submit(read_all)
    try:
        reading.result(timeout=SELF_TEST_HANG_GUARD_S)
    except BaseException:
        process.kill()
        concurrent.futures.wait([reading], timeout=10)
        raise
    finally:
        pool.shutdown(wait=False)
    decoder.finish()
    assert process.wait(60) == 0
    exit_record = frames[-1][1]
    assert exit_record["dropped_frames"] > 0 and exit_record["blocks_sent"] == exit_record["blocks_written"]
    assert len(slow) >= 3 and all(callbacks < exit_record["callbacks"] for *_, callbacks in slow)
    timebase, started = frames[0][1]["timebase"], frames[1][1]["host_now_ticks"]
    sent = [(ticks - started) * timebase["numer"] / timebase["denom"]
            for ticks in [started] + [value[0] for kind, value in frames if kind == "heartbeat"]]
    assert max(b - a for a, b in itertools.pairwise(sent)) < HEARTBEAT_LIMIT_NS


def test_the_decoder_never_buffers_more_than_the_bound_the_session_declares():
    bound = listen_native.DECODER_BUFFER_BYTES
    largest = max(listen_native.PAYLOAD_CAPS.values())
    assert bound == listen_native.HEADER.size + largest - 1 + listen_native.READ_BYTES
    decoder = listen_adapters.Decoder()
    header = listen_native.HEADER.pack(listen_native.MAGIC, listen_native.BLOCK, 0, largest, 0)
    stream = header + bytes(largest)
    chunks = [stream[i:i + listen_native.READ_BYTES] for i in range(0, len(stream), listen_native.READ_BYTES)]
    for chunk in chunks[:-1]:
        assert len(decoder._buffer) + len(chunk) <= bound
        assert decoder.feed(chunk) == []
    assert len(decoder._buffer) == len(stream) - len(chunks[-1]) <= bound - listen_native.READ_BYTES
    with pytest.raises(listen_adapters.ProtocolError):
        decoder.feed(chunks[-1])
    oversized = listen_adapters.Decoder()
    with pytest.raises(listen_adapters.ProtocolError) as raised:
        oversized.feed(listen_native.HEADER.pack(listen_native.MAGIC, listen_native.BLOCK, 0, largest + 1, 0))
    assert raised.value.reason == "payload_length"


def _hello(**changes):
    device = {"uid": UID, "name": "Fake Duo", "manufacturer": None, "model_uid": None, "transport": "virtual",
              "audio_object_id": 7, "alive": True, "hog_mode_pid": -1, "nominal_sample_rate": 48000.0,
              "input_channels": 2, "stream_configuration": [2], "channel_names": ["", ""],
              "input_streams": [{"starting_channel": 0, "channels": 2, "virtual_format": {}}]}
    hello = {"schema": "pocket.listen-helper-hello/v1", "mode": "capture", "helper_pid": 321, "device": device,
             "microphone_authorization": "authorized", "timebase": {"numer": 125, "denom": 3},
             "host_time_scope": "mach_absolute_time", "max_block_frames": 8192, "refusal": []}
    for key, value in changes.items():
        if key.startswith("device_"):
            device[key[len("device_"):]] = value
        else:
            hello[key] = value
    return hello


@pytest.mark.parametrize("changes, reasons", [
    ({}, ()),
    ({"schema": "pocket.listen-helper-exit/v1"}, ("helper_protocol_error",)),
    ({"mode": "self-test"}, ("helper_protocol_error",)),
    ({"device_uid": "fake:other"}, ("device_not_found",)),
    ({"device_alive": False}, ("device_not_alive",)),
    ({"device_hog_mode_pid": 999}, ("device_hogged",)),
    ({"device_hog_mode_pid": 321}, ()),
    ({"device_hog_mode_pid": None}, ()),
    ({"device_hog_mode_pid": "-1"}, ("helper_protocol_error",)),
    ({"microphone_authorization": "not_determined"}, ("microphone_not_authorized",)),
    ({"microphone_authorization": "denied"}, ("microphone_not_authorized",)),
    ({"microphone_authorization": "restricted"}, ("microphone_not_authorized",)),
    ({"microphone_authorization": "unknown"}, ("microphone_not_authorized",)),
    ({"microphone_authorization": "granted"}, ("helper_protocol_error",)),
    ({"device_nominal_sample_rate": 44100.0}, ("sample_rate_mismatch",)),
    ({"max_block_frames": 8193}, ("helper_protocol_error",)),
    ({"timebase": None}, ("helper_protocol_error",)),
    ({"timebase": {"numer": 0, "denom": 3}}, ("helper_protocol_error",)),
    ({"host_time_scope": "uptime"}, ("helper_protocol_error",)),
    ({"helper_pid": 322}, ("helper_protocol_error",)),
    ({"device_stream_configuration": [1]}, ("helper_protocol_error",)),
    ({"device_alive": 1}, ("helper_protocol_error",)),
    ({"refusal": ["microphone_not_authorized"]}, ("microphone_not_authorized",)),
    ({"refusal": ["device_not_found", "device_not_found"], "device": None}, ("device_not_found",)),
    ({"refusal": ["please_ignore"]}, ("helper_protocol_error",)),
    ({"refusal": None}, ("helper_protocol_error",)),
    ({"device_alive": False, "microphone_authorization": "denied", "device_nominal_sample_rate": 96000},
     ("device_not_alive", "microphone_not_authorized", "sample_rate_mismatch")),
])
def test_hello_validation_maps_each_mismatch_to_a_refusal_reason(changes, reasons):
    assert validate_hello(_hello(**changes), UID, RATE, helper_pid=321) == reasons


class Harness:
    def __init__(self):
        self.adapters = []

    def adapter(self, *fake_args, build=None, uid=UID, clock=None, **options):
        adapter = CoreAudioHelperAdapter(build or write_fake_build(*fake_args)[0], uid, RATE, clock or FakeClock(),
                                         **options)
        adapter.hello_timeout_s = adapter.started_timeout_s = LOADED_FAKE_START_ALLOWANCE_S
        self.adapters.append(adapter)
        return adapter

    def started(self, *fake_args, channels=(0, 1), **options):
        adapter = self.adapter(*fake_args, **options)
        opened = adapter.open()
        assert opened.refusal == (), opened
        started = adapter.start(channels)
        assert started.started, started
        return adapter

    def close_all(self):
        for adapter in self.adapters:
            process = adapter._process
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            adapter.close(_deadline(adapter, 5))


@pytest.fixture
def harness():
    h = Harness()
    yield h
    h.close_all()


def _read_until(adapter, predicate, seconds=10.0):
    items, deadline = [], time.monotonic() + seconds
    while time.monotonic() < deadline:
        item = adapter.read(50_000_000)
        if item is not None:
            items.append(item)
            if predicate(item, items):
                return items
    raise AssertionError(f"timed out after {len(items)} items: {items[-3:]}")


def _blocks(items):
    return [item for item in items if isinstance(item, AdapterBlock)]


def _deadline(adapter, seconds=LOADED_FAKE_QUIT_ALLOWANCE_S):
    return adapter._clock.monotonic_ns() + seconds * NS


def test_fake_helper_capture_round_trip_reports_identity_clocks_and_quits_cleanly(harness):
    clock = FakeClock()
    adapter = harness.adapter(clock=clock)
    opened = adapter.open()
    assert opened.refusal == () and opened.identity.uid == UID and opened.identity.name == "Fake Duo"
    assert opened.identity.instance == "audioobject-4242" and opened.detail["microphone_authorization"] == "authorized"
    fmt = opened.format
    assert (fmt.sample_rate, fmt.device_input_channels, fmt.host_time_scope, fmt.stream_configuration) == (
        48000, 2, "mach_absolute_time_ns", (2,))
    started = adapter.start((1, 0))
    assert started.started and started.selected_channels == (1, 0) and started.ring_frames == 131072
    assert adapter.stats()["last_heartbeat_monotonic_ns"] == clock.monotonic_ns()
    blocks = _blocks(_read_until(adapter, lambda item, items: len(_blocks(items)) >= 30))
    assert all(b.pcm.shape == (b.frames, 2) and b.pcm.dtype == np.float32 and b.pcm.flags.c_contiguous
               for b in blocks)
    assert [b.device_sample_time for b in blocks] == [480.0 * i for i in range(len(blocks))]
    assert all(b.device_host_time_ns is not None and b.callback_ns == 5000 for b in blocks)
    assert {b.received_monotonic_ns for b in blocks} == {clock.monotonic_ns()}
    t = np.arange(480, dtype=np.float64)
    expected = (10 ** (-12 / 20) * np.sin(2 * np.pi * 480.0 * t / RATE)).astype(np.float32)
    assert np.allclose(blocks[0].pcm[:, 0], expected) and np.allclose(blocks[0].pcm[:, 1], expected)
    stats = adapter.stats()
    assert stats["blocks"] >= 30 and stats["callback_ns"]["samples"] >= 30 and stats["device_period_ns"] == 10_000_000
    assert stats["helper_pid"] == adapter.helper_pid
    closed = adapter.close(_deadline(adapter))
    assert closed.cause == "clean" and closed.clean and closed.detail["exit_code"] == 0
    assert closed.detail["exit_reason"] == "quit"
    assert adapter.close(_deadline(adapter)) is closed
    assert "fake helper received: start 1,0" in adapter.stderr_tail()


def test_close_during_capture_never_reports_a_helper_failure(harness):
    adapter = harness.started()
    _read_until(adapter, lambda item, items: len(items) >= 3)
    closed = adapter.close(_deadline(adapter))
    assert closed.cause == "clean"
    reads = []
    reader = threading.Thread(target=lambda: reads.extend(adapter.read(20_000_000) for _ in range(5)),
                              daemon=True)
    reader.start()
    reader.join(30)
    assert not reader.is_alive() and reads == [None] * 5


def test_a_helper_that_exits_nonzero_on_quit_is_not_a_clean_close(harness):
    adapter = harness.started("--quit-exit-code", "5")
    _read_until(adapter, lambda item, items: len(items) >= 3)
    closed = adapter.close(_deadline(adapter))
    assert (closed.cause, closed.clean, closed.detail["exit_code"]) == ("exited_with_error", False, 5)
    assert closed.detail["exit_reason"] == "quit" and "dropped_frames" in closed.detail["exit_counters"]


def test_helper_exit_on_stdin_eof_is_reported_as_helper_exited(harness):
    adapter = harness.started()
    _read_until(adapter, lambda item, items: len(items) >= 2)
    adapter._process.stdin.close()
    items = _read_until(adapter, lambda item, items: isinstance(item, AdapterEvent))
    event = items[-1]
    assert event.type == "helper_exited" and event.detail["exit_reason"] == "stdin_closed"
    assert event.detail["exit_code"] in (0, None)
    assert adapter.read(10_000_000) is None
    assert adapter.close(_deadline(adapter)).cause == "clean"


def test_malformed_frame_is_a_protocol_error_and_the_adapter_terminates_its_own_child(harness):
    adapter = harness.started("--malformed-after", "3")
    items = _read_until(adapter, lambda item, items: isinstance(item, AdapterEvent))
    assert len(_blocks(items)) == 3
    assert items[-1].type == "helper_protocol_error" and items[-1].detail == {"reason": "bad_magic"}
    adapter._process.wait(timeout=5)
    assert "fake helper got SIGTERM" in adapter.stderr_tail()
    assert [adapter.read(10_000_000) for _ in range(3)] == [None] * 3
    assert adapter.close(_deadline(adapter)).cause == "clean"


def test_close_ladder_kills_a_helper_that_ignores_quit_eof_and_sigterm(harness):
    adapter = harness.started("--ignore-quit")
    _read_until(adapter, lambda item, items: len(items) >= 2)
    begun = time.monotonic()
    closed = adapter.close(_deadline(adapter, 3))
    elapsed = time.monotonic() - begun
    assert closed.cause == "killed" and not closed.clean
    assert adapter._process.returncode == -signal.SIGKILL
    assert 2.5 <= elapsed < CLOSE_HANG_GUARD_S and closed.detail["budget_ms"] == 3000


def test_close_ladder_follows_a_short_deadline(harness):
    adapter = harness.started("--ignore-quit")
    begun = time.monotonic()
    assert adapter.close(_deadline(adapter, 0.6)).cause == "killed"
    assert 0.5 <= time.monotonic() - begun < CLOSE_HANG_GUARD_S


@pytest.mark.parametrize("fake_args, authorization", [
    (("--refuse", "microphone_not_authorized"), "not_determined"),
    (("--refuse", "microphone_not_authorized", "--authorization", "denied"), "denied"),
    (("--authorization", "restricted"), "restricted"),
    (("--authorization", "not_determined"), "not_determined"),
])
def test_fake_helper_permission_refusal_is_reported_not_silence(harness, fake_args, authorization):
    adapter = harness.adapter(*fake_args)
    opened = adapter.open()
    assert opened.refusal == ("microphone_not_authorized",) and opened.identity is None and opened.format is None
    assert opened.detail["microphone_authorization"] == authorization
    adapter._process.wait(timeout=5)
    assert adapter._process.returncode in (0, 3)
    with pytest.raises(RuntimeError):
        adapter.start((0, 1))
    assert adapter.read(10_000_000) is None and adapter.stats()["blocks"] == 0
    assert "fake helper received" not in (adapter.stderr_tail() or "")


@pytest.mark.parametrize("fake_args, reasons", [
    (("--refuse", "device_hogged"), ("device_hogged",)),
    (("--hog-pid", "999"), ("device_hogged",)),
    (("--device-rate", "44100"), ("sample_rate_mismatch",)),
    (("--refuse", "device_not_found"), ("device_not_found",)),
])
def test_hello_refusals_from_the_helper_or_python_end_the_helper_before_io(harness, fake_args, reasons):
    adapter = harness.adapter(*fake_args)
    opened = adapter.open()
    assert opened.refusal == reasons
    assert adapter.close(_deadline(adapter)).detail["exit_code"] in (0, 3)
    assert "fake helper received" not in (adapter.stderr_tail() or "")


def test_start_refusals_are_reasons_and_a_wrong_format_never_starts_io(harness):
    adapter = harness.adapter("--non-float")
    assert adapter.open().refusal == ()
    refused = adapter.start((0, 1))
    assert not refused.started and refused.refusal == ("format_not_float32",)
    assert "fake helper received" not in (adapter.stderr_tail() or "")
    helper_side = harness.adapter("--start-refusal", "format_not_float32")
    assert helper_side.open().refusal == ()
    assert helper_side.start((0,)).refusal == ("format_not_float32",)
    unknown = harness.adapter("--start-refusal", "because")
    unknown.open()
    assert unknown.start((0,)).refusal == ("helper_protocol_error",)
    unexposed = harness.adapter()
    unexposed.open()
    assert unexposed.start((0, 2)).refusal == ("no_metered_channels_exposed",)


def test_hello_and_started_timeouts_are_refusals(harness):
    slow = harness.adapter("--hello-delay-ms", "3000")
    slow.hello_timeout_s = 0.3
    assert slow.open().refusal == ("helper_start_timeout",)
    silent = harness.adapter("--no-started")
    silent.started_timeout_s = 0.3
    assert silent.open().refusal == ()
    assert silent.start((0, 1)).refusal == ("helper_start_timeout",)
    assert silent.close(_deadline(silent)).cause in ("clean", "terminated", "killed")


def test_helper_build_changed_is_refused_before_anything_runs(harness):
    build_id, build = write_fake_build()
    helper = build / "listen-capture"
    helper.write_text(helper.read_text() + "# changed\n")
    changed = harness.adapter(build=build_id)
    opened = changed.open()
    assert opened.refusal == ("helper_build_changed",) and changed.helper_pid is None
    other = harness.adapter(expected_helper_sha256="0" * 64)
    assert other.open().refusal == ("helper_build_changed",) and other.helper_pid is None
    assert other.close(_deadline(other)).cause == "not_started"


def _copies():
    return set(listen_native._copy_parent().glob(listen_native.HELPER_COPY_PREFIX + "*"))


def _private_copy(helper, build_id):
    folder = helper.parent
    assert folder.parent == listen_native._copy_parent() and folder.name.startswith("pocket-listen-helper-")
    assert [p.name for p in folder.iterdir()] == ["listen-capture"]
    for path in (folder, helper):
        info = os.lstat(path)
        assert info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, path
    assert stat.S_ISDIR(os.lstat(folder).st_mode) and stat.S_ISREG(os.lstat(helper).st_mode)
    assert listen_native.file_sha256(helper) == listen_native.verify_build(build_id)["helper_sha256"]


def test_helper_spawn_runs_a_fresh_private_copy_and_never_inherits_the_lease(harness, monkeypatch):
    calls = []

    def fake_popen(argv, **kwargs):
        _private_copy(Path(argv[0]), build_id)
        calls.append((argv, kwargs))
        raise OSError("spawn captured")

    monkeypatch.setattr(listen_adapters.subprocess, "Popen", fake_popen)
    build_id, build = write_fake_build()
    before = _copies()
    adapter = harness.adapter(build=build_id)
    opened = adapter.open()
    assert opened.refusal == ("helper_exited",)
    (argv, kwargs), = calls
    helper = Path(argv[0])
    assert _copies() - before == {helper.parent} and not helper.is_relative_to(listen_native.registry_dir())
    assert sorted(p.name for p in build.iterdir()) == ["build.json", "build.json.sha256", "capture.swift",
                                                      "listen-capture"]
    assert argv[1:] == ["capture", "--device-uid", UID, "--expected-sample-rate", "48000", "--ring-seconds", "2",
                        "--max-block-frames", "8192"]
    assert kwargs["close_fds"] is True and "pass_fds" not in kwargs and kwargs["cwd"] == helper.parent
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.PIPE
    assert adapter.close(_deadline(adapter)).cause == "not_started" and _copies() == before


def test_a_helper_swapped_after_its_build_verified_is_refused_when_it_is_copied(harness, monkeypatch,
                                                                               tmp_path):
    marker = tmp_path / "helper-ran"
    before = _copies()
    real_verify = listen_native.verify_build

    def verify_then_swap(build_id):
        verified = real_verify(build_id)
        listen_fake_helper.write_launcher(Path(verified["build_dir"]) / listen_native.HELPER_NAME, marker=marker)
        return verified

    monkeypatch.setattr(listen_adapters, "verify_build", verify_then_swap)
    monkeypatch.setattr(listen_native, "verify_build", verify_then_swap)
    monkeypatch.setattr(listen_adapters.subprocess, "Popen", lambda *a, **k: pytest.fail("the helper ran"))
    monkeypatch.setattr(listen_native, "_run_bounded", lambda *a, **k: pytest.fail("the helper ran"))
    adapter = harness.adapter()
    opened = adapter.open()
    assert opened.refusal == ("helper_build_changed",) and adapter.helper_pid is None
    assert "didn't run it" in opened.detail["error"]
    with pytest.raises(PocketError) as raised:
        listen_native.probe_devices(write_fake_build()[0], timeout_s=30.0)
    assert raised.value.code == "evidence_mismatch" and "didn't run it" in str(raised.value)
    assert not marker.exists() and _copies() == before


def test_a_build_swapped_after_its_copy_was_checked_never_changes_what_runs(harness, monkeypatch, tmp_path):
    original, swapped = tmp_path / "original-ran", tmp_path / "swapped-ran"
    builds = []

    def swap_then(run):
        def wrapper(argv, *args, **kwargs):
            listen_fake_helper.write_launcher(builds[-1] / listen_native.HELPER_NAME, marker=swapped)
            return run(argv, *args, **kwargs)
        return wrapper

    monkeypatch.setattr(listen_adapters.subprocess, "Popen", swap_then(subprocess.Popen))
    monkeypatch.setattr(listen_native, "_run_bounded", swap_then(listen_native._run_bounded))
    for caller in ("adapter", "probe"):
        build_id, build = write_fake_build(marker=original)
        builds.append(build)
        recorded, before = listen_native.verify_build(build_id)["helper_sha256"], _copies()
        copy = None
        if caller == "adapter":
            adapter = harness.adapter(build=build_id)
            assert adapter.open().refusal == ()
            (copy,) = _copies() - before
            adapter.close(_deadline(adapter))
        else:
            assert listen_native.probe_devices(build_id, timeout_s=30.0)["status"] == "ok"
        ran = Path(original.read_text().strip())
        assert ran.name == listen_native.HELPER_NAME and ran.parent.parent == listen_native._copy_parent()
        assert ran.parent.name.startswith(listen_native.HELPER_COPY_PREFIX)
        assert copy is None or ran.parent == copy
        assert not swapped.exists()
        assert listen_native.file_sha256(build / listen_native.HELPER_NAME) != recorded
        assert _copies() == before and not ran.exists()
        original.unlink()


def test_the_hash_that_admits_a_copy_is_the_written_copys_own(monkeypatch, tmp_path):
    marker = tmp_path / "helper-ran"
    build_id, _ = write_fake_build(marker=marker)
    read = listen_native._read_regular

    def tamper_with_the_copy(path):
        if Path(path).parent.name.startswith(listen_native.HELPER_COPY_PREFIX):
            Path(path).write_bytes(b"#!/bin/sh\nexit 0\n")
        return read(path)

    monkeypatch.setattr(listen_native, "_read_regular", tamper_with_the_copy)
    before = _copies()
    with pytest.raises(PocketError) as raised:
        listen_native.probe_devices(build_id, timeout_s=30.0)
    assert raised.value.code == "evidence_mismatch"
    assert not marker.exists() and _copies() == before


def test_a_copy_left_by_a_killed_runtime_is_never_used_again(harness, tmp_path):
    left_marker, marker = tmp_path / "leftover-ran", tmp_path / "helper-ran"
    build_id, _ = write_fake_build(marker=marker)
    before = _copies()
    killed = harness.adapter(build=build_id)
    assert killed.open().refusal == ()
    killed._process.kill()
    killed._process.wait(timeout=5)
    (leftover,) = _copies() - before
    assert leftover.name.startswith("pocket-listen-helper-")
    listen_fake_helper.write_launcher(leftover / listen_native.HELPER_NAME, marker=left_marker)
    held = (leftover / listen_native.HELPER_NAME).read_bytes()
    marker.unlink()
    adapter = harness.adapter(build=build_id)
    assert adapter.open().refusal == ()
    (fresh,) = _copies() - before - {leftover}
    assert Path(marker.read_text().strip()) == fresh / listen_native.HELPER_NAME
    adapter.close(_deadline(adapter))
    assert listen_native.probe_devices(build_id, timeout_s=30.0)["status"] == "ok"
    assert not left_marker.exists() and _copies() - before == {leftover}
    assert (leftover / listen_native.HELPER_NAME).read_bytes() == held
    killed.close(_deadline(killed))
    assert _copies() == before


def _script(tmp_path, *segments):
    path = tmp_path / f"script-{time.monotonic_ns()}.json"
    path.write_text(json.dumps({"block_frames": 480, "devices": [{"uid": "synthetic:fake", "name": "Fake",
                                                                  "input_channels": 2, "sample_rate": RATE}],
                                "segments": list(segments)}))
    return str(path)


def _tone(frames):
    return {"kind": "tone", "frames": frames, "tones": [{"channel": 0, "hz": 480.0, "dbfs": -12.0}]}


def test_device_event_ends_capture_and_later_frames_are_ignored(harness, tmp_path):
    adapter = harness.started("--script", _script(tmp_path, _tone(4800),
                                                  {"kind": "rate_change", "sample_rate": 44100}))
    items = _read_until(adapter, lambda item, items: isinstance(item, AdapterEvent))
    assert len(_blocks(items)) == 10
    assert items[-1].type == "sample_rate_changed" and items[-1].detail == {"value": {"sample_rate": 44100}}
    assert [adapter.read(10_000_000) for _ in range(3)] == [None] * 3
    closed = adapter.close(_deadline(adapter))
    assert closed.cause == "clean" and closed.detail["exit_code"] == 4 and closed.detail["exit_reason"] == "device_event"


def test_adapter_blocks_give_the_stream_tracker_exact_helper_gaps_and_overloads(harness, tmp_path):
    adapter = harness.started("--script", _script(
        tmp_path, _tone(960), {"kind": "gap", "frames": 1440, "via": "helper_ring_overflow"}, _tone(960),
        {"kind": "gap", "frames": 480, "via": "processor_overload"}, _tone(960)), "--exit-at-end")
    items = _read_until(adapter, lambda item, items: isinstance(item, AdapterEvent) and item.type == "helper_exited")
    tracker = StreamTracker("rt-native", RATE)
    facts = []
    for item in items:
        if isinstance(item, AdapterBlock):
            facts += tracker.observe_block(item)[1]
        else:
            facts += tracker.observe_event(item)
    missing = [(f.cause, f.interval, f.detail["adapter_dropped_frames"]) for f in facts
               if f.type == "frames_missing"]
    assert missing == [("helper_ring_overflow", (960, 1440), 1440), ("processor_overload", (3360, 480), 0)]
    assert [f.detail["reported_by"] for f in facts if f.type == "processor_overload_reported"] == ["hal"]
    assert items[-1].detail["exit_reason"] == "script_complete" and facts[-1].type == "capture_failed"


def test_a_helper_block_with_its_sample_time_flag_clear_ends_capture(harness, tmp_path):
    adapter = harness.started("--script", _script(
        tmp_path, _tone(960), {"kind": "clocks", "sample_time_valid": False, "host_time_valid": True},
        _tone(960)), "--exit-at-end")
    items = _read_until(adapter, lambda item, items: isinstance(item, AdapterEvent)
                        and item.type == "helper_exited")
    blocks = _blocks(items)
    assert [b.device_sample_time for b in blocks] == [0.0, 480.0, None, None]
    tracker = StreamTracker("rt-native", RATE)
    facts = [f for b in blocks for f in tracker.observe_block(b)[1]]
    assert [(f.type, f.cause) for f in facts] == [("stream_started", "capture_opened"),
                                                  ("stream_ended", "device_sample_time_unavailable"),
                                                  ("capture_ended", "device_sample_time_unavailable")]
    assert facts[1].detail["sample_time"] == "missing" and facts[1].detail["end_frame"] == 960
    assert tracker.snapshot()["frames_received"] == 960 and tracker.snapshot()["after_end"]["blocks"] == 1


def test_coreaudio_adapter_runs_through_listen_core_and_stops_without_a_capture_failure(harness):
    clock = SystemClock()
    adapter = harness.adapter(clock=clock)
    opened = adapter.open()
    config = {"device": {"uid": UID, "expected_sample_rate": RATE},
              "routes": [{"route_id": "master", "role": "master", "channels": [0, 1]}],
              "measurement": {"clip_threshold_dbfs": -0.1, "window_frames": 4800,
                              "near_silence": {"level_dbfs": -60.0, "min_duration_ms": 2000}},
              "buffering": {"ring_seconds": 2}, "health": {"stale_after_ms": 500},
              "delivery": {"meter_coalesce_windows": 1, "normal_retention_events": 8192,
                           "normal_retention_bytes": 4194304, "health_retention_events": 1024,
                           "health_retention_bytes": 524288},
              "limits": {"max_runtime_seconds": 600}}
    assert resolve_routes(config["routes"], opened.format.device_input_channels)[1] == ()
    published = []
    core = ListenCore(config, opened, session_id="session-native", runtime_epoch="rt-0123456789abcdef",
                      audio_source="device_input", adapter_profile=adapter.profile, config_sha256="0" * 64,
                      clock=clock, adapter_stats=adapter.stats, on_publish=published.append)
    assert adapter.start(core.selected_channels).started
    core.begin()
    deadline = time.monotonic() + 15
    while core.tracker.snapshot()["frames_received"] < RATE // 2 and time.monotonic() < deadline:
        core.ingest(adapter.read(50_000_000))
        core.measure_pending()
        core.watchdog_tick()
    observation = core.observe()["observation"]
    capture = observation["capture"]
    assert capture["state"] == "running" and capture["clocks"]["device_sample_time"] == "available"
    assert capture["clocks"]["host_time_scope"] == "mach_absolute_time_ns"
    assert capture["clocks"]["domains_compared"] is False and capture["device"]["device_instance"] == "audioobject-4242"
    assert capture["adapter_stats"]["callback_ns"]["scope"] == "this_runtime_measured"
    levels = [e["evidence"] for e in published if e["evidence"]["type"] == "signal_level"]
    assert levels and all(abs(peak + 12.0) < 0.05 for e in levels for peak in e["sample_peak_dbfs"])
    assert core.begin_stop("listen_stop", request_id="stop-1")
    closed = adapter.close(clock.monotonic_ns() + 3 * NS)
    summary = core.finish_stop(drain_ns=500_000_000)
    assert closed.cause == "clean" and closed.detail["exit_reason"] == "quit"
    assert summary["state"] == "stopped" and summary["capture"]["callback_ns"]["samples"] > 0
    types = [e["evidence"]["type"] for e in published]
    assert "capture_failed" not in types and types[-1] == "runtime_stopped"


def test_heartbeats_update_the_stats_the_watchdog_reads(harness):
    clock = FakeClock()
    adapter = harness.started(clock=clock)
    clock.advance(NS)
    deadline = time.monotonic() + 30
    while adapter.stats()["heartbeats"] < 2 and time.monotonic() < deadline:
        adapter.read(50_000_000)
    stats = adapter.stats()
    assert stats["heartbeats"] >= 2 and stats["last_heartbeat_monotonic_ns"] == clock.monotonic_ns()
    assert stats["callback_max_ns_since_heartbeat"] == 5000


def _fake_toolchain(monkeypatch, calls, *, returncode=0, stderr=""):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(listen_native.platform, "mac_ver", lambda: ("15.4.1", ("", "", ""), "arm64"))
    monkeypatch.setattr(listen_native.shutil, "which", lambda name: "/tools/" + name)

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        assert argv[0] == "swiftc"
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, "Apple Swift version 6.3.3 (test)\nTarget: arm64\n", "")
        if returncode == 0:
            Path(argv[argv.index("-o") + 1]).write_bytes(b"\xcf\xfa\xed\xfe fake helper binary")
        return subprocess.CompletedProcess(argv, returncode, "", stderr)

    def no_popen(*args, **kwargs):
        raise AssertionError("listen_build_helper never runs the helper")

    monkeypatch.setattr(listen_native.subprocess, "run", fake_run)
    monkeypatch.setattr(listen_native.subprocess, "Popen", no_popen)


def test_build_helper_runs_only_swiftc_and_seals_the_build_in_listens_own_folder(monkeypatch):
    calls = []
    _fake_toolchain(monkeypatch, calls, stderr="")
    built = listen_native.build_helper()
    registry = listen_native.registry_dir()
    build_id, build = built["build_id"], Path(built["build_dir"])
    assert listen_native.BUILD_ID.fullmatch(build_id) and build == registry / "builds" / build_id
    compile_argv = calls[0][0]
    assert compile_argv[:5] == ["swiftc", "-O", "-enforce-exclusivity=unchecked", "-swift-version", "5"]
    assert compile_argv[5] == "-module-cache-path" and Path(compile_argv[6]).parent == build
    assert compile_argv[7:] == [str(build / "capture.swift"), "-o", str(build / "listen-capture")]
    assert calls[0][1]["timeout"] == listen_native.BUILD_TIMEOUT_S == 600
    assert calls[1][0] == ["swiftc", "--version"] and len(calls) == 2
    record = built["build"]
    assert record == json.loads((build / "build.json").read_text())
    assert record["schema"] == "pocket.listen-build/v1" and record["profile"] == "coreaudio-hal-input/v1"
    assert record["swiftc_version"] == "Apple Swift version 6.3.3 (test)" and record["wire"] == "PLH1"
    assert record["native_acceptance"] == "separate_from_build" and record["swift_language_mode"] == "5"
    assert record["files"]["capture.swift"] == listen_native.file_sha256(SWIFT)
    verified = listen_native.verify_build(build_id)
    assert verified["build_id"] == build_id and verified["build_dir"] == str(build)
    assert verified["build_json_sha256"] == built["build_json_sha256"]
    assert verified["helper_sha256"] == record["files"]["listen-capture"]
    registration = json.loads(listen_native._registration_path(registry / "builds", build_id).read_text())
    assert registration["build_id"] == build_id and "build_dir" not in registration
    assert registration["source_sha256"] == listen_native.file_sha256(SWIFT)
    assert registration["build_json_sha256"] == built["build_json_sha256"]
    key = (registry / listen_native.REGISTRY_KEY_NAME).read_bytes()
    assert key.hex() not in json.dumps(built) + json.dumps(verified) + json.dumps(registration)
    again = listen_native.build_helper()
    assert again["build_id"] != build_id and Path(again["build_dir"]).parent == build.parent
    assert sorted(p.name for p in build.parent.iterdir()) == sorted(
        [build_id, f"{build_id}.json", again["build_id"], f"{again['build_id']}.json"])


def test_the_xcode_select_shim_counts_as_swiftc_only_with_a_toolchain_behind_it(monkeypatch, tmp_path):
    monkeypatch.setattr(listen_native.shutil, "which", lambda name: listen_native.SWIFTC_SHIM)
    monkeypatch.delenv("DEVELOPER_DIR", raising=False)
    monkeypatch.setattr(listen_native, "XCODE_SELECT_LINKS", (tmp_path / "no-link",))
    xcode, tools = tmp_path / "Xcode", tmp_path / "CommandLineTools"
    monkeypatch.setattr(listen_native, "DEFAULT_DEVELOPER_DIRS", (xcode, tools))
    assert listen_native.swift_compiler() is None
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(listen_native.platform, "mac_ver", lambda: ("15.4", ("", "", ""), ""))
    monkeypatch.setattr(listen_native.subprocess, "run", lambda *a, **k: pytest.fail("nothing may run"))
    with pytest.raises(PocketError) as raised:
        listen_native.build_helper()
    assert "Swift compiler" in raised.value.hint
    (tools / "usr" / "bin").mkdir(parents=True)
    (tools / "usr" / "bin" / "swiftc").write_text("")
    assert listen_native.swift_compiler() == listen_native.SWIFTC_SHIM
    monkeypatch.setenv("DEVELOPER_DIR", str(xcode))
    assert listen_native.swift_compiler() is None
    monkeypatch.setattr(listen_native.shutil, "which", lambda name: "/opt/swift/bin/swiftc")
    assert listen_native.swift_compiler() == "/opt/swift/bin/swiftc"


@pytest.mark.parametrize("selection", ["var_select", "xcode_select_link", "developer_dir_app"])
def test_the_xcode_select_shim_finds_a_toolchain_where_the_shim_would(monkeypatch, tmp_path, selection):
    monkeypatch.setattr(listen_native.shutil, "which", lambda name: listen_native.SWIFTC_SHIM)
    monkeypatch.delenv("DEVELOPER_DIR", raising=False)
    default = tmp_path / "Xcode.app" / "Contents" / "Developer"
    monkeypatch.setattr(listen_native, "DEFAULT_DEVELOPER_DIRS", (default,))
    xcode = tmp_path / "Xcode-16.4.app"
    developer = xcode / "Contents" / "Developer"
    swiftc = developer / "Toolchains" / "XcodeDefault.xctoolchain" / "usr" / "bin" / "swiftc"
    swiftc.parent.mkdir(parents=True)
    swiftc.write_text("")
    links = (tmp_path / "var-select-developer_dir", tmp_path / "xcode_select_link")
    monkeypatch.setattr(listen_native, "XCODE_SELECT_LINKS", links)
    if selection == "developer_dir_app":
        monkeypatch.setenv("DEVELOPER_DIR", str(xcode))
    else:
        links[selection == "xcode_select_link"].symlink_to(developer)
    assert listen_native.swift_compiler() == listen_native.SWIFTC_SHIM
    if selection == "var_select":
        links[1].symlink_to(default)
        assert listen_native.swift_compiler() == listen_native.SWIFTC_SHIM
        links[0].unlink()
        assert listen_native.swift_compiler() is None


@pytest.mark.parametrize("platform_name, mac, swiftc, hint", [
    ("linux", "", True, "Swift compiler"),
    ("darwin", "14.6", True, "macOS 15 or later"),
    ("darwin", "15.0", False, "Swift compiler"),
])
def test_build_helper_refuses_outside_its_profile(monkeypatch, platform_name, mac, swiftc, hint):
    monkeypatch.setattr(sys, "platform", platform_name)
    monkeypatch.setattr(listen_native.platform, "mac_ver", lambda: (mac, ("", "", ""), ""))
    monkeypatch.setattr(listen_native.shutil, "which", lambda name: "/tools/swiftc" if swiftc else None)
    monkeypatch.setattr(listen_native.subprocess, "run", lambda *a, **k: pytest.fail("nothing may run"))
    with pytest.raises(PocketError) as raised:
        listen_native.build_helper()
    assert raised.value.code == "unsupported_profile" and hint in raised.value.hint
    assert not listen_native.registry_dir().exists()


def test_a_failed_compile_is_reported_and_leaves_no_build_behind(monkeypatch):
    calls = []
    _fake_toolchain(monkeypatch, calls, returncode=1, stderr="capture.swift:1: error: nope")
    with pytest.raises(PocketError) as raised:
        listen_native.build_helper()
    assert raised.value.code == "unsupported_profile" and "error: nope" in str(raised.value)
    assert "leaves nothing behind" in raised.value.hint
    assert len(calls) == 1 and list((listen_native.registry_dir() / "builds").iterdir()) == []


def _tamper(build, how):
    if how == "binary":
        (build / "listen-capture").write_text((build / "listen-capture").read_text() + "#\n")
    elif how == "source":
        (build / "capture.swift").write_text("// changed\n")
    elif how == "record":
        record = json.loads((build / "build.json").read_text())
        (build / "build.json").write_text(json.dumps({**record, "wire": "PLH2"}))
    elif how == "seal":
        (build / "build.json.sha256").write_text("0" * 64 + "\n")
    elif how == "symlink":
        target = build.parent / "elsewhere"
        shutil.copy2(build / "listen-capture", target)
        (build / "listen-capture").unlink()
        (build / "listen-capture").symlink_to(target)
    elif how == "missing":
        (build / "listen-capture").unlink()


@pytest.mark.parametrize("how", ["binary", "source", "record", "seal", "symlink", "missing"])
def test_verify_build_detects_any_change_without_running_anything(monkeypatch, how):
    build_id, build = write_fake_build()
    assert listen_native.verify_build(build_id)["build_dir"] == str(build)
    _tamper(build, how)
    monkeypatch.setattr(listen_native.subprocess, "Popen", lambda *a, **k: pytest.fail("nothing may run"))
    with pytest.raises(PocketError) as raised:
        listen_native.verify_build(build_id)
    assert raised.value.code == "evidence_mismatch" and "listen_build_helper" in raised.value.hint


def _self_sealed(build):
    data = (build / "build.json").read_bytes()
    return (hashlib.sha256(data).hexdigest() == (build / "build.json.sha256").read_text().strip()
            and all(listen_native.file_sha256(build / name) == sha
                    for name, sha in json.loads(data)["files"].items()))


def _reseal(build):
    (build / "build.json").unlink()
    (build / "build.json.sha256").unlink()
    listen_native._seal_build(build, listen_fake_helper.FIXTURE_VERSION)


def _refused(build_id):
    with pytest.raises(PocketError) as raised:
        listen_native.verify_build(build_id)
    assert raised.value.code == "evidence_mismatch" and "listen_build_helper" in raised.value.hint
    return raised.value


def _never_runs(monkeypatch, build_id, marker):
    _refused(build_id)
    with monkeypatch.context() as patch:
        patch.setattr(listen_adapters.subprocess, "Popen", lambda *a, **k: pytest.fail("the helper ran"))
        patch.setattr(listen_native, "_run_bounded", lambda *a, **k: pytest.fail("the helper ran"))
        opened = CoreAudioHelperAdapter(build_id, UID, RATE, FakeClock()).open()
        assert opened.refusal == ("helper_build_changed",)
        with pytest.raises(PocketError) as raised:
            listen_native.probe_devices(build_id, timeout_s=30.0)
    assert raised.value.code == "evidence_mismatch"
    assert not marker.exists()


def test_a_build_id_listen_never_recorded_is_refused_and_a_malformed_one_never_names_a_path(monkeypatch,
                                                                                            tmp_path):
    marker = tmp_path / "helper-ran"
    write_fake_build()
    assert "has no helper build" in str(_refused(UNRECORDED_BUILD_ID))
    _never_runs(monkeypatch, UNRECORDED_BUILD_ID, marker)
    for bad in ("", "../builds", "/tmp/build", "20200101T000000Z-0123abcd/..", "builds", 7, None):
        assert "must be one listen_build_helper returned" in str(_refused(bad))


@pytest.mark.parametrize("how", ["never_recorded", "copied", "copied_with_its_record", "moved"])
def test_a_build_listen_didnt_record_under_its_id_is_refused_and_never_runs(monkeypatch, tmp_path, how):
    marker = tmp_path / "helper-ran"
    if how == "never_recorded":
        build_id, build = write_fake_build(register=False, marker=marker)
    else:
        original_id, original = write_fake_build(marker=marker)
        assert listen_native.verify_build(original_id)["build_dir"] == str(original)
        build_id, build = UNRECORDED_BUILD_ID, original.parent / UNRECORDED_BUILD_ID
        if how == "moved":
            os.rename(original, build)
            assert "is missing" in str(_refused(original_id))
        else:
            shutil.copytree(original, build)
        if how == "copied_with_its_record":
            shutil.copy2(listen_native._registration_path(original.parent, original_id),
                         listen_native._registration_path(build.parent, build_id))
    assert _self_sealed(build)
    message = str(_refused(build_id))
    assert ("doesn't verify" if how == "copied_with_its_record" else "has no helper build") in message
    _never_runs(monkeypatch, build_id, marker)


def test_a_recorded_build_whose_helper_is_swapped_and_resealed_is_refused(monkeypatch, tmp_path):
    build_id, build = write_fake_build()
    recorded = listen_native.verify_build(build_id)["helper_sha256"]
    marker = tmp_path / "helper-ran"
    listen_fake_helper.write_launcher(build / listen_native.HELPER_NAME, marker=marker)
    _reseal(build)
    assert _self_sealed(build) and listen_native.file_sha256(build / listen_native.HELPER_NAME) != recorded
    assert "changed since listen_build_helper recorded it" in str(_refused(build_id))
    _never_runs(monkeypatch, build_id, marker)


@pytest.mark.parametrize("target", ["registry", "builds", "build"])
@pytest.mark.parametrize("mode", [0o750, 0o705])
def test_a_build_whose_folders_others_can_open_is_refused(monkeypatch, tmp_path, target, mode):
    marker = tmp_path / "helper-ran"
    build_id, build = write_fake_build(marker=marker)
    path = {"registry": listen_native.registry_dir(), "builds": build.parent, "build": build}[target]
    os.chmod(path, mode)
    try:
        assert "isn't a directory private to you" in str(_refused(build_id))
        _never_runs(monkeypatch, build_id, marker)
    finally:
        os.chmod(path, 0o700)
    assert listen_native.verify_build(build_id)["build_dir"] == str(build)


def test_a_symlink_in_place_of_a_build_or_the_builds_folder_is_refused_even_to_the_real_files(monkeypatch,
                                                                                             tmp_path):
    marker = tmp_path / "helper-ran"
    build_id, build = write_fake_build(marker=marker)
    builds = build.parent
    for place in (build, builds):
        moved = tmp_path / f"moved-{place.name}"
        os.rename(place, moved)
        place.symlink_to(moved, target_is_directory=True)
        try:
            assert "isn't a directory private to you" in str(_refused(build_id))
            _never_runs(monkeypatch, build_id, marker)
        finally:
            place.unlink()
            os.rename(moved, place)
    assert listen_native.probe_devices(build_id, timeout_s=30.0)["status"] == "ok" and marker.exists()


def _forge(build_id, build, mac):
    record = json.loads((build / "build.json").read_bytes())
    entry = {"schema": listen_native.REGISTRATION_SCHEMA, "build_id": build_id,
             "build_json_sha256": (build / "build.json.sha256").read_text().strip(),
             "helper_sha256": record["files"][listen_native.HELPER_NAME],
             "source_sha256": record["files"][listen_native.SOURCE_NAME]}
    path = listen_native._registration_path(build.parent, build_id)
    path.write_text(json.dumps(entry if mac is None else {**entry, "mac": mac}))
    os.chmod(path, 0o600)
    return entry, path


@pytest.mark.parametrize("mac", ["missing", "zeros", "other_key"])
def test_a_forged_registration_without_the_keys_hmac_is_refused(monkeypatch, tmp_path, mac):
    marker = tmp_path / "helper-ran"
    build_id, build = write_fake_build(register=False, marker=marker)
    entry, path = _forge(build_id, build, None)
    other = hmac.new(os.urandom(32), json.dumps(entry, sort_keys=True, separators=(",", ":")).encode(),
                     hashlib.sha256).hexdigest()
    _forge(build_id, build, {"missing": None, "zeros": "0" * 64, "other_key": other}[mac])
    assert "doesn't verify" in str(_refused(build_id))
    os.chmod(path, 0o644)
    assert "isn't private to you" in str(_refused(build_id))
    _never_runs(monkeypatch, build_id, marker)


def test_an_edited_registration_and_a_registry_others_can_read_are_refused(monkeypatch, tmp_path):
    build_id, build = write_fake_build()
    registry = listen_native.registry_dir()
    path = listen_native._registration_path(build.parent, build_id)
    registration = json.loads(path.read_text())
    marker = tmp_path / "helper-ran"
    listen_fake_helper.write_launcher(build / listen_native.HELPER_NAME, marker=marker)
    _reseal(build)
    record = json.loads((build / "build.json").read_bytes())
    path.write_text(json.dumps({**registration, "helper_sha256": record["files"][listen_native.HELPER_NAME],
                                "build_json_sha256": (build / "build.json.sha256").read_text().strip()}))
    assert "doesn't verify" in str(_refused(build_id))
    _never_runs(monkeypatch, build_id, marker)
    recorded, _ = write_fake_build()
    listen_native.verify_build(recorded)
    key = registry / listen_native.REGISTRY_KEY_NAME
    os.chmod(key, 0o640)
    with pytest.raises(PocketError) as raised:
        listen_native.verify_build(recorded)
    assert raised.value.code == "evidence_mismatch" and "POCKET_LISTEN_DIR" in raised.value.hint
    os.chmod(key, 0o600)
    assert listen_native.verify_build(recorded)["build_id"] == recorded


def test_the_registry_is_private_and_lives_in_pockets_state_folder(monkeypatch, tmp_path):
    build_id, build = write_fake_build()
    registry = listen_native.registry_dir()
    key = registry / listen_native.REGISTRY_KEY_NAME
    path = listen_native._registration_path(build.parent, build_id)
    for folder in (registry, build.parent, build):
        assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700, folder
    assert build.parent == registry / "builds" and path.parent == build.parent
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600 and len(key.read_bytes()) == 32
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    registration = json.loads(path.read_text())
    verified = listen_native.verify_build(build_id)
    fields = ("build_id", "build_json_sha256", "helper_sha256", "source_sha256")
    assert {k: registration[k] for k in fields} == {k: verified[k] for k in fields}
    assert key.read_bytes().hex() not in path.read_text() + json.dumps(verified)
    monkeypatch.delenv("POCKET_LISTEN_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert listen_native.registry_dir() == (tmp_path / "home" / ".pocket" / "listen").resolve()
    _refused(build_id)
    assert not (tmp_path / "home").exists()


def test_build_helper_refuses_a_registry_others_can_reach_before_compiling(tmp_path, monkeypatch):
    calls = []
    _fake_toolchain(monkeypatch, calls)
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    os.chmod(shared, 0o755)
    monkeypatch.setenv("POCKET_LISTEN_DIR", str(shared))
    with pytest.raises(PocketError) as raised:
        listen_native.build_helper()
    assert raised.value.code == "io_error" and "POCKET_LISTEN_DIR" in raised.value.hint
    assert calls == [] and list(shared.iterdir()) == []


def test_build_helper_refuses_a_registry_key_others_can_read_as_io_error_before_compiling(tmp_path, monkeypatch):
    calls = []
    _fake_toolchain(monkeypatch, calls)
    registry = tmp_path / "registry"
    registry.mkdir(mode=0o700)
    key = registry / listen_native.REGISTRY_KEY_NAME
    key.write_bytes(os.urandom(listen_native.REGISTRY_KEY_BYTES))
    os.chmod(key, 0o640)
    monkeypatch.setenv("POCKET_LISTEN_DIR", str(registry))
    with pytest.raises(PocketError) as raised:
        listen_native.build_helper()
    assert raised.value.code == "io_error" and "POCKET_LISTEN_DIR" in raised.value.hint
    assert calls == [] and not (registry / "builds").exists()


def test_the_build_folder_is_private_for_the_whole_compile_whatever_the_umask(monkeypatch):
    calls, seen = [], []
    _fake_toolchain(monkeypatch, calls)
    fake_run = listen_native.subprocess.run

    def watching(argv, **kwargs):
        if argv[1:] != ["--version"]:
            output = Path(argv[argv.index("-o") + 1]).parent
            seen.append([stat.S_IMODE(os.stat(p).st_mode) for p in (output, output.parent, output.parent.parent)])
        return fake_run(argv, **kwargs)

    monkeypatch.setattr(listen_native.subprocess, "run", watching)
    previous = os.umask(0o002)
    try:
        built = listen_native.build_helper()
    finally:
        os.umask(previous)
    assert seen == [[0o700, 0o700, 0o700]]
    assert listen_native.verify_build(built["build_id"])["source_sha256"] == listen_native.file_sha256(SWIFT)


def test_a_build_whose_source_changed_while_it_compiled_is_never_recorded(monkeypatch):
    calls = []
    _fake_toolchain(monkeypatch, calls)
    fake_run = listen_native.subprocess.run

    def swapping(argv, **kwargs):
        if argv[1:] != ["--version"]:
            Path(argv[-3]).write_text("// not the packaged helper\n")
        return fake_run(argv, **kwargs)

    monkeypatch.setattr(listen_native.subprocess, "run", swapping)
    with pytest.raises(PocketError) as raised:
        listen_native.build_helper()
    assert raised.value.code == "evidence_mismatch" and "changed while it was compiled" in str(raised.value)
    assert list((listen_native.registry_dir() / "builds").iterdir()) == []


INHERITABLE = "user:nobody allow list,search,read,add_file,delete_child,write,file_inherit,directory_inherit"
_unpatched_subprocess_run = subprocess.run
darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="extended access lists are a macOS check")


def _grant(path, entry=INHERITABLE):
    _unpatched_subprocess_run(["chmod", "+a", entry, str(path)], check=True, timeout=30)


@darwin_only
def test_listens_folder_created_under_an_inheritable_access_list_keeps_none_of_it(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o700)
    _grant(shared)
    (shared / "plain").mkdir(mode=0o700)
    assert listen_native.acl_grants_access(shared / "plain")
    calls = []
    _fake_toolchain(monkeypatch, calls)
    monkeypatch.setenv("POCKET_LISTEN_DIR", str(shared / "registry"))
    built = listen_native.build_helper()
    registry, build = shared / "registry", Path(built["build_dir"])
    created = (registry, registry / listen_native.REGISTRY_KEY_NAME, build.parent, build,
               listen_native._registration_path(build.parent, built["build_id"]),
               *(build / name for name in ("build.json", "build.json.sha256", "capture.swift", "listen-capture")))
    assert [path for path in created if listen_native.acl_grants_access(path)] == []
    assert listen_native.verify_build(built["build_id"])["build_dir"] == str(build)


@darwin_only
def test_an_access_list_that_lets_others_in_is_refused_wherever_listen_checks(monkeypatch):
    build_id, build = write_fake_build()
    registry = listen_native.registry_dir()
    checked = {registry: "isn't a directory private to you",
               registry / listen_native.REGISTRY_KEY_NAME: "isn't private to you",
               build.parent: "isn't a directory private to you",
               listen_native._registration_path(build.parent, build_id): "isn't private to you",
               build: "isn't a directory private to you"}
    for path, message in checked.items():
        _grant(path, "user:nobody allow read")
        assert message in str(_refused(build_id)), path
        listen_native.clear_acl(path)
        assert listen_native.verify_build(build_id)["build_dir"] == str(build)
    _grant(build, "user:nobody deny write")
    assert listen_native.verify_build(build_id)["build_dir"] == str(build)
    calls = []
    _grant(registry, "user:nobody allow read")
    _fake_toolchain(monkeypatch, calls)
    with pytest.raises(PocketError) as raised:
        listen_native.build_helper()
    assert raised.value.code == "io_error" and "chmod -R -N" in raised.value.hint
    assert calls == [] and sorted(p.name for p in build.parent.iterdir()) == [build_id, f"{build_id}.json"]


MNT_ROOTFS = 0x00004000
volume_check = pytest.mark.skipif(sys.platform != "darwin",
                                  reason="volumes that ignore ownership are a macOS check")


def _ignoring_ownership(monkeypatch, target):
    real, info = listen_native._volume_flags, os.lstat(target)

    def flags(where):
        seen = os.fstat(where) if isinstance(where, int) else os.stat(where)
        same = (seen.st_dev, seen.st_ino) == (info.st_dev, info.st_ino)
        return listen_native._MNT_IGNORE_OWNERSHIP if same else real(where)
    monkeypatch.setattr(listen_native, "_volume_flags", flags)


@volume_check
def test_the_volume_check_reads_real_mount_flags(tmp_path):
    flags = listen_native._volume_flags("/")
    assert flags is not None and flags & MNT_ROOTFS
    assert listen_native.volume_ignores_ownership(tmp_path) is False
    with open(tmp_path / "file", "wb") as stream:
        assert listen_native._volume_flags(stream.fileno()) == listen_native._volume_flags(tmp_path)
    assert listen_native._volume_flags(tmp_path / "missing") is None
    assert listen_native.volume_ignores_ownership(tmp_path / "missing") is True


@volume_check
def test_a_volume_that_ignores_ownership_is_refused_wherever_listen_checks(monkeypatch, tmp_path):
    marker = tmp_path / "helper-ran"
    build_id, build = write_fake_build(marker=marker)
    registry = listen_native.registry_dir()
    checked = {registry: "isn't a directory private to you",
               registry / listen_native.REGISTRY_KEY_NAME: "isn't private to you",
               build.parent: "isn't a directory private to you",
               listen_native._registration_path(build.parent, build_id): "isn't private to you",
               build: "isn't a directory private to you",
               build / "build.json": "Can't read the sealed",
               build / "build.json.sha256": "Can't read the sealed",
               build / "capture.swift": "on a volume that ignores ownership",
               build / "listen-capture": "on a volume that ignores ownership"}
    for path, message in checked.items():
        with monkeypatch.context() as patch:
            _ignoring_ownership(patch, path)
            assert message in str(_refused(build_id)), path
            _never_runs(patch, build_id, marker)
        assert listen_native.verify_build(build_id)["build_dir"] == str(build)
    calls = []
    _ignoring_ownership(monkeypatch, registry)
    _fake_toolchain(monkeypatch, calls)
    with pytest.raises(PocketError) as raised:
        listen_native.build_helper()
    assert raised.value.code == "io_error" and "on a volume that enforces ownership" in raised.value.hint
    assert calls == [] and sorted(p.name for p in build.parent.iterdir()) == [build_id, f"{build_id}.json"]


@pytest.fixture(scope="module")
def module_registry():
    return listen_native.registry_dir()


def test_module_scoped_fixtures_never_see_the_real_build_registry(module_registry, listen_registry):
    real = (Path.home() / ".pocket" / "listen").resolve()
    assert module_registry != real and os.environ.get("POCKET_LISTEN_DIR")
    assert listen_native.registry_dir() == Path(listen_registry).resolve() != module_registry


def test_synthetic_device_rows_have_the_coreaudio_virtual_format_shape():
    script = {"block_frames": 480, "segments": [],
              "devices": [{"uid": "synthetic:duo", "name": "Duo", "input_channels": 4, "sample_rate": 48000}]}
    (row,) = listen_adapters.synthetic_inventory(script)
    (stream,) = row["input_streams"]
    native = listen_native._stream_row({"starting_channel": 0, "channels": 4, "virtual_format": {}})
    assert set(stream["virtual_format"]) == set(native["virtual_format"])
    assert stream["virtual_format"]["channels_per_frame"] == 4
    assert listen_native.is_float32(stream["virtual_format"], 48000)


def test_a_device_with_one_non_float_stream_fits_on_its_float_stream_channels():
    device = listen_fake_helper._device("fake:mixed", "Mixed", 4, 48000)
    float_stream = listen_fake_helper._stream(2, 48000)
    int_stream = listen_fake_helper._stream(2, 48000, float32=False)
    device.update(input_streams=[float_stream, {**int_stream, "starting_channel": 2}],
                  stream_configuration=[2, 2], channel_names=[""] * 4)
    row = listen_native._device_row(device)
    assert row["profile_fit"] == "partial:not_float32_streams" and row["supported_channels"] == [0, 1]
    device.update(input_streams=[{**int_stream, "channels": 4}], stream_configuration=[4])
    row = listen_native._device_row(device)
    assert row["profile_fit"] == "unsupported:not_float32" and row["supported_channels"] == []
    short = {**int_stream, "virtual_format": {**int_stream["virtual_format"], "bits_per_channel": 16}}
    device.update(input_streams=[float_stream, {**short, "starting_channel": 2}], stream_configuration=[2, 2])
    row = listen_native._device_row(device)
    assert row["profile_fit"] == "unsupported:not_float32" and row["supported_channels"] == []


def test_a_start_is_refused_when_another_stream_of_the_input_isnt_four_bytes_a_sample(harness):
    adapter = harness.adapter()
    assert adapter.open().refusal == ()
    float_stream = listen_fake_helper._stream(2, 48000)
    short = listen_fake_helper._stream(2, 48000, float32=False)
    short["virtual_format"]["bits_per_channel"] = 16
    adapter._hello["device"]["input_streams"] = [float_stream, {**short, "starting_channel": 2}]
    refused = adapter.start((0, 1))
    assert not refused.started and refused.refusal == ("format_not_float32",)
    assert "fake helper received" not in (adapter.stderr_tail() or "")


def test_probe_devices_validates_rows_flags_shared_names_and_profile_fit():
    build_id, _ = write_fake_build()
    probe = listen_native.probe_devices(build_id)
    assert probe["status"] == "ok" and probe["microphone_authorization"] == "authorized"
    rows = {row["uid"]: row for row in probe["devices"]}
    assert {uid: row["profile_fit"] for uid, row in rows.items()} == {
        "fake:duo-a": "supported", "fake:duo-b": "supported", "fake:int": "unsupported:not_float32",
        "fake:hogged": "unsupported:hogged_by_other_process", "fake:odd-rate": "unsupported:rate_not_in_profile",
        "fake:gone": "unsupported:not_alive", "fake:hog-unread": "unsupported:hog_mode_unreadable"}
    assert rows["fake:hog-unread"]["hog_mode_pid"] is None
    assert rows["fake:duo-a"]["display_name_shared"] and rows["fake:duo-a"]["name_shared_with"] == 1
    assert not rows["fake:int"]["display_name_shared"] and rows["fake:duo-a"]["audio_object_id"] == 41
    assert probe["build"]["build_id"] == build_id


def test_the_probe_runs_a_fresh_private_copy_and_removes_it_when_the_listing_ends(monkeypatch):
    build_id, build = write_fake_build()
    runs, real = [], listen_native._run_bounded

    def checked(argv, cwd, timeout_s):
        _private_copy(Path(argv[0]), build_id)
        runs.append((argv, cwd))
        return real(argv, cwd, timeout_s)

    monkeypatch.setattr(listen_native, "_run_bounded", checked)
    before = _copies()
    for _ in range(2):
        assert listen_native.probe_devices(build_id, timeout_s=30.0)["status"] == "ok"
        assert _copies() == before
    (first, cwd), (second, _) = runs
    assert first[1:] == ["list-devices"] and cwd == Path(first[0]).parent
    assert Path(first[0]).parent != Path(second[0]).parent
    assert sorted(p.name for p in build.iterdir()) == ["build.json", "build.json.sha256", "capture.swift",
                                                      "listen-capture"]


@pytest.mark.parametrize("parent", ["yours_and_sticky", "roots_not_sticky"])
def test_no_copy_is_made_where_another_account_could_replace_it(monkeypatch, tmp_path, parent):
    build_id, _ = write_fake_build()
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o1777)
    folder = {"yours_and_sticky": shared, "roots_not_sticky": Path("/")}[parent]
    assert os.stat(folder).st_uid != 0 or not os.stat(folder).st_mode & stat.S_ISVTX
    monkeypatch.setattr(listen_native, "_copy_parent", lambda: folder)
    monkeypatch.setattr(listen_adapters.subprocess, "Popen", lambda *a, **k: pytest.fail("the helper ran"))
    with pytest.raises(PocketError) as raised:
        listen_native.probe_devices(build_id, timeout_s=30.0)
    assert raised.value.code == "io_error" and "sticky bit" in str(raised.value)
    assert "/private/tmp" in raised.value.hint and valid_hint(raised.value.hint)
    adapter = CoreAudioHelperAdapter(build_id, UID, RATE, FakeClock())
    with pytest.raises(PocketError) as raised:
        adapter.open()
    assert raised.value.code == "io_error" and adapter.helper_pid is None
    assert list(shared.iterdir()) == []


@pytest.mark.parametrize("fake_args, reason", [
    (("--list-exit-code", "5"), "nonzero_exit"),
    (("--list-output", "{garbage"), "invalid_output"),
    (("--list-output", "nan"), "invalid_output"),
    (("--list-output", "big"), "output_too_large"),
    (("--list-sleep-ms", "5000"), "timeout"),
])
def test_probe_failures_are_outcomes_not_errors(tmp_path, fake_args, reason):
    contents = {"{garbage": b"{garbage", "big": b" " * (listen_native.PROBE_STDOUT_BYTES + 10),
                "nan": json.dumps({"schema": listen_native.DEVICES_SCHEMA, "microphone_authorization": "authorized",
                                   "devices": [], "x": float("nan")}).encode()}
    if fake_args[0] == "--list-output":
        path = tmp_path / "output.json"
        path.write_bytes(contents[fake_args[1]])
        fake_args = ("--list-output", str(path))
    build_id, _ = write_fake_build(*fake_args)
    timeout_s = 1.0 if reason == "timeout" else LOADED_PROBE_ALLOWANCE_S
    begun = time.monotonic()
    probe = listen_native.probe_devices(build_id, timeout_s=timeout_s)
    assert probe["status"] == "failed" and probe["probe"]["reason"] == reason
    assert reason != "timeout" or time.monotonic() - begun < PROBE_HANG_GUARD_S


@pytest.mark.parametrize("document", [
    {"schema": "pocket.listen-helper-hello/v1"},
    {"schema": listen_native.DEVICES_SCHEMA, "microphone_authorization": "granted", "devices": []},
    {"schema": listen_native.DEVICES_SCHEMA, "microphone_authorization": "authorized", "devices": [{"uid": 3}]},
    {"schema": listen_native.DEVICES_SCHEMA, "microphone_authorization": "authorized", "devices": [{}] * 65},
])
def test_device_inventory_validation_fails_closed(document):
    with pytest.raises(listen_native.InventoryError):
        listen_native.validate_device_inventory(document)
