"""Real transports share interpretation providers, typed contracts and failures."""
import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_interpretations import evidence_fixture
from test_musical_context import q

import pocket_music


def flow(f):
    store, context, _, author, evidence, _, _ = f
    spec = {'store_root': store, 'request_id': 'transport-choice', 'context': context,
            'source_clock_id': 'recording', 'claim': {'kind': 'onset', 'status': 'selected', 'source_frame_q': q(3700)},
            'attribution': author, 'evidence': evidence}
    chosen = pocket_music.interpretation_create(**spec)['artifacts']['interpretation']
    return [('interpretation_create', spec), ('interpretation_query', {'store_root': store, 'interpretation': chosen}),
            ('context_bind_interpretation', {'store_root': store, 'request_id': 'transport-bind', 'context': context,
             'interpretation': chosen, 'binding': {'kind': 'anchor', 'binding_id': 'pick', 'anchor_id': 'picked',
             'label': 'Explicit attack'}, 'attribution': author})]


def test_cli_choice_binding_and_exact_errors(tmp_path):
    specs = flow(evidence_fixture(tmp_path))
    for i, (name, spec) in enumerate(specs):
        path = tmp_path / f'spec-{i}.json'
        path.write_text(json.dumps(spec))
        result = subprocess.run([sys.executable, '-m', 'pocket_music.cli', name.replace('_', '-'), '--spec', str(path)],
                                capture_output=True, text=True, timeout=30, check=False)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == getattr(pocket_music, name)(**spec)
    name, spec = specs[0]
    bad = {**spec, 'request_id': 'wrong-candidate', 'evidence': {**spec['evidence'], 'expected_revision': '0'*64}}
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(bad))
    result = subprocess.run([sys.executable, '-m', 'pocket_music.cli', 'interpretation-create', '--spec', str(path)],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 2 and 'Stale' in json.loads(result.stderr)['message']


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional agent extra')
def test_mcp_choice_binding_and_closed_types(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    specs = flow(evidence_fixture(tmp_path))
    async def run():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'],
            env={'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            schemas = {t.name: t.inputSchema for t in (await session.list_tools()).tools}
            assert schemas['interpretation_create']['$defs']['PointClaim']['additionalProperties'] is False
            for name, spec in specs:
                result = await session.call_tool(name, spec)
                assert not result.isError, result.content
                assert json.loads(result.content[0].text) == getattr(pocket_music, name)(**spec)
            name, spec = specs[0]
            invalid = await session.call_tool(name, {**spec, 'claim': {**spec['claim'], 'pretend_beat_one': True}})
            assert invalid.isError
    asyncio.run(asyncio.wait_for(run(), 60))
