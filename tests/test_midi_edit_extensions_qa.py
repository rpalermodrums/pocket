"""Independent F5 expectations authored before implementation; artifact-only evidence."""
from __future__ import annotations

import copy
import hashlib
import shutil
from fractions import Fraction
from pathlib import Path

import pytest
from test_midi_qa import decode_wire, expressed_material, import_wire, seal_literal, smf
from test_midi_relationships_qa import material, note, q

from pocket_music.artifact_store import read_bytes, read_record
from pocket_music.errors import PocketError
from pocket_music.material import material_query
from pocket_music.midi_edit import midi_transform
from pocket_music.midi_io import midi_export


def grid(**changes):
    return {'op': 'grid_quantize', 'grid_qn': q(Fraction(1, 2)), 'phase_qn': q(0),
            'strength': q(1), 'threshold_qn': q(Fraction(1, 4)), 'ties': 'earlier',
            'note_off': 'follow_onset', 'controller_timeline': 'preserve_existing',
            'time_space': 'clip_qn', **changes}


def duplicate(delta):
    return {'op': 'duplicate', 'delta_qn': q(delta), 'controller_timeline': 'preserve_existing'}


def apply(record, tmp_path, operations, *, ids=None, request='edit', **options):
    store = str(tmp_path / 'store')
    selection = material_query(record, store, selection={'note_ids': ids} if ids is not None else None)['selection']
    result = midi_transform(record, selection, operations, store, request, **options)
    return result, read_record(result['material'], store)


def wire_fixture():
    track = (b'\x00\xff\x01\x01X\x00\xb0\x01\x0b\x00\x90\x3c\x49'
             b'\x78\x80\x3c\x25\x00\xb0\x40\x7f\x00\x90\x40\x51'
             b'\x78\x90\x40\x00\x00\xb0\x01\x63\x78\xb0\x40\x00\x78\xff\x2f\x00')
    return smf([track], 480, format=0)


@pytest.mark.parametrize(('start', 'changes', 'expected'), [
    (Fraction(1, 4), {}, 0),
    (Fraction(1, 4), {'ties': 'later'}, Fraction(1, 2)),
    (Fraction(1, 8), {'strength': q(Fraction(1, 2)), 'threshold_qn': q(Fraction(1, 8))}, Fraction(1, 16)),
    (Fraction(1, 8), {'threshold_qn': q(Fraction(1, 9))}, Fraction(1, 8)),
    (Fraction(3, 8), {'strength': q(Fraction(1, 2))}, Fraction(7, 16)),
    (Fraction(3, 8), {'phase_qn': q(Fraction(1, 8)), 'strength': q(Fraction(1, 2))}, Fraction(1, 4)),
    (Fraction(3, 8), {'phase_qn': q(Fraction(1, 8)), 'strength': q(Fraction(1, 2)), 'ties': 'later'}, Fraction(1, 2)),
    (Fraction(-1, 4), {}, Fraction(-1, 2)),
    (Fraction(-1, 4), {'ties': 'later'}, 0),
])
def test_qa_grid_hand_expected_rationals_and_negative_pickup(tmp_path, start, changes, expected):
    record = material([note('n', start, Fraction(1, 8))])
    before = copy.deepcopy(record)
    _, child = apply(record, tmp_path, [grid(**changes)])
    wanted = copy.deepcopy(record['notes'][0])
    wanted['onset'] = {'space': 'clip_qn', **q(expected)}
    assert child['notes'] == [wanted]
    assert child['clips'] == record['clips']
    assert record == before


def test_qa_zero_strength_preserves_onset_lock_but_moving_grid_refuses(tmp_path):
    record = material([note('n', Fraction(1, 4), Fraction(1, 8))])
    _, child = apply(record, tmp_path, [grid(strength=q(0))], locks={'selected_fields': ['onset']})
    assert child['notes'] == record['notes']
    with pytest.raises(PocketError, match='lock'):
        apply(record, tmp_path, [grid()], request='moving', locks={'selected_fields': ['onset']})


def test_qa_grid_new_overlap_refused_but_touching_gates_allowed(tmp_path):
    record = material([note('a', Fraction(1, 8), Fraction(1, 8)), note('b', Fraction(1, 4), Fraction(1, 8))])
    with pytest.raises(PocketError, match='overlap'):
        apply(record, tmp_path, [grid()])
    touch = material([note('a', 0, Fraction(1, 2)), note('b', Fraction(3, 8), Fraction(1, 8))])
    _, child = apply(touch, tmp_path, [grid(ties='later')], ids=['b'], request='touch')
    assert child['notes'][1]['onset'] == {'space': 'clip_qn', **q(Fraction(1, 2))}


def test_qa_duplicate_ids_request_independent_and_clones_not_reselected(tmp_path):
    record = material([note('n', 0, Fraction(1, 4), velocity=73)])
    operations = [duplicate(1), duplicate(2), {'op': 'velocity', 'value': 99}]
    first, child = apply(record, tmp_path, operations, locks={'selected_fields': ['pitch', 'duration']})
    second, other = apply(record, tmp_path, operations, request='another')
    assert first['material'] == second['material'] and child == other
    assert len(child['notes']) == 3
    original = next(n for n in child['notes'] if n['id'] == 'n')
    clones = sorted((n for n in child['notes'] if n['id'] != 'n'), key=lambda n: n['onset']['n'])
    assert original['velocity']['value'] == 99
    assert [n['onset'] for n in clones] == [{'space': 'clip_qn', **q(1)}, {'space': 'clip_qn', **q(2)}]
    assert all(n['velocity']['value'] == 73 and n['source_binding'] is None and n['derived_from'] == ['n'] for n in clones)
    assert len({n['id'] for n in child['notes']}) == 3


@pytest.mark.parametrize('delta', [0, -1, 9])
def test_qa_duplicate_refuses_zero_or_outside_clip_without_clamping(tmp_path, delta):
    record = material([note('n', 0, 1)])
    with pytest.raises(PocketError):
        apply(record, tmp_path, [duplicate(delta)])


def test_qa_duplicate_expression_and_opaque_native_binding_refuse(tmp_path):
    expressive = expressed_material()
    with pytest.raises(PocketError):
        apply(expressive, tmp_path, [duplicate(2)], ids=['note:0'])
    record = material([note('n', 0, 1)])
    record['notes'][0]['source_binding'] = {'kind': 'live_clip', 'note_id': 99, 'unknown': {'chance': 0.5}}
    with pytest.raises(PocketError):
        apply(seal_literal(record), tmp_path, [duplicate(2)], request='native')


def test_qa_delete_selected_lock_refuses_without_touching_outside(tmp_path):
    record = material([note('a', 0, 1), note('b', 1, 1, 64)])
    before = copy.deepcopy(record)
    operation = {'op': 'delete', 'controller_timeline': 'preserve_existing'}
    with pytest.raises(PocketError):
        apply(record, tmp_path, [operation], ids=['a'], locks={'selected_fields': ['velocity']})
    _, child = apply(record, tmp_path, [operation], ids=['a'], request='unlocked')
    assert child['notes'] == [record['notes'][1]] and child['clips'][0]['note_ids'] == ['b']
    assert record == before


def test_qa_delete_imported_lifecycle_preserves_original_wire_and_controllers(tmp_path):
    handle = import_wire(tmp_path, wire_fixture())
    store = str(tmp_path / 'store')
    record = read_record(handle, store)
    source_bytes = read_bytes(record['sources'][0]['raw'], store)
    a, b = sorted(record['notes'], key=lambda n: n['pitch']['midi_note'])
    _, child = apply(handle, tmp_path, [{'op': 'delete', 'controller_timeline': 'preserve_existing'}], ids=[a['id']])
    assert child['notes'] == [b] and child['events'] == record['events']
    assert set(child['coverage']['source_only_note_event_ids']) == {a['source_binding']['on_event_id'], a['source_binding']['off_event_id']}
    exported = midi_export(child, store, str(tmp_path / 'deleted.mid'), 'export-delete', format='smf0', ppq=480)
    rows = decode_wire(read_bytes(exported['midi'], store))[2][0]
    assert [(time, data) for time, data in rows if data[0] >> 4 in (8, 9)] == [
        (Fraction(1, 4), b'\x90\x40\x51'), (Fraction(1, 2), b'\x90\x40\x00')]
    assert [(time, data) for time, data in rows if data[0] == 176] == [
        (Fraction(0), b'\xb0\x01\x0b'), (Fraction(1, 4), b'\xb0\x40\x7f'),
        (Fraction(1, 2), b'\xb0\x01\x63'), (Fraction(3, 4), b'\xb0\x40\x00')]
    assert read_bytes(record['sources'][0]['raw'], store) == source_bytes == wire_fixture()


def test_qa_duplicate_imported_binding_detaches_and_export_has_one_extra_lifecycle(tmp_path):
    handle = import_wire(tmp_path, wire_fixture())
    store = str(tmp_path / 'store')
    record = read_record(handle, store)
    selected = next(n for n in record['notes'] if n['pitch']['midi_note'] == 64)
    _, child = apply(handle, tmp_path, [duplicate(Fraction(1, 2))], ids=[selected['id']])
    originals = {n['id']: n for n in record['notes']}
    assert all(n == originals[n['id']] for n in child['notes'] if n['id'] in originals)
    clone = next(n for n in child['notes'] if n['id'] not in originals)
    assert clone['source_binding'] is None and clone['derived_from'] == [selected['id']]
    assert clone['onset'] == {'space': 'clip_qn', **q(Fraction(3, 4))}
    assert child['events'] == record['events']
    exported = midi_export(child, store, str(tmp_path / 'duplicate.mid'), 'export-dup', format='smf0', ppq=480)
    rows = decode_wire(read_bytes(exported['midi'], store))[2][0]
    note_rows = [(time, data) for time, data in rows if data[0] >> 4 in (8, 9)]
    assert len(note_rows) == 6
    assert note_rows[-2:] == [(Fraction(3, 4), b'\x90\x40\x51'), (Fraction(1), b'\x80\x40\x00')]


def test_qa_velocity_lookup_is_explicit_and_release_zero_remains_legal(tmp_path):
    record = material([note('a', 0, 1, velocity=40, release=0), note('b', 1, 1, velocity=80, release=55),
                       note('c', 2, 1, velocity=100, release=18)])
    operation = {'op': 'velocity_map', 'field': 'velocity', 'mapping': [
        {'from_value': 40, 'to_value': 20}, {'from_value': 80, 'to_value': 60}], 'unmapped': 'preserve'}
    _, child = apply(record, tmp_path, [operation])
    assert [n['velocity']['value'] for n in child['notes']] == [20, 60, 100]
    assert [n['release_velocity'] for n in child['notes']] == [n['release_velocity'] for n in record['notes']]
    with pytest.raises(PocketError):
        apply(record, tmp_path, [{**operation, 'unmapped': 'reject'}], request='reject')
    release = {'op': 'velocity_map', 'field': 'release_velocity', 'mapping': [
        {'from_value': 0, 'to_value': 37}, {'from_value': 55, 'to_value': 0}], 'unmapped': 'preserve'}
    _, child = apply(record, tmp_path, [release], request='release')
    assert [n['release_velocity']['value'] for n in child['notes']] == [37, 0, 18]
    assert [n['velocity'] for n in child['notes']] == [n['velocity'] for n in record['notes']]


@pytest.mark.parametrize('operation', [
    {'op': 'delete'}, {'op': 'duplicate', 'delta_qn': 1},
    grid(strength=True), grid(grid_qn=0), grid(threshold_qn=q(Fraction(1, 3))),
    grid(ties='nearest'), grid(note_off='keep_absolute'), grid(extra=True),
    {'op': 'velocity_map', 'field': 'velocity', 'mapping': [{'from_value': 80, 'to_value': 0}], 'unmapped': 'preserve'},
    {'op': 'velocity_map', 'field': 'velocity', 'mapping': [{'from_value': 80, 'to_value': True}], 'unmapped': 'preserve'},
    {'op': 'velocity_map', 'field': 'velocity', 'mapping': [{'from_value': 80, 'to_value': 5}, {'from_value': 80, 'to_value': 5}], 'unmapped': 'preserve'},
])
def test_qa_invalid_union_operations_never_publish_child(tmp_path, operation):
    with pytest.raises(PocketError):
        apply(material([note('n', 0, 1)]), tmp_path, [operation])
    artifacts = tmp_path / 'store' / 'artifacts'
    assert not artifacts.exists() or not list(artifacts.iterdir())


def test_qa_replay_tamper_and_relocated_store(tmp_path):
    record = material([note('n', 0, 1)])
    result, child = apply(record, tmp_path, [duplicate(2)])
    source = tmp_path / 'store'
    relocated = tmp_path / 'relocated'
    shutil.copytree(source, relocated)
    selection = material_query(record, str(relocated))['selection']
    assert midi_transform(record, selection, [duplicate(2)], str(relocated), 'edit') == result
    path = relocated / result['material']['artifact_uri']
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(PocketError):
        midi_transform(record, selection, [duplicate(2)], str(relocated), 'edit')
    assert read_record(result['material'], str(source)) == child
    assert hashlib.sha256(Path(source / result['material']['artifact_uri']).read_bytes()).hexdigest() == result['material']['sha256']


def test_qa_duplicate_source_identity_does_not_depend_on_other_selected_notes(tmp_path):
    record = material([note('a', 0, Fraction(1, 4)), note('b', 1, Fraction(1, 4), 64)])
    _, one = apply(record, tmp_path, [duplicate(2)], ids=['a'], request='one')
    _, both = apply(record, tmp_path, [duplicate(2)], ids=['a', 'b'], request='both')
    a_one = next(n for n in one['notes'] if n['derived_from'] == ['a'])
    a_both = next(n for n in both['notes'] if n['derived_from'] == ['a'])
    assert a_one == a_both


def test_qa_existing_overlap_pair_policy_does_not_gain_unrelated_duration_guard(tmp_path):
    record = material([note('a', 0, 1), note('b', Fraction(1, 2), 1)])
    _, child = apply(record, tmp_path, [grid(grid_qn=q(1), threshold_qn=q(Fraction(1, 2)))])
    assert [n['onset'] for n in child['notes']] == [{'space': 'clip_qn', **q(0)}] * 2
    assert [n['duration_qn'] for n in child['notes']] == [q(1), q(1)]


def test_qa_delete_note_expression_refuses_without_private_removal_policy(tmp_path):
    record = expressed_material()
    with pytest.raises(PocketError):
        apply(record, tmp_path, [{'op': 'delete', 'controller_timeline': 'preserve_existing'}], ids=['note:0'])


@pytest.mark.parametrize('resize_policy', ['stretch_with_gate', 'preserve_fraction', 'crop'])
def test_qa_grid_explicit_note_relative_expression_is_exact(tmp_path, resize_policy):
    record = expressed_material()
    record['notes'][0]['onset'] = {'space': 'clip_qn', **q(Fraction(1, 8))}
    record['curves'][0]['target']['resize_policy'] = resize_policy
    record = seal_literal(record)
    before = copy.deepcopy(record)
    with pytest.raises(PocketError):
        apply(record, tmp_path, [grid()], ids=['note:0'], request='default')
    _, child = apply(record, tmp_path, [grid()], ids=['note:0'], request='explicit',
                     expression_policy='preserve_relative',
                     locks={'selected_fields': ['pitch', 'velocity', 'release_velocity', 'duration', 'expression_shape']})
    assert child['notes'][0]['onset'] == {'space': 'clip_qn', **q(0)}
    expected = copy.deepcopy(record['notes'])
    expected[0]['onset'] = {'space': 'clip_qn', **q(0)}
    assert child['notes'] == expected
    assert child['curves'] == record['curves'] and child['clips'] == record['clips']
    assert record == before


@pytest.mark.parametrize('mutation', ['owned', 'clock_policy', 'before_note', 'after_gate'])
def test_qa_grid_unsupported_expression_never_crosses_ownership_or_gate(tmp_path, mutation):
    record = expressed_material()
    record['notes'][0]['onset'] = {'space': 'clip_qn', **q(Fraction(1, 8))}
    curve = record['curves'][0]
    if mutation == 'owned':
        curve['target']['ownership'] = 'host_automation'
    elif mutation == 'clock_policy':
        curve['target']['resize_policy'] = 'preserve_ms'
    elif mutation == 'before_note':
        curve['points'][0]['time'] = q(Fraction(-1, 8))
    else:
        curve['points'][1]['time'] = q(Fraction(1, 2))
    with pytest.raises(PocketError):
        apply(seal_literal(record), tmp_path, [grid()], ids=['note:0'], expression_policy='preserve_relative')


def test_qa_late_operation_failure_never_publishes_earlier_changes(tmp_path):
    record = material([note('a', 0, 1), note('b', 1, 1, 64)])
    before = copy.deepcopy(record)
    operations = [duplicate(4), {'op': 'velocity_map', 'field': 'velocity',
                                'mapping': [{'from_value': 79, 'to_value': 60}], 'unmapped': 'reject'}]
    with pytest.raises(PocketError):
        apply(record, tmp_path, operations)
    artifacts = tmp_path / 'store' / 'artifacts'
    assert not artifacts.exists() or not list(artifacts.iterdir())
    assert record == before


def test_qa_same_request_changed_policy_conflicts_and_raw_tamper_fails_replay(tmp_path):
    handle = import_wire(tmp_path, wire_fixture())
    store = str(tmp_path / 'store')
    record = read_record(handle, store)
    chosen = next(n['id'] for n in record['notes'] if n['pitch']['midi_note'] == 64)
    selection = material_query(handle, store, selection={'note_ids': [chosen]})['selection']
    operation = duplicate(Fraction(1, 2))
    result = midi_transform(handle, selection, [operation], store, 'copy')
    with pytest.raises(PocketError, match='[Ii]dempotency'):
        midi_transform(handle, selection, [operation], store, 'copy', expression_policy='preserve_relative')
    raw = Path(store) / record['sources'][0]['raw']['artifact_uri']
    raw.write_bytes(raw.read_bytes() + b'changed')
    with pytest.raises(PocketError):
        midi_transform(handle, selection, [operation], store, 'copy')
    assert (Path(store) / result['material']['artifact_uri']).is_file()


def test_qa_new_grid_controller_timeline_is_exact_while_note_timing_changes(tmp_path):
    handle = import_wire(tmp_path, wire_fixture())
    store = str(tmp_path / 'store')
    record = read_record(handle, store)
    selected = next(n for n in record['notes'] if n['pitch']['midi_note'] == 64)
    result, child = apply(handle, tmp_path, [grid()], ids=[selected['id']])
    assert child['events'] == record['events']
    assert 'Controller timelines remain fixed' in ' '.join(result['uncertainty'])
    exported = midi_export(child, store, str(tmp_path / 'moved.mid'), 'export-moved', format='smf0', ppq=480)
    rows = decode_wire(read_bytes(exported['midi'], store))[2][0]
    original_rows = decode_wire(wire_fixture())[2][0]
    assert [(t, data) for t, data in rows if data[0] == 176] == [(t, data) for t, data in original_rows if data[0] == 176]
    assert [(t, data) for t, data in rows if data[0] >> 4 in (8, 9) and data[1] == 64] == [
        (Fraction(0), b'\x90\x40\x51'), (Fraction(1, 4), b'\x90\x40\x00')]


def test_qa_duplicate_declared_wire_projection_reports_reparse_limit(tmp_path):
    handle = import_wire(tmp_path, wire_fixture())
    store = str(tmp_path / 'store')
    record = read_record(handle, store)
    selected = next(n for n in record['notes'] if n['pitch']['midi_note'] == 64)
    event_ids = {selected['source_binding']['on_event_id'], selected['source_binding']['off_event_id']}
    for event in record['events']:
        if event['id'] in event_ids:
            event['bytes'][1] = 65
    record = seal_literal(record)
    result, child = apply(record, tmp_path, [duplicate(Fraction(1, 2))], ids=[selected['id']])
    edit = read_record(result['edit'], store)
    report = edit['operation_reports'][0]
    assert report['source_binding_validation'] == 'retained_hash_and_declared_lifecycle'
    assert report['source_reparse'] == 'not_performed'
    assert child['events'] == record['events']
    assert read_bytes(child['sources'][0]['raw'], store) == wire_fixture()


def test_qa_canonical_new_edits_need_no_mido_daw_model_or_network(tmp_path, monkeypatch):
    import builtins

    original_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert name.split('.')[0] not in {'mido', 'mcp', 'torch', 'requests', 'httpx'}, name
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', guarded)
    record = material([note('a', Fraction(1, 8), Fraction(1, 8))])
    _, child = apply(record, tmp_path, [grid(), duplicate(2), {'op': 'delete', 'controller_timeline': 'preserve_existing'}])
    assert len(child['notes']) == 1 and child['notes'][0]['onset'] == {'space': 'clip_qn', **q(2)}


@pytest.mark.parametrize('field', ['grid_qn', 'phase_qn', 'strength', 'threshold_qn'])
def test_qa_new_grid_rationals_refuse_ignored_coordinate_tags(tmp_path, field):
    operation = grid()
    operation[field] = {**operation[field], 'space': 'seconds'}
    with pytest.raises(PocketError):
        apply(material([note('a', 0, 1)]), tmp_path, [operation])


def test_qa_duplicate_offset_refuses_unlabeled_clock_substitution(tmp_path):
    operation = duplicate(2)
    operation['delta_qn']['space'] = 'seconds'
    with pytest.raises(PocketError):
        apply(material([note('a', 0, 1)]), tmp_path, [operation])
