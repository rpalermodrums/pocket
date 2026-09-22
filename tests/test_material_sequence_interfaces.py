"""Independent real CLI/MCP and wire-decoded sequence delivery."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from fractions import Fraction

import pytest
from test_material_sequence_qa import definition
from test_midi_interfaces import cli_call, environment
from test_midi_qa import decode_wire

from pocket_music.artifact_store import read_bytes, read_record
from pocket_music.material import material_query
from pocket_music.material_sequence import material_sequence
from pocket_music.midi_edit import midi_transform
from pocket_music.midi_io import midi_export


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional MCP extra not installed')
def test_qa_sequence_actual_schema_cli_stdio_and_composed_wire_export(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    store = str(tmp_path / 'store')
    args = {'definition': definition(), 'store_root': store, 'request_id': 'sequence'}
    direct = material_sequence(**args)
    assert cli_call(tmp_path, 'material_sequence', args) == direct

    async def exchange():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'], env=environment())
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listed = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert 'material_sequence' in listed
            definitions = listed['material_sequence'].inputSchema['$defs']
            schema = next(d for d in definitions.values() if set(d.get('properties', {})) == set(definition()))
            assert set(schema['required']) == set(definition()) and schema['additionalProperties'] is False
            response = await session.call_tool('material_sequence', args)
            assert not response.isError and json.loads(response.content[0].text) == direct
            child = read_record(direct['artifacts']['material'], store)
            selected = material_query(direct['artifacts']['material'], store,
                                      selection={'note_ids': [child['notes'][1]['id']]})['selection']
            edit_args = {'material': direct['artifacts']['material'], 'selection': selected, 'store_root': store,
                         'request_id': 'edit', 'operations': [{'op': 'velocity', 'value': 99}]}
            response = await session.call_tool('midi_transform', edit_args)
            assert not response.isError
            edited = json.loads(response.content[0].text)
            assert edited == midi_transform(**edit_args) == cli_call(tmp_path, 'midi_transform', edit_args)
            export_args = {'material': edited['material'], 'store_root': store,
                           'output_path': str(tmp_path / 'sequence.mid'), 'request_id': 'export', 'ppq': 480}
            response = await session.call_tool('midi_export', export_args)
            assert not response.isError
            exported = json.loads(response.content[0].text)
            assert exported == midi_export(**export_args) == cli_call(tmp_path, 'midi_export', export_args)
            rows = decode_wire(read_bytes(exported['midi'], store))[2][0]
            attacks = [(time, data[1], data[2]) for time, data in rows if data[0] == 0x90 and data[2]]
            assert attacks == [(Fraction(1, 3), 60, 80), (Fraction(13, 2), 67, 99), (Fraction(28, 3), 60, 80)]
            assert rows[-1] == (13, b'\xff\x2f')
            assert read_record(direct['artifacts']['material'], store) == child
            for index, change in enumerate([{'extra': 1}, {'length_qn': True}, {'clock': None}]):
                bad = {**args, 'request_id': f'bad-{index}', 'definition': {**definition(), **change}}
                assert (await session.call_tool('material_sequence', bad)).isError
                assert cli_call(tmp_path, 'material_sequence', bad, success=False)['error'] == 'PocketError'

    asyncio.run(exchange())
