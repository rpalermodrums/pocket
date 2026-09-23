"""Literal edit contracts agree through the public library, CLI and real MCP."""
import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_context_edits import slip
from test_musical_context import fixture

import pocket_music


def specs(tmp_path):
    store, context, definition, _, _ = fixture(tmp_path)
    spec = {'store_root': store, 'request_id': 'transport-edit', 'context': context,
            'operations': [slip(['first', 'again'])], 'locks': [{'section': 'occurrences', 'object_id': 'first',
            'fields': ['timeline_span_qn']}], 'attribution': definition['attribution']}
    edit = pocket_music.context_edit(**spec)['artifacts']['edit']
    return [('context_edit', spec), ('context_edit_query', {'store_root': store, 'edit': edit})]


def test_cli_context_edit_and_readback(tmp_path):
    for i, (name, spec) in enumerate(specs(tmp_path)):
        path = tmp_path / f'spec-{i}.json'
        path.write_text(json.dumps(spec))
        result = subprocess.run([sys.executable, '-m', 'pocket_music.cli', name.replace('_', '-'), '--spec', str(path)],
                                capture_output=True, text=True, timeout=30, check=False)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == getattr(pocket_music, name)(**spec)


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional agent extra')
def test_mcp_literal_edits_closed_contracts_and_lock_refusal(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    calls = specs(tmp_path)
    async def run():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'],
            env={'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            schema = next(t.inputSchema for t in (await session.list_tools()).tools if t.name == 'context_edit')
            assert schema['$defs']['OccurrenceSlip']['additionalProperties'] is False
            for name, spec in calls:
                result = await session.call_tool(name, spec)
                assert not result.isError, result.content
                assert json.loads(result.content[0].text) == getattr(pocket_music, name)(**spec)
            spec = {**calls[0][1], 'request_id': 'locked', 'locks': [{'section': 'occurrences', 'object_id': 'first',
                                                                  'fields': ['source_span_frames']}]}
            result = await session.call_tool('context_edit', spec)
            assert result.isError and 'locked field' in str(result.content)
    asyncio.run(asyncio.wait_for(run(), 60))
