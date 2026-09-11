"""Public generated music-mechanics regressions; no recorded source fixtures."""

import json

import numpy as np
import pytest
import soundfile as sf

from pocket_music.errors import PocketError
from pocket_music.rhythm_continuity import circular_delta, compare_grids
from pocket_music.track_map import analyze_region

RATE = 12000


def add_tones(audio, times, frequency, level=.3):
    phase = np.arange(round(.09 * RATE)) / RATE
    shape = np.minimum(phase / .004, 1) * np.exp(-phase * 60)
    tone = level * np.sin(2 * np.pi * frequency * phase) * shape
    for time in times:
        start = round(time * RATE)
        stop = min(len(audio), start + len(tone))
        if start >= 0 and stop > start:
            audio[start:stop] += tone[:stop - start]


def layered(tmp_path, *, displace=False, changing=False, duration=40):
    audio = np.zeros(round(duration * RATE))
    times = np.arange(.30, duration - .3, .5)
    add_tones(audio, times, 650)
    add_tones(audio, times + (.25 if displace else 0), 3500)
    shifts = -.25 * np.clip((times - 16) / 4, 0, 1) if changing else 0
    add_tones(audio, times + shifts, 90, .4)
    path = tmp_path / "layers.wav"
    sf.write(path, audio, RATE, subtype="FLOAT")
    return path


def test_persistent_subdivision_layers_are_not_bar_rotations(tmp_path):
    path = layered(tmp_path, displace=True)
    first = analyze_region(path, 4, 28, bpm_hint=120)
    second = analyze_region(path, 4.125, 28, bpm_hint=120)
    for result in (first, second):
        rhythm = result["rhythm"]
        assert rhythm["phase_count_continuity"]["status"] == "competing_acoustic_phases"
        assert rhythm["bar_phase_status"] == "unresolved"
        modes = rhythm["acoustic_phase_candidates"]
        body = next(row for row in modes if row["band"].startswith("body"))
        high = next(row for row in modes if row["band"].startswith("high"))
        assert abs(circular_delta(body["phase_pulses"], high["phase_pulses"])) == pytest.approx(.5, abs=.04)
        assert all(row["status"] == "periodic_attack_layer_not_a_bar_origin" for row in modes)
        assert all(result["region"]["start_frame"] <= row["source_candidate_origin_frame"]
                   < result["region"]["end_frame_exclusive"] for row in modes)
        assert all(row["source_candidate_origin_frame"] == round(row["source_candidate_origin_seconds"] * RATE)
                   for row in modes)
    # Different requested origins retain the same absolute attack-layer lattice.
    for band in ("body_180_1500_hz", "high_1500_5500_hz"):
        a, b = [next(row for row in result["rhythm"]["acoustic_phase_candidates"] if row["band"] == band)
                for result in (first, second)]
        assert abs(circular_delta((a["source_candidate_origin_seconds"] - b["source_candidate_origin_seconds"])
                                  / .5, 0)) < .025


def test_steady_rate_does_not_hide_a_local_layer_count_displacement(tmp_path):
    result = analyze_region(layered(tmp_path, changing=True), duration_seconds=40, bpm_hint=120)
    rhythm = result["rhythm"]
    assert rhythm["selected_pulse_grid"]["bpm"] == pytest.approx(120, abs=.4)
    continuity = rhythm["phase_count_continuity"]
    assert continuity["status"] == "phase_or_count_continuity_unresolved"
    assert continuity["integer_pulse_count"] is None
    assert not continuity["automatic_edit_authorized"]
    changes = [row for row in continuity["change_intervals"] if row["band"].startswith("low")]
    assert changes
    assert any(row["source_start_seconds"] <= 20 and row["source_end_seconds"] >= 16 for row in changes)
    assert any(abs(row["signed_phase_change_pulses"]) > .25 for row in changes)
    assert all(row["source_start_frame"] >= 0 and row["source_end_frame_exclusive"] <= 40 * RATE
               for row in changes)
    assert "rate_only" in rhythm["drift"]["scope"]
    json.dumps(result, allow_nan=False)


def test_stable_multiband_control_is_positive_acoustic_evidence(tmp_path):
    result = analyze_region(layered(tmp_path), 4, 28, bpm_hint=120)
    rhythm = result["rhythm"]
    assert rhythm["phase_count_continuity"]["status"] == "locally_stable_acoustic_phase"
    assert rhythm["phase_count_continuity"]["change_intervals"] == []
    assert rhythm["crop_stability"]["status"] == "stable_in_tested_crops"
    assert len(rhythm["crop_stability"]["checks"]) == 2
    assert all(row["source_start_frame"] > 4 * RATE and row["source_end_frame_exclusive"] < 32 * RATE
               for row in rhythm["crop_stability"]["checks"])
    assert rhythm["bar_phase_status"] == "unresolved"


def test_irregular_sparse_passage_abstains_from_clock_continuity(tmp_path):
    audio = np.zeros(30 * RATE)
    times = np.cumsum(np.random.default_rng(515).uniform(.35, 2.3, 25))
    add_tones(audio, times[times < 29], 650)
    path = tmp_path / "free.wav"
    sf.write(path, audio, RATE, subtype="FLOAT")
    report = analyze_region(path, duration_seconds=30, bpm_hint=120)
    assert report["rhythm"]["phase_count_continuity"]["status"] == "insufficient_evidence"
    assert not report["rhythm"]["phase_count_continuity"]["automatic_edit_authorized"]


def test_silence_and_short_crop_do_not_get_stability_certificates(tmp_path):
    path = tmp_path / "quiet.wav"
    sf.write(path, np.zeros(10 * RATE), RATE, subtype="FLOAT")
    for duration in (.03, 10):
        rhythm = analyze_region(path, duration_seconds=duration, bpm_hint=120)["rhythm"]
        assert rhythm["crop_stability"]["status"] == "insufficient_evidence"
        assert rhythm["phase_count_continuity"]["status"] == "insufficient_evidence"
        assert rhythm["acoustic_phase_candidates"] == []


def test_report_parameters_do_not_mutate_future_analysis(tmp_path):
    path = layered(tmp_path, duration=16)
    first = analyze_region(path, duration_seconds=16, bpm_hint=120)
    expected = json.loads(json.dumps(first))
    first["rhythm"]["phase_count_continuity"]["parameters"]["minimum_attacks"] = 500
    first["rhythm"]["phase_count_continuity"]["parameters"]["local_window_seconds_limits"][0] = 100
    assert analyze_region(path, duration_seconds=16, bpm_hint=120) == expected


def test_crop_comparison_retains_subpulse_and_octave_disagreement():
    grid = {"bpm": 120, "pulse_period_seconds": .5, "source_lattice_origin_seconds": 13.2}
    half_pulse = {**grid, "source_lattice_origin_seconds": 13.45}
    comparison = compare_grids(grid, half_pulse, 20)
    assert comparison["status"] == "phase_sensitive_to_crop"
    assert abs(comparison["signed_phase_difference_pulses"]) == .5
    doubled = {**grid, "bpm": 240, "pulse_period_seconds": .25}
    comparison = compare_grids(grid, doubled, 20)
    assert comparison["status"] == "different_pulse_rate_or_counting"
    assert comparison["signed_phase_difference_pulses"] is None


def test_frame_addressing_never_roundtrips_through_seconds(tmp_path):
    rate = 44100
    path = tmp_path / "frame-address.wav"
    sf.write(path, np.zeros(3 * rate), rate, subtype="FLOAT")
    result = analyze_region(path, start_frame=11515, frames=44107)
    region = result["region"]
    assert region["start_frame"] == 11515
    assert region["end_frame_exclusive"] == 55622
    assert region["frames"] == 44107
    assert region["addressing"] == "source_frames"
    assert region["rounding"] == "none_explicit_source_frames"
    assert region["start_seconds"] == 11515 / rate


@pytest.mark.parametrize("arguments", [
    {"start_frame": 0}, {"frames": 1}, {"start_frame": True, "frames": 1},
    {"start_frame": 0, "frames": False}, {"start_frame": 1.0, "frames": 1},
    {"start_frame": -1, "frames": 1}, {"start_frame": 0, "frames": 0},
    {"start_frame": 0, "frames": RATE + 1}, {"start_frame": 0, "frames": 1, "start_seconds": .1},
    {"start_frame": 0, "frames": 1, "duration_seconds": 1},
])
def test_frame_addressing_rejects_mixed_missing_or_invalid_bounds(tmp_path, arguments):
    path = tmp_path / "bounds.wav"
    sf.write(path, np.zeros(RATE), RATE, subtype="FLOAT")
    with pytest.raises(PocketError):
        analyze_region(path, **arguments)
