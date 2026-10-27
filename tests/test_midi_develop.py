# SPDX-License-Identifier: AGPL-3.0-only
"""Motif choices preserve fixed material and compose real public providers."""
import copy
import importlib
import json
from fractions import Fraction

import pytest
from test_material_sequence import source

from pocket_music.artifact_store import canonical_bytes, put_record, read_record
from pocket_music.errors import PocketError
from pocket_music.material import finalize_material, load_material, qn
from pocket_music.material_sequence import material_sequence
from pocket_music.midi_develop import midi_develop
from pocket_music.midi_edit import midi_transform


def definition(record=None):
    record = source() if record is None else record
    # Explicit whole clip c:a contains one attack and retained trailing silence.
    return {'label': 'Motif return alternatives',
            'seed': {'material': record, 'material_revision': record['revision_sha256'], 'clip_id': 'c:a'},
            'clock': {'schema': 'pocket.time-context/v1', 'context_id': 'clock:declared',
                      'attribution': 'Fixture author supplied phrase placement.'},
            'origin': {'space': 'phrase_qn', 'n': 0, 'd': 1}, 'length_qn': 8,
            'occurrences': [{'occurrence_id': 'intro', 'at_qn': 0},
                            {'occurrence_id': 'repeat', 'at_qn': 2},
                            {'occurrence_id': 'return', 'at_qn': 6}],
            'locked_occurrence_id': 'intro', 'endpoint_note_id': 'n:a',
            'eligible_occurrence_ids': ['repeat', 'return'],
            'variation': {'pitch_offsets_semitones': [-2, 2],
                          'timing_offsets_qn': [qn(Fraction(-1, 12)), qn(Fraction(1, 12))],
                          'max_changed_notes': 1, 'pitch_min': 48, 'pitch_max': 72},
            'seeds': {'structure': 11, 'pitch': 17, 'timing': 23}}


def run(tmp_path, value=None, request='development'):
    return midi_develop(store_root=str(tmp_path), request_id=request,
                        definition=definition() if value is None else value)


def test_locked_seed_frozen_two_alternatives_and_noaddition(tmp_path):
    value = definition()
    original = copy.deepcopy(value)
    result = run(tmp_path, value)
    assert value == original
    assert len(canonical_bytes(result)) < 16000
    artifacts = result['artifacts']
    assert read_record(artifacts['unchanged'], tmp_path) == source()
    assert len(artifacts['alternatives']) == 2
    a, b = [load_material(handle, tmp_path) for handle in artifacts['alternatives']]
    assert a['notes'][0] == b['notes'][0]
    changed = [(first, second) for first, second in zip(a['notes'], b['notes'], strict=True) if first != second]
    assert len(changed) == 1
    first, second = changed[0]
    assert second['pitch']['midi_note'] - first['pitch']['midi_note'] in (-2, 2)
    for key in set(first) - {'pitch', 'onset'}:
        assert first[key] == second[key]
    silent = load_material(artifacts['no_addition'], tmp_path)
    assert not silent['notes'] and not silent['events'] and not silent['curves']
    assert silent['clips'][0]['length_qn'] == qn(8)
    report = read_record(artifacts['development'], tmp_path)
    assert report['identity_proofs']['b'][0]['rhythm_onsets'] == 'exact'
    assert report['constraint_result']['outside_eligible_endpoints'] == 'exact'
    assert report['step_environment'] == 'caller supplies store_root'
    assert all('store_root' not in step['arguments'] for step in report['public_steps'])
    assert str(tmp_path) not in canonical_bytes(report).decode()


def test_retry_handle_shorthand_and_direct_composition_equivalence(tmp_path):
    value = definition()
    result = run(tmp_path, value)
    assert run(tmp_path, value) == result
    alternate = copy.deepcopy(value)
    alternate['seed']['material'] = put_record(source(), tmp_path)
    alternate['length_qn'] = qn(8)
    for occurrence in alternate['occurrences']:
        occurrence['at_qn'] = qn(occurrence['at_qn'])
    second = run(tmp_path, alternate, 'other')
    assert second['artifacts']['alternatives'] == result['artifacts']['alternatives']
    assert second['artifacts']['no_addition'] == result['artifacts']['no_addition']
    report = read_record(result['artifacts']['development'], tmp_path)
    for step in report['public_steps']:
        provider = {'material_sequence': material_sequence, 'midi_transform': midi_transform}[step['tool']]
        assert provider(store_root=str(tmp_path), **step['arguments']) == step['result']
    value['seeds']['pitch'] += 1
    with pytest.raises(PocketError, match='idempotency_conflict'):
        run(tmp_path, value)


@pytest.mark.parametrize('mutate,match', [
    (lambda d: d.update(extra=True), 'fields'),
    (lambda d: d['seeds'].update(pitch=True), 'integer'),
    (lambda d: d['seeds'].update(extra=1), 'fields'),
    (lambda d: d['seed'].update(material_revision='stale'), 'Stale'),
    (lambda d: d['seed'].update(clip_id='missing'), 'Unknown'),
    (lambda d: d.update(endpoint_note_id='n:b'), 'exact seed clip'),
    (lambda d: d.update(locked_occurrence_id='repeat'), 'locked'),
    (lambda d: d.update(eligible_occurrence_ids=['intro']), 'strictly later'),
    (lambda d: d.update(eligible_occurrence_ids=['repeat', 'repeat']), 'unique'),
    (lambda d: d['variation'].update(pitch_offsets_semitones=[0], timing_offsets_qn=[0]), 'nonzero'),
    (lambda d: d['variation'].update(pitch_offsets_semitones=[100]), 'nonzero'),
    (lambda d: d['variation'].update(timing_offsets_qn=[-2]), 'nonzero'),
    (lambda d: d['variation'].update(timing_offsets_qn=[0, qn(0)]), 'unique'),
    (lambda d: d['variation'].update(pitch_offsets_semitones=[False]), 'integer'),
    (lambda d: d['variation'].update(max_changed_notes=0), 'range'),
    (lambda d: d['variation'].update(pitch_min=61), 'pitch bounds'),
    (lambda d: d['variation'].update(pitch_max=59), 'pitch bounds'),
    (lambda d: d.update(length_qn=4097), '4096'),
    (lambda d: d['occurrences'][1].update(occurrence_id='intro'), 'Duplicate'),
    (lambda d: d.update(occurrences=[d['occurrences'][0]]), 'entries'),
    (lambda d: d['clock'].update(unchecked=True), 'fields'),
])
def test_invalid_constraints_fail_before_publication(tmp_path, mutate, match):
    value = definition()
    mutate(value)
    with pytest.raises(PocketError, match=match):
        run(tmp_path, value)
    assert not (tmp_path / 'artifacts').exists()


def test_source_controller_profile_cannot_be_bypassed(tmp_path):
    record = source()
    record['events'] = [{'id': 'e:cc64', 'time': {'space': 'clip_qn', 'n': 0, 'd': 1}, 'order': 0,
                          'bytes': [176, 64, 127], 'message_type': 'control_change', 'is_meta': False}]
    record['clips'][0]['event_ids'] = ['e:cc64']
    with pytest.raises(PocketError, match='events/controllers'):
        run(tmp_path, definition(finalize_material(record)))
    assert not (tmp_path / 'artifacts').exists()


def test_partial_failure_retains_subordinate_evidence_without_success(tmp_path, monkeypatch):
    module = importlib.import_module('pocket_music.midi_develop')
    def fail(**kwargs):
        raise PocketError('injected selected endpoint refusal')
    monkeypatch.setattr(module, 'midi_transform', fail)
    with pytest.raises(PocketError, match='no valid alternatives advertised'):
        run(tmp_path)
    journal = json.loads((tmp_path / 'requests/development/journal.json').read_text())
    assert journal['state'] == 'failed' and 'receipt' not in journal
    reports = [json.loads(path.read_text()) for path in (tmp_path / 'artifacts').glob('*/record.json')]
    failed = next(record for record in reports if record.get('schema') == 'pocket.motif-development-failure/v1')
    assert failed['valid_alternatives'] == [] and failed['complete'] is False
    assert failed['completed_public_steps'][0]['tool'] == 'material_sequence'
    assert failed['attempting'][0]['tool'] == 'midi_transform'
    with pytest.raises(PocketError, match='did not complete'):
        run(tmp_path)


def test_interruption_retains_known_completed_stages_and_propagates(tmp_path, monkeypatch):
    module = importlib.import_module('pocket_music.midi_develop')
    def interrupt(**kwargs):
        raise KeyboardInterrupt('synthetic interruption')
    monkeypatch.setattr(module, 'midi_transform', interrupt)
    with pytest.raises(KeyboardInterrupt, match='synthetic') as observed:
        run(tmp_path)
    assert 'Incomplete development evidence' in observed.value.__notes__[0]
    reports = [json.loads(path.read_text()) for path in (tmp_path / 'artifacts').glob('*/record.json')]
    failed = next(record for record in reports if record.get('schema') == 'pocket.motif-development-failure/v1')
    assert failed['interruption_kind'] == 'KeyboardInterrupt' and failed['valid_alternatives'] == []
    assert len(failed['completed_public_steps']) == 1
    assert json.loads((tmp_path / 'requests/development/journal.json').read_text())['state'] == 'failed'
    with pytest.raises(PocketError, match='did not complete'):
        run(tmp_path)


def test_transitive_source_tamper_refuses_replay(tmp_path):
    result = run(tmp_path)
    (tmp_path / result['artifacts']['unchanged']['artifact_uri']).write_text('modified')
    with pytest.raises(PocketError, match='integrity'):
        run(tmp_path)


def test_all_eligible_endpoints_can_change_without_new_selection_members(tmp_path):
    value = definition()
    value['variation']['max_changed_notes'] = 32
    result = run(tmp_path, value)
    assert result['change_summary']['changed_endpoint_notes'] == 2
    a, b = [load_material(handle, tmp_path) for handle in result['artifacts']['alternatives']]
    assert a['notes'][0] == b['notes'][0]
    assert [note['id'] for note in a['notes']] == [note['id'] for note in b['notes']]


def test_noaddition_queries_bounded_selection_batches(tmp_path):
    record = source()
    base = record['notes'][0]
    record['notes'] = [{**copy.deepcopy(base), 'id': f'n:{i}',
                        'onset': {'space': 'clip_qn', **qn(Fraction(i, 128))}, 'duration_qn': qn(Fraction(1, 256))}
                       for i in range(128)]
    record['clips'][0]['note_ids'] = [note['id'] for note in record['notes']]
    record['clips'][1]['note_ids'] = []
    value = definition(finalize_material(record))
    value['endpoint_note_id'] = 'n:127'
    value['variation']['timing_offsets_qn'] = [0]
    result = run(tmp_path, value)
    assert result['change_summary']['notes_per_alternative'] == 384
    silent = load_material(result['artifacts']['no_addition'], tmp_path)
    assert silent['notes'] == []
    report = read_record(result['artifacts']['development'], tmp_path)
    assert len([step for step in report['public_steps'] if step['arguments'].get('operations', [{}])[0].get('op') == 'delete']) == 2


def test_maximum_output_boundary_and_overflow(tmp_path):
    record = source()
    base = record['notes'][0]
    record['notes'] = [{**copy.deepcopy(base), 'id': f'n:{i}',
                        'onset': {'space': 'clip_qn', **qn(Fraction(i, 64))}, 'duration_qn': qn(Fraction(1, 128))}
                       for i in range(64)]
    record['clips'][0]['note_ids'] = [note['id'] for note in record['notes']]
    record['clips'][1]['note_ids'] = []
    value = definition(finalize_material(record))
    value.update(length_qn=32, endpoint_note_id='n:63', locked_occurrence_id='o:0',
                 occurrences=[{'occurrence_id': f'o:{i}', 'at_qn': i * 2} for i in range(16)],
                 eligible_occurrence_ids=['o:15'])
    value['variation']['timing_offsets_qn'] = [0]
    result = run(tmp_path, value)
    assert result['change_summary']['notes_per_alternative'] == 1024
    assert len(canonical_bytes(result)) < 16000
    assert load_material(result['artifacts']['no_addition'], tmp_path)['notes'] == []
    extra = copy.deepcopy(record['notes'][-1])
    extra.update(id='n:64', onset={'space': 'clip_qn', **qn(1)})
    record['notes'].append(extra)
    record['clips'][0]['note_ids'].append('n:64')
    record = finalize_material(record)
    value['seed'] = {'material': record, 'material_revision': record['revision_sha256'], 'clip_id': 'c:a'}
    value['endpoint_note_id'] = 'n:64'
    with pytest.raises(PocketError, match='1024'):
        run(tmp_path, value, 'over-budget')


@pytest.mark.parametrize('source_count,expected', [(135, 9998), (136, 10000), (137, 10002)])
def test_material_note_evidence_budget_boundary(tmp_path, source_count, expected):
    record = source()
    base = record['notes'][0]
    record['notes'] = [{**copy.deepcopy(base), 'id': f'n:{i}',
                        'onset': {'space': 'clip_qn', **qn(Fraction(i % 16, 16))}, 'duration_qn': qn(Fraction(1, 32))}
                       for i in range(source_count)]
    record['clips'][0]['note_ids'] = [f'n:{i}' for i in range(16)]
    record['clips'][1]['note_ids'] = [f'n:{i}' for i in range(16, source_count)]
    value = definition(finalize_material(record))
    value.update(length_qn=128, endpoint_note_id='n:15', locked_occurrence_id='o:0',
                 occurrences=[{'occurrence_id': f'o:{i}', 'at_qn': i * 2} for i in range(64)],
                 eligible_occurrence_ids=[f'o:{i}' for i in range(1, 8)])
    value['variation'].update(timing_offsets_qn=[0], max_changed_notes=7)
    if expected > 10000:
        with pytest.raises(PocketError, match='10000 material-note copies'):
            run(tmp_path, value)
        assert not (tmp_path / 'artifacts').exists()
    else:
        result = run(tmp_path, value)
        report = read_record(result['artifacts']['development'], tmp_path)
        assert report['evidence_budget']['material_note_copies'] == expected


def test_thirty_two_changes_for_bounded_small_seed(tmp_path):
    value = definition()
    value.update(length_qn=66, locked_occurrence_id='o:0',
                 occurrences=[{'occurrence_id': f'o:{i}', 'at_qn': i * 2} for i in range(33)],
                 eligible_occurrence_ids=[f'o:{i}' for i in range(1, 33)])
    value['variation']['max_changed_notes'] = 32
    result = run(tmp_path, value)
    assert result['change_summary']['changed_endpoint_notes'] == 32
