# SPDX-License-Identifier: AGPL-3.0-only
"""Source-onset correction stays identical through public transports and discovery."""
import asyncio
import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_audio_score_practice import q, score_fixture
from test_capability_families_qa import catalog, direct_artifact_families

import pocket_music


def calls(tmp_path):
    f = score_fixture(tmp_path)
    corrected = pocket_music.audio_score_audition(**f['audition_args'])['artifacts']
    preview_args = {'store_root': f['store'], 'request_id': 'interface-preview',
                    'render': corrected['variant'], 'profile': 'browser-pcm16-original-rate/v1'}
    preview = pocket_music.practice_preview(**preview_args)['artifacts']['preview']
    feedback_args = {'store_root': f['store'], 'request_id': 'interface-feedback',
                     'comparison': corrected['comparison'], 'render': corrected['variant'],
                     'interval_frames': [96000, 192000], 'actor': 'generated test', 'actor_kind': 'agent',
                     'note': 'Numerical interface test; no listening.', 'decision': 'keep', 'preview': preview}
    feedback = pocket_music.practice_feedback(**feedback_args)['artifacts']['feedback']
    return [
        ('audio_score_render', f['render_args']),
        ('audio_score_audition', f['audition_args']),
        ('practice_query', {'store_root': f['store'], 'artifact': corrected['comparison']}),
        ('practice_preview', preview_args),
        ('practice_feedback', feedback_args),
        ('practice_feedback_query', {'store_root': f['store'], 'feedback': [feedback],
                                     'render': corrected['variant'], 'decision': 'keep'}),
    ]


def test_cli_score_correction_preview_and_feedback_match_direct_calls(tmp_path):
    for index, (name, spec) in enumerate(calls(tmp_path)):
        path = tmp_path / f'audition-spec-{index}.json'
        path.write_text(json.dumps(spec))
        result = subprocess.run([sys.executable, '-m', 'pocket_music.cli', name.replace('_', '-'),
                                 '--spec', str(path)], capture_output=True, text=True, timeout=60, check=False)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == getattr(pocket_music, name)(**spec)


def test_discovery_covers_score_render_comparison_preview_and_feedback_handles(tmp_path):
    rows = catalog()
    for name, spec in calls(tmp_path):
        assert direct_artifact_families(getattr(pocket_music, name)(**spec)) <= set(rows[name]['returned_artifact_schemas'])
    for name in ('audio_score_render', 'audio_score_audition'):
        assert rows[name]['required_profile'] == 'same-rate-overlay-linear/v1'
        assert rows[name]['native_verified'] is False and rows[name]['status'] == 'available'
    for name in ('practice_preview', 'practice_feedback', 'practice_query', 'practice_feedback_query'):
        assert 'pocket.audio-score-render/v1' in rows[name]['accepted_artifact_schemas']
    assert 'pocket.audio-score-comparison/v1' in rows['practice_query']['accepted_artifact_schemas']


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional agent extra')
def test_real_mcp_correction_and_feedback_match_with_closed_recipe_and_locks(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    requests = calls(tmp_path)

    async def run():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'],
            env={'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            schema = next(t.inputSchema for t in (await session.list_tools()).tools if t.name == 'audio_score_audition')
            for type_name in ('AudioScoreRecipe', 'AudioScoreOccurrenceControl', 'AudioScoreAmplitudePoint', 'ContextLock'):
                assert schema['$defs'][type_name]['additionalProperties'] is False
            for name, spec in requests:
                result = await session.call_tool(name, spec)
                assert not result.isError, result.content
                assert json.loads(result.content[0].text) == getattr(pocket_music, name)(**spec)
            invalid = copy.deepcopy(requests[1][1])
            invalid['request_id'] = 'invalid-correction'
            invalid['delta_qn'] = {'n': True, 'd': 4}
            assert (await session.call_tool('audio_score_audition', invalid)).isError
            invalid['delta_qn'] = q(1, 4)
            invalid['recipe']['controls'][0]['unrequested_effect'] = 'delay'
            assert (await session.call_tool('audio_score_audition', invalid)).isError
            del invalid['recipe']['controls'][0]['unrequested_effect']
            invalid['locks'].append({'section': 'occurrences', 'object_id': 'call', 'fields': ['timeline_span_qn']})
            refused = await session.call_tool('audio_score_audition', invalid)
            assert refused.isError and 'locked' in str(refused.content)

    asyncio.run(asyncio.wait_for(run(), 120))
