# SPDX-License-Identifier: AGPL-3.0-only
"""Passage scores have identical public Python, CLI and real stdio MCP contracts."""
import asyncio
import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_audio_score import score_fixture
from test_capability_families_qa import catalog, direct_artifact_families
from test_musical_context import q

import pocket_music


def calls(tmp_path):
    args, definition, _ = score_fixture(tmp_path)
    score = pocket_music.audio_score_create(**args)['artifacts']['score']
    edit = pocket_music.context_edit(args['store_root'], 'interface-edit', args['context'], [
        {'kind': 'occurrence_shift_timeline', 'occurrence_ids': ['first', 'again'], 'delta_qn': q(1, 4)}
    ], [{'section': 'occurrences', 'object_id': 'backing',
         'fields': ['timeline_span_qn', 'source_span_frames']}], definition['attribution'])['artifacts']['edit']
    return [
        ('audio_score_create', args),
        ('audio_score_query', {'store_root': args['store_root'], 'score': score,
                               'section': 'members', 'passage_ids': ['linked-figure'], 'limit': 1}),
        ('audio_score_revise', {'store_root': args['store_root'], 'request_id': 'interface-revise',
                                'score': score, 'context_edit': edit}),
    ]


def test_cli_scores_match_direct_provider(tmp_path):
    for index, (name, spec) in enumerate(calls(tmp_path)):
        path = tmp_path / f'score-spec-{index}.json'
        path.write_text(json.dumps(spec))
        result = subprocess.run([sys.executable, '-m', 'pocket_music.cli', name.replace('_', '-'),
                                 '--spec', str(path)], capture_output=True, text=True, timeout=30, check=False)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == getattr(pocket_music, name)(**spec)


def test_discovery_reports_actual_families_and_authored_only_profile(tmp_path):
    rows = catalog()
    for name, spec in calls(tmp_path):
        result = getattr(pocket_music, name)(**spec)
        row = rows[name]
        assert direct_artifact_families(result) <= set(row['returned_artifact_schemas'])
        assert row['status'] == 'available'
        assert row['native_verified'] is False
        assert row['required_profile'] == 'authored-audio-passages/v1'
        assert 'pocket.audio-score/v1' in row['returned_artifact_schemas']
    assert 'pocket.context-edit/v1' in rows['audio_score_revise']['accepted_artifact_schemas']


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional agent extra')
def test_real_mcp_scores_match_and_reject_extra_nested_coordinates(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    requests = calls(tmp_path)

    async def run():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'],
            env={'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            schemas = {tool.name: tool.inputSchema for tool in (await session.list_tools()).tools}
            assert schemas['audio_score_create']['$defs']['AudioPassageMember']['additionalProperties'] is False
            assert schemas['audio_score_create']['$defs']['AudioScoreDefinition']['additionalProperties'] is False
            for name, spec in requests:
                result = await session.call_tool(name, spec)
                assert not result.isError, result.content
                assert json.loads(result.content[0].text) == getattr(pocket_music, name)(**spec)
            malformed = copy.deepcopy(requests[0][1])
            malformed['request_id'] = 'unexpected-clock'
            malformed['definition']['passages'][0]['members'][0]['timeline_span_qn'] = [q(0), q(2)]
            result = await session.call_tool('audio_score_create', malformed)
            assert result.isError
            query = {**requests[1][1], 'limit': True}
            assert (await session.call_tool('audio_score_query', query)).isError

    asyncio.run(asyncio.wait_for(run(), 60))
