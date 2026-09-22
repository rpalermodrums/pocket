"""Independent real worker completion and public CLI/stdio cancellation boundaries."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_audio_hypotheses_qa import arguments
from test_midi_interfaces import cli_call, environment

from pocket_music.artifact_store import canonical_bytes, read_bytes, read_record
from pocket_music.audio_hypotheses import audio_hypotheses, audio_hypothesis_query
from pocket_music.audio_hypothesis_jobs import audio_hypothesis_submit, job_cancel, job_status
from pocket_music.capabilities import capabilities_list
from pocket_music.errors import PocketError


def terminal(store, job):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            state = job_status(store, job)
        except PocketError as error:
            if 'busy' not in str(error):
                raise
            time.sleep(.025)
            continue
        assert len(canonical_bytes(state)) < 16384
        if state['state'] in ('completed', 'cancelled', 'failed', 'interrupted'):
            return state
        assert state['result'] is None
        time.sleep(.025)
    pytest.fail('Owned test worker did not reach a terminal state within 20 seconds')


def test_jobs_direct_and_cli_real_completion_exact_primitive_equivalence(tmp_path):
    args = arguments(tmp_path)
    direct = audio_hypotheses(**{**args, 'request_id': 'direct-primitive'})
    submissions = [audio_hypothesis_submit(**{**args, 'request_id': 'provider-submit'}),
                   cli_call(tmp_path, 'audio_hypothesis_submit', {**args, 'request_id': 'cli-submit'})]
    for submitted in submissions:
        assert submitted['state'] == 'queued' and submitted['result'] is None
        assert submitted['coverage']['observation'].startswith('submission_snapshot')
        state = terminal(args['store_root'], submitted['job']['job_id'])
        assert state['state'] == 'completed'
        assert state['result'] == direct['artifacts']['hypotheses']
        status_args = {'store_root': args['store_root'], 'job_id': submitted['job']['job_id']}
        assert cli_call(tmp_path, 'job_status', status_args) == state
        late = {**status_args, 'expected_revision': state['revision']}
        assert cli_call(tmp_path, 'job_cancel', late) == job_cancel(**late)
        assert job_cancel(**late)['status'] == 'conflict'
        assert audio_hypothesis_query(store_root=args['store_root'], hypotheses=state['result'])['status'] == 'ok'
        record = read_record(state['result'], args['store_root'])
        assert read_bytes(record['original'], args['store_root']) == Path(args['source']['path']).read_bytes()
    assert cli_call(tmp_path, 'audio_hypothesis_submit', {**args, 'request_id': 'provider-submit'}) == submissions[0]
    with pytest.raises(PocketError, match='idempotency_conflict'):
        audio_hypothesis_submit(**{**args, 'request_id': 'provider-submit', 'settings': {**args['settings'], 'bpm_hint': 123}})


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional MCP extra not installed')
def test_jobs_stdio_real_submit_completion_schemas_and_malformed_inputs(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    args = arguments(tmp_path)
    expected = audio_hypotheses(**{**args, 'request_id': 'reference'})['artifacts']['hypotheses']

    async def exchange():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'], env=environment())
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listed = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert {'audio_hypothesis_submit', 'job_status', 'job_cancel'} <= set(listed)
            schema = listed['audio_hypothesis_submit'].inputSchema
            assert schema['additionalProperties'] is False and set(schema['required']) == set(args)
            for nested in schema['$defs'].values():
                if nested.get('type') == 'object':
                    assert nested['additionalProperties'] is False
            response = await session.call_tool('audio_hypothesis_submit', args)
            assert not response.isError
            submitted = json.loads(response.content[0].text)
            assert submitted == audio_hypothesis_submit(**args)
            state = await asyncio.to_thread(terminal, args['store_root'], submitted['job']['job_id'])
            assert state['state'] == 'completed' and state['result'] == expected
            status_args = {'store_root': args['store_root'], 'job_id': submitted['job']['job_id']}
            response = await session.call_tool('job_status', status_args)
            assert not response.isError and json.loads(response.content[0].text) == state
            cancellation = {**status_args, 'expected_revision': submitted['revision']}
            response = await session.call_tool('job_cancel', cancellation)
            assert not response.isError
            assert json.loads(response.content[0].text) == job_cancel(**cancellation)
            assert json.loads(response.content[0].text)['status'] == 'conflict'
            for bad in [{**args, 'source': {**args['source'], 'start_frame': True}},
                        {**args, 'settings': {**args['settings'], 'extra': 1}},
                        {**args, 'extra': 1}]:
                assert (await session.call_tool('audio_hypothesis_submit', bad)).isError
                assert cli_call(tmp_path, 'audio_hypothesis_submit', bad, success=False)['error'] == ('TypeError' if 'extra' in bad else 'PocketError')
            for name, bad in [('job_status', {**status_args, 'job_id': '../elsewhere'}),
                              ('job_cancel', {**cancellation, 'expected_revision': True})]:
                assert (await session.call_tool(name, bad)).isError
                assert cli_call(tmp_path, name, bad, success=False)['error'] == 'PocketError'
    asyncio.run(exchange())


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None or os.name != 'posix', reason='Requires optional MCP and POSIX workers')
def test_jobs_cli_stdio_cancel_owned_startup_barrier_without_publish(tmp_path, monkeypatch):
    """Gate only owned subprocess startup; production worker executes unchanged."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    module = importlib.import_module('pocket_music.audio_hypothesis_jobs')
    barrier = tmp_path / 'release-owned-test-worker'
    processes = []

    def held_spawn(root, job_id, nonce, lease_fd):
        script = ('import pathlib,runpy,sys,time; '
                  'barrier=pathlib.Path(sys.argv.pop(1)); deadline=time.monotonic()+20\n'
                  'while not barrier.exists() and time.monotonic()<deadline: time.sleep(.01)\n'
                  'runpy.run_module("pocket_music.audio_hypothesis_jobs",run_name="__main__")')
        process = subprocess.Popen([sys.executable, '-c', script, str(barrier), str(root), job_id, nonce, str(lease_fd)],
                                    pass_fds=(lease_fd,), env=environment(), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(process)
    monkeypatch.setattr(module, '_spawn', held_spawn)
    args = arguments(tmp_path)
    submitted = audio_hypothesis_submit(**args)
    status_args = {'store_root': args['store_root'], 'job_id': submitted['job']['job_id']}
    try:
        stale = cli_call(tmp_path, 'job_cancel', {**status_args, 'expected_revision': '0' * 64})
        assert stale['status'] == 'conflict' and stale['state'] == 'queued' and stale['result'] is None
        pending = cli_call(tmp_path, 'job_cancel', {**status_args, 'expected_revision': submitted['revision']})
        assert pending['state'] == 'cancel_requested' and pending['result'] is None

        async def exchange():
            params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'], env=environment())
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                response = await session.call_tool('job_status', status_args)
                assert not response.isError and json.loads(response.content[0].text) == pending
                response = await session.call_tool('job_cancel', {**status_args, 'expected_revision': pending['revision']})
                assert not response.isError and json.loads(response.content[0].text) == pending
                barrier.touch()
                state = await asyncio.to_thread(terminal, args['store_root'], submitted['job']['job_id'])
                assert state['state'] == 'cancelled' and state['result'] is None
                response = await session.call_tool('job_status', status_args)
                assert not response.isError and json.loads(response.content[0].text) == state
                assert state == cli_call(tmp_path, 'job_status', status_args)
                assert len(canonical_bytes(state)) < 16384
        asyncio.run(exchange())
        assert not list((Path(args['store_root']) / 'artifacts').glob('*/record.json'))
    finally:
        barrier.touch()
        for process in processes:
            assert process.wait(timeout=25) == 0


def test_jobs_discovery_exposes_real_dependencies_and_no_native(tmp_path):
    rows, cursor = [], None
    while True:
        reply = capabilities_list(limit=50, cursor=cursor)
        rows.extend(reply['capabilities'])
        cursor = reply['next_cursor']
        if cursor is None:
            break
    jobs = {row['public_tool']: row for row in rows if row['public_tool'] in ('audio_hypothesis_submit', 'job_status', 'job_cancel')}
    assert len(jobs) == 3
    for row in jobs.values():
        assert row['status'] == 'available'
        assert row['native_verified'] is False and row['required_profile'] is None
        assert any('POSIX' in requirement and 'no model or DAW' in requirement for requirement in row['prerequisites'])
        assert 'local_job_journal' in row['side_effects']
