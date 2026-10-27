# SPDX-License-Identifier: AGPL-3.0-only
"""Selected-note extension integration, complexity and source-preservation checks."""
from __future__ import annotations

import copy
import json
import random
import tracemalloc
from fractions import Fraction

import pytest
from test_midi_qa import decode_wire, expressed_material, import_wire, literal_material, seal_literal

from pocket_music.artifact_store import read_bytes, read_record
from pocket_music.errors import PocketError
from pocket_music.material import material_import, material_query, qn, rational
from pocket_music.midi_edit import _bounded_receipt, _reject_new_overlaps, midi_transform
from pocket_music.midi_io import midi_export


def one_note(start=0, *, length=8):
    record = literal_material()
    record['notes'] = record['notes'][:1]
    record['notes'][0]['onset'] = {'space': 'clip_qn', **qn(Fraction(start))}
    record['clips'][0]['note_ids'] = [record['notes'][0]['id']]
    record['clips'][0]['length_qn'] = qn(length)
    return seal_literal(record)


def apply(record, tmp_path, operations, *, request='edit', ids=None, **kwargs):
    store = str(tmp_path / 'store')
    selection = material_query(record, store, selection={'note_ids': ids} if ids is not None else None)['selection']
    result = midi_transform(record, selection, operations, store, request, **kwargs)
    return result, read_record(result['material'], store)


def duplicate(delta=1):
    return {'op': 'duplicate', 'delta_qn': qn(Fraction(delta)), 'controller_timeline': 'preserve_existing'}


def grid(**changes):
    return {'op': 'grid_quantize', 'grid_qn': qn(Fraction(1, 2)), 'phase_qn': qn(0),
            'strength': qn(1), 'threshold_qn': qn(Fraction(1, 4)), 'ties': 'earlier',
            'note_off': 'follow_onset', 'controller_timeline': 'preserve_existing',
            'time_space': 'clip_qn', **changes}


def test_duplicate_then_delete_keeps_only_new_unselected_child_and_complete_inverse(tmp_path):
    record = one_note()
    original = copy.deepcopy(record)
    result, child = apply(record, tmp_path, [duplicate(1),
        {'op': 'delete', 'controller_timeline': 'preserve_existing'}, {'op': 'velocity', 'value': 99}])
    assert len(child['notes']) == 1
    clone = child['notes'][0]
    assert clone['id'] != record['notes'][0]['id'] and clone['velocity']['value'] == 80
    assert clone['derived_from'] == [record['notes'][0]['id']] and clone['source_binding'] is None
    edit = read_record(result['edit'], tmp_path / 'store')
    assert read_record(edit['inverse']['restore_parent'], tmp_path / 'store') == original
    assert edit['operation_reports'][0]['copied_fields'] == 'exact_except_id_onset_source_binding_derived_from'
    assert edit['operation_reports'][0]['source_binding_validation'] == 'retained_hash_and_declared_lifecycle'
    assert edit['operation_reports'][0]['source_reparse'] == 'not_performed'
    assert edit['semantic_diff']['inserted'] == [clone['id']]
    assert edit['semantic_diff']['deleted'] == [record['notes'][0]['id']]
    assert record == original


def test_duplicate_offset_is_exact_and_keeps_all_existing_locks(tmp_path):
    record = one_note(Fraction(1, 3))
    record['notes'][0]['duration_qn'] = qn(Fraction(1, 8))
    record = seal_literal(record)
    locks = {'selected_fields': [key for key in record['notes'][0] if key != 'id']}
    result, child = apply(record, tmp_path, [duplicate(Fraction(1, 7))], locks=locks)
    assert child['notes'][0] == record['notes'][0]
    assert rational(child['notes'][1]['onset']) == Fraction(10, 21)
    assert child['notes'][1]['duration_qn'] == record['notes'][0]['duration_qn']
    assert result['invariant_report']['ordered_events'] == 'exact'


def test_transformed_imported_smf_can_duplicate_without_rewriting_old_wire(tmp_path):
    handle = import_wire(tmp_path)
    store = str(tmp_path / 'store')
    original = read_record(handle, store)
    source = original['sources'][0]['raw']
    payload = read_bytes(source, store)
    note_id = original['notes'][1]['id']
    shifted, _ = apply(handle, tmp_path, [{'op': 'transpose', 'semitones': 7}], ids=[note_id], request='transpose')
    copied, child = apply(shifted['material'], tmp_path, [duplicate(2)], ids=[note_id], request='duplicate')
    assert child['events'] == original['events'] and child['sources'] == original['sources']
    assert [n['pitch']['midi_note'] for n in child['notes']] == [60, 71, 71]
    assert child['notes'][-1]['source_binding'] is None
    assert read_bytes(source, store) == payload
    exported = midi_export(copied['material'], store, str(tmp_path / 'derivative.mid'), 'export', ppq=480)
    decoded = decode_wire(read_bytes(exported['midi'], store))[2]
    assert [(t, list(b)) for t, b in decoded[1] if b[0] >> 4 in (8, 9)] == [
        (0, [144, 60, 80]), (1, [128, 60, 37]), (1, [144, 71, 70]), (2, [144, 71, 0]),
        (3, [144, 71, 70]), (4, [128, 71, 0])]
    reparsed = material_import({'kind': 'smf', 'path': str(tmp_path / 'derivative.mid'),
        'expected_sha256': exported['midi']['sha256']}, store, 'reimport')
    notes = read_record(reparsed['material'], store)['notes']
    fields = ('pitch', 'onset', 'duration_qn', 'velocity', 'release_velocity', 'channel', 'mute')
    assert [{k: n[k] for k in fields} for n in notes] == [{k: n[k] for k in fields} for n in child['notes']]
    assert read_bytes(source, store) == payload


@pytest.mark.parametrize('resize', ['stretch_with_gate', 'preserve_fraction', 'crop'])
def test_quantize_relative_expression_requires_explicit_policy_and_keeps_curves_exact(tmp_path, resize):
    record = expressed_material()
    record['notes'][0]['onset'] = {'space': 'clip_qn', **qn(Fraction(1, 8))}
    record['curves'][0]['target']['resize_policy'] = resize
    record = seal_literal(record)
    with pytest.raises(PocketError, match='preserve_relative'):
        apply(record, tmp_path, [grid()], ids=['note:0'])
    _, child = apply(record, tmp_path, [grid()], ids=['note:0'], request='qualified',
        expression_policy='preserve_relative', locks={'selected_fields': ['duration', 'expression_shape']})
    assert child['curves'] == record['curves']
    assert child['notes'][0]['onset'] == {'space': 'clip_qn', **qn(0)}
    assert child['notes'][1:] == record['notes'][1:]


@pytest.mark.parametrize('change', ['milliseconds', 'absolute', 'out_of_gate', 'owned'])
def test_quantize_unqualified_expression_never_transforms_note(tmp_path, change):
    record = expressed_material()
    curve = record['curves'][0]
    if change == 'milliseconds':
        curve['target']['resize_policy'] = 'preserve_ms'
    elif change == 'absolute':
        curve['space'] = 'clip_qn'
    elif change == 'out_of_gate':
        curve['points'][-1]['time'] = qn(1)
    else:
        curve['target']['ownership'] = 'host'
    record = seal_literal(record)
    with pytest.raises(PocketError):
        apply(record, tmp_path, [grid()], ids=['note:0'], expression_policy='preserve_relative')


def test_quantize_does_not_extend_clip_or_clip_a_negative_pickup(tmp_path):
    record = one_note(Fraction(-1, 4), length=0)
    _, child = apply(record, tmp_path, [grid()])
    assert rational(child['notes'][0]['onset']) == Fraction(-1, 2)
    assert child['clips'] == record['clips']
    with pytest.raises(PocketError):
        midi_export(child, str(tmp_path / 'store'), str(tmp_path / 'negative.mid'), 'export')


def test_sequential_velocity_maps_use_current_selected_values(tmp_path):
    record = one_note()
    first = {'op': 'velocity_map', 'field': 'velocity', 'mapping': [{'from_value': 80, 'to_value': 90}],
             'unmapped': 'reject'}
    second = {**first, 'mapping': [{'from_value': 90, 'to_value': 75}]}
    _, child = apply(record, tmp_path, [first, duplicate(1), second])
    assert [n['velocity']['value'] for n in child['notes']] == [75, 90]


def test_noop_maps_honor_locks_and_preserve_preexisting_dense_overlap(tmp_path):
    record = one_note()
    record['notes'] = [{**copy.deepcopy(record['notes'][0]), 'id': f'n{i}'} for i in range(2000)]
    record['clips'][0]['note_ids'] = [n['id'] for n in record['notes']]
    record = seal_literal(record)
    operation = {'op': 'velocity_map', 'field': 'velocity', 'mapping': [{'from_value': 80, 'to_value': 80}],
                 'unmapped': 'reject'}
    result, child = apply(record, tmp_path, [operation], locks={'selected_fields': ['velocity']})
    assert child['notes'] == record['notes']
    assert read_record(result['edit'], tmp_path / 'store')['overlap_validation']['comparisons'] == 0


def test_overlap_sweep_matches_independent_pair_definition():
    rng = random.Random(901)
    def pairs(record):
        rows = record['notes']
        return {frozenset((a['id'], b['id'])) for i, a in enumerate(rows) for b in rows[i + 1:]
                if a['channel'] == b['channel'] and a['pitch']['midi_note'] == b['pitch']['midi_note']
                and rational(a['onset']) < rational(b['onset']) + rational(b['duration_qn'])
                and rational(b['onset']) < rational(a['onset']) + rational(a['duration_qn'])}
    for _ in range(150):
        before = one_note()
        before['notes'] = [{**copy.deepcopy(before['notes'][0]), 'id': f'n{i}',
            'onset': {'space': 'clip_qn', **qn(Fraction(rng.randrange(-4, 9), 4))},
            'duration_qn': qn(Fraction(rng.randrange(1, 5), 4))} for i in range(9)]
        before['clips'][0]['note_ids'] = [n['id'] for n in before['notes']]
        after = copy.deepcopy(before)
        after['notes'][rng.randrange(9)]['onset'] = {'space': 'clip_qn', **qn(Fraction(rng.randrange(-4, 9), 4))}
        if pairs(after) - pairs(before):
            with pytest.raises(PocketError, match='overlap'):
                _reject_new_overlaps(before, after)
        else:
            _reject_new_overlaps(before, after)


def test_overlap_comparison_budget_refuses_without_publishing(tmp_path, monkeypatch):
    record = one_note()
    record['notes'] = [{**copy.deepcopy(record['notes'][0]), 'id': f'n{i}'} for i in range(5)]
    record['clips'][0]['note_ids'] = [n['id'] for n in record['notes']]
    monkeypatch.setattr('pocket_music.midi_edit._OVERLAP_COMPARISON_LIMIT', 5)
    with pytest.raises(PocketError, match='budget'):
        apply(seal_literal(record), tmp_path, [grid(phase_qn=qn(Fraction(1, 8)))])
    assert not list((tmp_path / 'store' / 'artifacts').glob('*/record.json'))


def test_oversized_lock_preview_is_bounded_and_full_proof_retained(tmp_path):
    record = one_note()
    fields = ['pitch'] * 20000
    result, child = apply(record, tmp_path, [duplicate()], locks={'selected_fields': fields})
    assert len((json.dumps(result, indent=2, ensure_ascii=True) + '\n').encode()) <= 16384
    assert result['invariant_report']['selected_locked_fields'] == fields[:32]
    assert result['invariant_report']['omitted_selected_locked_fields'] == len(fields) - 32
    assert read_record(result['edit'], tmp_path / 'store')['invariant_report']['selected_locked_fields'] == fields
    assert child['notes'][0] == record['notes'][0]


def test_receipt_projection_does_not_allocate_or_destroy_huge_opaque_preview(tmp_path):
    result, _ = apply(one_note(), tmp_path, [{'op': 'velocity_map', 'field': 'velocity',
        'mapping': [{'from_value': 80, 'to_value': 90}], 'unmapped': 'reject'}])
    huge = 'x' * (20 * 1024 * 1024)
    result['change_summary']['changed'][0]['fields']['opaque'] = {'before': huge, 'after': huge}
    tracemalloc.start()
    try:
        projected = _bounded_receipt(result)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1024 * 1024
    assert projected['change_summary']['changed'] == []
    assert projected['change_summary']['omitted_changed'] == 1
    assert result['change_summary']['changed'][0]['fields']['opaque']['before'] is huge


def test_small_legacy_receipt_preserves_repeated_lock_list(tmp_path):
    fields = ['pitch'] * 33
    result, _ = apply(one_note(), tmp_path, [{'op': 'velocity', 'value': 90}],
                      locks={'selected_fields': fields})
    assert result['invariant_report']['selected_locked_fields'] == fields
    assert 'omitted_selected_locked_fields' not in result['invariant_report']
    edit = read_record(result['edit'], tmp_path / 'store')
    assert 'extension_profile' not in edit and 'operation_reports' not in edit


def test_source_qualification_reads_each_original_once_per_duplicate_operation(tmp_path, monkeypatch):
    handle = import_wire(tmp_path)
    from pocket_music import midi_edit
    original = midi_edit.read_bytes
    calls = []
    def counted(handle, store):
        calls.append(handle)
        return original(handle, store)
    monkeypatch.setattr(midi_edit, 'read_bytes', counted)
    result, child = apply(handle, tmp_path, [duplicate(2)])
    assert len(child['notes']) == 4 and len(calls) == 1
    assert read_record(result['edit'], tmp_path / 'store')['operation_reports'][0]['source_reparse'] == 'not_performed'


def test_plain_extensions_do_not_import_optional_mido_or_native_adapters(tmp_path, monkeypatch):
    import builtins
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name == 'mido' or name.endswith(('native_candidates', 'native_midi')):
            raise AssertionError('Unrelated optional provider imported')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    _, child = apply(one_note(Fraction(1, 8)), tmp_path, [grid(), duplicate(1),
        {'op': 'velocity_map', 'field': 'velocity', 'mapping': [{'from_value': 80, 'to_value': 90}],
         'unmapped': 'preserve'}, {'op': 'delete', 'controller_timeline': 'preserve_existing'}])
    assert len(child['notes']) == 1 and child['notes'][0]['velocity']['value'] == 80
