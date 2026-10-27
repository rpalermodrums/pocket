# SPDX-License-Identifier: AGPL-3.0-only
"""Reusable decoded-signal evidence, independent of native candidates and hosts."""
import math

import numpy as np
import soundfile as sf

from .errors import PocketError


def validate_feedback_report(interval_frames, frames, actor, actor_kind, note, decision):
    """Shared attribution/interval rules; caller supplies an exact audio identity."""
    if not isinstance(interval_frames, list) or len(interval_frames) != 2:
        raise PocketError("Feedback requires exact start/end frames")
    if any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 2**63 - 1
           for v in interval_frames):
        raise PocketError("interval frame must be an integer from 0 through 9223372036854775807")
    start, end = interval_frames
    if end <= start or end > frames:
        raise PocketError("Feedback interval is outside this exact render")
    for value, label, limit in ((actor, "actor", 240), (note, "note", 8000)):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise PocketError(f"{label} must be nonempty text, at most {limit} characters")
    if actor_kind not in ("human", "agent"):
        raise PocketError("Feedback actor_kind must be human or agent")
    if decision not in (None, "keep", "revise", "reject", "no_addition"):
        raise PocketError("Unknown attributed decision")


def measure_audio(source, plan, *, require_float=True):
    """Decode signal evidence; plan supplies expectations, never musical approval."""
    frames = nonfinite = nonzero = overload = 0
    peak = squared = 0.0
    try:
        with sf.SoundFile(source) as audio:
            if audio.format not in {"WAV", "WAVEX"} or (require_float and audio.subtype not in {"FLOAT", "DOUBLE"}):
                raise PocketError("Native evidence requires completed float WAV")
            rate, channel_count = audio.samplerate, audio.channels
            declared_frames = audio.frames
            for block in audio.blocks(blocksize=65536, dtype="float64", always_2d=True):
                frames += len(block)
                finite = np.isfinite(block)
                nonfinite += int(np.count_nonzero(~finite))
                safe = np.where(finite, block, 0)
                nonzero += int(np.count_nonzero(safe))
                overload += int(np.count_nonzero(np.abs(safe) >= 1))
                peak = max(peak, float(np.max(np.abs(safe))) if len(block) else 0)
                squared += float(np.sum(safe * safe))
    except (RuntimeError, OSError) as error:
        raise PocketError(f"Cannot completely decode render: {error}") from error
    if frames != declared_frames or frames <= 0:
        raise PocketError("Render is empty or incomplete")
    rms = math.sqrt(squared / (frames * channel_count))
    disposition = ("nonfinite_audio" if nonfinite else "sample_overload" if overload else
                   "wrong_format" if (rate != plan["settings"]["sample_rate"] or
                                      channel_count != plan["settings"]["channels"]) else
                   "wrong_duration" if abs(frames - plan["expected_frames"]) > plan["frame_tolerance"] else
                   "intentional_silence" if plan["signal_expectation"] == "intentional_silence" and rms <= 1e-6 else
                   "unexpected_audio" if plan["signal_expectation"] == "intentional_silence" else
                   "unexpected_silence" if nonzero == 0 else
                   "unexpected_near_silence" if rms <= 1e-6 else "usable_signal")
    return {"frames": frames, "sample_rate": rate, "channels": channel_count,
            "sample_peak": peak, "rms": rms, "nonfinite_samples": nonfinite,
            "nonzero_samples": nonzero, "sample_overload_count": overload,
            "disposition": disposition, "usable_for_expectation": disposition in {"usable_signal", "intentional_silence"},
            "true_peak": None, "loudness_lufs": None}
