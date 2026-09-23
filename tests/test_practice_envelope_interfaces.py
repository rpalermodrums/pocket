"""Real CLI and MCP use the same declared join provider and closed nested shape."""
import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_practice_envelopes import envelope_fixture

import pocket_music


def calls(tmp_path):
    spec, _ = envelope_fixture(tmp_path)
    result = pocket_music.practice_envelope(**spec)
    render = result['artifacts']['render']
    comparison = {'store_root': spec['store_root'], 'request_id': 'compare', 'baseline': spec['render'],
                  'variants': [render], 'question': 'Synthetic transport proof only'}
    return [('practice_envelope', spec), ('practice_compare_processed', comparison),
            ('practice_query', {'store_root': spec['store_root'], 'artifact': render})]


def test_cli_envelope(tmp_path):
    for index, (name, spec) in enumerate(calls(tmp_path)):
        path = tmp_path/f'spec-{index}.json'
        path.write_text(json.dumps(spec))
        result = subprocess.run([sys.executable, '-m', 'pocket_music.cli', name.replace('_', '-'), '--spec', str(path)],
                                capture_output=True, text=True, timeout=30, check=False)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == getattr(pocket_music, name)(**spec)


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional agent extra')
def test_mcp_envelope(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    specs = calls(tmp_path)
    async def run():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'],
            env={'PYTHONPATH': str(Path(__file__).resolve().parents[1]/'src')})
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            schema = next(t.inputSchema for t in (await session.list_tools()).tools if t.name == 'practice_envelope')
            assert schema['$defs']['JoinEnvelope']['additionalProperties'] is False
            for name, spec in specs:
                result = await session.call_tool(name, spec)
                assert not result.isError, result.content
                assert json.loads(result.content[0].text) == getattr(pocket_music, name)(**spec)
            name, spec = specs[0]
            result = await session.call_tool(name, {**spec, 'request_id': 'invalid',
                'joins': [{**spec['joins'][0], 'align_automatically': True}]})
            assert result.isError
    asyncio.run(asyncio.wait_for(run(), 60))
