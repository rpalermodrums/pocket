"""Independent expression-allocation numerics and source-preserving fixtures."""
from __future__ import annotations

import copy
import json
from fractions import Fraction

import pytest
from test_midi_lifecycle_qa import control, phrase
from test_midi_qa import seal_literal
from test_midi_relationships_qa import material, note, q

from pocket_music.artifact_store import put_record, read_record
from pocket_music.errors import PocketError
from pocket_music.midi_expression import midi_expression_plan
from pocket_music.midi_lifecycle import midi_lifecycle


def expression_curve(key, note_id, dimension, points):
    pitch = dimension == 'pitch'
    return {'schema': 'pocket.curve/v1', 'id': key, 'curve_id': key,
            'target': {'kind': f'per_note_{dimension}', 'target_id': f'{dimension}:{note_id}',
                       'scope': 'note', 'unit': 'cents' if pitch else 'normalized',
                       'value_min': -4800 if pitch else 0, 'value_max': 4800 if pitch else 1,
                       'quantized': False, 'values': [], 'ownership': 'none',
                       'value_mode': 'additive' if pitch else 'absolute',
                       'note_id': note_id, 'resize_policy': 'stretch_with_gate'},
            'space': 'note_relative_qn', 'interpolation': 'step',
            'points': [{'time': q(at), 'value': value, 'order': order}
                       for order, (at, value) in enumerate(points)],
            'parent': None, 'context': None, 'executable': False,
            'provenance': {'provider': 'external-independent-fixture'}}


def expressive_phrase(cents=30):
    record = material([note('a', 0, 8, 60), note('b', 0, 8, 67)], length=8)
    curves = [expression_curve('curve:a:pitch', 'a', 'pitch', [(0, 0), (3, cents), (8, 0)]),
              expression_curve('curve:a:pressure', 'a', 'pressure', [(0, 0.15), (3, 0.4), (8, 0.15)]),
              expression_curve('curve:a:slide', 'a', 'slide', [(0, 0), (3, 1)])]
    record['curves'] = curves
    record['notes'][0]['expression_refs'] = [row['id'] for row in curves]
    record['clips'][0]['curve_ids'] = [row['id'] for row in curves]
    return seal_literal(record)


def lifecycle(record, *, horizon=8, tail=0):
    return {'clip_id': record['clips'][0]['id'],
            'initial_state': {'active_notes': 'none', 'sustain': [{'channel': 1, 'value': 0}]},
            'horizon_qn': horizon, 'release_tail_qn': tail,
            'equal_time_order': 'note_off_cc_note_on', 'source_basis': 'canonical_notes_retained_cc64'}


def encoding(**changes):
    return {'source_channel_policy': 'single_source_channel_to_zone', 'sustain_route': 'manager_cc64',
            'allocation': 'lowest_available_zone_order', 'exhaustion': 'reject_no_stealing',
            'reservation': 'through_symbolic_release_plus_declared_tail',
            'same_pitch_policy': 'new_per_note_channel_realization', 'tuning': 'tuning:12tet-a440',
            'neutral': {'pitch_cents': 0, 'pressure_7bit': 0, 'slide_7bit': 64},
            'curve_mode': 'step_only', 'rounding': 'nearest_ties_even',
            'max_pitch_error_cents': q(Fraction(1, 8)),
            'max_control_error_normalized': q(Fraction(1, 500)),
            'reuse_order': 'old_expression_then_declared_off_cc_then_reset_setup_on', **changes}


def with_curve_change(record, curve_index, change):
    modified = copy.deepcopy(record)
    change(modified['curves'][curve_index])
    return seal_literal(modified)


def receiver(**changes):
    return {'label': 'Explicit unverified fixture receiver', 'zone': 'lower', 'manager_channel': 1,
            'member_channels': [2, 3], 'member_bend_range_semitones': 48,
            'manager_pitch_policy': 'neutral_no_pitch_messages',
            'configuration_policy': 'assume_preconfigured_no_setup_messages',
            'configuration_status': 'declared_unverified',
            'attribution': {'actor': 'Independent fixture author', 'actor_kind': 'agent',
                            'statement': 'Assumed configuration only; no receiver tested.', 'evidence': []},
            'instrument_state': None, **changes}


def arguments(record, tmp_path, **changes):
    return {'material': record, 'lifecycle': lifecycle(record), 'receiver_assumption': receiver(),
            'encoding': encoding(), 'store_root': str(tmp_path / 'store'), 'request_id': 'expression', **changes}


def run(record, tmp_path, **changes):
    args = arguments(record, tmp_path, **changes)
    result = midi_expression_plan(**args)
    return result, read_record(result['plan'], args['store_root'])


def test_qa_expression_30_cent_and_controls_independent_exact_proof(tmp_path):
    record = expressive_phrase()
    before = copy.deepcopy(record)
    result, plan = run(record, tmp_path)
    assert [(row['note_id'], row['member_channel']) for row in plan['allocations']] == [('a', 2), ('b', 3)]
    pitch = next(row for row in plan['quantization'] if row['curve_id'] == 'curve:a:pitch' and row['time_qn'] == q(3))
    assert pitch['encoded_unsigned14'] == 8243
    assert pitch['requested'] == q(30) and pitch['decoded'] == q(Fraction(3825, 128))
    assert pitch['absolute_error'] == q(Fraction(15, 128))
    pressure = [row for row in plan['quantization'] if row['curve_id'] == 'curve:a:pressure']
    assert [row['encoded_7bit'] for row in pressure] == [19, 51, 19]
    assert [row['absolute_error'] for row in pressure] == [q(Fraction(1, 2540)), q(Fraction(1, 635)), q(Fraction(1, 2540))]
    assert any(row['time_qn'] == q(3) and row['bytes'] == [0xE1, 51, 64] for row in plan['events'])
    assert any(row['time_qn'] == q(3) and row['bytes'] == [0xB1, 74, 127] for row in plan['events'])
    assert not any(row['time_qn'] == q(3) and row['bytes'][0] & 15 == 2 for row in plan['events'])
    assert record == before
    assert read_record(plan['material'], str(tmp_path / 'store')) == record
    assert result['coverage']['native_verified'] is False
    assert result['coverage']['receiver_configuration'] == 'declared_unverified'
    assert result['coverage']['encoding'] == 'planned_not_exported'
    assert result['coverage']['listening'] == 'not_performed'
    assert not any(row['bytes'][0] >= 240 for row in plan['events'])
    assert not any(row['bytes'][0] >> 4 == 11 and row['bytes'][1] not in (64, 74) for row in plan['events'])


@pytest.mark.parametrize('cents,unsigned,decoded,error', [
    (-30, 8141, Fraction(-3825, 128), Fraction(15, 128)),
    (4800, 16383, Fraction(614325, 128), Fraction(75, 128)),
    (-4800, 0, -4800, 0),
    (0.29296875, 8192, 0, Fraction(75, 256)),
    (0.87890625, 8194, Fraction(75, 64), Fraction(75, 256)),
])
def test_qa_expression_bend_asymmetry_and_exact_ties_even(tmp_path, cents, unsigned, decoded, error):
    _, plan = run(expressive_phrase(cents), tmp_path, encoding=encoding(max_pitch_error_cents=1))
    proof = next(row for row in plan['quantization'] if row['curve_id'] == 'curve:a:pitch' and row['time_qn'] == q(3))
    assert proof['encoded_unsigned14'] == unsigned
    assert proof['decoded'] == q(decoded) and proof['absolute_error'] == q(error)


@pytest.mark.parametrize('changes', [
    {'max_pitch_error_cents': q(Fraction(1, 10))},
    {'max_control_error_normalized': q(Fraction(1, 1000))},
])
def test_qa_expression_explicit_quantization_tolerance_refuses(tmp_path, changes):
    with pytest.raises(PocketError):
        run(expressive_phrase(), tmp_path, encoding=encoding(**changes))
    schemas = [json.loads(p.read_text())['schema'] for p in (tmp_path / 'store' / 'artifacts').glob('*/record.json')]
    assert 'pocket.midi-expression-plan/v1' not in schemas


def test_qa_expression_range_overflow_never_clamps_even_with_tolerance(tmp_path):
    record = expressive_phrase(4800)
    record['notes'][0]['pitch']['cents_offset'] = 0.1
    with pytest.raises(PocketError):
        run(seal_literal(record), tmp_path, encoding=encoding(max_pitch_error_cents=100))


def test_qa_expression_upper_zone_stable_identity_and_samepitch_channels(tmp_path):
    record = material([note('b', 0, 1), note('a', 0, 1)], length=1)
    _, plan = run(record, tmp_path, lifecycle=lifecycle(record, horizon=1),
                  receiver_assumption=receiver(zone='upper', manager_channel=16, member_channels=[15, 14]))
    assert [(row['note_id'], row['member_channel']) for row in plan['allocations']] == [('a', 15), ('b', 14)]
    assert plan['source_receiver_ambiguity']
    assert [row['bytes'][0] for row in plan['events'] if row['purpose'] == 'note_on'] == [0x9E, 0x9D]
    assert all(row['bytes'][0] == 0xBF for row in plan['events'] if row['purpose'] == 'initial_manager_sustain')
    with pytest.raises(PocketError, match='exhaust'):
        run(record, tmp_path, lifecycle=lifecycle(record, horizon=1),
            receiver_assumption=receiver(member_channels=[2]), request_id='exhausted')


def test_qa_expression_sustain_tail_reuse_and_reset_order(tmp_path):
    record = phrase(notes=[note('a', 0, 2, 60), note('b', 5, 1, 67)],
                    controls=[control('down', 1, 127), control('up', 4, 0, order=1)])
    record['clips'][0]['length_qn'] = q(7)
    record = seal_literal(record)
    _, plan = run(record, tmp_path, lifecycle=lifecycle(record, horizon=7, tail=1),
                  receiver_assumption=receiver(member_channels=[2]))
    assert [(row['member_channel'], row['release_qn'], row['reserved_until_qn']) for row in plan['allocations']] == [
        (2, q(4), q(5)), (2, q(6), q(7))]
    resets = [row for row in plan['events'] if row['purpose'] == 'member_release_reset' and row['note_id'] == 'a']
    assert len(resets) == 3 and all(row['time_qn'] == q(5) for row in resets)
    at_five = [row['purpose'] for row in plan['events'] if row['time_qn'] == q(5)]
    assert at_five == ['member_release_reset'] * 3 + ['member_initial_neutral'] * 3 + ['note_initial_expression', 'note_on']
    record['notes'][1]['onset'] = {'space': 'clip_qn', **q(Fraction(4999, 1000))}
    with pytest.raises(PocketError, match='exhaust'):
        run(seal_literal(record), tmp_path, lifecycle=lifecycle(record, horizon=7, tail=1),
            receiver_assumption=receiver(member_channels=[2]), request_id='too-early')


def test_qa_expression_final_curve_points_before_off_and_discontinuity_order(tmp_path):
    record = expressive_phrase()
    curve = record['curves'][0]
    curve['points'].insert(2, {'time': q(3), 'value': -30, 'order': 3})
    curve['points'][-1]['order'] = 4
    record = seal_literal(record)
    _, plan = run(record, tmp_path)
    bends = [row['bytes'] for row in plan['events'] if row['curve_id'] == curve['id'] and row['time_qn'] == q(3)]
    assert bends == [[0xE1, 51, 64], [0xE1, 77, 63]]
    at_end = [row for row in plan['events'] if row['time_qn'] == q(8)]
    assert max(row['order'] for row in at_end if row['purpose'] == 'note_expression') < min(
        row['order'] for row in at_end if row['purpose'] == 'note_off')
    assert [(row['note_id'], row['bytes'][2]) for row in at_end if row['purpose'] == 'note_off'] == [('a', 37), ('b', 37)]


@pytest.mark.parametrize('change', [
    lambda curve: curve.update(interpolation='linear'),
    lambda curve: curve['points'][0].update(time=q(1)),
    lambda curve: curve['target'].update(ownership='host_automation'),
    lambda curve: curve['target'].update(value_mode='absolute'),
    lambda curve: curve['target'].update(unit='normalized'),
    lambda curve: curve['target'].update(resize_policy='preserve_ms'),
])
def test_qa_expression_unsupported_curve_profiles_refuse(tmp_path, change):
    with pytest.raises(PocketError):
        run(with_curve_change(expressive_phrase(), 0, change), tmp_path)


@pytest.mark.parametrize('field,value', [
    ('manager_channel', True), ('manager_channel', 2), ('member_channels', [2, 4]),
    ('member_channels', [2, 2]), ('member_channels', [1, 2]), ('member_channels', [2.0, 3]),
    ('member_bend_range_semitones', 0), ('member_bend_range_semitones', 97),
    ('configuration_status', 'native_verified'), ('configuration_policy', 'configure_receiver'),
])
def test_qa_expression_receiver_assumptions_are_strict_not_authority(tmp_path, field, value):
    with pytest.raises(PocketError):
        run(expressive_phrase(), tmp_path, receiver_assumption=receiver(**{field: value}))


def test_qa_expression_inline_lifecycle_equivalence_forgery_and_replay(tmp_path):
    record = expressive_phrase()
    store = str(tmp_path / 'store')
    life = midi_lifecycle(material=record, store_root=store, request_id='external-lifecycle', **lifecycle(record))
    result, plan = run(record, tmp_path)
    other, handled = run(record, tmp_path, lifecycle=life['report'], request_id='from-lifecycle-handle')
    assert plan == handled and result['plan'] == other['plan']
    forged = read_record(life['report'], store)
    forged['notes'][0]['reserved_until_qn'] = q(0)
    forged_handle = put_record(forged, store)
    with pytest.raises(PocketError, match='recomputed'):
        run(record, tmp_path, lifecycle=forged_handle, request_id='forged')
    assert midi_expression_plan(**arguments(record, tmp_path)) == result
    with pytest.raises(PocketError, match='idempotency_conflict'):
        run(record, tmp_path, receiver_assumption=receiver(label='changed'))
    (tmp_path / 'store' / plan['material']['artifact_uri']).write_bytes(b'corrupt')
    with pytest.raises(PocketError, match='integrity'):
        midi_expression_plan(**arguments(record, tmp_path))


@pytest.mark.parametrize('horizon,tail,pedal', [(7, 0, False), (8, 1, False), (8, 0, True)])
def test_qa_expression_requires_complete_finite_horizon(tmp_path, horizon, tail, pedal):
    record = expressive_phrase()
    if pedal:
        record['events'] = [control('down', 1, 127)]
        record['clips'][0]['event_ids'] = ['down']
        record = seal_literal(record)
    with pytest.raises(PocketError):
        run(record, tmp_path, lifecycle=lifecycle(record, horizon=horizon, tail=tail))
    assert not any(json.loads(path.read_text())['schema'] == 'pocket.midi-expression-plan/v1'
                   for path in (tmp_path / 'store' / 'artifacts').glob('*/record.json'))


@pytest.mark.parametrize('cc', [1, 11, 74, 101, 120, 121, 123])
def test_qa_expression_does_not_silently_drop_other_controllers(tmp_path, cc):
    record = phrase(controls=[control('foreign', 1, 12, cc=cc)])
    with pytest.raises(PocketError):
        run(record, tmp_path, lifecycle=lifecycle(record, horizon=16))


@pytest.mark.parametrize('changes', [
    {'rounding': 'floor'}, {'exhaustion': 'steal_oldest'},
    {'neutral': {'pitch_cents': 0, 'pressure_7bit': 0, 'slide_7bit': 128}},
    {'max_pitch_error_cents': True}, {'max_control_error_normalized': {'n': 1, 'd': 0}},
    {'extra': 'unsupported'},
])
def test_qa_expression_encoding_contract_is_closed(tmp_path, changes):
    with pytest.raises(PocketError):
        run(expressive_phrase(), tmp_path, encoding=encoding(**changes))


def test_qa_expression_two_source_channels_are_not_implicitly_merged(tmp_path):
    record = expressive_phrase()
    record['notes'][1]['channel'] = 2
    record = seal_literal(record)
    declared = lifecycle(record)
    declared['initial_state']['sustain'].append({'channel': 2, 'value': 0})
    with pytest.raises(PocketError, match='one source'):
        run(record, tmp_path, lifecycle=declared)


def test_qa_expression_bounded_public_reply_keeps_full_allocation_proof(tmp_path):
    record = material([note(f'n{index:03}', index, 1) for index in range(100)], length=100)
    result, plan = run(record, tmp_path, lifecycle=lifecycle(record, horizon=100),
                       receiver_assumption=receiver(member_channels=[2]))
    assert len(json.dumps(result, indent=2).encode()) <= 16384
    assert len(result['allocations']) + result['omitted_allocations'] == 100
    assert len(plan['allocations']) == 100
    assert all(row['member_channel'] == 2 for row in plan['allocations'])
    assert [row['note_id'] for row in plan['allocations']] == [f'n{index:03}' for index in range(100)]
