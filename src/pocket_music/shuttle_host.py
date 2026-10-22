# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in local Shuttle runtime. Native I/O lives here, outside the agent clock."""

from __future__ import annotations
import argparse
from contextlib import nullcontext
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
import unicodedata

import numpy as np
import soundfile as sf

from .errors import PocketError
from .shuttle import PROFILE, _hash, _pcm_identity

CAPTURE_MAX_FRAMES = 1200 * 48000


def _read_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        block = stream.read(size - len(data))
        if not block:
            raise PocketError("Master stream ended inside its WAV header")
        data.extend(block)
    return bytes(data)


def _master_stream_header(stream):
    """Validate the negotiated format instead of interpreting arbitrary raw PCM."""
    header = _read_exact(stream, 12)
    if header[:4] != b"RIFF" or header[8:] != b"WAVE":
        raise PocketError("Master stream must have a self-describing WAV header")
    consumed, qualified = 12, False
    while consumed < 65536:
        name, size = struct.unpack("<4sI", _read_exact(stream, 8))
        consumed += 8
        if name == b"data":
            if not qualified:
                raise PocketError("Master stream has no qualified format before its data")
            return
        if consumed + size + size % 2 > 65536:
            raise PocketError("Master WAV header exceeds its bounded size")
        chunk = _read_exact(stream, size + size % 2)[:size]
        consumed += size + size % 2
        if name != b"fmt ":
            continue
        if size < 16:
            raise PocketError("Master WAV format is incomplete")
        encoding, channels, rate, byte_rate, align, bits = struct.unpack("<HHIIHH", chunk[:16])
        if encoding == 65534 and size >= 40:
            # WAVE_FORMAT_EXTENSIBLE IEEE_FLOAT, valid bits equal container bits.
            if chunk[18:20] == b"\x20\x00" and chunk[24:40] == bytes.fromhex("0300000000001000800000aa00389b71"):
                encoding = 3
        if (encoding, channels, rate, byte_rate, align, bits) != (3, 2, 48000, 384000, 8, 32):
            raise PocketError("Master stream must negotiate FLOAT32 stereo 48000 Hz; no implicit conversion")
        qualified = True
    raise PocketError("Master WAV header exceeds its bounded size")


def _norm(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text).casefold() if c.isalnum())


def _clock(text):
    match = re.fullmatch(r"(\d{2}):(\d{2})\.(\d{1,2})", text)
    if not match or int(match[2]) >= 60:
        return None
    return int(match[1]) * 60 + int(match[2]) + int(match[3]) / 10 ** len(match[3])


def _deck_fields(raw, deck):
    left = deck == "left"

    def text(x0, x1, y0, y1):
        rows = [r for r in raw.get("texts", []) if x0 < r["x"] < x1 and y0 < r["y"] < y1]
        return max(rows, key=lambda r: r["confidence"])["text"] if rows else ""

    return {
        "playing": raw.get(deck + "_playing"),
        "title": text(20, 380, 73, 90) if left else text(925, 1280, 73, 90),
        "artist": text(20, 280, 90, 104) if left else text(925, 1175, 90, 104),
        "duration_seconds": _clock(text(390, 460, 90, 104) if left else text(1290, 1350, 90, 104)),
        "position_seconds": _clock(text(590, 650, 154, 175) if left else text(815, 885, 154, 175)),
    }


def _match(fields, assets):
    matches = [
        a
        for a in assets
        if _norm(a["artist"]) == _norm(fields["artist"])
        and fields["duration_seconds"] is not None
        and abs(a["duration_seconds"] - fields["duration_seconds"]) < 0.04
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def validate_action(action):
    if not isinstance(action, dict):
        raise PocketError("action must be an object")
    kind = action.get("kind")
    fields = {
        "claim": set(),
        "release": set(),
        "play": {"deck", "allow_silence"},
        "pause": {"deck"},
        "sync": {"deck"},
        "gain": {"deck", "value", "duration_seconds", "allow_silence"},
        "loop": {"deck", "beats"},
        "load": {"deck", "track_id"},
    }
    if kind not in fields or set(action) - ({"kind"} | fields[kind]):
        raise PocketError("Unknown Shuttle action or fields")
    if kind not in ("claim", "release") and action.get("deck") not in ("left", "right"):
        raise PocketError("deck must be left or right")
    if "allow_silence" in action and type(action["allow_silence"]) is not bool:
        raise PocketError("allow_silence must be an explicit boolean")
    if kind == "gain":
        if type(action.get("value")) is not int or not 0 <= action["value"] <= 64:
            raise PocketError("Gain is bounded to MIDI 0–64 (about -24 to 0 dB), not a mute")
        seconds = action.get("duration_seconds", 0)
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 4:
            raise PocketError("A gain ride must be between zero and four seconds")
    if kind == "loop" and (
        action.get("beats") is not None
        and (type(action["beats"]) is not int or action["beats"] not in (8, 16))
    ):
        raise PocketError("The qualified loops are 8, 16 or null for off")
    if kind == "load" and not isinstance(action.get("track_id"), str):
        raise PocketError("load requires an exact prepared track_id")
    return action


class Runtime:
    def __init__(self, build_dir, runtime_dir, capture_file=None):
        self.build = Path(build_dir).resolve()
        data = (self.build / "build.json").read_bytes()
        if hashlib.sha256(data).hexdigest() != (self.build / "build.json.sha256").read_text().strip():
            raise PocketError("Shuttle build seal changed")
        build = json.loads(data)
        if build["profile"] != PROFILE or any(_hash(self.build / p) != h for p, h in build["files"].items()):
            raise PocketError("Shuttle native build changed")
        self.directory = Path(runtime_dir).resolve()
        self.directory.mkdir(parents=True, exist_ok=False)
        os.chmod(self.directory, 0o700)
        self.socket_dir = Path(tempfile.mkdtemp(prefix="pocket-shuttle-", dir="/private/tmp"))
        self.nonce = secrets.token_hex(24)
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.action_lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.observation_lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.cancel = self.directory / "cancel"
        self.sessions = {}
        self.requests = {}
        self.acks = {}
        self.serial = 0
        self.revision = 0
        self.owner = None
        self.lease = None
        self.last_physical_ns = 0
        self.physical_events = 0
        self.feedback = {}
        self.feedback_ns = {}
        self.audio = None
        self.audio_error = None
        self.ready = False
        self.initial_inventory_seen = False
        self.topology_changed = False
        self.gains = {"left": None, "right": None}
        self.binding = {}
        self.binding_pid = {}
        self.verified_pcm = {}
        self.midi = subprocess.Popen(
            [str(self.build / "midi-bridge")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=(self.directory / "midi.stderr").open("w"),
            text=True,
            bufsize=1,
        )
        self.capture = subprocess.Popen(
            [
                build["ffmpeg"],
                "-hide_banner",
                "-loglevel",
                "warning",
                "-f",
                "avfoundation",
                "-i",
                ":Serato Virtual Audio",
                "-map",
                "0:a:0",
                "-c:a",
                "pcm_f32le",
                "-f",
                "wav",
                "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=(self.directory / "audio.stderr").open("w"),
            bufsize=0,
        )
        self.capture_file = capture_file
        self.capture_status = {
            "state": "starting" if capture_file else "not_requested",
            "frames_written": 0,
            "maximum_frames": CAPTURE_MAX_FRAMES,
            "sample_rate": 48000,
            "channels": 2,
            "format_verified": False,
            "completion_scope": "local_file_finalization_not_continuity_or_listening_proof",
        }
        self.midi_thread = threading.Thread(target=self._midi_reader, daemon=True)
        self.midi_thread.start()
        self.audio_thread = threading.Thread(target=self._audio_reader, daemon=True)
        self.audio_thread.start()

    def log(self, value):
        with self.log_lock, (self.directory / "events.ndjson").open("a") as stream:
            stream.write(json.dumps({"observed_ns": time.monotonic_ns(), **value}, allow_nan=False) + "\n")

    def _yield(self, reason):
        with self.lock:
            self.owner = None
            self.lease = None
            self.gains = {"left": None, "right": None}
            self.binding.clear()
            self.binding_pid.clear()
            self.revision += 1
            self.cancel.touch(exist_ok=True)
        self.log({"event": "yield", "reason": reason, "revision": self.revision})

    def _midi_reader(self):
        for line in self.midi.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            self.log(event)
            with self.condition:
                if event.get("event") == "ready":
                    if not self.initial_inventory_seen:
                        # The helper creates its own endpoints before emitting
                        # its complete initial inventory. Only this first
                        # baseline may supersede those setup notifications.
                        self.topology_changed = False
                        self.initial_inventory_seen = True
                    self.ready = any("FLX4" in s["name"].upper() for s in event["sources"])
                if event.get("event") == "sent":
                    self.acks.setdefault(event.get("request_id"), []).append(event["status"])
                    self.condition.notify_all()
                if event.get("origin") == "serato_feedback":
                    b = event["bytes"]
                    for i in range(0, len(b) - 2, 3):
                        self.feedback[(b[i], b[i + 1])] = b[i + 2]
                        self.feedback_ns[(b[i], b[i + 1])] = event.get("monotonic_ns", 0)
                if event.get("origin") == "flx4" and any(128 <= v < 240 for v in event["bytes"]):
                    self.physical_events += 1
                    self.last_physical_ns = time.monotonic_ns()
                    self.gains = {"left": None, "right": None}
                    self.binding.clear()
                    self.binding_pid.clear()
                    if self.owner is not None:
                        self._yield("FLX4 intervention")
                if event.get("event") == "topology":
                    self.topology_changed = True
                    self.binding.clear()
                    self.binding_pid.clear()
                    self.gains = {"left": None, "right": None}
                    if self.owner is not None:
                        self._yield("MIDI topology changed; restart and requalify connection")

    def _audio_reader(self):
        pending = b""
        frame = 0
        saved = None
        destination = None
        try:
            _master_stream_header(self.capture.stdout)
            with self.lock:
                self.capture_status["format_verified"] = True
            if self.capture_file:
                # Exclusive create also protects against a destination appearing
                # between a preflight check and the writer opening it.
                destination = Path(self.capture_file).open("xb")
                saved = sf.SoundFile(
                    destination, "w", samplerate=48000, channels=2, format="WAV", subtype="FLOAT"
                )
                with self.lock:
                    self.capture_status["state"] = "recording"
            while True:
                more = self.capture.stdout.read(38400 - len(pending))
                ended = not more
                pending += more
                if ended and not pending:
                    break
                if not ended and len(pending) < 38400:
                    continue
                if len(pending) % 8:
                    raise PocketError("Master stream ended with an incomplete stereo frame")
                x = np.frombuffer(pending, dtype="<f4").reshape(-1, 2)
                pending = b""
                finite = bool(np.isfinite(x).all())
                rms = np.sqrt(np.mean(x.astype("float64") ** 2, axis=0)) if finite else [0, 0]
                peaks = np.max(np.abs(x), axis=0) if finite else [0, 0]
                event = {
                    "event": "audio",
                    "at_ns": time.monotonic_ns(),
                    "frame": frame,
                    "frames": len(x),
                    "sample_rate": 48000,
                    "channels": 2,
                    "finite": finite,
                    "rms_dbfs": [float(20 * np.log10(max(v, 1e-12))) for v in rms],
                    "peak_dbfs": [float(20 * np.log10(max(v, 1e-12))) for v in peaks],
                }
                frame += len(x)
                if not finite:
                    raise PocketError("Master stream contains nonfinite samples")
                if saved:
                    remaining = CAPTURE_MAX_FRAMES - self.capture_status["frames_written"]
                    kept = x[:remaining]
                    saved.write(kept)
                    saved.flush()
                    with self.lock:
                        self.capture_status["frames_written"] += len(kept)
                    if len(kept) == remaining:
                        saved.close()
                        saved = None
                        destination.close()
                        destination = None
                        with self.lock:
                            self.capture_status["state"] = "exhausted"
                        self.log({"event": "capture_exhausted", **self.capture_status})
                with self.lock:
                    self.audio = event
                self.log(event)
                if ended:
                    break
        except Exception as exc:
            with self.lock:
                self.audio_error = str(exc)
                self.audio = None
                if self.capture_file:
                    self.capture_status.update(state="failed", error=str(exc))
            self.log({"event": "audio_failed", "error": str(exc), "capture": dict(self.capture_status)})
        finally:
            closing_error = None
            for output in (saved, destination):
                if output is None:
                    continue
                try:
                    output.close()
                except Exception as exc:
                    closing_error = str(exc)
            if closing_error is None:
                with self.lock:
                    if self.capture_status["state"] == "recording":
                        self.capture_status["state"] = "finalized"
                if self.capture_file:
                    self.log({"event": "capture_closed", **self.capture_status})
            else:
                with self.lock:
                    self.capture_status.update(state="failed", error=closing_error)
                self.log({"event": "capture_failed", **self.capture_status})

    def _guard_monitor(self, require_capture=True):
        with self.lock:
            audio = self.audio
            if (
                not self.ready
                or self.topology_changed
                or self.midi.poll() is not None
                or self.capture.poll() is not None
                or audio is None
                or not audio["finite"]
                or type(audio.get("at_ns")) is not int
                or not 0 <= time.monotonic_ns() - audio["at_ns"] <= 600_000_000
            ):
                raise PocketError("Live monitoring is unavailable or stale; no control dispatched")
            capture = getattr(self, "capture_status", {})
            if require_capture and capture.get("state") in ("starting", "exhausted", "failed", "finalized"):
                raise PocketError("Requested capture is not recording (" + capture["state"] + "); no control dispatched")
        return audio

    def _send(self, messages, *, session_id, lease):
        with self.send_lock:
            with self.condition:
                # Authority and bounded submission are serialized with revoke.
                # Note-on/off are submitted together, before releasing this lock.
                self._owned(session_id, lease)
                self.serial += 1
                ident = str(self.serial)
                self.acks[ident] = []
                payload = "".join(json.dumps({"send": message, "request_id": ident}) + "\n"
                                  for message in messages)
                self.midi.stdin.write(payload)
                self.midi.stdin.flush()
                # Condition.wait_for releases the state lock, so physical input,
                # explicit release and the acknowledgement reader remain live.
                complete = self.condition.wait_for(lambda: len(self.acks[ident]) == len(messages), timeout=1)
                acks = self.acks.pop(ident)
            if not complete or any(acks):
                raise PocketError("Native dispatch outcome unknown; inspect request status before retrying")

    def _pulse(self, note, *, session_id, lease):
        self._send([[144, note, 127], [128, note, 0]], session_id=session_id, lease=lease)

    def _raw_observe(self):
        if not self.observation_lock.acquire(blocking=False):
            raise PocketError("Fresh Serato observation is busy; no cached result returned")
        try:
            return self._capture_observation()
        finally:
            self.observation_lock.release()

    def _capture_observation(self):
        self._guard_monitor(require_capture=False)
        path = self.directory / ("view-" + secrets.token_hex(6) + ".png")
        requested_ns = time.monotonic_ns()
        result = subprocess.run(
            [str(self.build / "observe"), str(path)], capture_output=True, text=True, timeout=8
        )
        if result.returncode:
            raise PocketError("Fresh Serato observation failed")
        raw = json.loads(result.stdout)
        if raw["status"] != "ok":
            raise PocketError(
                "Unsupported Serato view; use the qualified Extended 1470x956 view, MIDI learn off"
            )
        now = time.monotonic_ns()
        started, completed = raw.get("started_ns"), raw.get("completed_ns")
        if (
            type(started) is not int or type(completed) is not int
            or not requested_ns <= started <= completed <= now
            or now - started > 8_000_000_000
            or raw.get("image_path") != str(path)
        ):
            raise PocketError("Native observation is stale, future-dated or belongs to another capture")
        self._guard_monitor(require_capture=False)
        return raw

    def observe(self, session_id):
        session = self.sessions[session_id]
        raw = self._raw_observe()
        decks = {}
        for deck in ("left", "right"):
            fields = _deck_fields(raw, deck)
            match = _match(fields, session["assets"])
            if (not match or self.binding.get(deck) != match["track_id"]
                    or type(raw.get("serato_pid")) is not int
                    or self.binding_pid.get(deck) != raw.get("serato_pid")):
                self.binding.pop(deck, None)
                self.binding_pid.pop(deck, None)
            first = 30 if deck == "left" else 32
            active = [
                beats
                for note, beats in [(first, 8), (first + 1, 16)]
                if self.feedback.get((144, note)) == 127
            ]
            fields.update(
                track_id=match["track_id"] if match else None,
                identity_scope="unique_display_artist_and_duration" if match else "unresolved",
                loop_beats=active[0] if len(active) == 1 else None,
                loop_state_known=all((144, n) in self.feedback for n in (first, first + 1)),
                commanded_gain_cc=self.gains[deck],
            )
            if match and self.binding.get(deck) == match["track_id"]:
                fields["identity_scope"] = "source_checked_selection_and_fresh_serato_cache_event"
            decks[deck] = fields
        with self.lock:
            return {
                "schema": "pocket.shuttle-observation/v1",
                "status": "ok",
                "nonce": self.nonce,
                "revision": self.revision,
                "owner": self.owner,
                "session_id": session_id,
                "owned": self.owner == session_id,
                "physical_events": self.physical_events,
                "audio": dict(self.audio),
                "capture": dict(self.capture_status),
                "decks": decks,
                "profile": PROFILE,
                "native_observation_ns": raw["started_ns"],
                "native_observation_completed_ns": raw["completed_ns"],
                "image_path": raw["image_path"],
                "serato_pid": raw.get("serato_pid"),
                "coverage": {
                    "audio": "master_only",
                    "headphone_cue": "not_observed",
                    "musical_listening": "not_performed",
                    "beat_accuracy": "not_qualified",
                },
            }

    def _owned(self, session_id, lease):
        with self.lock:
            if self.owner != session_id or self.lease != lease:
                raise PocketError("Shuttle yielded; pending work cancelled without restoring controls")
        self._guard_monitor()

    def _verified_source(self, target):
        path = Path(target["native_path"])

        def stamp():
            value = path.stat()
            return (str(path), value.st_dev, value.st_ino, value.st_ctime_ns,
                    value.st_mtime_ns, value.st_size)

        observed = stamp()
        if observed not in self.verified_pcm:
            decoded = _pcm_identity(path)
            if stamp() != observed:
                raise PocketError("Native source changed during verification")
            self.verified_pcm[observed] = decoded
        if self.verified_pcm[observed] != target["pcm"]:
            raise PocketError("Native source audio differs from the prepared recording; prepare again if its identity is outdated")
        return observed

    def _execute(self, session_id, action, lease):
        kind = action["kind"]
        observation = self.observe(session_id)
        self._owned(session_id, lease)
        deck = action["deck"]
        fields = observation["decks"][deck]
        right = deck == "right"
        if kind == "load":
            if fields["playing"] is not False:
                raise PocketError("Refusing to load a playing or unresolved deck")
            assets = self.sessions[session_id]["assets"]
            matches = [a for a in assets if a["track_id"] == action["track_id"]]
            if len(matches) != 1:
                raise PocketError("Record is not in the prepared crate")
            target = matches[0]
            path = Path(target["native_path"])
            self._verified_source(target)
            subprocess.run(
                [str(self.build / "library-keys"), str(len(assets)), str(target["index"]), str(self.cancel)],
                check=True,
                timeout=12,
            )
            self._owned(session_id, lease)
            self._verified_source(target)
            self._owned(session_id, lease)
            log = Path("~/Music/_Serato_/Logs/DJ.INFO").expanduser()
            before = log.stat()
            if raw_pid := observation.get("serato_pid"):
                if not log.resolve().name.endswith("." + str(raw_pid) + ".log"):
                    raise PocketError("Serato cache log does not belong to the observed app process")
            # Loading restores per-record trim; a previous command no longer
            # supplies a valid starting value for a gain ride on this deck.
            with self.lock:
                self._owned(session_id, lease)
                self.gains[deck] = None
                self.binding.pop(deck, None)
                self.binding_pid.pop(deck, None)
            self._pulse(51 if right else 50, session_id=session_id, lease=lease)
            time.sleep(0.4)
            after = self.observe(session_id)
            last = log.stat()
            if before.st_ino != last.st_ino or last.st_size - before.st_size > 512 * 1024:
                raise PocketError("Serato log rotated or exceeded the bounded load observation")
            with log.open("rb") as stream:
                stream.seek(before.st_size)
                fresh = stream.read(512 * 1024).decode("utf-8", "replace")
            expected = "The cache `" + str(path) + "` was requested"
            cache_paths = set(re.findall(r"\[View\] The cache `(.*?)` was requested", fresh))
            if (
                after["decks"][deck]["track_id"] != target["track_id"]
                or expected not in fresh
                or cache_paths != {str(path)}
            ):
                raise PocketError("Loaded source not verified; no playback was started")
            if after.get("serato_pid") != observation.get("serato_pid"):
                raise PocketError("Serato process changed during loading; source binding unavailable")
            self._verified_source(target)
            with self.lock:
                self._owned(session_id, lease)
                self.binding[deck] = target["track_id"]
                self.binding_pid[deck] = after.get("serato_pid")
            return {
                "status": "verified",
                "track_id": target["track_id"],
                "source_evidence": "fresh Serato cache-path log and unique display artist/duration",
            }
        if kind in ("play", "pause"):
            desired = kind == "play"
            if desired and fields["track_id"] is None:
                raise PocketError("Playback source is unresolved in this crate")
            if fields["playing"] is None:
                raise PocketError("Transport state is unresolved")
            if fields["playing"] != desired:
                self._pulse(21 if right else 20, session_id=session_id, lease=lease)
            after = self.observe(session_id)
            if after["decks"][deck]["playing"] != desired:
                raise PocketError("Transport command was not verified; do not toggle again blindly")
            other = after["decks"]["left" if right else "right"]
            if desired and not other["playing"] and not action.get("allow_silence", False):
                deadline = time.monotonic() + 1
                while max(self._guard_monitor()["rms_dbfs"]) < -60 and time.monotonic() < deadline:
                    time.sleep(0.1)
                if max(self._guard_monitor()["rms_dbfs"]) < -60:
                    raise PocketError(
                        "Transport started but the master remains near-silent; audio readiness is unverified"
                    )
            return {
                "status": "verified",
                "track_id": after["decks"][deck]["track_id"],
                "playing": desired,
                "master_audio": after["audio"],
            }
        if fields["track_id"] is None:
            raise PocketError("Control source is unresolved in this crate")
        if kind == "gain":
            target = action["value"]
            duration = action.get("duration_seconds", 0)
            start = self.gains[deck]
            if duration and start is None:
                raise PocketError("Set an explicit gain first; the initial native gain is unknown")
            steps = max(1, math.ceil(duration * 20))
            began = time.monotonic()
            quiet_since = None
            for step in range(1, steps + 1):
                self._owned(session_id, lease)
                audio = self._guard_monitor()
                if (
                    fields["playing"]
                    and not action.get("allow_silence", False)
                    and max(audio["rms_dbfs"]) < -60
                ):
                    quiet_since = audio["at_ns"] if quiet_since is None else quiet_since
                    if audio["at_ns"] - quiet_since > 500_000_000:
                        raise PocketError("Unexpected near-silent master; gain ride cancelled")
                else:
                    quiet_since = None
                value = target if start is None else round(start + (target - start) * step / steps)
                self._send([[176, 24 if right else 23, value]], session_id=session_id, lease=lease)
                with self.lock:
                    self._owned(session_id, lease)
                    self.gains[deck] = value
                delay = began + duration * step / steps - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            return {
                "status": "dispatched",
                "commanded_gain_cc": target,
                "verification": "native dispatch acknowledgement and live master monitoring; not numeric gain readback",
            }
        if kind == "loop":
            wanted = action.get("beats")
            current = fields["loop_beats"]
            first = 32 if right else 30
            if wanted is None and not fields.get("loop_state_known", False):
                raise PocketError("Loop feedback is unknown; refusing to claim loop exit")
            if wanted == current:
                return {"status": "unchanged", "loop_beats": wanted,
                        "verification": "cached MIDI feedback; no new dispatch or fresh readback"}
            sent_ns = time.monotonic_ns()
            if wanted != current:
                if wanted is None:
                    if current is None:
                        raise PocketError("Loop state is unresolved")
                    note = first + (current == 16)
                else:
                    note = first + (wanted == 16)
                self._pulse(int(note), session_id=session_id, lease=lease)
            deadline = time.monotonic() + 0.5
            while True:
                self._owned(session_id, lease)
                now_ns = time.monotonic_ns()
                with self.lock:
                    if wanted is not None:
                        address = (144, first + (wanted == 16))
                        confirmed = self.feedback.get(address) == 127
                    else:
                        address = (144, note)
                        confirmed = all(self.feedback.get((144, n)) == 0 for n in (first, first + 1))
                    seen = self.feedback_ns.get(address, 0)
                    confirmed = confirmed and type(seen) is int and sent_ns <= seen <= now_ns
                if confirmed:
                    break
                if time.monotonic() >= deadline:
                    raise PocketError("Loop state not confirmed by post-dispatch MIDI feedback; do not toggle again blindly")
                time.sleep(0.02)
            return {"status": "verified", "loop_beats": wanted}
        if kind == "sync":
            note = 35 if right else 34
            self._pulse(note, session_id=session_id, lease=lease)
            time.sleep(0.15)
            return {
                "status": "dispatched",
                "sync_feedback": self.feedback.get((144, note)),
                "beat_alignment": "not_established_by_sync_light",
            }
        raise PocketError("Unsupported action")

    def act(self, request):
        action = validate_action(request["action"])
        kind = action["kind"]
        session_id = request["session_id"]
        key = session_id + ":" + request["request_id"]
        digest = json.dumps(request, sort_keys=True)
        with self.action_lock if kind != "release" else nullcontext():
            if key in self.requests:
                old, result = self.requests[key]
                if old != digest:
                    raise PocketError("request_id reused with different inputs")
                return {**result, "replayed": True}
            with self.lock:
                if request["expected_revision"] != self.revision:
                    raise PocketError("Stale Shuttle revision; observe before proposing another move")
                if kind == "claim":
                    self._guard_monitor()
                    if (
                        self.owner not in (None, session_id)
                        or time.monotonic_ns() - self.last_physical_ns < 1_000_000_000
                    ):
                        raise PocketError("Another performer owns the controls, or the FLX4 is still moving")
                    self.owner = session_id
                    self.lease = secrets.token_hex(12)
                    self.cancel.unlink(missing_ok=True)
                    self.revision += 1
                    result = {"status": "verified", "owned": True}
                elif kind == "release":
                    if self.owner not in (None, session_id):
                        raise PocketError("This session does not own Shuttle")
                    self._yield("explicit release")
                    result = {"status": "verified", "owned": False}
                else:
                    if self.owner != session_id:
                        raise PocketError(
                            "Shuttle is unowned or yielded; explicitly claim from a fresh observation"
                        )
                    lease = self.lease
                    self.revision += 1
                    result = None
            self.log({"event": "request_started", "request": request, "revision": self.revision})
            self.requests[key] = (digest, {"status": "pending", "request_id": request["request_id"]})
            if result is None:
                try:
                    result = self._execute(session_id, action, lease)
                    self._owned(session_id, lease)
                except Exception as exc:
                    result = {
                        "status": "unverified",
                        "error": str(exc),
                        "retry_policy": "inspect fresh state; no automatic redispatch",
                    }
            result = {
                **result,
                "request_id": request["request_id"],
                "revision": self.revision,
                "replayed": False,
            }
            self.requests[key] = (digest, result)
            self.log({"event": "request_completed", "request_id": request["request_id"], **result})
            return result

    def dispatch(self, request):
        if request.get("nonce") != self.nonce:
            raise PocketError("Stale Shuttle process nonce")
        op = request.get("op")
        if op == "register":
            session_id = request["session_id"]
            assets = request["assets"]
            if not 1 <= len(assets) <= 64 or session_id in self.sessions:
                raise PocketError("Invalid or already registered Shuttle session")
            self.sessions[session_id] = {"assets": assets, "crate_name": request["crate_name"]}
            return {"status": "registered", "revision": self.revision, "nonce": self.nonce}
        if op == "request_status":
            value = self.requests.get(request["session_id"] + ":" + request["request_id"])
            return (
                value[1] if value else {"status": "unknown", "redispatch": "not_authorized_by_unknown_status"}
            )
        if request.get("session_id") not in self.sessions:
            raise PocketError("Unregistered Shuttle session")
        if op == "observe":
            return self.observe(request["session_id"])
        if op == "act":
            return self.act(request)
        raise PocketError("Unsupported Shuttle request")

    def close(self):
        self._yield("host shutdown")
        for child in [self.midi, self.capture]:
            child.terminate()
        for child in [self.midi, self.capture]:
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        self.audio_thread.join(timeout=2)
        self.midi_thread.join(timeout=2)
        shutil.rmtree(self.socket_dir, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the opt-in local Serato/FLX4 Shuttle bridge")
    parser.add_argument("--build-dir", required=True)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--capture-file")
    args = parser.parse_args(argv)
    runtime = Runtime(args.build_dir, args.runtime_dir, args.capture_file)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(30)
            try:
                raw = self.rfile.readline(262145)
                if len(raw) > 262144:
                    raise PocketError("Oversized request")
                result = runtime.dispatch(json.loads(raw))
            except Exception as exc:
                result = {"status": "error", "error": str(exc)}
            self.wfile.write((json.dumps(result, allow_nan=False) + "\n").encode())

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

    address = runtime.socket_dir / "host.sock"
    server = Server(str(address), Handler)
    os.chmod(address, 0o600)
    descriptor = {
        "schema": "pocket.shuttle-connection/v1",
        "socket": str(address),
        "nonce": runtime.nonce,
        "pid": os.getpid(),
        "profile": PROFILE,
    }
    (runtime.directory / "connection.json").write_text(json.dumps(descriptor))
    os.chmod(runtime.directory / "connection.json", 0o600)
    print(json.dumps({"status": "started", "runtime_dir": str(runtime.directory)}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        runtime.close()


if __name__ == "__main__":
    main()
