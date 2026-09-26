# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit CC derivatives, with an independent wire decoder and retained sources."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import struct
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest

from pocket_music.artifact_store import canonical_bytes, read_bytes, read_record
from pocket_music.curves import curve_transform
from pocket_music.errors import PocketError
from pocket_music.material import material_import
from pocket_music.midi_io import midi_export

ROOT = Path(__file__).resolve().parents[1]


def revision(record):
    result = copy.deepcopy(record)
    result['revision_sha256'] = hashlib.sha256(canonical_bytes(
        {key: value for key, value in result.items() if key != 'revision_sha256'})).hexdigest()
    return result


def point(time, value, order=0):
    time = Fraction(time)
    return {'time': {'n': time.numerator, 'd': time.denominator}, 'value': value, 'order': order}


def material_with_curve(tmp_path, *, record=None, points=None, curve_id=None, channel=1):
    if record is None:
        record = json.loads((ROOT / 'examples/midi-workflow/external-import.json').read_text())['source']['material']
    target = {'kind': 'cc', 'target_id': 'opaque musical intention, not a wire address', 'scope': 'channel',
              'channel': channel, 'unit': 'midi1_7bit', 'value_min': 0, 'value_max': 127,
              'quantized': True, 'values': list(range(128)), 'ownership': 'none', 'value_mode': 'absolute'}
    curve = curve_transform(store_root=str(tmp_path / 'store'), request_id='curve-' + str(len(record['curves'])),
                            target=target, operations=[{'op': 'create', 'space': 'clip_qn', 'interpolation': 'step',
                            'points': points or [point(0, 51), point(Fraction(1, 7), 63),
                                                 point(Fraction(1, 7), 79, 1), point(1, 0)]}])['artifacts']['curve']
    curve_record = read_record(curve, tmp_path / 'store')
    if curve_id is not None:
        curve_record['curve_id'] = curve_id
    result = copy.deepcopy(record)
    result['curves'].append({'id': curve_record['curve_id'], **curve_record})
    result['clips'][0]['curve_ids'].append(curve_record['curve_id'])
    return revision(result), curve


def binding(record, controller=11, index=0):
    return {'curve_id': record['curves'][index]['id'], 'controller': controller, 'same_tick_order': 'before_existing'}


def export(tmp_path, record, bindings=None, request='export', **kwargs):
    return midi_export(record, str(tmp_path / 'store'), str(tmp_path / (request + '.mid')), request,
                       cc_step_bindings=bindings, **kwargs)


def decode(payload):
    """Decode wire bytes independently of Mido and Pocket's importer."""
    assert payload[:8] == b'MThd\x00\x00\x00\x06'
    kind, count, ppq = struct.unpack('>HHH', payload[8:14])
    offset, tracks = 14, []
    for _ in range(count):
        assert payload[offset:offset + 4] == b'MTrk'
        length = struct.unpack('>I', payload[offset + 4:offset + 8])[0]
        block = payload[offset + 8:offset + 8 + length]
        offset += 8 + length
        pos, tick, running, rows = 0, 0, None, []

        def variable(block=block):
            nonlocal pos
            value = 0
            while True:
                byte = block[pos]
                pos += 1
                value = value * 128 + (byte & 127)
                if byte < 128:
                    return value

        while pos < len(block):
            tick += variable()
            status = block[pos]
            if status >= 128:
                pos += 1
                running = status if status < 240 else None
            else:
                assert running is not None
                status = running
            if status == 255:
                meta = block[pos]
                pos += 1
                size = variable()
                data = bytes([status, meta]) + block[pos:pos + size]
            elif status in (240, 247):
                size = variable()
                data = bytes([status]) + block[pos:pos + size]
            else:
                size = 1 if status >> 4 in (12, 13) else 2
                data = bytes([status]) + block[pos:pos + size]
            pos += size
            rows.append((Fraction(tick, ppq), data))
        tracks.append(rows)
    assert offset == len(payload)
    return kind, ppq, tracks


def test_external_master_and_public_curve_encode_exact_steps_without_source_edit(tmp_path):
    record, curve = material_with_curve(tmp_path)
    snapshot, curve_bytes = copy.deepcopy(record), read_bytes(curve, tmp_path / 'store')
    imported = material_import({'kind': 'material', 'material': record}, str(tmp_path / 'store'), 'external')
    result = export(tmp_path, imported['material'], [binding(record)], format='smf0')
    kind, ppq, tracks = decode(read_bytes(result['midi'], tmp_path / 'store'))
    assert kind == 0 and ppq == 84
    assert [row for row in tracks[0] if row[1][0] == 176] == [
        (0, bytes([176, 11, 51])), (Fraction(1, 7), bytes([176, 11, 63])),
        (Fraction(1, 7), bytes([176, 11, 79])), (1, bytes([176, 11, 0]))]
    assert tracks[0][0] == (0, bytes([176, 11, 51]))
    assert [row for row in tracks[0] if row[1][0] == 128] == [
        (Fraction(1, 4), bytes([128, 60, 37])), (Fraction(7, 12), bytes([128, 64, 37])),
        (Fraction(47, 28), bytes([128, 67, 37]))]
    assert record == snapshot and read_record(imported['material'], tmp_path / 'store') == snapshot
    assert read_bytes(curve, tmp_path / 'store') == curve_bytes
    sidecar = read_record(result['sidecar'], tmp_path / 'store')
    assert read_record(sidecar['material'], tmp_path / 'store') == snapshot
    assert sidecar['cc_step_encoding']['bindings'][0]['target_id'] == record['curves'][0]['target']['target_id']
    assert sidecar['losses'] == [] and sidecar['maximum_quantization_error_qn'] == {'n': 0, 'd': 1}
    assert 'bindings' not in result['coverage']['fidelity']['cc_step_encoding']
    assert result['coverage']['native'] == 'not_verified' and sidecar['listening'] == 'not_performed'
    roundtrip = material_import({'kind': 'smf', 'path': result['output_path'],
                                 'expected_sha256': result['midi']['sha256']}, str(tmp_path / 'store'), 'reimport')
    restored = read_record(roundtrip['material'], tmp_path / 'store')
    fields = ('onset', 'duration_qn', 'pitch', 'velocity', 'release_velocity', 'channel')
    assert [{key: note[key] for key in fields} for note in restored['notes']] == [
        {key: note[key] for key in fields} for note in snapshot['notes']]
    assert [event['bytes'] for event in restored['events'] if event['message_type'] == 'control_change'] == [
        [176, 11, 51], [176, 11, 63], [176, 11, 79], [176, 11, 0]]


def test_public_curve_handle_composes_without_embedding_or_master_rewrite(tmp_path):
    master = json.loads((ROOT / 'examples/midi-workflow/external-import.json').read_text())['source']['material']
    _, curve = material_with_curve(tmp_path, record=master)
    before_master, before_curve = copy.deepcopy(master), read_bytes(curve, tmp_path / 'store')
    external = {'curve': curve, 'clip_id': master['clips'][0]['id'], 'controller': 11,
                'same_tick_order': 'before_existing'}
    result = export(tmp_path, master, [external])
    sidecar = read_record(result['sidecar'], tmp_path / 'store')
    assert sidecar['cc_step_encoding']['bindings'][0]['curve'] == curve
    assert read_record(sidecar['material'], tmp_path / 'store') == before_master
    assert read_bytes(curve, tmp_path / 'store') == before_curve and master == before_master
    assert sidecar['losses'] == []
    assert len([data for _, data in decode(read_bytes(result['midi'], tmp_path / 'store'))[2][0]
                if data[0] == 176]) == 4
    invalids = [{**external, 'clip_id': 'unresolved'}, {**external, 'curve_id': 'extra'},
                {**external, 'curve': {**curve, 'artifact_schema': 'pocket.material/v1'}}]
    for index, invalid in enumerate(invalids):
        with pytest.raises(PocketError):
            export(tmp_path, master, [invalid], request='external-invalid-' + str(index))
    embedded, _ = material_with_curve(tmp_path)
    with pytest.raises(PocketError, match='ambiguously'):
        export(tmp_path, embedded, [external], request='external-collision', loss_policy='approved',
               approved_losses=['canonical_expression_curves'])


def test_imported_raw_events_sustain_releases_and_passthrough_remain_unchanged(tmp_path):
    from test_midi_io import fixture_bytes
    source = tmp_path / 'source.mid'
    source.write_bytes(fixture_bytes())
    stamp = source.stat().st_mtime_ns
    imported = material_import({'kind': 'smf', 'path': str(source),
                                'expected_sha256': hashlib.sha256(fixture_bytes()).hexdigest()},
                               str(tmp_path / 'store'), 'import')
    raw = read_record(imported['material'], tmp_path / 'store')
    old = export(tmp_path, imported['material'], request='raw', format='smf0')
    assert read_bytes(old['midi'], tmp_path / 'store') == fixture_bytes()
    assert export(tmp_path, imported['material'], None, request='raw', format='smf0') == old
    master, _ = material_with_curve(tmp_path, record=raw, points=[point(0, 51), point(Fraction(1, 2), 70)])
    result = export(tmp_path, master, [binding(master)], format='smf0', ppq=480)
    rows = decode(read_bytes(result['midi'], tmp_path / 'store'))[2][0]
    assert [row for row in rows if row[1][:2] != bytes([176, 11])] == decode(fixture_bytes())[2][0]
    assert rows[0] == (0, bytes([176, 11, 51]))
    half = [data for time, data in rows if time == Fraction(1, 2)]
    assert half == [bytes([176, 11, 70]), bytes([128, 60, 45]), bytes([176, 64, 0]), bytes([144, 62, 77])]
    assert source.read_bytes() == fixture_bytes() and source.stat().st_mtime_ns == stamp
    assert read_record(imported['material'], tmp_path / 'store') == raw


@pytest.mark.parametrize('bad', [[], {}, [{'curve_id': 'missing', 'controller': 11, 'same_tick_order': 'before_existing'}],
                               'all'])
def test_bad_bindings_refuse_without_output(tmp_path, bad):
    master, _ = material_with_curve(tmp_path)
    with pytest.raises(PocketError):
        export(tmp_path, master, bad)
    assert not (tmp_path / 'export.mid').exists()


@pytest.mark.parametrize('controller', [0, 7, 10, 32, 64, 120, 127, True, 1.0, '11'])
def test_initial_controller_profile_refuses_bank_sustain_and_unsupported_routes(tmp_path, controller):
    master, _ = material_with_curve(tmp_path)
    with pytest.raises(PocketError):
        export(tmp_path, master, [binding(master, controller)])


@pytest.mark.parametrize('mutation', ['order', 'extra', 'duplicate', 'too_many', 'ownership', 'additive',
                                     'phrase_time', 'unit', 'enum', 'range', 'fractional', 'negative', 'after_end',
                                     'context_family'])
def test_unsupported_semantics_cannot_be_approved_away(tmp_path, mutation):
    master, curve_handle = material_with_curve(tmp_path)
    bindings = [binding(master)]
    curve = master['curves'][0]
    if mutation == 'order':
        bindings[0]['same_tick_order'] = 'after_existing'
    elif mutation == 'extra':
        bindings[0]['ignore_conflict'] = True
    elif mutation == 'duplicate':
        bindings *= 2
    elif mutation == 'too_many':
        bindings *= 17
    elif mutation == 'ownership':
        curve['target']['ownership'] = 'remote'
    elif mutation == 'additive':
        curve['target']['value_mode'] = 'additive'
    elif mutation == 'phrase_time':
        curve['space'] = 'phrase_qn'
    elif mutation == 'unit':
        curve['target']['unit'] = 'normalized'
    elif mutation == 'enum':
        curve['target']['values'] = []
    elif mutation == 'range':
        curve['target']['value_max'] = 128
    elif mutation == 'fractional':
        curve['points'][0]['value'] = 50.5
    elif mutation == 'negative':
        curve['points'][0]['time'] = {'n': -1, 'd': 1}
    elif mutation == 'after_end':
        curve['points'][-1]['time'] = {'n': 5, 'd': 1}
    else:
        curve['context'] = curve_handle
    with pytest.raises(PocketError):
        export(tmp_path, revision(master), bindings, loss_policy='approved', approved_losses=['canonical_expression_curves'])
    assert not (tmp_path / 'export.mid').exists()


def test_exact_ppq_float_values_unbound_loss_and_raw_conflict(tmp_path):
    master, _ = material_with_curve(tmp_path, points=[point(Fraction(1, 7), 64.0)])
    with pytest.raises(PocketError, match='Unrepresentable'):
        export(tmp_path, master, [binding(master)], ppq=480, request='rounded')
    result = export(tmp_path, master, [binding(master)])
    assert [data for _, data in decode(read_bytes(result['midi'], tmp_path / 'store'))[2][0]
            if data[0] == 176] == [bytes([176, 11, 64])]
    with pytest.raises(PocketError, match='canonical_expression_curves'):
        export(tmp_path, master, request='implicit')
    other, _ = material_with_curve(tmp_path, record=master, channel=2)
    with pytest.raises(PocketError, match='canonical_expression_curves'):
        export(tmp_path, other, [binding(other)], request='unbound')
    approved = export(tmp_path, other, [binding(other)], request='approved', loss_policy='approved',
                      approved_losses=['canonical_expression_curves'])
    assert approved['warnings'] == ['canonical_expression_curves']
    event = {'id': 'retained-cc', 'time': {'space': 'clip_qn', 'n': 0, 'd': 1}, 'order': 0,
             'bytes': [176, 11, 23], 'is_meta': False, 'message_type': 'control_change'}
    master['events'] = [event]
    master['clips'][0]['event_ids'] = [event['id']]
    with pytest.raises(PocketError, match='raw controller'):
        export(tmp_path, revision(master), [binding(master)], request='conflict')


def test_global_binding_order_conflict_and_tampered_replay(tmp_path):
    master, _ = material_with_curve(tmp_path, points=[point(0, 51), point(0, 63, 1)])
    master, _ = material_with_curve(tmp_path, record=master, points=[point(0, 79)], channel=2)
    second_clip = copy.deepcopy(master['clips'][0])
    second_clip.update(id='second-clip', note_ids=[], event_ids=[], curve_ids=[master['curves'][1]['id']])
    master['clips'][0]['curve_ids'] = [master['curves'][0]['id']]
    master['clips'].append(second_clip)
    master = revision(master)
    bindings = [binding(master, 1, 1), binding(master, 11, 0)]
    result = export(tmp_path, master, bindings, format='smf0')
    rows = decode(read_bytes(result['midi'], tmp_path / 'store'))[2][0]
    assert [data for time, data in rows if time == 0][:4] == [
        bytes([177, 1, 79]), bytes([176, 11, 51]), bytes([176, 11, 63]), bytes([144, 60, 80])]
    assert export(tmp_path, master, bindings, format='smf0') == result
    with pytest.raises(PocketError, match='idempotency'):
        export(tmp_path, master, list(reversed(bindings)), format='smf0')
    (tmp_path / 'export.mid').write_bytes(b'tampered')
    with pytest.raises(PocketError, match='changed'):
        export(tmp_path, master, bindings, format='smf0')
    master['curves'][1]['target']['channel'] = 1
    with pytest.raises(PocketError, match='conflicts across'):
        export(tmp_path, revision(master), [binding(master, 11, 0), binding(master, 11, 1)], request='duplicate-channel')


@pytest.mark.parametrize('other_activity', ['note', 'program_change', 'pitchwheel', 'mapped_curve'])
def test_smf1_refuses_cross_track_bound_channel_order_but_smf0_merges_exactly(tmp_path, other_activity):
    master, _ = material_with_curve(tmp_path, points=[point(0, 79)])
    bound_clip = copy.deepcopy(master['clips'][0])
    bound_clip.update(id='second-clip', note_ids=[], event_ids=[])
    master['clips'][0]['curve_ids'] = []
    master['clips'].append(bound_clip)
    if other_activity != 'note':
        master['notes'] = []
        master['clips'][0]['note_ids'] = []
    if other_activity in ('program_change', 'pitchwheel'):
        data = [192, 4] if other_activity == 'program_change' else [224, 0, 64]
        master['events'] = [{'id': 'other-channel-message', 'time': {'space': 'clip_qn', 'n': 0, 'd': 1},
                             'order': 0, 'bytes': data, 'is_meta': False, 'message_type': other_activity}]
        master['clips'][0]['event_ids'] = ['other-channel-message']
    bindings = [binding(master)]
    if other_activity == 'mapped_curve':
        master, _ = material_with_curve(tmp_path, record=master, points=[point(0, 51)])
        bindings.append(binding(master, 1, 1))
    master = revision(master)
    with pytest.raises(PocketError, match='SMF1'):
        export(tmp_path, master, bindings, format='smf1', request='separate')
    result = export(tmp_path, master, bindings, format='smf0', request='merged')
    rows = decode(read_bytes(result['midi'], tmp_path / 'store'))[2][0]
    assert rows[0] == (0, bytes([176, 11, 79]))
    assert result['coverage']['fidelity']['cc_step_encoding']['same_tick_scope'] == 'merged_smf0'


def test_new_cc_receipt_bounds_clip_metadata_but_sidecar_retains_every_origin(tmp_path):
    master, _ = material_with_curve(tmp_path, points=[point(0, 79)])
    for index in range(1000):
        master['clips'].append({**master['clips'][0], 'id': 'empty-' + str(index),
                                'note_ids': [], 'event_ids': [], 'curve_ids': []})
    result = export(tmp_path, revision(master), [binding(master)])
    assert len(json.dumps(result, ensure_ascii=True, indent=2).encode()) < 6000
    assert 'clip_origins' not in result['coverage']['fidelity']
    assert result['coverage']['fidelity']['clip_count'] == 1001
    assert len(read_record(result['sidecar'], tmp_path / 'store')['clip_origins']) == 1001


def cli(args, tmp_path):
    spec = tmp_path / 'cc-input.json'
    spec.write_text(json.dumps(args))
    return subprocess.run([sys.executable, '-m', 'pocket_music.cli', 'midi-export', '--spec', str(spec)],
                          capture_output=True, text=True, timeout=30, check=False,
                          env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')})


def test_actual_cli_typed_opt_in_and_direct_replay(tmp_path):
    _, curve = material_with_curve(tmp_path)
    master = json.loads((ROOT / 'examples/midi-workflow/external-import.json').read_text())['source']['material']
    args = {'material': master, 'store_root': str(tmp_path / 'store'), 'output_path': str(tmp_path / 'cli.mid'),
            'request_id': 'cli', 'cc_step_bindings': [{'curve': curve, 'clip_id': master['clips'][0]['id'],
                                                   'controller': 11, 'same_tick_order': 'before_existing'}]}
    direct = midi_export(**args)
    result = cli(args, tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == direct
    args['cc_step_bindings'][0]['controller'] = True
    assert cli(args, tmp_path).returncode != 0


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional MCP extra absent')
def test_actual_stdio_typed_opt_in_and_direct_replay(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exchange():
        _, curve = material_with_curve(tmp_path)
        master = json.loads((ROOT / 'examples/midi-workflow/external-import.json').read_text())['source']['material']
        args = {'material': master, 'store_root': str(tmp_path / 'store'), 'output_path': str(tmp_path / 'mcp.mid'),
                'request_id': 'mcp', 'cc_step_bindings': [{'curve': curve, 'clip_id': master['clips'][0]['id'],
                                                       'controller': 11, 'same_tick_order': 'before_existing'}]}
        direct = midi_export(**args)
        parameters = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'],
                                            env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')})
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            descriptor = next(tool for tool in (await session.list_tools()).tools if tool.name == 'midi_export')
            assert 'cc_step_bindings' in descriptor.inputSchema['properties']
            result = await session.call_tool('midi_export', args)
            assert not result.isError and json.loads(result.content[0].text) == direct
            for invalid in (True, 64):
                args['cc_step_bindings'][0]['controller'] = invalid
                assert (await session.call_tool('midi_export', args)).isError

    asyncio.run(exchange())
