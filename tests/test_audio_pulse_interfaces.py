"""Public learned-audio interfaces; mocked model evidence is never accuracy evidence."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from unittest.mock import patch

import pytest
from test_audio_models import PulseModelTests
from test_midi_interfaces import cli_call, environment

from pocket_music import audio_hypothesis_jobs as jobs
from pocket_music.audio_hypotheses import audio_hypothesis_query
from pocket_music.audio_pulse_hypotheses import SETTINGS, audio_model_inspect
from pocket_music.capabilities import capabilities_list


@pytest.fixture
def fixture():
    value = PulseModelTests()
    value.setUp()
    yield value
    value.doCleanups()


def test_cli_retained_model_query_and_missing_optional_runtime(fixture, tmp_path):
    with patch('pocket_music.audio_pulse_hypotheses._runner', side_effect=fixture.runner):
        handle = fixture.call()['artifacts']['hypotheses']
    args = {'store_root': fixture.store, 'hypotheses': handle, 'view': 'summary'}
    assert cli_call(tmp_path, 'audio_hypothesis_query', args) == audio_hypothesis_query(**args)
    inspect = {'store_root': fixture.store, 'request_id': 'base-runtime', 'declaration': fixture.declaration}
    expected = audio_model_inspect(**inspect)
    assert expected['status'] == 'unsupported' and not expected['artifacts']
    assert cli_call(tmp_path, 'audio_model_inspect', inspect) == expected
    rows = capabilities_list(domain='audio', limit=50)['capabilities']
    by_name = {row['public_tool']: row for row in rows}
    assert by_name['audio_pulse_hypotheses']['status'] == 'requires_optional_setup'
    assert by_name['audio_pulse_submit']['required_profile'] == 'beat_this_cpu_v1/synthetic_cpu_v1'
    assert 'pocket.audio-model-hypotheses/v1' in by_name['audio_hypothesis_query']['accepted_artifact_schemas']


def test_direct_and_owned_worker_use_same_primitive(fixture, monkeypatch):
    import os
    captured = {}
    def spawn(root, job_id, nonce, lease_fd):
        captured.update(root=root, job_id=job_id, nonce=nonce, lease_fd=os.dup(lease_fd))
    monkeypatch.setattr(jobs, '_spawn', spawn)
    arguments = {'store_root': fixture.store, 'request_id': 'owned', 'source': fixture.source,
                 'model': fixture.model, 'settings': SETTINGS, 'attribution': fixture.actor}
    with patch('pocket_music.audio_pulse_hypotheses._runner', side_effect=fixture.runner):
        expected = fixture.call('direct')
        submitted = jobs.audio_pulse_submit(**arguments)
        jobs._run_worker(**captured)
    observed = jobs.job_status(fixture.store, submitted['job']['job_id'])
    assert observed['state'] == 'completed'
    assert observed['result'] == expected['artifacts']['hypotheses']
    assert jobs.audio_pulse_submit(**arguments) == submitted
    assert len(json.dumps(observed).encode()) <= 16384


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional MCP environment')
def test_actual_stdio_model_schemas_query_and_closed_settings(fixture):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    with patch('pocket_music.audio_pulse_hypotheses._runner', side_effect=fixture.runner):
        handle = fixture.call()['artifacts']['hypotheses']
    query = {'store_root': fixture.store, 'hypotheses': handle, 'view': 'summary'}
    expected = audio_hypothesis_query(**query)
    async def check():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'], env=environment())
        async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            for name in ('audio_model_inspect', 'audio_pulse_hypotheses', 'audio_pulse_submit'):
                assert name in tools
            schema = json.dumps(tools['audio_pulse_submit'].inputSchema)
            assert 'beat_this_cpu_v1' in schema and 'inspected' in schema and 'additionalProperties' in schema
            response = await session.call_tool('audio_hypothesis_query', query)
            assert not response.isError and json.loads(response.content[0].text) == expected
            for settings in ({**SETTINGS, 'seed': True}, {**SETTINGS, 'unknown': 1}):
                response = await session.call_tool('audio_pulse_submit', {
                    'store_root': fixture.store, 'request_id': 'invalid', 'source': fixture.source,
                    'model': fixture.model, 'settings': settings, 'attribution': fixture.actor})
                assert response.isError
    asyncio.run(check())
