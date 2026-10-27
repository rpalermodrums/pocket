"""Pure retained-array note ledger, v2 retained-frame count; no model runtime.

Decoder derivative: Copyright 2022 Spotify AB, Apache License 2.0; see shipped
basic_pitch_LICENSE.txt. Sources: spotify/basic-pitch 0.4.0 note_creation.py,
SHA256 9c813509acf57ed9b902d2b9fcd9f8118b2c5ffe568a06df9cfa60f5c5ee2572, and the
retained-frame count of unwrap_output in inference.py at commit e989e40 (#179),
SHA256 b85aeaa13d421861378d8bd5e841d9087cecd3dc4bfd15abc1f40c1ea2b8abc9.
Changes: restrict to explicit infer_onsets=False, melodia_trick=False, no frequency
filter; validate arrays/bounds; preserve vendor float32 mean and traversal order;
retain source-coordinate projection separately from the vendor clock estimate.
Unlike audio_note_projection.py (v1), keep the model frames that the 36164-sample
hops cover, not floor(samples * 86 / 22050), so a note still sounding at the end
of a region is no longer cut short or dropped.

This file is complete on its own. Its exact bytes are this profile's recorded
identity, as v1's are, so v1 stays unchanged and existing records keep verifying.
"""
from __future__ import annotations

import math
from fractions import Fraction

import numpy as np
from scipy.signal import argrelmax

from .errors import PocketError

DECODER = 'basic_pitch_0_4_0_false_false_v2'
# int(441000 / 36164 * 142): the frames kept for the longest (20 s) region.
MAX_FRAMES = 1731
TRANSFORM = {'sample_rate': 22050, 'window_samples': 43844, 'prefix_zeros': 3840,
             'advance_samples': 36164, 'window_frames': 172, 'trim_frames': 15,
             'retained_window_frames': 142,
             'retained_frame_count': 'int(resampled_frames / advance_samples * retained_window_frames)',
             'retained_frame_count_source': 'spotify/basic-pitch e989e40 (#179) inference.py unwrap_output',
             'vendor_time_hop': 256, 'vendor_time_correction_seconds': .0018,
             'time_semantics': 'vendor_float_estimate_not_exact_acoustic_onset',
             'downmix': 'arithmetic_mean_float32', 'resampler': 'soxr_hq',
             'source_normalization': 'none', 'source_clipping': 'none'}
SETTINGS = {'device': 'cpu', 'dtype': 'float32', 'threads': 1, 'downmix': 'arithmetic_mean',
            'resampler': 'soxr_hq', 'decoder': DECODER, 'onset_threshold': .5,
            'frame_threshold': .3, 'min_note_frames': 11, 'energy_tol': 11,
            'infer_onsets': False, 'melodia_trick': False}


def _matrix(value, bins, label):
    if (not isinstance(value, np.ndarray) or value.dtype != np.dtype('float32')
            or value.ndim != 2 or value.shape[1] != bins or not 1 <= value.shape[0] <= MAX_FRAMES
            or not np.isfinite(value).all() or np.any(value < 0) or np.any(value > 1)):
        raise PocketError(f'Invalid bounded float32 {label} activations')
    return value


def retained_frame_count(resampled_frames):
    """Upstream e989e40's count, evaluated the same way: float windows times frames per window."""
    if type(resampled_frames) is not int or not 44100 <= resampled_frames <= 441000:
        raise PocketError('Model source must span 2-20 resampled seconds')
    return int(resampled_frames / 36164 * 142)


def decode_note_frames(frames, onsets):
    """Exact narrow Apache 2.0 vendor profile, independently executable in base runtime."""
    frames = _matrix(frames, 88, 'note')
    onsets = _matrix(onsets, 88, 'onset')
    if frames.shape != onsets.shape: raise PocketError('Note/onset shape mismatch')
    peak_threshold = np.zeros(onsets.shape)
    peaks = argrelmax(onsets, axis=0)
    peak_threshold[peaks] = onsets[peaks]
    onset_indices = np.where(peak_threshold >= .5)
    remaining = np.zeros(frames.shape)
    remaining[:, :] = frames[:, :]
    events = []
    for start, frequency in zip(onset_indices[0][::-1], onset_indices[1][::-1], strict=True):
        if start >= frames.shape[0] - 1: continue
        end, below = start + 1, 0
        while end < frames.shape[0] - 1 and below < 11:
            below = below + 1 if remaining[end, frequency] < .3 else 0
            end += 1
        end -= below
        if end - start <= 11: continue
        remaining[start:end, frequency] = 0
        if frequency < 87: remaining[start:end, frequency + 1] = 0
        if frequency > 0: remaining[start:end, frequency - 1] = 0
        # Do not upcast: float32 vendor reduction is part of this decoder profile.
        amplitude = np.mean(frames[start:end, frequency])
        events.append([int(start), int(end), int(frequency + 21), float(amplitude)])
        if len(events) > 4096: raise PocketError('Decoded note ledger exceeds 4096 candidates')
    return events


def vendor_frame_times(count):
    if type(count) is not int or not 1 <= count <= MAX_FRAMES: raise PocketError('Invalid model frame count')
    indices = np.arange(count)
    original = indices * 256 / 22050
    correction = (256 / 22050) * (172 - 43844 / 256) + .0018
    return original - correction * np.floor(indices / 172)


def _q(value):
    return {'n': value.numerator, 'd': value.denominator}


def project_note_arrays(windows, source, resampled_frames):
    """Recompute complete window->frame->event ledger with exact float-origin envelopes."""
    if not isinstance(windows, dict) or set(windows) != {'note', 'onset', 'contour'}:
        raise PocketError('Expected complete note/onset/contour window arrays')
    frame_count = retained_frame_count(resampled_frames)
    starts = list(range(0, resampled_frames + 3840, 36164))
    arrays = {}
    for name, bins in [('note', 88), ('onset', 88), ('contour', 264)]:
        value = windows[name]
        if (not isinstance(value, np.ndarray) or value.dtype != np.dtype('float32')
                or value.shape != (len(starts), 172, bins) or not np.isfinite(value).all()
                or np.any(value < 0) or np.any(value > 1)):
            raise PocketError('Malformed model window arrays, including discarded edge frames')
        arrays[name] = value[:, 15:-15, :].reshape(-1, bins)[:frame_count]
        _matrix(arrays[name], bins, name)
        # The retained windows always cover the count; never let a short slice pass silently.
        if arrays[name].shape[0] != frame_count: raise PocketError('Retained windows do not cover the frame count')
    decoded = decode_note_frames(arrays['note'], arrays['onset'])
    times = vendor_frame_times(frame_count)
    events, ledger, excluded = [], [], []
    low, high, rate = source['start_frame'], source['end_frame_exclusive'], source['sample_rate']
    for start, end, pitch, amplitude in decoded:
        start_seconds, end_seconds = float(times[start]), float(times[end])
        left_q = Fraction.from_float(start_seconds) * rate + low
        right_q = Fraction.from_float(end_seconds) * rate + low
        left, right = math.floor(left_q), math.ceil(right_q)
        row = {'start_model_frame': start, 'end_model_frame': end, 'midi_note': pitch,
               'amplitude_estimate': amplitude, 'local_start_seconds': start_seconds,
               'local_end_seconds': end_seconds, 'local_start_hex': start_seconds.hex(),
               'local_end_hex': end_seconds.hex(), 'original_start_frame_q': _q(left_q),
               'original_end_frame_q': _q(right_q), 'outward_envelope': {'start_frame': left, 'end_frame_exclusive': right},
               'raw_start': {'window_index': start // 142, 'frame_index': 15 + start % 142},
               'raw_end': {'window_index': end // 142, 'frame_index': 15 + end % 142}}
        if not low <= left_q < right_q <= high or not low <= left < right <= high:
            excluded.append({**row, 'reason': 'outside_or_empty_half_open_source_envelope'})
        else:
            row['event_index'] = len(events)
            events.append({'kind': 'note_hypothesis', 'start_frame': left, 'end_frame_exclusive': right,
                           'midi_note': pitch, 'cents': 0, 'tuning_ref': 'model-bin:12tet-a440'})
        ledger.append(row)
    return {'decoder': DECODER, 'frame_count': frame_count, 'window_starts': starts,
            'events': events, 'ledger': ledger, 'excluded': excluded,
            'uncertainty': ['Vendor time estimates and outward rounding are not exact acoustic onset or confidence intervals.',
                            'Pitch bins and amplitude do not establish tuning, voice, instrument, or MIDI expression.']}
