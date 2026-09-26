# SPDX-License-Identifier: AGPL-3.0-only
"""Actual CLI and stdio acceptance for symbolic lifecycle and motif composition."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys

import pytest
from test_midi_develop_qa import definition
from test_midi_interfaces import cli_call, environment
from test_midi_lifecycle_qa import arguments, control, phrase
from test_midi_qa import decode_wire

from pocket_music.artifact_store import read_bytes, read_record
from pocket_music.midi_develop import midi_develop
from pocket_music.midi_io import midi_export
from pocket_music.midi_lifecycle import midi_lifecycle


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional MCP extra not installed')
def test_qa_lifecycle_and_develop_actual_strict_cli_stdio_and_external_composition(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    record = phrase(controls=[control('down', 1, 127), control('up', 4, 0, order=1)])
    lifecycle_args = arguments(record, tmp_path)
    lifecycle = midi_lifecycle(**lifecycle_args)
    assert cli_call(tmp_path, 'midi_lifecycle', lifecycle_args) == lifecycle
    develop_args = {'definition': definition(), 'store_root': lifecycle_args['store_root'], 'request_id': 'develop'}
    developed = midi_develop(**develop_args)
    assert cli_call(tmp_path, 'midi_develop', develop_args) == developed

    async def exchange():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'], env=environment())
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listed = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert {'midi_lifecycle', 'midi_develop'} <= set(listed)
            assert set(listed['midi_lifecycle'].inputSchema['required']) == set(lifecycle_args)
            definitions = listed['midi_develop'].inputSchema['$defs']
            schema = next(d for d in definitions.values() if set(d.get('properties', {})) == set(definition()))
            assert set(schema['required']) == set(definition()) and schema['additionalProperties'] is False
            for name, args, expected in [('midi_lifecycle', lifecycle_args, lifecycle),
                                          ('midi_develop', develop_args, developed)]:
                response = await session.call_tool(name, args)
                assert not response.isError and json.loads(response.content[0].text) == expected
            store = lifecycle_args['store_root']
            varied = developed['artifacts']['alternatives'][1]
            child = read_record(varied, store)
            composed = arguments(child, tmp_path, material=varied, horizon_qn=12,
                                 release_tail_qn=0, request_id='developed-lifecycle')
            response = await session.call_tool('midi_lifecycle', composed)
            assert not response.isError
            observed = json.loads(response.content[0].text)
            assert observed == midi_lifecycle(**composed) == cli_call(tmp_path, 'midi_lifecycle', composed)
            proof = read_record(observed['report'], store)
            assert [row['release_qn'] for row in proof['notes']] == [
                {'n': t, 'd': 1} for t in [1, 4, 5, 8, 9, 12]]
            export_args = {'material': varied, 'store_root': store, 'request_id': 'export',
                           'output_path': str(tmp_path / 'development.mid'), 'ppq': 480}
            response = await session.call_tool('midi_export', export_args)
            assert not response.isError
            exported = json.loads(response.content[0].text)
            assert exported == midi_export(**export_args)
            rows = decode_wire(read_bytes(exported['midi'], store))[2][0]
            assert [(t, data[1]) for t, data in rows if data[0] == 0x90 and data[2]] == [
                (0, 60), (3, 64), (4, 60), (7, 64), (8, 60), (11, 66)]
            assert rows[-1] == (12, b'\xff\x2f')
            invalid = {**lifecycle_args, 'request_id': 'bad-life', 'release_tail_qn': True}
            assert (await session.call_tool('midi_lifecycle', invalid)).isError
            assert cli_call(tmp_path, 'midi_lifecycle', invalid, success=False)['error'] == 'PocketError'
            invalid = {**develop_args, 'request_id': 'bad-develop', 'definition': {**definition(), 'extra': True}}
            assert (await session.call_tool('midi_develop', invalid)).isError
            assert cli_call(tmp_path, 'midi_develop', invalid, success=False)['error'] == 'PocketError'

    asyncio.run(exchange())
