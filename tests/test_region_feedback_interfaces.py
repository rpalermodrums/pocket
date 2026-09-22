"""Actual public transport parity for synthetic region and attributed feedback evidence."""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import struct
import sys
import time
import wave

import pytest
from test_auditions import attachment_args
from test_midi_interfaces import cli_call, environment

from pocket_music.audio_hypothesis_jobs import audio_region_submit, job_status
from pocket_music.audio_region_analysis import audio_region_hypotheses, audio_region_query
from pocket_music.audio_region_corrections import audio_region_correct
from pocket_music.audio_regions import audio_region_capture
from pocket_music.auditions import attach_candidate_render, audition_feedback
from pocket_music.errors import PocketError
from pocket_music.feedback_query import audition_feedback_query


def region_args(tmp_path):
    path = tmp_path / 'source.wav'
    with wave.open(str(path), 'wb') as output:
        output.setparams((2, 2, 8000, 20000, 'NONE', 'not compressed'))
        output.writeframes(b''.join(struct.pack('<hh', i % 1000, -(i % 1000)) for i in range(20000)))
    return {'store_root': str(tmp_path / 'region-store'), 'request_id': 'capture',
            'source': {'path': str(path), 'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                       'start_frame': 400, 'frames': 16000, 'source_origin': 'independently_acquired'}}


def feedback_args(tmp_path):
    args, _ = attachment_args(tmp_path)
    attachment = attach_candidate_render(**args)['artifacts']['attachment']
    handle = audition_feedback(args['store_root'], 'technical-feedback', attachment, [100, 300],
                               'Synthetic reviewer', 'agent', 'Technical fixture, no listening.',
                               'no_addition')['artifacts']['feedback']
    return {'store_root': args['store_root'], 'feedback': [handle], 'actor_kind': 'agent',
            'render_sha256': args['expected_sha256'], 'interval_frames': [200, 400]}


def region_analysis_args(capture):
    return {'store_root': capture['store_root'], 'request_id': 'analysis',
            'region': {'kind': 'inline', 'source': capture['source']},
            'analysis': {'kind': 'peek', 'settings': {'bpm_hint': None, 'beats_per_bar': 4}},
            'attribution': {'actor': 'Synthetic interface test', 'actor_kind': 'agent',
                            'statement': 'Test exact passage coordinates.', 'uncertainty': []}}


def correction_args(analysis, analyzed):
    handle = analyzed['artifacts']['hypotheses']
    return {'store_root': analysis['store_root'], 'request_id': 'correct', 'parent': handle,
            'expected_revision': handle['sha256'], 'attribution': analysis['attribution'],
            'batch': {'coordinate_space': 'original_source_frame', 'corrections': [{
                'correction_id': 'explicit-span', 'supersedes': [],
                'annotation': {'kind': 'phrase_anchor', 'start_frame': 400,
                               'end_frame_exclusive': 16400, 'label': 'Declared interval'},
                'support': [{'kind': 'analysis_pointer', 'reference': '/analysis/region/start_frame'}],
                'uncertainty': []}]}}


def completed(store, submitted):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            state = job_status(store, submitted['job']['job_id'])
        except PocketError as error:
            if str(error) != 'Job state is busy; refresh status before retry':
                raise
            time.sleep(.02)
            continue
        if state['state'] in {'completed', 'cancelled', 'failed', 'interrupted'}:
            assert state['state'] == 'completed', state
            return state
        time.sleep(.02)
    raise AssertionError('Region worker failed to finish its synthetic input within 30 seconds')


def test_cli_provider_region_and_feedback_parity(tmp_path):
    capture = region_args(tmp_path)
    expected = audio_region_capture(**capture)
    assert cli_call(tmp_path, 'audio_region_capture', capture) == expected
    analysis = region_analysis_args(capture)
    analyzed = audio_region_hypotheses(**analysis)
    assert cli_call(tmp_path, 'audio_region_hypotheses', analysis) == analyzed
    composed = {**analysis, 'request_id': 'composed',
                'region': {'kind': 'captured', 'region': expected['artifacts']['region']}}
    assert cli_call(tmp_path, 'audio_region_hypotheses', composed)['artifacts'] == analyzed['artifacts']
    page = {'store_root': capture['store_root'], 'hypotheses': analyzed['artifacts']['hypotheses'],
            'view': 'annotations', 'limit': 1, 'max_bytes': 4096}
    assert cli_call(tmp_path, 'audio_region_query', page) == audio_region_query(**page)
    correction = correction_args(analysis, analyzed)
    assert cli_call(tmp_path, 'audio_region_correct', correction) == audio_region_correct(**correction)
    submitted = audio_region_submit(**{**analysis, 'request_id': 'provider-worker'})
    cli_submitted = cli_call(tmp_path, 'audio_region_submit', {**analysis, 'request_id': 'cli-worker'})
    for job in (submitted, cli_submitted):
        assert completed(capture['store_root'], job)['result'] == analyzed['artifacts']['hypotheses']
    query = feedback_args(tmp_path)
    expected = audition_feedback_query(**query)
    assert cli_call(tmp_path, 'audition_feedback_query', query) == expected
    assert expected['items'][0]['evidence_kind'] == 'agent_report'


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional MCP environment')
def test_stdio_strict_region_and_feedback_parity(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    capture = region_args(tmp_path)
    query = feedback_args(tmp_path)
    analysis = region_analysis_args(capture)
    analyzed = audio_region_hypotheses(**analysis)
    page = {'store_root': capture['store_root'], 'hypotheses': analyzed['artifacts']['hypotheses']}
    correction = correction_args(analysis, analyzed)
    cases = [('audio_region_capture', capture, audio_region_capture(**capture)),
             ('audio_region_hypotheses', analysis, analyzed),
             ('audio_region_query', page, audio_region_query(**page)),
             ('audio_region_correct', correction, audio_region_correct(**correction)),
             ('audition_feedback_query', query, audition_feedback_query(**query))]
    async def check():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'], env=environment())
        async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
            await session.initialize()
            available = {item.name: item for item in (await session.list_tools()).tools}
            for name, arguments, expected in cases:
                assert name in available
                result = await session.call_tool(name, arguments)
                assert not result.isError and json.loads(result.content[0].text) == expected
            for source in ({**capture['source'], 'frames': True}, {**capture['source'], 'hidden': 1}):
                result = await session.call_tool('audio_region_capture', {**capture, 'request_id': 'bad', 'source': source})
                assert result.isError
            result = await session.call_tool('audio_region_correct', {**correction,
                'batch': {**correction['batch'], 'coordinate_space': 'host_beats'}})
            assert result.isError
            result = await session.call_tool('audio_region_submit', {**analysis, 'request_id': 'mcp-worker'})
            assert not result.isError
            submitted = json.loads(result.content[0].text)
            final = await asyncio.to_thread(completed, capture['store_root'], submitted)
            assert final['result'] == analyzed['artifacts']['hypotheses']
            result = await session.call_tool('job_status', {'store_root': capture['store_root'],
                                                          'job_id': submitted['job']['job_id']})
            assert not result.isError and json.loads(result.content[0].text) == final
            result = await session.call_tool('audition_feedback_query', {**query, 'actor_kind': 'algorithm'})
            assert result.isError
            for bad in ({'kind': 'peek', 'settings': analysis['analysis']['settings'], 'model': {}},
                        {'kind': 'hidden', 'settings': analysis['analysis']['settings']}):
                result = await session.call_tool('audio_region_hypotheses', {**analysis, 'analysis': bad})
                assert result.isError
    asyncio.run(check())
