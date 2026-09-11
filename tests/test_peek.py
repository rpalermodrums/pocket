"""Generated fixtures exercise timing, abstention, and metrical ambiguity."""

import hashlib
import json
import warnings

import numpy as np
import pytest
import soundfile as sf

from pocket_music import peek
from pocket_music.errors import PocketError
from pocket_music.peek import analyze_region

RATE = 12000


def audio_file(tmp_path, audio, name="fixture.wav"):
    path = tmp_path / name
    sf.write(path, audio, RATE, subtype="FLOAT")
    return path


def clicks(duration=28, bpm=120, beginning=.25, end_bpm=None, accents=None):
    audio = np.zeros(round(duration * RATE))
    pulse_times = []
    time = beginning
    index = 0
    while time < duration - .08:
        pulse_times.append(time)
        first = round(time * RATE)
        length = min(round(.025 * RATE), len(audio) - first)
        phase = np.arange(length) / RATE
        pulse = np.exp(-phase * 230) * np.cos(2 * np.pi * 1000 * phase)
        amplitude = .7 if accents is None else accents[index % len(accents)]
        audio[first:first + length] += pulse * amplitude
        current_bpm = bpm if end_bpm is None else bpm + (end_bpm - bpm) * time / duration
        time += 60 / current_bpm
        index += 1
    return audio, np.array(pulse_times)


def test_exact_identity_and_original_source_time(tmp_path):
    audio, pulse_times = clicks()
    path = audio_file(tmp_path, audio)
    result = analyze_region(path, 5, 16, bpm_hint=120)
    assert result["asset"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["region"]["start_frame"] == 5 * RATE
    assert result["region"]["end_frame_exclusive"] == 21 * RATE
    events = result["onsets"]["events"]
    detected = np.array([row["source_seconds"] for row in events])
    truth = pulse_times[(pulse_times > 5.05) & (pulse_times < 20.95)]
    assert len(events) >= len(truth) - 1
    assert max(min(abs(detected - value)) for value in truth) < .012
    assert all(row["source_frame"] == round(row["source_seconds"] * RATE) for row in events)
    assert all(5 * RATE <= row["source_frame"] < 21 * RATE for row in events)
    json.dumps(result, allow_nan=False)


def test_click_grid_keeps_half_double_and_bar_alternatives(tmp_path):
    path = audio_file(tmp_path, clicks(accents=[.8, .25, .5, .25])[0])
    result = analyze_region(path, duration_seconds=28, bpm_hint=120)
    rhythm = result["rhythm"]
    assert rhythm["selected_pulse_grid"]["bpm"] == pytest.approx(120, abs=.3)
    assert [row["bpm"] for row in rhythm["counting_alternatives"]] == pytest.approx([60, 120, 240], abs=.6)
    assert rhythm["bar_phase_status"] == "unresolved"
    assert len(rhythm["bar_interpretations"]) == 4
    assert all("not_certified" in row["status"] for row in rhythm["bar_interpretations"])
    assert rhythm["drift"]["status"] == "approximately_stable_local_periodicity"
    assert rhythm["selected_pulse_grid"]["residual_p95_ms"] < 3


def test_tempo_without_hint_is_measured(tmp_path):
    path = audio_file(tmp_path, clicks(bpm=123)[0])
    rhythm = analyze_region(path, duration_seconds=28)["rhythm"]
    assert rhythm["selected_pulse_grid"]["bpm"] == pytest.approx(123, abs=.5)


def test_silence_abstains_even_with_a_tempo_hint(tmp_path):
    path = audio_file(tmp_path, np.zeros(20 * RATE))
    result = analyze_region(path, duration_seconds=20, bpm_hint=120)
    assert result["signal"]["silence_or_near_silence"]
    assert result["onsets"]["events"] == []
    assert result["rhythm"]["status"] == "insufficient_evidence"
    assert result["rhythm"]["selected_pulse_grid"] is None
    assert result["harmony"]["status"] == "insufficient_evidence"
    assert result["repetitions"]["candidates"] == []
    json.dumps(result, allow_nan=False)


def test_changing_tempo_is_not_certified_as_constant_grid(tmp_path):
    path = audio_file(tmp_path, clicks(duration=40, bpm=108, end_bpm=132)[0])
    rhythm = analyze_region(path, duration_seconds=40, bpm_hint=120)["rhythm"]
    drift = rhythm["drift"]
    assert drift is not None
    assert drift["status"] == "changing_local_periodicity"
    assert drift["local_bpm_range"][1] - drift["local_bpm_range"][0] > 10
    assert drift["bpm_per_minute_linear_trend"] > 20
    assert drift["constant_grid_authorized"] is False


def test_local_pitch_classes_are_not_a_key_verdict(tmp_path):
    time = np.arange(8 * RATE) / RATE
    path = audio_file(tmp_path, .2 * np.sin(2 * np.pi * 440 * time))
    harmony = analyze_region(path, duration_seconds=8)["harmony"]
    assert harmony["status"] == "local_evidence"
    assert all(window["prominent_pitch_classes"][0]["name"] == "A" for window in harmony["windows"])
    assert all(window["key_verdict"] is None for window in harmony["windows"])


def test_noise_abstains_from_harmonic_verdict(tmp_path):
    path = audio_file(tmp_path, np.random.default_rng(20).normal(0, .08, 12 * RATE))
    harmony = analyze_region(path, duration_seconds=12)["harmony"]
    assert harmony["status"] == "insufficient_evidence"
    assert all(row["pitch_class_energy"] is None for row in harmony["windows"])


def test_antiphase_stereo_does_not_disappear(tmp_path):
    mono = clicks(duration=16)[0]
    path = audio_file(tmp_path, np.column_stack([mono, -mono]))
    result = analyze_region(path, duration_seconds=16, bpm_hint=120)
    assert not result["signal"]["silence_or_near_silence"]
    assert result["rhythm"]["selected_pulse_grid"]["bpm"] == pytest.approx(120, abs=.3)


@pytest.mark.parametrize("start,duration", [(-1, 1), (0, 0), (0, -1), (9, 2),
                                          (float("nan"), 1), (0, float("inf")), (0, 601)])
def test_invalid_and_out_of_bounds_regions_are_not_clamped(tmp_path, start, duration):
    path = audio_file(tmp_path, np.zeros(10 * RATE))
    with pytest.raises(PocketError):
        analyze_region(path, start, duration)


def test_subsample_region_rounds_inward_and_eof_is_accepted(tmp_path):
    path = audio_file(tmp_path, np.zeros(10 * RATE))
    result = analyze_region(path, .25 / RATE, 1)
    assert result["region"]["start_frame"] == 1
    assert result["region"]["end_frame_exclusive"] == RATE
    result = analyze_region(path, 9, 1)
    assert result["region"]["end_frame_exclusive"] == 10 * RATE
    with pytest.raises(PocketError):
        analyze_region(path, 0, .25 / RATE)


def test_nonfinite_audio_is_rejected(tmp_path):
    audio = np.zeros(RATE)
    audio[100] = np.nan
    path = audio_file(tmp_path, audio)
    with pytest.raises(PocketError, match="nonfinite"):
        analyze_region(path, duration_seconds=1)


def test_repeated_texture_candidates_keep_source_offsets(tmp_path):
    phrase, _ = clicks(duration=4, bpm=120)
    audio = np.concatenate([phrase, phrase * .7, phrase, phrase * .7])
    result = analyze_region(audio_file(tmp_path, audio), 4, 12, bpm_hint=120)
    candidates = result["repetitions"]["candidates"]
    assert any(row["source_a_start_seconds"] == 4 and row["source_b_start_seconds"] == 12
               for row in candidates)
    assert all(row["status"] == "similar_texture_not_confirmed_phrase" for row in candidates)


def test_result_is_deterministic_and_wrong_hint_does_not_supply_evidence(tmp_path):
    path = audio_file(tmp_path, clicks(duration=16, bpm=123)[0])
    first = analyze_region(path, duration_seconds=16, bpm_hint=90)
    second = analyze_region(path, duration_seconds=16, bpm_hint=90)
    assert first == second
    assert first["rhythm"]["selected_pulse_grid"]["bpm"] == pytest.approx(123, abs=.5)


def test_identity_read_race_is_rejected(tmp_path, monkeypatch):
    path = audio_file(tmp_path, np.zeros(2 * RATE))
    identify = peek.identify_audio

    def identify_then_change(source):
        identity = identify(source)
        sf.write(source, np.ones(2 * RATE) * .1, RATE, subtype="FLOAT")
        return identity

    monkeypatch.setattr(peek, "identify_audio", identify_then_change)
    with pytest.raises(PocketError, match="changed during analysis"):
        analyze_region(path, duration_seconds=2)


@pytest.mark.parametrize("extra_frames", [1, 2, 3])
def test_sub_analysis_frame_tail_has_explicit_harmonic_abstention(tmp_path, extra_frames):
    rate = 48000
    start_frame = 11515
    frames = 4 * rate + extra_frames
    time = np.arange(start_frame + frames) / rate
    path = tmp_path / "fractional-analysis-tail.wav"
    sf.write(path, .2 * np.sin(2 * np.pi * 440 * time), rate, subtype="FLOAT")
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        result = analyze_region(path, start_frame=start_frame, frames=frames)
    assert result["region"]["start_frame"] == start_frame
    assert result["region"]["frames"] == frames
    assert result["region"]["end_frame_exclusive"] == start_frame + frames
    windows = result["harmony"]["windows"]
    assert windows[0]["status"] == "local_pitch_class_evidence"
    assert windows[-1]["source_start_seconds"] == start_frame / rate + 4
    assert windows[-1]["source_end_seconds"] == pytest.approx((start_frame + frames) / rate)
    assert windows[-1]["status"] == "insufficient_tonal_evidence"
    assert windows[-1]["reason"] == "fewer_than_one_complete_analysis_frame"
    assert windows[-1]["rms_dbfs"] is None
    assert windows[-1]["pitch_class_energy"] is None
    json.dumps(result, allow_nan=False)
