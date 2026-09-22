"""Independent code-review regressions for the selected-note extension boundary."""
from __future__ import annotations

import copy
import json
import random
from fractions import Fraction
from types import SimpleNamespace

import pytest
from test_midi_qa import import_wire, literal_material, seal_literal, smf

from pocket_music import midi_edit
from pocket_music.artifact_store import read_record, request_status
from pocket_music.errors import PocketError
from pocket_music.material import material_query
from pocket_music.midi_edit import midi_transform


def source_material(tmp_path):
    payload = smf([b'\x00\x90\x3c\x50\x78\x80\x3c\x25\x78\xff\x2f\x00'], 480, format=0)
    handle = import_wire(tmp_path, payload)
    return read_record(handle, tmp_path / 'store')


def transform(record, tmp_path, operation, *, request='review'):
    selection = material_query(record, str(tmp_path / 'store'), max_bytes=65536)['selection']
    return midi_transform(record, selection, [operation], str(tmp_path / 'store'), request)


def duplication():
    return {'op': 'duplicate', 'delta_qn': {'n': 1, 'd': 4}, 'controller_timeline': 'preserve_existing'}


@pytest.mark.parametrize('bad', [None, [], 4])
def test_review_malformed_raw_source_reference_is_domain_refusal(tmp_path, bad):
    record = source_material(tmp_path)
    record['sources'][0]['raw'] = bad
    record = seal_literal(record)
    before = copy.deepcopy(record)
    with pytest.raises(PocketError):
        transform(record, tmp_path, duplication())
    assert record == before


@pytest.mark.parametrize('bad', [False, 0.0])
def test_review_smf_event_track_aliases_do_not_qualify_as_exact_source_identity(tmp_path, bad):
    record = source_material(tmp_path)
    for event in record['events']:
        if event['message_type'] in ('note_on', 'note_off'):
            event['track_index'] = bad
    with pytest.raises(PocketError):
        transform(seal_literal(record), tmp_path, duplication())


@pytest.mark.parametrize('bad', [[], {}])
def test_review_non_string_operation_discriminator_is_domain_refusal(tmp_path, bad):
    with pytest.raises(PocketError):
        transform(literal_material(), tmp_path, {'op': bad})


def test_review_complete_pretty_receipt_budget_includes_cli_newline(tmp_path):
    # Tune a source ID, not production constants. Hash strings remain fixed
    # length, so the one-row receipt grows exactly one byte per ASCII ID byte.
    record = literal_material()
    record['notes'] = record['notes'][:1]
    record['clips'][0]['note_ids'] = [record['notes'][0]['id']]
    record = seal_literal(record)
    operation = {'op': 'velocity_map', 'field': 'velocity',
                 'mapping': [{'from_value': 80, 'to_value': 81}], 'unmapped': 'reject'}
    initial = transform(record, tmp_path, operation, request='review-0')
    initial_size = len(json.dumps(initial, ensure_ascii=True, indent=2).encode())
    current_id = record['notes'][0]['id']
    record['notes'][0]['id'] = current_id + 'x' * (16384 - initial_size)
    record['clips'][0]['note_ids'] = [record['notes'][0]['id']]
    result = transform(seal_literal(record), tmp_path, operation, request='review-1')
    assert len((json.dumps(result, ensure_ascii=True, indent=2) + '\n').encode()) <= 16384


@pytest.mark.parametrize('mutation', ['missing_schema', 'missing_uri', 'wrong_family', 'extra_field', 'corrupt_bytes'])
def test_review_duplicate_requires_complete_verified_original_handle(tmp_path, mutation):
    record = source_material(tmp_path)
    handle = record['sources'][0]['raw']
    if mutation == 'missing_schema':
        handle.pop('schema')
    elif mutation == 'missing_uri':
        handle.pop('artifact_uri')
    elif mutation == 'wrong_family':
        handle['artifact_schema'] = 'pocket.live-set-bytes/v1'
    elif mutation == 'extra_field':
        handle['trust_me'] = True
    else:
        path = tmp_path / 'store' / handle['artifact_uri']
        path.write_bytes(path.read_bytes() + b'changed')
    with pytest.raises(PocketError):
        transform(seal_literal(record), tmp_path, duplication())


def test_review_repeated_valid_locks_are_projected_once_and_full_proof_is_retained(tmp_path, monkeypatch):
    record = literal_material()
    store = str(tmp_path / 'store')
    selection = material_query(record, store)['selection']
    fields = ['pitch'] * 12000
    locks = {'outside_selection': 'all', 'selected_fields': fields}
    original_fits = midi_edit._reply_fits
    calls = []

    def bounded_check(value):
        calls.append(1)
        assert len(calls) <= 45, 'Receipt trimming must not serialize once per duplicate lock'
        return original_fits(value)

    def forbidden_dumps(*args, **kwargs):
        raise AssertionError('Receipt projection must stream its bounded formatter')

    # Replace only this provider's formatter reference, not shared JSON used
    # to publish the complete immutable edit and material proofs.
    monkeypatch.setattr(midi_edit, 'json', SimpleNamespace(JSONEncoder=json.JSONEncoder, dumps=forbidden_dumps))
    monkeypatch.setattr(midi_edit, '_reply_fits', bounded_check)
    operation = {'op': 'velocity_map', 'field': 'velocity',
                 'mapping': [{'from_value': 80, 'to_value': 81}], 'unmapped': 'reject'}
    result = midi_transform(record, selection, [operation], store, 'lock-proof', locks=locks)
    assert len(calls) <= 2  # Initial fit check preserves small legacy receipts.
    assert fields == ['pitch'] * 12000
    invariant = result['invariant_report']
    assert len(invariant['selected_locked_fields']) == 32
    assert invariant['omitted_selected_locked_fields'] == 11968
    proof = read_record(result['edit'], store)
    assert proof['invariant_report']['selected_locked_fields'] == fields
    assert len(proof['semantic_diff']['changed']) == 3
    assert len((json.dumps(result, ensure_ascii=True, indent=2) + '\n').encode()) <= 16384
    assert midi_transform(record, selection, [operation], store, 'lock-proof', locks=locks) == result


def test_review_small_legacy_receipt_keeps_complete_repeated_lock_list(tmp_path):
    record = literal_material()
    store = str(tmp_path / 'store')
    selection = material_query(record, store)['selection']
    fields = ['pitch'] * 33
    result = midi_transform(record, selection, [{'op': 'velocity', 'value': 81}], store,
                            'small-legacy', locks={'selected_fields': fields})
    assert result['invariant_report']['selected_locked_fields'] == fields
    assert 'omitted_selected_locked_fields' not in result['invariant_report']
    assert len((json.dumps(result, ensure_ascii=True, indent=2) + '\n').encode()) < 16384


@pytest.mark.parametrize('op,kind', [('duplicate', 'inserted'), ('delete', 'deleted')])
def test_review_initial_identity_preview_reports_omissions_and_keeps_full_diff(tmp_path, op, kind):
    record = literal_material()
    record['notes'] = [copy.deepcopy(record['notes'][0]) for _ in range(17)]
    for index, note in enumerate(record['notes']):
        note['id'] = f'n:{index:02}'
        note['pitch']['midi_note'] = 60 + index
    record['clips'][0]['note_ids'] = [note['id'] for note in record['notes']]
    operation = duplication() if op == 'duplicate' else {'op': 'delete', 'controller_timeline': 'preserve_existing'}
    result = transform(seal_literal(record), tmp_path, operation)
    summary = result['change_summary']
    assert summary['total_' + kind] == 17 and len(summary[kind]) == 16
    assert summary['omitted_' + kind] == 1
    assert len(read_record(result['edit'], tmp_path / 'store')['semantic_diff'][kind]) == 17


def _overlap_pairs(record):
    # Definition-level oracle uses all unordered source pairs, intentionally
    # different from the implementation's changed-interval active sweep.
    notes = {note['id']: note for note in record['notes']}
    pairs = set()
    for clip in record['clips']:
        for index, left_id in enumerate(clip['note_ids']):
            left = notes[left_id]
            for right_id in clip['note_ids'][index + 1:]:
                right = notes[right_id]
                if (left['channel'], left['pitch']['midi_note']) != (right['channel'], right['pitch']['midi_note']):
                    continue
                a, b = (Fraction(note['onset']['n'], note['onset']['d']) for note in (left, right))
                x, y = (Fraction(note['duration_qn']['n'], note['duration_qn']['d']) for note in (left, right))
                if a < b + y and b < a + x:
                    pairs.add(tuple(sorted((left_id, right_id))))
    return pairs


@pytest.mark.parametrize('seed', range(32))
def test_review_new_overlap_sweep_matches_legacy_pair_policy(seed):
    rng = random.Random(seed)
    record = literal_material()
    prototype = record['notes'][0]
    record['notes'] = []
    for index in range(8):
        note = copy.deepcopy(prototype)
        note['id'] = str(index)
        note['onset'] = {'space': 'clip_qn', 'n': rng.randrange(-2, 5), 'd': 1}
        note['duration_qn'] = {'n': rng.randrange(1, 4), 'd': 1}
        note['channel'] = rng.randrange(1, 3)
        note['pitch']['midi_note'] = rng.randrange(60, 63)
        record['notes'].append(note)
    record['clips'][0]['note_ids'] = [note['id'] for note in record['notes']]
    child = copy.deepcopy(record)
    for note in child['notes']:
        if rng.random() < .4:
            note['onset']['n'] += rng.choice([-1, 1])
            note['pitch']['midi_note'] = rng.randrange(60, 63)
    if seed % 3 == 0:
        child['notes'].pop()
        child['clips'][0]['note_ids'].pop()
    expected_new = _overlap_pairs(child) - _overlap_pairs(record)
    if expected_new:
        with pytest.raises(PocketError, match='overlap'):
            midi_edit._reject_new_overlaps(record, child)
    else:
        assert midi_edit._reject_new_overlaps(record, child) >= 0


def test_review_overlap_budget_refuses_without_allocating_full_pair_history(monkeypatch):
    record = literal_material()
    record['notes'] = [copy.deepcopy(record['notes'][0]) for _ in range(20)]
    for index, note in enumerate(record['notes']):
        note['id'] = str(index)
        note['duration_qn'] = {'n': 4, 'd': 1}
    record['clips'][0]['note_ids'] = [note['id'] for note in record['notes']]
    child = copy.deepcopy(record)
    for note in child['notes']:
        note['onset']['n'] = 1
    assert _overlap_pairs(record) == _overlap_pairs(child)
    monkeypatch.setattr(midi_edit, '_OVERLAP_COMPARISON_LIMIT', 100)
    with pytest.raises(PocketError, match='bounded comparison budget'):
        midi_edit._reject_new_overlaps(record, child)


def test_review_late_operation_failure_publishes_no_child_and_retry_does_not_reexecute(tmp_path):
    record = literal_material()
    before = copy.deepcopy(record)
    store = str(tmp_path / 'store')
    selection = material_query(record, store)['selection']
    operations = [{'op': 'duplicate', 'delta_qn': 1, 'controller_timeline': 'preserve_existing'},
                  {'op': 'velocity_map', 'field': 'velocity', 'mapping': [{'from_value': 79, 'to_value': 90}],
                   'unmapped': 'reject'}]
    with pytest.raises(PocketError, match='no explicit mapping'):
        midi_transform(record, selection, operations, store, 'late-failure')
    assert record == before and not (tmp_path / 'store/artifacts').exists()
    journal = tmp_path / 'store/requests/late-failure/journal.json'
    retained = journal.read_bytes()
    assert request_status(store, 'late-failure')['journal_state'] == 'failed'
    with pytest.raises(PocketError, match='did not complete'):
        midi_transform(record, selection, operations, store, 'late-failure')
    assert journal.read_bytes() == retained and not (tmp_path / 'store/artifacts').exists()


@pytest.mark.parametrize('field', ['delta_qn', 'grid_qn', 'phase_qn', 'strength', 'threshold_qn'])
def test_review_new_untagged_rationals_never_discard_a_conflicting_time_unit(tmp_path, field):
    operation = {'op': 'grid_quantize', 'grid_qn': {'n': 1, 'd': 4}, 'phase_qn': {'n': 0, 'd': 1},
                 'strength': {'n': 1, 'd': 2}, 'threshold_qn': {'n': 1, 'd': 8}, 'ties': 'earlier',
                 'note_off': 'follow_onset', 'controller_timeline': 'preserve_existing', 'time_space': 'clip_qn'}
    if field == 'delta_qn':
        operation = {**duplication(), 'delta_qn': {'n': 1, 'd': 1}}
    operation[field] = {**operation[field], 'space': 'seconds'}
    with pytest.raises(PocketError):
        transform(literal_material(), tmp_path, operation)
