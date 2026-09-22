"""Independent semantic discovery checks against actual file-only provider outputs."""
import copy

import pytest
from test_auditions import attachment_args
from test_instruments import observation
from test_native_candidates import sealed_fixture

from pocket_music.artifact_store import canonical_bytes
from pocket_music.auditions import attach_candidate_render
from pocket_music.capabilities import PUBLIC_CAPABILITIES, capabilities_list
from pocket_music.errors import PocketError
from pocket_music.instruments import instrument_inspect, instrument_parameters
from pocket_music.native_candidates import candidate_inspect


def catalog_pages(limit=7, **options):
    pages = []
    cursor = None
    seen = set()
    while True:
        result = capabilities_list(limit=limit, cursor=cursor, **options)
        assert len(result['capabilities']) <= limit
        assert result['omitted'] == result['total'] - len(result['capabilities'])
        assert len(canonical_bytes(result)) <= 65536
        if pages:
            assert result['catalog_sha256'] == pages[0]['catalog_sha256']
            assert result['host_profile_sha256'] == pages[0]['host_profile_sha256']
            assert result['total'] == pages[0]['total']
        pages.append(result)
        cursor = result['next_cursor']
        if cursor is None:
            assert sum(len(page['capabilities']) for page in pages) == result['total']
            return pages
        assert cursor not in seen
        seen.add(cursor)


def catalog(**options):
    rows = [row for page in catalog_pages(**options) for row in page['capabilities']]
    return {row.get('public_tool') or row['proposed_tool']: row for row in rows}


def direct_artifact_families(result):
    families = set()
    def visit(value):
        if isinstance(value, dict) and value.get('schema') == 'pocket.artifact-handle/v1':
            families.add(value['artifact_schema'])
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)
    for value in result['artifacts'].values():
        visit(value)
    return families


def test_qa_parameter_descriptor_declares_the_state_handle_actually_returned(tmp_path):
    inspected = instrument_inspect(store_root=str(tmp_path), request_id='qa-instrument-state',
        scope='supplied_observation', observation=observation())
    result = instrument_parameters(store_root=str(tmp_path), state=inspected['artifacts']['state'])
    assert direct_artifact_families(result) == {'pocket.instrument-state/v1'}
    assert direct_artifact_families(result) <= set(catalog()['instrument_parameters']['returned_artifact_schemas'])


def test_qa_render_descriptor_declares_both_attachment_and_audio_artifacts(tmp_path):
    args, _ = attachment_args(tmp_path)
    result = attach_candidate_render(**args)
    assert direct_artifact_families(result) == {'pocket.render-attachment/v3', 'pocket.render-audio/v1'}
    assert direct_artifact_families(result) <= set(catalog()['attach_candidate_render']['returned_artifact_schemas'])


def test_qa_inspection_descriptor_includes_a_sealed_workspace_candidate(tmp_path):
    sealed, _, args, _ = sealed_fixture(tmp_path)
    result = candidate_inspect(args['store_root'], sealed['workspace']['workspace_id'], sealed['workspace']['revision'])
    handles = [value for value in result['workspace'].values()
               if isinstance(value, dict) and value.get('schema') == 'pocket.artifact-handle/v1']
    families = {value['artifact_schema'] for value in handles}
    assert 'pocket.native-trial/v3' in families
    assert families <= set(catalog()['candidate_inspect']['returned_artifact_schemas'])


def test_qa_abandonment_metadata_distinguishes_pending_and_observation_from_its_permanent_record():
    row = catalog()['candidate_native_abandon']
    assert set(row['accepted_artifact_schemas']) == {'pocket.native-pending/v1', 'pocket.native-midi-observation/v1'}
    assert set(row['returned_artifact_schemas']) == {'pocket.native-abandonment/v1', 'pocket.native-pending/v1'}
    assert row['accepted_handles'] == ['pocket.artifact-handle/v1']
    assert row['output_schema'] == 'pocket.operation-receipt/v1'
    assert row['native_verified'] is False


def test_qa_discovery_copies_all_mutable_family_metadata_and_remains_stable():
    before = capabilities_list(limit=50)
    expected = copy.deepcopy(before)
    for row in before['capabilities']:
        for value in row.values():
            if isinstance(value, list):
                value.append('pocket.poisoned-response-only/v1')
    again = capabilities_list(limit=50)
    assert again == expected


def test_qa_discovery_is_complete_page_bounded_and_preserves_semantic_metadata():
    full = catalog_pages(limit=50)
    assert len(full) >= 2
    expected = [row for result in full for row in result['capabilities']]
    pages = []
    cursor = None
    while True:
        result = capabilities_list(limit=1, cursor=cursor)
        assert len(result['capabilities']) <= 1
        assert len(canonical_bytes(result)) <= 4096
        assert result['catalog_sha256'] == full[0]['catalog_sha256']
        pages.extend(result['capabilities'])
        following = result['next_cursor']
        assert following is None or following != cursor
        cursor = following
        if cursor is None:
            break
    assert pages == expected
    assert {row['public_tool'] for row in pages if row['public_tool']} == {row[0] for row in PUBLIC_CAPABILITIES}
    for row in pages:
        for field in ['accepted_artifact_schemas', 'returned_artifact_schemas', 'accepted_handles']:
            assert isinstance(row[field], list)
            assert len(row[field]) == len(set(row[field]))
        assert 'pocket.artifact-handle/v1' not in row['accepted_artifact_schemas']
        assert 'pocket.artifact-handle/v1' not in row['returned_artifact_schemas']
        if row['public_tool'] is None:
            assert row['accepted_artifact_schemas'] == row['returned_artifact_schemas'] == []


@pytest.mark.parametrize('mutation', [{'domain': 'native'}, {'operation': 'inspect'}, {'host_profile': {'user_claimed': 'verified'}}])
def test_qa_cursor_cannot_cross_filters_or_claimed_host_profile(mutation):
    first = capabilities_list(limit=1)
    with pytest.raises(PocketError, match='cursor'):
        capabilities_list(limit=1, cursor=first['next_cursor'], **mutation)


@pytest.mark.parametrize('profile', [
    {'native_verified': True, 'serum_installed': True},
    {'status': 'available', 'required_profile': None, 'allowed_operations': ['delete_all_notes']},
    {'host_version': 'unqualified', 'instrument': 'Serum', 'max_notes': 10000},
])
def test_qa_host_claims_cannot_expand_qualified_writing_or_change_families(profile):
    before = catalog()
    claimed = catalog_pages(limit=50, host_profile=profile)
    assert len(claimed) >= 2
    after = {row.get('public_tool') or row['proposed_tool']: row
             for page in claimed for row in page['capabilities']}
    assert list(after) == list(before)
    for name in before:
        assert after[name] == before[name]
    writer = after['native_midi_write']
    assert writer['public_tool'] == 'native_midi_write'
    assert writer['status'] == 'requires_native_setup'
    assert writer['required_profile'] == 'live-12.4.5-supervised-ordinary-notes/v1'
    assert writer['native_verified'] is False
    assert set(writer['side_effects']) == {'new_local_artifacts', 'native_owned_clip_notes',
                                           'workspace_revision_update', 'durable_host_lease'}
    assert after['native_midi_cancel']['public_tool'] is None
    assert after['native_midi_cancel']['implementation_status'] == 'implemented_unqualified'
    assert after['native_render']['public_tool'] is None
    assert after['native_save_checkpoint']['public_tool'] is None


def test_qa_semantic_family_metadata_survives_actual_cli_and_stdio_mcp(tmp_path):
    import asyncio
    import json
    import sys

    from test_midi_interfaces import cli_call, environment

    pytest.importorskip('mcp')
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    profile = {'native_verified': True, 'serum_installed': True}
    expected = catalog_pages(limit=50, host_profile=profile)
    assert len(expected) >= 2
    cursor = None
    for page in expected:
        assert cli_call(tmp_path, 'capabilities_list',
                        {'limit': 50, 'cursor': cursor, 'host_profile': profile}) == page
        cursor = page['next_cursor']
    assert cursor is None

    async def exchange():
        parameters = StdioServerParameters(command=sys.executable,
            args=['-m', 'pocket_music.mcp_server'], env=environment())
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            available = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert {'native_midi_write', 'candidate_native_reconcile', 'candidate_native_abandon'} <= available.keys()
            assert not {'native_midi_cancel', 'native_render', 'native_save_checkpoint'} & available.keys()
            cursor = None
            for page in expected:
                result = await session.call_tool('capabilities_list',
                    {'limit': 50, 'cursor': cursor, 'host_profile': profile})
                assert not result.isError and len(result.content) == 1
                assert json.loads(result.content[0].text) == page
                cursor = page['next_cursor']
            assert cursor is None

    asyncio.run(asyncio.wait_for(exchange(), timeout=30))


def test_qa_reconcile_metadata_includes_both_returned_terminal_and_pending(tmp_path, monkeypatch):
    from test_candidate_native_pending import fixture, reconcile_args, terminal, unknown

    from pocket_music import native_midi
    from pocket_music.artifact_store import read_record
    from pocket_music.native_candidates import candidate_native_reconcile

    # Isolate file recovery metadata here; actual native terminal validity has
    # separate end-to-end writer coverage and actual native evidence review.
    monkeypatch.setattr(native_midi, 'validate_native_terminal', lambda handle, store: read_record(handle, store))
    monkeypatch.setattr(native_midi, 'release_native_host_lease', lambda handle, store: None)
    scope, binding, _, _ = fixture(tmp_path)
    pending = unknown(scope, binding)
    result = candidate_native_reconcile(**reconcile_args(scope, terminal(pending, scope['store_root'])))
    assert result['coverage']['native_redispatched'] is False
    assert direct_artifact_families(result) == {'pocket.native-midi-terminal/v1', 'pocket.native-pending/v1'}
    row = catalog()['candidate_native_reconcile']
    assert direct_artifact_families(result) <= set(row['returned_artifact_schemas'])
    assert row['status'] == 'available'
    assert set(row['accepted_artifact_schemas']) == {'pocket.native-midi-terminal/v1'}
    assert 'native_owned_clip_notes' not in row['side_effects']
