# SPDX-License-Identifier: AGPL-3.0-only
"""Independent symbolic voice and role expectations; no listening/native fixtures."""
from __future__ import annotations

import copy
import hashlib
import json
import sys

import pytest

from pocket_music.artifact_store import put_record, read_bytes
from pocket_music.errors import PocketError
from pocket_music.material import material_query
from pocket_music.midi_analysis import midi_analyze


def q(n, d=1):
    return {'n': n, 'd': d}


def seal(record):
    record = copy.deepcopy(record)
    record['revision_sha256'] = hashlib.sha256(json.dumps(
        {key: value for key, value in record.items() if key != 'revision_sha256'},
        sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()).hexdigest()
    return record


def note(key, onset=0, duration=1, pitch=60, cents=0, voice='voice:declared', role='role:declared', mute=False):
    return {'id': key, 'voice_id': voice, 'role_ref': role,
            'onset': {'space': 'clip_qn', **(q(onset) if isinstance(onset, int) else onset)},
            'duration_qn': q(duration) if isinstance(duration, int) else duration,
            'pitch': {'midi_note': pitch, 'cents_offset': cents, 'tuning_ref': 'tuning:12tet-a440'},
            'velocity': {'value': 80, 'domain': 'midi1_7bit'},
            'release_velocity': {'value': 37, 'domain': 'midi1_7bit'}, 'channel': 1,
            'mute': mute, 'expression_refs': [], 'source_binding': None, 'derived_from': []}


def material(notes, *, second_clip=None):
    clips = [{'id': 'clip:a', 'track_id': 'track:a', 'origin': {'space': 'phrase_qn', **q(0)},
              'length_qn': q(2048), 'loop': False, 'note_ids': [n['id'] for n in notes],
              'event_ids': [], 'curve_ids': []}]
    if second_clip is not None:
        clips.append({**clips[0], 'id': 'clip:b', 'note_ids': [n['id'] for n in second_clip]})
        notes = [*notes, *second_clip]
    return seal({'schema': 'pocket.material/v1', 'material_id': 'literal:relationships', 'revision_sha256': '',
        'parent_revision': None, 'sources': [], 'tracks': [{'id': 'track:a', 'name': 'Authored fixture'}],
        'clips': clips, 'notes': notes, 'events': [], 'curves': [], 'tempo_map_ref': None, 'meter_map_ref': None,
        'coverage': {'editing_allowed': True, 'issues': []},
        'provenance': {'provider': 'independent_literal_author', 'musical_decision': None}})


def analyze(record, tmp_path, names=None, **kwargs):
    return midi_analyze(record, str(tmp_path / 'store'), analyses=names, **kwargs)


def voice_result(record, tmp_path):
    return analyze(record, tmp_path, ['voice_leading'])['measurements']['voice_leading']


def role_result(record, tmp_path):
    return analyze(record, tmp_path, ['role_overlap'])['measurements']['role_overlap']


def test_default_and_empty_analysis_list_retain_existing_output(tmp_path):
    record = material([note('a', 0, q(1, 2)), note('b', 1, 1, 64)])
    result = analyze(record, tmp_path)
    assert result == analyze(record, tmp_path, [])
    assert set(result['measurements']) == {'note_count', 'integrity', 'pitch', 'rhythm', 'omitted_clips'}
    assert result['measurements']['pitch'] == {'midi_min': 60, 'midi_max': 64, 'pitch_class_counts': {'0': 1, '4': 1}}
    assert result['measurements']['rhythm'] == [{'clip_id': 'clip:a', 'attacks': 2,
        'first_onset_qn': q(0), 'last_gate_end_qn': q(2),
        'inter_onset_intervals': [{'interval_qn': q(1), 'count': 1}], 'omitted_interval_types': 0}]
    assert result['coverage'] == {'basis': 'symbolic', 'native': 'not_performed', 'listening': 'not_performed'}
    assert result['hypotheses'] == [] and result['listening'] == 'not_performed'


@pytest.mark.parametrize('bad', [True, 3, 'voice_leading', ('voice_leading',), {}, [None], [True], [[]], ['inferred_key']])
def test_malformed_analysis_names_raise_domain_error(tmp_path, bad):
    with pytest.raises(PocketError):
        analyze(material([note('a')]), tmp_path, bad)
    assert not (tmp_path / 'store').exists()


def test_voice_interval_contour_uses_exact_declared_pitch_and_gate_gaps(tmp_path):
    record = material([note('a', 0, 1, 60, 0.25), note('b', 1, q(1, 2), 64, -0.5),
                       note('c', 2, q(1, 2), 60, -0.5), note('d', 3, 1, 60, -0.5)])
    result = voice_result(record, tmp_path)
    assert result['gap_policy'] == 'next_onset_minus_previous_gate_end'
    voice = result['clips'][0]['voices'][0]
    rows = voice['transitions']
    assert voice['pitch_interval_contour_cents'] == [q(1597, 4), q(-400), q(0)]
    assert [row['interval']['contour'] for row in rows] == ['up', 'down', 'same']
    assert [row['interval']['gap_qn'] for row in rows] == [q(0), q(1, 2), q(1, 2)]
    assert rows[0]['interval'] == {'signed_midi_interval': 4, 'cents_offset_delta': q(-3, 4),
                                   'signed_total_cents': q(1597, 4), 'contour': 'up', 'gap_qn': q(0)}
    assert rows[0]['from_notes'] == [{'note_id': 'a', 'gate_span_qn': [q(0), q(1)], 'mute': False,
                                     'tuning_ref': 'tuning:12tet-a440'}]
    assert rows[0]['to_notes'][0]['gate_span_qn'] == [q(1), q(3, 2)]
    assert all(row['status'] == 'measured' and row['abstentions'] == [] for row in rows)
    assert result['tuning_policy'] == 'tuning:12tet-a440_only'
    assert result['expression_curves'] == 'not_interpreted'


def test_other_declared_voice_does_not_make_monophonic_voice_ambiguous(tmp_path):
    record = material([note('a', 0, q(1, 2)), note('b', 1, q(1, 2), 64),
                       note('other', 0, 4, voice='voice:other')])
    result = voice_result(record, tmp_path)
    voice = next(v for v in result['clips'][0]['voices'] if v['voice_id'] == 'voice:declared')
    assert voice['transitions'][0]['interval']['signed_total_cents'] == q(400)


def test_selecting_away_a_sustained_note_is_explicitly_only_selected_context(tmp_path):
    record = material([note('held', 0, 4), note('b', 1, q(1, 4)), note('c', 2, q(1, 4), 64)])
    selection = material_query(record, str(tmp_path / 'store'), query='events',
                               selection={'note_ids': ['b', 'c']})['selection']
    result = analyze(record, tmp_path, ['voice_leading'], selection=selection)['measurements']['voice_leading']
    assert result['note_scope'] == 'selected_notes_only'
    assert result['selected_note_count'] == 2 and result['selection_sha256'] == selection['selection_sha256']
    row = result['clips'][0]['voices'][0]['transitions'][0]
    assert row['status'] == 'measured' and row['interval']['gap_qn'] == q(3, 4)
    assert result['clips'][0]['voices'][0]['note_count'] == 2


def test_chord_groups_abstain_and_order_is_independent_of_storage_order(tmp_path):
    record = material([note('c', 1, 1, 67), note('b', 0, 1, 64), note('a', 0, 1, 60)])
    first = voice_result(record, tmp_path)['clips']
    record['notes'].reverse()
    record['clips'][0]['note_ids'].reverse()
    assert voice_result(seal(record), tmp_path)['clips'] == first
    row = first[0]['voices'][0]['transitions'][0]
    assert row['status'] == 'abstained' and row['interval'] is None
    assert row['abstentions'] == ['simultaneous_onset_group']
    assert [n['note_id'] for n in row['from_notes']] == ['a', 'b']


@pytest.mark.parametrize('duration', [q(3, 2), 4])
def test_sustained_note_ambiguity_is_checked_at_both_endpoint_attacks(tmp_path, duration):
    record = material([note('a', 0, duration), note('b', 1, q(1, 4)), note('c', 2, q(1, 4))])
    rows = voice_result(record, tmp_path)['clips'][0]['voices'][0]['transitions']
    assert len(rows) == 2
    assert all(row['interval'] is None and 'same_voice_gate_overlap' in row['abstentions'] for row in rows)


@pytest.mark.parametrize('left,right', [('custom:X', 'custom:X'), ('custom:X', 'custom:Y'),
                                       ('tuning:12tet-a440', ''), ('', '')])
def test_custom_unknown_or_mismatched_tuning_never_implies_cent_intervals(tmp_path, left, right):
    a, b = note('a'), note('b', 1, 1, 64)
    a['pitch']['tuning_ref'], b['pitch']['tuning_ref'] = left, right
    row = voice_result(material([a, b]), tmp_path)['clips'][0]['voices'][0]['transitions'][0]
    assert row['interval'] is None and row['abstentions'] == ['unresolved_tuning']
    assert row['from_notes'][0]['gate_span_qn'] == [q(0), q(1)]
    assert row['from_notes'][0]['tuning_ref'] == left and row['to_notes'][0]['tuning_ref'] == right


def test_distinct_clip_clocks_are_never_compared(tmp_path):
    record = material([note('a', 0, 4, role='bass')], second_clip=[note('b', 0, 4, role='upper')])
    result = analyze(record, tmp_path, ['voice_leading', 'role_overlap'])['measurements']
    assert len(result['voice_leading']['clips']) == len(result['role_overlap']['clips']) == 2
    assert all(clip['voices'][0]['transitions'] == [] for clip in result['voice_leading']['clips'])
    assert all(clip['role_pairs'] == [] for clip in result['role_overlap']['clips'])


def test_role_pair_sum_and_union_are_distinct_exact_measurements(tmp_path):
    record = material([note('b1', 0, 3, role='bass'), note('b2', 2, 2, role='bass'),
                       note('u1', 1, 2, role='upper'), note('u2', 2, 3, role='upper')])
    row = role_result(record, tmp_path)['clips'][0]['role_pairs'][0]
    assert row['roles'] == ['bass', 'upper']
    assert row['gate_overlap_pair_count'] == 4
    assert row['pair_duration_sum_qn'] == q(6)
    assert row['shared_active_duration_qn'] == q(3)
    assert row['onset_coincidence_pair_count'] == 1
    assert row['overlap_evidence'] == [
        {'note_ids': ['b1', 'u1'], 'span_qn': [q(1), q(3)]},
        {'note_ids': ['b1', 'u2'], 'span_qn': [q(2), q(3)]},
        {'note_ids': ['b2', 'u1'], 'span_qn': [q(2), q(3)]},
        {'note_ids': ['b2', 'u2'], 'span_qn': [q(2), q(4)]}]
    assert row['onset_evidence'] == [{'note_ids': ['b2', 'u2'], 'onset_qn': q(2)}]


def test_half_open_gates_touch_without_overlap_and_roles_are_not_guessed(tmp_path):
    record = material([note('a', 0, 1, role='bass'), note('b', 1, 1, role='upper'),
                       note('null', 0, 2, role=None), note('empty', 0, 2, role='')])
    clip = role_result(record, tmp_path)['clips'][0]
    assert clip['unassigned_note_count'] == 2 and clip['declared_role_count'] == 2
    pair = clip['role_pairs'][0]
    assert pair['gate_overlap_pair_count'] == pair['onset_coincidence_pair_count'] == 0
    assert pair['pair_duration_sum_qn'] == pair['shared_active_duration_qn'] == q(0)
    assert pair['overlap_evidence'] == pair['onset_evidence'] == []


def test_role_evidence_is_bounded_but_counts_are_complete(tmp_path):
    record = material([note(f'a:{i:02}', 0, 1, role='a') for i in range(10)] + [note('b', 0, 1, role='b')])
    row = role_result(record, tmp_path)['clips'][0]['role_pairs'][0]
    assert row['gate_overlap_pair_count'] == row['onset_coincidence_pair_count'] == 10
    assert row['pair_duration_sum_qn'] == q(10) and row['shared_active_duration_qn'] == q(1)
    assert len(row['overlap_evidence']) == len(row['onset_evidence']) == 8
    assert row['omitted_overlap_pairs'] == row['omitted_onset_pairs'] == 2
    assert [entry['note_ids'][0] for entry in row['overlap_evidence']] == [f'a:{i:02}' for i in range(8)]


def test_muted_records_are_explicitly_symbolic_not_silently_removed(tmp_path):
    record = material([note('a', 0, 1, role='a', mute=True), note('b', 1, 1, role='b'),
                       note('c', 0, 1, role='c', voice='different')])
    result = analyze(record, tmp_path, ['voice_leading', 'role_overlap'])['measurements']
    assert result['voice_leading']['mute_policy'] == result['role_overlap']['mute_policy'] == 'included_as_symbolic_records'
    row = next(v for v in result['voice_leading']['clips'][0]['voices'] if v['voice_id'] == 'voice:declared')
    assert row['transitions'][0]['from_notes'][0]['mute'] is True
    pair = next(p for p in result['role_overlap']['clips'][0]['role_pairs'] if p['roles'] == ['a', 'c'])
    assert pair['gate_overlap_pair_count'] == 1


def test_exact_selection_and_full_gates_preserve_source_without_optional_dependencies(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'mido', None)
    record = material([note('a', 0, 4), note('b', 1, 1), note('c', 3, 1)])
    store = tmp_path / 'store'
    handle = put_record(record, store)
    original = read_bytes(handle, store)
    files = {p.relative_to(store).as_posix(): p.read_bytes() for p in store.rglob('*') if p.is_file()}
    query = material_query(handle, str(store), query='events', selection={'clip_ids': ['clip:a'], 'span_qn': [0, 2]})
    result = analyze(handle, tmp_path, ['voice_leading'], selection=query['selection'])
    fact = result['measurements']['voice_leading']
    assert fact['selection_sha256'] == query['selection']['selection_sha256'] and fact['selected_note_count'] == 2
    assert fact['clips'][0]['voices'][0]['transitions'][0]['from_notes'][0]['gate_span_qn'] == [q(0), q(4)]
    assert read_bytes(handle, store) == original
    assert {p.relative_to(store).as_posix(): p.read_bytes() for p in store.rglob('*') if p.is_file()} == files
    assert len((json.dumps(result, indent=2, ensure_ascii=True) + '\n').encode()) <= 16384


@pytest.mark.parametrize('field', ['material_revision', 'selection_sha256'])
def test_new_relationship_rejects_stale_resolved_selection(tmp_path, field):
    record = material([note('a'), note('b', 1)])
    selection = material_query(record, str(tmp_path / 'store'), query='events')['selection']
    selection[field] = '0' * 64
    with pytest.raises(PocketError, match='Stale'):
        analyze(record, tmp_path, ['role_overlap'], selection=selection)


def test_relationship_note_bound_does_not_restrict_existing_pitch_statistics(tmp_path):
    record = material([note(str(i), i) for i in range(513)])
    assert analyze(record, tmp_path, ['pitch'])['measurements']['note_count'] == 513
    with pytest.raises(PocketError, match='512'):
        analyze(record, tmp_path, ['voice_leading'])
    result = analyze(record, tmp_path, ['voice_leading'], selection={'note_ids': ['0', '1']})
    assert result['measurements']['voice_leading']['selected_note_count'] == 2


@pytest.mark.parametrize('field', ['voice_id', 'role_ref', 'id', 'clip_id'])
def test_long_unicode_identity_refuses_complete_output_without_mutation(tmp_path, field):
    a, b = note('a', role='a'), note('b', 1, role='b')
    if field != 'clip_id':
        a[field] = '\U0001f3b9' * 3000
    record = material([a, b])
    if field == 'clip_id':
        record['clips'][0]['id'] = '\U0001f3b9' * 3000
        record = seal(record)
    before = copy.deepcopy(record)
    with pytest.raises(PocketError, match='16 KiB'):
        analyze(record, tmp_path, ['voice_leading', 'role_overlap'])
    assert record == before and not (tmp_path / 'store').exists()


def test_large_legacy_output_refuses_instead_of_silent_truncation(tmp_path):
    record = material([note('a')])
    record['coverage']['issues'] = ['\U0001f3b9' * 3000]
    with pytest.raises(PocketError, match='16 KiB'):
        analyze(seal(record), tmp_path)


def test_null_voice_keeps_existing_material_contract_rejection(tmp_path):
    a = note('a')
    a['voice_id'] = None
    with pytest.raises(PocketError, match='voice_id'):
        analyze(material([a]), tmp_path, ['voice_leading'])
