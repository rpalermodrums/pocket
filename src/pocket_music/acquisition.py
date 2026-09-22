"""Optional bounded yt-dlp acquisition, with immutable originals and explicit derivatives."""

from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import soundfile as sf

from .assets import identify_audio, sha256_file
from .errors import PocketError
from .spotify_bridge import _checked_plan, _new_dir
from .stitch import _integer, _now, _seal, _text

_SCHEMA = "pocket.acquisition-plan/v1"
_COMMON = ["--ignore-config", "--no-plugin-dirs", "--no-cache-dir"]


def _url(value: str) -> str:
    value = _text(value, "source_url", 4000)
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise PocketError("source_url must be an HTTP(S) URL without embedded credentials")
    if parsed.hostname.lower() in ("spotify.com", "open.spotify.com", "api.spotify.com"):
        raise PocketError("Spotify streaming URLs are not acquisition sources")
    return value


def _run(args: list[str], *, timeout: int = 180) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PocketError("Optional media executable unavailable or operation timed out") from exc


def _diagnostic(value: str) -> str:
    return re.sub(r"https?://\S+", "[URL omitted]", value[-8000:])


def _json_command(args: list[str], *, timeout: int = 180) -> dict:
    process = _run(args, timeout=timeout)
    if process.returncode:
        raise PocketError("Media metadata command failed; no recording was published")
    if len(process.stdout) > 8_000_000:
        raise PocketError("Metadata exceeds bounded response size")
    try:
        data = json.loads(process.stdout)
    except (ValueError, TypeError) as exc:
        raise PocketError("Media metadata is malformed") from exc
    if not isinstance(data, dict):
        raise PocketError("Media metadata must be an object")
    return data


def discover_sources(query: str, *, limit: int = 5, executable: str = "yt-dlp") -> dict:
    """Return bounded YouTube metadata candidates. Never download or choose an edition."""
    query = _text(query, "query", 300)
    _integer(limit, "limit", 1)
    if limit > 10:
        raise PocketError("Discovery limit must be at most 10")
    info = _json_command([executable, *_COMMON, "--flat-playlist", "--skip-download",
                          "--dump-single-json", "--", f"ytsearch{limit}:{query}"])
    entries = info.get("entries")
    if not isinstance(entries, list):
        raise PocketError("Discovery returned malformed candidates")
    candidates = []
    for item in entries[:limit]:
        if not isinstance(item, dict):
            continue
        url = item.get("webpage_url") or item.get("url")
        try:
            url = _url(url)
        except PocketError:
            continue
        candidates.append({k: item.get(k) for k in ("id", "title", "channel", "uploader", "duration")}
                          | {"source_url": url, "version_identity": "unverified"})
    return {"schema": "pocket.source-discovery/v1", "query": query, "candidates": candidates,
            "downloaded": False, "automatic_selection": False}


def _format_id(value: str) -> str:
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value)
            or value in {"best", "worst", "bestaudio", "worstaudio", "bestvideo", "worstvideo", "all"}):
        raise PocketError("format_id must be one concrete format ID, not a selection expression")
    return value


def inspect_source_formats(source_url: str, *, executable: str = "yt-dlp", limit: int = 32) -> dict:
    """Inspect bounded audio formats for an explicit retry; expose no signed media URLs."""
    source_url = _url(source_url)
    _integer(limit, "limit", 1)
    if limit > 64:
        raise PocketError("Format inspection limit must be at most 64")
    info = _json_command([executable, *_COMMON, "--no-playlist", "--skip-download",
                          "--dump-single-json", "--", source_url])
    if info.get("entries") is not None or info.get("is_live"):
        raise PocketError("Inspect one finite recording source")
    rows = info.get("formats")
    if not isinstance(rows, list):
        rows = [info]
    formats = []
    for row in rows:
        if not isinstance(row, dict) or row.get("acodec") in (None, "none"):
            continue
        try:
            ident = _format_id(row.get("format_id"))
        except PocketError:
            continue
        fields = {key: row.get(key) for key in
                  ("ext", "acodec", "vcodec", "abr", "asr", "audio_channels", "filesize", "filesize_approx")
                  if isinstance(row.get(key), (str, int, float, type(None)))}
        fields = {key: (None if isinstance(value, float) and not math.isfinite(value) else value)
                  for key, value in fields.items()}
        formats.append({"format_id": ident, **fields})
    return {"schema": "pocket.source-formats/v1", "source_url": source_url,
            "source": _metadata(info), "formats": formats[:limit],
            "total_audio_formats": len(formats), "truncated": len(formats) > limit,
            "downloaded": False, "quality": "Reported encoding metadata; not a fidelity ranking"}


def plan_acquisition(
    source_url: str, output_dir: str, *, version_note: str, authorization_note: str,
    expected_source_id: str | None = None, sample_rate: int | None = None,
    channels: int | None = None, format_id: str | None = None,
) -> dict:
    """Seal an explicitly selected URL. None rate/channels preserves decoded source format."""
    source_url = _url(source_url)
    if format_id is not None:
        _format_id(format_id)
    if sample_rate is not None:
        _integer(sample_rate, "sample_rate", 8000)
        if sample_rate > 192000:
            raise PocketError("sample_rate must be at most 192000")
    if channels is not None and (type(channels) is not int or channels not in (1, 2)):
        raise PocketError("Explicit channels must be 1 or 2")
    plan = {"schema": _SCHEMA, "created_at": _now(), "source_url": source_url,
            "version_note": _text(version_note, "version_note", 2000),
            "authorization_note": _text(authorization_note, "authorization_note", 2000),
            "expected_source_id": None if expected_source_id is None else
            _text(expected_source_id, "expected_source_id", 200),
            "sample_rate": sample_rate, "channels": channels,
            "format_id": format_id,
            "format_policy": ("explicit inspected format; verify exact ID before download" if format_id else
                              "bestaudio/best; pin selected accessible format before download"),
            "maximum_seconds": 1800, "maximum_original_bytes": 512 * 1024 * 1024,
            "gain_db": 0, "fades": False, "normalization": False,
            "version_identity": "unverified"}
    folder = _new_dir(output_dir)
    digest = _seal(folder, "plan.json", plan)
    return {"schema": "pocket.acquisition-plan-handle/v1", "plan_dir": str(folder),
            "path": str(folder / "plan.json"), "sha256": digest,
            "status": "planned", "version_identity": "unverified"}


def _metadata(info: dict) -> dict:
    keys = ("id", "title", "channel", "channel_id", "uploader", "webpage_url", "duration",
            "upload_date", "format_id", "format", "acodec", "abr", "asr", "audio_channels", "ext")
    # Do not persist signed CDN URLs, request headers, cookies or the full extractor dump.
    return {k: info.get(k) for k in keys if isinstance(info.get(k), (str, int, float, bool, type(None)))}


def _probe(path: Path, executable: str) -> dict:
    info = _json_command([executable, "-v", "error", "-show_format", "-show_streams", "-of", "json",
                          str(path)])
    streams = [s for s in info.get("streams", []) if isinstance(s, dict) and s.get("codec_type") == "audio"]
    if len(streams) != 1:
        raise PocketError("Source must contain exactly one unambiguous audio stream")
    stream = streams[0]
    try:
        rate, channels = int(stream["sample_rate"]), int(stream["channels"])
        duration = float(stream.get("duration") or info.get("format", {}).get("duration"))
    except (KeyError, ValueError, TypeError) as exc:
        raise PocketError("Source audio clock/channel/duration metadata is incomplete") from exc
    if not 8000 <= rate <= 192000 or not 1 <= channels <= 8 or not 0 < duration <= 1800:
        raise PocketError("Source exceeds supported rate/channel/duration bounds")
    return {"sample_rate": rate, "channels": channels, "duration_seconds": duration,
            "codec": stream.get("codec_name"), "container": info.get("format", {}).get("format_name"),
            "bit_rate": stream.get("bit_rate") or info.get("format", {}).get("bit_rate")}


def _scan(path: Path) -> dict:
    frames = nonfinite = nonzero = overs = 0
    peak = squared = 0.0
    with sf.SoundFile(path) as source:
        channels, rate = source.channels, source.samplerate
        for block in source.blocks(blocksize=65536, dtype="float64", always_2d=True):
            frames += len(block)
            finite = np.isfinite(block)
            nonfinite += int((~finite).sum())
            values = block[finite]
            nonzero += int(np.count_nonzero(values))
            overs += int(np.count_nonzero(np.abs(values) >= 1))
            peak = max(peak, float(np.max(np.abs(values), initial=0)))
            squared += float(np.dot(values, values))
    rms = math.sqrt(squared / max(1, frames * channels))
    reasons = []
    if frames == 0 or nonfinite:
        reasons.append("empty_or_nonfinite")
    if not nonzero:
        reasons.append("digital_silence")
    elif rms <= 0.001:
        reasons.append("near_silence_rms_at_or_below_minus_60_dbfs")
    if overs:
        reasons.append("samples_at_or_above_full_scale")
    return {"frames": frames, "sample_rate": rate, "channels": channels,
            "nonfinite_samples": nonfinite, "nonzero_samples": nonzero, "full_scale_samples": overs,
            "sample_peak_dbfs": 20 * math.log10(peak) if peak else None,
            "rms_dbfs": 20 * math.log10(rms) if rms else None,
            "status": "usable" if not reasons else "needs_review", "reasons": reasons,
            "true_peak_measured": False, "auditioned": False}


def acquire_source(
    plan_dir: str, *, expected_plan_sha256: str, output_dir: str,
    yt_dlp_executable: str = "yt-dlp", ffmpeg_executable: str = "ffmpeg",
    ffprobe_executable: str = "ffprobe",
) -> dict:
    """Download one approved URL, retain bytes, fully decode and publish an honest receipt."""
    plan_folder = Path(plan_dir).expanduser().resolve()
    plan = _checked_plan(plan_folder, expected_plan_sha256, _SCHEMA)
    _url(plan["source_url"])
    folder = _new_dir(output_dir)
    stage = folder / ".staging"
    stage.mkdir()
    result = {"schema": "pocket.acquisition-receipt/v1", "at": _now(),
              "plan_sha256": expected_plan_sha256, "source_url": plan["source_url"],
              "version_note": plan["version_note"], "version_identity": "unverified",
              "authorization_note": plan["authorization_note"], "ready_for_analysis": False,
              "ready_as_requested_recording": False, "auditioned": False}
    try:
        version = _run([yt_dlp_executable, "--ignore-config", "--version"])
        if version.returncode or not version.stdout.strip():
            raise PocketError("Cannot establish yt-dlp runtime version")
        result["yt_dlp_version"] = version.stdout.strip()[:200]
        for key, executable in (("ffmpeg_version", ffmpeg_executable), ("ffprobe_version", ffprobe_executable)):
            runtime = _run([executable, "-version"])
            if runtime.returncode or not runtime.stdout.strip():
                raise PocketError("Cannot establish decoder/probe runtime version")
            result[key] = runtime.stdout.splitlines()[0][:300]
        info = _json_command([yt_dlp_executable, *_COMMON, "--no-playlist", "--skip-download",
                              "--dump-single-json", "-f", plan.get("format_id") or "bestaudio/best",
                              "--", plan["source_url"]])
        if info.get("_type") in ("playlist", "multi_video") or info.get("entries") is not None:
            raise PocketError("Select one recording URL, not a playlist")
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            raise PocketError("Live/unbounded sources are unsupported")
        if plan["expected_source_id"] and info.get("id") != plan["expected_source_id"]:
            raise PocketError("Source ID differs from the approved plan")
        if not isinstance(info.get("id"), str) or not info["id"]:
            raise PocketError("Extractor did not establish a stable source ID")
        duration = info.get("duration")
        if isinstance(duration, (int, float)) and (not math.isfinite(duration) or duration > 1800):
            raise PocketError("Source exceeds 30-minute acquisition bound")
        format_id = info.get("format_id")
        if not isinstance(format_id, str) or not format_id or len(format_id) > 100:
            raise PocketError("Extractor did not establish a concrete format")
        if plan.get("format_id") is not None and format_id != plan["format_id"]:
            raise PocketError("Inspected format differs from the explicitly selected plan format")
        result["source_metadata"] = _metadata(info)
        args = [yt_dlp_executable, *_COMMON, "--no-playlist", "--no-overwrites",
                "--abort-on-unavailable-fragments", "--retries", "2", "--fragment-retries", "2",
                "--max-filesize", "512M", "--socket-timeout", "25", "--write-info-json",
                "--no-progress", "-f", format_id, "-o", str(stage / "original.%(ext)s"),
                "--", plan["source_url"]]
        downloaded = _run(args, timeout=600)
        # Store only diagnostic status, never potentially signed URL/error text from subprocesses.
        result["download_exit_code"] = downloaded.returncode
        result["download_diagnostic"] = _diagnostic(downloaded.stderr)
        if downloaded.returncode:
            raise PocketError("Download failed; partial staging files are preserved")
        originals = [p for p in stage.iterdir() if p.is_file() and p.name.startswith("original.")
                     and not p.name.endswith((".json", ".part", ".ytdl"))]
        partial = [p for p in stage.iterdir() if p.name.endswith((".part", ".ytdl"))]
        if len(originals) != 1 or partial:
            raise PocketError("Download output is absent, ambiguous or partial")
        original = originals[0]
        if not 0 < original.stat().st_size <= plan["maximum_original_bytes"]:
            raise PocketError("Original byte size is outside plan bounds")
        downloaded_info_path = stage / "original.info.json"
        if not downloaded_info_path.is_file():
            raise PocketError("Completed download metadata is missing")
        downloaded_info = json.loads(downloaded_info_path.read_text())
        if (not isinstance(downloaded_info, dict) or downloaded_info.get("id") != info.get("id")
                or downloaded_info.get("format_id") != format_id):
            raise PocketError("Downloaded source/format differs from inspected candidate")
        probe = _probe(original, ffprobe_executable)
        original_hash = sha256_file(original)
        decoded = stage / "decoded.wav"
        conversion = ["-ar", str(plan["sample_rate"])] if plan["sample_rate"] is not None else []
        conversion += ["-ac", str(plan["channels"])] if plan["channels"] is not None else []
        # Matroska supplies complete Opus packets. FFmpeg 8.0.1's redundant
        # AVParser reports a packet error on its empty EOF flush. Keep the
        # actual decoder and every diagnostic gate strict; do not suppress logs.
        packet_framed_opus = (probe["codec"] == "opus"
                             and probe["container"] in ("matroska,webm", "matroska", "webm"))
        input_options = ["-fflags", "+noparse+nofillin"] if packet_framed_opus else []
        args = [ffmpeg_executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
                "-err_detect", "explode", *input_options, "-n", "-i", str(original), "-map", "0:a:0", "-vn",
                *conversion, "-c:a", "pcm_f32le", str(decoded)]
        result["decode_configuration"] = ("packet_framed_matroska_opus_v1" if packet_framed_opus
                                          else "strict_default_v1")
        result["decode_command"] = args
        decode = _run(args, timeout=600)
        result["decode_exit_code"] = decode.returncode
        result["decode_diagnostic"] = _diagnostic(decode.stderr)
        if decode.returncode or decode.stderr.strip():
            raise PocketError("Strict full decode reported an error; staging is preserved")
        identity = identify_audio(decoded)
        signal = _scan(decoded)
        expected_rate = plan["sample_rate"] or probe["sample_rate"]
        expected_channels = plan["channels"] or probe["channels"]
        if identity["sample_rate"] != expected_rate or identity["channels"] != expected_channels:
            raise PocketError("Decoded channel/rate policy mismatch")
        if identity["frames"] != signal["frames"]:
            raise PocketError("Decoded read did not cover the complete header frame count")
        if sha256_file(decoded) != identity["sha256"]:
            raise PocketError("Decoded file changed during signal validation")
        difference = identity["duration_seconds"] - probe["duration_seconds"]
        # Container duration can include packet padding. Preserve the exact discrepancy, never trim it.
        if abs(difference) > 0.1:
            raise PocketError("Decoded duration differs from container by more than 100 ms")
        if sha256_file(original) != original_hash:
            raise PocketError("Original changed during validation")
        _checked_plan(plan_folder, expected_plan_sha256, _SCHEMA)
        original_name = original.name
        original.rename(folder / original_name)
        decoded.rename(folder / "decoded.wav")
        _seal(folder, "source-metadata.json", _metadata(downloaded_info))
        # Full extractor dumps can contain signed URLs; preserve only selected provenance metadata.
        downloaded_info_path.unlink()
        identity["local_path"] = str(folder / "decoded.wav")
        download_warnings = "WARNING" in downloaded.stderr.upper()
        result.update({"status": "completed", "original": {"path": str(folder / original_name),
                       "sha256": original_hash, "bytes": (folder / original_name).stat().st_size,
                       "probe": probe}, "decoded": identity, "signal": signal,
                       "duration_difference_seconds": difference,
                       "conversion": {"sample_rate": plan["sample_rate"], "channels": plan["channels"],
                                      "codec": "pcm_f32le", "gain_db": 0, "fades": False,
                                      "normalization": False},
                       "quality_claim": "Original accessible codec retained; float decode is not a quality upgrade",
                       "readiness_reasons": signal["reasons"] + (["download_warning_requires_review"]
                                                                   if download_warnings else []),
                       "ready_for_analysis": signal["status"] == "usable" and not download_warnings})
    except (PocketError, OSError, ValueError, RuntimeError) as exc:
        result.update({"status": "failed", "reason": str(exc), "partial_artifacts": ".staging",
                       "ready_for_analysis": False})
    digest = _seal(folder, "receipt.json", result)
    return {**result, "path": str(folder / "receipt.json"), "sha256": digest}
