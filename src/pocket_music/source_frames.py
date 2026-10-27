# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit inward frame intervals; never silently expand a requested crop."""
from __future__ import annotations

import math

from .errors import PocketError
from .timing import finite

BOUNDARY_TOLERANCE_SAMPLES = 0.1
FRAME_POLICY = 'inward_complete_frames_with_0.1_sample_file_boundary_snap/v1'


def source_frame_interval(start_seconds: float, end_seconds: float,
                          sample_rate: int, source_frames: int) -> dict:
    """Convert continuous source bounds to [ceil(start), floor(end)).

    Only deviations just *outside the file* may snap to zero/EOF (at most
    0.1 sample). Interior positions do not acquire a broad rounding tolerance.
    One-ULP nextafter handles exact-frame floating multiplication consistently
    with Peek. Raw/snapped positions and every adjustment remain explicit.
    """
    start = finite(start_seconds, 'source start seconds')
    end = finite(end_seconds, 'source end seconds')
    if (isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or sample_rate <= 0
            or isinstance(source_frames, bool) or not isinstance(source_frames, int) or source_frames <= 0):
        raise PocketError('Source sample rate and frame count must be positive integers')
    if end <= start:
        raise PocketError('Source interval must have positive duration')
    raw_start, raw_end = start * sample_rate, end * sample_rate
    if not math.isfinite(raw_start) or not math.isfinite(raw_end):
        raise PocketError('Source frame coordinates must be finite')
    result = {'policy': FRAME_POLICY, 'tolerance_samples': BOUNDARY_TOLERANCE_SAMPLES,
              'sample_rate': sample_rate, 'source_frames': source_frames,
              'raw_start_seconds': start, 'raw_end_seconds': end,
              'raw_start_frame': raw_start, 'raw_end_frame': raw_end,
              'start_frame': None, 'end_frame_exclusive': None, 'adjustments': []}
    if raw_start < -BOUNDARY_TOLERANCE_SAMPLES or raw_end > source_frames + BOUNDARY_TOLERANCE_SAMPLES:
        return {**result, 'status': 'out_of_bounds', 'reason': 'Source interval exceeds the file boundary tolerance'}
    if raw_start >= source_frames or raw_end <= 0:
        return {**result, 'status': 'out_of_bounds', 'reason': 'Source interval has no positive intersection with the file'}
    snapped_start, snapped_end = raw_start, raw_end
    if raw_start < 0:
        snapped_start = 0.0
        result['adjustments'].append({'boundary': 'start', 'reason': 'native_file_boundary_precision',
                                      'delta_samples': -raw_start})
    if raw_end > source_frames:
        snapped_end = float(source_frames)
        result['adjustments'].append({'boundary': 'end', 'reason': 'native_file_boundary_precision',
                                      'delta_samples': source_frames - raw_end})
    first = max(0, math.ceil(math.nextafter(snapped_start, -math.inf)))
    last = min(source_frames, math.floor(math.nextafter(snapped_end, math.inf)))
    result.update(snapped_start_seconds=snapped_start / sample_rate,
                  snapped_end_seconds=snapped_end / sample_rate,
                  start_frame=first, end_frame_exclusive=last,
                  start_seconds=first / sample_rate, end_seconds=last / sample_rate,
                  frames=max(0, last - first))
    if last <= first:
        return {**result, 'status': 'empty', 'reason': 'Requested interval contains no complete source frames'}
    start_arg, end_arg = first / sample_rate, last / sample_rate
    duration_arg = end_arg - start_arg
    restored_first = math.ceil(math.nextafter(start_arg * sample_rate, -math.inf))
    restored_last = math.floor(math.nextafter((start_arg + duration_arg) * sample_rate, math.inf))
    safe = restored_first == first and restored_last == last
    result['analyze_region_frame_args'] = {'start_frame': first, 'frames': last - first}
    result['analyze_region_args'] = {'start_seconds': start_arg, 'duration_seconds': duration_arg} if safe else None
    result['seconds_handoff'] = 'inward frame roundtrip verified' if safe else 'use integer frames; seconds roundtrip not exact'
    return {**result, 'status': 'valid', 'reason': None}
