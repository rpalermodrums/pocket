"""Per-note attributed harmonic alternatives remain explicit symbolic edits."""
from __future__ import annotations

import copy
import json

import pytest
from test_midi_note_construction import apply
from test_midi_qa import decode_wire, expressed_material, import_wire, literal_material, seal_literal

from pocket_music.artifact_store import read_bytes, read_record
from pocket_music.errors import PocketError
from pocket_music.material import material_query
from pocket_music.midi_edit import midi_transform
from pocket_music.midi_io import midi_export


def pitch(value, **updates):
    return {'midi_note': value, 'cents_offset': 0, 'tuning_ref': 'tuning:12tet-a440', **updates}


def revoice(*pairs, **updates):
    return {'op': 'revoice',
        'destinations': [{'note_id': key, 'pitch': value} for key, value in (pairs or [('note:1', pitch(65))])],
        'hypothesis': {'label': 'Supplied alternative', 'actor': 'Fixture author', 'actor_kind': 'agent',
                       'statement': 'Move only explicitly chosen inner notes.', 'uncertainty': ['Sound unverified.']},
        'controller_timeline': 'preserve_existing', 'pitch_expression': 'preserve_relative', **updates}


def test_nonuniform_revoice_preserves_declared_voices_rhythm_and_outside_notes(tmp_path):
    record = literal_material()
    result, child = apply(record, tmp_path, [revoice(('note:0', pitch(58)), ('note:1', pitch(65)))],
        ids=['note:0', 'note:1'], locks={'selected_fields': ['onset', 'duration_qn', 'velocity',
            'release_velocity', 'channel', 'voice_id', 'role_ref', 'source_binding', 'expression_refs']})
    assert [note['pitch']['midi_note'] for note in child['notes']] == [58, 65, 67]
    assert child['notes'][2] == record['notes'][2]
    for before, after in zip(record['notes'], child['notes'], strict=True):
        assert {key: value for key, value in before.items() if key != 'pitch'} == {
            key: value for key, value in after.items() if key != 'pitch'}
    proof = read_record(result['edit'], tmp_path / 'store')['operation_reports'][0]
    assert proof['changed'] == 2 and proof['hypothesis_basis'] == 'supplied_attribution_not_inferred'
    assert proof['listening'] == 'not_performed' and proof['audible_contour'] == 'not_inferred'


def test_complete_pitch_record_replaces_only_explicit_pitch_and_no_conversion(tmp_path):
    record = literal_material()
    target = pitch(64, cents_offset=17.5, tuning_ref='tuning:caller-declared-alternative')
    result, child = apply(record, tmp_path, [revoice(('note:1', target))], ids=['note:1'])
    assert child['notes'][1]['pitch'] == target
    proof = read_record(result['edit'], tmp_path / 'store')['operation_reports'][0]
    assert proof['tuning_conversion'] == 'not_performed'


def test_revoice_and_composed_per_note_pitches_equal_in_fields(tmp_path):
    record = literal_material()
    result, child = apply(record, tmp_path, [revoice(('note:0', pitch(58)), ('note:1', pitch(65)))],
                          ids=['note:0', 'note:1'])
    first, _ = apply(record, tmp_path, [{'op': 'repitch', 'pitch': 58}], ids=['note:0'], request='one')
    _, second = apply(first['material'], tmp_path, [{'op': 'repitch', 'pitch': 65}], ids=['note:1'], request='two')
    assert second['notes'] == child['notes']
    assert result['invariant_report']['outside_selection'] == 'exact'


def test_source_bound_revoice_retains_wire_and_independent_export(tmp_path):
    material = import_wire(tmp_path)
    store = str(tmp_path / 'store')
    parent = read_record(material, store)
    original = read_bytes(parent['sources'][0]['raw'], store)
    operation = revoice(*[(note['id'], pitch(value)) for note, value in zip(parent['notes'], [59, 67], strict=True)])
    result, child = apply(material, tmp_path, [operation])
    assert child['events'] == parent['events'] and child['sources'] == parent['sources']
    assert [note['source_binding'] for note in child['notes']] == [note['source_binding'] for note in parent['notes']]
    exported = midi_export(result['material'], store, str(tmp_path / 'revoice.mid'), 'export', ppq=480)
    wire = decode_wire(read_bytes(exported['midi'], store))[2]
    assert [(time, list(data)) for time, data in wire[1] if data[0] >> 4 in (8, 9)] == [
        (0, [144, 59, 80]), (1, [128, 59, 37]), (1, [144, 67, 70]), (2, [144, 67, 0])]
    assert read_bytes(parent['sources'][0]['raw'], store) == original


def test_noop_pitch_lock_and_changed_pitch_lock(tmp_path):
    record = literal_material()
    _, child = apply(record, tmp_path, [revoice(('note:1', pitch(64)))], ids=['note:1'],
                      locks={'selected_fields': ['pitch']})
    assert child['notes'] == record['notes']
    with pytest.raises(PocketError, match='lock'):
        apply(record, tmp_path, [revoice()], ids=['note:1'], request='change', locks={'selected_fields': ['pitch']})


def test_unowned_fixed_relative_pressure_shape_stays_exact(tmp_path):
    record = expressed_material()
    op = revoice(('note:0', pitch(62)))
    with pytest.raises(PocketError, match='preserve_relative'):
        apply(record, tmp_path, [op], ids=['note:0'])
    _, child = apply(record, tmp_path, [op], ids=['note:0'], request='relative', expression_policy='preserve_relative')
    assert child['curves'] == record['curves']
    assert child['notes'][0]['expression_refs'] == record['notes'][0]['expression_refs']


@pytest.mark.parametrize('unit', ['cents', 'semitones'])
def test_additive_pitch_curve_preserves_explicit_relative_values(tmp_path, unit):
    record = expressed_material()
    record['curves'][0]['target'].update(kind='per_note_pitch', unit=unit, value_mode='additive')
    record = seal_literal(record)
    _, child = apply(record, tmp_path, [revoice(('note:0', pitch(62)))], ids=['note:0'],
                     expression_policy='preserve_relative')
    assert child['curves'] == record['curves']


@pytest.mark.parametrize('target_updates', [{'value_mode': 'absolute', 'unit': 'cents'},
    {'value_mode': 'multiplicative', 'unit': 'semitones'}, {'value_mode': 'additive', 'unit': 'normalized'},
    {'value_mode': 'additive', 'unit': 'hz'}, {'ownership': 'remote'}, {'resize_policy': 'preserve_ms'}])
def test_unqualified_pitch_expression_refuses(tmp_path, target_updates):
    record = expressed_material()
    record['curves'][0]['target'].update(kind='per_note_pitch', unit='cents', value_mode='additive')
    record['curves'][0]['target'].update(target_updates)
    with pytest.raises(PocketError):
        apply(seal_literal(record), tmp_path, [revoice(('note:0', pitch(62)))], ids=['note:0'],
              expression_policy='preserve_relative')


@pytest.mark.parametrize('destinations', [[], [{'note_id': 'note:0', 'pitch': pitch(61)}],
    [{'note_id': 'missing', 'pitch': pitch(61)}],
    [{'note_id': 'note:1', 'pitch': pitch(61)}, {'note_id': 'note:1', 'pitch': pitch(62)}],
    [{'note_id': [], 'pitch': pitch(61)}], [{'note_id': 'note:1', 'pitch': pitch(61), 'hidden': 0}],
    [{'note_id': 'note:1', 'pitch': {'midi_note': 60}}]])
def test_revoice_exact_mapping_coverage_and_shape(tmp_path, destinations):
    with pytest.raises(PocketError):
        apply(literal_material(), tmp_path, [revoice(destinations=destinations)], ids=['note:1'])


@pytest.mark.parametrize('field,value', [('actor', ''), ('actor', ' '), ('actor', 'a' * 257),
    ('label', 'a' * 257), ('statement', ''), ('statement', 'a' * 4097), ('actor_kind', 'model'),
    ('uncertainty', 'unknown'), ('uncertainty', [''] ), ('uncertainty', ['x' * 1025]),
    ('uncertainty', ['x'] * 33), ('unexpected', True)])
def test_hypothesis_strict_bounded_attribution(tmp_path, field, value):
    operation = revoice()
    operation['hypothesis'][field] = value
    with pytest.raises(PocketError):
        apply(literal_material(), tmp_path, [operation], ids=['note:1'])


def test_revoice_new_overlap_and_opaque_source_refused(tmp_path):
    record = literal_material()
    record['notes'][0]['duration_qn'] = {'n': 1, 'd': 1}
    with pytest.raises(PocketError, match='overlap'):
        apply(seal_literal(record), tmp_path, [revoice(('note:1', pitch(60)))], ids=['note:1'])
    record = literal_material()
    record['notes'][1]['source_binding'] = {'kind': 'native', 'extra': 'unknown'}
    with pytest.raises(PocketError, match='opaque/native'):
        apply(seal_literal(record), tmp_path, [revoice()], ids=['note:1'], request='native')


def test_revoice_fixed_selection_after_delete_and_request_replay(tmp_path):
    record = literal_material()
    with pytest.raises(PocketError, match='cover'):
        apply(record, tmp_path, [{'op': 'delete', 'controller_timeline': 'preserve_existing'}, revoice()], ids=['note:1'])
    result, _ = apply(record, tmp_path, [revoice()], ids=['note:1'], request='ok')
    replay, _ = apply(record, tmp_path, [revoice()], ids=['note:1'], request='ok')
    assert replay == result
    changed = revoice()
    changed['hypothesis']['statement'] = 'Another interpretation'
    with pytest.raises(PocketError, match='idempotency'):
        apply(record, tmp_path, [changed], ids=['note:1'], request='ok')


def test_revoice_stale_selection_and_large_proof(tmp_path):
    record = literal_material()
    store = str(tmp_path / 'store')
    selection = material_query(record, store, selection={'note_ids': ['note:1']})['selection']
    changed = copy.deepcopy(record)
    changed['notes'][1]['velocity']['value'] = 77
    with pytest.raises(PocketError, match='Stale'):
        midi_transform(seal_literal(changed), selection, [revoice()], store, 'stale')
    operation = revoice()
    operation['hypothesis']['uncertainty'] = ['x' * 1024] * 32
    result, _ = apply(record, tmp_path, [operation], ids=['note:1'], request='large')
    assert len((json.dumps(result, indent=2) + '\n').encode()) <= 16384
    proof = read_record(result['edit'], store)
    assert proof['operation_reports'][0]['hypothesis']['uncertainty'] == ['x' * 1024] * 32
