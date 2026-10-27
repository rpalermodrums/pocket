# SPDX-License-Identifier: AGPL-3.0-only
"""Real CLI and stdio MCP call the same preview provider with a closed declared profile."""
import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_musical_context import fixture

import pocket_music
from pocket_music.capabilities import capabilities_list

PROFILE = 'browser-pcm16-original-rate/v1'


def calls(tmp_path):
    store, context, _, _, _ = fixture(tmp_path)
    render = pocket_music.practice_render(store, 'raw', context, ['first', 'again'])['artifacts']['render']
    spec = {'store_root': store, 'request_id': 'preview', 'render': render, 'profile': PROFILE}
    preview = pocket_music.practice_preview(**spec)['artifacts']['preview']
    return [('practice_preview', spec), ('practice_query', {'store_root': store, 'artifact': preview}),
            ('practice_query', {'store_root': store, 'artifact': preview, 'section': 'mappings'})]


def test_cli_preview_and_query(tmp_path):
    for index, (name, spec) in enumerate(calls(tmp_path)):
        path = tmp_path / f'spec-{index}.json'
        path.write_text(json.dumps(spec))
        result = subprocess.run([sys.executable, '-m', 'pocket_music.cli', name.replace('_', '-'), '--spec', str(path)],
                                capture_output=True, text=True, timeout=60, check=False)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == getattr(pocket_music, name)(**spec)
    (tmp_path / 'again').mkdir()
    name, spec = calls(tmp_path / 'again')[0]
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps({**spec, 'request_id': 'other', 'profile': 'browser-float32/v1'}))
    result = subprocess.run([sys.executable, '-m', 'pocket_music.cli', '--error-format', 'v2', 'practice-preview',
                             '--spec', str(path)], capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 2
    assert json.loads(result.stderr)['code'] == 'unsupported_profile'


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional agent extra')
def test_mcp_preview_and_query(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    specs = calls(tmp_path)
    async def run():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'],
            env={'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tool = next(t for t in (await session.list_tools()).tools if t.name == 'practice_preview')
            assert tool.inputSchema['properties']['profile']['const'] == PROFILE
            assert tool.inputSchema['additionalProperties'] is False
            assert tool.annotations.readOnlyHint is False
            for name, spec in specs:
                result = await session.call_tool(name, spec)
                assert not result.isError, result.content
                assert json.loads(result.content[0].text) == getattr(pocket_music, name)(**spec)
            name, spec = specs[0]
            for invalid in ({**spec, 'request_id': 'bad-profile', 'profile': 'browser-float32/v1'},
                            {**spec, 'request_id': 'extra', 'normalize': True}):
                assert (await session.call_tool(name, invalid)).isError
    asyncio.run(asyncio.wait_for(run(), 60))


def test_discovery_declares_preview_families_and_limits():
    rows = {row['public_tool']: row for row in capabilities_list(domain='practice', limit=50)['capabilities']}
    preview = rows['practice_preview']
    assert preview['accepted_artifact_schemas'] == ['pocket.practice-render/v1', 'pocket.practice-envelope/v1']
    assert preview['returned_artifact_schemas'] == ['pocket.practice-preview/v1', 'pocket.practice-preview-audio/v1']
    assert preview['required_profile'] == PROFILE and preview['side_effects'] == ['new_local_artifacts']
    assert 'pocket.practice-preview/v1' in rows['practice_query']['accepted_artifact_schemas']
