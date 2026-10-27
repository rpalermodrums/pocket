# SPDX-License-Identifier: AGPL-3.0-only
"""Baste: fresh, read-only observations from the local Max for Live device.

Connection descriptors are not observations. Nothing here accepts a saved session
handle, replays a prior observation or writes into Live.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import secrets
import shutil
import struct
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from .errors import PocketError
from .stitch import _publish, _staging

SCHEMA = "pocket.live-observation/v1"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def _live_running() -> bool:
    """OS process presence only; this does not establish device availability."""
    try:
        result = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True,
                                text=True, check=True, timeout=3)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PocketError("Cannot determine whether Ableton is running on this platform") from exc
    return any(Path(line.strip()).name == "Live" or Path(line.strip()).name.startswith("Ableton Live")
               for line in result.stdout.splitlines())


def _bridge_directory(bridge_dir: str | None) -> Path:
    return Path(bridge_dir or os.environ.get("POCKET_BASTE_DIR", "~/.pocket/baste")).expanduser().resolve()


def _request(descriptor: dict, route: str, timeout: float, payload: dict | None = None) -> dict:
    port = descriptor.get("port")
    token = descriptor.get("token")
    if (descriptor.get("schema") != "pocket.baste-connection/v1"
            or descriptor.get("host") != "127.0.0.1"
            or type(port) is not int or not 1 <= port <= 65535
            or not isinstance(token, str) or len(token) != 64
            or any(c not in "0123456789abcdef" for c in token)):
        raise PocketError("Invalid Baste loopback connection descriptor")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=max(0.01, timeout))
    try:
        body = json.dumps(payload, allow_nan=False) if payload is not None else None
        connection.request("POST" if payload is not None else "GET", route, body,
                           {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise PocketError("Baste response exceeds the bounded transport limit")
        if response.status != 200:
            raise PocketError(f"Baste bridge returned HTTP {response.status}")
        record = json.loads(raw)
        if not isinstance(record, dict):
            raise PocketError("Baste bridge returned a non-object response")
        return record
    finally:
        connection.close()


def observe_live(*, timeout_seconds: float = 35, bridge_dir: str | None = None) -> dict:
    """Read the currently open Live session once; no save, mutation or cached result.

    Install/load the Baste device first. Runtime object IDs describe this read only.
    A sequential observation is neither atomic nor valid evidence of later state.
    """
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0.1 <= timeout_seconds <= 60):
        raise PocketError("timeout_seconds must be between 0.1 and 60")
    started = time.monotonic()
    request_id = secrets.token_hex(16)
    header = {"schema": SCHEMA, "request_id": request_id,
              "requested_at": datetime.now(UTC).isoformat()}

    def finish(disposition, **fields):
        return {**header, "received_at": datetime.now(UTC).isoformat(),
                "round_trip_ms": round((time.monotonic() - started) * 1000, 3),
                "disposition": disposition, "observation": None, **fields,
                "identity_scope": "runtime_ids_within_this_observation_only",
                "consistency": "sequential_read_not_atomic_or_durable",
                "musical_verdict": None}

    if not _live_running():
        return finish("ableton_not_running")
    folder = _bridge_directory(bridge_dir)
    descriptors = sorted(folder.glob("connection-*.json")) if folder.is_dir() else []
    if len(descriptors) > 64:
        return finish("bridge_error", reason="Too many connection descriptors; remove stale device records")
    active, failures = [], []
    deadline = started + timeout_seconds
    for path in descriptors:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return finish("observation_timeout")
        try:
            if path.stat().st_size > 4096:
                raise PocketError("Oversized Baste connection descriptor")
            descriptor = json.loads(path.read_text())
            if not isinstance(descriptor, dict):
                raise PocketError("Invalid Baste connection descriptor")
            health = _request(descriptor, "/health", min(0.5, remaining))
            if (health.get("instance_id") != descriptor.get("instance_id")
                    or health.get("schema") != "pocket.baste-health/v1"):
                raise PocketError("Baste connection identity mismatch")
            active.append(descriptor)
        except ConnectionRefusedError:
            continue  # A dead device's descriptor is not an observation.
        except (OSError, ValueError, PocketError, http.client.HTTPException) as exc:
            failures.append(type(exc).__name__)
    if failures:
        return finish("bridge_error", reason="Connection discovery failed", failures=failures)
    if not active:
        return finish("device_not_loaded")
    if len(active) != 1:
        return finish("ambiguous_devices", active_devices=len(active))
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return finish("observation_timeout")
    try:
        record = _request(active[0], "/observe", remaining,
                          {"request_id": request_id, "timeout_ms": int(remaining * 1000)})
        if record.get("request_id") != request_id or record.get("schema") != SCHEMA:
            raise PocketError("Baste response does not belong to this fresh request")
        disposition = record.get("disposition")
        if disposition not in {"ok", "path_invalid", "unsupported_property", "read_limit", "device_not_loaded",
                               "observation_timeout", "bridge_error"}:
            raise PocketError("Unknown Baste response disposition")
        observation = record.get("observation")
        if disposition == "ok" and (not isinstance(observation, dict)
                                     or not isinstance(observation.get("tracks"), list)
                                     or not isinstance(observation.get("return_tracks"), list)
                                     or not isinstance(observation.get("main_track"), dict)):
            raise PocketError("Incomplete Baste observation")
        # Release counts describe the device's cleanup of its own LiveAPI objects,
        # reported for failures too. A shortfall leaves the observation intact.
        return finish(disposition, observation=observation if disposition == "ok" else None,
                      read_started_at=record.get("read_started_at"), read_ended_at=record.get("read_ended_at"),
                      read_elapsed_ms=record.get("read_elapsed_ms"), error=record.get("error"),
                      live_objects_created=record.get("live_objects_created"),
                      live_objects_released=record.get("live_objects_released"),
                      release_elapsed_ms=record.get("release_elapsed_ms"),
                      release_error=record.get("release_error"))
    except TimeoutError:
        return finish("observation_timeout")
    except (OSError, ValueError, PocketError, http.client.HTTPException) as exc:
        return finish("bridge_error", reason=str(exc))


def build_baste_device(output_dir: str) -> dict:
    """Build an editable Baste audio-effect device in a new directory; does not load it.

    Keep the .amxd and all adjacent scripts together. Live/Max supplies node.script
    and max-api; no npm installation, model or remote service is required.
    """
    destination = Path(output_dir).expanduser().resolve()
    stage = _staging(destination)
    sources = Path(__file__).parent / "devices" / "baste"
    try:
        for name in ("baste_reader.js", "baste_device.js", "baste_bridge.js", "Baste.maxpat"):
            shutil.copyfile(sources / name, stage / name)
        patch = (stage / "Baste.maxpat").read_bytes() + b"\x00"
        # Max's ampf audio-effect container. The editable patch and JS are shipped
        # as source; the generated binary is an install artifact, not a fixture.
        header = b"ampf" + struct.pack("<I", 4) + b"aaaa" + b"meta" + struct.pack("<II", 4, 0)
        (stage / "Baste.amxd").write_bytes(header + b"ptch" + struct.pack("<I", len(patch)) + patch)
        _publish(stage, destination)
        return {"schema": "pocket.baste-device/v1", "device_path": str(destination / "Baste.amxd"),
                "device_dir": str(destination), "loaded_in_live": False,
                "instructions": "Load Baste.amxd on an audio-capable track; keep its adjacent scripts together"}
    finally:
        if stage.exists():
            shutil.rmtree(stage)
