"""Bounded, deterministic source evidence; never a downbeat or key authority."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import scipy
import soundfile as sf
from scipy import signal
from scipy.ndimage import median_filter

from pocket_music.assets import identify_audio
from pocket_music.errors import PocketError
from pocket_music.rhythm_continuity import analyze_phase_continuity, compare_grids

SCHEMA = "pocket.track-map/v1"
ANALYSIS_VERSION = "1.1.1"
MAX_REGION_SECONDS = 600.0
_RATE = 12000
_HOP = 120
_PITCH_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def _db(value: float) -> float | None:
    return float(20 * np.log10(value)) if value > 0 else None


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise PocketError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise PocketError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise PocketError(f"{name} must be a finite number")
    return result


def _spectrogram(audio: np.ndarray, rate: int, nfft: int, hop: int) -> tuple:
    # Average channel power, not channel samples: anti-phase stereo stays visible.
    frequency, times, spectra = signal.stft(
        audio.T, fs=rate, window="hann", nperseg=nfft,
        noverlap=nfft - hop, boundary="zeros", padded=False, axis=-1,
    )
    power = np.mean(np.abs(spectra) ** 2, axis=0)
    return frequency, times, power


def _onsets(audio: np.ndarray, rate: int, original: np.ndarray, original_rate: int,
            start_frame: int) -> tuple[list[dict], np.ndarray, np.ndarray]:
    nfft = min(512, len(audio))
    hop = min(_HOP, max(1, nfft // 4))
    frequencies, times, power = _spectrogram(audio, rate, nfft, hop)
    magnitude = np.sqrt(power)
    positive_difference = np.maximum(np.diff(magnitude, axis=1, prepend=np.zeros_like(magnitude[:, :1])), 0)
    flux = positive_difference.sum(axis=0)
    band_flux = {}
    for name, low, high in (("low_35_180_hz", 35, 180), ("mid_180_2000_hz", 180, 2000),
                            ("high_2000_6000_hz", 2000, 6001)):
        values = positive_difference[(frequencies >= low) & (frequencies < high)].sum(axis=0)
        band_flux[name] = values / max(float(values.max(initial=0)), 1e-20)
    background = median_filter(flux, size=max(3, int(.35 * rate / hop) | 1), mode="nearest")
    novelty = np.maximum(flux - background, 0)
    scale = float(novelty.max(initial=0))
    if scale <= 1e-12:
        return [], np.zeros_like(novelty), times
    novelty /= scale
    indices, _ = signal.find_peaks(novelty, height=.07, prominence=.045,
                                  distance=max(1, round(.075 * rate / hop)))
    # Refine within the original waveform using a 2 ms envelope rise. This gives
    # exact *coordinates*, not a claim that the musical attack is sample-exact.
    envelope = np.mean(original ** 2, axis=1)
    kernel = max(1, round(.002 * original_rate))
    smooth = signal.convolve(envelope, np.ones(kernel) / kernel, mode="same")
    rise = np.maximum(smooth - np.roll(smooth, kernel), 0)
    rise[:kernel] = 0
    events = []
    for index in indices:
        center = float(times[index])
        left = max(0, int((center - .028) * original_rate))
        right = min(len(original), int((center + .024) * original_rate) + 1)
        if right <= left:
            continue
        local = left + int(np.argmax(rise[left:right]))
        source_frame = start_frame + local
        events.append({
            "source_frame": source_frame,
            "source_seconds": source_frame / original_rate,
            "region_seconds": local / original_rate,
            "strength_relative": float(novelty[index]),
            "band_strengths_relative": {name: float(values[index]) for name, values in band_flux.items()},
            "spectral_frame_region_seconds": center,
        })
    # Refinement can collapse nearby candidates onto one attack.
    deduplicated = {event["source_frame"]: event for event in events}
    return sorted(deduplicated.values(), key=lambda event: event["source_frame"]), novelty, times


def _grid_fit(times: np.ndarray, weights: np.ndarray, bpm: float) -> dict | None:
    if len(times) < 5:
        return None
    period = 60 / bpm
    phases = np.remainder(times, period) / period
    histogram, _ = np.histogram(phases, bins=80, range=(0, 1), weights=weights)
    spread = np.roll(histogram, 1) + 2 * histogram + np.roll(histogram, -1)
    origin = (int(np.argmax(spread)) + .5) / 80 * period
    for _ in range(4):
        pulses = np.rint((times - origin) / period)
        residual = times - (origin + pulses * period)
        mask = np.abs(residual) <= min(.12 * period, .06)
        if mask.sum() < 4 or np.ptp(pulses[mask]) < 3:
            return None
        design = np.column_stack([pulses[mask], np.ones(mask.sum())])
        root_weights = np.sqrt(weights[mask])
        fitted_period, fitted_origin = np.linalg.lstsq(
            design * root_weights[:, None], times[mask] * root_weights, rcond=None,
        )[0]
        if abs(fitted_period / period - 1) > .08 or fitted_period <= 0:
            break
        period, origin = float(fitted_period), float(fitted_origin)
    origin %= period
    pulses = np.rint((times - origin) / period)
    residual = times - (origin + pulses * period)
    mask = np.abs(residual) <= min(.12 * period, .06)
    occupied = len(np.unique(pulses[mask]))
    span = int(np.ptp(pulses)) + 1
    fraction = float(weights[mask].sum() / weights.sum())
    occupancy = occupied / max(1, span)
    return {
        "bpm": 60 / period, "pulse_period_seconds": period,
        "region_lattice_origin_seconds": origin,
        "onset_weight_inlier_fraction": fraction,
        "occupied_pulse_fraction": occupancy,
        "onset_count_used": int(mask.sum()),
        "residual_median_ms": float(np.median(np.abs(residual[mask])) * 1000),
        "residual_p95_ms": float(np.percentile(np.abs(residual[mask]), 95) * 1000),
        "regularity_score": fraction * math.sqrt(occupancy),
    }


def _tempo(onsets: list[dict], novelty: np.ndarray, frame_times: np.ndarray,
           hint: float | None) -> tuple[dict | None, list[dict]]:
    if len(onsets) < 5 or len(frame_times) < 5 or frame_times[-1] - frame_times[0] < 3:
        return None, []
    times = np.array([onset["region_seconds"] for onset in onsets])
    weights = np.array([onset["strength_relative"] for onset in onsets])
    if times[-1] - times[0] < 2:
        return None, []
    step = float(np.median(np.diff(frame_times)))
    centered = novelty - np.mean(novelty)
    ac = signal.correlate(centered, centered, mode="full", method="fft")[len(centered) - 1:]
    ac /= np.arange(len(ac), 0, -1)
    lo, hi = max(1, int(60 / 400 / step)), min(len(ac) - 2, int(60 / 20 / step))
    if hi <= lo or ac[0] <= 1e-10:
        return None, []
    peaks, _ = signal.find_peaks(ac[lo:hi + 1])
    lags = sorted((int(index + lo) for index in peaks), key=lambda lag: ac[lag], reverse=True)[:8]
    candidates = []
    for lag in lags:
        if ac[lag] / ac[0] < .10:
            continue
        denominator = ac[lag - 1] - 2 * ac[lag] + ac[lag + 1]
        correction = .5 * (ac[lag - 1] - ac[lag + 1]) / denominator if denominator else 0
        candidates.append(60 / ((lag + float(np.clip(correction, -.5, .5))) * step))
    if not candidates:
        return None, []
    if hint is not None:
        lag = 60 / hint / step
        first, last = max(1, int(lag * .96)), min(len(ac), int(lag * 1.04) + 1)
        if first < last and float(ac[first:last].max()) / ac[0] >= .10:
            candidates.append(hint)
    fits = []
    for bpm in candidates:
        fit = _grid_fit(times, weights, bpm)
        if fit and not any(abs(fit["bpm"] / other["bpm"] - 1) < .01 for other in fits):
            fits.append(fit)
    fits.sort(key=lambda fit: fit["regularity_score"], reverse=True)
    if not fits or fits[0]["regularity_score"] < .30:
        return None, fits[:5]
    selected = fits[0]
    if hint:
        nearby = [fit for fit in fits if abs(math.log2(fit["bpm"] / hint)) < .32
                  and fit["regularity_score"] >= .6 * selected["regularity_score"]]
        if nearby:
            selected = min(nearby, key=lambda fit: abs(math.log2(fit["bpm"] / hint)))
    return selected, fits[:5]


def _rhythm(events: list[dict], novelty: np.ndarray, times: np.ndarray, duration: float,
            start_seconds: float, hint: float | None, bar_size: int) -> dict:
    selected, candidates = _tempo(events, novelty, times, hint)
    candidates = [dict(fit, source_lattice_origin_seconds=start_seconds + fit["region_lattice_origin_seconds"])
                  for fit in candidates]
    base = {"status": "insufficient_evidence", "bpm_hint": hint,
            "selected_pulse_grid": None, "tempo_candidates": candidates,
            "counting_alternatives": [], "local_windows": [], "local_counting_ambiguities": [], "drift": None,
            "bar_phase_status": "unresolved", "bar_interpretations": [],
            "bar_interpretations_reference": "selected_pulse_grid_only_not_all_acoustic_phases"}
    # A failed whole-crop lattice must not suppress evidence of a changing
    # local clock. Analyze windows independently before judging a global fit.
    window = min(8., duration)
    local = []
    local_hint = selected["bpm"] if selected else hint
    for beginning in np.arange(0., max(0., duration - window) + .001, 4.):
        end = float(beginning + window)
        local_events = [dict(event, region_seconds=event["region_seconds"] - beginning)
                        for event in events if beginning <= event["region_seconds"] < end]
        mask = (times >= beginning) & (times < end)
        fit, _ = _tempo(local_events, novelty[mask], times[mask] - beginning, local_hint)
        if fit:
            fit = dict(fit, source_lattice_origin_seconds=start_seconds + float(beginning)
                       + fit["region_lattice_origin_seconds"])
        local.append({"source_start_seconds": start_seconds + float(beginning),
                      "source_end_seconds": start_seconds + end,
                      "status": "candidate_grid" if fit else "insufficient_evidence",
                      "fit": fit})
    base["local_windows"] = local
    reliable = [row for row in local if row["fit"] and row["fit"]["regularity_score"] >= .45]
    reference_bpm = selected["bpm"] if selected else (
        float(np.median([row["fit"]["bpm"] for row in reliable])) if reliable else None)
    if reference_bpm:
        for row in local:
            if not row["fit"]:
                continue
            ratio = row["fit"]["bpm"] / reference_bpm
            alternative = min((.5, 1., 2.), key=lambda value: abs(math.log2(ratio / value)))
            row["counting_relation"] = (
                "same_pulse_rate" if alternative == 1 else "possible_half_or_double_counting"
            ) if abs(math.log2(ratio / alternative)) < .15 else "different_local_periodicity"
            if row["counting_relation"] == "possible_half_or_double_counting":
                base["local_counting_ambiguities"].append({
                    "source_start_seconds": row["source_start_seconds"],
                    "source_end_seconds": row["source_end_seconds"],
                    "relative_pulse_rate": alternative,
                    "status": "do_not_treat_as_confirmed_tempo_change",
                })
        reliable = [row for row in reliable if abs(math.log2(row["fit"]["bpm"] / reference_bpm)) < .35]
    if len(reliable) >= 3:
        centers = np.array([(row["source_start_seconds"] + row["source_end_seconds"]) / 2 for row in reliable])
        bpms = np.array([row["fit"]["bpm"] for row in reliable])
        slope = float(np.polyfit(centers - centers[0], bpms, 1)[0])
        spread = float(np.ptp(bpms))
        base["drift"] = {"status": "changing_local_periodicity" if spread > max(1.5, reference_bpm * .015)
                          else "approximately_stable_local_periodicity",
                          "local_bpm_range": [float(bpms.min()), float(bpms.max())],
                          "bpm_per_minute_linear_trend": slope * 60,
                          "window_count": len(reliable),
                          "scope": "pulse_rate_only_not_phase_or_count_continuity",
                          "constant_grid_authorized": False}
    if selected is None:
        if reliable:
            base["status"] = "local_grids_only"
        return base
    selected = dict(selected, source_lattice_origin_seconds=start_seconds + selected["region_lattice_origin_seconds"])
    base.update(status="candidate_grid", selected_pulse_grid=selected)
    base["counting_alternatives"] = [
        {"bpm": selected["bpm"] * ratio, "relative_pulse_rate": ratio,
         "interpretation": label, "status": "metrical_interpretation_not_resolved"}
        for ratio, label in ((.5, "half-time counting"), (1., "selected pulse rate"), (2., "double-time counting"))
    ]
    period, origin = selected["pulse_period_seconds"], selected["region_lattice_origin_seconds"]
    pulse_numbers = np.rint((np.array([event["region_seconds"] for event in events]) - origin) / period).astype(int)
    strength = np.array([event["strength_relative"] for event in events])
    deviations = np.abs(np.array([event["region_seconds"] for event in events]) - (origin + pulse_numbers * period))
    valid = deviations < min(.12 * period, .06)
    accents = np.array([strength[valid & (pulse_numbers % bar_size == offset)].sum()
                        for offset in range(bar_size)])
    accents /= max(float(accents.sum()), 1e-12)
    band_accents = {}
    for name in events[0].get("band_strengths_relative", {}):
        values = np.array([event["band_strengths_relative"][name] for event in events])
        shares = np.array([values[valid & (pulse_numbers % bar_size == offset)].sum()
                           for offset in range(bar_size)])
        band_accents[name] = shares / max(float(shares.sum()), 1e-20)
    base["bar_interpretations"] = [
        {"candidate_bar_origin_offset_pulses": offset,
         "source_candidate_origin_seconds": start_seconds + origin + offset * period,
         "onset_accent_share": float(accents[offset]),
         "band_accent_shares": {name: float(shares[offset]) for name, shares in band_accents.items()},
         "status": "accent_evidence_only_not_certified_downbeat"}
        for offset in range(bar_size)
    ]
    return base


def _crop_stability(rate: int, original: np.ndarray, original_rate: int,
                    start_frame: int, grid: dict | None) -> dict:
    result = {"status": "insufficient_evidence", "checks": [],
              "method": "independent_onset_and_grid_refits_on_overlapping_inward_crops",
              "inset_seconds": [.125, .375], "automatic_edit_authorized": False,
              "limitation": "Only these two smaller crops are checked; matching clocks do not certify beat one."}
    if grid is None or len(original) / original_rate < 8:
        result["reason"] = "Requires a supported reference lattice and at least eight seconds."
        return result
    for inset in result["inset_seconds"]:
        first = math.ceil(inset * original_rate)
        last = len(original) - first
        # Recompute the resampler and STFT origins as a real cropped call would;
        # subsetting the first report's events would hide crop-dependent attacks.
        inner = original[first:last]
        divisor = math.gcd(rate, original_rate)
        inner_audio = signal.resample_poly(inner, rate // divisor, original_rate // divisor, axis=0) \
            if rate != original_rate else inner
        events, novelty, times = _onsets(inner_audio, rate, inner, original_rate, start_frame + first)
        fit, _ = _tempo(events, novelty, times, grid["bpm"])
        row = {"source_start_frame": start_frame + first, "source_end_frame_exclusive": start_frame + last,
               "source_start_seconds": (start_frame + first) / original_rate,
               "source_end_seconds": (start_frame + last) / original_rate, "fit": None}
        if fit:
            fit = dict(fit, source_lattice_origin_seconds=(start_frame + first) / original_rate
                       + fit["region_lattice_origin_seconds"])
            midpoint = (2 * start_frame + first + last) / (2 * original_rate)
            row.update(compare_grids(grid, fit, midpoint), fit=fit, comparison_source_seconds=midpoint)
        else:
            row["status"] = "insufficient_evidence"
        result["checks"].append(row)
    states = {row["status"] for row in result["checks"]}
    if "phase_sensitive_to_crop" in states:
        result["status"] = "phase_sensitive_to_crop"
    elif "different_pulse_rate_or_counting" in states:
        result["status"] = "counting_sensitive_to_crop"
    elif states == {"similar_acoustic_phase"}:
        result["status"] = "stable_in_tested_crops"
    return result


def _harmony(audio: np.ndarray, rate: int, source_start: float,
             source_duration: float) -> tuple[dict, np.ndarray, np.ndarray]:
    if len(audio) < 1024:
        return {"status": "insufficient_evidence", "windows": []}, np.empty((0, 12)), np.array([])
    nfft = min(4096, len(audio))
    frequency, times, power = _spectrogram(audio, rate, nfft, min(480, nfft // 4))
    relevant = (frequency >= 55) & (frequency <= 5000)
    frequency, power = frequency[relevant], power[relevant]
    midi = 69 + 12 * np.log2(frequency / 440)
    pitch_class = np.rint(midi).astype(int) % 12
    # Only local spectral peaks close to equal-tempered bins contribute. This is
    # pitch-class energy, not fundamental tracking or note transcription.
    peak_mask = (power > np.roll(power, 1, axis=0)) & (power >= np.roll(power, -1, axis=0))
    peak_mask[[0, -1], :] = False
    tuning_mask = np.abs(midi - np.rint(midi)) < .35
    chroma = np.stack([(power * peak_mask * tuning_mask[:, None])[pitch_class == pc].sum(axis=0)
                       for pc in range(12)], axis=1)
    windows = []
    duration = source_duration
    for beginning in np.arange(0, duration, 4.):
        end = min(duration, float(beginning + 4))
        first = int(beginning * rate)
        last = min(len(audio), int(end * rate))
        if last <= first:
            # An exact source crop can end a fraction of one downsampled frame
            # after a window boundary. Keep that source interval explicit, but
            # do not take mean(empty) or infer pitch from adjacent STFT padding.
            windows.append({"source_start_seconds": source_start + float(beginning),
                            "source_end_seconds": source_start + end,
                            "status": "insufficient_tonal_evidence",
                            "reason": "fewer_than_one_complete_analysis_frame",
                            "pitch_class_energy": None, "prominent_pitch_classes": [],
                            "spectral_flatness": None, "pitch_class_entropy": None,
                            "rms_dbfs": None, "key_verdict": None})
            continue
        mask = (times >= beginning) & (times < end)
        if not mask.any():
            continue
        energy = chroma[mask].sum(axis=0)
        distribution = energy / max(float(energy.sum()), 1e-30)
        spectrum = power[:, mask].mean(axis=1)
        flatness = float(np.exp(np.mean(np.log(spectrum + 1e-20))) / max(float(spectrum.mean()), 1e-20))
        entropy = float(-np.sum(distribution * np.log(distribution + 1e-30)) / np.log(12))
        rms = float(np.sqrt(np.mean(audio[first:last] ** 2)))
        tonal = rms > 10 ** (-65 / 20) and float(energy.sum()) > 1e-12 and flatness < .20 and entropy < .91
        ranking = np.argsort(distribution)[::-1][:3]
        windows.append({"source_start_seconds": source_start + float(beginning),
                        "source_end_seconds": source_start + end,
                        "status": "local_pitch_class_evidence" if tonal else "insufficient_tonal_evidence",
                        "pitch_class_energy": [float(value) for value in distribution] if tonal else None,
                        "prominent_pitch_classes": [{"name": _PITCH_NAMES[pc], "energy_share": float(distribution[pc])}
                                                    for pc in ranking] if tonal else [],
                        "spectral_flatness": flatness, "pitch_class_entropy": entropy,
                        "rms_dbfs": _db(rms), "key_verdict": None})
    return {"status": "local_evidence" if any(row["status"] == "local_pitch_class_evidence" for row in windows)
            else "insufficient_evidence", "pitch_class_order": list(_PITCH_NAMES), "windows": windows}, chroma, times


def _repetitions(audio: np.ndarray, rate: int, source_start: float, source_duration: float) -> dict:
    # Coarse 4-second fingerprints: evidence of repeated waveform texture only.
    width = 4 * rate
    count = min(150, len(audio) // width, int(source_duration // 4))
    features, levels = [], []
    for index in range(count):
        chunk = audio[index * width:(index + 1) * width]
        _, _, power = _spectrogram(chunk, rate, 512, 240)
        bands = np.array_split(power[1:], 12, axis=0)
        band_frames = np.stack([band.sum(axis=0) for band in bands])
        # Preserve coarse within-section order; a similar timbre alone is weaker.
        pooled = np.stack([part.mean(axis=1) for part in np.array_split(band_frames, 16, axis=1)], axis=1)
        feature = np.sqrt(pooled).ravel()
        features.append(feature / max(float(np.linalg.norm(feature)), 1e-20))
        levels.append(float(np.sqrt(np.mean(chunk ** 2))))
    matches = []
    for earlier in range(count):
        for later in range(earlier + 2, count):
            if min(levels[earlier], levels[later]) < 10 ** (-60 / 20):
                continue
            similarity = float(np.clip(np.dot(features[earlier], features[later]), 0, 1))
            if similarity >= .985:
                matches.append({"source_a_start_seconds": source_start + earlier * 4,
                                "source_b_start_seconds": source_start + later * 4,
                                "duration_seconds": 4., "feature_cosine_similarity": similarity,
                                "status": "similar_texture_not_confirmed_phrase"})
    matches.sort(key=lambda row: row["feature_cosine_similarity"], reverse=True)
    return {"method": "four_second_ordered_band_energy_fingerprints", "candidates": matches[:12],
            "limitation": "Similar sustained timbre can score highly; this does not establish a repeated musical section."}


def analyze_region(path: str | Path, start_seconds: float = 0, duration_seconds: float = 30,
                   bpm_hint: float | None = None, beats_per_bar: int = 4, *,
                   start_frame: int | None = None, frames: int | None = None) -> dict:
    """Analyze a bounded source crop without changing media or authorizing edits.

    Seconds limits round inward; paired start_frame/frames address exact frames
    and require default seconds arguments. A BPM hint
    selects among measured candidate pulse rates; it is not evidence of tempo.
    """
    beginning = _number(start_seconds, "start_seconds")
    duration = _number(duration_seconds, "duration_seconds")
    frame_addressing = start_frame is not None or frames is not None
    if frame_addressing:
        if (isinstance(start_frame, bool) or not isinstance(start_frame, int)
                or isinstance(frames, bool) or not isinstance(frames, int)):
            raise PocketError("start_frame and frames must both be integers, not booleans")
        if beginning != 0 or duration != 30:
            raise PocketError("Frame addressing cannot be mixed with nondefault seconds arguments")
        if start_frame < 0 or frames <= 0:
            raise PocketError("start_frame must be nonnegative and frames must be positive")
    elif beginning < 0 or duration <= 0 or duration > MAX_REGION_SECONDS:
        raise PocketError(f"Region must start at or after zero and last >0 to {MAX_REGION_SECONDS:g} seconds")
    hint = None if bpm_hint is None else _number(bpm_hint, "bpm_hint")
    if hint is not None and not 20 <= hint <= 400:
        raise PocketError("bpm_hint must be between 20 and 400")
    if isinstance(beats_per_bar, bool) or not isinstance(beats_per_bar, int) or not 2 <= beats_per_bar <= 12:
        raise PocketError("beats_per_bar must be an integer between 2 and 12")
    source = Path(path).expanduser().resolve()
    try:
        stat = source.stat()
    except OSError as exc:
        raise PocketError(f"Cannot read audio: {exc}") from exc
    asset = identify_audio(source)
    rate = int(asset["sample_rate"])
    if frame_addressing:
        end_frame = start_frame + frames
        if end_frame > asset["frames"] or frames / rate > MAX_REGION_SECONDS:
            raise PocketError("Frame region exceeds the source or maximum duration")
        beginning, duration = start_frame / rate, frames / rate
    else:
        end = beginning + duration
        if not math.isfinite(end) or end > asset["frames"] / rate:
            raise PocketError("Requested region exceeds the source; crops are never silently widened or truncated")
        # ULP tolerance prevents a decimal representation of an exact frame from
        # losing that frame; it does not permit a whole sample outside the request.
        start_frame = math.ceil(np.nextafter(beginning * rate, -np.inf))
        end_frame = math.floor(np.nextafter(end * rate, np.inf))
    if end_frame <= start_frame:
        raise PocketError("Requested region contains no complete source frames")
    try:
        original, read_rate = sf.read(source, start=start_frame, stop=end_frame, dtype="float64", always_2d=True)
    except (OSError, RuntimeError) as exc:
        raise PocketError(f"Cannot read audio region: {exc}") from exc
    if read_rate != rate or len(original) != end_frame - start_frame:
        raise PocketError("Audio changed or region could not be read completely")
    if not np.isfinite(original).all():
        raise PocketError("Audio region contains nonfinite samples")
    peak = float(np.max(np.abs(original), initial=0))
    rms = float(np.sqrt(np.mean(original ** 2)))
    analysis_rate = min(_RATE, rate)
    divisor = math.gcd(rate, analysis_rate)
    audio = signal.resample_poly(original, analysis_rate // divisor, rate // divisor, axis=0) if rate != analysis_rate else original
    source_start = start_frame / rate
    actual_duration = len(original) / rate
    silent = rms < 10 ** (-70 / 20)
    if silent or len(audio) < 64:
        events, novelty, times = [], np.zeros(0), np.zeros(0)
        harmony = {"status": "insufficient_evidence", "pitch_class_order": list(_PITCH_NAMES), "windows": []}
        repeats = {"method": "four_second_ordered_band_energy_fingerprints", "candidates": []}
    else:
        events, novelty, times = _onsets(audio, analysis_rate, original, rate, start_frame)
        harmony, _, _ = _harmony(audio, analysis_rate, source_start, actual_duration)
        repeats = _repetitions(audio, analysis_rate, source_start, actual_duration)
    rhythm = _rhythm(events, novelty, times, actual_duration, source_start, hint, beats_per_bar)
    rhythm.update(analyze_phase_continuity(audio, analysis_rate, source_start, rate, end_frame,
                                           rhythm["selected_pulse_grid"]))
    rhythm["crop_stability"] = _crop_stability(analysis_rate, original, rate, start_frame,
                                               rhythm["selected_pulse_grid"])
    continuity = rhythm["phase_count_continuity"]
    if rhythm["crop_stability"]["status"] in ("phase_sensitive_to_crop", "counting_sensitive_to_crop"):
        continuity["crop_sensitivity"] = rhythm["crop_stability"]["status"]
        if continuity["status"] == "locally_stable_acoustic_phase":
            continuity["status"] = "crop_sensitive_acoustic_grid"
            continuity["reason"] = "The overlapping crop refits disagree despite stable band phases."
    latest = source.stat()
    if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != (
            latest.st_dev, latest.st_ino, latest.st_size, latest.st_mtime_ns):
        raise PocketError("Source file changed during analysis; discard this result")
    return {
        "schema": SCHEMA, "asset": asset,
        "region": {"requested_start_seconds": beginning, "requested_duration_seconds": duration,
                   "start_frame": start_frame, "end_frame_exclusive": end_frame,
                   "start_seconds": source_start, "end_seconds": end_frame / rate,
                   "frames": len(original), "duration_seconds": actual_duration,
                   "addressing": "source_frames" if frame_addressing else "source_seconds",
                   "rounding": "none_explicit_source_frames" if frame_addressing
                   else "inward_to_complete_original_source_frames"},
        "signal": {"peak_dbfs": _db(peak), "rms_dbfs": _db(rms),
                   "silence_or_near_silence": silent, "nonfinite_samples": 0,
                   "samples_at_or_above_unity": int(np.count_nonzero(np.abs(original) >= 1))},
        "onsets": {"method": "positive_spectral_flux_with_original_waveform_envelope_refinement",
                   "nominal_hop_seconds": min(_HOP, max(1, min(512, len(audio)) // 4)) / analysis_rate,
                   "timing_search_radius_seconds": .028,
                   "events": events},
        "rhythm": rhythm, "harmony": harmony, "repetitions": repeats,
        "provenance": {"analysis_version": ANALYSIS_VERSION, "deterministic": True,
                       "analysis_sample_rate": analysis_rate,
                       "parameters": {"onset_fft_size_max": 512, "onset_hop_samples_max": _HOP,
                                      "onset_minimum_spacing_seconds": .075,
                                      "onset_relative_height": .07, "onset_relative_prominence": .045,
                                      "silence_rms_threshold_dbfs": -70,
                                      "tempo_autocorrelation_search_bpm": [20, 400],
                                      "local_tempo_window_seconds": 8, "local_tempo_step_seconds": 4,
                                      "harmony_window_seconds": 4, "beats_per_bar_hypothesis": beats_per_bar},
                       "libraries": {"numpy": np.__version__, "scipy": scipy.__version__, "soundfile": sf.__version__},
                       "learned_models": [], "human_feedback": [],
                       "source_stability": "device_inode_size_mtime_unchanged_during_identity_read_and_analysis"},
        "limitations": [
            "Detected attacks have exact source coordinates but finite timing resolution and may be syncopations or subdivisions.",
            "Pulse regularity, tempo hints and strong accents do not certify beat one; all bar orientations remain hypotheses.",
            "Local tempo differences can reflect detector errors or rhythm changes; no automatic warp or edit is authorized.",
            "A stable pulse rate does not establish phase or count continuity; band changes and crop alternatives are reported separately.",
            "Pitch-class evidence includes overtones and mixed instruments, assumes A440 equal temperament, and is not a global key or note transcription.",
            "Only the requested crop is analyzed; boundary attacks and structure outside it may be missed.",
        ],
    }
