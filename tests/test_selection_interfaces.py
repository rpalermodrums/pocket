"""Real local adapters and generated evidence; external actions are not executed."""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from pocket_music import __version__
from pocket_music.cli import _SPEC_OPERATIONS, main
from pocket_music.music_embeddings import DIMENSION, MODEL_ID, MODEL_REVISION, WEIGHT_SHA256


def entries():
    return [{'track_id': f'r{i}', 'title': f'Generated {i}', 'artists': ['Fixture artist'],
             'duration_seconds': 240 + i * 20,
             'profile': {'energy': i / 5, 'bpm': None, 'vocal_density': None, 'provenance': 'user'}} for i in range(6)]


def write_spec(tmp_path, name, value):
    path = tmp_path / (name + '.json')
    path.write_text(json.dumps(value))
    return path


def cli(group, action, spec, output=None):
    args = [sys.executable, '-m', 'pocket_music.cli', group, action, '--spec', str(spec)]
    if output:
        args.extend(['--output', str(output)])
    result = subprocess.run(args, capture_output=True, text=True, timeout=45, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout else json.loads(Path(output).read_text())


@pytest.mark.parametrize('group,action,module,function,destination', [
    (group, action, *provider) for group, operations in _SPEC_OPERATIONS.items()
    for action, provider in operations.items()
])
def test_all_command_routes_match_public_functions(tmp_path, monkeypatch, capsys,
                                                     group, action, module, function, destination):
    module = importlib.import_module('pocket_music.' + module)
    seen = []
    monkeypatch.setattr(module, function, lambda **kwargs: seen.append(kwargs) or {'received': kwargs})
    spec = write_spec(tmp_path, 'spec', {'example': 3})
    output = tmp_path / ('new-artifact' if destination else 'response.json')
    assert main([group, action, '--spec', str(spec), '--output', str(output)]) == 0
    expected = {'example': 3, **({destination: str(output)} if destination else {})}
    assert seen == [expected]
    stdout = capsys.readouterr().out
    assert json.loads(stdout if destination else output.read_text()) == {'received': expected}
    if destination:
        duplicate = write_spec(tmp_path, 'duplicate', {destination: 'unexpected'})
        assert main([group, action, '--spec', str(duplicate), '--output', str(output)]) == 2
        assert len(seen) == 1
    else:
        assert main([group, action, '--spec', str(spec), '--output', str(output)]) == 2
        assert len(seen) == 1  # no mutation before a known response-file conflict


def test_cli_bag_routes_scoped_feedback_and_session_cas(tmp_path):
    bag = cli('bag', 'create', write_spec(tmp_path, 'bag-input', {'tracks': entries(), 'title': 'Generated'}),
              tmp_path / 'bag')
    found = cli('bag', 'query', write_spec(tmp_path, 'query', {'handle': bag['handle'], 'limit': 2}))
    assert found['returned'] == 2 and found['next_offset'] == 2
    plan = cli('workshop', 'plan', write_spec(tmp_path, 'plan-spec', {
        'bag_handle': bag['handle'], 'brief': {'setting': 'warm_up', 'track_count': 4}, 'seed': 7}), tmp_path / 'plan')
    route = plan['routes'][0]
    event = cli('workshop', 'feedback', write_spec(tmp_path, 'feedback-spec', {
        'plan_handle': plan['handle'], 'route_id': route['route_id'], 'disposition': 'avoid',
        'from_track_id': route['track_ids'][0], 'to_track_id': route['track_ids'][1]}), tmp_path / 'feedback')
    next_plan = cli('workshop', 'replan', write_spec(tmp_path, 'replan-spec', {
        'plan_handle': event['handle'], 'seed': 7}), tmp_path / 'next-plan')
    assert next_plan['summary']['feedback_applied_count'] == 1
    state = cli('on-deck', 'prepare', write_spec(tmp_path, 'session-spec', {
        'bag_handle': bag['handle'], 'current_track_id': 'r0', 'intent': {'setting': 'peak_time'}}), tmp_path / 'session')
    options = cli('on-deck', 'options', write_spec(tmp_path, 'options-spec', {
        'session_dir': state['session_dir'], 'limit': 3}))
    assert len(options['options']) == 3 and all(t['track_id'] != 'r0' for t in options['options'])
    update = write_spec(tmp_path, 'update-spec', {'session_dir': state['session_dir'], 'action': 'choose',
                        'track_id': options['options'][0]['track_id'], 'expected_revision': state['revision'],
                        'expected_sha256': state['sha256']})
    chosen = cli('on-deck', 'update', update)
    assert chosen['revision'] == 1
    stale = subprocess.run([sys.executable, '-m', 'pocket_music.cli', 'on-deck', 'update', '--spec', str(update)],
                           capture_output=True, text=True, timeout=45, check=False)
    assert stale.returncode == 2 and 'Session changed' in json.loads(stale.stderr)['message']


def test_public_exports_are_lazy_and_metadata_matches():
    import pocket_music
    from pocket_music.record_bag import create_record_bag
    assert pocket_music.create_record_bag is create_record_bag
    assert pocket_music.__version__ == '0.3.0'
    import tomllib
    metadata = tomllib.loads((Path(__file__).parents[1] / 'pyproject.toml').read_text())
    assert metadata['project']['version'] == __version__
    assert 'web/*.js' in metadata['tool']['setuptools']['package-data']['pocket_music']
    result = subprocess.run([sys.executable, '-c',
                             "import sys,pocket_music; print('torch' in sys.modules, 'transformers' in sys.modules)"],
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'False False'


def receipt(modality):
    result = {'schema': 'pocket.music-embedding/v1', 'model_id': MODEL_ID,
              'model_revision': MODEL_REVISION, 'checkpoint_sha256': WEIGHT_SHA256,
              'dimension': DIMENSION, 'vector': [1.0] + [0.0] * (DIMENSION - 1), 'modality': modality,
              'fixture_provenance': 'Generated vector for schema testing, not model inference'}
    if modality == 'audio':
        result.update(asset={'sha256': 'a' * 64, 'frames': 480000},
                      source_region={'source_origin': 'user_recording', 'source_start_frame': 0,
                                     'source_frames': 480000, 'extra_recipe_note': 'preserve'})
    else:
        result['text_origin'] = 'user_authored'
    return result


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional agent extra')
def test_mcp_selection_schema_and_real_typed_calls(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exchange():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'],
                                       env={'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name: t for t in (await session.list_tools()).tools}
            assert len(tools) == 35
            assert tools['session_options'].inputSchema['properties']['limit']['type'] == 'integer'
            assert tools['update_session'].inputSchema['properties']['action']['enum'] == ['choose', 'skip', 'intent']
            assert tools['execute_spotify_playlist'].annotations.openWorldHint
            assert not tools['execute_spotify_playlist'].annotations.readOnlyHint
            assert tools['query_record_bag'].annotations.readOnlyHint
            assert tools['inspect_source_formats'].annotations.openWorldHint
            assert tools['inspect_source_formats'].inputSchema['properties']['limit']['type'] == 'integer'
            assert not tools['plan_set_routes'].annotations.openWorldHint
            assert 'token' not in tools['execute_spotify_playlist'].inputSchema['properties']
            assert 'workspace' not in tools
            plan_schema = tools['plan_set_routes'].inputSchema
            assert plan_schema['$defs']['SetBrief']['properties']['track_count']['type'] == 'integer'
            bag_schema = tools['create_record_bag'].inputSchema
            assert {'schema', 'path', 'sha256'} <= set(plan_schema['$defs']['BagHandle']['required'])
            assert any(v.get('type') == 'null' for v in bag_schema['$defs']['MusicalProfile']['properties']['energy']['anyOf'])

            async def call(name, args):
                result = await session.call_tool(name, args)
                assert not result.isError, result.content
                assert result.structuredContent is None and len(result.content) == 1
                assert len(result.content[0].text.splitlines()) == 1
                return json.loads(result.content[0].text)

            bag = await call('create_record_bag', {'tracks': entries(), 'title': 'Generated MCP',
                                                   'output_dir': str(tmp_path / 'bag')})
            page = await call('query_record_bag', {'handle': bag['handle'], 'limit': 2})
            assert page['returned'] == 2 and page['truncated']
            plans = await call('plan_set_routes', {'bag_handle': bag['handle'], 'brief': {
                'setting': 'after_hours', 'track_count': 4, 'anchor_track_ids': ['r1', 'r4']},
                'output_dir': str(tmp_path / 'plans'), 'seed': 12})
            assert len(plans['routes']) == 3
            state = await call('prepare_session', {'bag_handle': bag['handle'],
                'output_dir': str(tmp_path / 'session'), 'current_track_id': 'r0'})
            opts = await call('session_options', {'session_dir': state['session_dir'], 'limit': 2,
                'expected_revision': state['revision'], 'expected_sha256': state['sha256']})
            state2 = await call('update_session', {'session_dir': state['session_dir'], 'action': 'choose',
                'track_id': opts['options'][0]['track_id'], 'expected_revision': state['revision'],
                'expected_sha256': state['sha256']})
            assert state2['revision'] == 1
            stale = await session.call_tool('update_session', {'session_dir': state['session_dir'],
                'action': 'skip', 'track_id': 'r5', 'expected_revision': 0, 'expected_sha256': state['sha256']})
            assert stale.isError
            imported = await call('import_spotify_items', {'items': [{'uri': 'spotify:track:' + 'A' * 22,
                'title': 'Generated catalogue row', 'artists': ['Test']}], 'output_dir': str(tmp_path / 'import')})
            assert imported['model_input_allowed'] is False
            spotify = await call('plan_spotify_playlist', {'uris': ['spotify:track:' + 'A' * 22],
                'name': 'Generated plan only', 'output_dir': str(tmp_path / 'spotify-plan')})
            verified = await call('verify_spotify_playlist_ui', {'plan_dir': spotify['plan_dir'],
                'expected_plan_sha256': spotify['sha256'], 'playlist_url': 'https://open.spotify.com/playlist/' + 'B' * 22,
                'observed_uris': ['spotify:track:' + 'A' * 22], 'observed_private': True,
                'observer': 'Generated fixture, no real UI observation'})
            assert verified['verification_method'] == 'operator_reported_ui'
            acquisition = await call('plan_acquisition', {'source_url': 'https://www.youtube.com/watch?v=abcdefghijk',
                'output_dir': str(tmp_path / 'acquisition-plan'), 'version_note': 'Generated plan only',
                'authorization_note': 'No download executed'})
            assert acquisition['version_identity'] == 'unverified'
            preflight = await call('model_preflight', {'model_dir': str(tmp_path / 'absent-model')})
            assert not preflight['files_ready']
            audio_receipt = receipt('audio')
            index = await call('build_embedding_index', {'receipts': [audio_receipt],
                                                          'output_path': str(tmp_path / 'index.json')})
            # Schema validation must preserve complete provider provenance, not discard extra fields.
            stored = json.loads(Path(index['path']).read_text())
            expected = hashlib.sha256((json.dumps(audio_receipt, sort_keys=True, indent=2,
                                                  allow_nan=False) + '\n').encode()).hexdigest()
            assert stored['entries']['a' * 64]['receipt_sha256'] == [expected]
            matches = await call('rank_embedding_query', {'query_receipt': receipt('text'), 'index_handle': index,
                                                            'limit': 1})
            assert matches[0]['audio_sha256'] == 'a' * 64
            bad = await session.call_tool('plan_set_routes', {'bag_handle': bag['handle'],
                'brief': {'track_count': -2}, 'output_dir': str(tmp_path / 'bad')})
            assert bad.isError and not (tmp_path / 'bad').exists()
            typo = await session.call_tool('plan_set_routes', {'bag_handle': bag['handle'],
                    'brief': {'track_count': 3, 'exluded_track_ids': ['r1']}, 'output_dir': str(tmp_path / 'typo')})
            assert typo.isError and not (tmp_path / 'typo').exists()

    asyncio.run(asyncio.wait_for(exchange(), timeout=60))


def test_cli_bad_response_parent_does_not_update_session(tmp_path, monkeypatch, capsys):
    from pocket_music import on_deck
    seen = []
    monkeypatch.setattr(on_deck, 'update_session', lambda **kwargs: seen.append(kwargs))
    spec = write_spec(tmp_path, 'spec', {})
    assert main(['on-deck', 'update', '--spec', str(spec), '--output', str(tmp_path / 'missing' / 'out.json')]) == 2
    assert not seen
    assert 'parent' in capsys.readouterr().err
