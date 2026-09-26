# SPDX-License-Identifier: AGPL-3.0-only
"""Declared browser previews of exact practice renders.

A preview is a separately identified derivative for a browser player, never a
musical edit, baseline or envelope parent. The only profile encodes decoded
parent samples as original-rate PCM16 with nearest rounding (ties to even) and
no dither. Values that do not round into PCM16 are refused, never clamped or
normalized. Frames map one-to-one to the exact parent render.
"""
from __future__ import annotations

import io
import struct
from typing import Literal

import numpy as np
import soundfile as sf

from . import __version__
from .artifact_store import (
    ArtifactHandle,
    _verify_handles,
    call_memo,
    canonical_bytes,
    per_call_verification,
    put_bytes,
    put_record,
    read_bytes,
    read_record,
    run_request,
)
from .audio_evidence import measure_audio
from .coordinates import fields, text
from .errors import PocketError
from .musical_context import context_receipt
from .practice_audio import MAX_OUTPUT_BYTES, RENDER_SCHEMA, load_practice_audio

SCHEMA = "pocket.practice-preview/v1"
AUDIO_SCHEMA = "pocket.practice-preview-audio/v1"
PROFILE = "browser-pcm16-original-rate/v1"
IMPLEMENTATION = "pocket.practice-preview/v1"
ENVELOPE_SCHEMA = "pocket.practice-envelope/v1"
PARENT_SCHEMAS = (RENDER_SCHEMA, ENVELOPE_SCHEMA)
FULL_SCALE = 32768
# Rates whose PCM16 WAV bytes are qualified against a browser decoder. Other
# rates are refused rather than resampled.
SUPPORTED_RATES = (8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000, 88200, 96000)
FORMAT = {"container": "RIFF/WAVE", "encoding": "pcm_s16le", "header": "canonical-44-byte/v1",
          "media_type": "audio/wav"}
CONVERSION = {"algorithm": "pcm16-nearest-even/v1", "input": "decoded_float64_parent_samples",
              "scale": FULL_SCALE, "rounding": "nearest_ties_to_even", "dither": "none",
              "representable_range": [-FULL_SCALE, FULL_SCALE - 1], "out_of_range": "refuse",
              "nonfinite": "refuse", "clamping": False, "normalization": False, "gain": False,
              "resampling": False, "channel_conversion": False, "time_stretch": False, "fades": False,
              "frame_count_changed": False}
FRAME_MAPPING = {"kind": "identity", "parent_offset_frames": 0}
PLAYBACK = {"provider_playback": False, "device_output_verified": False,
            "scope": "Verified bytes are the encoded input to a player; the browser or device may resample "
                     "or process output."}
COVERAGE = {"profile": PROFILE, "provider_playback": False, "device_output_verified": False,
            "native_execution": False, "human_listening": "not_performed"}


def quantize_pcm16(samples):
    """Return exact PCM16 integers and error statistics, or refuse unrepresentable values."""
    if not isinstance(samples, np.ndarray) or samples.dtype != np.float64 or samples.ndim != 2:
        raise PocketError("Preview conversion requires decoded float64 frames")
    finite = np.isfinite(samples)
    if not finite.all():
        frame, channel = map(int, np.argwhere(~finite)[0])
        raise PocketError(f"Preview refuses nonfinite sample at frame {frame}, channel {channel}",
                          code="unsupported_profile")
    # Scale only bounded values so the power-of-two product stays exact and finite;
    # anything larger is out of range anyway. One mask names the first bad sample.
    huge = np.abs(samples) > 2
    scaled = np.where(huge, 0.0, samples) * FULL_SCALE
    rounded = np.rint(scaled)
    bad = huge | (rounded < -FULL_SCALE) | (rounded > FULL_SCALE - 1)
    if bad.any():
        frame, channel = map(int, np.argwhere(bad)[0])
        raise PocketError(
            f"Preview sample at frame {frame}, channel {channel} does not round into PCM16 "
            f"[-{FULL_SCALE}, {FULL_SCALE - 1}]; this profile never clamps or normalizes. "
            "Keep the exact render, or declare a different processing experiment explicitly.",
            code="unsupported_profile")
    # Both terms are exact, so the difference is the exact rounding error.
    error = np.abs(scaled - rounded)
    stats = {"samples": int(rounded.size), "exact_samples": int(np.count_nonzero(error == 0)),
             "max_abs_error_lsb": float(error.max()) if error.size else 0.0, "full_scale": FULL_SCALE}
    return rounded.astype("<i2"), stats


def pcm16_wav(values, sample_rate):
    """Canonical 44-byte RIFF header plus interleaved little-endian PCM16 data."""
    channels = values.shape[1]
    data = np.ascontiguousarray(values, dtype="<i2").tobytes()
    header = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
              + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
                                      sample_rate * channels * 2, channels * 2, 16)
              + b"data" + struct.pack("<I", len(data)))
    return header + data


def _parent(render, store_root):
    if not isinstance(render, dict) or render.get("artifact_schema") not in PARENT_SCHEMAS:
        raise PocketError("A preview requires an exact practice render or join-envelope render; "
                          "previews, comparisons and reports are not preview parents", code="unsupported_profile")
    return load_practice_audio(render, store_root)


def _encode(parent, store_root):
    signal = parent["signal"]
    rate, channels, frames = signal["sample_rate"], signal["channels"], signal["frames"]
    if rate not in SUPPORTED_RATES:
        raise PocketError(f"{rate} Hz is not a qualified browser preview rate; supported: "
                          f"{', '.join(map(str, SUPPORTED_RATES))}. No resampling is performed.",
                          code="unsupported_profile")
    if channels not in (1, 2) or frames > 120 * rate:
        raise PocketError("Preview requires a mono/stereo parent of at most 120 seconds", code="unsupported_profile")
    samples, decoded_rate = sf.read(io.BytesIO(read_bytes(parent["audio"], store_root)), dtype="float64",
                                    always_2d=True)
    if decoded_rate != rate or samples.shape != (frames, channels):
        raise PocketError("Parent audio differs from its verified signal evidence", code="evidence_mismatch")
    values, stats = quantize_pcm16(samples)
    payload = pcm16_wav(values, rate)
    if len(payload) > MAX_OUTPUT_BYTES:
        raise PocketError("Preview exceeds the parent render byte bound")
    measured = measure_audio(io.BytesIO(payload), {"settings": {"sample_rate": rate, "channels": channels},
                             "expected_frames": frames, "frame_tolerance": 0, "signal_expectation": "audible"},
                             require_float=False)
    described = {**FORMAT, "sample_rate": rate, "channels": channels, "frames": frames, "bytes": len(payload)}
    return payload, described, stats, measured


@call_memo("practice_preview")
def load_practice_preview(handle, store_root):
    """Rebuild expected bytes from the fully revalidated parent; a rehashed manifest is not trusted."""
    _verify_handles(handle, store_root)
    record = read_record(handle, store_root, SCHEMA)
    fields(record, {"schema", "profile", "parent", "parent_audio", "audio", "format", "frame_mapping",
                    "conversion", "quantization", "input_signal", "signal", "provider", "playback",
                    "listening", "musical_verdict"})
    fields(record["provider"], {"implementation", "package_version"})
    text(record["provider"]["package_version"], "preview provider version", 80)
    if (record["profile"] != PROFILE or record["provider"]["implementation"] != IMPLEMENTATION
            or canonical_bytes(record["conversion"]) != canonical_bytes(CONVERSION)
            or canonical_bytes(record["frame_mapping"]) != canonical_bytes(FRAME_MAPPING)
            or canonical_bytes(record["playback"]) != canonical_bytes(PLAYBACK)
            or record["listening"] != "not_reviewed" or record["musical_verdict"] is not None):
        raise PocketError("Preview profile declaration mismatch", code="evidence_mismatch")
    parent = _parent(record["parent"], store_root)
    if not isinstance(record["audio"], dict) or record["audio"].get("artifact_schema") != AUDIO_SCHEMA:
        raise PocketError("Preview requires an identified preview audio artifact", code="evidence_mismatch")
    payload, described, stats, measured = _encode(parent, store_root)
    if (read_bytes(record["audio"], store_root) != payload
            or canonical_bytes(record["parent_audio"]) != canonical_bytes(parent["audio"])
            or canonical_bytes(record["format"]) != canonical_bytes(described)
            or canonical_bytes(record["quantization"]) != canonical_bytes(stats)
            or canonical_bytes(record["input_signal"]) != canonical_bytes(parent["signal"])
            or canonical_bytes(record["signal"]) != canonical_bytes(measured)):
        raise PocketError("Preview bytes or evidence differ from its exact parent", code="evidence_mismatch")
    return record, parent


@per_call_verification
def practice_preview(store_root: str, request_id: str, render: ArtifactHandle,
                     profile: Literal["browser-pcm16-original-rate/v1"]) -> dict:
    """Create a declared original-rate PCM16 preview of an exact practice render; no other DSP."""
    inputs = {"render": render, "profile": profile}
    def work():
        if profile != PROFILE:
            raise PocketError("Unknown preview profile; alternative encodings need their own profile",
                              code="unsupported_profile")
        parent = _parent(render, store_root)
        payload, described, stats, measured = _encode(parent, store_root)
        audio = put_bytes(payload, store_root, "preview.wav", AUDIO_SCHEMA)
        record = {"schema": SCHEMA, "profile": PROFILE, "parent": render, "parent_audio": parent["audio"],
                  "audio": audio, "format": described, "frame_mapping": FRAME_MAPPING, "conversion": CONVERSION,
                  "quantization": stats, "input_signal": parent["signal"], "signal": measured,
                  "provider": {"implementation": IMPLEMENTATION, "package_version": __version__},
                  "playback": PLAYBACK, "listening": "not_reviewed", "musical_verdict": None}
        handle = put_record(record, store_root)
        load_practice_preview(handle, store_root)
        warnings = [] if parent["signal"]["usable_for_expectation"] else ["Parent: " + parent["signal"]["disposition"]]
        if not measured["usable_for_expectation"]:
            warnings.append("Preview: " + measured["disposition"])
        return context_receipt(request_id=request_id, artifacts={"preview": handle, "audio": audio},
                               change_summary={"frames": described["frames"], "channels": described["channels"],
                                               "sample_rate": described["sample_rate"],
                                               "exact_samples": stats["exact_samples"],
                                               "rounded_samples": stats["samples"] - stats["exact_samples"]},
                               coverage={**COVERAGE, "parent_profile": parent["processing"]["profile"],
                                         "parent_samples_exact": stats["exact_samples"] == stats["samples"],
                                         "max_abs_error_lsb": stats["max_abs_error_lsb"]},
                               warnings=warnings)
    return run_request(store_root, request_id, "practice_preview", inputs, work)
