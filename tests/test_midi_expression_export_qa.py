# SPDX-License-Identifier: AGPL-3.0-only
"""Independent wire and actual-interface qualification of explicit expression SMF."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
from fractions import Fraction

import pytest
from test_midi_expression_qa import encoding, expressive_phrase, lifecycle, receiver
from test_midi_interfaces import cli_call, environment
from test_midi_lifecycle_qa import control, phrase
from test_midi_qa import decode_wire, import_wire, seal_literal, smf, vlq
from test_midi_relationships_qa import note, q

from pocket_music.artifact_store import put_record, read_bytes, read_record
from pocket_music.errors import PocketError
from pocket_music.midi_expression import midi_expression_plan
from pocket_music.midi_io import midi_export


def configuration(record, **life):
    return {'lifecycle': lifecycle(record, **life), 'receiver_assumption': receiver(), 'encoding': encoding()}


def arguments(record, tmp_path, **changes):
    return {'material': record, 'store_root': str(tmp_path / 'store'), 'request_id': 'export-expression',
            'output_path': str(tmp_path / 'expression.mid'), 'ppq': 480,
            'expression': changes.pop('expression') if 'expression' in changes else configuration(record), **changes}


def test_qa_expression_smf_independent_bytes_composed_plan_and_parent(tmp_path):
    record = expressive_phrase()
    original = copy.deepcopy(record)
    config = configuration(record)
    args = arguments(record, tmp_path)
    planned = midi_expression_plan(material=record, store_root=args['store_root'], request_id='plan', **config)
    direct = midi_export(**args)
    composed = midi_export(**{**args, 'expression': planned['plan'], 'request_id': 'composed',
                             'output_path': str(tmp_path / 'composed.mid')})
    assert direct['midi'] == composed['midi']
    fmt, ppq, tracks = decode_wire(read_bytes(direct['midi'], args['store_root']))
    assert fmt == 1 and ppq == 480 and len(tracks) == 1
    rows = tracks[0]
    assert [(time, data) for time, data in rows if data[0] >> 4 == 9] == [
        (0, bytes([0x91, 60, 80])), (0, bytes([0x92, 67, 80]))]
    assert (3, bytes([0xE1, 51, 64])) in rows
    assert (3, bytes([0xD1, 51])) in rows and (3, bytes([0xB1, 74, 127])) in rows
    assert [(time, data) for time, data in rows if data[0] >> 4 == 8] == [
        (8, bytes([0x81, 60, 37])), (8, bytes([0x82, 67, 37]))]
    at_end = [data for time, data in rows if time == 8]
    assert at_end.index(bytes([0xE1, 0, 64])) < at_end.index(bytes([0x81, 60, 37]))
    assert rows[-1] == (8, b'\xff\x2f')
    assert all(data[1] in (64, 74) for _, data in rows if data[0] >> 4 == 11)
    sidecar = read_record(direct['sidecar'], args['store_root'])
    assert sidecar['expression_encoding']['plan'] == planned['plan']
    assert sidecar['maximum_quantization_error_qn'] == q(0)
    assert read_record(sidecar['material'], args['store_root']) == original == record
    assert len(json.dumps(direct, indent=2).encode()) <= 16384
    assert midi_export(**args) == direct


def test_qa_expression_smf_tail_reuse_controller_and_reset_order(tmp_path):
    record = phrase(notes=[note('a', 0, 2), note('b', 5, 1, 67)],
                    controls=[control('down', 1, 127), control('up', 4, 0, order=1)])
    record['clips'][0]['length_qn'] = q(6)
    record = seal_literal(record)
    config = configuration(record, horizon=7, tail=1)
    config['receiver_assumption'] = receiver(member_channels=[2])
    args = arguments(record, tmp_path, expression=config)
    result = midi_export(**args)
    rows = decode_wire(read_bytes(result['midi'], args['store_root']))[2][0]
    assert [(time, data) for time, data in rows if data[0] == 0xB0] == [
        (0, bytes([0xB0, 64, 0])), (1, bytes([0xB0, 64, 127])), (4, bytes([0xB0, 64, 0]))]
    reset = [bytes([0xE1, 0, 64]), bytes([0xD1, 0]), bytes([0xB1, 74, 64])]
    assert [data for time, data in rows if time == 5] == reset + reset + [bytes([0xE1, 0, 64]), bytes([0x91, 67, 80])]
    assert [data for time, data in rows if time == 7] == reset + [b'\xff\x2f']
    proof = read_record(result['sidecar'], args['store_root'])['expression_encoding']
    assert proof['encoded_end_qn'] == q(7) and proof['tail_extension_qn'] == q(1)


def test_qa_expression_smf_exact_rational_ppq_and_no_silent_rounding(tmp_path):
    record = expressive_phrase()
    record['curves'][0]['points'][1]['time'] = q(Fraction(1, 7))
    record = seal_literal(record)
    with pytest.raises(PocketError, match='timing'):
        midi_export(**arguments(record, tmp_path))
    assert not (tmp_path / 'expression.mid').exists()
    args = arguments(record, tmp_path, ppq=None, request_id='exact')
    result = midi_export(**args)
    _, ppq, tracks = decode_wire(read_bytes(result['midi'], args['store_root']))
    assert ppq == 7 and (Fraction(1, 7), bytes([0xE1, 51, 64])) in tracks[0]


def test_qa_expression_smf_retains_source_meta_order_raw_parent_and_tail(tmp_path):
    wire = smf([b'\0\xff\x03\x04Name\0\xff\x51\x03\x07\xa1\x20'
                b'\0\x90\x3c\x57' + vlq(480) + b'\x80\x3c\x25'
                + vlq(480) + b'\xff\x06\x03end' + vlq(480) + b'\xff\x2f\0'])
    handle = import_wire(tmp_path, wire)
    store = str(tmp_path / 'store')
    record = read_record(handle, store)
    args = arguments(handle, tmp_path, expression=configuration(record, horizon=3))
    result = midi_export(**args)
    rows = decode_wire(read_bytes(result['midi'], store))[2][0]
    assert [(t, data) for t, data in rows if data[0] == 255] == [
        (0, b'\xff\x03Name'), (0, b'\xff\x51\x07\xa1\x20'), (2, b'\xff\x06end'), (3, b'\xff\x2f')]
    assert read_record(handle, store) == record
    assert (tmp_path / 'wire.mid').read_bytes() == wire


@pytest.mark.parametrize('metadata', [b'\xff\x20\x01\x00', b'\xff\x21\x01\x00', b'\xff\x7f\x01\x01'])
def test_qa_expression_smf_routing_or_opaque_metadata_refuses(tmp_path, metadata):
    wire = smf([b'\0' + metadata + b'\0\x90\x3c\x57' + vlq(480) + b'\x80\x3c\x25\0\xff\x2f\0'])
    handle = import_wire(tmp_path, wire)
    record = read_record(handle, str(tmp_path / 'store'))
    with pytest.raises(PocketError):
        midi_export(**arguments(handle, tmp_path, expression=configuration(record, horizon=1)))
    assert not (tmp_path / 'expression.mid').exists()


def test_qa_expression_smf_forged_plan_and_optional_absent_compatibility(tmp_path):
    record = expressive_phrase()
    args = arguments(record, tmp_path)
    planned = midi_expression_plan(material=record, store_root=args['store_root'], request_id='plan', **args['expression'])
    forged = read_record(planned['plan'], args['store_root'])
    forged['events'][0]['bytes'] = [0xB0, 64, 127]
    handle = put_record(forged, args['store_root'])
    with pytest.raises(PocketError, match='recomputed'):
        midi_export(**{**args, 'expression': handle})
    with pytest.raises(PocketError):
        midi_export(**{**args, 'expression': planned['plan'], 'cc_step_bindings': [], 'request_id': 'both'})
    with pytest.raises(PocketError, match='canonical_expression'):
        midi_export(**{key: value for key, value in {**args, 'request_id': 'ordinary'}.items() if key != 'expression'})
    assert not (tmp_path / 'expression.mid').exists()


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional MCP extra not installed')
def test_qa_expression_export_actual_cli_stdio_plan_composition(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    record = expressive_phrase()
    args = arguments(record, tmp_path)
    planned = midi_expression_plan(material=record, store_root=args['store_root'], request_id='plan', **args['expression'])
    args['expression'] = planned['plan']
    result = midi_export(**args)
    assert cli_call(tmp_path, 'midi_export', args) == result
    async def exchange():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'], env=environment())
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listed = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert 'expression' in listed['midi_export'].inputSchema['properties']
            response = await session.call_tool('midi_export', args)
            assert not response.isError and json.loads(response.content[0].text) == result
            malformed = {**args, 'request_id': 'bad', 'expression': {'bad': 'record'}}
            assert (await session.call_tool('midi_export', malformed)).isError
            assert cli_call(tmp_path, 'midi_export', malformed, success=False)['error'] == 'PocketError'
    asyncio.run(exchange())


def test_qa_expression_smf_delta_exceeding_four_byte_vlq_refuses(tmp_path):
    record = phrase(notes=[note('far', 600000, 1)])
    record['clips'][0]['length_qn'] = q(600001)
    record = seal_literal(record)
    with pytest.raises(PocketError):
        midi_export(**arguments(record, tmp_path, expression=configuration(record, horizon=600001)))
    assert not (tmp_path / 'expression.mid').exists()


@pytest.mark.parametrize('format', ['smf0', 'smf1'])
def test_qa_expression_same_pitch_overlaps_remain_independent_members(tmp_path, format):
    record = phrase(notes=[note('a', 0, 2), note('b', 1, 2)])
    record['clips'][0]['length_qn'] = q(3)
    record = seal_literal(record)
    args = arguments(record, tmp_path, format=format, expression=configuration(record, horizon=3))
    result = midi_export(**args)
    actual_format, _, tracks = decode_wire(read_bytes(result['midi'], args['store_root']))
    assert actual_format == int(format[-1])
    assert [(t, data) for t, data in tracks[0] if data[0] >> 4 in (8, 9)] == [
        (0, bytes([0x91, 60, 80])), (1, bytes([0x92, 60, 80])),
        (2, bytes([0x81, 60, 37])), (3, bytes([0x82, 60, 37]))]


def test_qa_expression_stale_plan_does_not_authorize_changed_material(tmp_path):
    record = expressive_phrase()
    args = arguments(record, tmp_path)
    planned = midi_expression_plan(material=record, store_root=args['store_root'], request_id='plan', **args['expression'])
    changed = copy.deepcopy(record)
    changed['notes'][0]['velocity']['value'] = 79
    with pytest.raises(PocketError):
        midi_export(**{**args, 'material': seal_literal(changed), 'expression': planned['plan']})
    assert not (tmp_path / 'expression.mid').exists()


def test_qa_expression_muted_notes_require_named_loss_and_retain_parent(tmp_path):
    record = expressive_phrase()
    record['notes'][1]['mute'] = True
    record = seal_literal(record)
    args = arguments(record, tmp_path)
    with pytest.raises(PocketError, match='muted_notes_omitted'):
        midi_export(**args)
    result = midi_export(**{**args, 'request_id': 'approved', 'loss_policy': 'approved',
                           'approved_losses': ['muted_notes_omitted']})
    rows = decode_wire(read_bytes(result['midi'], args['store_root']))[2][0]
    assert len([data for _, data in rows if data[0] >> 4 == 9]) == 1
    sidecar = read_record(result['sidecar'], args['store_root'])
    assert sidecar['losses'] == ['muted_notes_omitted']
    assert read_record(sidecar['material'], args['store_root']) == record
