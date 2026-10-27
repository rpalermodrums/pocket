# SPDX-License-Identifier: AGPL-3.0-only
"""Actual public interface and external-material composition acceptance."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys

import pytest
from test_midi_construction_qa import add, literal, merge, split
from test_midi_interfaces import cli_call, environment
from test_midi_qa import decode_wire, literal_material

from pocket_music.artifact_store import read_bytes, read_record
from pocket_music.material import material_import, material_query
from pocket_music.midi_edit import midi_transform
from pocket_music.midi_io import midi_export


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional MCP extra not installed')
def test_qa_construction_real_interfaces_external_source_and_replaceable_steps(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    record = literal_material()
    store = str(tmp_path / 'store')

    async def exchange():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'], env=environment())
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listed = {tool.name: tool for tool in (await session.list_tools()).tools}
            defs = listed['midi_transform'].inputSchema.get('$defs', {})
            for operation in [add(record), split(), merge()]:
                variants = [d for d in defs.values() if d.get('properties', {}).get('op', {}).get('const') == operation['op']]
                assert len(variants) == 1
                assert set(variants[0]['required']) == set(operation)
                assert variants[0]['additionalProperties'] is False

            async def call(name, args):
                response = await session.call_tool(name, args)
                assert not response.isError, response.content
                assert response.structuredContent is None and len(response.content) == 1
                return json.loads(response.content[0].text)

            imported_args = {'source': {'kind': 'material', 'material': record}, 'store_root': store, 'request_id': 'external'}
            imported = await call('material_import', imported_args)
            assert imported == material_import(**imported_args) == cli_call(tmp_path, 'material_import', imported_args)
            query = {'material': imported['material'], 'store_root': store, 'selection': {'note_ids': []}}
            selection = (await call('material_query', query))['selection']
            assert selection == material_query(**query)['selection']
            operation = add(record, [literal(onset_qn=2, duration_qn=1)])
            args = {'material': imported['material'], 'store_root': store, 'selection': selection,
                    'operations': [operation], 'request_id': 'add'}
            added = await call('midi_transform', args)
            assert added == midi_transform(**args) == cli_call(tmp_path, 'midi_transform', args)
            direct = midi_transform(**{**args, 'material': record, 'request_id': 'direct'})
            assert direct['material'] == added['material']
            child = read_record(added['material'], store)
            new_id = child['notes'][-1]['id']
            selected = material_query(added['material'], store, selection={'note_ids': [new_id]})['selection']
            split_args = {**args, 'material': added['material'], 'selection': selected,
                          'operations': [split([{'n': 1, 'd': 2}])], 'request_id': 'split'}
            divided = await call('midi_transform', split_args)
            assert divided == midi_transform(**split_args) == cli_call(tmp_path, 'midi_transform', split_args)
            pieces = read_record(divided['material'], store)
            ids = [n['id'] for n in pieces['notes'] if n['derived_from'] == [new_id]]
            assert len(ids) == 2
            selected = material_query(divided['material'], store, selection={'note_ids': ids})['selection']
            merge_args = {**args, 'material': divided['material'], 'selection': selected,
                          'operations': [merge()], 'request_id': 'merge'}
            merged = await call('midi_transform', merge_args)
            assert merged == midi_transform(**merge_args) == cli_call(tmp_path, 'midi_transform', merge_args)
            export_args = {'material': merged['material'], 'store_root': store,
                           'output_path': str(tmp_path / 'result.mid'), 'request_id': 'export', 'ppq': 3360}
            output = await call('midi_export', export_args)
            assert output == midi_export(**export_args) == cli_call(tmp_path, 'midi_export', export_args)
            rows = [row for track in decode_wire(read_bytes(output['midi'], store))[2] for row in track]
            assert [(t, data) for t, data in rows if data[0] == 0x91 and data[2]] == [(2, b'\x91\x47\x49')]
            assert [(t, data) for t, data in rows if data[0] == 0x81] == [(3, b'\x81\x47\x25')]
            assert read_record(imported['material'], store) == record
            for index, operation in enumerate([
                add(record, [literal(channel=True)]), add(record, [literal(id='forbidden')]),
                add(record, time_space='seconds'), split(articulation='tie'),
                merge(articulation='choose_first'), split([{'n': 2, 'd': 4}]),
            ]):
                bad = {**args, 'operations': [operation], 'request_id': f'invalid-{index}'}
                assert (await session.call_tool('midi_transform', bad)).isError
                assert cli_call(tmp_path, 'midi_transform', bad, success=False)['error'] == 'PocketError'

    asyncio.run(exchange())
