# SPDX-License-Identifier: AGPL-3.0-only
"""Independent revoicing expectations; no native or musical acceptance implied."""
import asyncio
import copy
import importlib.util
import json
import sys

import pytest
from test_midi_interfaces import cli_call, environment
from test_midi_qa import decode_wire, expressed_material, import_wire, literal_material, seal_literal

from pocket_music.artifact_store import canonical_bytes, read_bytes, read_record
from pocket_music.errors import PocketError
from pocket_music.material import material_query
from pocket_music.midi_edit import midi_transform
from pocket_music.midi_io import midi_export


def operation(ids=('note:0',), pitch=62):
    return {'op': 'revoice', 'destinations': [
        {'note_id': note_id, 'pitch': {'midi_note': pitch, 'cents_offset': 0,
                                     'tuning_ref': 'tuning:12tet-a440'}} for note_id in ids],
        'hypothesis': {'label': 'Alternate inner voicing', 'actor': 'Independent fixture',
                       'actor_kind': 'agent', 'statement': 'A supplied hypothesis, not a detected key.',
                       'uncertainty': ['No human listening has occurred.']},
        'controller_timeline': 'preserve_existing', 'pitch_expression': 'preserve_relative'}


def arguments(tmp_path, record=None, op=None, ids=('note:0',), request='revoice'):
    record = literal_material() if record is None else record
    store = str(tmp_path / 'store')
    return {'material': record, 'store_root': store, 'request_id': request,
            'selection': material_query(record, store, selection={'note_ids': list(ids)})['selection'],
            'operations': [operation(ids) if op is None else op]}


def test_qa_pitch_only_exact_locks_hypothesis_and_original(tmp_path):
    args = arguments(tmp_path)
    original = copy.deepcopy(args['material'])
    args['locks'] = {'selected_fields': [field for field in original['notes'][0] if field not in ('id', 'pitch')]}
    result = midi_transform(**args)
    child = read_record(result['material'], args['store_root'])
    assert args['material'] == original
    assert child['notes'][1:] == original['notes'][1:]
    expected = copy.deepcopy(original['notes'][0])
    expected['pitch']['midi_note'] = 62
    assert child['notes'][0] == expected
    for field in ('events', 'curves', 'clips', 'tracks', 'sources', 'tempo_map_ref', 'meter_map_ref'):
        assert child[field] == original[field]
    edit = read_record(result['edit'], args['store_root'])
    assert edit['operation_reports'][0]['hypothesis'] == args['operations'][0]['hypothesis']
    assert edit['operation_reports'][0]['hypothesis_basis'] == 'supplied_attribution_not_inferred'
    assert edit['operation_reports'][0]['listening'] == 'not_performed'
    assert read_record(edit['inverse']['restore_parent'], args['store_root']) == original
    assert midi_transform(**args) == result
    args['operations'][0]['destinations'][0]['pitch']['midi_note'] = 63
    with pytest.raises(PocketError, match='idempotency_conflict'):
        midi_transform(**args)


@pytest.mark.parametrize('change,match', [
    (lambda op: op.update(extra=1), 'fields'),
    (lambda op: op['destinations'][0].update(extra=1), 'exactly'),
    (lambda op: op['destinations'].append(copy.deepcopy(op['destinations'][0])), 'unique'),
    (lambda op: op.update(destinations=[]), '1–4096'),
    (lambda op: op['destinations'][0].update(note_id='note:1'), 'exactly cover'),
    (lambda op: op['destinations'][0]['pitch'].update(midi_note=True), 'integer'),
    (lambda op: op['destinations'][0]['pitch'].update(midi_note=128), 'range'),
    (lambda op: op['destinations'][0]['pitch'].update(cents_offset=True), 'finite numeric'),
    (lambda op: op['destinations'][0]['pitch'].update(cents_offset=float('inf')), 'finite|JSON'),
    (lambda op: op['destinations'][0]['pitch'].update(tuning_ref=''), 'tuning'),
    (lambda op: op['destinations'][0].update(pitch={'midi_note': 62}), 'exactly'),
    (lambda op: op.update(pitch_expression='absolute'), 'preserve_relative'),
    (lambda op: op.update(controller_timeline='copy'), 'controllers'),
    (lambda op: op['hypothesis'].update(actor_kind='model'), 'attribution'),
    (lambda op: op['hypothesis'].update(actor=' '), 'nonempty'),
    (lambda op: op['hypothesis'].update(statement='x' * 4097), '4096'),
    (lambda op: op['hypothesis'].update(uncertainty=['x'] * 33), '32'),
    (lambda op: op['hypothesis'].update(uncertainty=['']), 'nonempty'),
    (lambda op: op['hypothesis'].update(ground_truth=True), 'complete'),
])
def test_qa_malformed_destinations_and_claims_refuse(tmp_path, change, match):
    op = operation()
    change(op)
    with pytest.raises(PocketError, match=match):
        midi_transform(**arguments(tmp_path, op=op))
    assert not (tmp_path / 'store/artifacts').exists()


def test_qa_pitch_lock_and_new_overlap_refuse(tmp_path):
    args = arguments(tmp_path)
    args['locks'] = {'selected_fields': ['pitch']}
    with pytest.raises(PocketError, match='lock'):
        midi_transform(**args)
    record = literal_material()
    record['notes'][1]['onset'] = copy.deepcopy(record['notes'][0]['onset'])
    record = seal_literal(record)
    with pytest.raises(PocketError, match='overlap'):
        midi_transform(**arguments(tmp_path, record, operation(pitch=64), request='overlap'))


def test_qa_stale_selection_oversized_map_and_failed_retry_refuse(tmp_path):
    args = arguments(tmp_path)
    args['selection']['material_revision'] = '0' * 64
    with pytest.raises(PocketError, match='revision|selection'):
        midi_transform(**args)
    op = operation()
    op['destinations'] *= 4097
    oversized = arguments(tmp_path, op=op, request='oversized')
    with pytest.raises(PocketError, match='4096'):
        midi_transform(**oversized)
    with pytest.raises(PocketError, match='did not complete'):
        midi_transform(**oversized)
    assert not (tmp_path / 'store/artifacts').exists()


def test_qa_custom_tuning_remains_literal_without_interpretation(tmp_path):
    op = operation()
    op['destinations'][0]['pitch'] = {'midi_note': 61, 'cents_offset': -17.25, 'tuning_ref': 'user:unknown-tuning'}
    args = arguments(tmp_path, op=op)
    result = midi_transform(**args)
    child = read_record(result['material'], args['store_root'])
    assert child['notes'][0]['pitch'] == op['destinations'][0]['pitch']
    report = read_record(result['edit'], args['store_root'])['operation_reports'][0]
    assert report['tuning_conversion'] == 'not_performed'
    assert report['audible_contour'] == 'not_inferred'
    with pytest.raises(PocketError, match='Unapproved'):
        midi_export(result['material'], args['store_root'], str(tmp_path / 'no.mid'), 'export')


def pitch_expression(mode='additive', unit='cents'):
    record = expressed_material()
    target = record['curves'][0]['target']
    target.update(kind='per_note_pitch', unit=unit, value_mode=mode, value_min=-100, value_max=100)
    record['curves'][0]['points'][1]['value'] = 30
    return seal_literal(record)


@pytest.mark.parametrize('unit', ['cents', 'semitones'])
def test_qa_additive_note_expression_exact_with_explicit_policy(tmp_path, unit):
    record = pitch_expression(unit=unit)
    args = arguments(tmp_path, record)
    with pytest.raises(PocketError, match='expression_policy'):
        midi_transform(**args)
    args.update(expression_policy='preserve_relative', request_id='qualified')
    result = midi_transform(**args)
    child = read_record(result['material'], args['store_root'])
    assert child['curves'] == record['curves']
    assert child['notes'][0]['expression_refs'] == record['notes'][0]['expression_refs']


@pytest.mark.parametrize('mode,unit', [('absolute', 'cents'), ('multiplicative', 'semitones'), ('additive', 'Hz')])
def test_qa_unqualified_pitch_expression_refuses(tmp_path, mode, unit):
    args = arguments(tmp_path, pitch_expression(mode, unit))
    args['expression_policy'] = 'preserve_relative'
    with pytest.raises(PocketError, match='additive|unit'):
        midi_transform(**args)


def test_qa_duplicates_never_join_selected_revoice_targets(tmp_path):
    args = arguments(tmp_path)
    args['operations'].insert(0, {'op': 'duplicate', 'delta_qn': 2, 'controller_timeline': 'preserve_existing'})
    result = midi_transform(**args)
    child = read_record(result['material'], args['store_root'])
    assert child['notes'][0]['pitch']['midi_note'] == 62
    assert child['notes'][-1]['pitch']['midi_note'] == 60
    assert child['notes'][-1]['derived_from'] == ['note:0']


def test_qa_imported_raw_source_controller_order_and_off_encoding_preserved(tmp_path):
    handle = import_wire(tmp_path)
    store = str(tmp_path / 'store')
    parent = read_record(handle, store)
    raw = read_bytes(parent['sources'][0]['raw'], store)
    ids = tuple(note['id'] for note in parent['notes'])
    op = operation(ids)
    op['destinations'][1]['pitch']['midi_note'] = 65
    args = arguments(tmp_path, handle, op, ids)
    result = midi_transform(**args)
    child = read_record(result['material'], store)
    assert child['events'] == parent['events']
    assert [note['source_binding'] for note in child['notes']] == [note['source_binding'] for note in parent['notes']]
    assert read_bytes(parent['sources'][0]['raw'], store) == raw
    exported = midi_export(result['material'], store, str(tmp_path / 'out.mid'), 'export')
    tracks = decode_wire(read_bytes(exported['midi'], store))[2]
    controllers_before = [(time, data) for track in decode_wire(raw)[2] for time, data in track if data[0] >> 4 == 11]
    controllers_after = [(time, data) for track in tracks for time, data in track if data[0] >> 4 == 11]
    assert controllers_after == controllers_before
    assert any(data == bytes([144, 65, 0]) for track in tracks for _, data in track)
    assert [data[1] for track in tracks for _, data in track if data[0] >> 4 == 9 and data[2] > 0] == [62, 65]


def test_qa_opaque_source_refuses_and_response_remains_bounded(tmp_path):
    record = literal_material()
    record['notes'][0]['source_binding'] = {'kind': 'live_clip', 'unobserved': True}
    with pytest.raises(PocketError, match='opaque/native'):
        midi_transform(**arguments(tmp_path, seal_literal(record)))
    op = operation()
    op['hypothesis']['statement'] = 'x' * 4096
    op['hypothesis']['uncertainty'] = ['x' * 1024] * 32
    result = midi_transform(**arguments(tmp_path, op=op, request='large-proof'))
    assert len(canonical_bytes(result)) <= 16384
    assert read_record(result['edit'], str(tmp_path / 'store'))['operation_reports'][0]['hypothesis'] == op['hypothesis']


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional MCP extra absent')
def test_qa_revoice_actual_cli_stdio_schema_and_replay(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    args = arguments(tmp_path)
    direct = midi_transform(**args)
    assert cli_call(tmp_path, 'midi_transform', args) == direct

    async def exchange():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'], env=environment())
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            variants = [value for value in tools['midi_transform'].inputSchema['$defs'].values()
                        if value.get('properties', {}).get('op', {}).get('const') == 'revoice']
            assert len(variants) == 1 and variants[0]['additionalProperties'] is False
            assert set(variants[0]['required']) == set(operation())
            response = await session.call_tool('midi_transform', args)
            assert not response.isError, response.content
            assert response.structuredContent is None
            assert json.loads(response.content[0].text) == direct
            bad = copy.deepcopy(args)
            bad['request_id'] = 'bad-interface'
            bad['operations'][0]['destinations'][0]['pitch']['midi_note'] = True
            invalid = await session.call_tool('midi_transform', bad)
            assert invalid.isError
            assert not (tmp_path / 'store/requests/bad-interface').exists()
    asyncio.run(exchange())
