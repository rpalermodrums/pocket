# SPDX-License-Identifier: AGPL-3.0-only
"""Independent multi-section identities, complete spans and public failure boundaries."""
from __future__ import annotations

import copy

import pytest
from test_material_structure_qa import attribution
from test_midi_qa import seal_literal
from test_midi_relationships_qa import material, note, q

from pocket_music.errors import PocketError
from pocket_music.material_structure import material_structure


def source_graph(tmp_path, *, mutate=None, parent=None, request="graph"):
    """Distinct parents intentionally reuse every local note/clip/voice identity."""
    left = material([note('opening', 0, 1, 60, velocity=79, release=29),
                     note('endpoint', 2, 1, 64, velocity=91, release=37)], length=4)
    right = material([note('opening', 0, 1, 67, velocity=73, release=11),
                      note('endpoint', 2, 1, 71, velocity=83, release=43)], length=4)
    left['clips'][0]['origin'] = {'space': 'arrangement_qn', **q(123)}
    right['clips'][0]['origin'] = {'space': 'phrase_qn', **q(-19)}
    records = [seal_literal(left), seal_literal(right)]
    graph = {'label': 'Independent ambiguous-local-ID sources',
             'materials': [{'key': key, 'material': record} for key, record in zip(('left', 'right'), records)],
             'nodes': [], 'relations': [], 'attribution': attribution(), 'cycle_policy': 'reject'}
    for key, record in zip(('left', 'right'), records):
        graph['nodes'].append({'node_id': f'node:{key}', 'kind': 'phrase', 'label': key,
            'material_key': key, 'material_revision': record['revision_sha256'],
            'clip_id': record['clips'][0]['id'], 'space': 'clip_qn',
            'span_qn': {'start': q(0), 'end': q(4)}, 'note_ids': ['opening', 'endpoint'],
            'attribution': attribution()})
    if parent is not None:
        graph['parent_structure'] = parent
    if mutate is not None:
        mutate(graph)
    result = material_structure('create', str(tmp_path / 'store'), request_id=request, definition=graph)
    return result['artifacts']['structure'], copy.deepcopy(records)


def definition(handle):
    return {'label': 'Independent long arrangement', 'structure': handle,
            'expected_structure_revision': handle['sha256'],
            'clock': {'schema': 'pocket.time-context/v1', 'context_id': 'explicit-long-clock',
                      'attribution': 'Independent explicit quarter-note placements'},
            'origin': {'space': 'arrangement_qn', **q(0)}, 'length_qn': 16388,
            'sections': [{'section_id': 'opening', 'node_id': 'node:left', 'at_qn': 0},
                         {'section_id': 'middle', 'node_id': 'node:right', 'at_qn': 8192},
                         {'section_id': 'outro', 'node_id': 'node:left', 'at_qn': 16384}],
            'locked_section_ids': ['opening', 'outro'],
            'variations': [{'section_id': 'middle', 'endpoint_note_id': 'endpoint',
                            'pitch_offsets_semitones': [2], 'timing_offsets_qn': [0],
                            'pitch_min': 0, 'pitch_max': 127}],
            'seeds': {'structure': 1, 'pitch': 2, 'timing': 3}, 'attribution': attribution()}


def run(tmp_path, value=None, request='arrangement'):
    from pocket_music.arrangement_develop import midi_arrangement_develop
    if value is None:
        handle, _ = source_graph(tmp_path)
        value = definition(handle)
    return midi_arrangement_develop(store_root=str(tmp_path / 'store'), request_id=request, definition=value)


def test_qa_arrangement_long_sparse_multiple_locks_source_identity_and_exact_fields(tmp_path):
    import json

    from pocket_music.artifact_store import read_record
    handle, sources = source_graph(tmp_path)
    result = run(tmp_path, definition(handle))
    store = str(tmp_path / 'store')
    a, b = [read_record(h, store) for h in result['artifacts']['alternatives']]
    assert [row['pitch']['midi_note'] for row in a['notes']] == [60, 64, 67, 71, 60, 64]
    assert [row['pitch']['midi_note'] for row in b['notes']] == [60, 64, 67, 73, 60, 64]
    assert [row['onset'] for row in b['notes']] == [{'space': 'clip_qn', **q(x)} for x in [0, 2, 8192, 8194, 16384, 16386]]
    assert len({row['id'] for row in a['notes']}) == 6
    assert len({row['voice_id'] for row in a['notes']}) == 3
    assert a['notes'][:3] == b['notes'][:3] and a['notes'][4:] == b['notes'][4:]
    assert {k: v for k, v in a['notes'][3].items() if k != 'pitch'} == {k: v for k, v in b['notes'][3].items() if k != 'pitch'}
    assert result['artifacts']['unchanged']['structure'] == handle
    assert [read_record(h, store) for h in result['artifacts']['unchanged']['materials']] == sources
    silent = read_record(result['artifacts']['no_addition'], store)
    assert silent['notes'] == silent['events'] == silent['curves'] == []
    assert silent['clips'][0]['length_qn'] == q(16388)
    assert len(json.dumps(result, indent=2).encode()) <= 16384
    assert run(tmp_path, definition(handle)) == result


def test_qa_arrangement_public_steps_replay_and_paginated_sections(tmp_path):
    from pocket_music.arrangement_develop import midi_arrangement_query
    from pocket_music.artifact_store import read_record
    from pocket_music.material_sequence import material_sequence
    from pocket_music.midi_edit import midi_transform
    result = run(tmp_path)
    store = str(tmp_path / 'store')
    proof = read_record(result['artifacts']['development'], store)
    providers = {'material_sequence': material_sequence, 'midi_transform': midi_transform}
    for step in proof['public_steps']:
        assert 'store_root' not in step['arguments']
        assert providers[step['provider']](store_root=store, **step['arguments']) == step['result']
    cursor, rows = None, []
    while True:
        page = midi_arrangement_query(store_root=store, development=result['artifacts']['development'], view='sections', limit=1, cursor=cursor)
        rows.extend(page['items'])
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert [row['section_id'] for row in rows] == ['opening', 'middle', 'outro']
    assert [row['locked'] for row in rows] == [True, False, True]
    assert [row['source_origin'] for row in rows] == [
        {'space': 'arrangement_qn', **q(123)}, {'space': 'phrase_qn', **q(-19)}, {'space': 'arrangement_qn', **q(123)}]


@pytest.mark.parametrize('change', [
    lambda d: d.update(expected_structure_revision='0' * 64),
    lambda d: d['sections'][1].update(at_qn=3),
    lambda d: d['sections'][1].update(section_id='opening'),
    lambda d: d['sections'][1].update(node_id='unknown'),
    lambda d: d['sections'].reverse(),
    lambda d: d.update(locked_section_ids=['outro']),
    lambda d: d['variations'][0].update(section_id='outro'),
    lambda d: d['variations'][0].update(endpoint_note_id='opening'),
    lambda d: d['variations'][0].update(pitch_offsets_semitones=[0]),
    lambda d: d['variations'][0].update(timing_offsets_qn=[True]),
    lambda d: d.update(length_qn=65537),
])
def test_qa_arrangement_stale_ambiguous_locks_bounds_and_rest_overlap_refuse(tmp_path, change):
    handle, _ = source_graph(tmp_path)
    value = definition(handle)
    change(value)
    with pytest.raises(PocketError):
        run(tmp_path, value)


def test_qa_arrangement_adjacent_full_sections_touch_half_open_without_overlap(tmp_path):
    handle, _ = source_graph(tmp_path)
    value = definition(handle)
    value['sections'][1]['at_qn'] = 4
    value['sections'][2]['at_qn'] = 8
    value['length_qn'] = 12
    assert run(tmp_path, value)['status'] == 'ok'


@pytest.mark.parametrize('mutate', [
    lambda g: g['nodes'][1]['span_qn'].update(end=q(3)),
    lambda g: g['nodes'][1].update(note_ids=['endpoint']),
])
def test_qa_arrangement_requires_full_clip_span_and_membership(tmp_path, mutate):
    handle, _ = source_graph(tmp_path, mutate=mutate)
    with pytest.raises(PocketError, match='whole-clip'):
        run(tmp_path, definition(handle))


@pytest.mark.parametrize('target', ['public_steps', 'sequence_definition', 'sequence_occurrences', 'b_parent', 'b_last_edit', 'changes', 'sections'])
def test_qa_arrangement_resealed_false_report_is_not_query_authority(tmp_path, target):
    from pocket_music.arrangement_develop import midi_arrangement_query
    from pocket_music.artifact_store import put_record, read_record
    result = run(tmp_path)
    store = str(tmp_path / 'store')
    proof = read_record(result['artifacts']['development'], store)
    if target == 'public_steps':
        proof['public_steps'] = []
    elif target.startswith('sequence_'):
        sequence = read_record(proof['sequence'], store)
        if target == 'sequence_definition':
            sequence['definition']['label'] = 'False source recipe'
        else:
            sequence['occurrences'][0]['at_qn'] = q(999)
        proof['sequence'] = put_record(sequence, store)
    elif target.startswith('b_'):
        varied = read_record(proof['alternatives'][1], store)
        if target == 'b_parent':
            varied['parent_revision'] = None
        else:
            varied['provenance']['last_edit'] = {'invented': True}
        proof['alternatives'][1] = put_record(seal_literal(varied), store)
    elif target == 'changes':
        proof['changes'][0]['after']['pitch']['midi_note'] = 74
    else:
        proof['sections'][0]['locked'] = False
    with pytest.raises(PocketError):
        midi_arrangement_query(store_root=store, development=put_record(proof, store))


@pytest.mark.parametrize('boundary', [1, 2, 3])
@pytest.mark.parametrize('interrupted', [False, True])
def test_qa_arrangement_every_public_write_boundary_failure_no_valid_result(tmp_path, monkeypatch, boundary, interrupted):
    import json

    from pocket_music import arrangement_develop as module
    handle, _ = source_graph(tmp_path)
    value = definition(handle)
    count = 0
    error = KeyboardInterrupt if interrupted else RuntimeError
    def wrapper(original):
        def call(**kwargs):
            nonlocal count
            count += 1
            if count == boundary:
                raise error('Independent public boundary failure')
            return original(**kwargs)
        return call
    monkeypatch.setattr(module, 'material_sequence', wrapper(module.material_sequence))
    monkeypatch.setattr(module, 'midi_transform', wrapper(module.midi_transform))
    with pytest.raises((KeyboardInterrupt, RuntimeError, PocketError)):
        run(tmp_path, value)
    assert count == boundary
    records = [json.loads(path.read_text()) for path in (tmp_path / 'store' / 'artifacts').glob('*/record.json')]
    assert not any(r['schema'] == 'pocket.arrangement-development/v1' for r in records)
    failures = [r for r in records if r['schema'] == 'pocket.arrangement-development-failure/v1']
    if boundary > 1:
        assert len(failures) == 1 and failures[0]['valid_alternatives'] == []
        assert len(failures[0]['completed_public_steps']) == boundary - 1
    with pytest.raises(PocketError, match='did not complete'):
        run(tmp_path, value)
    assert count == boundary


def test_qa_arrangement_external_named_graph_cannot_bypass_source_budget(tmp_path):
    from pocket_music.artifact_store import put_bytes, read_bytes, read_record
    handle, _ = source_graph(tmp_path)
    store = str(tmp_path / 'store')
    external = put_bytes(read_bytes(handle, store), store, 'external.json', 'pocket.material-structure/v1')
    try:
        result = run(tmp_path, definition(external))
    except PocketError:
        return  # A declared narrower qualified profile may refuse noncanonical names.
    proof = read_record(result['artifacts']['development'], store)
    assert proof['evidence_budget']['source_material_notes'] == 4
    assert proof['evidence_budget']['source_metadata_bytes'] > 0


def test_qa_arrangement_unused_evidence_ancestry_counts_toward_aggregate_budget(tmp_path):
    from pocket_music.artifact_store import put_record, read_record
    handle, _ = source_graph(tmp_path)
    store = str(tmp_path / 'store')
    unused = material([note(f'unused:{i}', i, 1, 50) for i in range(20)], length=20)
    value = definition(handle)
    value['attribution']['evidence'] = [put_record(unused, store)]
    result = run(tmp_path, value)
    proof = read_record(result['artifacts']['development'], store)
    assert proof['evidence_budget']['source_material_notes'] == 24
    assert proof['evidence_budget']['material_note_copies'] == 24 + 6 * 3


def test_qa_arrangement_stale_cursor_tamper_and_readonly_without_request_history(tmp_path):
    import shutil

    from pocket_music.arrangement_develop import midi_arrangement_query
    from pocket_music.artifact_store import read_record
    result = run(tmp_path)
    handle = result['artifacts']['development']
    store = str(tmp_path / 'store')
    first = midi_arrangement_query(store_root=store, development=handle, view='sections', limit=1)
    with pytest.raises(PocketError):
        midi_arrangement_query(store_root=store, development=handle, view='changes', cursor=first['next_cursor'])
    relocated = tmp_path / 'relocated'
    shutil.copytree(tmp_path / 'store' / 'artifacts', relocated / 'artifacts')
    assert midi_arrangement_query(store_root=str(relocated), development=handle, view='sections', limit=1) == first
    proof = read_record(handle, str(relocated))
    parent = proof['unchanged']['materials'][0]
    (relocated / parent['artifact_uri']).write_bytes(b'corrupted parent')
    with pytest.raises(PocketError, match='integrity'):
        midi_arrangement_query(store_root=str(relocated), development=handle)


@pytest.mark.parametrize('rich', ['controller', 'curve'])
def test_qa_arrangement_selected_rich_source_never_projects_away_expression(tmp_path, rich):
    from test_midi_expression_qa import expression_curve
    from test_midi_lifecycle_qa import control
    def change(graph):
        source = graph['materials'][1]['material']
        if rich == 'controller':
            source['events'] = [control('pedal', 1, 127)]
            source['clips'][0]['event_ids'] = ['pedal']
        else:
            curve = expression_curve('rich', 'endpoint', 'pitch', [(0, 0), (1, 30)])
            source['curves'] = [curve]
            source['clips'][0]['curve_ids'] = ['rich']
            source['notes'][1]['expression_refs'] = ['rich']
        source = seal_literal(source)
        graph['materials'][1]['material'] = source
        graph['nodes'][1]['material_revision'] = source['revision_sha256']
    handle, _ = source_graph(tmp_path, mutate=change)
    with pytest.raises(PocketError):
        run(tmp_path, definition(handle))


def test_qa_arrangement_aggregate_density_budget_refuses_before_child_publication(tmp_path, monkeypatch):
    from pocket_music import arrangement_develop as module
    def larger(graph):
        for index, binding in enumerate(graph['materials']):
            source = material([note('opening' if i == 0 else 'endpoint' if i == 31 else f'n{i}', i, 1, 60)
                               for i in range(32)], length=32)
            binding['material'] = source
            graph['nodes'][index].update(material_revision=source['revision_sha256'],
                span_qn={'start': q(0), 'end': q(32)}, note_ids=[n['id'] for n in source['notes']])
    handle, _ = source_graph(tmp_path, mutate=larger)
    value = definition(handle)
    value['length_qn'] = 1024
    value['sections'] = [{'section_id': f's{i}', 'node_id': 'node:left', 'at_qn': i * 32} for i in range(32)]
    value['locked_section_ids'] = ['s0']
    value['variations'] = [{**value['variations'][0], 'section_id': f's{i}'} for i in range(1, 10)]
    calls = []
    monkeypatch.setattr(module, 'material_sequence', lambda **kwargs: calls.append(kwargs))
    with pytest.raises(PocketError, match='evidence|proof reserve'):
        run(tmp_path, value)
    assert calls == []


@pytest.mark.parametrize('name', ['material_sequence', 'midi_develop', 'audio_hypotheses', 'midi_lifecycle'])
def test_qa_public_provider_stays_callable_after_submodule_import_in_fresh_runtime(name):
    import subprocess
    import sys

    from test_midi_interfaces import environment
    script = '''import importlib, types
import pocket_music
name = __import__('sys').argv[1]
module = importlib.import_module('pocket_music.' + name)
assert isinstance(module, types.ModuleType)
assert callable(getattr(module, name))
assert callable(getattr(pocket_music, name)), 'Public root provider replaced by noncallable module'
namespace = {}
exec('import pocket_music.' + name + ' as observed', namespace)
assert isinstance(namespace['observed'], types.ModuleType), 'Dotted module import lost its module identity'
assert namespace['observed'] is module
from inspect import signature
assert signature(getattr(pocket_music, name)) == signature(getattr(module, name))
sentinel = object()
setattr(module, name, lambda *a, **k: (sentinel, a, k))
assert getattr(pocket_music, name)(1, explicit=2) == (sentinel, (1,), {'explicit': 2})
brand = importlib.import_module('pocket_music.thread')
assert isinstance(brand, types.ModuleType) and not callable(brand)
'''
    process = subprocess.run([sys.executable, '-c', script, name], env=environment(),
                             capture_output=True, text=True, timeout=20, check=False)
    assert process.returncode == 0, process.stderr
