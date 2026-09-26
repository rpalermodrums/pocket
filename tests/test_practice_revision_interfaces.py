# SPDX-License-Identifier: AGPL-3.0-only
"""Cross-revision comparisons and scoped feedback agree on every current transport."""
import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_practice_revisions import comparison_fixture

import pocket_music


def calls(tmp_path):
    _, spec = comparison_fixture(tmp_path)
    result = pocket_music.practice_compare_revisions(**spec)
    comparison = result['artifacts']['comparison']
    feedback = {'store_root': spec['store_root'], 'request_id': 'transport-feedback', 'comparison': comparison,
                'render': spec['variants'][0], 'interval_frames': [0, 8000], 'actor': 'Synthetic fixture',
                'actor_kind': 'agent', 'note': 'Interface proof only; no listening'}
    report = pocket_music.practice_feedback(**feedback)['artifacts']['feedback']
    return [('practice_compare_revisions', spec), ('practice_feedback', feedback),
            ('practice_feedback_query', {'store_root': spec['store_root'], 'feedback': [report], 'render': spec['variants'][0]}),
            ('practice_query', {'store_root': spec['store_root'], 'artifact': comparison})]


def test_cli_revision_comparison(tmp_path):
    for i, (name, spec) in enumerate(calls(tmp_path)):
        path = tmp_path / f'spec-{i}.json'
        path.write_text(json.dumps(spec))
        result = subprocess.run([sys.executable, '-m', 'pocket_music.cli', name.replace('_', '-'), '--spec', str(path)],
                                capture_output=True, text=True, timeout=30, check=False)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == getattr(pocket_music, name)(**spec)


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional agent extra')
def test_mcp_revision_comparison_closed_correspondence(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    specs = calls(tmp_path)
    async def run():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'],
            env={'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            schema = next(t.inputSchema for t in (await session.list_tools()).tools if t.name == 'practice_compare_revisions')
            assert schema['$defs']['OccurrencePair']['additionalProperties'] is False
            for name, spec in specs:
                result = await session.call_tool(name, spec)
                assert not result.isError, result.content
                assert json.loads(result.content[0].text) == getattr(pocket_music, name)(**spec)
            name, spec = specs[0]
            result = await session.call_tool(name, {**spec, 'request_id': 'missing-proof', 'edit_receipts': []})
            assert result.isError
    asyncio.run(asyncio.wait_for(run(), 60))
