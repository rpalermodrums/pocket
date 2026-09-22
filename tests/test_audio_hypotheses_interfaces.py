"""Real CLI/stdio composition for retained audio evidence and corrections."""
import asyncio
import importlib.util
import json
import sys

import pytest
from test_audio_hypotheses_qa import arguments, corrections
from test_midi_interfaces import cli_call, environment

from pocket_music.artifact_store import read_record
from pocket_music.audio_hypotheses import audio_hypotheses, audio_hypothesis_correct, audio_hypothesis_query


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional MCP extra not installed')
def test_qa_audio_actual_cli_stdio_direct_corrected_query(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    args = arguments(tmp_path, True)
    result = audio_hypotheses(**args)
    assert cli_call(tmp_path, 'audio_hypotheses', args) == result
    handle = result['artifacts']['hypotheses']
    edit = corrections(args, handle, read_record(handle, args['store_root']), 2)
    edited = audio_hypothesis_correct(**edit)
    assert cli_call(tmp_path, 'audio_hypothesis_correct', edit) == edited
    query_args = {'store_root': args['store_root'], 'hypotheses': edited['artifacts']['hypotheses'],
                  'view': 'annotations', 'limit': 2, 'max_bytes': 4096}
    query = audio_hypothesis_query(**query_args)
    assert cli_call(tmp_path, 'audio_hypothesis_query', query_args) == query

    async def exchange():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'], env=environment())
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listing = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert {'audio_hypotheses', 'audio_hypothesis_correct', 'audio_hypothesis_query'} <= set(listing)
            for name, supplied, expected in [('audio_hypotheses', args, result),
                                              ('audio_hypothesis_correct', edit, edited),
                                              ('audio_hypothesis_query', query_args, query)]:
                response = await session.call_tool(name, supplied)
                assert not response.isError and json.loads(response.content[0].text) == expected
            composed = {**query_args, 'cursor': query['next_cursor']}
            response = await session.call_tool('audio_hypothesis_query', composed)
            assert not response.isError
            assert json.loads(response.content[0].text) == audio_hypothesis_query(**composed)
            assert json.loads(response.content[0].text)['items'][0]['annotation']['kind'] == 'note_hypothesis'
            for invalid in [{**args, 'request_id': 'bad-bool', 'source': {**args['source'], 'start_frame': True}},
                            {**args, 'request_id': 'bad-extra', 'settings': {**args['settings'], 'extra': True}}]:
                assert (await session.call_tool('audio_hypotheses', invalid)).isError
                assert cli_call(tmp_path, 'audio_hypotheses', invalid, success=False)['error'] == 'PocketError'

    asyncio.run(exchange())
