# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import json
import os
import queue
import shlex
import shutil
import signal
import sys
import threading
import time
from pathlib import Path

from pocket_music import listen_native
from pocket_music.listen_native import (
    BLOCK,
    EVENT,
    EXIT,
    HEARTBEAT,
    HEARTBEAT_TYPE,
    HELLO,
    ORDERLY_EXIT_CODES,
    STARTED,
    encode_frame,
)

FAKE_FLAGS = {"--non-float", "--no-started", "--ignore-quit", "--exit-at-end", "--no-heartbeat",
              "--start-failed"}
FAKE_VALUES = {"--refuse", "--authorization", "--input-channels", "--device-rate", "--start-refusal",
               "--device-changed",
               "--hello-delay-ms", "--script", "--malformed-after", "--hog-pid", "--list-output",
               "--list-exit-code", "--list-sleep-ms", "--quit-exit-code", "--late-authorization",
               "--list-unreadable"}


FIXTURE_VERSION = "test fixture: Python fake helper, not a native build"


def write_launcher(path, *fake_args, marker=None):
    command = " ".join(shlex.quote(str(a)) for a in (sys.executable, Path(__file__).resolve(), *fake_args))
    touch = "" if marker is None else f"printf '%s\\n' \"$0\" > {shlex.quote(str(marker))}\n"
    Path(path).write_text(f'#!/bin/sh\n{touch}exec {command} "$@"\n')
    Path(path).chmod(0o755)


def write_fake_build(*fake_args, register=True, marker=None):
    if not os.environ.get(listen_native.REGISTRY_ENV):
        raise RuntimeError("POCKET_LISTEN_DIR must name a test registry; tests/conftest.py sets it")
    build_id, build = listen_native._new_build()
    shutil.copy2(listen_native.helper_source(), build / listen_native.SOURCE_NAME)
    write_launcher(build / listen_native.HELPER_NAME, *fake_args, marker=marker)
    if register:
        listen_native._seal_and_register(build_id, build, FIXTURE_VERSION)
    else:
        listen_native._seal_build(build, FIXTURE_VERSION)
    return build_id, build


class _Clock:
    def monotonic_ns(self):
        return time.monotonic_ns()

    def sleep_ns(self, ns):
        if ns > 0:
            time.sleep(ns / 1e9)


def _parse(argv):
    fake, i = {"--refuse": []}, 0
    while i < len(argv) and argv[i].startswith("--"):
        key = argv[i]
        if key in FAKE_FLAGS:
            fake[key] = True
            i += 1
        elif key in FAKE_VALUES:
            if key == "--refuse":
                fake[key].append(argv[i + 1])
            else:
                fake[key] = argv[i + 1]
            i += 2
        else:
            raise SystemExit(f"unknown fake option {key}")
    mode = argv[i] if i < len(argv) else ""
    rest = argv[i + 1:]
    options = {rest[j][2:]: rest[j + 1] for j in range(0, len(rest) - 1, 2)}
    return fake, mode, options


def _stream(channels, rate, *, float32=True):
    return {"starting_channel": 0, "channels": channels,
            "virtual_format": {"format_id": "lpcm", "float": float32, "bits_per_channel": 32,
                               "channels_per_frame": channels, "interleaved": True, "packed": True,
                               "native_endian": True, "sample_rate": rate}}


def _device(uid, name, channels, rate, *, alive=True, hog=-1, float32=True, object_id=4242):
    return {"uid": uid, "name": name, "manufacturer": "Pocket test", "model_uid": "fake-helper",
            "transport": "virtual", "audio_object_id": object_id, "alive": alive, "hog_mode_pid": hog,
            "nominal_sample_rate": rate, "input_channels": channels, "stream_configuration": [channels],
            "input_streams": [_stream(channels, rate, float32=float32)], "channel_names": [""] * channels,
            "buffer_frame_size": 480, "device_latency_frames": 0, "safety_offset_frames": 0}


def list_devices(fake):
    time.sleep(int(fake.get("--list-sleep-ms", 0)) / 1000)
    if "--list-output" in fake:
        sys.stdout.buffer.write(Path(fake["--list-output"]).read_bytes())
    else:
        devices = [_device("fake:duo-a", "Practice Duo", 4, 48000, object_id=41),
                   _device("fake:duo-b", "Practice Duo", 2, 48000, object_id=42),
                   _device("fake:int", "Integer Input", 2, 48000, float32=False, object_id=43),
                   _device("fake:hogged", "Taken Input", 2, 48000, hog=999, object_id=44),
                   _device("fake:odd-rate", "Odd Rate", 2, 22050, object_id=45),
                   _device("fake:gone", "Gone Input", 2, 48000, alive=False, object_id=46),
                   _device("fake:hog-unread", "Unread Input", 2, 48000, hog=None, object_id=47)]
        document = {"schema": listen_native.DEVICES_SCHEMA, "macos_version": "26.0.0",
                    "microphone_authorization": fake.get("--authorization", "authorized"),
                    "timebase": {"numer": 1, "denom": 1}, "devices": devices, "devices_omitted": 0,
                    "devices_unreadable": int(fake.get("--list-unreadable", 0))}
        sys.stdout.buffer.write(json.dumps(document).encode() + b"\n")
    sys.stdout.buffer.flush()
    return int(fake.get("--list-exit-code", 0))


class _Out:
    def __init__(self):
        self.serial = 0
        self.broken = False

    def frame(self, kind, payload):
        self.raw(encode_frame(kind, payload, self.serial))
        self.serial += 1

    def raw(self, data):
        if self.broken:
            return
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except (BrokenPipeError, OSError):
            self.broken = True


def _stdin_lines(lines):
    for line in sys.stdin.buffer:
        lines.put(line.decode("utf-8", "replace").rstrip("\n"))
    lines.put(None)


def _default_script(uid, channels, rate):
    return {"block_frames": 480, "devices": [{"uid": uid, "name": "Fake", "input_channels": channels,
                                              "sample_rate": rate}],
            "segments": [{"kind": "tone", "frames": rate * 30,
                          "tones": [{"channel": c, "hz": 480.0, "dbfs": -12.0} for c in range(channels)]}]}


def capture(fake, options):
    uid, rate = options["device-uid"], int(options["expected-sample-rate"])
    ignore = fake.get("--ignore-quit", False)
    stop = threading.Event()

    def terminated(*_):
        print("fake helper got SIGTERM", file=sys.stderr, flush=True)
        stop.set()

    signal.signal(signal.SIGTERM, signal.SIG_IGN if ignore else terminated)
    lines = queue.Queue()
    threading.Thread(target=_stdin_lines, args=(lines,), daemon=True).start()
    out = _Out()
    time.sleep(int(fake.get("--hello-delay-ms", 0)) / 1000)
    channels = int(fake.get("--input-channels", 2))
    device_rate = int(fake.get("--device-rate", rate))
    refusal = list(fake["--refuse"])
    authorization = fake.get("--authorization",
                             "not_determined" if "microphone_not_authorized" in refusal else "authorized")
    hog = fake.get("--hog-pid", "-1")
    device = _device(uid, "Fake Duo", channels, device_rate, hog=None if hog == "unreadable" else int(hog),
                     float32=not fake.get("--non-float", False))
    out.frame(HELLO, {"schema": "pocket.listen-helper-hello/v1", "mode": "capture", "helper_pid": os.getpid(),
                      "macos_version": "26.0.0", "device": device, "microphone_authorization": authorization,
                      "timebase": {"numer": 1, "denom": 1}, "host_time_scope": "mach_absolute_time",
                      "max_block_frames": 8192, "refusal": refusal})
    if refusal:
        out.frame(EXIT, {"schema": "pocket.listen-helper-exit/v1", "reason": "refused", "refusal": refusal})
        return ORDERLY_EXIT_CODES["refused"]
    try:
        line = lines.get(timeout=10)
    except queue.Empty:
        line = None
    if line is None or not line.startswith("start ") or fake.get("--no-started"):
        while not ignore and line is not None and line != "quit":
            line = lines.get()
        out.frame(EXIT, {"schema": "pocket.listen-helper-exit/v1", "reason": "stdin_closed"})
        return 0
    print(f"fake helper received: {line}", file=sys.stderr, flush=True)
    from pocket_music.listen_adapters import SyntheticAdapter
    selected = tuple(int(c) for c in line[6:].split(","))
    if "--start-refusal" in fake:
        late = {"microphone_authorization": fake.get("--late-authorization", "denied")} \
            if fake["--start-refusal"] == "microphone_not_authorized" else {}
        out.frame(EXIT, {"schema": "pocket.listen-helper-exit/v1", "reason": "refused",
                         "refusal": [fake["--start-refusal"]], **late})
        return ORDERLY_EXIT_CODES["refused"]
    if "--device-changed" in fake:
        value = ({"property": "hog_mode"} if fake["--device-changed"] == "device_property_unreadable"
                 else {"stream_configuration": [2]})
        out.frame(EXIT, {"schema": "pocket.listen-helper-exit/v1", "reason": "device_changed_before_start",
                         "type": fake["--device-changed"], "value": value})
        return ORDERLY_EXIT_CODES["device_changed_before_start"]
    if fake.get("--start-failed"):
        out.frame(EXIT, {"schema": "pocket.listen-helper-exit/v1", "reason": "start_failed", "stage": "start",
                         "osstatus": -10851})
        return 5
    script = (json.loads(Path(fake["--script"]).read_text()) if "--script" in fake
              else _default_script("synthetic:fake", channels, rate))
    adapter = SyntheticAdapter(script, script["devices"][0]["uid"], rate, _Clock(), pacing="realtime")
    adapter.open()
    adapter.start(selected)
    out.frame(STARTED, {"schema": "pocket.listen-helper-started/v1", "selected_channels": list(selected),
                        "ring_frames": 131072, "ring_bytes": 131072 * len(selected) * 4, "record_capacity": 4096,
                        "max_block_frames": 8192, "host_now_ticks": time.monotonic_ns(),
                        "unwatched_properties": []})
    return _stream_blocks(fake, adapter, out, lines, stop, ignore)


def _stream_blocks(fake, adapter, out, lines, stop, ignore):
    from pocket_music.listen_adapters import AdapterBlock, encode_block
    malformed_after = int(fake.get("--malformed-after", -1))
    index = dropped_blocks = dropped_total = 0
    last_beat = time.monotonic()
    ended = False
    reason = None
    while True:
        while not lines.empty():
            line = lines.get()
            if not ignore and line in (None, "quit") and reason is None:
                reason = "quit" if line == "quit" else "stdin_closed"
                stop.set()
        if stop.is_set() and reason is None:
            reason = "terminated"
        if stop.is_set() and not ignore:
            out.frame(EXIT, {"schema": "pocket.listen-helper-exit/v1", "reason": reason, "callbacks": index,
                             "blocks_written": index, "blocks_sent": index, "dropped_frames": dropped_total,
                             "dropped_blocks": dropped_blocks, "oversize_blocks": 0, "layout_mismatch_blocks": 0,
                             "undelivered_blocks": 0, "undelivered_frames": 0})
            return int(fake.get("--quit-exit-code", 0))
        item = None if ended else adapter.read(20_000_000)
        if isinstance(item, AdapterBlock):
            if item.adapter_dropped_frames_total > dropped_total:
                dropped_blocks += 1
                dropped_total = item.adapter_dropped_frames_total
            flags = (1 if item.device_sample_time is not None else 0) | (2 if item.device_host_time_ns is not None
                                                                          else 0)
            host = item.device_host_time_ns or time.monotonic_ns()
            out.frame(BLOCK, encode_block(index, item.device_sample_time or 0.0, host, flags, item.pcm,
                                          dropped_total, dropped_blocks, host, host + 5000))
            index += 1
            if index == malformed_after:
                out.raw(b"PLH2" + bytes(12))
        elif item is not None:
            if item.type == "end_of_stream":
                ended = True
                if fake.get("--exit-at-end"):
                    out.frame(EXIT, {"schema": "pocket.listen-helper-exit/v1", "reason": "script_complete"})
                    return 0
            else:
                out.frame(EVENT, {"schema": "pocket.listen-helper-event/v1", "type": item.type,
                                  "value": item.detail or None, "host_now_ticks": time.monotonic_ns()})
                if item.type != "processor_overload":
                    out.frame(EXIT, {"schema": "pocket.listen-helper-exit/v1", "reason": "device_event"})
                    return ORDERLY_EXIT_CODES["device_event"]
        else:
            time.sleep(0.02)
        if not fake.get("--no-heartbeat") and time.monotonic() - last_beat >= 0.25:
            last_beat = time.monotonic()
            out.frame(HEARTBEAT_TYPE, HEARTBEAT.pack(time.monotonic_ns(), index, dropped_total, 5000, index))


def main(argv):
    fake, mode, options = _parse(argv)
    if mode == "list-devices":
        return list_devices(fake)
    if mode == "capture":
        return capture(fake, options)
    print("usage: listen_fake_helper.py [fake options] list-devices|capture ...", file=sys.stderr)
    return 2


if __name__ == "__main__":
    code = main(sys.argv[1:])
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except OSError:
        pass
    # Skip interpreter shutdown: the stdin reader thread may hold stdin's lock, which aborts it.
    os._exit(code)
