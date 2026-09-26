# SPDX-License-Identifier: AGPL-3.0-only
"""Exact source identity; no song-title or filename-based identity shortcuts."""

from __future__ import annotations

import hashlib
from pathlib import Path

import soundfile as sf

from .errors import PocketError


def _stamp(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def sha256_file(path: str | Path) -> str:
    """Hash a stable file and reject a file that changed while it was read."""
    source = Path(path).expanduser().resolve()
    try:
        before = _stamp(source)
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if _stamp(source) != before:
            raise PocketError(f"File changed while hashing: {source.name}")
        return digest.hexdigest()
    except OSError as exc:
        raise PocketError(f"Cannot read {source}: {exc.strerror or exc}") from exc


def identify_audio(path: str | Path) -> dict:
    """Return stable byte identity and decoded metadata, without inferring quality."""
    source = Path(path).expanduser().resolve()
    try:
        before = _stamp(source)
        info = sf.info(source)
        digest = sha256_file(source)
        if _stamp(source) != before:
            raise PocketError(f"Audio changed during identification: {source.name}")
        if info.frames <= 0 or info.samplerate <= 0 or info.channels <= 0:
            raise PocketError("Audio must have positive frames, sample rate and channel count")
    except (OSError, RuntimeError) as exc:
        raise PocketError(f"Cannot identify audio {source.name}: {exc}") from exc
    return {
        "schema": "pocket.asset/v1",
        "asset_id": f"sha256:{digest}",
        "sha256": digest,
        "filename": source.name,
        "local_path": str(source),
        "frames": int(info.frames),
        "sample_rate": int(info.samplerate),
        "channels": int(info.channels),
        "duration_seconds": info.frames / info.samplerate,
        "format": info.format,
        "subtype": info.subtype,
        "identity_basis": "file_bytes",
        "quality_inference": None,
    }
