# SPDX-License-Identifier: AGPL-3.0-only
"""Independent exact-byte CC1/11 export expectations; no native or listening claims."""
from __future__ import annotations

import copy
import hashlib
from fractions import Fraction
from pathlib import Path

import pytest
from test_midi_qa import decode_wire, literal_material, seal_literal, smf, vlq

from pocket_music.artifact_store import put_bytes, put_record, read_bytes, read_record
from pocket_music.errors import PocketError
from pocket_music.material import material_import
from pocket_music.midi_io import midi_export

CURVE_ID = 'opaque:claims-cc64-🎹'


def qn(n, d=1):
    return {'n': n, 'd': d}


def cc_curve(curve_id=CURVE_ID, *, channel=1):
    return {'schema': 'pocket.curve/v1', 'id': curve_id, 'curve_id': curve_id,
            'target': {'kind': 'cc', 'target_id': 'opaque-target-says-cc74', 'scope': 'channel',
                       'unit': 'midi1_7bit', 'value_min': 0, 'value_max': 127,
                       'quantized': True, 'values': list(range(128)), 'ownership': 'none',
                       'value_mode': 'absolute', 'channel': channel},
            'space': 'clip_qn', 'interpolation': 'step',
            'points': [{'time': qn(0), 'value': 16, 'order': 0},
                       {'time': qn(1, 3), 'value': 100.0, 'order': 7},
                       {'time': qn(1, 3), 'value': 40, 'order': 9},
                       {'time': qn(17, 16), 'value': 0, 'order': 0},
                       {'time': qn(4), 'value': 127, 'order': 0}],
            'parent': None, 'context': None, 'executable': False,
            'provenance': {'provider': 'independent external author', 'musical_decision': None}}


def external_material():
    material = literal_material()
    curve = cc_curve()
    material['curves'] = [curve]
    material['clips'][0]['curve_ids'] = [curve['id']]
    return seal_literal(material)


def binding(curve_id=CURVE_ID, controller=11):
    return {'curve_id': curve_id, 'controller': controller, 'same_tick_order': 'before_existing'}


def args(tmp_path, material=None, **changes):
    result = {'material': external_material() if material is None else material,
              'store_root': str(tmp_path / 'store'), 'output_path': str(tmp_path / 'out.mid'),
              'request_id': 'export-qa', 'cc_step_bindings': [binding()]}
    result.update(changes)
    return result


def cc_rows(tracks):
    return [(track_index, time, wire) for track_index, track in enumerate(tracks)
            for time, wire in track if wire[0] & 0xF0 == 0xB0]


def expected_cc(channel=1, controller=11):
    return [(Fraction(0), bytes((0xB0 + channel - 1, controller, 16))),
            (Fraction(1, 3), bytes((0xB0 + channel - 1, controller, 100))),
            (Fraction(1, 3), bytes((0xB0 + channel - 1, controller, 40))),
            (Fraction(17, 16), bytes((0xB0 + channel - 1, controller, 0))),
            (Fraction(4), bytes((0xB0 + channel - 1, controller, 127)))]


@pytest.mark.parametrize('format', ['smf0', 'smf1'])
@pytest.mark.parametrize('controller,channel', [(1, 1), (11, 16)])
def test_qa_literal_external_cc_bytes_order_and_note_gates_are_exact(tmp_path, format, controller, channel):
    material = external_material()
    material['curves'][0]['target']['channel'] = channel
    material = seal_literal(material)
    before = copy.deepcopy(material)
    result = midi_export(**args(tmp_path, material, format=format, ppq=672,
                                cc_step_bindings=[binding(controller=controller)]))
    actual_format, ppq, tracks = decode_wire(read_bytes(result['midi'], tmp_path / 'store'))
    assert actual_format == int(format[-1]) and ppq == 672 and len(tracks) == 1
    encoding = result['coverage']['fidelity']['cc_step_encoding']
    assert encoding['same_tick_scope'] == ('merged_smf0' if format == 'smf0' else 'within_each_smf_track')
    assert [(time, wire) for _, time, wire in cc_rows(tracks)] == expected_cc(channel, controller)
    expected_notes = [(Fraction(0), b'\x90\x3c\x50'), (Fraction(1, 4), b'\x80\x3c\x25'),
                      (Fraction(1, 3), b'\x90\x40\x50'), (Fraction(7, 12), b'\x80\x40\x25'),
                      (Fraction(10, 7), b'\x90\x43\x50'), (Fraction(47, 28), b'\x80\x43\x25')]
    assert [(time, wire) for time, wire in tracks[0] if wire[0] & 0xF0 in {0x80, 0x90}] == expected_notes
    assert tracks[0][-1] == (Fraction(4), b'\xff\x2f')
    at_third = [wire for time, wire in tracks[0] if time == Fraction(1, 3)]
    assert at_third == [bytes((0xB0 + channel - 1, controller, 100)),
                        bytes((0xB0 + channel - 1, controller, 40)), b'\x90\x40\x50']
    assert material == before
    assert Path(result['output_path']).read_bytes() == read_bytes(result['midi'], tmp_path / 'store')
    assert midi_export(**args(tmp_path, material, format=format, ppq=672,
                              cc_step_bindings=[binding(controller=controller)])) == result
    sidecar = read_record(result['sidecar'], tmp_path / 'store')
    assert 'canonical_expression_curves' not in sidecar['losses']
    assert sidecar['maximum_quantization_error_qn'] == qn(0)
    assert sidecar['native_roundtrip'] == 'not_performed' and sidecar['listening'] == 'not_performed'


def test_qa_auto_ppq_includes_controller_times_and_never_rounds(tmp_path):
    result = midi_export(**args(tmp_path))
    _, ppq, tracks = decode_wire(read_bytes(result['midi'], tmp_path / 'store'))
    assert ppq == 336
    assert [(time, wire) for _, time, wire in cc_rows(tracks)] == expected_cc()
    with pytest.raises(PocketError, match='timing|PPQ|represent'):
        midi_export(**args(tmp_path, request_id='unrepresentable-qa', output_path=str(tmp_path / 'no.mid'), ppq=480))
    assert not (tmp_path / 'no.mid').exists()


def test_qa_default_and_empty_route_preserve_explicit_loss_behavior(tmp_path):
    material = external_material()
    for index, route in enumerate([{}, {'cc_step_bindings': None}, {'cc_step_bindings': []}]):
        call = args(tmp_path, material, request_id=f'default-{index}', output_path=str(tmp_path / f'default-{index}.mid'))
        call.pop('cc_step_bindings')
        call.update(route)
        with pytest.raises(PocketError, match='cc_step_bindings' if route.get('cc_step_bindings') == [] else 'canonical_expression_curves'):
            midi_export(**call)
        assert not Path(call['output_path']).exists()
    plain = literal_material()
    old_args = args(tmp_path, plain, request_id='legacy', output_path=str(tmp_path / 'legacy.mid'))
    old_args.pop('cc_step_bindings')
    result = midi_export(**old_args)
    assert midi_export(**{**old_args, 'cc_step_bindings': None}) == result


@pytest.mark.parametrize('mutation', [
    {'controller': True}, {'controller': 11.0}, {'controller': '11'}, {'controller': 64},
    {'controller': 0}, {'controller': 7}, {'controller': 120}, {'controller': 128},
    {'same_tick_order': 'after_existing'}, {'curve_id': 'unbound-stale-id'}, {'extra': True},
])
def test_qa_invalid_binding_is_never_bypassed_by_loss_approval(tmp_path, mutation):
    b = {**binding(), **mutation}
    call = args(tmp_path, cc_step_bindings=[b], loss_policy='approved',
                approved_losses=['canonical_expression_curves'])
    with pytest.raises(PocketError):
        midi_export(**call)
    assert not (tmp_path / 'out.mid').exists()


@pytest.mark.parametrize('bad', [True, {}, 'not-a-list', [None], [binding()] * 17,
                                  [{'curve_id': CURVE_ID, 'controller': 11}]])
def test_qa_malformed_or_oversized_binding_containers_refuse(tmp_path, bad):
    with pytest.raises(PocketError):
        midi_export(**args(tmp_path, cc_step_bindings=bad))
    assert not (tmp_path / 'out.mid').exists()


@pytest.mark.parametrize('field,value', [
    ('unit', 'normalized'), ('value_min', 1), ('value_max', 128), ('quantized', False),
    ('values', []), ('ownership', 'host_automation'), ('ownership', 'unknown'),
    ('value_mode', 'additive'), ('value_mode', 'multiplicative'), ('channel', True), ('channel', 17),
    ('note_id', 'a-channel-curve-must-not-carry-note-ownership'),
])
def test_qa_bound_target_profile_is_exact_and_loss_approval_cannot_relax_it(tmp_path, field, value):
    material = external_material()
    material['curves'][0]['target'][field] = value
    material = seal_literal(material)
    with pytest.raises(PocketError):
        midi_export(**args(tmp_path, material, loss_policy='approved', approved_losses=['canonical_expression_curves']))
    assert not (tmp_path / 'out.mid').exists()


@pytest.mark.parametrize('mutation', ['phrase', 'linear', 'fractional', 'bool', 'negative', 'after_end',
                                      'duplicate_order', 'nonreduced', 'too_many', 'unowned'])
def test_qa_invalid_curve_timing_shape_and_membership_refuse(tmp_path, mutation):
    material = external_material()
    curve = material['curves'][0]
    if mutation == 'phrase': curve['space'] = 'phrase_qn'
    elif mutation == 'linear': curve['interpolation'] = 'linear'
    elif mutation == 'fractional': curve['points'][0]['value'] = 16.5
    elif mutation == 'bool': curve['points'][0]['value'] = True
    elif mutation == 'negative': curve['points'][0]['time'] = qn(-1)
    elif mutation == 'after_end': curve['points'][-1]['time'] = qn(5)
    elif mutation == 'duplicate_order': curve['points'][2]['order'] = 7
    elif mutation == 'nonreduced': curve['points'][0]['time'] = qn(0, 2)
    elif mutation == 'too_many': curve['points'] = [{'time': qn(0), 'value': 1, 'order': i} for i in range(4097)]
    else: material['clips'][0]['curve_ids'] = []
    material = seal_literal(material)
    with pytest.raises(PocketError):
        midi_export(**args(tmp_path, material))
    assert not (tmp_path / 'out.mid').exists()


def raw_source():
    conductor = (b'\x00\xff\x51\x03\x07\xa1\x20\x00\xff\x58\x04\x04\x02\x18\x08'
                 + vlq(960) + b'\xff\x51\x03\x06\x1a\x80' + vlq(960) + b'\xff\x2f\x00')
    music = (b'\x00\xb0\x40\x7f\x00\xb0\x07\x60\x00\x90\x3c\x50'
             + vlq(240) + b'\x80\x3c\x25\x00\xe0\x00\x50' + vlq(240) + b'\xb0\x40\x00'
             + vlq(1440) + b'\xff\x2f\x00')
    return smf([conductor, music], ppq=480)


def imported_material(tmp_path):
    payload = raw_source()
    source = tmp_path / 'source.mid'
    source.write_bytes(payload)
    imported = material_import({'kind': 'smf', 'path': str(source), 'expected_sha256': hashlib.sha256(payload).hexdigest()},
                               str(tmp_path / 'store'), 'import-source')
    record = read_record(imported['material'], tmp_path / 'store')
    record['parent_revision'] = record['revision_sha256']
    record['curves'] = [cc_curve()]
    record['clips'][1]['curve_ids'] = [CURVE_ID]
    return seal_literal(record), source, payload


def test_qa_imported_wire_tempo_sustain_release_and_source_bytes_remain_exact(tmp_path):
    material, source, original = imported_material(tmp_path)
    before = copy.deepcopy(material)
    source_stamp = (source.stat().st_ino, source.stat().st_mtime_ns, source.stat().st_size)
    result = midi_export(**args(tmp_path, material, ppq=960))
    _, _, tracks = decode_wire(read_bytes(result['midi'], tmp_path / 'store'))
    _, _, original_tracks = decode_wire(original)
    assert tracks[0] == original_tracks[0]
    assert [(time, wire) for time, wire in tracks[1] if not (wire[0] == 0xB0 and wire[1] == 11)] == original_tracks[1]
    assert [(time, wire) for _, time, wire in cc_rows([tracks[1]]) if wire[1] == 11] == expected_cc()
    assert source.read_bytes() == original
    assert source_stamp == (source.stat().st_ino, source.stat().st_mtime_ns, source.stat().st_size)
    assert material == before
    assert tracks[1][:4] == [(0, b'\xb0\x0b\x10'), (0, b'\xb0\x40\x7f'),
                             (0, b'\xb0\x07\x60'), (0, b'\x90\x3c\x50')]


def add_curve(material, *, curve_id='second-opaque', channel=1, other_clip=False):
    curve = cc_curve(curve_id, channel=channel)
    material['curves'].append(curve)
    if other_clip:
        clip = copy.deepcopy(material['clips'][0])
        clip.update(id='second-clip', note_ids=[], event_ids=[], curve_ids=[curve_id])
        material['clips'].append(clip)
    else:
        material['clips'][0]['curve_ids'].append(curve_id)
    return curve


@pytest.mark.parametrize('other_clip', [False, True])
@pytest.mark.parametrize('format', ['smf0', 'smf1'])
def test_qa_duplicate_channel_controller_conflicts_globally(tmp_path, other_clip, format):
    material = external_material()
    second = add_curve(material, other_clip=other_clip)
    with pytest.raises(PocketError):
        midi_export(**args(tmp_path, seal_literal(material), format=format,
                          cc_step_bindings=[binding(), binding(second['id'])]))
    assert not (tmp_path / 'out.mid').exists()


def test_qa_distinct_channel_or_controller_binding_order_is_explicit(tmp_path):
    material = external_material()
    second = add_curve(material, channel=2)
    # First wire slot follows the explicit bindings list, not ID text or the
    # canonical material curve array's order.
    result = midi_export(**args(tmp_path, seal_literal(material),
                                cc_step_bindings=[binding(second['id'], 1), binding()]))
    _, _, tracks = decode_wire(read_bytes(result['midi'], tmp_path / 'store'))
    assert [wire for time, wire in tracks[0] if time == 0][:3] == [b'\xb1\x01\x10', b'\xb0\x0b\x10', b'\x90\x3c\x50']


@pytest.mark.parametrize('format', ['smf0', 'smf1'])
def test_qa_raw_controller_conflict_on_another_track_refuses(tmp_path, format):
    material, _, _ = imported_material(tmp_path)
    # Move the planned curve into the conductor clip; the same channel raw
    # controller remains on the music track. Controller7 is outside the public
    # binding profile, so use an independently authored existing CC11 instead.
    raw = next(event for event in material['events'] if event['bytes'][:2] == [0xB0, 7])
    raw['bytes'][1] = 11
    material['clips'][1]['curve_ids'] = []
    material['clips'][0]['curve_ids'] = [CURVE_ID]
    material = seal_literal(material)
    with pytest.raises(PocketError):
        midi_export(**args(tmp_path, material, format=format))
    assert not (tmp_path / 'out.mid').exists()


def test_qa_unbound_curves_still_require_explicit_degradation_and_remain_in_source(tmp_path):
    material = external_material()
    second = add_curve(material, channel=2)
    material = seal_literal(material)
    before = copy.deepcopy(material)
    with pytest.raises(PocketError, match='canonical_expression_curves'):
        midi_export(**args(tmp_path, material))
    result = midi_export(**args(tmp_path, material, request_id='approved-unbound', loss_policy='approved',
                                approved_losses=['canonical_expression_curves']))
    _, _, tracks = decode_wire(read_bytes(result['midi'], tmp_path / 'store'))
    assert [(time, wire) for _, time, wire in cc_rows(tracks)] == expected_cc()
    assert second['id'] in material['clips'][0]['curve_ids']
    assert read_record(result['sidecar'], tmp_path / 'store')['losses'] == ['canonical_expression_curves']
    assert material == before


def test_qa_tampered_material_and_transitive_curve_source_refuse(tmp_path):
    material = external_material()
    parent = cc_curve('historical-parent')
    parent.pop('id')
    parent_handle = put_record(parent, tmp_path / 'store')
    material['curves'][0]['parent'] = parent_handle
    material = seal_literal(material)
    handle = put_record(material, tmp_path / 'store')
    (tmp_path / 'store' / parent_handle['artifact_uri']).write_bytes(b'corrupted retained curve')
    with pytest.raises(PocketError):
        midi_export(**args(tmp_path, handle))
    assert not (tmp_path / 'out.mid').exists()


def test_qa_wrong_parent_artifact_family_refuses_even_when_bytes_are_intact(tmp_path):
    material = external_material()
    raw = put_bytes(b'opaque retained bytes', tmp_path / 'store', 'opaque.bin', 'pocket.binary-asset/v1')
    material['curves'][0]['parent'] = raw
    with pytest.raises(PocketError):
        midi_export(**args(tmp_path, seal_literal(material)))
    assert not (tmp_path / 'out.mid').exists()


def test_qa_maximum_unicode_bindings_keep_full_source_behind_bounded_receipt(tmp_path):
    import json
    import shutil

    from pocket_music.artifact_store import _verify_handles

    material = literal_material()
    material['curves'] = []
    material['clips'][0]['curve_ids'] = []
    bindings = []
    for channel in range(1, 17):
        curve_id = f'{channel:02}-' + '🎹' * 1021
        curve = cc_curve(curve_id, channel=channel)
        curve['target']['target_id'] = '🎼' * 1024
        curve['points'] = [curve['points'][0]]
        material['curves'].append(curve)
        material['clips'][0]['curve_ids'].append(curve_id)
        bindings.append(binding(curve_id))
    material = seal_literal(material)
    result = midi_export(**args(tmp_path, material, cc_step_bindings=bindings))
    sidecar = read_record(result['sidecar'], tmp_path / 'store')
    encoding = sidecar['cc_step_encoding']
    assert encoding['binding_count'] == 16 and encoding['point_count'] == 16
    assert [row['curve_id'] for row in encoding['bindings']] == [row['curve_id'] for row in bindings]
    assert read_record(sidecar['material'], tmp_path / 'store') == material
    assert 'bindings' not in result['coverage']['fidelity']['cc_step_encoding']
    assert len((json.dumps(result, indent=2) + '\n').encode()) < 8192
    moved = tmp_path / 'relocated'
    shutil.copytree(tmp_path / 'store' / 'artifacts', moved / 'artifacts')
    _verify_handles(result['sidecar'], moved)
    assert read_record(sidecar['material'], moved) == material
    assert read_bytes(result['midi'], moved) == Path(result['output_path']).read_bytes()


def test_qa_output_mutation_and_changed_binding_retries_refuse(tmp_path):
    call = args(tmp_path)
    result = midi_export(**call)
    path = Path(result['output_path'])
    original = path.read_bytes()
    with pytest.raises(PocketError, match='idempotency_conflict'):
        midi_export(**{**call, 'cc_step_bindings': [binding(controller=1)]})
    assert path.read_bytes() == original
    path.write_bytes(original + b'tampered')
    with pytest.raises(PocketError, match='destination changed'):
        midi_export(**call)
    assert path.read_bytes() == original + b'tampered'


def test_qa_smoothed_fractional_curve_requires_explicit_quantization_before_export(tmp_path):
    material = external_material()
    material['curves'][0]['target']['quantized'] = False
    material['curves'][0]['target']['values'] = []
    material['curves'][0]['points'][0]['value'] = 16.5
    with pytest.raises(PocketError):
        midi_export(**args(tmp_path, seal_literal(material), loss_policy='approved',
                          approved_losses=['canonical_expression_curves']))
    assert not (tmp_path / 'out.mid').exists()


def test_qa_extreme_finite_integer_curve_value_returns_domain_error(tmp_path):
    material = external_material()
    # This integer is valid JSON and does not hit Python's large-int text cap.
    # It remains outside the declared MIDI domain and must not cause traceback.
    material['curves'][0]['points'][0]['value'] = 10 ** 400
    with pytest.raises(PocketError):
        midi_export(**args(tmp_path, seal_literal(material)))
    assert not (tmp_path / 'out.mid').exists()


def external_binding(tmp_path, *, record=None, clip_id='clip:external', controller=11):
    record = copy.deepcopy(cc_curve() if record is None else record)
    record.pop('id', None)
    handle = put_record(record, tmp_path / 'store')
    return {'curve': handle, 'clip_id': clip_id, 'controller': controller,
            'same_tick_order': 'before_existing'}


def test_qa_public_curve_handle_exports_without_mutating_or_assembling_material(tmp_path):
    from pocket_music.curves import curve_transform

    fixture = cc_curve()
    creation = {'store_root': str(tmp_path / 'store'), 'request_id': 'public-curve-source',
                'target': fixture['target'], 'operations': [{'op': 'create', 'space': 'clip_qn',
                 'interpolation': 'step', 'points': fixture['points']}]}
    created = curve_transform(**creation)
    curve_handle = created['artifacts']['curve']
    material = literal_material()
    before_material = copy.deepcopy(material)
    before_curve = read_bytes(curve_handle, tmp_path / 'store')
    bindings = [{'curve': curve_handle, 'clip_id': material['clips'][0]['id'],
                 'controller': 11, 'same_tick_order': 'before_existing'}]
    result = midi_export(**args(tmp_path, material, cc_step_bindings=bindings))
    _, _, tracks = decode_wire(read_bytes(result['midi'], tmp_path / 'store'))
    assert [(time, wire) for _, time, wire in cc_rows(tracks)] == expected_cc()
    assert material == before_material and material['curves'] == []
    assert read_bytes(curve_handle, tmp_path / 'store') == before_curve
    sidecar = read_record(result['sidecar'], tmp_path / 'store')
    assert read_record(sidecar['material'], tmp_path / 'store') == material
    assert sidecar['cc_step_encoding']['bindings'][0]['curve'] == curve_handle
    assert read_record(curve_handle, tmp_path / 'store')['executable'] is False
    assert sidecar['losses'] == []


@pytest.mark.parametrize('mutation', ['unknown_clip', 'mixed_fields', 'no_clip', 'wrong_family', 'alias_uri'])
def test_qa_external_binding_identity_and_union_are_strict(tmp_path, mutation):
    b = external_binding(tmp_path)
    if mutation == 'unknown_clip': b['clip_id'] = 'other-clip:stale'
    elif mutation == 'mixed_fields': b['curve_id'] = CURVE_ID
    elif mutation == 'no_clip': b.pop('clip_id')
    elif mutation == 'wrong_family': b['curve']['artifact_schema'] = 'pocket.material/v1'
    else: b['curve']['artifact_uri'] = b['curve']['artifact_uri'].replace('/', '//')
    with pytest.raises(PocketError):
        midi_export(**args(tmp_path, literal_material(), cc_step_bindings=[b]))
    assert not (tmp_path / 'out.mid').exists()


@pytest.mark.parametrize('same_record', [True, False])
def test_qa_external_id_cannot_hide_unbound_embedded_expression(tmp_path, same_record):
    material = external_material()
    external = cc_curve()
    if not same_record:
        external['points'][0]['value'] = 99
    b = external_binding(tmp_path, record=external)
    with pytest.raises(PocketError):
        midi_export(**args(tmp_path, material, cc_step_bindings=[b]))
    assert not (tmp_path / 'out.mid').exists()


def test_qa_external_curve_channel_can_be_exported_without_any_generated_notes(tmp_path):
    material = literal_material()
    material['notes'] = []
    material['clips'][0]['note_ids'] = []
    material = seal_literal(material)
    b = external_binding(tmp_path)
    result = midi_export(**args(tmp_path, material, cc_step_bindings=[b]))
    _, _, tracks = decode_wire(read_bytes(result['midi'], tmp_path / 'store'))
    assert tracks == [[*expected_cc(), (Fraction(4), b'\xff\x2f')]]


def test_qa_external_curve_graph_survives_relocation_without_original_material_mutation(tmp_path):
    import shutil

    from pocket_music.artifact_store import _verify_handles

    material = literal_material()
    b = external_binding(tmp_path)
    curve_before = read_bytes(b['curve'], tmp_path / 'store')
    result = midi_export(**args(tmp_path, material, cc_step_bindings=[b]))
    moved = tmp_path / 'moved'
    shutil.copytree(tmp_path / 'store' / 'artifacts', moved / 'artifacts')
    _verify_handles(result['sidecar'], moved)
    sidecar = read_record(result['sidecar'], moved)
    assert read_bytes(sidecar['cc_step_encoding']['bindings'][0]['curve'], moved) == curve_before
    assert read_record(sidecar['material'], moved) == material
    assert material['curves'] == []
    (moved / b['curve']['artifact_uri']).write_bytes(b'corrupt copied provenance')
    with pytest.raises(PocketError):
        _verify_handles(result['sidecar'], moved)
    assert read_bytes(b['curve'], tmp_path / 'store') == curve_before


@pytest.mark.parametrize('format', ['smf0', 'smf1'])
def test_qa_large_valid_wire_ordinals_do_not_round_controller_precedence(tmp_path, format):
    material = external_material()
    material['notes'] = []
    material['clips'][0]['note_ids'] = []
    material['curves'][0]['points'] = [material['curves'][0]['points'][0]]
    material['events'] = [{'id': 'event:retained-controller', 'time': {'space': 'clip_qn', 'n': 0, 'd': 1},
                           'order': 10 ** 18, 'bytes': [0xB0, 7, 96], 'message_type': 'control_change',
                           'is_meta': False}]
    material['clips'][0]['event_ids'] = [material['events'][0]['id']]
    result = midi_export(**args(tmp_path, seal_literal(material), format=format))
    _, _, tracks = decode_wire(read_bytes(result['midi'], tmp_path / 'store'))
    assert tracks == [[(Fraction(0), b'\xb0\x0b\x10'), (Fraction(0), b'\xb0\x07\x60'),
                       (Fraction(4), b'\xff\x2f')]]


@pytest.mark.parametrize('format', ['smf0', 'smf1'])
def test_qa_shared_channel_control_cannot_claim_cross_track_precedence_in_smf1(tmp_path, format):
    material = literal_material()
    clip = copy.deepcopy(material['clips'][0])
    clip.update(id='controller-clip', note_ids=[], event_ids=[], curve_ids=[])
    material['clips'].append(clip)
    b = external_binding(tmp_path, clip_id='controller-clip')
    material = seal_literal(material)
    call = args(tmp_path, material, format=format, cc_step_bindings=[b])
    if format == 'smf1':
        with pytest.raises(PocketError):
            midi_export(**call)
        assert not (tmp_path / 'out.mid').exists()
    else:
        result = midi_export(**call)
        _, _, tracks = decode_wire(read_bytes(result['midi'], tmp_path / 'store'))
        assert len(tracks) == 1
        assert tracks[0][:2] == [(0, b'\xb0\x0b\x10'), (0, b'\x90\x3c\x50')]


def test_qa_distinct_channel_smf1_control_stays_in_exact_owning_track(tmp_path):
    material = literal_material()
    clip = copy.deepcopy(material['clips'][0])
    clip.update(id='controller-clip', note_ids=[], event_ids=[], curve_ids=[])
    material['clips'].append(clip)
    b = external_binding(tmp_path, record=cc_curve(channel=2), clip_id='controller-clip')
    result = midi_export(**args(tmp_path, seal_literal(material), format='smf1', cc_step_bindings=[b]))
    _, _, tracks = decode_wire(read_bytes(result['midi'], tmp_path / 'store'))
    assert len(tracks) == 2
    assert not cc_rows([tracks[0]])
    assert tracks[1] == [*expected_cc(channel=2), (Fraction(4), b'\xff\x2f')]
    assert result['coverage']['fidelity']['cc_step_encoding']['same_tick_scope'] == 'within_each_smf_track'


@pytest.mark.parametrize('format', ['smf0', 'smf1'])
@pytest.mark.parametrize('wire,kind', [([0xB0, 7, 96], 'control_change'), ([0xC0, 5], 'program_change'),
                                     ([0xE0, 0, 64], 'pitchwheel')])
@pytest.mark.parametrize('channel', [1, 2])
def test_qa_smf1_channel_ownership_checks_every_other_track_channel_message(tmp_path, format, wire, kind, channel):
    material = literal_material()
    material['notes'] = []
    material['clips'][0]['note_ids'] = []
    event = {'id': 'event:other-track', 'time': {'space': 'clip_qn', 'n': 0, 'd': 1},
             'order': 0, 'bytes': [wire[0] + channel - 1, *wire[1:]], 'message_type': kind, 'is_meta': False}
    material['events'] = [event]
    material['clips'][0]['event_ids'] = [event['id']]
    clip = copy.deepcopy(material['clips'][0])
    clip.update(id='controller-clip', event_ids=[])
    material['clips'].append(clip)
    b = external_binding(tmp_path, clip_id='controller-clip')
    material = seal_literal(material)
    before = copy.deepcopy(material)
    call = args(tmp_path, material, format=format, cc_step_bindings=[b])
    if format == 'smf1' and channel == 1:
        with pytest.raises(PocketError, match='another clip/track'):
            midi_export(**call)
        assert not (tmp_path / 'out.mid').exists()
    else:
        result = midi_export(**call)
        _, _, tracks = decode_wire(read_bytes(result['midi'], tmp_path / 'store'))
        retained = bytes(event['bytes'])
        if format == 'smf0':
            assert tracks[0][:2] == [(0, b'\xb0\x0b\x10'), (0, retained)]
        else:
            assert tracks[0] == [(0, retained), (4, b'\xff\x2f')]
            assert tracks[1] == [*expected_cc(), (4, b'\xff\x2f')]
    assert material == before
