# SPDX-License-Identifier: AGPL-3.0-only
"""Offline canonical gates and explicitly ordered binary-sustain interpretation.

This is a declared symbolic simulation, never receiver, allocator or panic proof.
"""
from __future__ import annotations

import heapq
import json
from fractions import Fraction
from typing import Literal

from .artifact_store import put_record, read_bytes, read_record, receipt, run_request
from .errors import PocketError
from .lifecycle_types import LifecycleInitialState
from .material import integer, load_material, qn, rational, validate_material
from .material_types import ArtifactHandle, MaterialRecord, Rational

MAX_LIFECYCLE_NOTES = 10000
MAX_LIFECYCLE_EVENTS = 20000


def _qn(value, label):
    if isinstance(value, dict) and set(value) != {'n', 'd'}:
        raise PocketError(f'{label} requires exactly n and d')
    return rational(value, label)


def _source_wire(payload):
    """Read raw SMF event positions without importing an optional MIDI codec.

    This narrowly checks retained wire/clock evidence; it does not reconstruct notes.
    """
    if len(payload) > 32 * 1024 * 1024 or len(payload) < 14 or payload[:4] != b'MThd':
        raise PocketError('Lifecycle requires a bounded ordinary SMF source')
    header = int.from_bytes(payload[4:8], 'big')
    kind, count, ppq = (int.from_bytes(payload[start:start + 2], 'big') for start in (8, 10, 12))
    if header != 6 or kind not in (0, 1) or count < 1 or not 1 <= ppq <= 32767 or kind == 0 and count != 1:
        raise PocketError('Lifecycle source requires SMF 0/1 and positive PPQ')
    cursor, tracks, total = 14, [], 0
    for _ in range(count):
        if payload[cursor:cursor + 4] != b'MTrk' or cursor + 8 > len(payload):
            raise PocketError('Malformed lifecycle source track')
        size = int.from_bytes(payload[cursor + 4:cursor + 8], 'big')
        cursor += 8
        end = cursor + size
        if end > len(payload):
            raise PocketError('Truncated lifecycle source track')
        tick, running, rows = 0, None, []

        def variable(end=end):
            nonlocal cursor
            value = 0
            for _ in range(4):
                if cursor >= end:
                    raise PocketError('Truncated lifecycle source variable integer')
                byte = payload[cursor]
                cursor += 1
                value = value * 128 + (byte & 127)
                if byte < 128:
                    return value
            raise PocketError('Oversized lifecycle source variable integer')

        while cursor < end:
            tick += variable()
            if cursor >= end:
                raise PocketError('Missing lifecycle source event')
            status = payload[cursor]
            if status >= 128:
                cursor += 1
                running = status if status < 240 else None
            elif running is None:
                raise PocketError('Missing lifecycle source running status')
            else:
                status = running
            if status == 255:
                if cursor >= end:
                    raise PocketError('Missing lifecycle source meta type')
                meta = payload[cursor]
                cursor += 1
                length_start = cursor
                length = variable()
                wire = [255, meta, *payload[length_start:cursor], *payload[cursor:cursor + length]]
            elif status in (240, 247):
                length = variable()
                wire = [status, *payload[cursor:cursor + length]]
            elif 128 <= status < 240:
                length = 1 if status >> 4 in (12, 13) else 2
                wire = [status, *payload[cursor:cursor + length]]
                if any(byte >= 128 for byte in wire[1:]):
                    raise PocketError('Invalid lifecycle source channel bytes')
            else:
                lengths = {241: 1, 242: 2, 243: 1, 246: 0, 248: 0, 250: 0, 251: 0, 252: 0, 254: 0}
                if status not in lengths:
                    raise PocketError('Unsupported lifecycle source system status')
                length = lengths[status]
                wire = [status, *payload[cursor:cursor + length]]
            cursor += length
            if cursor > end:
                raise PocketError('Truncated lifecycle source event')
            total += 1
            if total > 200000:
                raise PocketError('Lifecycle source exceeds event verification bound')
            rows.append((tick, wire))
        if not rows or rows[-1][1] != [255, 47, 0] or sum(wire[:2] == [255, 47] for _, wire in rows) != 1:
            raise PocketError('Lifecycle source tracks require exactly one terminal end-of-track')
        tracks.append(rows)
    if cursor != len(payload):
        raise PocketError('Lifecycle source has undeclared trailing data')
    return ppq, tracks



def _ordinary_lineage(record, store_root):
    """Sequence parent references are provenance, not omitted active wire events."""
    pending, seen = [record], set()
    while pending:
        current = pending.pop()
        revision = current['revision_sha256']
        if revision in seen:
            continue
        seen.add(revision)
        if len(seen) > 1000:
            raise PocketError('Lifecycle ordinary material lineage exceeds its verification bound')
        if (current['coverage'].get('editing_allowed') is not True
                or current['events'] or current['curves'] or current['tempo_map_ref'] is not None
                or current['meter_map_ref'] is not None or any(note['source_binding'] is not None
                    or note['expression_refs'] for note in current['notes'])):
            raise PocketError('Lifecycle sequence provenance must remain ordinary controller-free material')
        for source in current['sources']:
            if (set(source) != {'kind', 'key', 'material'}
                    or source['kind'] != 'material_sequence_source' or not isinstance(source['key'], str)):
                raise PocketError('Lifecycle source-backed clip requires complete retained event coverage')
            parent = read_record(source['material'], store_root, 'pocket.material/v1')
            pending.append(validate_material(parent))
    return len(seen)

def _event_coordinates(record, clip, events, store_root):
    """Prove retained source CC positions, or identify caller-declared clip coordinates."""
    if not events and not record['sources']:
        return {'basis': 'no_retained_events', 'source_reparse': 'not_needed'}
    if not events:
        checked = _ordinary_lineage(record, store_root)
        return {'basis': 'verified_controller_free_material_lineage',
                'source_reparse': 'no_wire_sources', 'material_revisions_checked': checked}
    if not record['sources']:
        return {'basis': 'caller_declared_clip_qn', 'source_reparse': 'no_source_claim'}
    if len(record['sources']) != 1 or record['sources'][0].get('kind') != 'smf':
        raise PocketError('Lifecycle retained events require one qualified SMF source or explicit source-free material')
    source = record['sources'][0]
    if rational(clip['origin']) != 0:
        raise PocketError('Lifecycle source-to-clip controller mapping requires zero source clip origin')
    raw = source.get('raw')
    if (not isinstance(raw, dict) or raw.get('artifact_schema') != 'pocket.smf-original/v1'
            or raw.get('sha256') != source.get('sha256')):
        raise PocketError('Lifecycle retained events require exact original SMF evidence')
    ppq, tracks = _source_wire(read_bytes(raw, store_root))
    if type(source.get('ppq')) is not int or source['ppq'] != ppq:
        raise PocketError('Lifecycle source PPQ evidence disagrees with source bytes')
    clip_track = next(track for track in record['tracks'] if track['id'] == clip['track_id'])
    source_track = integer(clip_track.get('source_track'), 'clip source track', 0, len(tracks) - 1)
    selected_ids = set(clip['note_ids'])
    selected_notes = [note for note in record['notes'] if note['id'] in selected_ids]
    for note in selected_notes:
        binding = note['source_binding']
        if binding is not None and (binding.get('source_sha256') != source['sha256']
                or type(binding.get('track_index')) is not int or binding['track_index'] != source_track):
            raise PocketError('Lifecycle note source binding must match the isolated source track')
    scoped_channels = {note['channel'] for note in selected_notes}
    scoped_channels.update((event['bytes'][0] & 15) + 1 for event in events
                           if not event['is_meta'] and event['bytes'][0] < 240)
    for track_index, track_events in enumerate(tracks):
        if track_index == source_track:
            continue
        if any(128 <= wire[0] < 240 and (wire[0] & 15) + 1 in scoped_channels
               for _, wire in track_events):
            raise PocketError('Lifecycle shared-channel events on another source track require an explicit merged realization')
    bound_tracks = set()
    for event in events:
        track = integer(event.get('track_index'), 'retained source track', 0, len(tracks) - 1)
        if track != source_track:
            raise PocketError('Lifecycle retained events must belong to the exact isolated clip source track')
        order = integer(event['order'], 'retained source order', 0)
        if order >= len(tracks[track]):
            raise PocketError('Lifecycle retained source order is missing')
        tick, wire = tracks[track][order]
        if type(event.get('source_tick')) is not int or event['source_tick'] != tick:
            raise PocketError('Lifecycle retained controller source tick disagrees with source bytes')
        if rational(event['time']) != Fraction(tick, ppq):
            raise PocketError('Lifecycle retained controller clip coordinate disagrees with exact source tick/PPQ')
        if event['bytes'] != wire:
            raise PocketError('Lifecycle retained event bytes disagree with original source')
        bound_tracks.add(track)
    # A partial source track could omit a pedal/reset event and invent a release.
    actual = {(event['track_index'], event['order']) for event in events}
    expected = {(track, order) for track in bound_tracks for order in range(len(tracks[track]))}
    if actual != expected:
        raise PocketError('Lifecycle source event coverage must retain every event in its source track')
    return {'basis': 'source_tick_over_ppq_at_zero_clip_origin', 'source_reparse': 'wire_event_positions',
            'source_sha256': source['sha256'], 'ppq': ppq, 'tracks': sorted(bound_tracks),
            'source_track_scope': 'isolated_no_other_source_track_uses_involved_channels'}


def _prepare(record, clip_id, initial_state, horizon, tail, order, source_basis, store_root):
    if not isinstance(clip_id, str):
        raise PocketError('Lifecycle requires an exact clip identity')
    clips = [clip for clip in record['clips'] if clip['id'] == clip_id]
    if len(clips) != 1:
        raise PocketError('Lifecycle clip identity does not resolve exactly')
    if source_basis != 'canonical_notes_retained_cc64':
        raise PocketError('Lifecycle requires explicit canonical_notes_retained_cc64 source basis')
    if record['coverage'].get('editing_allowed') is not True:
        raise PocketError('Lifecycle requires a complete unambiguous canonical note projection')
    if horizon <= 0 or tail < 0 or order not in ('note_off_cc_note_on', 'cc_note_off_note_on'):
        raise PocketError('Lifecycle requires positive horizon, nonnegative tail and explicit equal-time order')
    clip = clips[0]
    note_ids, event_ids, curve_ids = set(clip['note_ids']), set(clip['event_ids']), set(clip['curve_ids'])
    notes = [note for note in record['notes'] if note['id'] in note_ids]
    events = [event for event in record['events'] if event['id'] in event_ids]
    curves = [curve for curve in record['curves'] if curve['id'] in curve_ids]
    if len(notes) > MAX_LIFECYCLE_NOTES or len(events) > MAX_LIFECYCLE_EVENTS:
        raise PocketError('Lifecycle exceeds its 10000-note or 20000-event clip bound')
    if any(curve['target']['scope'] == 'channel' for curve in curves):
        raise PocketError('Lifecycle competing channel curves require a qualified controller realization')
    if any(rational(note['onset']) < 0 for note in notes) or any(rational(event['time']) < 0 for event in events):
        raise PocketError('Lifecycle initial_notes:none requires a nonnegative timeline; pickups need explicit chase')
    # Source-bound notes may be edited, but their richer native attributes are not a qualified lifecycle.
    if any(note['source_binding'] is not None and note['source_binding'].get('kind') != 'smf' for note in notes):
        raise PocketError('Lifecycle opaque/native note state is unqualified')
    coordinates = _event_coordinates(record, clip, events, store_root)
    channels = {note['channel'] for note in notes}
    channels.update((event['bytes'][0] & 15) + 1 for event in events if not event['is_meta'] and event['bytes'][0] < 240)
    if (not isinstance(initial_state, dict) or set(initial_state) != {'active_notes', 'sustain'}
            or initial_state['active_notes'] != 'none' or not isinstance(initial_state['sustain'], list)):
        raise PocketError('Lifecycle requires explicit initial active_notes:none and sustain states')
    initial = {}
    for state in initial_state['sustain']:
        if not isinstance(state, dict) or set(state) != {'channel', 'value'}:
            raise PocketError('Lifecycle initial sustain requires exactly channel and value')
        channel = integer(state['channel'], 'initial sustain channel', 1, 16)
        value = integer(state['value'], 'initial sustain value', 0, 127)
        if channel in initial:
            raise PocketError('Lifecycle initial sustain channels must be unique')
        initial[channel] = value
    if set(initial) != channels:
        raise PocketError('Lifecycle initial sustain must exactly cover every involved clip channel')
    cc, ignored, equal_time_tracks = [], [], {}
    for event in events:
        time = rational(event['time'])
        if time > horizon:
            continue
        data = event['bytes']
        if event['is_meta'] or data[0] >> 4 in (8, 9):
            continue
        if data[0] >= 240:
            raise PocketError('Lifecycle SysEx/system events have unsupported receiver semantics')
        if data[0] >> 4 == 11:
            controller = data[1]
            if controller in (66, 69) or controller >= 120:
                raise PocketError('Lifecycle sostenuto/hold2/reset/channel-mode controls are unsupported')
            if controller == 64:
                track = integer(event.get('track_index'), 'retained controller track', 0)
                equal_time_tracks.setdefault(time, set()).add(track)
                cc.append((time, event['order'], event))
                continue
        ignored.append(event['id'])
    if any(len(tracks) > 1 for tracks in equal_time_tracks.values()):
        raise PocketError('Lifecycle equal-time controller order across source tracks is ambiguous')
    return clip, notes, events, curves, initial, cc, ignored, coordinates


def _simulate(notes, initial, cc, horizon, tail, order):
    timeline, rows, excluded = [], {}, []
    off_priority, cc_priority = (0, 1) if order == 'note_off_cc_note_on' else (1, 0)
    for note in notes:
        onset, end = rational(note['onset']), rational(note['onset']) + rational(note['duration_qn'])
        if note['mute'] or onset > horizon:
            excluded.append({'note_id': note['id'], 'reason': 'muted' if note['mute'] else 'after_horizon'})
            continue
        rows[note['id']] = {'note_id': note['id'], 'channel': note['channel'], 'pitch': note['pitch'],
            'onset_qn': qn(onset), 'gate_off_qn': qn(end), 'release_qn': None, 'reserved_until_qn': None,
            'unresolved_reason': None, 'state_at_horizon': None, 'receiver_pairing_ambiguous': False}
        timeline.append((onset, 2, note['id'], 'on', note))
        if end <= horizon:
            timeline.append((end, off_priority, note['id'], 'off', note))
    for time, rank, event in cc:
        timeline.append((time, cc_priority, rank, 'cc', event))
    pedal = dict(initial)
    gates = {channel: set() for channel in initial}
    held = {channel: set() for channel in initial}
    occupancy, witnesses, expiry, conflicts = {}, {}, [], []

    def release(note_id, time):
        row = rows[note_id]
        row['release_qn'] = qn(time)
        row['reserved_until_qn'] = qn(time + tail)
        heapq.heappush(expiry, (time + tail, note_id))

    for time, _, _, kind, value in sorted(timeline):
        while expiry and expiry[0][0] <= time:
            _, note_id = heapq.heappop(expiry)
            row = rows[note_id]
            occupancy[(row['channel'], row['pitch']['midi_note'])].discard(note_id)
        if kind == 'cc':
            channel = (value['bytes'][0] & 15) + 1
            pedal[channel] = value['bytes'][2]
            if pedal[channel] < 64:
                for note_id in sorted(held[channel]):
                    release(note_id, time)
                held[channel].clear()
            continue
        note_id, channel = value['id'], value['channel']
        key = (channel, value['pitch']['midi_note'])
        if kind == 'on':
            active = occupancy.setdefault(key, set())
            witness = witnesses.setdefault(key, [])
            while witness and witness[0] not in active:
                heapq.heappop(witness)
            if active:
                conflicts.append({'time_qn': qn(time), 'channel': channel, 'midi_note': value['pitch']['midi_note'],
                                  'note_id': note_id, 'earlier_note_id': witness[0]})
            active.add(note_id)
            heapq.heappush(witness, note_id)
            gates[channel].add(note_id)
        else:
            gates[channel].remove(note_id)
            if pedal[channel] >= 64:
                held[channel].add(note_id)
            else:
                release(note_id, time)
    ambiguous_channels = {conflict['channel'] for conflict in conflicts}
    channel_rows = []
    for channel in sorted(initial):
        tail_ids = []
        for row in rows.values():
            if row['channel'] != channel:
                continue
            row['receiver_pairing_ambiguous'] = channel in ambiguous_channels
            if row['note_id'] in gates[channel]:
                row['state_at_horizon'] = 'active_gate'
                row['unresolved_reason'] = 'gate_active_at_horizon'
            elif row['note_id'] in held[channel]:
                row['state_at_horizon'] = 'sustain_held'
                row['unresolved_reason'] = 'sustain_held_at_horizon'
            elif rational(row['reserved_until_qn']) > horizon:
                row['state_at_horizon'] = 'tail_reserved'
                tail_ids.append(row['note_id'])
            else:
                row['state_at_horizon'] = 'released'
        channel_rows.append({'channel': channel, 'initial_sustain': initial[channel], 'final_sustain': pedal[channel],
            'active_gate_note_ids': sorted(gates[channel]), 'sustain_held_note_ids': sorted(held[channel]),
            'tail_reserved_note_ids': sorted(tail_ids), 'receiver_pairing_ambiguous': channel in ambiguous_channels,
            'ambiguous_attack_count': sum(conflict['channel'] == channel for conflict in conflicts)})
    return list(rows.values()), channel_rows, conflicts, excluded


def _fits(result):
    pending, size = [iter((result,))], 1
    while pending:
        try:
            item = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        size += len(item) if isinstance(item, str) else 1
        if size > 16384:
            return False
        if isinstance(item, dict):
            pending.append(iter(item.keys()))
            pending.append(iter(item.values()))
        elif isinstance(item, list):
            pending.append(iter(item))
    size = 1
    for chunk in json.JSONEncoder(indent=2, ensure_ascii=True, allow_nan=False).iterencode(result):
        size += len(chunk.encode())
        if size > 16384:
            return False
    return True


def _bounded(result):
    # No full-proof fields enter the preview; drop large rows honestly as needed.
    while not _fits(result):
        if result['notes']:
            result['notes'].pop()
            result['omitted_notes'] += 1
        else:
            raise PocketError('Lifecycle receipt metadata exceeds its 16 KiB bound')
    return result


def midi_lifecycle(material: ArtifactHandle | MaterialRecord, clip_id: str,
                   initial_state: LifecycleInitialState, horizon_qn: Rational | int,
                   release_tail_qn: Rational | int,
                   equal_time_order: Literal['note_off_cc_note_on', 'cc_note_off_note_on'],
                   source_basis: Literal['canonical_notes_retained_cc64'], store_root: str, request_id: str) -> dict:
    """Analyze one declared canonical-note/CC64 realization, without sending MIDI."""
    record = load_material(material, store_root)
    inputs = {'material': material, 'clip_id': clip_id, 'initial_state': initial_state,
        'horizon_qn': horizon_qn, 'release_tail_qn': release_tail_qn,
        'equal_time_order': equal_time_order, 'source_basis': source_basis}

    def work():
        horizon, tail = _qn(horizon_qn, 'lifecycle horizon'), _qn(release_tail_qn, 'declared release tail')
        clip, notes, events, curves, initial, cc, ignored, coordinates = _prepare(
            record, clip_id, initial_state, horizon, tail, equal_time_order, source_basis, store_root)
        findings, channels, conflicts, excluded = _simulate(notes, initial, cc, horizon, tail, equal_time_order)
        coverage = {'basis': 'symbolic', 'source_basis': source_basis, 'native_verified': False,
            'receiver_behavior': 'unknown', 'encoding': 'not_performed', 'allocation': 'not_performed',
            'performance': 'not_performed', 'listening': 'not_performed'}
        summary = {'notes': len(findings), 'excluded_notes': len(excluded), 'channels': len(channels),
            'unresolved_releases': sum(row['release_qn'] is None for row in findings),
            'ambiguous_attacks': len(conflicts), 'ambiguous_channels': sum(row['receiver_pairing_ambiguous'] for row in channels),
            'retained_cc64_events': len(cc), 'other_channel_events_not_modeled': len(ignored)}
        parent = put_record(record, store_root)
        proof = {'schema': 'pocket.midi-lifecycle/v1', 'material': parent,
            'material_revision': record['revision_sha256'], 'clip_id': clip['id'], 'coverage': coverage,
            'initial_state': initial_state, 'horizon_qn': qn(horizon), 'release_tail_qn': qn(tail),
            'release_tail_basis': 'caller_declared_not_measured', 'equal_time_order': equal_time_order,
            'boundary': 'events_at_horizon_processed;gate_and_reservation_intervals_half_open',
            'endpoint_order_basis': 'declared_canonical_realization_not_original_wire_order',
            'controller_coordinates': coordinates, 'summary': summary,
            'notes': findings, 'channels': channels, 'conflicts': conflicts, 'excluded_notes': excluded,
            'cc64_realization': [{'event_id': event['id'], 'time_qn': qn(time), 'order': rank,
                'channel': (event['bytes'][0] & 15) + 1, 'value': event['bytes'][2]} for time, rank, event in sorted(cc)],
            'original_note_events_excluded': [event['id'] for event in events if event['message_type'] in ('note_on', 'note_off')],
            'other_channel_events_not_modeled': ignored,
            'expression_curves_not_modeled': [curve['id'] for curve in curves],
            'reserved_through_horizon_note_ids': [row['note_id'] for row in findings if row['release_qn'] is None]}
        handle = put_record(proof, store_root)
        compact_channels = [{key: value for key, value in channel.items() if not key.endswith('_note_ids')}
                            | {key.replace('_note_ids', '_count'): len(value) for key, value in channel.items()
                               if key.endswith('_note_ids')} for channel in channels]
        result = receipt(request_id=request_id, artifacts={'report': handle}, report=handle,
            change_summary={'source_mutations': 0}, summary=summary, notes=findings[:16], channels=compact_channels,
            omitted_notes=max(0, len(findings) - 16), coverage=coverage,
            uncertainty=['Binary sustain and release tails are declared symbolic assumptions, not receiver or acoustic proof.',
                'Original raw note endpoints are retained evidence only; canonical endpoint/controller order is caller-declared.',
                'No safe channel allocation, reset, panic, scheduling or native expression route is qualified.'])
        return _bounded(result)
    return run_request(store_root, request_id, 'midi_lifecycle', inputs, work)
