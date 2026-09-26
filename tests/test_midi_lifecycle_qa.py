# SPDX-License-Identifier: AGPL-3.0-only
"""Independent symbolic pedal lifecycle expectations; never receiver acceptance."""
from __future__ import annotations

import copy
import json

import pytest
from test_midi_qa import import_wire, seal_literal, smf, vlq
from test_midi_relationships_qa import material, note, q

from pocket_music.artifact_store import read_record
from pocket_music.errors import PocketError
from pocket_music.midi_lifecycle import midi_lifecycle


def control(key, at, value, *, order=0, channel=1, cc=64, track=0):
    return {'id': key, 'time': {'space': 'clip_qn', **q(at)}, 'order': order,
            'bytes': [0xB0 + channel - 1, cc, value], 'is_meta': False,
            'message_type': 'control_change', 'track_index': track}


def phrase(notes=None, controls=None):
    record = material([note('n', 0, 2)] if notes is None else notes, length=16)
    record['events'] = [] if controls is None else controls
    record['clips'][0]['event_ids'] = [e['id'] for e in record['events']]
    return seal_literal(record)


def arguments(record, tmp_path, **changes):
    return {'material': record, 'clip_id': record['clips'][0]['id'],
            'initial_state': {'active_notes': 'none', 'sustain': [{'channel': 1, 'value': 0}]},
            'horizon_qn': 6, 'release_tail_qn': 1, 'equal_time_order': 'note_off_cc_note_on',
            'source_basis': 'canonical_notes_retained_cc64', 'store_root': str(tmp_path / 'store'),
            'request_id': 'lifecycle', **changes}


@pytest.mark.parametrize('changes', [
    {'clip_id': 'unknown'}, {'horizon_qn': 0}, {'horizon_qn': True}, {'horizon_qn': 1.0},
    {'release_tail_qn': -1}, {'release_tail_qn': {'n': 2, 'd': 4}},
    {'horizon_qn': {'space': 'seconds', 'n': 6, 'd': 1}}, {'source_basis': 'raw_notes'},
    {'equal_time_order': 'infer'}, {'initial_state': {'active_notes': 'none', 'sustain': []}},
    {'initial_state': {'active_notes': 'none', 'sustain': [{'channel': True, 'value': 0}]}},
    {'initial_state': {'active_notes': 'none', 'sustain': [{'channel': 1, 'value': 0.0}]}},
    {'initial_state': {'active_notes': 'none', 'sustain': [{'channel': 1, 'value': 128}]}},
    {'initial_state': {'active_notes': 'none', 'sustain': [{'channel': 1, 'value': 0}, {'channel': 1, 'value': 0}]}},
    {'initial_state': {'active_notes': 'unknown', 'sustain': [{'channel': 1, 'value': 0}]}},
    {'initial_state': {'active_notes': 'none', 'sustain': [{'channel': 1, 'value': 0}], 'extra': True}},
])
def test_qa_lifecycle_strict_initial_clock_and_basis(tmp_path, changes):
    with pytest.raises(PocketError):
        midi_lifecycle(**arguments(phrase(), tmp_path, **changes))
    assert not list((tmp_path / 'store' / 'artifacts').glob('*'))


@pytest.mark.parametrize('cc', [66, 69, 120, 121, 122, 123, 124, 125, 126, 127])
def test_qa_lifecycle_unsupported_pedal_and_channel_modes_refuse(tmp_path, cc):
    record = phrase(controls=[control('unsupported', 1, 0, cc=cc)])
    with pytest.raises(PocketError):
        midi_lifecycle(**arguments(record, tmp_path))


def test_qa_lifecycle_pickup_and_same_tick_multitrack_refuse(tmp_path):
    pickup = phrase(notes=[note('n', -1, 2)])
    with pytest.raises(PocketError):
        midi_lifecycle(**arguments(pickup, tmp_path, request_id='pickup'))
    ambiguous = phrase(controls=[control('down', 1, 127, order=0),
                                control('up', 1, 0, order=1, track=1)])
    with pytest.raises(PocketError):
        midi_lifecycle(**arguments(ambiguous, tmp_path, request_id='ambiguous'))


def analyze(record, tmp_path, **changes):
    args = arguments(record, tmp_path, **changes)
    result = midi_lifecycle(**args)
    proof = read_record(result['report'], args['store_root'])
    return result, proof


def test_qa_lifecycle_delayed_release_and_declared_tail_are_exact(tmp_path):
    record = phrase(controls=[control('down', 1, 127), control('up', 4, 0, order=1)])
    before = copy.deepcopy(record)
    result, proof = analyze(record, tmp_path)
    row = proof['notes'][0]
    assert row['gate_off_qn'] == q(2)
    assert row['release_qn'] == q(4) and row['reserved_until_qn'] == q(5)
    assert row['state_at_horizon'] == 'released' and row['unresolved_reason'] is None
    assert record == before
    for field, wanted in {'basis': 'symbolic', 'source_basis': 'canonical_notes_retained_cc64',
            'native_verified': False, 'receiver_behavior': 'unknown', 'encoding': 'not_performed',
            'allocation': 'not_performed', 'performance': 'not_performed', 'listening': 'not_performed'}.items():
        assert result['coverage'][field] == wanted


@pytest.mark.parametrize('horizon,initial,expected_state,reason', [
    (1, 0, 'active_gate', 'gate_active_at_horizon'),
    (6, 127, 'sustain_held', 'sustain_held_at_horizon'),
])
def test_qa_lifecycle_horizon_censors_intent_without_invented_release(tmp_path, horizon, initial, expected_state, reason):
    _, proof = analyze(phrase(), tmp_path, horizon_qn=horizon,
                       initial_state={'active_notes': 'none', 'sustain': [{'channel': 1, 'value': initial}]})
    row = proof['notes'][0]
    assert row['gate_off_qn'] == q(2)
    assert row['release_qn'] is None and row['reserved_until_qn'] is None
    assert row['state_at_horizon'] == expected_state and row['unresolved_reason'] == reason


@pytest.mark.parametrize('order,released', [('note_off_cc_note_on', True), ('cc_note_off_note_on', False)])
def test_qa_lifecycle_same_time_explicit_canonical_endpoint_policy(tmp_path, order, released):
    _, proof = analyze(phrase(controls=[control('down', 2, 127)]), tmp_path, equal_time_order=order)
    assert proof['notes'][0]['release_qn'] == (q(2) if released else None)
    assert proof['notes'][0]['state_at_horizon'] == ('released' if released else 'sustain_held')


def test_qa_lifecycle_same_tick_controller_order_not_identifier_sort(tmp_path):
    record = phrase(controls=[control('z-up', 4, 0, order=0), control('a-down', 4, 127, order=1)])
    _, proof = analyze(record, tmp_path, horizon_qn=4,
                       initial_state={'active_notes': 'none', 'sustain': [{'channel': 1, 'value': 127}]})
    assert proof['notes'][0]['release_qn'] == q(4)
    assert proof['notes'][0]['reserved_until_qn'] == q(5)
    assert proof['notes'][0]['state_at_horizon'] == 'tail_reserved'
    assert proof['channels'][0]['final_sustain'] == 127


@pytest.mark.parametrize('pedal,released', [(0, True), (63, True), (64, False), (127, False)])
def test_qa_lifecycle_binary_threshold_explicit_not_half_pedal(tmp_path, pedal, released):
    _, proof = analyze(phrase(), tmp_path,
                       initial_state={'active_notes': 'none', 'sustain': [{'channel': 1, 'value': pedal}]})
    assert proof['notes'][0]['release_qn'] == (q(2) if released else None)


@pytest.mark.parametrize('onset,pitch,ambiguous', [(1, 60, True), (2, 60, True), (3, 60, False), (1, 67, False)])
def test_qa_lifecycle_receiver_ambiguity_includes_tail_but_not_touching(tmp_path, onset, pitch, ambiguous):
    record = phrase(notes=[note('a', 0, 2), note('b', onset, 1, pitch)])
    _, proof = analyze(record, tmp_path)
    assert proof['channels'][0]['receiver_pairing_ambiguous'] is ambiguous
    assert {row['note_id'] for row in proof['notes']} == {'a', 'b'}


def test_qa_lifecycle_after_horizon_controller_never_releases_now(tmp_path):
    record = phrase(controls=[control('future-up', 7, 0)])
    _, proof = analyze(record, tmp_path,
                       initial_state={'active_notes': 'none', 'sustain': [{'channel': 1, 'value': 127}]})
    assert proof['notes'][0]['release_qn'] is None
    assert proof['notes'][0]['state_at_horizon'] == 'sustain_held'


def test_qa_lifecycle_retry_tamper_and_full_report_bounded(tmp_path):
    record = phrase(notes=[note('note:' + '𝄞' * 1000 + str(i), i * 2, 1) for i in range(50)])
    args = arguments(record, tmp_path, horizon_qn=100)
    result = midi_lifecycle(**args)
    assert midi_lifecycle(**args) == result
    assert len((json.dumps(result, ensure_ascii=True, indent=2) + '\n').encode()) <= 16384
    proof = read_record(result['report'], args['store_root'])
    assert len(proof['notes']) == 50
    with pytest.raises(PocketError, match='idempotency_conflict'):
        midi_lifecycle(**{**args, 'release_tail_qn': 2})
    (tmp_path / 'store' / result['report']['artifact_uri']).write_bytes(b'corrupt')
    with pytest.raises(PocketError, match='integrity'):
        midi_lifecycle(**args)


def imported_sustain(tmp_path):
    payload = smf([(b'\x00\x90\x3c\x50' + vlq(480) + b'\xb0\x40\x7f'
                    + vlq(480) + b'\x80\x3c\x25' + vlq(960) + b'\xb0\x40\x00'
                    + vlq(480) + b'\xff\x2f\x00')], 480, format=0)
    handle = import_wire(tmp_path, payload)
    return read_record(handle, str(tmp_path / 'store'))


def test_qa_lifecycle_retained_wire_cannot_disguise_pedal_as_meta(tmp_path):
    record = imported_sustain(tmp_path)
    down = next(e for e in record['events'] if e['bytes'] == [0xB0, 64, 127])
    down.update(bytes=[0xFF, 1, 0], is_meta=True, message_type='text')
    record = seal_literal(record)
    with pytest.raises(PocketError):
        midi_lifecycle(**arguments(record, tmp_path))


def test_qa_lifecycle_retained_raw_endpoints_do_not_override_current_canonical_gate(tmp_path):
    record = imported_sustain(tmp_path)
    _, original = analyze(record, tmp_path, request_id='original')
    assert original['notes'][0]['release_qn'] == q(4)
    changed = copy.deepcopy(record)
    changed['notes'][0]['duration_qn'] = q(1)
    changed = seal_literal(changed)
    _, proof = analyze(changed, tmp_path, request_id='changed')
    assert proof['notes'][0]['gate_off_qn'] == q(1)
    assert proof['notes'][0]['release_qn'] == q(1)
    assert proof['original_note_events_excluded']
    assert proof['controller_coordinates']['source_reparse'] == 'wire_event_positions'
    assert changed['events'] == record['events']


def test_qa_lifecycle_muted_future_and_boundary_attacks_are_distinct(tmp_path):
    muted = note('muted', 0, 2)
    muted['mute'] = True
    record = phrase(notes=[muted, note('boundary', 6, 1, 67), note('future', 7, 1, 72)])
    _, proof = analyze(record, tmp_path)
    assert [row['note_id'] for row in proof['notes']] == ['boundary']
    assert proof['notes'][0]['state_at_horizon'] == 'active_gate'
    assert {(row['note_id'], row['reason']) for row in proof['excluded_notes']} == {
        ('muted', 'muted'), ('future', 'after_horizon')}


def test_qa_lifecycle_source_controller_coordinate_forgery_refuses(tmp_path):
    record = imported_sustain(tmp_path)
    down = next(e for e in record['events'] if e['bytes'] == [176, 64, 127])
    down['time'] = {'space': 'clip_qn', **q(3)}
    with pytest.raises(PocketError):
        midi_lifecycle(**arguments(seal_literal(record), tmp_path))
