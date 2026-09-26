# SPDX-License-Identifier: AGPL-3.0-only
"""Exact SMF serialization of recomputed declared expression plans.

This profile deliberately has one clip/track and no receiver setup authority.
The immutable plan retains allocation, lifecycle and quantization evidence.
"""
from __future__ import annotations

import io
import math

from .artifact_store import _verify_handles, put_record, read_record
from .errors import PocketError
from .material import qn, rational
from .midi_expression import expression_plan_for_export

_META = {'text', 'copyright', 'track_name', 'instrument_name', 'lyrics', 'marker',
         'cue_marker', 'set_tempo', 'time_signature', 'key_signature', 'end_of_track'}


def encode_expression(record, expression, store_root, smf_format, ppq, approved):
    """Return bytes and full fidelity evidence; never write the destination file."""
    from .midi_io import _message, _mido, _parse, _semantics

    if len(record['clips']) != 1:
        raise PocketError('Expression SMF export requires exactly one complete clip')
    supplied = None
    if isinstance(expression, dict) and expression.get('schema') == 'pocket.artifact-handle/v1':
        supplied = read_record(expression, store_root, 'pocket.midi-expression-plan/v1')
        _verify_handles(supplied, store_root)
        try:
            configuration = {key: supplied[key] for key in ('lifecycle', 'receiver_assumption', 'encoding')}
        except (KeyError, TypeError) as error:
            raise PocketError('Expression plan has missing configuration fields') from error
    else:
        configuration = expression
    plan = expression_plan_for_export(record, configuration, store_root)
    if supplied is not None and supplied != plan:
        raise PocketError('Expression plan does not equal independently recomputed material/lifecycle evidence')
    if any(source.get('kind') == 'live_clip' for source in record['sources']):
        raise PocketError('Expression export does not qualify opaque native note payload conversion')
    if any(source.get('kind') == 'smf' and source.get('format') == 2 for source in record['sources']):
        raise PocketError('Expression export cannot reinterpret independent SMF2 sequences')
    for field, kind in (('tempo_map_ref', 'set_tempo'), ('meter_map_ref', 'time_signature')):
        if record[field] is not None:
            mapping = read_record(record[field], store_root)
            wire = [event for event in record['events'] if event['message_type'] == kind]
            if mapping.get('basis') != 'ordered_smf_events' or mapping.get('events') != wire or not wire:
                raise PocketError('Expression export requires an exact retained SMF tempo/meter map')
    losses = ['muted_notes_omitted'] if plan['excluded_muted_note_ids'] else []
    if set(losses) - approved:
        raise PocketError('Unapproved MIDI degradation: ' + ', '.join(sorted(set(losses) - approved)))
    clip = record['clips'][0]
    batch = []
    meta_count = 0
    for event in record['events']:
        if not event['is_meta']:
            continue  # All channel/system events were qualified or refused by the recomputed plan.
        if event['message_type'] not in _META:
            raise PocketError('Expression export cannot remap routing/opaque metadata: ' + event['message_type'])
        message = _message(event)
        if message.type != 'end_of_track':
            batch.append((rational(event['time']), 0, event['order'], message))
            meta_count += 1
    mido = _mido()
    for event in plan['events']:
        batch.append((rational(event['time_qn']), 1, event['order'], mido.Message.from_bytes(event['bytes'])))
    length = rational(clip['length_qn'])
    # Reset events belong after the declared release tail, even beyond the musical clip.
    end = max([length, *(row[0] for row in batch)])
    times = [length, end, *(row[0] for row in batch)]
    if any(time < 0 for time in times):
        raise PocketError('Expression SMF cannot encode negative clip time')
    denominator = math.lcm(*(time.denominator for time in times))
    chosen_ppq = ppq if ppq is not None else (9600 if 9600 % denominator == 0 else denominator)
    if chosen_ppq > 32767 or chosen_ppq % denominator:
        raise PocketError('Unrepresentable expression timing at requested/available PPQ; no silent rounding')
    midi = mido.MidiFile(type=int(smf_format[-1]), ticks_per_beat=chosen_ppq)
    track, previous, expected = mido.MidiTrack(), 0, []
    for time, _kind, _order, message in sorted(batch, key=lambda row: row[:3]):
        tick = int(time * chosen_ppq)
        track.append(message.copy(time=tick - previous))
        expected.append((time, bytes(message.bytes())))
        previous = tick
    track.append(mido.MetaMessage('end_of_track', time=int(end * chosen_ppq) - previous))
    midi.tracks.append(track)
    stream = io.BytesIO()
    midi.save(file=stream)
    payload = stream.getvalue()
    if _semantics(_parse(payload)) != [expected]:
        raise PocketError('Expression export reparse differs from exact ordered realization')
    handle = put_record(plan, store_root)
    proof = {'schema': 'pocket.midi-expression-encoding/v1', 'plan': handle,
        'events': len(plan['events']), 'retained_meta_events': meta_count,
        'metadata_order': 'retained_meta_order_before_realized_channel_events_at_equal_time',
        'end_of_track': 'regenerated_after_clip_and_declared_release_reset',
        'musical_clip_end_qn': qn(length), 'encoded_end_qn': qn(end), 'tail_extension_qn': qn(end - length),
        'channel_semantics': 'explicit_new_member_channel_realization',
        'note_off_encoding': 'explicit_note_off_preserving_canonical_release_velocity',
        'maximum_pitch_error_cents': plan['maximum_pitch_error_cents'],
        'maximum_control_error_normalized': plan['maximum_control_error_normalized'],
        'receiver_configuration': 'declared_unverified_no_setup_messages', 'native_verified': False,
        'performance_ready': False, 'listening': 'not_performed'}
    return payload, chosen_ppq, losses, proof
