# SPDX-License-Identifier: AGPL-3.0-only
"""Independent actual CLI and stdio parity for explicit expression planning."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys

import pytest
from test_midi_expression_qa import arguments, expressive_phrase, lifecycle
from test_midi_interfaces import cli_call, environment

from pocket_music.artifact_store import put_record
from pocket_music.midi_expression import midi_expression_plan
from pocket_music.midi_lifecycle import midi_lifecycle


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional MCP extra not installed')
def test_qa_expression_actual_cli_stdio_external_material_and_lifecycle(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    record = expressive_phrase()
    args = arguments(record, tmp_path)
    expected = midi_expression_plan(**args)
    assert cli_call(tmp_path, 'midi_expression_plan', args) == expected
    store = args['store_root']
    supplied_lifecycle = midi_lifecycle(material=record, **lifecycle(record), store_root=store,
                                       request_id='separate-lifecycle')
    composed = {**args, 'material': put_record(record, store), 'lifecycle': supplied_lifecycle['report'],
                'request_id': 'composed-expression'}
    composed_result = midi_expression_plan(**composed)
    assert composed_result['plan'] == expected['plan']
    assert cli_call(tmp_path, 'midi_expression_plan', composed) == composed_result

    async def exchange():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'], env=environment())
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listing = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert 'midi_expression_plan' in listing
            schema = listing['midi_expression_plan'].inputSchema
            assert set(schema['required']) == set(args)
            for key in ('receiver_assumption', 'encoding'):
                definition = next(value for value in schema['$defs'].values()
                                  if set(value.get('properties', {})) == set(args[key]))
                assert definition['additionalProperties'] is False
                assert set(definition['required']) == set(args[key])
            for actual_args, result in [(args, expected), (composed, composed_result)]:
                response = await session.call_tool('midi_expression_plan', actual_args)
                assert not response.isError
                assert json.loads(response.content[0].text) == result
            for field, value in [('member_channels', [2.0, 3]), ('extra', 'unqualified')]:
                invalid = copy.deepcopy(args)
                invalid['request_id'] = f'invalid-{field}'
                invalid['receiver_assumption'][field] = value
                assert (await session.call_tool('midi_expression_plan', invalid)).isError
                assert cli_call(tmp_path, 'midi_expression_plan', invalid, success=False)['error'] == 'PocketError'

    asyncio.run(exchange())
