"""Actual public transports for authored timing composition and proof readback."""
# Imported pytest fixture names intentionally appear as test parameters.
# ruff: noqa: F811
import asyncio
import copy
import importlib.util
import json
import sys

import pytest
from test_midi_interfaces import cli_call, environment
from test_midi_timing_alternatives import fixture as timing_fixture  # noqa: F401

from pocket_music import midi_timing_alternatives, midi_timing_query
from pocket_music.capabilities import capabilities_list
from pocket_music.errors import PocketError


def test_timing_cli_fresh_composition_and_query(tmp_path, timing_fixture):
    args, _ = timing_fixture
    direct = midi_timing_alternatives(**args)
    actual = cli_call(tmp_path, 'midi_timing_alternatives', {**args, 'request_id': 'cli-fresh'})
    assert actual['artifacts'] == direct['artifacts']
    assert actual['alternatives'] == direct['alternatives']
    assert actual['unchanged'] == actual['no_addition'] == direct['unchanged']
    for view in ('summary', 'matches', 'steps'):
        query = {'store_root': args['store_root'], 'manifest': direct['artifacts']['manifest'], 'view': view}
        assert cli_call(tmp_path, 'midi_timing_query', query) == midi_timing_query(**query)
    rows = {row['public_tool']: row for row in capabilities_list(domain='midi', limit=50)['capabilities']}
    assert rows['midi_timing_query']['output_schema'] == 'pocket.midi-timing-query/v1'
    assert rows['midi_timing_query']['native_verified'] is False
    steps = midi_timing_query(store_root=args['store_root'], manifest=direct['artifacts']['manifest'], view='steps')
    assert all(row['edit']['artifact_schema'] in rows['midi_timing_query']['returned_artifact_schemas']
               for row in steps['items'] if row['edit'] is not None)
    assert rows['midi_timing_alternatives']['accepted_artifact_schemas'] == [
        'pocket.material/v1', 'pocket.audio-region-hypotheses/v1', 'pocket.time-map/v1']


@pytest.mark.parametrize('field', ['boolean_rate', 'mixed_point', 'stale_revision'])
def test_timing_strict_cli_provider_refusals(tmp_path, timing_fixture, field):
    args, _ = timing_fixture
    args = copy.deepcopy(args)
    if field == 'boolean_rate':
        args['alignment']['sample_rate'] = True
    elif field == 'mixed_point':
        args['alternatives'][0]['matches'][0]['point']['original_source_frame_q'] = {'n': 1, 'd': 1}
    else:
        args['expected_hypotheses_revision'] = 'a' * 64
    with pytest.raises(PocketError):
        midi_timing_alternatives(**args)
    assert cli_call(tmp_path, 'midi_timing_alternatives', args, success=False)['error']


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional MCP environment')
def test_timing_actual_stdio_fresh_composition_and_strict_schema(timing_fixture):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    args, _ = timing_fixture
    direct = midi_timing_alternatives(**args)

    async def run():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'],
                                       env=environment())
        async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
            await session.initialize()
            listed = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert listed['midi_timing_query'].annotations.readOnlyHint is True
            result = await session.call_tool('midi_timing_alternatives', {**args, 'request_id': 'mcp-fresh'})
            assert not result.isError, result.content
            actual = json.loads(result.content[0].text)
            assert actual['artifacts'] == direct['artifacts']
            assert actual['alternatives'] == direct['alternatives']
            query = {'store_root': args['store_root'], 'manifest': actual['artifacts']['manifest'], 'view': 'steps'}
            response = await session.call_tool('midi_timing_query', query)
            assert not response.isError
            assert json.loads(response.content[0].text) == midi_timing_query(**query)
            malformed = copy.deepcopy(args)
            malformed['alternatives'][0]['strength']['n'] = True
            response = await session.call_tool('midi_timing_alternatives', malformed)
            assert response.isError
            response = await session.call_tool('midi_timing_query', {**query, 'limit': True})
            assert response.isError

    asyncio.run(asyncio.wait_for(run(), timeout=45))
