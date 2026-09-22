"""Independent expected event values for offline declared expression realization."""
from __future__ import annotations

import copy
import json
from fractions import Fraction

import pytest
from test_midi_lifecycle import fixture as lifecycle_fixture
from test_midi_lifecycle import imported
from test_midi_qa import expressed_material, seal_literal

from pocket_music.artifact_store import put_record, read_record
from pocket_music.errors import PocketError
from pocket_music.material import qn, rational
from pocket_music.midi_expression import expression_plan_for_export, midi_expression_plan
from pocket_music.midi_lifecycle import midi_lifecycle


def configuration(clip_id='clip:external', *, members=None):
    return {'lifecycle': {'clip_id': clip_id,
        'initial_state': {'active_notes': 'none', 'sustain': [{'channel': 1, 'value': 0}]},
        'horizon_qn': 6, 'release_tail_qn': 1, 'equal_time_order': 'note_off_cc_note_on',
        'source_basis': 'canonical_notes_retained_cc64'},
        'receiver_assumption': {'label': 'Declared fixture receiver', 'zone': 'lower', 'manager_channel': 1,
            'member_channels': [2, 3] if members is None else members, 'member_bend_range_semitones': 48,
            'manager_pitch_policy': 'neutral_no_pitch_messages',
            'configuration_policy': 'assume_preconfigured_no_setup_messages',
            'configuration_status': 'declared_unverified', 'instrument_state': None,
            'attribution': {'actor': 'Test fixture', 'actor_kind': 'agent',
                            'statement': 'Assume a configured receiver for symbolic testing only.', 'evidence': []}},
        'encoding': {'source_channel_policy': 'single_source_channel_to_zone', 'sustain_route': 'manager_cc64',
            'allocation': 'lowest_available_zone_order', 'exhaustion': 'reject_no_stealing',
            'reservation': 'through_symbolic_release_plus_declared_tail',
            'same_pitch_policy': 'new_per_note_channel_realization', 'tuning': 'tuning:12tet-a440',
            'neutral': {'pitch_cents': 0, 'pressure_7bit': 0, 'slide_7bit': 64}, 'curve_mode': 'step_only',
            'rounding': 'nearest_ties_even', 'max_pitch_error_cents': {'n': 1, 'd': 8},
            'max_control_error_normalized': {'n': 1, 'd': 254},
            'reuse_order': 'old_expression_then_declared_off_cc_then_reset_setup_on'}}


def expressive(value=30, kind='per_note_pitch', unit='cents', *, two=True):
    record = expressed_material()
    record['notes'] = record['notes'][:2 if two else 1]
    record['notes'][0]['duration_qn'] = qn(2)
    if two:
        record['notes'][1]['onset'] = {'space': 'clip_qn', **qn(0)}
        record['notes'][1]['duration_qn'] = qn(2)
    record['clips'][0]['note_ids'] = [note['id'] for note in record['notes']]
    curve = record['curves'][0]
    curve['target'].update(kind=kind, unit=unit, value_mode='additive' if kind == 'per_note_pitch' else 'absolute',
                           value_min=-10000 if kind == 'per_note_pitch' else 0,
                           value_max=10000 if kind == 'per_note_pitch' else 1)
    curve['interpolation'] = 'step'
    curve['points'] = [{'time': qn(0), 'value': 0, 'order': 0},
                       {'time': qn(1), 'value': value, 'order': 1},
                       {'time': qn(2), 'value': 0, 'order': 2}]
    return seal_literal(record)


def plan(record, tmp_path, config=None, request='expression'):
    config = config or configuration(record['clips'][0]['id'])
    result = midi_expression_plan(record, store_root=str(tmp_path / 'store'), request_id=request, **config)
    return result, read_record(result['plan'], tmp_path / 'store')


def test_two_notes_separate_members_exact_pitch_bytes_and_preservation(tmp_path):
    record = expressive()
    original = copy.deepcopy(record)
    result, proof = plan(record, tmp_path)
    assert [(row['note_id'], row['member_channel']) for row in proof['allocations']] == [('note:0', 2), ('note:1', 3)]
    positive = [row for row in proof['events'] if row['time_qn'] == qn(1)]
    assert [row['bytes'] for row in positive] == [[225, 51, 64]]
    numeric = next(row for row in proof['quantization'] if row['time_qn'] == qn(1))
    assert numeric['encoded_unsigned14'] == 8243
    assert numeric['decoded'] == {'n': 3825, 'd': 128}
    assert numeric['absolute_error'] == {'n': 15, 'd': 128}
    assert result['coverage']['native_verified'] is False and result['coverage']['encoding'] == 'planned_not_exported'
    assert record == original and read_record(proof['material'], tmp_path / 'store') == original
    assert all(row['bytes'][1] not in (100, 101, 120, 121, 123) for row in proof['events'] if row['bytes'][0] >> 4 == 11)


@pytest.mark.parametrize('cents,encoded,error', [(-30, 8141, Fraction(15, 128)),
    (4800, 16383, Fraction(75, 128)), (-4800, 0, Fraction(0)),
    (Fraction(75, 256), 8192, Fraction(75, 256)), (Fraction(225, 256), 8194, Fraction(75, 256))])
def test_pitch_endpoints_and_ties_even(tmp_path, cents, encoded, error):
    record = expressive(float(cents), two=False)
    config = configuration()
    config['encoding']['max_pitch_error_cents'] = qn(error)
    _, proof = plan(record, tmp_path, config)
    row = next(row for row in proof['quantization'] if row['time_qn'] == qn(1))
    assert row['encoded_unsigned14'] == encoded and row['absolute_error'] == qn(error)


def test_tolerance_and_range_are_not_implicitly_relaxed(tmp_path):
    config = configuration()
    config['encoding']['max_pitch_error_cents'] = {'n': 1, 'd': 10}
    with pytest.raises(PocketError, match='tolerance'):
        plan(expressive(), tmp_path, config)
    config['encoding']['max_pitch_error_cents'] = 9999
    with pytest.raises(PocketError, match='range'):
        plan(expressive(4800.1), tmp_path, config, request='outside')


@pytest.mark.parametrize('kind,value,raw,error', [('per_note_pressure', 0.15, 19, Fraction(1, 2540)),
    ('per_note_pressure', 0.4, 51, Fraction(1, 635)), ('per_note_pressure', 0.5, 64, Fraction(1, 254)),
    ('per_note_slide', 1, 127, Fraction(0))])
def test_pressure_slide_explicit_mapping(tmp_path, kind, value, raw, error):
    _, proof = plan(expressive(value, kind, 'normalized', two=False), tmp_path)
    event = next(row for row in proof['events'] if row['purpose'] == 'note_expression' and row['time_qn'] == qn(1))
    assert event['bytes'] == ([209, raw] if kind == 'per_note_pressure' else [177, 74, raw])
    numeric = next(row for row in proof['quantization'] if row['dimension'] == kind and row['time_qn'] == qn(1))
    assert numeric['absolute_error'] == qn(error)


def test_declared_sustain_tail_blocks_reuse_and_orders_final_reset(tmp_path):
    record = lifecycle_fixture((1, 64, 127), (4, 64, 0))
    second = {**copy.deepcopy(record['notes'][0]), 'id': 'note:second',
              'onset': {'space': 'clip_qn', **qn(5)}, 'duration_qn': qn(1)}
    record['notes'].append(second)
    record['clips'][0]['note_ids'].append(second['id'])
    record['clips'][0]['length_qn'] = qn(6)
    config = configuration(members=[2])
    config['lifecycle']['horizon_qn'] = 7
    _, proof = plan(seal_literal(record), tmp_path, config)
    assert [row['member_channel'] for row in proof['allocations']] == [2, 2]
    assert proof['allocations'][0]['reserved_until_qn'] == qn(5)
    at_five = [row for row in proof['events'] if row['time_qn'] == qn(5)]
    assert [row['purpose'] for row in at_five] == ['member_release_reset'] * 3 + ['member_initial_neutral'] * 3 + ['note_initial_expression', 'note_on']
    assert not [row for row in proof['events'] if row['purpose'] == 'member_release_reset' and row['time_qn'] in (qn(2), qn(4))]
    record['notes'][1]['onset'] = {'space': 'clip_qn', **qn(Fraction(4999, 1000))}
    with pytest.raises(PocketError, match='exhaustion'):
        plan(seal_literal(record), tmp_path, config, request='early')


def test_old_final_expression_precedes_off_and_no_zero_tail_early_reset(tmp_path):
    config = configuration()
    config['lifecycle']['release_tail_qn'] = 0
    _, proof = plan(expressive(), tmp_path, config)
    at_two = [row for row in proof['events'] if row['time_qn'] == qn(2)]
    kinds = [row['purpose'] for row in at_two]
    assert kinds[0] == 'note_expression'
    assert max(index for index, kind in enumerate(kinds) if kind == 'note_off') < min(
        index for index, kind in enumerate(kinds) if kind == 'member_release_reset')


def test_same_pitch_canonical_overlap_gets_new_member_identity(tmp_path):
    record = expressive()
    record['notes'][1]['pitch'] = copy.deepcopy(record['notes'][0]['pitch'])
    _, proof = plan(seal_literal(record), tmp_path)
    assert [row['member_channel'] for row in proof['allocations']] == [2, 3]
    assert len(proof['source_receiver_ambiguity']) == 1
    with pytest.raises(PocketError, match='exhaustion'):
        plan(seal_literal(record), tmp_path, configuration(members=[2]), request='no-sharing')


def test_valid_handle_and_inline_lifecycle_yield_identical_plan(tmp_path):
    record = expressive()
    config = configuration()
    analyzed = midi_lifecycle(record, store_root=str(tmp_path / 'store'), request_id='analyze', **config['lifecycle'])
    _, inline = plan(record, tmp_path, config)
    config['lifecycle'] = analyzed['report']
    _, via_handle = plan(record, tmp_path, config, request='handle')
    assert via_handle == inline
    assert expression_plan_for_export(record, config, str(tmp_path / 'store')) == inline


def test_forged_lifecycle_reservation_refuses_even_with_valid_hash(tmp_path):
    record = expressive()
    config = configuration()
    result = midi_lifecycle(record, store_root=str(tmp_path / 'store'), request_id='analyze', **config['lifecycle'])
    report = read_record(result['report'], tmp_path / 'store')
    report['notes'][0]['reserved_until_qn'] = qn(0)
    config['lifecycle'] = put_record(report, tmp_path / 'store')
    with pytest.raises(PocketError, match='recomputed'):
        plan(record, tmp_path, config)


@pytest.mark.parametrize('section,field,value', [('receiver_assumption', 'manager_channel', True),
    ('receiver_assumption', 'member_channels', [3]), ('receiver_assumption', 'member_channels', [2, 2]),
    ('receiver_assumption', 'member_bend_range_semitones', 0), ('receiver_assumption', 'member_bend_range_semitones', 48.0),
    ('receiver_assumption', 'configuration_status', 'native_verified'),
    ('receiver_assumption', 'configuration_policy', 'send_setup'),
    ('encoding', 'exhaustion', 'steal'), ('encoding', 'curve_mode', 'linear'),
    ('encoding', 'rounding', 'floor'), ('encoding', 'max_pitch_error_cents', True),
    ('encoding', 'max_control_error_normalized', {'n': 1, 'd': 2, 'space': 'seconds'}),
    ('encoding', 'max_pitch_error_cents', -1), ('encoding', 'unexpected', 1)])
def test_strict_configuration_refusals(tmp_path, section, field, value):
    config = configuration()
    config[section][field] = value
    with pytest.raises(PocketError):
        plan(expressive(), tmp_path, config)


@pytest.mark.parametrize('mutation', ['linear', 'absolute_pitch', 'unknown_tuning', 'first_point', 'duplicate_dimension',
                                      'owned', 'tail_point', 'unmodeled_raw'])
def test_unqualified_expression_routes_refuse(tmp_path, mutation):
    record = expressive()
    if mutation == 'linear':
        record['curves'][0]['interpolation'] = 'linear'
    elif mutation == 'absolute_pitch':
        record['curves'][0]['target']['value_mode'] = 'absolute'
    elif mutation == 'unknown_tuning':
        record['notes'][0]['pitch']['tuning_ref'] = 'tuning:other'
    elif mutation == 'first_point':
        record['curves'][0]['points'] = record['curves'][0]['points'][1:]
    elif mutation == 'owned':
        record['curves'][0]['target']['ownership'] = 'remote'
    elif mutation == 'tail_point':
        record['curves'][0]['points'][-1]['time'] = qn(3)
    elif mutation == 'duplicate_dimension':
        curve = copy.deepcopy(record['curves'][0])
        curve['id'] = curve['curve_id'] = 'curve:second'
        record['curves'].append(curve)
        record['clips'][0]['curve_ids'].append(curve['id'])
        record['notes'][0]['expression_refs'].append(curve['id'])
    else:
        event = {'id': 'raw:bend', 'time': {'space': 'clip_qn', **qn(0)}, 'order': 0,
                 'track_index': 0, 'message_type': 'pitchwheel', 'is_meta': False, 'bytes': [224, 0, 64]}
        record['events'].append(event)
        record['clips'][0]['event_ids'].append(event['id'])
    with pytest.raises(PocketError):
        plan(seal_literal(record), tmp_path)


def test_upper_zone_order_and_release_velocity(tmp_path):
    record = expressive()
    config = configuration()
    config['receiver_assumption'].update(zone='upper', manager_channel=16, member_channels=[15, 14])
    _, proof = plan(record, tmp_path, config)
    assert [row['member_channel'] for row in proof['allocations']] == [15, 14]
    assert [row['bytes'] for row in proof['events'] if row['purpose'] == 'note_off'] == [[142, 60, 37], [141, 64, 37]]


def test_real_source_controller_bytes_preserved_and_composable(tmp_path):
    record = imported(tmp_path)
    original = copy.deepcopy(record)
    _, proof = plan(record, tmp_path, configuration(record['clips'][0]['id']))
    assert proof['allocations'][0]['release_qn'] == qn(4)
    assert proof['allocations'][0]['reserved_until_qn'] == qn(5)
    assert record == original


def test_pending_release_incomplete_scope_and_multiple_channels_refuse(tmp_path):
    record = lifecycle_fixture()
    record['clips'][0]['length_qn'] = qn(4)
    config = configuration()
    config['lifecycle']['initial_state']['sustain'][0]['value'] = 127
    with pytest.raises(PocketError, match='finite'):
        plan(seal_literal(record), tmp_path, config)
    config = configuration()
    config['lifecycle']['horizon_qn'] = 2
    with pytest.raises(PocketError, match='complete clip'):
        plan(seal_literal(record), tmp_path, config, request='truncated')
    record = expressive()
    record['notes'][1]['channel'] = 2
    config = configuration()
    config['lifecycle']['initial_state']['sustain'].append({'channel': 2, 'value': 0})
    with pytest.raises(PocketError, match='one source'):
        plan(seal_literal(record), tmp_path, config, request='channels')


def test_budget_failure_no_published_plan_but_diagnostic_lifecycle_retained(tmp_path, monkeypatch):
    import pocket_music.midi_expression as module
    monkeypatch.setattr(module, 'MAX_EXPRESSION_EVENTS', 3)
    with pytest.raises(PocketError, match='event bound'):
        plan(expressive(), tmp_path)
    records = [json.loads(path.read_text()) for path in (tmp_path / 'store' / 'artifacts').rglob('record.json')]
    assert any(record['schema'] == 'pocket.midi-lifecycle/v1' for record in records)
    assert not any(record['schema'] == 'pocket.midi-expression-plan/v1' for record in records)


def test_request_retry_conflict_and_tamper(tmp_path):
    record = expressive()
    result, original = plan(record, tmp_path)
    replay, _ = plan(record, tmp_path)
    assert replay == result
    changed = configuration()
    changed['receiver_assumption']['attribution']['statement'] = 'Different declaration'
    with pytest.raises(PocketError, match='idempotency'):
        plan(record, tmp_path, changed)
    (tmp_path / 'store' / result['plan']['artifact_uri']).write_text('{}')
    with pytest.raises(PocketError):
        plan(record, tmp_path)
    assert original['material_revision'] == record['revision_sha256']


def test_public_bound_complete_proof_and_no_optional_imports(tmp_path, monkeypatch):
    import builtins
    original_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert not name.startswith(('mido', 'torch', 'pocket_music.native'))
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', guarded)
    result, proof = plan(expressive(), tmp_path)
    assert len((json.dumps(result, indent=2, ensure_ascii=True) + '\n').encode()) <= 16384
    assert [row['order'] for row in proof['events']] == list(range(len(proof['events'])))
    assert all(rational(left['time_qn']) <= rational(right['time_qn']) for left, right in zip(proof['events'], proof['events'][1:]))
