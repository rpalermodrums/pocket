"""Independent hand-expected F3 relationships; no native or listening evidence."""
from __future__ import annotations

import builtins
import copy
import json
from fractions import Fraction

import pytest
from test_midi_qa import literal_material, seal_literal

from pocket_music.errors import PocketError
from pocket_music.material import material_query
from pocket_music.midi_analysis import midi_analyze


def q(value):
    value = Fraction(value)
    return {'n': value.numerator, 'd': value.denominator}


def note(key, onset, duration, pitch=60, *, voice='v', role=None, cents=0,
         tuning='tuning:12tet-a440', velocity=80, release=37):
    value = copy.deepcopy(literal_material()['notes'][0])
    value.update(id=key, voice_id=voice, role_ref=role,
                 onset={'space': 'clip_qn', **q(onset)}, duration_qn=q(duration))
    value['pitch'] = {'midi_note': pitch, 'cents_offset': cents, 'tuning_ref': tuning}
    value['velocity']['value'] = velocity
    value['release_velocity']['value'] = release
    return value


def material(notes, *, length=8):
    value = literal_material()
    value['notes'] = copy.deepcopy(notes)
    value['clips'][0]['note_ids'] = [n['id'] for n in notes]
    value['clips'][0]['length_qn'] = q(length)
    return seal_literal(value)


def analysis(record, tmp_path, names=('voice_leading',), selection=None):
    return midi_analyze(record, str(tmp_path), selection=selection, analyses=list(names))


def transitions(result):
    return result['measurements']['voice_leading']['clips'][0]['voices'][0]['transitions']


def test_qa_hand_expected_voice_intervals_and_rest_gaps(tmp_path):
    record = material([note('a', 0, Fraction(1, 2)), note('b', 1, Fraction(1, 2), 64), note('c', 2, 1, 62)])
    before = copy.deepcopy(record)
    result = analysis(record, tmp_path)
    assert [row['interval'] for row in transitions(result)] == [
        {'signed_midi_interval': 4, 'cents_offset_delta': q(0), 'signed_total_cents': q(400),
         'contour': 'up', 'gap_qn': q(Fraction(1, 2))},
        {'signed_midi_interval': -2, 'cents_offset_delta': q(0), 'signed_total_cents': q(-200),
         'contour': 'down', 'gap_qn': q(Fraction(1, 2))}]
    assert result['measurements']['voice_leading']['gap_policy'] == 'next_onset_minus_previous_gate_end'
    assert record == before
    assert result['listening'] == 'not_performed'
    assert not tmp_path.exists() or not list(tmp_path.iterdir())


@pytest.mark.parametrize(('left', 'right', 'cents', 'contour'), [
    ((60, 25), (64, -25), Fraction(350), 'up'),
    ((60, 0.1), (60, 0.2), Fraction(1, 10), 'up'),
    ((60, 100), (61, 0), Fraction(0), 'same'),
    ((61, 0), (60, 99.9), Fraction(-1, 10), 'down'),
])
def test_qa_known_tuning_exact_declared_cents(tmp_path, left, right, cents, contour):
    result = analysis(material([note('a', 0, 1, left[0], cents=left[1]),
                                note('b', 1, 1, right[0], cents=right[1])]), tmp_path)
    interval = transitions(result)[0]['interval']
    assert interval['signed_total_cents'] == q(cents)
    assert interval['contour'] == contour
    assert interval['gap_qn'] == q(0)


@pytest.mark.parametrize('tuning', ['tuning:custom-just', '', None, 5])
def test_qa_unresolved_tuning_never_assumes_hundred_cent_keys(tmp_path, tuning):
    result = analysis(material([note('a', 0, 1, tuning=tuning), note('b', 1, 1, 64, tuning=tuning)]), tmp_path)
    row = transitions(result)[0]
    assert row['status'] == 'abstained' and row['interval'] is None
    assert 'unresolved_tuning' in row['abstentions']
    assert row['from_notes'][0]['note_id'] == 'a'


def test_qa_equal_time_polyphony_preserves_multiplicity_and_order_independence(tmp_path):
    notes = [note('b', 0, Fraction(1, 2), 64), note('a', 0, Fraction(1, 2), 60), note('c', 1, 1, 67)]
    first = transitions(analysis(material(notes), tmp_path))
    second = transitions(analysis(material(list(reversed(notes))), tmp_path))
    assert first == second and len(first) == 1
    assert first[0]['interval'] is None and 'simultaneous_onset_group' in first[0]['abstentions']
    assert [n['note_id'] for n in first[0]['from_notes']] == ['a', 'b']


@pytest.mark.parametrize('same_pitch', [False, True])
def test_qa_equal_time_duplicate_never_deduplicates(tmp_path, same_pitch):
    record = material([note('a', 0, 1), note('b', 0, 1, 60 if same_pitch else 64), note('c', 1, 1)])
    assert len(transitions(analysis(record, tmp_path))[0]['from_notes']) == 2


def test_qa_long_same_voice_overlap_but_other_voice_does_not_block(tmp_path):
    same = material([note('long', 0, 4), note('a', 1, Fraction(1, 4), 64), note('b', 2, Fraction(1, 4), 67)])
    rows = transitions(analysis(same, tmp_path))
    assert all(row['interval'] is None and 'same_voice_gate_overlap' in row['abstentions'] for row in rows)
    separate = copy.deepcopy(same)
    separate['notes'][0]['voice_id'] = 'other'
    result = analysis(seal_literal(separate), tmp_path)
    v = next(v for v in result['measurements']['voice_leading']['clips'][0]['voices'] if v['voice_id'] == 'v')
    assert v['transitions'][0]['interval']['signed_total_cents'] == q(300)


def test_qa_role_pair_multiplicity_differs_from_shared_time(tmp_path):
    notes = [note('b1', 0, 3, role='bass'), note('b2', 2, 2, role='bass'),
             note('u1', 1, 2, role='upper'), note('u2', 2, 3, role='upper')]
    result = analysis(material(notes), tmp_path, ('role_overlap',))
    row = result['measurements']['role_overlap']['clips'][0]['role_pairs'][0]
    assert row['roles'] == ['bass', 'upper']
    assert row['gate_overlap_pair_count'] == 4
    assert row['pair_duration_sum_qn'] == q(6)
    assert row['shared_active_duration_qn'] == q(3)
    assert row['onset_coincidence_pair_count'] == 1
    assert row['onset_evidence'] == [{'note_ids': ['b2', 'u2'], 'onset_qn': q(2)}]
    assert [r['span_qn'] for r in row['overlap_evidence']] == [[q(1), q(3)], [q(2), q(3)], [q(2), q(3)], [q(2), q(4)]]


def test_qa_touching_roles_unassigned_and_muted_scope(tmp_path):
    notes = [note('a', 0, 1, role='a'), note('b', 1, 1, role='b'),
             note('none', 0, 1), note('empty', 0, 1, role='')]
    notes[0]['mute'] = True
    result = analysis(material(notes), tmp_path, ('role_overlap',))['measurements']['role_overlap']
    row = result['clips'][0]['role_pairs'][0]
    assert result['mute_policy'] == 'included_as_symbolic_records'
    assert result['clips'][0]['unassigned_note_count'] == 2
    assert row['gate_overlap_pair_count'] == row['onset_coincidence_pair_count'] == 0
    assert row['shared_active_duration_qn'] == q(0)


def test_qa_relationship_clocks_never_merge_clips(tmp_path):
    record = material([note('a', 0, 1, role='a'), note('b', 0, 1, role='b')])
    first = record['clips'][0]
    first['note_ids'] = ['a']
    second = {**copy.deepcopy(first), 'id': 'clip:other', 'note_ids': ['b'],
              'origin': {'space': 'arrangement_qn', **q(100)}}
    record['clips'].append(second)
    result = analysis(seal_literal(record), tmp_path, ('voice_leading', 'role_overlap'))
    for name in ('voice_leading', 'role_overlap'):
        assert result['measurements'][name]['comparison_scope'] == 'within_each_clip'
    assert all(not c['role_pairs'] for c in result['measurements']['role_overlap']['clips'])
    assert all(not v['transitions'] for c in result['measurements']['voice_leading']['clips'] for v in c['voices'])


def test_qa_full_receipt_and_evidence_bounds_at_512_selected(tmp_path):
    notes = [note(f'a{i}', 0, 1, role='a') for i in range(256)] + [note(f'b{i}', 0, 1, role='b') for i in range(257)]
    record = material(notes)
    with pytest.raises(PocketError, match='512'):
        analysis(record, tmp_path, ('role_overlap',))
    result = analysis(record, tmp_path, ('role_overlap',), {'note_ids': [n['id'] for n in notes[:512]]})
    row = result['measurements']['role_overlap']['clips'][0]['role_pairs'][0]
    assert row['gate_overlap_pair_count'] == row['onset_coincidence_pair_count'] == 65536
    assert row['pair_duration_sum_qn'] == q(65536) and row['shared_active_duration_qn'] == q(1)
    assert len(row['overlap_evidence']) == len(row['onset_evidence']) == 8
    assert row['omitted_overlap_pairs'] == row['omitted_onset_pairs'] == 65528
    assert len((json.dumps(result, indent=2, ensure_ascii=True) + '\n').encode()) <= 16384


def test_qa_escaped_identifiers_cannot_bypass_receipt_budget(tmp_path):
    record = material([note('音' * 5000, 0, 1), note('b', 1, 1)])
    with pytest.raises(PocketError, match='16 KiB'):
        analysis(record, tmp_path)


def test_qa_stale_selection_and_offline_independence(tmp_path, monkeypatch):
    record = material([note('a', 0, 1), note('b', 1, 1, 64)])
    selection = material_query(record, str(tmp_path))['selection']
    importer = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert name.split('.')[0] not in {'mido', 'mcp', 'torch', 'requests'}, name
        return importer(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', guarded)
    assert transitions(analysis(record, tmp_path, selection=selection))[0]['interval']['signed_midi_interval'] == 4
    stale = {**selection, 'selection_sha256': '0' * 64}
    with pytest.raises(PocketError, match='Stale'):
        analysis(record, tmp_path, selection=stale)


def test_qa_relationship_selection_does_not_pull_in_outside_sustained_voice(tmp_path):
    record = material([note('outside', 0, 4), note('a', 1, Fraction(1, 4), 64), note('b', 2, Fraction(1, 4), 67)])
    selected = material_query(record, str(tmp_path), selection={'note_ids': ['a', 'b']})['selection']
    result = analysis(record, tmp_path, selection=selected)['measurements']['voice_leading']
    assert result['note_scope'] == 'selected_notes_only'
    assert result['selected_note_count'] == 2 and result['selection_sha256'] == selected['selection_sha256']
    assert result['clips'][0]['voices'][0]['transitions'][0]['interval']['signed_total_cents'] == q(300)
