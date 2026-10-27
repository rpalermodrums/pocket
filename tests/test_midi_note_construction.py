# SPDX-License-Identifier: AGPL-3.0-only
"""File-only note construction checks using externally authored material and wire decoding."""
from __future__ import annotations

import copy
import json
from fractions import Fraction

import pytest
from test_midi_qa import decode_wire, expressed_material, import_wire, literal_material, seal_literal

from pocket_music.artifact_store import read_bytes, read_record
from pocket_music.errors import PocketError
from pocket_music.material import material_import, material_query, qn, rational
from pocket_music.midi_edit import midi_transform
from pocket_music.midi_io import midi_export


def literal(**updates):
    return {'onset_qn': 2, 'duration_qn': {'n': 1, 'd': 3},
            'pitch': {'midi_note': 71, 'cents_offset': 0, 'tuning_ref': 'tuning:12tet-a440'},
            'velocity': {'value': 91, 'domain': 'midi1_7bit'},
            'release_velocity': {'value': 29, 'domain': 'midi1_7bit'},
            'channel': 1, 'mute': False, 'voice_id': 'voice:external', 'role_ref': None, **updates}


def add(*notes, **updates):
    return {'op': 'add', 'clip_id': 'clip:external', 'notes': list(notes or [literal()]),
            'controller_timeline': 'preserve_existing', 'time_space': 'clip_qn', **updates}


def split(*offsets, **updates):
    return {'op': 'split', 'offsets_qn': list(offsets or [{'n': 1, 'd': 8}]),
            'controller_timeline': 'preserve_existing', 'time_space': 'note_relative_qn',
            'articulation': 'retrigger', **updates}


def merge(**updates):
    return {'op': 'merge', 'controller_timeline': 'preserve_existing', 'time_space': 'clip_qn',
            'articulation': 'remove_retriggers', **updates}


def apply(record, tmp_path, operations, *, ids=None, request='edit', **kwargs):
    store = str(tmp_path / 'store')
    selection = material_query(record, store, selection={'note_ids': ids} if ids is not None else None)['selection']
    result = midi_transform(record, selection, operations, store, request, **kwargs)
    return result, read_record(result['material'], store)


def touching():
    record = literal_material()
    record['notes'] = record['notes'][:2]
    record['notes'][1] = {**copy.deepcopy(record['notes'][0]), 'id': 'note:1',
                          'onset': {'space': 'clip_qn', 'n': 1, 'd': 4}}
    record['clips'][0]['note_ids'] = ['note:0', 'note:1']
    return seal_literal(record)


def test_literal_add_empty_selection_and_fresh_selection_required(tmp_path):
    record = literal_material()
    result, child = apply(record, tmp_path, [add(), {'op': 'velocity', 'value': 1}], ids=[])
    assert child['notes'][:3] == record['notes']
    new = child['notes'][3]
    assert new['onset'] == {'space': 'clip_qn', 'n': 2, 'd': 1}
    assert new['duration_qn'] == {'n': 1, 'd': 3}
    assert new['velocity']['value'] == 91 and new['release_velocity']['value'] == 29
    assert new['derived_from'] == [] and new['source_binding'] is None and new['expression_refs'] == []
    assert result['change_summary']['total_inserted'] == 1
    for key in ('events', 'curves', 'sources', 'tempo_map_ref', 'meter_map_ref'):
        assert child[key] == record[key]


def test_add_identity_normalizes_rational_times_and_request_path(tmp_path):
    record = literal_material()
    first, left = apply(record, tmp_path, [add()], request='one', ids=[])
    _, right = apply(record, tmp_path, [add(literal(onset_qn=qn(2)))], request='two', ids=[])
    assert left['notes'][-1]['id'] == right['notes'][-1]['id']
    replay, _ = apply(record, tmp_path, [add()], request='one', ids=[])
    assert replay == first
    with pytest.raises(PocketError, match='idempotency'):
        apply(record, tmp_path, [add(literal(onset_qn=3))], request='one', ids=[])


def test_add_obeys_locks_on_existing_notes(tmp_path):
    record = literal_material()
    _, child = apply(record, tmp_path, [add()], locks={'selected_fields': ['onset', 'pitch', 'velocity']})
    assert child['notes'][:3] == record['notes']


def test_split_exact_rationals_then_merge_new_selection(tmp_path):
    record = literal_material()
    record['notes'][1]['duration_qn'] = {'n': 5, 'd': 6}
    record = seal_literal(record)
    result, child = apply(record, tmp_path, [split({'n': 1, 'd': 6}, {'n': 1, 'd': 2}),
                                            {'op': 'velocity', 'value': 1}], ids=['note:1'])
    pieces = [note for note in child['notes'] if note['derived_from'] == ['note:1']]
    assert [rational(note['onset']) for note in pieces] == [Fraction(1, 3), Fraction(1, 2), Fraction(5, 6)]
    assert [rational(note['duration_qn']) for note in pieces] == [Fraction(1, 6), Fraction(1, 3), Fraction(1, 3)]
    assert [note['velocity']['value'] for note in pieces] == [80, 80, 80]
    assert len({note['id'] for note in pieces}) == 3
    _, merged = apply(result['material'], tmp_path, [merge()], ids=[note['id'] for note in pieces], request='merge')
    combined = merged['notes'][-1]
    assert combined['onset'] == record['notes'][1]['onset']
    assert combined['duration_qn'] == record['notes'][1]['duration_qn']
    assert combined['derived_from'] == [note['id'] for note in pieces]
    assert combined['id'] not in {note['id'] for note in pieces} | {'note:1'}
    assert merged['notes'][:2] == [record['notes'][0], record['notes'][2]]


def test_imported_split_keeps_source_and_exports_explicit_retriggers(tmp_path):
    handle = import_wire(tmp_path)
    store = str(tmp_path / 'store')
    record = read_record(handle, store)
    original = read_bytes(record['sources'][0]['raw'], store)
    result, child = apply(handle, tmp_path, [split({'n': 1, 'd': 2})], ids=[record['notes'][0]['id']])
    assert child['events'] == record['events'] and child['sources'] == record['sources']
    assert set(child['coverage']['source_only_note_event_ids']) == {
        record['notes'][0]['source_binding']['on_event_id'], record['notes'][0]['source_binding']['off_event_id']}
    assert read_bytes(record['sources'][0]['raw'], store) == original
    exported = midi_export(result['material'], store, str(tmp_path / 'split.mid'), 'export', ppq=480)
    wire = decode_wire(read_bytes(exported['midi'], store))[2]
    note_events = [(tick, list(payload)) for tick, payload in wire[1] if payload[0] >> 4 in (8, 9)]
    assert note_events == [(0, [144, 60, 80]), (Fraction(1, 2), [128, 60, 37]),
                           (Fraction(1, 2), [144, 60, 80]), (1, [128, 60, 37]),
                           (1, [144, 64, 70]), (2, [144, 64, 0])]
    reparsed = material_import({'kind': 'smf', 'path': str(tmp_path / 'split.mid'),
                               'expected_sha256': exported['midi']['sha256']}, store, 'reimport')
    notes = read_record(reparsed['material'], store)['notes']
    assert len(notes) == 3
    assert sorted(rational(note['duration_qn']) for note in notes) == [Fraction(1, 2), Fraction(1, 2), 1]


@pytest.mark.parametrize('operation', [split(), merge()])
@pytest.mark.parametrize('field', ['onset', 'duration_qn', 'pitch', 'velocity', 'release_velocity',
                                  'channel', 'mute', 'expression_refs', 'voice_id', 'role_ref',
                                  'source_binding', 'derived_from'])
def test_replacement_refuses_every_selected_field_lock(tmp_path, operation, field):
    with pytest.raises(PocketError, match='locked'):
        apply(touching(), tmp_path, [operation], locks={'selected_fields': [field]})


@pytest.mark.parametrize('value', [True, 0.5, {'n': 2, 'd': 4}, {'n': 1, 'd': 2, 'space': 'seconds'},
                                  {'n': 1, 'd': 0}, {'n': 1, 'd': True}])
def test_add_and_split_strict_rationals(tmp_path, value):
    with pytest.raises(PocketError):
        apply(literal_material(), tmp_path, [add(literal(onset_qn=value))], ids=[])
    with pytest.raises(PocketError):
        apply(literal_material(), tmp_path, [split(value)], request='split')


@pytest.mark.parametrize('change', [
    {'onset_qn': -1}, {'duration_qn': 0}, {'duration_qn': 3}, {'channel': True}, {'channel': 17},
    {'mute': 1}, {'voice_id': ''}, {'role_ref': {}}, {'id': 'caller'},
    {'pitch': {'midi_note': 60, 'cents_offset': float('inf'), 'tuning_ref': 'x'}},
    {'pitch': {'midi_note': True, 'cents_offset': 0, 'tuning_ref': 'x'}},
    {'pitch': {'midi_note': 60, 'cents_offset': 0, 'tuning_ref': ''}},
    {'velocity': {'value': 0, 'domain': 'midi1_7bit'}},
    {'release_velocity': {'value': 1, 'domain': 'other'}},
    {'release_velocity': {'value': 1, 'domain': 'midi1_7bit', 'hidden': 0}},
])
def test_literal_invalid_fields_refuse(tmp_path, change):
    with pytest.raises(PocketError):
        apply(literal_material(), tmp_path, [add(literal(**change))], ids=[])


@pytest.mark.parametrize('operation', [add(clip_id='missing'), add(clip_id=[]), add(time_space='seconds'),
    add(notes=[]), add(controller_timeline='copy'), split(0), split({'n': 1, 'd': 4}),
    split({'n': 1, 'd': 8}, {'n': 1, 'd': 8}), split(articulation='tie'),
    split(time_space='clip_qn'), merge(articulation='legato'), merge(time_space='seconds')])
def test_invalid_operation_policies_refuse(tmp_path, operation):
    with pytest.raises(PocketError):
        apply(touching(), tmp_path, [operation])


@pytest.mark.parametrize('operation', [split(), merge()])
def test_empty_selection_and_expression_refused(tmp_path, operation):
    with pytest.raises(PocketError, match='surviving'):
        apply(literal_material(), tmp_path, [operation], ids=[])
    with pytest.raises(PocketError, match='expression'):
        apply(expressed_material(), tmp_path, [operation], request='expression')


@pytest.mark.parametrize('field,value', [('velocity', {'value': 99, 'domain': 'midi1_7bit'}),
    ('release_velocity', {'value': 0, 'domain': 'midi1_7bit'}), ('voice_id', 'other'),
    ('role_ref', 'other'), ('channel', 2), ('mute', True),
    ('pitch', {'midi_note': 61, 'cents_offset': 0, 'tuning_ref': 'tuning:12tet-a440'})])
def test_merge_mixed_attributes_refuse(tmp_path, field, value):
    record = touching()
    record['notes'][1][field] = value
    with pytest.raises(PocketError, match='identical'):
        apply(seal_literal(record), tmp_path, [merge()])


def test_merge_gap_overlap_and_cross_clip_refuse(tmp_path):
    for index, onset in enumerate([0, Fraction(1, 2)]):
        record = touching()
        record['notes'][1]['onset'] = {'space': 'clip_qn', **qn(onset)}
        with pytest.raises(PocketError, match='contiguous'):
            apply(seal_literal(record), tmp_path, [merge()], request=f'time-{index}')
    record = touching()
    clip = copy.deepcopy(record['clips'][0])
    clip.update(id='clip:other', note_ids=['note:1'])
    record['clips'][0]['note_ids'] = ['note:0']
    record['clips'].append(clip)
    with pytest.raises(PocketError, match='one clip'):
        apply(seal_literal(record), tmp_path, [merge()], request='clips')


def test_new_overlap_and_late_failure_publish_no_child(tmp_path):
    record = literal_material()
    overlapping = literal(onset_qn=0, pitch=copy.deepcopy(record['notes'][0]['pitch']))
    with pytest.raises(PocketError, match='overlap'):
        apply(record, tmp_path, [add(overlapping)], ids=[])
    with pytest.raises(PocketError):
        apply(record, tmp_path, [add(), split(0)], request='late')
    assert not list((tmp_path / 'store' / 'artifacts').rglob('*'))


def test_source_opaque_refusal_and_artifact_response_bound(tmp_path):
    record = touching()
    record['notes'][0]['source_binding'] = {'kind': 'native', 'unknown': 'retain'}
    record = seal_literal(record)
    with pytest.raises(PocketError, match='opaque/native'):
        apply(record, tmp_path, [split()])
    many = [literal(onset_qn=qn(Fraction(index, 100)), duration_qn=qn(Fraction(1, 100)))
            for index in range(100)]
    result, child = apply(literal_material(), tmp_path, [add(*many)], request='many', ids=[])
    assert len(child['notes']) == 103
    assert len((json.dumps(result, indent=2, ensure_ascii=True) + '\n').encode()) <= 16384
    edit = read_record(result['edit'], tmp_path / 'store')
    assert len(edit['semantic_diff']['inserted']) == 100
    assert result['change_summary']['omitted_inserted'] == 84


def test_construction_note_budgets_and_identity_collisions(tmp_path, monkeypatch):
    import pocket_music.midi_edit as editor
    monkeypatch.setattr(editor, 'MAX_NOTES', 3)
    with pytest.raises(PocketError, match='bound'):
        apply(literal_material(), tmp_path, [add()], ids=[])
    with pytest.raises(PocketError, match='bound'):
        apply(literal_material(), tmp_path, [split()], request='split')
    monkeypatch.setattr(editor, 'MAX_NOTES', 100000)
    monkeypatch.setattr(editor, 'identifier', lambda *args: 'note:0')
    with pytest.raises(PocketError, match='collides'):
        apply(literal_material(), tmp_path, [add()], ids=[], request='collision')


def test_merge_imported_sources_preserves_ordered_controls_and_release(tmp_path):
    import hashlib

    from test_midi_qa import smf
    payload = smf([(b'\x00\xb0\x40\x7f\x00\x90\x3c\x50\x78\x80\x3c\x25'
                   b'\x00\xb0\x0b\x40\x00\xb0\x0b\x60\x00\x90\x3c\x50'
                   b'\x78\x80\x3c\x25\x00\xb0\x40\x00\x00\xff\x2f\x00')], format=0)
    path = tmp_path / 'external.mid'
    path.write_bytes(payload)
    store = str(tmp_path / 'store')
    imported = material_import({'kind': 'smf', 'path': str(path),
                                'expected_sha256': hashlib.sha256(payload).hexdigest()}, store, 'import')
    parent = read_record(imported['material'], store)
    result, child = apply(imported['material'], tmp_path, [merge()])
    assert child['events'] == parent['events']
    assert len(child['coverage']['source_only_note_event_ids']) == 4
    assert child['notes'][0]['duration_qn'] == {'n': 1, 'd': 2}
    exported = midi_export(result['material'], store, str(tmp_path / 'merged.mid'), 'export', ppq=480)
    wire = decode_wire(read_bytes(exported['midi'], store))[2]
    assert [(time, list(message)) for time, message in wire[0] if message[0] >> 4 in (8, 9)] == [
        (0, [144, 60, 80]), (Fraction(1, 2), [128, 60, 37])]
    assert [(time, list(message)) for time, message in wire[0] if message[0] >> 4 == 11] == [
        (0, [176, 64, 127]), (Fraction(1, 4), [176, 11, 64]),
        (Fraction(1, 4), [176, 11, 96]), (Fraction(1, 2), [176, 64, 0])]
    assert path.read_bytes() == payload


def test_split_refuses_corrupted_transitive_source(tmp_path):
    handle = import_wire(tmp_path)
    store = tmp_path / 'store'
    parent = read_record(handle, store)
    (store / parent['sources'][0]['raw']['artifact_uri']).write_bytes(b'corrupted')
    with pytest.raises(PocketError):
        apply(handle, tmp_path, [split()])


def test_failed_construction_request_is_not_replayed(tmp_path):
    record = literal_material()
    with pytest.raises(PocketError):
        apply(record, tmp_path, [add(), split(0)])
    files = {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    with pytest.raises(PocketError):
        apply(record, tmp_path, [add(), split(0)])
    assert {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()} == files


def test_pure_construction_does_not_import_optional_codec_or_native(tmp_path, monkeypatch):
    import builtins
    original = builtins.__import__

    def restricted(name, *args, **kwargs):
        assert not name.startswith(('mido', 'pocket_music.native', 'torch'))
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', restricted)
    result, child = apply(literal_material(), tmp_path, [add()], ids=[])
    assert result['status'] == 'ok' and len(child['notes']) == 4
