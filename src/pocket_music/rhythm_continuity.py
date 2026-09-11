"""Acoustic phase evidence, deliberately separate from musical bar orientation.

All phases describe periodic attack layers. A syncopation, instrumentation change
or detector error can move a layer without moving the underlying musical beat.
"""

from __future__ import annotations

import math
from copy import deepcopy

import numpy as np
from scipy import signal
from scipy.ndimage import gaussian_filter1d, median_filter

BANDS = (("low_35_180_hz", 35, 180), ("body_180_1500_hz", 180, 1500),
         ("high_1500_5500_hz", 1500, 5500))
PARAMETERS = {"spectral_hop_seconds": .002, "spectral_fft_size_max": 512,
              "background_seconds": .202, "attack_min_spacing_seconds": .05,
              "attack_prominence_fraction": .06, "phase_histogram_bins": 100,
              "phase_smoothing_sigma_bins": 2, "phase_support_radius_pulses": .08,
              "minimum_phase_weight_fraction": .30, "minimum_attacks": 5,
              "local_window_pulses": 8, "local_window_seconds_limits": [4, 8],
              "sustained_flank_windows": 3, "flank_max_spread_pulses": .09,
              "phase_change_minimum_pulses": .18}


def circular_delta(a: float | np.ndarray, b: float | np.ndarray) -> float | np.ndarray:
    """Signed nearest-phase difference; integer counts cannot be recovered here."""
    return (a - b + .5) % 1 - .5


def _modes(times: np.ndarray, weights: np.ndarray, period: float) -> list[dict]:
    if len(times) < PARAMETERS["minimum_attacks"] or np.ptp(times) < 3 * period:
        return []
    phases = times % period / period
    histogram = np.histogram(phases, bins=100, range=(0, 1), weights=weights)[0]
    smooth = gaussian_filter1d(histogram.astype(float), 2, mode="wrap")
    peaks = signal.find_peaks(np.tile(smooth, 3), distance=12)[0]
    peaks = [int(index - 100) for index in peaks if 100 <= index < 200]
    result = []
    for index in sorted(peaks, key=lambda i: smooth[i], reverse=True)[:4]:
        phase = (index + .5) / 100
        mask = np.abs(circular_delta(phases, phase)) <= .08
        if mask.sum() < 5:
            continue
        phase = float((phase + np.average(circular_delta(phases[mask], phase), weights=weights[mask])) % 1)
        mask = np.abs(circular_delta(phases, phase)) <= .08
        fraction = float(weights[mask].sum() / max(float(weights.sum()), 1e-30))
        pulse_numbers = np.rint(times[mask] / period - phase)
        coverage = len(np.unique(pulse_numbers)) / max(1, math.floor(np.ptp(times) / period) + 1)
        if fraction < .30 or mask.sum() < 5 or coverage < .30:
            continue
        result.append({"phase_pulses": phase, "onset_weight_fraction": fraction,
                       "occupied_pulse_fraction": min(1., coverage), "attack_count": int(mask.sum()),
                       "median_residual_ms": float(np.median(np.abs(circular_delta(phases[mask], phase)))
                                                   * period * 1000)})
    return result


def _band_attacks(audio: np.ndarray, rate: int) -> dict:
    """Independent band peaks on a common 2 ms clock; chunked to bound STFT RAM."""
    hop = max(1, round(.002 * rate))
    width = max(512, round(24 * rate / hop) * hop)
    guard = max(512, math.ceil(.35 * rate / hop) * hop)
    all_times, flux_chunks = [], {name: [] for name, _, _ in BANDS}
    for beginning in range(0, len(audio), width):
        end = min(len(audio), beginning + width)
        left, right = max(0, beginning - guard), min(len(audio), end + guard)
        nfft = min(512, right - left)
        if nfft <= hop:
            continue
        frequency, times, spectra = signal.stft(
            audio[left:right].T, fs=rate, nperseg=nfft, noverlap=nfft - hop,
            boundary="zeros", padded=False, axis=-1,
        )
        magnitude = np.sqrt(np.mean(np.abs(spectra) ** 2, axis=0))
        change = np.maximum(np.diff(magnitude, axis=1, prepend=magnitude[:, :1]), 0)
        times = times + left / rate
        keep = (times >= beginning / rate) & (times < end / rate)
        all_times.append(times[keep])
        for name, lo, hi in BANDS:
            flux_chunks[name].append(change[(frequency >= lo) & (frequency < hi)].sum(axis=0)[keep])
    if not all_times:
        return {name: (np.array([]), np.array([])) for name, _, _ in BANDS}
    times = np.concatenate(all_times)
    result = {}
    for name, _, _ in BANDS:
        flux = np.concatenate(flux_chunks[name])
        flux = np.maximum(flux - median_filter(flux, size=max(3, round(.202 * rate / hop) | 1),
                                               mode="nearest"), 0)
        maximum = float(flux.max(initial=0))
        if maximum <= 1e-12:
            result[name] = (np.array([]), np.array([]))
            continue
        indices = signal.find_peaks(flux, prominence=.06 * maximum,
                                    distance=max(1, round(.05 * rate / hop)))[0]
        # Suppress crop-boundary transients; guards never read beyond the request.
        indices = indices[(times[indices] >= .06) & (times[indices] < len(audio) / rate - .06)]
        result[name] = (times[indices], flux[indices] / maximum)
    return result


def _phase_changes(rows: list[dict], period: float, window: float, source_start: float) -> list[dict]:
    """Locate sustained displaced flanks, allowing a short transitional interval."""
    candidates = []
    for split in range(3, len(rows) - 2):
        before = rows[split - 3:split]
        for gap in range(4):
            after = rows[split + gap:split + gap + 3]
            if len(after) != 3 or any(not row["modes"] for row in before + after):
                continue
            a = np.array([row["modes"][0]["phase_pulses"] for row in before])
            b = np.array([row["modes"][0]["phase_pulses"] for row in after])
            mean_a = float((a[0] + np.median(circular_delta(a, a[0]))) % 1)
            mean_b = float((b[0] + np.median(circular_delta(b, b[0]))) % 1)
            spread = max(float(np.max(np.abs(circular_delta(a, mean_a)))),
                         float(np.max(np.abs(circular_delta(b, mean_b)))))
            delta = float(circular_delta(mean_b, mean_a))
            if spread > .09 or abs(delta) < .18:
                continue
            # A competing mode on either flank means a change of dominance, not
            # evidence that the same acoustic layer accelerated.
            competing = any(len(row["modes"]) > 1 for row in before + after)
            candidates.append({
                "source_start_seconds": source_start + before[-1]["center"] - window / 2,
                "source_end_seconds": source_start + after[0]["center"] + window / 2,
                "before_phase_pulses": mean_a, "after_phase_pulses": mean_b,
                "signed_phase_change_pulses": delta, "signed_phase_change_ms": delta * period * 1000,
                "flank_spread_pulses": spread, "flank_windows_each": 3,
                "competing_modes_on_flanks": competing,
                "integer_pulse_count": None,
                "status": "layer_dominance_or_timing_change_unresolved",
            })
            break
    # Overlapping detections are one uncertainty interval. Preserve their union:
    # selecting a single tightest window can hide the later part of a passage.
    groups = []
    for row in candidates:
        if groups and row["source_start_seconds"] <= groups[-1][-1]["source_end_seconds"]:
            groups[-1].append(row)
        else:
            groups.append([row])
    result = []
    for group in groups:
        representative = min(group, key=lambda row: row["flank_spread_pulses"])
        result.append({**representative,
                       "source_start_seconds": min(row["source_start_seconds"] for row in group),
                       "source_end_seconds": max(row["source_end_seconds"] for row in group),
                       "overlapping_detections": len(group),
                       "localization": "union_of_supported_windows_not_exact_change_boundaries"})
    return result


def analyze_phase_continuity(audio: np.ndarray, rate: int, source_start: float, source_rate: int,
                             source_end_frame: int, grid: dict | None) -> dict:
    """Return clock-relative competing phases and band-resolved continuity limits."""
    result = {"acoustic_phase_candidates": [], "phase_count_continuity": {
        "status": "insufficient_evidence", "reference_bpm": None, "bands": [],
        "change_intervals": [], "integer_pulse_count": None, "automatic_edit_authorized": False,
        "method": "independent_band_flux_circular_modes_and_sustained_local_flanks",
        "parameters": deepcopy(PARAMETERS),
    }}
    continuity = result["phase_count_continuity"]
    if grid is None or len(audio) / rate < 4:
        continuity["reason"] = "No supported crop-wide pulse rate or fewer than four seconds."
        return result
    period = grid["pulse_period_seconds"]
    window = float(np.clip(8 * period, 4, 8))
    duration = len(audio) / rate
    continuity["reference_bpm"] = grid["bpm"]
    continuity["local_window_seconds"] = window
    continuity["local_step_seconds"] = window / 4
    tracks = _band_attacks(audio, rate)
    supported_bands = 0
    stable_bands = []
    for name, (times, weights) in tracks.items():
        modes = _modes(times, weights, period)
        rows = []
        for beginning in np.arange(0, duration - window + .000001, window / 4):
            mask = (times >= beginning) & (times < beginning + window)
            rows.append({"center": float(beginning + window / 2),
                         "modes": _modes(times[mask], weights[mask], period)})
        reliable = [row for row in rows if row["modes"]]
        coverage = len(reliable) / max(1, len(rows))
        changes = _phase_changes(rows, period, window, source_start)
        for row in changes:
            row["band"] = name
            # Exact frame addresses of evidence windows, not sub-sample attacks.
            row["source_start_frame"] = max(round(source_start * source_rate),
                                             math.ceil(row["source_start_seconds"] * source_rate))
            row["source_end_frame_exclusive"] = min(source_end_frame,
                                                     math.floor(row["source_end_seconds"] * source_rate))
        continuity["change_intervals"].extend(changes)
        band_supported = coverage >= .6 and len(reliable) >= 6
        supported_bands += int(band_supported)
        for mode in modes:
            matching = sum(any(abs(circular_delta(local["phase_pulses"], mode["phase_pulses"])) <= .10
                               for local in row["modes"]) for row in rows)
            persistence = matching / max(1, len(rows))
            if persistence < .4:
                continue
            origin = source_start + mode["phase_pulses"] * period
            result["acoustic_phase_candidates"].append({
                **mode, "band": name, "reference_bpm": grid["bpm"],
                "source_candidate_origin_seconds": origin,
                "source_candidate_origin_frame": min(source_end_frame - 1, round(origin * source_rate)),
                "offset_from_selected_grid_pulses": float(circular_delta(
                    mode["phase_pulses"], grid["region_lattice_origin_seconds"] / period)),
                "local_window_support_fraction": persistence,
                "status": "periodic_attack_layer_not_a_bar_origin",
            })
        # Spread around the best global mode is evidence only when sustained and
        # actually local; a high crop-wide histogram peak cannot supply it alone.
        residuals = [abs(circular_delta(row["modes"][0]["phase_pulses"], modes[0]["phase_pulses"]))
                     for row in reliable] if modes else []
        spread = float(np.percentile(residuals, 90)) if residuals else None
        stable = band_supported and not changes and spread is not None and spread <= .09
        if stable:
            stable_bands.append(name)
        continuity["bands"].append({
            "band": name, "attack_count": len(times), "window_count": len(rows),
            "supported_window_count": len(reliable), "supported_window_fraction": coverage,
            "dominant_phase_residual_p90_pulses": spread,
            "status": "phase_change_or_layer_change" if changes else (
                "locally_stable_acoustic_phase" if stable else "insufficient_or_scattered_phase_evidence"),
            "local_windows": [{"source_start_seconds": source_start + row["center"] - window / 2,
                               "source_end_seconds": source_start + row["center"] + window / 2,
                               "modes": row["modes"][:2]} for row in rows],
        })
    candidates = result["acoustic_phase_candidates"]
    separated = any(abs(circular_delta(a["phase_pulses"], b["phase_pulses"])) >= .18
                    for index, a in enumerate(candidates) for b in candidates[index + 1:])
    continuity["competing_acoustic_phases"] = separated
    continuity["locally_stable_bands"] = stable_bands
    if continuity["change_intervals"]:
        continuity["status"] = "phase_or_count_continuity_unresolved"
        continuity["reason"] = "Sustained band-phase displacement; rate stability does not establish pulse count."
    elif separated:
        continuity["status"] = "competing_acoustic_phases"
        continuity["reason"] = "Persistent separated attack layers can exchange dominance with crop or instrumentation."
    elif supported_bands >= 2 and len(stable_bands) >= 2:
        continuity["status"] = "locally_stable_acoustic_phase"
        continuity["reason"] = "At least two bands have sustained locally stable phase in this crop; musical count is unverified."
    else:
        continuity["reason"] = "Too little sustained agreement between independently detected attack bands."
    return result


def compare_grids(reference: dict, other: dict, at_seconds: float) -> dict:
    """Compare acoustic clocks at a shared source instant, retaining octave changes."""
    ratio = other["bpm"] / reference["bpm"]
    same_rate = abs(math.log2(ratio)) < .03
    if not same_rate:
        return {"status": "different_pulse_rate_or_counting", "relative_pulse_rate": ratio,
                "signed_phase_difference_pulses": None, "signed_phase_difference_ms": None}
    a = (at_seconds - reference["source_lattice_origin_seconds"]) / reference["pulse_period_seconds"]
    b = (at_seconds - other["source_lattice_origin_seconds"]) / other["pulse_period_seconds"]
    delta = float(circular_delta(a, b))
    return {"status": "phase_sensitive_to_crop" if abs(delta) >= .15 else "similar_acoustic_phase",
            "relative_pulse_rate": ratio, "signed_phase_difference_pulses": delta,
            "signed_phase_difference_ms": delta * reference["pulse_period_seconds"] * 1000}
