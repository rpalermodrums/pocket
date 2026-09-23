"""Real CLI and stdio MCP create and read preview-aware reports through the same providers."""
import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_practice_feedback_previews import NOTE, processed

import pocket_music
from pocket_music.artifact_store import read_record


def calls(tmp_path):
    store, comparison, raw, joined, previews, _ = processed(tmp_path)
    old = pocket_music.practice_feedback(store, 'old', comparison, raw, [0, 10], 'Synthetic reviewer', 'agent', NOTE,
                                         None)['artifacts']['feedback']
    spec = {'store_root': store, 'request_id': 'heard', 'comparison': comparison, 'render': joined,
            'interval_frames': [7990, 8010], 'actor': 'Synthetic reviewer', 'actor_kind': 'human', 'note': NOTE,
            'decision': 'revise', 'preview': previews['joined']}
    heard = pocket_music.practice_feedback(**spec)['artifacts']['feedback']
    return [('practice_feedback', spec),
            ('practice_query', {'store_root': store, 'artifact': heard}),
            ('practice_feedback_query', {'store_root': store, 'feedback': [old, heard], 'render': joined}),
            ('practice_feedback_query', {'store_root': store, 'feedback': [old, heard]})]


def test_cli_preview_reports(tmp_path):
    for index, (name, spec) in enumerate(calls(tmp_path)):
        path = tmp_path / f'spec-{index}.json'
        path.write_text(json.dumps(spec))
        result = subprocess.run([sys.executable, '-m', 'pocket_music.cli', name.replace('_', '-'), '--spec', str(path)],
                                capture_output=True, text=True, timeout=60, check=False)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == getattr(pocket_music, name)(**spec)


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional agent extra')
def test_mcp_preview_reports(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    specs = calls(tmp_path)
    async def run():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'],
            env={'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tool = next(t for t in (await session.list_tools()).tools if t.name == 'practice_feedback')
            assert 'preview' in tool.inputSchema['properties'] and 'preview' not in tool.inputSchema['required']
            for name, spec in specs:
                result = await session.call_tool(name, spec)
                assert not result.isError, result.content
                assert json.loads(result.content[0].text) == getattr(pocket_music, name)(**spec)
            name, spec = specs[0]
            baseline = read_record(spec['comparison'], spec['store_root'])['baseline']
            stale = {**spec, 'request_id': 'stale', 'render': baseline}
            assert (await session.call_tool(name, stale)).isError
            assert (await session.call_tool(name, {**spec, 'request_id': 'x', 'preview': {'schema': 'bad'}})).isError
    asyncio.run(asyncio.wait_for(run(), 90))
