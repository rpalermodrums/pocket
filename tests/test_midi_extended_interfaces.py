# SPDX-License-Identifier: AGPL-3.0-only
"""Actual CLI/stdio parity for independent F3/F5 fixtures and compositions."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from fractions import Fraction
from pathlib import Path

import pytest
from test_midi_edit_extensions_qa import duplicate, grid
from test_midi_interfaces import cli_call, environment
from test_midi_qa import decode_wire
from test_midi_relationships_qa import material, note, q

from pocket_music.artifact_store import read_bytes, read_record
from pocket_music.material import material_import, material_query
from pocket_music.midi_analysis import midi_analyze
from pocket_music.midi_edit import midi_transform
from pocket_music.midi_io import midi_export


def external_phrase():
    return material([note('a', Fraction(1, 8), Fraction(1, 8), 60, voice='bass', role='bass', velocity=40),
                     note('b', Fraction(1, 2), Fraction(1, 8), 64, voice='upper', role='upper', velocity=80),
                     note('c', 1, Fraction(1, 8), 67, voice='upper', role='upper', velocity=100)])


def edit_plan():
    return [grid(), {'op': 'velocity_map', 'field': 'velocity',
                     'mapping': [{'from_value': 40, 'to_value': 20}], 'unmapped': 'preserve'},
            duplicate(2), {'op': 'delete', 'controller_timeline': 'preserve_existing'}]


def test_qa_actual_cli_new_relationships_use_external_material(tmp_path):
    args = {'material': external_phrase(), 'store_root': str(tmp_path / 'store'),
            'analyses': ['voice_leading', 'role_overlap']}
    direct = midi_analyze(**args)
    assert cli_call(tmp_path, 'midi_analyze', args) == direct
    upper = next(v for v in direct['measurements']['voice_leading']['clips'][0]['voices'] if v['voice_id'] == 'upper')
    assert upper['transitions'][0]['interval']['signed_total_cents'] == q(300)
    assert direct['coverage']['listening'] == 'not_performed'


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional MCP extra not installed')
def test_qa_actual_interfaces_share_external_f3_f5_and_export_capabilities(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    record = external_phrase()
    store = str(tmp_path / 'store')

    async def exchange():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'], env=environment())
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listed = {tool.name: tool for tool in (await session.list_tools()).tools}
            defs = listed['midi_transform'].inputSchema.get('$defs', {})
            for expected_op, required in [
                ('delete', {'op', 'controller_timeline'}),
                ('duplicate', {'op', 'delta_qn', 'controller_timeline'}),
                ('grid_quantize', set(grid())),
                ('velocity_map', {'op', 'field', 'mapping', 'unmapped'}),
            ]:
                variants = [d for d in defs.values() if d.get('properties', {}).get('op', {}).get('const') == expected_op]
                assert len(variants) == 1, (expected_op, defs)
                assert set(variants[0]['required']) == required
                assert variants[0]['additionalProperties'] is False

            async def call(name, args):
                response = await session.call_tool(name, args)
                assert not response.isError, response.content
                assert response.structuredContent is None
                assert len(response.content) == 1
                return json.loads(response.content[0].text)

            imported_args = {'source': {'kind': 'material', 'material': record}, 'store_root': store, 'request_id': 'external'}
            imported = await call('material_import', imported_args)
            assert imported == material_import(**imported_args) == cli_call(tmp_path, 'material_import', imported_args)
            query_args = {'material': imported['material'], 'store_root': store}
            selected = await call('material_query', query_args)
            assert selected == material_query(**query_args)
            analysis_args = {**query_args, 'analyses': ['voice_leading', 'role_overlap'], 'selection': selected['selection']}
            assert await call('midi_analyze', analysis_args) == midi_analyze(**analysis_args) == cli_call(tmp_path, 'midi_analyze', analysis_args)
            direct_args = {'material': record, 'store_root': store, 'selection': selected['selection'],
                           'operations': edit_plan(), 'request_id': 'direct'}
            direct = midi_transform(**direct_args)
            composed_args = {**direct_args, 'material': imported['material'], 'request_id': 'composed'}
            composed = await call('midi_transform', composed_args)
            assert composed == midi_transform(**composed_args) == cli_call(tmp_path, 'midi_transform', composed_args)
            assert direct['material'] == composed['material']
            child = read_record(composed['material'], store)
            notes = sorted(child['notes'], key=lambda n: n['pitch']['midi_note'])
            assert [n['onset'] for n in notes] == [{'space': 'clip_qn', **q(t)} for t in [2, Fraction(5, 2), 3]]
            assert [n['velocity']['value'] for n in notes] == [20, 80, 100]
            assert {n['derived_from'][0] for n in notes} == {'a', 'b', 'c'}
            assert not {'a', 'b', 'c'} & {n['id'] for n in notes}
            export_args = {'material': composed['material'], 'store_root': store,
                           'output_path': str(tmp_path / 'edited.mid'), 'request_id': 'export', 'ppq': 480}
            exported = await call('midi_export', export_args)
            assert exported == midi_export(**export_args) == cli_call(tmp_path, 'midi_export', export_args)
            _, _, tracks = decode_wire(read_bytes(exported['midi'], store))
            attacks = [(t, data[1], data[2]) for row in tracks for t, data in row if data[0] >> 4 == 9 and data[2] > 0]
            assert attacks == [(Fraction(2), 60, 20), (Fraction(5, 2), 64, 80), (Fraction(3), 67, 100)]
            assert read_record(imported['material'], store) == record

            bad_operations = [
                {'op': 'delete'}, duplicate(1) | {'controller_timeline': 'copy'},
                grid(strength=True), grid(time_space='arrangement_qn'), grid(note_off='keep_absolute'),
                {'op': 'velocity_map', 'field': 'velocity', 'mapping': [{'from_value': 40, 'to_value': 20.0}], 'unmapped': 'preserve'},
                {'op': 'velocity_map', 'field': 'velocity', 'mapping': [{'from_value': 40, 'to_value': 20, 'extra': 1}], 'unmapped': 'preserve'},
            ]
            for index, operation in enumerate(bad_operations):
                bad = {**composed_args, 'request_id': f'bad-{index}', 'operations': [operation]}
                assert (await session.call_tool('midi_transform', bad)).isError
                assert cli_call(tmp_path, 'midi_transform', bad, success=False)['error'] == 'PocketError'
            assert read_record(imported['material'], store) == record
            assert read_record(composed['material'], store) == child

    asyncio.run(asyncio.wait_for(exchange(), timeout=60))


def test_qa_actual_cli_relationship_size_refusal_is_actionable_json(tmp_path):
    record = material([note('音' * 5000, 0, 1), note('b', 1, 1)])
    error = cli_call(tmp_path, 'midi_analyze', {'material': record, 'store_root': str(tmp_path / 'store'),
                    'analyses': ['voice_leading']}, success=False)
    assert error['error'] == 'PocketError'
    assert '16 KiB' in json.dumps(error)
    assert not (Path(tmp_path) / 'store').exists()


def test_qa_actual_cli_edit_receipt_includes_newline_in_byte_bound(tmp_path):
    import hashlib

    def arguments(length, directory):
        record = material([note('n' * length, 0, 1)])
        selected = {'material_revision': record['revision_sha256'], 'note_ids': [record['notes'][0]['id']]}
        # Inline exact selection is a public path; no prerequisite query history.
        selected['selection_sha256'] = hashlib.sha256(json.dumps(
            selected, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        return {'material': record, 'selection': selected, 'store_root': str(directory), 'request_id': 'bound',
                'operations': [{'op': 'velocity_map', 'field': 'velocity',
                                'mapping': [{'from_value': 80, 'to_value': 81}], 'unmapped': 'reject'}]}

    small = midi_transform(**arguments(1, tmp_path / 'small'))
    length = 16384 - len(json.dumps(small, ensure_ascii=True, indent=2).encode()) + 1
    args = arguments(length, tmp_path / 'large')
    direct = midi_transform(**args)
    assert len((json.dumps(direct, ensure_ascii=True, indent=2) + '\n').encode()) <= 16384
    assert cli_call(tmp_path, 'midi_transform', args) == direct
    edit = read_record(direct['edit'], args['store_root'])
    assert edit['semantic_diff']['changed'][0]['id'] == 'n' * length
