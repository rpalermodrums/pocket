# SPDX-License-Identifier: AGPL-3.0-only
"""Owner checks for explicit whole-clip arrangement choices and exact locks."""
import copy
import importlib

import pytest
from test_material_sequence import source
from test_material_structure import attributed

from pocket_music.arrangement_develop import midi_arrangement_develop, midi_arrangement_query
from pocket_music.artifact_store import canonical_bytes, put_record, read_record, request_status
from pocket_music.errors import PocketError
from pocket_music.material import load_material, qn
from pocket_music.material_structure import material_structure


def definition(tmp_path):
    record = source()
    graph = {'label': 'Supplied graph', 'materials': [{'key': 'source', 'material': record}],
             'nodes': [], 'relations': [], 'attribution': attributed(), 'cycle_policy': 'reject'}
    for index, clip in enumerate(record['clips']):
        graph['nodes'].append({'node_id': f'node:{index}', 'kind': 'phrase', 'label': f'Clip {index}',
            'material_key': 'source', 'material_revision': record['revision_sha256'], 'clip_id': clip['id'],
            'space': 'clip_qn', 'span_qn': {'start': qn(0), 'end': clip['length_qn']},
            'note_ids': clip['note_ids'], 'attribution': attributed()})
    handle = material_structure('create', str(tmp_path), request_id='graph', definition=graph)['artifacts']['structure']
    return {'label': 'Sparse declared form', 'structure': handle, 'expected_structure_revision': handle['sha256'],
            'clock': {'schema': 'pocket.time-context/v1', 'context_id': 'declared-clock', 'attribution': 'Explicit fixture placement'},
            'origin': {'space': 'arrangement_qn', 'n': 0, 'd': 1}, 'length_qn': 65536,
            'sections': [{'section_id': 'opening', 'node_id': 'node:0', 'at_qn': 0},
                         {'section_id': 'return', 'node_id': 'node:1', 'at_qn': 16000},
                         {'section_id': 'outro', 'node_id': 'node:0', 'at_qn': 65534}],
            'locked_section_ids': ['opening', 'outro'], 'variations': [
                {'section_id': 'return', 'endpoint_note_id': 'n:b', 'pitch_offsets_semitones': [2],
                 'timing_offsets_qn': [0], 'pitch_min': 48, 'pitch_max': 84}],
            'seeds': {'structure': 3, 'pitch': 7, 'timing': 11}, 'attribution': attributed()}


def run(tmp_path, value=None, request='arrange'):
    return midi_arrangement_develop(store_root=str(tmp_path), request_id=request,
                                    definition=definition(tmp_path) if value is None else value)


def test_long_sparse_sections_locked_outro_and_exact_source_proofs(tmp_path):
    value = definition(tmp_path)
    original = copy.deepcopy(value)
    result = run(tmp_path, value)
    assert value == original and len(canonical_bytes(result)) < 16384
    report = read_record(result['artifacts']['development'], tmp_path)
    assert result['artifacts']['unchanged']['structure'] == value['structure']
    a, b = [load_material(handle, tmp_path) for handle in result['artifacts']['alternatives']]
    assert a['notes'][0] == b['notes'][0] and a['notes'][-1] == b['notes'][-1]
    assert b['notes'][1]['pitch']['midi_note'] == 69
    for field in a['notes'][1].keys() - {'pitch'}:
        assert a['notes'][1][field] == b['notes'][1][field]
    assert report['sections'][1]['at_qn'] == qn(16000)
    assert report['sections'][2]['end_qn'] == qn(65536)
    assert report['evidence_budget']['material_note_copies'] == 11
    assert not load_material(result['artifacts']['no_addition'], tmp_path)['notes']
    assert read_record(result['artifacts']['unchanged']['materials'][0], tmp_path) == source()
    for step in report['public_steps']:
        assert 'store_root' not in step['arguments']
    query = midi_arrangement_query(store_root=str(tmp_path), development=result['artifacts']['development'], view='sections', limit=1)
    assert len(query['items']) == 1 and not query['complete']
    following = midi_arrangement_query(store_root=str(tmp_path), development=result['artifacts']['development'], view='sections', cursor=query['next_cursor'])
    assert [row['section_id'] for row in following['items']] == ['return', 'outro']


def test_public_composition_replay_and_integer_rational_normalization(tmp_path):
    value = definition(tmp_path)
    first = run(tmp_path, value)
    assert run(tmp_path, value) == first
    normalized = copy.deepcopy(value)
    normalized['length_qn'] = qn(normalized['length_qn'])
    for section in normalized['sections']:
        section['at_qn'] = qn(section['at_qn'])
    normalized['variations'][0]['timing_offsets_qn'] = [qn(0)]
    assert run(tmp_path, normalized, 'normalized')['artifacts'] == first['artifacts'] or (
        run(tmp_path, normalized, 'normalized')['artifacts']['alternatives'] == first['artifacts']['alternatives'])
    report = read_record(first['artifacts']['development'], tmp_path)
    from pocket_music.material_sequence import material_sequence
    from pocket_music.midi_edit import midi_transform
    providers = {'material_sequence': material_sequence, 'midi_transform': midi_transform}
    for step in report['public_steps']:
        assert providers[step['provider']](store_root=str(tmp_path), **step['arguments']) == step['result']


@pytest.mark.parametrize('change', [
    lambda d: d.update(extra=True), lambda d: d.update(length_qn=65537), lambda d: d.update(length_qn=True),
    lambda d: d.update(expected_structure_revision='0' * 64),
    lambda d: d['sections'][1].update(at_qn=1),
    lambda d: d['sections'][1].update(at_qn=-1),
    lambda d: d['sections'][1].update(section_id='opening'),
    lambda d: d['sections'][1].update(node_id='unknown'),
    lambda d: d.update(locked_section_ids=['unknown']),
    lambda d: d.update(locked_section_ids=['opening', 'opening']),
    lambda d: d['variations'][0].update(section_id='outro'),
    lambda d: d['variations'][0].update(endpoint_note_id='missing'),
    lambda d: d['variations'][0].update(pitch_offsets_semitones=[0]),
    lambda d: d['variations'][0].update(timing_offsets_qn=[True]),
    lambda d: d['variations'][0].update(pitch_offsets_semitones=[2, 2]),
    lambda d: d['variations'][0].update(pitch_min=70),
    lambda d: d['seeds'].update(pitch=True),
])
def test_strict_preflight_refuses_before_publication(tmp_path, change):
    value = definition(tmp_path)
    change(value)
    before = set(tmp_path.glob('artifacts/*/record.json'))
    with pytest.raises(PocketError):
        run(tmp_path, value)
    assert set(tmp_path.glob('artifacts/*/record.json')) == before


def test_resealed_report_cannot_claim_false_lock_or_change(tmp_path):
    result = run(tmp_path)
    report = read_record(result['artifacts']['development'], tmp_path)
    report['sections'][1]['locked'] = True
    forged = put_record(report, tmp_path)
    with pytest.raises(PocketError, match='proofs mismatch'):
        midi_arrangement_query(store_root=str(tmp_path), development=forged)
    report = read_record(result['artifacts']['development'], tmp_path)
    report['changes'][0]['after']['velocity'] = 12
    forged = put_record(report, tmp_path)
    with pytest.raises(PocketError, match='changes proof'):
        midi_arrangement_query(store_root=str(tmp_path), development=forged)


def test_partial_failure_and_interruption_no_success_receipt(tmp_path, monkeypatch):
    module = importlib.import_module('pocket_music.arrangement_develop')
    value = definition(tmp_path)
    def fail(**kwargs):
        raise KeyboardInterrupt('owned test interruption')
    monkeypatch.setattr(module, 'midi_transform', fail)
    with pytest.raises(KeyboardInterrupt) as captured:
        run(tmp_path, value)
    assert 'Incomplete arrangement evidence' in captured.value.__notes__[0]
    assert request_status(str(tmp_path), 'arrange')['journal_state'] == 'failed'
    failed = [read_record({'schema': 'pocket.artifact-handle/v1', 'artifact_uri': str(path.relative_to(tmp_path)),
        'sha256': path.parent.name, 'artifact_schema': 'pocket.arrangement-development-failure/v1'}, tmp_path)
        for path in tmp_path.glob('artifacts/*/record.json') if 'pocket.arrangement-development-failure/v1' in path.read_text()]
    assert len(failed) == 1 and failed[0]['valid_alternatives'] == []
    assert len(failed[0]['completed_public_steps']) == 1


def budget_definition(tmp_path, unused_count, primary_count=31, section_count=16, change_count=15):
    from pocket_music.material import make_note, new_material
    def material(name, count):
        notes = [make_note(f'{name}:{i}', i, 1, 60, 80, voice='voice') for i in range(count)]
        return new_material(name, tracks=[{'id': 't', 'name': name}], clips=[
            {'id': 'c', 'track_id': 't', 'origin': {'space': 'phrase_qn', **qn(0)}, 'length_qn': qn(count),
             'loop': False, 'note_ids': [row['id'] for row in notes], 'event_ids': [], 'curve_ids': []}], notes=notes)
    main, unused = material('main', primary_count), material('unused', unused_count)
    graph = {'label': 'Unused source ancestry is counted', 'materials': [
        {'key': 'main', 'material': main}, {'key': 'unused', 'material': unused}],
        'nodes': [{'node_id': 'node', 'kind': 'phrase', 'label': 'Seed', 'material_key': 'main',
                   'material_revision': main['revision_sha256'], 'clip_id': 'c', 'space': 'clip_qn',
                   'span_qn': {'start': qn(0), 'end': qn(primary_count)}, 'note_ids': main['clips'][0]['note_ids'],
                   'attribution': attributed()}], 'relations': [], 'attribution': attributed(), 'cycle_policy': 'reject'}
    handle = material_structure('create', str(tmp_path), request_id='budget-graph', definition=graph)['artifacts']['structure']
    return {'label': 'Boundary', 'structure': handle, 'expected_structure_revision': handle['sha256'],
        'clock': {'schema': 'pocket.time-context/v1', 'context_id': 'clock', 'attribution': 'Explicit fixture'},
        'origin': {'space': 'arrangement_qn', **qn(0)}, 'length_qn': primary_count * section_count,
        'sections': [{'section_id': f's{i}', 'node_id': 'node', 'at_qn': primary_count * i} for i in range(section_count)],
        'locked_section_ids': ['s0'], 'variations': [
            {'section_id': f's{i}', 'endpoint_note_id': f'main:{primary_count - 1}', 'pitch_offsets_semitones': [2],
             'timing_offsets_qn': [0], 'pitch_min': 0, 'pitch_max': 127} for i in range(1, change_count + 1)],
        'seeds': {'structure': 1, 'pitch': 2, 'timing': 3}, 'attribution': attributed()}


@pytest.mark.parametrize('unused,total', [(1296, 9999), (1297, 10000), (1298, 10001)])
def test_aggregate_note_budget_boundary_includes_unused_graph_material(tmp_path, unused, total):
    value = budget_definition(tmp_path, unused)
    before = set(tmp_path.glob('artifacts/*/record.json'))
    if total > 10000:
        with pytest.raises(PocketError, match='10000 material-note copies'):
            run(tmp_path, value)
        assert set(tmp_path.glob('artifacts/*/record.json')) == before
    else:
        result = run(tmp_path, value)
        report = read_record(result['artifacts']['development'], tmp_path)
        assert report['evidence_budget']['material_note_copies'] == total
        assert len(report['unchanged']['materials']) == 2
        assert midi_arrangement_query(store_root=str(tmp_path), development=result['artifacts']['development'])['status'] == 'ok'


def test_maximum_output_1024_and_overflow(tmp_path):
    accepted = tmp_path / 'accepted'
    value = budget_definition(accepted, 1, primary_count=32, section_count=32, change_count=1)
    result = run(accepted, value)
    report = read_record(result['artifacts']['development'], accepted)
    assert result['change_summary']['notes_per_alternative'] == 1024
    assert len(report['public_steps']) == 6
    assert midi_arrangement_query(store_root=str(accepted), development=result['artifacts']['development'])['status'] == 'ok'
    refused = tmp_path / 'refused'
    value = budget_definition(refused, 1, primary_count=33, section_count=32, change_count=1)
    with pytest.raises(PocketError, match='1024 notes'):
        run(refused, value)
