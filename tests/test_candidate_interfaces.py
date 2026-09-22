"""Independent actual transports over synthetic native evidence, never Live control."""
import asyncio
import copy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from test_native_candidates import CONTEXT, material_fixture, seal_args, source_fixture

from pocket_music import auditions, native_candidates
from pocket_music.artifact_store import read_record
from pocket_music.assets import sha256_file
from pocket_music.capabilities import capabilities_list
from pocket_music.errors import PocketError


def environment():
    return {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}


def cli_call(tmp_path, name, arguments, success=True):
    spec = tmp_path / (name + '-spec.json')
    spec.write_text(json.dumps(arguments))
    result = subprocess.run([sys.executable, '-m', 'pocket_music.cli', name.replace('_', '-'),
                             '--spec', str(spec)], env=environment(), capture_output=True,
                            text=True, timeout=30, check=False)
    if success:
        assert result.returncode == 0, result.stderr
        assert len(result.stdout.encode()) < 65536
        return json.loads(result.stdout)
    assert result.returncode == 2, result.stderr
    assert 'Traceback' not in result.stderr
    return json.loads(result.stderr)


def assert_equivalent(name, actual, expected):
    if name == 'validate_candidate':
        assert actual['schema'] == 'pocket.operation-receipt/v1'
        assert len(json.dumps(actual).encode()) < 3072
    if name == 'validate_candidate_promotion':
        # Thread intentionally issues a new immutable snapshot file per read.
        # Verify each actual handle, then compare every content identity/result.
        from pocket_music.thread_queries import _load
        actual, expected = copy.deepcopy(actual), copy.deepcopy(expected)
        for result in (actual, expected):
            handle = result['thread']['handle']
            _load(handle)
            handle.pop('cache_path')
    assert actual == expected


def preparation_args(tmp_path, request_id='baseline', mode='clone_only'):
    source = tmp_path / 'source/fixture.als'
    if not source.exists():
        source = source_fixture(tmp_path)
    store = str(tmp_path / 'store')
    args = {'source_als': str(source), 'expected_source_sha256': sha256_file(source),
            'store_root': store, 'request_id': request_id, 'mode': mode, 'context': CONTEXT}
    if mode == 'with_material':
        args.update(material=material_fixture(store),
                    layer={'track_name': 'Pocket Layer', 'instrument_device': 'Operator'})
    return args


def provider(name):
    module = auditions if hasattr(auditions, name) else native_candidates
    return getattr(module, name)


async def lifecycle(tmp_path, check):
    # Concrete external material replaces generation; no DAW, Serum, UI or model is used.
    baseline_args = preparation_args(tmp_path)
    baseline = await check('candidate_prepare', baseline_args)
    inspect_args = {'store_root': baseline_args['store_root'],
                    'workspace_id': baseline['workspace']['workspace_id'], 'expected_revision': 1}
    inspected = await check('candidate_inspect', inspect_args)
    assert inspected['history']['total'] == 2 and inspected['history']['rows'] == []
    page = await check('candidate_inspect', {**inspect_args, 'history_limit': 1})
    assert len(page['history']['rows']) == 1 and page['history']['next_cursor'] is not None
    final_page = await check('candidate_inspect', {**inspect_args, 'history_limit': 1,
                                                 'history_cursor': page['history']['next_cursor']})
    assert final_page['history']['rows'][0]['revision'] == 1
    assert final_page['history']['next_cursor'] is None
    cancelled = await check('candidate_prepare', preparation_args(tmp_path, 'cancelled'))
    await check('candidate_cancel', {'store_root': baseline_args['store_root'],
        'workspace_id': cancelled['workspace']['workspace_id'], 'expected_revision': 1, 'request_id': 'cancel'})
    base = await check('candidate_seal', seal_args(baseline, baseline_args['store_root'], request_id='seal-base'))
    variant = await check('candidate_prepare', preparation_args(tmp_path, 'variant', 'with_material'))
    chosen = await check('candidate_seal', seal_args(variant, baseline_args['store_root'], True, 'seal-variant'))
    store = baseline_args['store_root']
    candidate = chosen['artifacts']['candidate']
    await check('validate_candidate', {'store_root': store, 'candidate': candidate})
    planned = await check('audition_plan', {'store_root': store, 'request_id': 'plan', 'candidates': [candidate],
        'baseline': base['artifacts']['candidate'], 'span_qn': [{'n': 4, 'd': 1}, {'n': 8, 'd': 1}],
        'question': 'Synthetic transport comparison; no listening', 'pre_roll_qn': {'n': 4, 'd': 1},
        'post_roll_qn': {'n': 4, 'd': 1}, 'tail_seconds': 1, 'sample_rate': 8000})
    plan_handle = planned['artifacts']['audition_plan']
    plan = read_record(plan_handle, store)
    trial = read_record(candidate, store)
    render = tmp_path / 'generated-fixture.wav'
    sf.write(render, np.full((plan['expected_frames'], 2), .01), 8000, subtype='FLOAT')
    attachment = await check('attach_candidate_render', {'store_root': store, 'request_id': 'attach',
        'candidate': candidate, 'render_plan': plan_handle, 'path': str(render), 'expected_sha256': sha256_file(render),
        'actual_settings': plan['settings'], 'native_report': {
            'actor': 'Synthetic technical fixture; no native export', 'actor_kind': 'agent',
            'observed_at': '2026-09-16T00:00:00+00:00', 'candidate_sha256': trial['saved_als_sha256'],
            'export_completed': True, 'arrangement_only': True, 'no_missing_media': True}})
    assert attachment['coverage']['human_listening'] == 'not_reviewed'
    feedback = await check('audition_feedback', {'store_root': store, 'request_id': 'feedback',
        'attachment': attachment['artifacts']['attachment'], 'interval_frames': [0, 8000],
        'actor': 'Synthetic technical reviewer', 'actor_kind': 'agent', 'note': 'Transport test only', 'decision': 'keep'})
    promoted = await check('promote_candidate', {'store_root': store, 'request_id': 'promote',
        'candidate': candidate, 'attachment': attachment['artifacts']['attachment'], 'output_dir': str(tmp_path / 'kept'),
        'decision': {'action': 'keep', 'actor': 'Synthetic technical reviewer', 'actor_kind': 'agent',
                     'reason': 'Technical evidence parity; no musical judgment', 'feedback': feedback['artifacts']['feedback']}})
    assert promoted['coverage']['decision'] == 'agent_technical_keep'
    await check('validate_candidate_promotion', {'promotion_dir': promoted['promotion_dir'],
                                                'expected_lineage_sha256': promoted['lineage_sha256']})


def complete_catalog(limit=50):
    rows = []
    pages = []
    seen = set()
    cursor = None
    while True:
        page = capabilities_list(limit=limit, cursor=cursor)
        assert 0 < len(page['capabilities']) <= limit
        if pages:
            for field in ('total', 'catalog_sha256', 'host_profile_sha256'):
                assert page[field] == pages[0][field]
        pages.append(page)
        rows.extend(page['capabilities'])
        cursor = page['next_cursor']
        if cursor is None:
            break
        assert cursor not in seen
        seen.add(cursor)
    assert len(pages) >= 2
    assert len(rows) == pages[0]['total']
    public = [row['public_tool'] for row in rows if row['public_tool']]
    assert len(public) == len(set(public))
    return rows


def test_candidate_catalog_page_sizes_preserve_complete_inventory():
    wide = complete_catalog(limit=50)
    narrow = complete_catalog(limit=7)
    assert narrow == wide
    names = {row['public_tool'] for row in narrow if row['public_tool']}
    assert {'attach_candidate_render', 'audition_feedback', 'promote_candidate',
            'candidate_prepare', 'validate_candidate_promotion'} <= names


def test_candidate_cli_public_lifecycle_parity_and_catalog(tmp_path):
    outputs = {}

    async def check(name, arguments):
        expected = provider(name)(**arguments)
        actual = cli_call(tmp_path, name, arguments)
        assert_equivalent(name, actual, expected)
        outputs[name] = actual
        return actual

    asyncio.run(lifecycle(tmp_path, check))
    rows = {row['public_tool']: row for row in complete_catalog() if row['public_tool']}
    for name, output in outputs.items():
        assert rows[name]['output_schema'] == output['schema'], f'Incorrect catalog output schema for {name}'


@pytest.mark.parametrize('change', [
    {'context': {**CONTEXT, 'tempo_bpm': '120'}},
    {'context': {**CONTEXT, 'length_qn': {'n': True, 'd': 1}}},
    {'context': {**CONTEXT, 'ignore_source_changes': True}},
])
def test_candidate_cli_rejects_coercion_and_undeclared_policy(tmp_path, change):
    args = {**preparation_args(tmp_path), **change}
    with pytest.raises(PocketError):
        native_candidates.candidate_prepare(**args)
    assert cli_call(tmp_path, 'candidate_prepare', args, success=False)['error'] == 'PocketError'
    assert not (tmp_path / 'store/workspaces').exists()


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional MCP extra not installed')
def test_candidate_real_stdio_composition_and_strict_inputs(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exchange():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'], env=environment())
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            names = {'candidate_prepare', 'candidate_inspect', 'candidate_cancel', 'candidate_seal', 'validate_candidate',
                     'audition_plan', 'attach_candidate_render', 'audition_feedback', 'promote_candidate',
                     'validate_candidate_promotion', 'native_midi_write', 'candidate_native_reconcile',
                     'candidate_native_abandon'}
            assert names <= tools.keys()
            assert all(tools[name].inputSchema.get('additionalProperties') is False for name in names)
            assert tools['candidate_inspect'].annotations.readOnlyHint is True
            assert tools['candidate_seal'].annotations.readOnlyHint is False
            assert all(tools[name].annotations.readOnlyHint is False for name in
                       ('native_midi_write', 'candidate_native_reconcile', 'candidate_native_abandon'))
            assert not {'native_midi_cancel', 'native_render', 'candidate_apply'} & tools.keys()

            async def check(name, arguments):
                expected = provider(name)(**arguments)
                response = await session.call_tool(name, arguments)
                assert not response.isError, (name, response.content)
                assert response.structuredContent is None and len(response.content) == 1
                text = response.content[0].text
                assert len(text.splitlines()) == 1 and len(text.encode()) < 65536
                actual = json.loads(text)
                assert_equivalent(name, actual, expected)
                assert_equivalent(name, cli_call(tmp_path, name, arguments), actual)
                return actual

            await lifecycle(tmp_path, check)
            for index, change in enumerate([
                    {'context': {**CONTEXT, 'tempo_bpm': '120'}},
                    {'context': {**CONTEXT, 'length_qn': {'n': True, 'd': 1}}},
                    {'context': {**CONTEXT, 'ignore_source_changes': True}},
                    {'unexpected_top_level': True}]):
                args = {**preparation_args(tmp_path, f'invalid-{index}'), **change}
                response = await session.call_tool('candidate_prepare', args)
                assert response.isError, (change, response.content)
                assert not (tmp_path / 'store/requests' / f'invalid-{index}').exists()
            baseline = native_candidates.candidate_prepare(**preparation_args(tmp_path, 'strict-revision'))
            for invalid in (True, '1'):
                response = await session.call_tool('candidate_inspect', {'store_root': str(tmp_path / 'store'),
                    'workspace_id': baseline['workspace']['workspace_id'], 'expected_revision': invalid})
                assert response.isError, response.content

    asyncio.run(asyncio.wait_for(exchange(), timeout=120))
