# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded symbolic measurements, kept separate from listening and musical judgment."""
from __future__ import annotations

import json
from collections import Counter
from fractions import Fraction
from itertools import pairwise

from .artifact_store import receipt
from .errors import PocketError
from .material import load_material, qn, rational, resolve_selection
from .material_types import ArtifactHandle, MaterialRecord, MaterialSelection

MAX_RESPONSE_BYTES = 16 * 1024
MAX_RELATIONSHIP_NOTES = 512
_SIZE_ERROR = ('Analysis exceeds the 16 KiB response limit; use material_query with a smaller '
               'note/clip selection and request fewer analyses.')


def _bounded(value):
    # Check long opaque strings before the encoder can expand Unicode escapes.
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str) and len(item) > MAX_RESPONSE_BYTES:
            raise PocketError(_SIZE_ERROR)
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    size = 1  # CLI trailing newline is part of the public response budget.
    try:
        for chunk in json.JSONEncoder(indent=2, ensure_ascii=True, allow_nan=False).iterencode(value):
            size += len(chunk.encode('utf-8'))
            if size > MAX_RESPONSE_BYTES:
                raise PocketError(_SIZE_ERROR)
    except PocketError:
        raise
    except (ValueError, TypeError, RecursionError) as error:
        raise PocketError('Analysis cannot be represented as bounded JSON') from error
    return value


def _span(note):
    start = rational(note['onset'])
    return start, start + rational(note['duration_qn'])


def _note_evidence(note):
    start, end = _span(note)
    return {'note_id': note['id'], 'gate_span_qn': [qn(start), qn(end)], 'mute': note['mute'],
            'tuning_ref': note['pitch'].get('tuning_ref')}


def _relationship_scope(name, resolved):
    return {'schema': f'pocket.{name}-analysis/v1', 'basis': 'symbolic',
            'selection_sha256': resolved['selection_sha256'],
            'selected_note_count': len(resolved['note_ids']), 'time_space': 'clip_qn',
            'note_scope': 'selected_notes_only',
            'comparison_scope': 'within_each_clip', 'gate_policy': 'half_open',
            'mute_policy': 'included_as_symbolic_records', 'assignment_policy': 'declared_only',
            'clips': []}


def _voice_leading(local_clips, resolved):
    result = _relationship_scope('voice-leading', resolved)
    result.update(pitch_basis='static_midi_note_and_declared_cents',
                  tuning_policy='tuning:12tet-a440_only',
                  cents_rationalization='canonical_decimal_number',
                  expression_curves='not_interpreted',
                  gap_policy='next_onset_minus_previous_gate_end',
                  interval_policy='consecutive_singleton_groups_without_same_voice_gate_overlap')
    for clip, local in local_clips:
        voices = {}
        for note in local:
            voices.setdefault(note['voice_id'], []).append(note)
        clip_result = {'clip_id': clip['id'], 'voices': []}
        for voice_id, voice_notes in sorted(voices.items()):
            groups = {}
            for note in voice_notes:
                groups.setdefault(_span(note)[0], []).append(note)
            ordered = [(time, sorted(group, key=lambda note: note['id'])) for time, group in sorted(groups.items())]
            transitions = []
            for (left_time, left), (right_time, right) in pairwise(ordered):
                reasons = []
                if len(left) != 1 or len(right) != 1:
                    reasons.append('simultaneous_onset_group')
                # A note sustained from any earlier group can make either
                # endpoint polyphonic, even if these two groups are singletons.
                if any(start < time < end for note in voice_notes for start, end in [_span(note)]
                       for time in (left_time, right_time)):
                    reasons.append('same_voice_gate_overlap')
                if any(note['pitch'].get('tuning_ref') != 'tuning:12tet-a440' for note in [*left, *right]):
                    reasons.append('unresolved_tuning')
                row = {'from_onset_qn': qn(left_time), 'to_onset_qn': qn(right_time),
                       'from_notes': [_note_evidence(note) for note in left],
                       'to_notes': [_note_evidence(note) for note in right],
                       'status': 'abstained' if reasons else 'measured', 'abstentions': reasons,
                       'interval': None}
                if not reasons:
                    a, b = left[0], right[0]
                    midi_delta = b['pitch']['midi_note'] - a['pitch']['midi_note']
                    cents_delta = Fraction(str(b['pitch']['cents_offset'])) - Fraction(str(a['pitch']['cents_offset']))
                    total = 100 * midi_delta + cents_delta
                    row['interval'] = {'signed_midi_interval': midi_delta, 'cents_offset_delta': qn(cents_delta),
                                       'signed_total_cents': qn(total),
                                       'contour': 'up' if total > 0 else 'down' if total < 0 else 'same',
                                       'gap_qn': qn(right_time - _span(a)[1])}
                transitions.append(row)
                _bounded(transitions)
            clip_result['voices'].append({'voice_id': voice_id, 'note_count': len(voice_notes),
                'onset_group_count': len(ordered), 'transitions': transitions,
                'pitch_interval_contour_cents': [row['interval']['signed_total_cents'] if row['interval'] else None
                                                 for row in transitions]})
            _bounded(clip_result)
        result['clips'].append(clip_result)
        _bounded(result)
    return result


def _union(spans):
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _role_overlap(local_clips, resolved):
    result = _relationship_scope('role-overlap', resolved)
    result.update(role_pair_policy='unordered_distinct_declared_roles', unassigned_role_policy='null_or_empty',
                  pair_duration_policy='sum_of_note_pair_intersections_may_count_time_more_than_once',
                  shared_active_duration_policy='union_intersection_counts_time_once', evidence_limit_per_kind=8)
    for clip, local in local_clips:
        roles = {}
        for note in local:
            if note['role_ref']:
                roles.setdefault(note['role_ref'], []).append(note)
        role_ids = sorted(roles)
        clip_result = {'clip_id': clip['id'], 'unassigned_note_count': sum(not note['role_ref'] for note in local),
                       'declared_role_count': len(roles), 'role_pairs': []}
        for index, a_role in enumerate(role_ids):
            a_notes = sorted(roles[a_role], key=lambda note: (_span(note)[0], note['id']))
            for b_role in role_ids[index + 1:]:
                b_notes = sorted(roles[b_role], key=lambda note: (_span(note)[0], note['id']))
                pair_count = coincidence_count = 0
                duration_sum = Fraction(0)
                overlaps, coincidences = [], []
                for a in a_notes:
                    a_start, a_end = _span(a)
                    for b in b_notes:
                        b_start, b_end = _span(b)
                        start, end = max(a_start, b_start), min(a_end, b_end)
                        if start < end:
                            pair_count += 1
                            duration_sum += end - start
                            if len(overlaps) < 8:
                                overlaps.append({'note_ids': [a['id'], b['id']], 'span_qn': [qn(start), qn(end)]})
                        if a_start == b_start:
                            coincidence_count += 1
                            if len(coincidences) < 8:
                                coincidences.append({'note_ids': [a['id'], b['id']], 'onset_qn': qn(a_start)})
                shared_duration = sum((min(a_end, b_end) - max(a_start, b_start)
                    for a_start, a_end in _union(_span(note) for note in a_notes)
                    for b_start, b_end in _union(_span(note) for note in b_notes)
                    if max(a_start, b_start) < min(a_end, b_end)), Fraction(0))
                clip_result['role_pairs'].append({'roles': [a_role, b_role],
                    'gate_overlap_pair_count': pair_count, 'pair_duration_sum_qn': qn(duration_sum),
                    'shared_active_duration_qn': qn(shared_duration), 'onset_coincidence_pair_count': coincidence_count,
                    'overlap_evidence': overlaps, 'omitted_overlap_pairs': pair_count - len(overlaps),
                    'onset_evidence': coincidences, 'omitted_onset_pairs': coincidence_count - len(coincidences)})
                _bounded(clip_result)
        result['clips'].append(clip_result)
        _bounded(result)
    return result


def midi_analyze(material: ArtifactHandle | MaterialRecord, store_root: str,
                 selection: MaterialSelection | None = None,
                 analyses: list[str] | None = None) -> dict:
    """Measure symbolic facts and explicit declared-voice/role relationships, with bounded evidence."""
    if analyses is not None and (not isinstance(analyses, list) or any(not isinstance(name, str) for name in analyses)):
        raise PocketError('analyses must be a list of symbolic analysis names')
    record = load_material(material, store_root)
    requested = analyses or ['integrity', 'rhythm', 'pitch']
    if set(requested) - {'integrity', 'rhythm', 'pitch', 'voice_leading', 'role_overlap'}:
        raise PocketError('Available symbolic analyses: integrity, rhythm, pitch, voice_leading, role_overlap')
    resolved = resolve_selection(record, selection)
    relationships = set(requested) & {'voice_leading', 'role_overlap'}
    if relationships and isinstance(selection, dict) and {'material_revision', 'selection_sha256'} & selection.keys():
        resolved = resolve_selection(record, selection, require_hash=True)
    if relationships and len(resolved['note_ids']) > MAX_RELATIONSHIP_NOTES:
        raise PocketError('Relationship analysis supports at most 512 selected notes; use material_query for a smaller selection.')
    selected = set(resolved['note_ids'])
    notes = [n for n in record['notes'] if n['id'] in selected]
    facts = {'note_count': len(notes)}
    if 'integrity' in requested:
        facts['integrity'] = {'issues': record['coverage'].get('issues', [])[:32],
                              'omitted_issues': max(0, len(record['coverage'].get('issues', [])) - 32),
                              'editing_allowed': record['coverage'].get('editing_allowed', False)}
    if 'pitch' in requested:
        pitches = [n['pitch']['midi_note'] for n in notes]
        facts['pitch'] = {'midi_min': min(pitches) if pitches else None, 'midi_max': max(pitches) if pitches else None,
                         'pitch_class_counts': {str(k): v for k, v in sorted(Counter(p % 12 for p in pitches).items())}}
    if 'rhythm' in requested:
        per_clip = []
        for clip in record['clips']:
            local = [n for n in notes if n['id'] in clip['note_ids']]
            onsets = sorted({rational(n['onset']) for n in local})
            gaps = Counter(b - a for a, b in pairwise(onsets))
            per_clip.append({'clip_id': clip['id'], 'attacks': len(onsets),
                             'first_onset_qn': qn(onsets[0]) if onsets else None,
                             'last_gate_end_qn': qn(max(rational(n['onset']) + rational(n['duration_qn']) for n in local)) if local else None,
                             'inter_onset_intervals': [{'interval_qn': qn(k), 'count': v} for k, v in sorted(gaps.items())][:8],
                             'omitted_interval_types': max(0, len(gaps) - 8)})
        facts['rhythm'] = per_clip[:16]
        facts['omitted_clips'] = max(0, len(per_clip) - 16)
    if relationships:
        note_by_id = {note['id']: note for note in notes}
        local_clips = [(clip, [note_by_id[key] for key in clip['note_ids'] if key in note_by_id])
                       for clip in record['clips'] if any(key in note_by_id for key in clip['note_ids'])]
        if 'voice_leading' in requested:
            facts['voice_leading'] = _voice_leading(local_clips, resolved)
        if 'role_overlap' in requested:
            facts['role_overlap'] = _role_overlap(local_clips, resolved)
    result = {'result_schema': 'pocket.midi-analysis/v1', 'material_revision': record['revision_sha256'],
            'basis': 'symbolic', 'provider': 'symbolic-v1', 'measurements': facts,
            'hypotheses': [], 'abstentions': ['No unique key, downbeat or musical usefulness inferred.',
                'Acoustic release, masking, loudness and groove judgment require sound and listening.'],
            'listening': 'not_performed'}

    _bounded(result)
    return _bounded(receipt(coverage={'basis': 'symbolic', 'native': 'not_performed', 'listening': 'not_performed'}, **result))
