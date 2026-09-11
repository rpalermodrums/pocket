"""Canonical Peek/Thread/Stitch names preserve legacy CLI and MCP behavior."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pocket_music.cli import parser

CLI_PAIRS = [
    ('peek', 'track-map', ['source.wav', '--output', 'result.json']),
    ('thread', 'set-map', ['source.als', '--full', '--output', 'result.json']),
    ('thread-region', 'set-region', ['handle.json', '1:02', '--output', 'result.json']),
    ('thread-find-clips', 'find-clips', ['handle.json', 'clip', '--output', 'result.json']),
    ('thread-export', 'map-export', ['handle.json', '--output', 'result.json']),
    ('thread-source-position', 'source-position', ['map.json', 'track:1/clip:2', '4', '--output', 'result.json']),
    ('stitch', 'lab', ['create', '--spec', 'spec.json', '--output', 'new-trial']),
]
MCP_PAIRS = {
    'peek': 'analyze_region', 'thread': 'inspect_set', 'thread_region': 'query_set_region',
    'thread_find_clips': 'find_clips', 'thread_export': 'export_set_map',
    'thread_source_position': 'source_position', 'thread_arrangement_position': 'arrangement_position',
    'stitch': 'create_trial', 'stitch_prepare_native': 'prepare_native_trial',
    'stitch_feedback': 'record_feedback', 'stitch_feedback_list': 'query_feedback',
    'stitch_attach_render': 'attach_completed_render', 'stitch_validate_native': 'validate_native_trial',
}


def _cli(*args):
    return subprocess.run([sys.executable, '-m', 'pocket_music.cli', *map(str, args)],
                          env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')},
                          capture_output=True, text=True, timeout=45, check=False)


@pytest.mark.parametrize('primary,old,args', CLI_PAIRS)
def test_cli_aliases_normalize_before_output_dispatch(primary, old, args):
    assert vars(parser().parse_args([primary, *args])) == vars(parser().parse_args([old, *args]))
    assert parser().parse_args([old, *args]).command == primary
    help_text = parser().format_help()
    assert f'{primary} ({old})' in help_text and f'compatibility alias: {old}' in ' '.join(help_text.split())


def test_pipette_is_not_a_command(capsys):
    with pytest.raises(SystemExit) as caught:
        parser().parse_args(['pipette'])
    assert caught.value.code == 2
    assert "invalid choice: 'pipette'" in capsys.readouterr().err


def test_legacy_read_output_and_export_write_the_same_artifact_kinds(tmp_path):
    from test_stitch import als_fixture

    source_set, _ = als_fixture(tmp_path)
    overview = tmp_path / 'overview.json'
    result = _cli('set-map', source_set, '--cache-dir', tmp_path / 'cache', '--output', overview)
    assert result.returncode == 0 and result.stdout == '', result.stderr
    summary = json.loads(overview.read_text())
    assert summary['schema'] == 'pocket.set-summary/v1'
    for primary, old, tail in [
        ('thread-region', 'set-region', [overview, '0:00', '--duration', '3']),
        ('thread-find-clips', 'find-clips', [overview, 'track:100/clip:0']),
    ]:
        destination = tmp_path / (old + '.json')
        expected = _cli(primary, *tail)
        actual = _cli(old, *tail, '--output', destination)
        assert expected.returncode == actual.returncode == 0
        assert actual.stdout == '' and json.loads(destination.read_text()) == json.loads(expected.stdout)
        original = destination.read_bytes()
        rejected = _cli(old, *tail, '--output', destination)
        assert rejected.returncode == 2 and destination.read_bytes() == original
    exported = tmp_path / 'raw.json'
    receipt = _cli('map-export', overview, '--output', exported)
    assert receipt.returncode == 0, receipt.stderr
    assert json.loads(receipt.stdout)['path'] == str(exported)
    raw = json.loads(exported.read_text())
    assert raw['schema'] == 'pocket.set-map/v1' and 'clips' in raw
    before = exported.read_bytes()
    assert _cli('thread-export', overview, '--output', exported).returncode == 2
    assert exported.read_bytes() == before
    assert json.loads(_cli('source-position', exported, 'track:100/clip:0', 2).stdout) == json.loads(
        _cli('thread-source-position', exported, 'track:100/clip:0', 2).stdout)
    source = tmp_path / 'source.wav'
    output = tmp_path / 'peek.json'
    old = _cli('track-map', source, '--start-frame', 3, '--frames', 8000, '--output', output)
    canonical = _cli('peek', source, '--start-frame', 3, '--frames', 8000)
    assert old.returncode == canonical.returncode == 0 and old.stdout == ''
    assert json.loads(output.read_text()) == json.loads(canonical.stdout)
    for command in ('peek', 'track-map'):
        error = _cli(command, source, '--duration', -1)
        assert error.returncode == 2
        assert json.loads(error.stderr)['error'] == 'PocketError'


def test_lab_alias_keeps_output_as_trial_directory_and_stitch_rejects_overwrite(tmp_path):
    from test_stitch import audio

    source, _ = audio(tmp_path)
    spec = tmp_path / 'spec.json'
    spec.write_text(json.dumps({'variants': [{'source_path': str(source), 'label': 'Generated'}],
                                'start_frame': 0, 'frames': 1000}))
    destination = tmp_path / 'trial'
    made = _cli('lab', 'create', '--spec', spec, '--output', destination)
    assert made.returncode == 0, made.stderr
    assert destination.is_dir()
    result = json.loads(made.stdout)
    assert result['trial_dir'] == str(destination)
    assert (destination / 'v01.wav').is_file()
    before = (destination / 'trial.json').read_bytes()
    rejected = _cli('stitch', 'create', '--spec', spec, '--output', destination)
    assert rejected.returncode == 2 and (destination / 'trial.json').read_bytes() == before
    duplicate = tmp_path / 'duplicate.json'
    duplicate.write_text(json.dumps({**json.loads(spec.read_text()), 'output_dir': 'forbidden'}))
    for command in ('stitch', 'lab'):
        bad = _cli(command, 'create', '--spec', duplicate, '--output', tmp_path / command)
        assert bad.returncode == 2 and 'Use --output' in json.loads(bad.stderr)['message']
        assert not (tmp_path / command).exists()


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional agent extra')
def test_all_mcp_aliases_share_callable_schema_annotations_and_transport():
    from pocket_music.mcp_server import build_server

    async def check():
        server = build_server()
        listed = await server.list_tools()
        tools = {tool.name: tool for tool in listed}
        assert len(tools) == 48
        assert not any('pipette' in name.casefold() for name in tools)
        names = [tool.name for tool in listed]
        assert min(names.index(old) for old in MCP_PAIRS.values()) >= 35
        for primary, old in MCP_PAIRS.items():
            assert tools[primary].inputSchema == tools[old].inputSchema
            assert tools[primary].outputSchema == tools[old].outputSchema
            assert tools[primary].annotations == tools[old].annotations
            assert tools[old].description.startswith(f'Compatibility alias for {primary}.')
            # Covers write operations without writing a second artifact simply
            # to compare paths/timestamps: both registrations execute one object.
            new_callable = server._tool_manager.get_tool(primary).fn
            old_callable = server._tool_manager.get_tool(old).fn
            assert new_callable is old_callable
    asyncio.run(check())


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional agent extra')
def test_actual_mcp_alias_read_results_and_feedback_are_equal(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from test_stitch import als_fixture

    source_set, _ = als_fixture(tmp_path)
    async def check():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'],
            env={'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            async def call(name, args):
                result = await session.call_tool(name, args)
                assert not result.isError, result.content
                return result
            def parsed(result):
                return result.structuredContent or json.loads(next(b.text for b in result.content if b.type == 'text'))
            async def same(primary, args):
                a = await call(primary, args)
                b = await call(MCP_PAIRS[primary], args)
                assert a.structuredContent == b.structuredContent and a.content == b.content
                return parsed(a)
            await same('peek', {'path': str(tmp_path / 'source.wav'), 'start_frame': 5, 'frames': 8000})
            summary = parsed(await call('thread', {'path': str(source_set), 'cache_dir': str(tmp_path / 'cache')}))
            handle = summary['handle']
            region = await same('thread_region', {'handle': handle, 'start_seconds': '0:00', 'duration_seconds': 3})
            assert 'analyze_region_frame_args' in region['clips'][0]['source_interval']
            await same('thread_find_clips', {'handle': handle, 'query': 'track:100/clip:0'})
            exported = parsed(await call('export_set_map', {'handle': handle, 'output_path': str(tmp_path / 'map.json')}))
            mapped = json.loads(Path(exported['path']).read_text())
            location = await same('thread_source_position', {'set_map': mapped, 'clip_id': 'track:100/clip:0',
                                                             'arrangement_beat': 2})
            await same('thread_arrangement_position', {'set_map': mapped, 'clip_id': 'track:100/clip:0',
                                                       'source_seconds': location['source_seconds']})
            trial = parsed(await call('create_trial', {'output_dir': str(tmp_path / 'trial'), 'variants': [
                {'source_path': str(tmp_path / 'source.wav'), 'label': 'Generated'}], 'start_frame': 0, 'frames': 1000}))
            await call('stitch_feedback', {'trial_dir': trial['trial_dir'], 'variant_id': 'v01',
                'output_sha256': trial['variants'][0]['output']['sha256'], 'start_frame': 0, 'end_frame': 100,
                'scope': 'bar_phase', 'note': 'Generated alias fixture, not a listening verdict'})
            await same('stitch_feedback_list', {'trial_dir': trial['trial_dir'], 'variant_id': 'v01'})
            for name in ('pipette', 'pipette_analyze'):
                rejected = await session.call_tool(name, {})
                assert rejected.isError
    asyncio.run(asyncio.wait_for(check(), timeout=60))
