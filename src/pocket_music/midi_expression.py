"""Declared offline expression allocation and ordered MIDI1 event realization.

No function here opens a port, configures a receiver, controls transport, or
qualifies native response. Rich source notes/curves remain immutable evidence.
"""
from __future__ import annotations

import copy
import heapq
import json
import math
from fractions import Fraction

from .artifact_store import _verify_handles, digest, put_record, read_record, receipt, run_request
from .errors import PocketError
from .expression_types import (
    ExpressionConfiguration,
    ExpressionEncoding,
    ExpressionLifecycleRequest,
    ReceiverAssumption,
)
from .material import integer, load_material, qn, rational
from .material_types import ArtifactHandle, MaterialRecord
from .midi_lifecycle import midi_lifecycle

MAX_EXPRESSION_NOTES = 4096
MAX_EXPRESSION_EVENTS = 65536
LIFECYCLE_FIELDS = {'clip_id', 'initial_state', 'horizon_qn', 'release_tail_qn', 'equal_time_order', 'source_basis'}
ENCODING_CONSTANTS = {
    'source_channel_policy': 'single_source_channel_to_zone', 'sustain_route': 'manager_cc64',
    'allocation': 'lowest_available_zone_order', 'exhaustion': 'reject_no_stealing',
    'reservation': 'through_symbolic_release_plus_declared_tail',
    'same_pitch_policy': 'new_per_note_channel_realization', 'tuning': 'tuning:12tet-a440',
    'curve_mode': 'step_only', 'rounding': 'nearest_ties_even',
    'reuse_order': 'old_expression_then_declared_off_cc_then_reset_setup_on',
}


def _fields(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise PocketError(f'{label} has missing or unsupported fields')


def _text(value, label, bound):
    if not isinstance(value, str) or not value.strip() or len(value) > bound:
        raise PocketError(f'{label} requires nonempty text within {bound} characters')


def _q(value, label):
    if isinstance(value, dict) and set(value) != {'n', 'd'}:
        raise PocketError(f'{label} requires exactly n and d')
    return rational(value, label)


def _decimal(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or isinstance(value, float) and not math.isfinite(value):
        raise PocketError(f'{label} requires finite numeric data')
    # Existing curve/material values are JSON decimal numbers. Preserve that
    # declared decimal value instead of introducing binary-float roundoff.
    return Fraction(str(value))


def _configuration(receiver, encoding, store_root):
    _fields(receiver, {'label', 'zone', 'manager_channel', 'member_channels', 'member_bend_range_semitones',
        'manager_pitch_policy', 'configuration_policy', 'configuration_status', 'attribution', 'instrument_state'},
        'Receiver assumption')
    _text(receiver['label'], 'Receiver label', 256)
    if receiver['zone'] not in ('lower', 'upper'):
        raise PocketError('Receiver zone must be lower or upper')
    manager = integer(receiver['manager_channel'], 'Manager channel', 1, 16)
    members = receiver['member_channels']
    if not isinstance(members, list) or not 1 <= len(members) <= 15:
        raise PocketError('Receiver requires 1–15 explicit member channels')
    for channel in members:
        integer(channel, 'Member channel', 1, 16)
    expected = list(range(2, 2 + len(members))) if receiver['zone'] == 'lower' else list(range(15, 15 - len(members), -1))
    if manager != (1 if receiver['zone'] == 'lower' else 16) or members != expected:
        raise PocketError('Receiver zone manager/members must be contiguous in declared zone order')
    integer(receiver['member_bend_range_semitones'], 'Declared member bend range', 1, 96)
    policies = {'manager_pitch_policy': 'neutral_no_pitch_messages',
        'configuration_policy': 'assume_preconfigured_no_setup_messages', 'configuration_status': 'declared_unverified'}
    if any(receiver[key] != value for key, value in policies.items()):
        raise PocketError('Receiver configuration is a declared unverified assumption; setup/native qualification unavailable')
    attribution = receiver['attribution']
    _fields(attribution, {'actor', 'actor_kind', 'statement', 'evidence'}, 'Receiver attribution')
    _text(attribution['actor'], 'Receiver actor', 256)
    _text(attribution['statement'], 'Receiver statement', 4096)
    if attribution['actor_kind'] not in ('human', 'agent'):
        raise PocketError('Receiver actor kind must be human or agent')
    if not isinstance(attribution['evidence'], list) or len(attribution['evidence']) > 16:
        raise PocketError('Receiver evidence requires up to 16 artifact handles')
    for handle in attribution['evidence']:
        # A raw evidence artifact is legitimate; complete shape/hash checks are shared.
        from .artifact_store import read_bytes
        read_bytes(handle, store_root)
    if receiver['instrument_state'] is not None:
        read_record(receiver['instrument_state'], store_root, 'pocket.instrument-state/v1')
    _verify_handles(receiver, store_root)
    _fields(encoding, set(ENCODING_CONSTANTS) | {'neutral', 'max_pitch_error_cents', 'max_control_error_normalized'},
            'Expression encoding')
    if any(encoding[key] != value for key, value in ENCODING_CONSTANTS.items()):
        raise PocketError('Unsupported expression encoding policy')
    neutral = encoding['neutral']
    _fields(neutral, {'pitch_cents', 'pressure_7bit', 'slide_7bit'}, 'Expression neutral')
    integer(neutral['pitch_cents'], 'Neutral pitch cents', 0, 0)
    integer(neutral['pressure_7bit'], 'Neutral pressure', 0, 127)
    integer(neutral['slide_7bit'], 'Neutral slide', 0, 127)
    pitch_error = _q(encoding['max_pitch_error_cents'], 'Pitch quantization tolerance')
    control_error = _q(encoding['max_control_error_normalized'], 'Control quantization tolerance')
    if pitch_error < 0 or control_error < 0:
        raise PocketError('Expression error tolerances must be nonnegative')
    return pitch_error, control_error


def _round_even(value):
    low = value.numerator // value.denominator
    remainder = value - low
    return low + (remainder > Fraction(1, 2) or remainder == Fraction(1, 2) and low % 2 != 0)


def _bend(cents, bend_range, tolerance):
    span = bend_range * 100
    if abs(cents) > span:
        raise PocketError('Intended pitch exceeds the explicitly declared bend range')
    intended = cents * 8192 / span
    displacement = min(8191, max(-8192, _round_even(intended)))
    decoded = Fraction(displacement * span, 8192)
    error = abs(decoded - cents)
    if error > tolerance:
        raise PocketError('Pitch quantization exceeds its exact declared tolerance')
    unsigned = displacement + 8192
    return [unsigned & 127, unsigned >> 7], {'unit': 'cents', 'requested': qn(cents),
        'decoded': qn(decoded), 'absolute_error': qn(error), 'encoded_unsigned14': unsigned}


def _control(value, tolerance):
    if not 0 <= value <= 1:
        raise PocketError('Pressure/slide values require the explicit normalized 0..1 domain')
    raw = _round_even(value * 127)
    decoded = Fraction(raw, 127)
    error = abs(decoded - value)
    if error > tolerance:
        raise PocketError('Control quantization exceeds its exact declared tolerance')
    return raw, {'unit': 'normalized', 'requested': qn(value), 'decoded': qn(decoded),
                 'absolute_error': qn(error), 'encoded_7bit': raw}


def _curves(record, clip, notes):
    curves = {curve['id']: curve for curve in record['curves']}
    supported = {'per_note_pitch', 'per_note_pressure', 'per_note_slide'}
    resolved, used = {}, set()
    for note in notes:
        by_kind = {}
        for curve_id in note['expression_refs']:
            curve = curves[curve_id]
            target = curve['target']
            if (target['kind'] not in supported or target['scope'] != 'note' or target.get('note_id') != note['id']
                    or target['ownership'] != 'none' or curve['space'] != 'note_relative_qn'
                    or curve['interpolation'] != 'step' or target.get('resize_policy') == 'preserve_ms'):
                raise PocketError('Expression requires unowned fixed-gate note-relative step pitch/pressure/slide curves')
            if target['kind'] in by_kind:
                raise PocketError('A note cannot have duplicate expression dimensions')
            if rational(curve['points'][0]['time']) != 0 or any(
                    not 0 <= rational(point['time']) <= rational(note['duration_qn']) for point in curve['points']):
                raise PocketError('Expression curves must start at zero and stay within the complete note gate')
            if target['kind'] == 'per_note_pitch':
                if target['value_mode'] != 'additive' or target['unit'] not in ('cents', 'semitones'):
                    raise PocketError('Per-note pitch requires additive cents or semitones')
            elif (target['value_mode'] != 'absolute' or target['unit'] != 'normalized'
                    or target['value_min'] != 0 or target['value_max'] != 1):
                raise PocketError('Per-note pressure/slide requires absolute normalized 0..1 domain')
            by_kind[target['kind']] = curve
            used.add(curve_id)
        resolved[note['id']] = by_kind
    if used != set(clip['curve_ids']):
        raise PocketError('Expression clip contains unmodeled curves or curve ownership')
    return resolved


def realize_expression(record: dict, lifecycle_report: dict, receiver_assumption: ReceiverAssumption,
                       encoding: ExpressionEncoding, store_root: str) -> dict:
    """Pure realizer for validated/recomputed lifecycle evidence; publishes nothing.

    Public callers must use midi_expression_plan or expression_plan_for_export,
    which revalidate material and recompute the supplied lifecycle first.
    """
    receiver = receiver_assumption
    pitch_error, control_error = _configuration(receiver, encoding, store_root)
    if lifecycle_report['material_revision'] != record['revision_sha256']:
        raise PocketError('Expression lifecycle and material revisions disagree')
    clip = next(clip for clip in record['clips'] if clip['id'] == lifecycle_report['clip_id'])
    note_ids = set(clip['note_ids'])
    all_notes = [note for note in record['notes'] if note['id'] in note_ids]
    notes = [note for note in all_notes if not note['mute']]
    if not 1 <= len(notes) <= MAX_EXPRESSION_NOTES:
        raise PocketError('Expression plan requires 1–4096 audible notes')
    channels = {note['channel'] for note in notes}
    channels.update(row['channel'] for row in lifecycle_report['cc64_realization'])
    if len(channels) != 1:
        raise PocketError('Expression requires one source note/sustain channel before zone mapping')
    source_channel = next(iter(channels))
    if any(row['channel'] != source_channel for row in lifecycle_report['channels']):
        raise PocketError('Expression initial state contains additional source channels')
    horizon = rational(lifecycle_report['horizon_qn'])
    if horizon < rational(clip['length_qn']):
        raise PocketError('Expression horizon must include the complete clip')
    lifetimes = {row['note_id']: row for row in lifecycle_report['notes']}
    if set(lifetimes) != {note['id'] for note in notes} or any(row['reserved_until_qn'] is None
            or rational(row['reserved_until_qn']) > horizon for row in lifetimes.values()):
        raise PocketError('Expression requires every audible note and a finite complete release/tail horizon')
    event_ids = set(clip['event_ids'])
    for event in record['events']:
        if event['id'] not in event_ids or event['is_meta'] or event['message_type'] in ('note_on', 'note_off'):
            continue
        data = event['bytes']
        if data[0] >> 4 != 11 or data[1] != 64 or rational(event['time']) > horizon:
            raise PocketError('Expression derivative cannot drop or remap unmodeled raw channel/system events')
    curves = _curves(record, clip, all_notes)
    for note in all_notes:
        if set(note['pitch']) != {'midi_note', 'cents_offset', 'tuning_ref'} or note['pitch']['tuning_ref'] != encoding['tuning']:
            raise PocketError('Expression pitch requires an explicit supported complete tuning record')
        _decimal(note['pitch']['cents_offset'], 'Note cents offset')
    members = receiver['member_channels']
    available, active = list(range(len(members))), []
    heapq.heapify(available)
    allocations = []
    for note in sorted(notes, key=lambda note: (rational(note['onset']), note['id'])):
        onset = rational(note['onset'])
        while active and active[0][0] <= onset:
            _, index = heapq.heappop(active)
            heapq.heappush(available, index)
        if not available:
            raise PocketError(f'Expression channel exhaustion at {qn(onset)}: {len(members)} members reserved; no stealing')
        index = heapq.heappop(available)
        life = lifetimes[note['id']]
        end = rational(life['reserved_until_qn'])
        heapq.heappush(active, (end, index))
        allocations.append({'note_id': note['id'], 'source_channel': source_channel,
            'member_channel': members[index], 'onset_qn': qn(rational(note['onset'])),
            'gate_off_qn': life['gate_off_qn'], 'release_qn': life['release_qn'], 'reserved_until_qn': life['reserved_until_qn']})
    by_id = {note['id']: note for note in notes}
    staged, proofs = [], []

    def emit(time, phase, data, purpose, note_id=None, curve_id=None, source_event_id=None):
        if len(staged) >= MAX_EXPRESSION_EVENTS:
            raise PocketError('Expression realization exceeds its 65536-event bound')
        staged.append({'time_qn': qn(time), 'phase_rank': phase, 'bytes': data, 'purpose': purpose,
            'note_id': note_id, 'curve_id': curve_id, 'source_event_id': source_event_id,
            '_insertion_order': len(staged)})

    neutral = encoding['neutral']
    manager_status = 175 + receiver['manager_channel']
    initial = lifecycle_report['channels'][0]['initial_sustain']
    emit(Fraction(0), 0, [manager_status, 64, initial], 'initial_manager_sustain')
    off_rank, cc_rank = (20, 21) if lifecycle_report['equal_time_order'] == 'note_off_cc_note_on' else (21, 20)
    for event in lifecycle_report['cc64_realization']:
        emit(rational(event['time_qn']), cc_rank, [manager_status, 64, event['value']],
             'manager_sustain', source_event_id=event['event_id'])
    for allocation in allocations:
        note = by_id[allocation['note_id']]
        channel = allocation['member_channel']
        onset, gate, reserve = (rational(allocation[key]) for key in ('onset_qn', 'gate_off_qn', 'reserved_until_qn'))
        for time, rank, purpose in ((onset, 40, 'member_initial_neutral'), (reserve, 30, 'member_release_reset')):
            emit(time, rank, [223 + channel, 0, 64], purpose, note['id'])
            emit(time, rank, [207 + channel, neutral['pressure_7bit']], purpose, note['id'])
            emit(time, rank, [175 + channel, 74, neutral['slide_7bit']], purpose, note['id'])
        dimensions = curves[note['id']]
        base_cents = _decimal(note['pitch']['cents_offset'], 'Note cents offset')
        for kind in ('per_note_pitch', 'per_note_pressure', 'per_note_slide'):
            curve = dimensions.get(kind)
            if curve is None:
                if kind != 'per_note_pitch':
                    continue
                points, curve_id, unit = [{'time': qn(0), 'value': 0, 'order': 0}], None, 'cents'
            else:
                points, curve_id, unit = curve['points'], curve['id'], curve['target']['unit']
            for point in points:
                value = _decimal(point['value'], 'Expression point')
                local_time = rational(point['time'])
                if kind == 'per_note_pitch':
                    cents = base_cents + value * (100 if unit == 'semitones' else 1)
                    raw, proof = _bend(cents, receiver['member_bend_range_semitones'], pitch_error)
                    data = [223 + channel, *raw]
                else:
                    raw, proof = _control(value, control_error)
                    data = [207 + channel, raw] if kind == 'per_note_pressure' else [175 + channel, 74, raw]
                emit(onset + local_time, 50 if local_time == 0 else 10, data,
                     'note_initial_expression' if local_time == 0 else 'note_expression', note['id'], curve_id)
                proofs.append({'note_id': note['id'], 'curve_id': curve_id, 'dimension': kind,
                    'point_order': point['order'], 'time_qn': qn(onset + local_time), **proof})
        emit(gate, off_rank, [127 + channel, note['pitch']['midi_note'], note['release_velocity']['value']], 'note_off', note['id'])
        emit(onset, 60, [143 + channel, note['pitch']['midi_note'], note['velocity']['value']], 'note_on', note['id'])
    ordered = sorted(staged, key=lambda row: (rational(row['time_qn']), row['phase_rank'], row['_insertion_order']))
    events = [{key: value for key, value in row.items() if key != '_insertion_order'} | {'order': index}
              for index, row in enumerate(ordered)]
    return {'schema': 'pocket.midi-expression-plan/v1', 'material_revision': record['revision_sha256'],
        'clip_id': clip['id'], 'source_channel': source_channel,
        'receiver_assumption': copy.deepcopy(receiver), 'encoding': copy.deepcopy(encoding),
        'allocations': allocations, 'events': events, 'quantization': proofs,
        'excluded_muted_note_ids': [note['id'] for note in all_notes if note['mute']],
        'source_receiver_ambiguity': lifecycle_report['conflicts'],
        'maximum_pitch_error_cents': qn(max((rational(row['absolute_error']) for row in proofs if row['unit'] == 'cents'), default=Fraction(0))),
        'maximum_control_error_normalized': qn(max((rational(row['absolute_error']) for row in proofs if row['unit'] == 'normalized'), default=Fraction(0))),
        'coverage': {'file_plan': 'validated', 'encoding': 'planned_not_exported',
            'receiver_configuration': 'declared_unverified', 'native_verified': False, 'performance_ready': False,
            'listening': 'not_performed', 'linear_curves': 'unsupported', 'transport': 'unsupported',
            'source_preservation': 'immutable_parent_and_curves', 'release_tail': 'caller_declared_not_receiver_measured'},
        'ordering': {'lifecycle': lifecycle_report['equal_time_order'],
            'same_time': 'old_expression_then_declared_off_cc_then_reset_setup_on',
            'note_order': 'exact_onset_then_stable_id', 'curve_equal_time_order': 'retained_point_order'}}


def _validated_lifecycle(record, lifecycle, store_root):
    supplied = None
    if isinstance(lifecycle, dict) and lifecycle.get('schema') == 'pocket.artifact-handle/v1':
        supplied = read_record(lifecycle, store_root, 'pocket.midi-lifecycle/v1')
        _verify_handles(supplied, store_root)
        try:
            request = {key: supplied[key] for key in LIFECYCLE_FIELDS - {'source_basis'}}
            request['source_basis'] = supplied['coverage']['source_basis']
        except (KeyError, TypeError) as error:
            raise PocketError('Supplied lifecycle report has missing policy fields') from error
    else:
        _fields(lifecycle, LIFECYCLE_FIELDS, 'Expression lifecycle request')
        request = copy.deepcopy(lifecycle)
    request_id = 'expression-lifecycle-' + digest({'material': record, 'request': request})
    result = midi_lifecycle(material=record, store_root=store_root, request_id=request_id, **request)
    proof = read_record(result['report'], store_root, 'pocket.midi-lifecycle/v1')
    if supplied is not None and supplied != proof:
        raise PocketError('Supplied lifecycle report does not equal independently recomputed evidence')
    return result['report'], proof


def expression_plan_for_export(material: ArtifactHandle | MaterialRecord, configuration: ExpressionConfiguration,
                               store_root: str) -> dict:
    """Validate/recompute evidence then realize events for plan or exporter callers.

    The only published prerequisites are honest public midi_lifecycle artifacts.
    No expression plan or output file is published here.
    """
    _fields(configuration, {'lifecycle', 'receiver_assumption', 'encoding'}, 'Expression configuration')
    record = load_material(material, store_root)
    _configuration(configuration['receiver_assumption'], configuration['encoding'], store_root)
    lifecycle_handle, proof = _validated_lifecycle(record, configuration['lifecycle'], store_root)
    plan = realize_expression(record, proof, configuration['receiver_assumption'], configuration['encoding'], store_root)
    return {**plan, 'material': put_record(record, store_root), 'lifecycle': lifecycle_handle}


def _bounded_receipt(result):
    while True:
        preview = result['allocations']
        if any(len(str(row['note_id'])) > 1024 for row in preview):
            result['omitted_allocations'] += len(preview)
            result['allocations'] = []
        size = 1
        for part in json.JSONEncoder(indent=2, ensure_ascii=True, allow_nan=False).iterencode(result):
            size += len(part.encode())
            if size > 16384:
                break
        if size <= 16384:
            return result
        if not result['allocations']:
            raise PocketError('Expression receipt exceeds its 16 KiB bound')
        result['allocations'].pop()
        result['omitted_allocations'] += 1


def midi_expression_plan(material: ArtifactHandle | MaterialRecord,
                         lifecycle: ArtifactHandle | ExpressionLifecycleRequest,
                         receiver_assumption: ReceiverAssumption, encoding: ExpressionEncoding,
                         store_root: str, request_id: str) -> dict:
    """Plan a bounded MIDI1 member-channel realization without configuring a receiver."""
    configuration = {'lifecycle': lifecycle, 'receiver_assumption': receiver_assumption, 'encoding': encoding}
    inputs = {'material': material, **configuration}

    def work():
        plan = expression_plan_for_export(material, configuration, store_root)
        handle = put_record(plan, store_root)
        summary = {'allocated_notes': len(plan['allocations']), 'events': len(plan['events']),
            'member_channels': len(receiver_assumption['member_channels']),
            'maximum_pitch_error_cents': plan['maximum_pitch_error_cents'],
            'maximum_control_error_normalized': plan['maximum_control_error_normalized']}
        return _bounded_receipt(receipt(request_id=request_id, artifacts={'plan': handle}, plan=handle,
            change_summary=summary, coverage=plan['coverage'], allocations=plan['allocations'][:16],
            omitted_allocations=max(0, len(plan['allocations']) - 16),
            uncertainty=['Receiver zone/bend range and release tail are declared assumptions, not native qualification.',
                'This is a new per-note channel realization; original same-channel receiver pairing is not inferred.',
                'No MIDI file, device configuration, transport, panic, playback or listening is performed.']))
    return run_request(store_root, request_id, 'midi_expression_plan', inputs, work)
