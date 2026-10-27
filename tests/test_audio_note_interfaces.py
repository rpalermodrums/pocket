# SPDX-License-Identifier: AGPL-3.0-only
"""Actual transports over synthetic retained proofs; real ONNX gates are separate."""
# Imported fixture names intentionally recur as pytest parameters.
# ruff: noqa: F811
import asyncio
import copy
import importlib.util
import json
import subprocess
import sys

import pytest
from test_audio_note_hypotheses import args, note_fixture  # noqa: F401
from test_capability_families_qa import catalog, direct_artifact_families
from test_midi_interfaces import environment

from pocket_music import audio_note_hypotheses as notes
from pocket_music.audio_hypotheses import audio_hypothesis_correct, audio_hypothesis_query


def bootstrap(entry):
    # Allow only this deliberately fake fixture's weight identity in the child.
    # No runner/decode/correction/registry/transport implementation is patched.
    return (f'import pocket_music.audio_note_hypotheses as n; n.KNOWN_WEIGHT={notes.KNOWN_WEIGHT!r}; '
            f'from pocket_music.{entry} import main; main()')


def cli(tmp_path, name, spec):
    path = tmp_path / f'{name}.json'
    path.write_text(json.dumps(spec))
    completed = subprocess.run([sys.executable, '-c', bootstrap('cli'), name.replace('_', '-'), '--spec', str(path)],
                               env=environment(), text=True, capture_output=True, timeout=45, check=False)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def correction(fixture, parent, request):
    return {'store_root': fixture['store_root'], 'request_id': request, 'parent': parent,
            'expected_revision': parent['sha256'], 'attribution': fixture['attribution'],
            'corrections': [{'correction_id': 'authored-pitch', 'supersedes': [],
                            'annotation': {'kind': 'note_hypothesis', 'start_frame': 10000,
                                           'end_frame_exclusive': 11000, 'midi_note': 48,
                                           'cents': 0, 'tuning_ref': 'explicit-test-12tet-a440'},
                            'support': [{'kind': 'analysis_pointer', 'reference': '/analysis/projection/events/0'}],
                            'uncertainty': ['Synthetic authored alternative, not listening approval.']}]}


def test_note_retained_query_correction_actual_cli(note_fixture, tmp_path):
    f = note_fixture
    parent = notes.audio_note_hypotheses(request_id='direct', **args(f))['artifacts']['hypotheses']
    direct = audio_hypothesis_correct(**correction(f, parent, 'direct-correction'))
    actual = cli(tmp_path, 'audio_hypothesis_correct', correction(f, parent, 'cli-correction'))
    assert actual['artifacts'] == direct['artifacts']
    for view in ('summary', 'annotations', 'history'):
        spec = {'store_root': f['store_root'], 'hypotheses': actual['artifacts']['hypotheses'], 'view': view}
        queried = audio_hypothesis_query(**spec)
        assert cli(tmp_path, 'audio_hypothesis_query', spec) == queried
        assert direct_artifact_families(queried) <= set(catalog()['audio_hypothesis_query']['returned_artifact_schemas'])


def test_note_discovery_dependencies_and_distinct_typed_families():
    rows = catalog()
    for name in ('audio_note_model_inspect', 'audio_note_hypotheses', 'audio_note_submit'):
        assert rows[name]['status'] == 'requires_optional_setup'
        assert rows[name]['native_verified'] is False
        assert rows[name]['required_profile'] == 'basic_pitch_onnx_cpu_v1/synthetic_onnx_cpu_v1'
    assert rows['audio_note_model_inspect']['returned_artifact_schemas'] == ['pocket.audio-note-model/v1']
    assert rows['audio_note_hypotheses']['accepted_artifact_schemas'] == ['pocket.audio-note-model/v1']
    assert 'pocket.audio-note-model/v1' in rows['audio_region_hypotheses']['accepted_artifact_schemas']
    assert 'pocket.audio-note-hypotheses/v1' in rows['job_status']['returned_artifact_schemas']
    assert rows['audio_transcribe']['public_tool'] is None


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional MCP transport')
def test_note_actual_stdio_retained_correction_and_strict_inputs(note_fixture):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    f = note_fixture
    parent = notes.audio_note_hypotheses(request_id='direct', **args(f))['artifacts']['hypotheses']
    expected = audio_hypothesis_correct(**correction(f, parent, 'direct-correction'))
    async def run():
        params = StdioServerParameters(command=sys.executable, args=['-c', bootstrap('mcp_server')], env=environment())
        async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
            await session.initialize()
            listed = {tool.name: tool for tool in (await session.list_tools()).tools}
            for name in ('audio_note_model_inspect', 'audio_note_hypotheses', 'audio_note_submit'):
                assert name in listed
            result = await session.call_tool('audio_hypothesis_correct', correction(f, parent, 'mcp-correction'))
            assert not result.isError, result.content
            actual = json.loads(result.content[0].text)
            assert actual['artifacts'] == expected['artifacts']
            spec = {'store_root': f['store_root'], 'hypotheses': actual['artifacts']['hypotheses'], 'view': 'annotations'}
            response = await session.call_tool('audio_hypothesis_query', spec)
            assert not response.isError, response.content
            assert json.loads(response.content[0].text) == audio_hypothesis_query(**spec)
            bad = copy.deepcopy(args(f)); bad['request_id'] = 'bad'; bad['settings']['threads'] = True
            response = await session.call_tool('audio_note_hypotheses', bad)
            assert response.isError
            bad['settings'] = copy.deepcopy(f['settings']); bad['model']['declaration']['adapter'] = 'beat_this_cpu_v1'
            response = await session.call_tool('audio_note_hypotheses', bad)
            assert response.isError
    asyncio.run(asyncio.wait_for(run(), timeout=60))
