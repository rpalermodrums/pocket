"""Actual public interfaces and replay in an independent store for long sections."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest
from test_midi_interfaces import cli_call, environment

import pocket_music
from pocket_music.arrangement_develop import midi_arrangement_develop, midi_arrangement_query
from pocket_music.artifact_store import read_bytes, read_record
from pocket_music.material import material_import
from pocket_music.material_structure import material_structure


def fixture(tmp_path):
    store = str(tmp_path / 'store')
    attribution = {'actor': 'interface fixture', 'actor_kind': 'agent',
                   'statement': 'Explicit synthetic sections; no listening.',
                   'uncertainty': ['No perceptual identity claim.'], 'evidence': []}
    materials, nodes = [], []
    for name in ('opening', 'response'):
        material = json.loads((Path(__file__).parents[1] / 'examples' / 'midi-arrangement'
                               / f'{name}.json').read_text())
        handle = material_import({'kind': 'material', 'material': material}, store,
                                 f'import-{name}')['material']
        materials.append({'key': name, 'material': handle})
        nodes.append({'node_id': name, 'kind': 'phrase', 'label': name, 'material_key': name,
                      'material_revision': material['revision_sha256'], 'clip_id': 'clip:authored',
                      'space': 'clip_qn', 'span_qn': {'start': {'n': 0, 'd': 1}, 'end': {'n': 8, 'd': 1}},
                      'note_ids': ['note:0', 'note:1'], 'attribution': attribution})
    graph = material_structure('create', store, 'graph', {
        'label': 'External graph', 'materials': materials, 'nodes': nodes, 'relations': [],
        'attribution': attribution, 'cycle_policy': 'reject'})['artifacts']['structure']
    return {
        'store_root': store, 'request_id': 'arrange',
        'definition': {
            'label': 'Sparse 77-minute declared span', 'structure': graph,
            'expected_structure_revision': graph['sha256'],
            'clock': {'schema': 'pocket.time-context/v1', 'context_id': 'declared',
                      'attribution': 'Synthetic declared clock'},
            'origin': {'space': 'arrangement_qn', 'n': 0, 'd': 1}, 'length_qn': 9240,
            'sections': [{'section_id': 'first', 'node_id': 'opening', 'at_qn': 0},
                         {'section_id': 'middle', 'node_id': 'response', 'at_qn': 4600},
                         {'section_id': 'last', 'node_id': 'opening', 'at_qn': 9224}],
            'locked_section_ids': ['first'],
            'variations': [{'section_id': 'middle', 'endpoint_note_id': 'note:1',
                            'pitch_offsets_semitones': [-2],
                            'timing_offsets_qn': [{'n': 1, 'd': 4}], 'pitch_min': 48, 'pitch_max': 84}],
            'seeds': {'structure': 11, 'pitch': 23, 'timing': 37}, 'attribution': attribution,
        },
    }


def test_public_arrangement_cli_and_independent_composition(tmp_path):
    args = fixture(tmp_path)
    result = midi_arrangement_develop(**args)
    assert cli_call(tmp_path, 'midi_arrangement_develop', args) == result
    handle = result['artifacts']['development']
    report = read_record(handle, args['store_root'])
    separate = tmp_path / 'independent-composition'
    graph_handle = args['definition']['structure']
    graph = read_record(graph_handle, args['store_root'])
    source_handles = [graph_handle, *(row['material'] for row in graph['materials'])]
    for source_handle in source_handles:
        src = Path(args['store_root']) / source_handle['artifact_uri']
        dest = separate / source_handle['artifact_uri']
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        assert src.stat().st_ino != dest.stat().st_ino
    for step in report['public_steps']:
        actual = getattr(pocket_music, step['provider'])(**step['arguments'], store_root=str(separate))
        assert actual == step['result']
    for material in [*result['artifacts']['alternatives'], result['artifacts']['no_addition']]:
        assert read_bytes(material, separate) == read_bytes(material, args['store_root'])
    query = {'store_root': args['store_root'], 'development': handle, 'view': 'sections', 'limit': 1}
    first = midi_arrangement_query(**query)
    assert cli_call(tmp_path, 'midi_arrangement_query', query) == first
    assert first['next_cursor'] is not None
    rest = midi_arrangement_query(**query, cursor=first['next_cursor'])
    assert first['items'] != rest['items']
    a, b = [read_record(h, args['store_root']) for h in result['artifacts']['alternatives']]
    assert len(a['notes']) == len(b['notes']) == 6
    assert a['clips'][0]['length_qn'] == {'n': 9240, 'd': 1}
    assert a['notes'][:2] == b['notes'][:2] and a['notes'][4:] == b['notes'][4:]


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional MCP extra not installed')
def test_arrangement_actual_stdio_schemas_errors_and_queries(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    args = fixture(tmp_path)
    expected = midi_arrangement_develop(**args)

    async def exchange():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'],
                                       env=environment())
        async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
            await session.initialize()
            listed = {tool.name: tool for tool in (await session.list_tools()).tools}
            names = {'midi_arrangement_develop', 'midi_arrangement_query'}
            assert names <= listed.keys()
            schema = listed['midi_arrangement_develop'].inputSchema
            assert set(schema['required']) == set(args)
            nested = next(row for row in schema['$defs'].values()
                          if set(row.get('properties', {})) == set(args['definition']))
            assert nested['additionalProperties'] is False
            assert set(nested['required']) == set(args['definition'])
            response = await session.call_tool('midi_arrangement_develop', args)
            assert not response.isError and json.loads(response.content[0].text) == expected
            for view in ('summary', 'sections', 'changes'):
                query = {'store_root': args['store_root'], 'development': expected['artifacts']['development'],
                         'view': view, 'limit': 2, 'max_bytes': 8192}
                wanted = midi_arrangement_query(**query)
                response = await session.call_tool('midi_arrangement_query', query)
                assert not response.isError and json.loads(response.content[0].text) == wanted
                assert cli_call(tmp_path, 'midi_arrangement_query', query) == wanted
            for change in ('unknown', 'stale', 'boolean-time'):
                malformed = copy.deepcopy(args)
                malformed['request_id'] = 'bad-' + change
                if change == 'unknown':
                    malformed['definition']['extra'] = True
                elif change == 'stale':
                    malformed['definition']['expected_structure_revision'] = '0' * 64
                else:
                    malformed['definition']['sections'][1]['at_qn'] = True
                response = await session.call_tool('midi_arrangement_develop', malformed)
                assert response.isError
                assert cli_call(tmp_path, 'midi_arrangement_develop', malformed, success=False)['error'] == 'PocketError'

    asyncio.run(exchange())
