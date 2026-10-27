# SPDX-License-Identifier: AGPL-3.0-only
"""Declared offline lifecycle checks; no receiver, native or performance qualification."""
from __future__ import annotations

import copy
import hashlib
import json
from fractions import Fraction

import pytest
from test_midi_qa import literal_material, seal_literal, smf, vlq

from pocket_music.artifact_store import read_record
from pocket_music.errors import PocketError
from pocket_music.material import material_import, qn
from pocket_music.midi_lifecycle import midi_lifecycle


def fixture(*controls, duration=2):
    record = literal_material()
    record['notes'] = record['notes'][:1]
    record['notes'][0]['duration_qn'] = qn(duration)
    record['clips'][0]['note_ids'] = ['note:0']
    record['clips'][0]['length_qn'] = qn(8)
    record['events'] = [{'id': f'cc:{index}', 'time': {'space': 'clip_qn', **qn(time)},
        'order': index, 'track_index': 0, 'message_type': 'control_change', 'is_meta': False,
        'bytes': [176, controller, value]} for index, (time, controller, value) in enumerate(controls)]
    record['clips'][0]['event_ids'] = [event['id'] for event in record['events']]
    return seal_literal(record)


def analyze(record, tmp_path, *, request='lifecycle', **updates):
    arguments = {'material': record, 'clip_id': 'clip:external',
        'initial_state': {'active_notes': 'none', 'sustain': [{'channel': 1, 'value': 0}]},
        'horizon_qn': 6, 'release_tail_qn': 1, 'equal_time_order': 'note_off_cc_note_on',
        'source_basis': 'canonical_notes_retained_cc64', 'store_root': str(tmp_path / 'store'), 'request_id': request,
        **updates}
    result = midi_lifecycle(**arguments)
    return result, read_record(result['report'], tmp_path / 'store')


def test_sustain_release_tail_and_conservative_horizon(tmp_path):
    result, report = analyze(fixture((1, 64, 127), (4, 64, 0)), tmp_path)
    note = report['notes'][0]
    assert note['gate_off_qn'] == qn(2) and note['release_qn'] == qn(4)
    assert note['reserved_until_qn'] == qn(5) and note['state_at_horizon'] == 'released'
    assert result['coverage']['receiver_behavior'] == 'unknown'
    assert result['coverage']['allocation'] == 'not_performed'
    assert report['controller_coordinates']['basis'] == 'caller_declared_clip_qn'
    _, held = analyze(fixture(), tmp_path, request='held',
        initial_state={'active_notes': 'none', 'sustain': [{'channel': 1, 'value': 127}]})
    assert held['notes'][0]['release_qn'] is None and held['notes'][0]['reserved_until_qn'] is None
    assert held['notes'][0]['unresolved_reason'] == 'sustain_held_at_horizon'
    assert held['reserved_through_horizon_note_ids'] == ['note:0']


def test_half_open_gate_tail_and_equal_time_policy(tmp_path):
    _, off_first = analyze(fixture((2, 64, 127)), tmp_path, horizon_qn=2)
    assert off_first['notes'][0]['release_qn'] == qn(2)
    _, pedal_first = analyze(fixture((2, 64, 127)), tmp_path, request='pedal', horizon_qn=2,
                            equal_time_order='cc_note_off_note_on')
    assert pedal_first['notes'][0]['release_qn'] is None
    _, future = analyze(fixture((4, 64, 0)), tmp_path, request='future', horizon_qn=1)
    assert future['notes'][0]['state_at_horizon'] == 'active_gate'
    assert future['notes'][0]['release_qn'] is None
    _, tail = analyze(fixture(), tmp_path, request='tail', horizon_qn=3)
    assert tail['notes'][0]['reserved_until_qn'] == qn(3)
    assert tail['notes'][0]['state_at_horizon'] == 'released'


def test_ordered_same_tick_pedal_up_then_down_does_not_rehold(tmp_path):
    record = fixture((1, 64, 127), (4, 64, 0), (4, 64, 127))
    record['events'][1]['id'], record['events'][2]['id'] = 'z:last-lexical', 'a:first-lexical'
    record['clips'][0]['event_ids'] = [event['id'] for event in record['events']]
    _, report = analyze(seal_literal(record), tmp_path)
    assert report['notes'][0]['release_qn'] == qn(4)
    assert report['channels'][0]['final_sustain'] == 127
    assert [row['value'] for row in report['cc64_realization']] == [127, 0, 127]


def test_same_pitch_reservations_and_exact_reuse_boundary(tmp_path):
    record = fixture(duration=1)
    second = {**copy.deepcopy(record['notes'][0]), 'id': 'note:second', 'onset': {'space': 'clip_qn', **qn(2)}}
    record['notes'].append(second)
    record['clips'][0]['note_ids'].append(second['id'])
    _, exact = analyze(seal_literal(record), tmp_path)
    assert exact['summary']['ambiguous_attacks'] == 0
    record['notes'][1]['onset'] = {'space': 'clip_qn', **qn(Fraction(3, 2))}
    _, overlap = analyze(seal_literal(record), tmp_path, request='overlap')
    assert overlap['summary']['ambiguous_attacks'] == 1
    assert overlap['channels'][0]['receiver_pairing_ambiguous'] is True
    assert {row['note_id'] for row in overlap['notes']} == {'note:0', 'note:second'}


@pytest.mark.parametrize('state', [{'active_notes': 'unknown', 'sustain': [{'channel': 1, 'value': 0}]},
    {'active_notes': 'none', 'sustain': []}, {'active_notes': 'none', 'sustain': [{'channel': 2, 'value': 0}]},
    {'active_notes': 'none', 'sustain': [{'channel': 1, 'value': 0}, {'channel': 1, 'value': 0}]},
    {'active_notes': 'none', 'sustain': [{'channel': True, 'value': 0}]},
    {'active_notes': 'none', 'sustain': [{'channel': 1, 'value': True}]},
    {'active_notes': 'none', 'sustain': [{'channel': 1, 'value': 128}]},
    {'active_notes': 'none', 'sustain': [{'channel': 1, 'value': 0}], 'hidden': 1}])
def test_initial_states_strict_and_complete(tmp_path, state):
    with pytest.raises(PocketError):
        analyze(fixture(), tmp_path, initial_state=state)


@pytest.mark.parametrize('controller', [66, 69, *range(120, 128)])
def test_unsupported_receiver_lifecycle_controls_refuse(tmp_path, controller):
    with pytest.raises(PocketError, match='unsupported'):
        analyze(fixture((1, controller, 0)), tmp_path)
    _, bounded = analyze(fixture((7, controller, 127)), tmp_path, request='future')
    assert bounded['notes'][0]['release_qn'] == qn(2)


@pytest.mark.parametrize('value', [True, 0.5, {'n': 2, 'd': 4}, {'n': 1, 'd': 2, 'space': 'seconds'}])
def test_rational_units_strict(tmp_path, value):
    with pytest.raises(PocketError):
        analyze(fixture(), tmp_path, horizon_qn=value)


def imported(tmp_path, *, extra_track=False):
    data = (b'\x00\x90\x3c\x50' + vlq(480) + b'\xb0\x40\x7f' + vlq(480)
            + b'\x80\x3c\x25' + vlq(960) + b'\xb0\x40\x00\x00\xff\x2f\x00')
    tracks = [data]
    if extra_track:
        tracks.append(b'\x00\xb0\x40\x7f\x00\xff\x2f\x00')
    payload = smf(tracks, format=1 if extra_track else 0)
    path = tmp_path / ('shared.mid' if extra_track else 'input.mid')
    path.write_bytes(payload)
    result = material_import({'kind': 'smf', 'path': str(path),
                              'expected_sha256': hashlib.sha256(payload).hexdigest()}, str(tmp_path / 'store'), 'import')
    return read_record(result['material'], tmp_path / 'store')


def test_imported_controller_coordinates_use_current_canonical_gates(tmp_path):
    parent = imported(tmp_path)
    original = copy.deepcopy(parent)
    parent['notes'][0]['duration_qn'] = qn(Fraction(1, 2))
    _, report = analyze(seal_literal(parent), tmp_path, clip_id=parent['clips'][0]['id'])
    assert report['notes'][0]['gate_off_qn'] == qn(Fraction(1, 2))
    assert report['notes'][0]['release_qn'] == qn(Fraction(1, 2))
    assert len(report['original_note_events_excluded']) == 2
    assert report['controller_coordinates']['source_reparse'] == 'wire_event_positions'
    assert parent['events'] == original['events']


@pytest.mark.parametrize('change', ['all_events', 'cc', 'tick', 'time', 'bytes', 'origin', 'track'])
def test_source_evidence_omission_or_coordinate_forgery_refused(tmp_path, change):
    parent = imported(tmp_path)
    clip = parent['clips'][0]
    cc = next(event for event in parent['events'] if event['message_type'] == 'control_change')
    if change == 'all_events':
        parent['events'] = []
        clip['event_ids'] = []
    elif change == 'cc':
        parent['events'].remove(cc)
        clip['event_ids'].remove(cc['id'])
    elif change == 'tick':
        cc['source_tick'] += 1
    elif change == 'time':
        cc['time'] = {'space': 'clip_qn', **qn(Fraction(3, 2))}
    elif change == 'bytes':
        cc['bytes'][2] = 0
    elif change == 'origin':
        clip['origin'] = {'space': 'phrase_qn', **qn(1)}
    else:
        parent['tracks'][0]['source_track'] = True
    with pytest.raises(PocketError):
        analyze(seal_literal(parent), tmp_path, clip_id=clip['id'])


def test_other_source_track_shared_channel_refuses_even_if_omitted(tmp_path):
    parent = imported(tmp_path, extra_track=True)
    clip = parent['clips'][0]
    removed = set(parent['clips'][1]['event_ids'])
    parent['events'] = [event for event in parent['events'] if event['id'] not in removed]
    parent['clips'] = parent['clips'][:1]
    parent['tracks'] = parent['tracks'][:1]
    with pytest.raises(PocketError, match='shared-channel'):
        analyze(seal_literal(parent), tmp_path, clip_id=clip['id'])


def test_optional_codec_not_required_and_receipt_is_bounded(tmp_path, monkeypatch):
    import builtins
    original = builtins.__import__

    def restricted(name, *args, **kwargs):
        assert not name.startswith(('mido', 'torch', 'pocket_music.native'))
        return original(name, *args, **kwargs)

    parent = imported(tmp_path)
    monkeypatch.setattr(builtins, '__import__', restricted)
    result, _ = analyze(parent, tmp_path, clip_id=parent['clips'][0]['id'])
    assert len((json.dumps(result, indent=2) + '\n').encode()) <= 16384


def test_huge_note_identity_omitted_from_receipt_retained_in_proof(tmp_path):
    record = fixture()
    note_id = 'n' * 20000
    record['notes'][0]['id'] = note_id
    record['clips'][0]['note_ids'] = [note_id]
    result, report = analyze(seal_literal(record), tmp_path)
    assert result['notes'] == [] and result['omitted_notes'] == 1
    assert report['notes'][0]['note_id'] == note_id
    assert len((json.dumps(result, indent=2) + '\n').encode()) <= 16384


def test_retry_conflict_and_source_preservation(tmp_path):
    record = fixture((1, 64, 127), (4, 64, 0))
    original = copy.deepcopy(record)
    result, _ = analyze(record, tmp_path)
    replay, _ = analyze(record, tmp_path)
    assert replay == result and record == original
    with pytest.raises(PocketError, match='idempotency'):
        analyze(record, tmp_path, release_tail_qn=2)
