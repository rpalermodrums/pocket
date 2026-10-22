# SPDX-License-Identifier: AGPL-3.0-only
"""Shuttle: a crate and short, observed moves in a local Serato session.

The native bridge is opt-in. File identity, display observations, master signal
and human listening are separate evidence; no operation plans a running order.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from urllib.parse import quote

import soundfile as sf
import numpy as np

from .errors import PocketError
from .shuttle_types import ShuttleAction
from .record_bag import create_record_bag, load_record_bag
from .whisker import prepare_session, rank_next_tracks, session_snapshot, update_session

PROFILE = "serato-5-extended-1470x956/v1"
MAX_TRACKS = 64


def _hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _pcm_identity(path):
    """Decoded samples; keep integer v1 identities and tag exact floats as v2."""
    path = Path(path)
    before = path.stat()
    h = hashlib.sha256()
    representation = None
    with sf.SoundFile(path) as audio:
        header = [audio.samplerate, audio.channels, len(audio), audio.subtype]
        h.update(json.dumps(header).encode())
        floating = audio.subtype in ("FLOAT", "DOUBLE")
        dtype = "float32" if audio.subtype == "FLOAT" else "float64" if floating else "int32"
        if floating:
            representation = "decoded-" + dtype + "-little-endian/v2"
            h.update(representation.encode())
        while True:
            data = audio.read(65536, dtype=dtype, always_2d=True)
            if not len(data):
                break
            if floating and not np.isfinite(data).all():
                raise PocketError("Floating-point source contains nonfinite samples")
            h.update(data.astype("<f4" if dtype == "float32" else "<f8" if floating else "<i4", copy=False).tobytes())
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino, before.st_dev, before.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
        after.st_dev,
        after.st_ctime_ns,
    ):
        raise PocketError("Recording changed during identity verification")
    result = {
        "sha256": h.hexdigest(),
        "sample_rate": header[0],
        "channels": header[1],
        "frames": header[2],
        "subtype": header[3],
    }
    if representation:
        result["representation"] = representation
    return result


def _clone(source, destination):
    if sys.platform == "darwin":
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        if lib.clonefile(os.fsencode(source), os.fsencode(destination), 0) == 0:
            return
    shutil.copy2(source, destination)


def _seal(path, value):
    data = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    with Path(path).open("xb") as stream:
        stream.write(data)
    Path(str(path) + ".sha256").write_text(hashlib.sha256(data).hexdigest() + "\n")


def _session(session_dir):
    p = Path(session_dir).expanduser().resolve() / "shuttle.json"
    try:
        data = p.read_bytes()
        if hashlib.sha256(data).hexdigest() != Path(str(p) + ".sha256").read_text().strip():
            raise PocketError("Shuttle session manifest changed")
        value = json.loads(data)
        if value["schema"] != "pocket.shuttle-session/v1":
            raise PocketError("Unsupported Shuttle session")
        return value
    except PocketError:
        raise
    except (OSError, ValueError, KeyError) as exc:
        raise PocketError("Cannot read the sealed Shuttle session") from exc


def _rpc(runtime_dir, request):
    try:
        descriptor = json.loads((Path(runtime_dir) / "connection.json").read_text())
        if descriptor["schema"] != "pocket.shuttle-connection/v1":
            raise PocketError("Unsupported Shuttle connection")
        payload = json.dumps({**request, "nonce": descriptor["nonce"]}, allow_nan=False).encode() + b"\n"
        if len(payload) > 262144:
            raise PocketError("Shuttle request is too large")
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(30)
            connection.connect(descriptor["socket"])
            connection.sendall(payload)
            raw = connection.makefile("rb").readline(262145)
        if len(raw) > 262144:
            raise PocketError("Shuttle response is too large")
        result = json.loads(raw)
        if result.get("status") == "error":
            raise PocketError(result["error"])
        return result
    except PocketError:
        raise
    except (OSError, ValueError, KeyError) as exc:
        raise PocketError(
            "Shuttle connection failed; an action outcome may be unknown. "
            "Inspect shuttle_request_status before doing anything again."
        ) from exc


def shuttle_build_bridge(output_dir: str) -> dict:
    """Build optional macOS helpers and an isolated MIDI preset; does not run them."""
    if sys.platform != "darwin" or not shutil.which("swiftc") or not shutil.which("ffmpeg"):
        raise PocketError("Shuttle requires macOS, the Swift compiler and an installed ffmpeg")
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).parent / "devices" / "shuttle"
    files = {}
    for name in ["midi.swift", "observe.swift", "library.swift", "Shuttle.xml"]:
        shutil.copy2(source / name, output / name)
        files[name] = _hash(output / name)
    with tempfile.TemporaryDirectory(prefix="shuttle-swift-") as cache:
        for name, binary in [("midi", "midi-bridge"), ("observe", "observe"), ("library", "library-keys")]:
            result = subprocess.run(
                [
                    "swiftc",
                    "-module-cache-path",
                    cache,
                    str(output / (name + ".swift")),
                    "-o",
                    str(output / binary),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode:
                raise PocketError("Shuttle native build failed: " + result.stderr[-2000:])
            files[binary] = _hash(output / binary)
    profile = {
        "schema": "pocket.shuttle-build/v1",
        "profile": PROFILE,
        "files": files,
        "ffmpeg": shutil.which("ffmpeg"),
        "native_acceptance": "separate_from_build",
    }
    _seal(output / "build.json", profile)
    return {
        "schema": "pocket.shuttle-build-receipt/v1",
        "build_dir": str(output),
        "profile": PROFILE,
        "status": "built_not_started",
        "files": files,
        "next_action": "Run python -m pocket_music.shuttle_host with --build-dir and a new --runtime-dir",
    }


def shuttle_prepare(
    crate_name: str,
    output_dir: str,
    runtime_dir: str,
    library_path: str | None = None,
    intent: dict | None = None,
) -> dict:
    """Snapshot one local Serato crate and prepare Whisker; no deck changes or route."""
    if not isinstance(crate_name, str) or not crate_name or len(crate_name) > 300:
        raise PocketError("Supply an exact bounded crate name")
    library = Path(library_path or "~/Library/Application Support/Serato/Library/master.sqlite").expanduser()
    try:
        with sqlite3.connect("file:" + quote(str(library.resolve())) + "?mode=ro", uri=True) as database:
            database.row_factory = sqlite3.Row
            containers = database.execute("select id from container where name=?", (crate_name,)).fetchall()
            if len(containers) != 1:
                raise PocketError("The crate name must identify exactly one Serato container")
            rows = database.execute(
                """select a.id,a.artist,a.name,a.bpm,a.key,a.length_ms,
                       a.portable_id,ca.list_order from location_container lc
                       join container_asset ca on ca.location_container_id=lc.id
                       join asset a on a.id=ca.asset_id
                       where lc.container_id=? order by ca.list_order""",
                (containers[0]["id"],),
            ).fetchall()
    except sqlite3.Error as exc:
        raise PocketError("Unsupported or unavailable Serato library; opened read-only") from exc
    if not 1 <= len(rows) <= MAX_TRACKS:
        raise PocketError("The first Shuttle profile supports local crates with 1–64 records")
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    media = output / "recordings"
    media.mkdir()
    assets, tracks = [], []
    for index, row in enumerate(rows):
        source = Path("/" + row["portable_id"].lstrip("/"))
        if not source.is_file():
            raise PocketError("Every crate entry must reference an available local recording")
        frozen = media / (str(index + 1).zfill(3) + source.suffix)
        _clone(source, frozen)
        pcm = _pcm_identity(frozen)
        if _pcm_identity(source) != pcm:
            raise PocketError("Source changed while its immutable snapshot was prepared")
        track_id = "serato-" + str(row["id"]) + "-" + str(index)
        profile = {
            "provenance": "measured",
            "notes": "Serato library analysis; no audition or beat-one claim",
        }
        if row["bpm"] and 20 <= row["bpm"] <= 400:
            profile["bpm"] = row["bpm"]
        tracks.append(
            {
                "track_id": track_id,
                "title": row["name"],
                "artists": [row["artist"]],
                "catalog_source": "user_list",
                "local_path": str(frozen),
                "profile": profile,
            }
        )
        assets.append(
            {
                "track_id": track_id,
                "index": index,
                "title": row["name"],
                "artist": row["artist"],
                "duration_seconds": row["length_ms"] / 1000,
                "native_path": str(source),
                "snapshot_path": str(frozen),
                "pcm": pcm,
                "snapshot_sha256": _hash(frozen),
            }
        )
    bag = create_record_bag(tracks, str(output / "bag"), crate_name)
    prepared = prepare_session(
        bag["handle"], str(output / "whisker"), intent=intent or {"direction": "explore"}
    )
    session = {
        "schema": "pocket.shuttle-session/v1",
        "session_id": secrets.token_hex(16),
        "crate_name": crate_name,
        "library_path": str(library.resolve()),
        "assets": assets,
        "bag": bag["handle"],
        "whisker_dir": prepared["session_dir"],
        "runtime_dir": str(Path(runtime_dir).expanduser().resolve()),
    }
    _seal(output / "shuttle.json", session)
    result = _rpc(
        session["runtime_dir"],
        {"op": "register", "session_id": session["session_id"], "assets": assets, "crate_name": crate_name},
    )
    return {
        "schema": "pocket.shuttle-prepared/v1",
        "session_dir": str(output),
        "crate_name": crate_name,
        "track_count": len(assets),
        "bridge": result,
        "status": "prepared_unowned",
        "running_order": None,
    }


def shuttle_observe(session_dir: str) -> dict:
    """Read fresh native deck state and master signal; never reuses a failed read."""
    s = _session(session_dir)
    return _rpc(s["runtime_dir"], {"op": "observe", "session_id": s["session_id"]})


def shuttle_options(session_dir: str, limit: int = 6, intent: dict | None = None) -> dict:
    """Propose records from this crate and actual current playback; no deck writes."""
    s = _session(session_dir)
    observation = shuttle_observe(session_dir)
    history = session_snapshot(s["whisker_dir"])
    bag = load_record_bag(s["bag"])
    playing = [d.get("track_id") for d in observation.get("decks", {}).values() if d.get("playing")]
    current = playing[0] if len(playing) == 1 else history["current_track_id"]
    options = rank_next_tracks(
        bag["tracks"],
        current_track_id=current,
        played_ids=list(dict.fromkeys(history["played_ids"] + history["skipped_ids"])),
        intent=intent or history["intent"],
        limit=limit,
    )
    return {
        "schema": "pocket.shuttle-options/v1",
        "observation": observation,
        "options": options,
        "history_revision": history["revision"],
        "scope": "proposals_no_order_or_audition",
    }


def shuttle_act(session_dir: str, expected_revision: int, request_id: str, action: ShuttleAction) -> dict:
    """Claim/release, load an exact track_id, play/pause, gain (0–64; up to 4s), loop (8/16/null), or sync. Requires current revision and a new request ID; repeated identical IDs inspect the retained result."""
    s = _session(session_dir)
    if type(expected_revision) is not int or expected_revision < 0:
        raise PocketError("expected_revision must be a nonnegative integer")
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
        raise PocketError("Supply a bounded unique request_id")
    result = _rpc(
        s["runtime_dir"],
        {
            "op": "act",
            "session_id": s["session_id"],
            "expected_revision": expected_revision,
            "request_id": request_id,
            "action": action,
        },
    )
    if result.get("status") == "verified" and not result.get("replayed") and action.get("kind") == "play":
        track_id = result.get("track_id")
        if track_id:
            history = session_snapshot(s["whisker_dir"])
            if history["current_track_id"] != track_id:
                try:
                    update_session(
                        s["whisker_dir"],
                        expected_revision=history["revision"],
                        expected_sha256=history["sha256"],
                        action="choose",
                        track_id=track_id,
                        note="Agent control: observed transport started; not a listening verdict",
                    )
                except PocketError as exc:
                    result = {**result, "history_warning": str(exc)}
    return result


def shuttle_request_status(session_dir: str, request_id: str) -> dict:
    """Inspect a retained native request without sending it again."""
    s = _session(session_dir)
    return _rpc(
        s["runtime_dir"], {"op": "request_status", "request_id": request_id, "session_id": s["session_id"]}
    )
