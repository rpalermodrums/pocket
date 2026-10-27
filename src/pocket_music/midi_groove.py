# SPDX-License-Identifier: AGPL-3.0-only
"""Exact supplied-template timing for the shared selected-material editor."""
from __future__ import annotations

from bisect import bisect_right
from itertools import pairwise

from .artifact_store import digest
from .errors import PocketError
from .material import qn, rational

GROOVE_FIELDS = {'op', 'cycle_qn', 'anchors', 'phase_qn', 'strength', 'threshold_qn',
                 'ties', 'note_off', 'controller_timeline', 'time_space'}


def _rational(value, name):
    if isinstance(value, dict) and set(value) != {'n', 'd'}:
        raise PocketError(f'{name} requires exactly n and d')
    return rational(value, name)


def apply_groove(notes, operation):
    """Mutate only staged note onsets; return full template/offset evidence.

    Nearest nominal anchors select an offset. Existing deviation from that
    anchor is preserved, rather than silently quantized away.
    """
    if set(operation) != GROOVE_FIELDS or operation['op'] != 'groove':
        raise PocketError('Groove has missing or unsupported fields')
    if (operation['time_space'] != 'clip_qn' or operation['note_off'] != 'follow_onset'
            or operation['controller_timeline'] != 'preserve_existing'
            or operation['ties'] not in ('earlier', 'later')):
        raise PocketError('Groove requires explicit clip timing, ties and preserved controller timeline')
    cycle, phase, strength, threshold = (_rational(operation[field], field)
        for field in ('cycle_qn', 'phase_qn', 'strength', 'threshold_qn'))
    if cycle <= 0 or not 0 <= strength <= 1 or not 0 <= threshold <= cycle / 2:
        raise PocketError('Groove requires positive cycle, strength 0..1 and threshold 0..cycle/2')
    anchors = operation['anchors']
    if not isinstance(anchors, list) or not 1 <= len(anchors) <= 256:
        raise PocketError('Groove requires 1–256 explicit nominal/offset anchors')
    parsed = []
    for anchor in anchors:
        if not isinstance(anchor, dict) or set(anchor) != {'nominal_qn', 'offset_qn'}:
            raise PocketError('Groove anchor requires exactly nominal_qn and offset_qn')
        nominal, offset = (_rational(anchor[field], field) for field in ('nominal_qn', 'offset_qn'))
        if not 0 <= nominal < cycle or abs(offset) > cycle / 2:
            raise PocketError('Groove nominal must lie in its cycle and offset within half a cycle')
        if parsed and nominal <= parsed[-1][0]:
            raise PocketError('Groove nominal anchors must be strictly increasing')
        parsed.append((nominal, offset))
    targets = [nominal + offset for nominal, offset in parsed]
    if any(right <= left for left, right in pairwise(targets)) or targets[-1] >= targets[0] + cycle:
        raise PocketError('Groove target anchors must remain strictly ordered through the cycle seam')
    nominal_times = [row[0] for row in parsed]
    normalized = {'cycle_qn': qn(cycle), 'anchors': [
        {'nominal_qn': qn(nominal), 'offset_qn': qn(offset)} for nominal, offset in parsed]}
    changes, outside, largest = [], 0, rational(0)
    for note in notes:
        onset = rational(note['onset'])
        occurrence = (onset - phase) // cycle
        local = onset - phase - occurrence * cycle
        index = bisect_right(nominal_times, local)
        lower_index = (index - 1) % len(parsed)
        upper_index = index % len(parsed)
        lower_cycle = occurrence - (index == 0)
        upper_cycle = occurrence + (index == len(parsed))
        lower = phase + lower_cycle * cycle + parsed[lower_index][0]
        upper = phase + upper_cycle * cycle + parsed[upper_index][0]
        earlier = onset - lower < upper - onset or (onset - lower == upper - onset and operation['ties'] == 'earlier')
        anchor_time, anchor_index, anchor_cycle = ((lower, lower_index, lower_cycle) if earlier
                                                  else (upper, upper_index, upper_cycle))
        if abs(onset - anchor_time) > threshold:
            outside += 1
            continue
        delta = strength * parsed[anchor_index][1]
        note['onset'] = {'space': 'clip_qn', **qn(onset + delta)}
        largest = max(largest, abs(delta))
        changes.append({'note_id': note['id'], 'anchor_index': anchor_index,
                        'cycle_index': anchor_cycle, 'nominal_qn': qn(anchor_time),
                        'delta_qn': qn(delta), 'onset_before': qn(onset), 'onset_after': qn(onset + delta)})
    return {'op': 'groove', 'template_sha256': digest(normalized), 'template': normalized,
            'selected': len(notes), 'outside_threshold': outside,
            'moved': sum(row['delta_qn']['n'] != 0 for row in changes),
            'maximum_onset_change_qn': qn(largest), 'applied': changes,
            'off_grid_deviation': 'preserved', 'gate': 'unchanged',
            'note_relative_curves': 'exact', 'controller_timeline': 'preserve_existing',
            'clip_length': 'unchanged', 'native': 'not_performed'}
