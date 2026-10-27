# SPDX-License-Identifier: AGPL-3.0-only
"""Optional Mido interchange with exact originals, ordered events and explicit loss gates."""
from __future__ import annotations

import gzip
import hashlib
import io
import math
import re
import struct
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

from .artifact_store import (
    _verify_handles,
    put_bytes,
    put_record,
    read_bytes,
    read_record,
    receipt,
    run_request,
)
from .errors import PocketError
from .expression_types import ExpressionConfiguration
from .material import (
    finalize_material,
    identifier,
    integer,
    load_material,
    make_note,
    new_material,
    qn,
    rational,
)
from .material_types import ArtifactHandle, CCStepBinding, MaterialRecord


def _mido():
    try:
        import mido
    except ImportError as error:
        raise PocketError("Install Pocket's optional MIDI extra to use Standard MIDI files") from error
    return mido



def _check_track_framing(track):
    """Bound wire parsing independently of Mido's permissive variable integers."""
    offset, running, ended = 0, None, False

    def variable(start):
        result = 0
        for size in range(1, 5):
            if start + size > len(track):
                raise PocketError('Malformed truncated MIDI variable integer')
            byte = track[start + size - 1]
            result = result * 128 + (byte & 127)
            if byte < 128:
                if size > 1 and result < 128 ** (size - 1):
                    raise PocketError('Malformed noncanonical MIDI variable integer')
                return result, start + size
        raise PocketError('Malformed MIDI variable integer exceeds four bytes')

    while offset < len(track):
        if ended:
            raise PocketError('Malformed MIDI events follow end_of_track')
        _, offset = variable(offset)
        if offset >= len(track):
            raise PocketError('Malformed MIDI missing event after delta')
        status = track[offset]
        if status < 128:
            if running is None:
                raise PocketError('Malformed MIDI running status without channel status')
            status = running
        else:
            offset += 1
            running = status if status < 240 else None
        if status == 255:
            if offset >= len(track):
                raise PocketError('Malformed MIDI missing meta type')
            meta_type = track[offset]
            length, offset = variable(offset + 1)
            ended = meta_type == 47
            if ended and length != 0:
                raise PocketError('Malformed MIDI end_of_track length')
        elif status in (240, 247):
            length, offset = variable(offset)
        elif 128 <= status < 240:
            length = 1 if status >> 4 in (12, 13) else 2
            if any(value >= 128 for value in track[offset:offset + length]):
                raise PocketError('Malformed MIDI channel data')
        else:
            system_lengths = {241: 1, 242: 2, 243: 1, 246: 0, 248: 0, 250: 0,
                              251: 0, 252: 0, 254: 0}
            if status not in system_lengths:
                raise PocketError('Malformed MIDI system status')
            length = system_lengths[status]
        offset += length
        if offset > len(track):
            raise PocketError('Malformed MIDI truncated event payload')
    if not ended:
        raise PocketError('Malformed MIDI missing end_of_track')

def _parse(payload):
    if len(payload) > 32 * 1024 * 1024:
        raise PocketError('MIDI file exceeds the 32 MiB import bound')
    try:
        if len(payload) < 14 or payload[:4] != b'MThd':
            raise PocketError('Malformed MIDI header')
        header_size, kind, track_count, _division = struct.unpack('>IHHH', payload[4:14])
        if header_size != 6 or kind not in (0, 1, 2) or track_count < 1 or (kind == 0 and track_count != 1):
            raise PocketError('Malformed MIDI header format/track count')
        offset = 14
        for _ in range(track_count):
            if offset + 8 > len(payload) or payload[offset:offset + 4] != b'MTrk':
                raise PocketError('Malformed MIDI track framing')
            size = int.from_bytes(payload[offset + 4:offset + 8], 'big')
            track_begin = offset + 8
            offset += 8 + size
            if offset > len(payload):
                raise PocketError('Malformed truncated MIDI track')
            _check_track_framing(payload[track_begin:offset])
        if offset != len(payload):
            raise PocketError('Malformed MIDI has undeclared trailing chunks or bytes')
        midi = _mido().MidiFile(file=io.BytesIO(payload), clip=False)
        if any(not track or track[-1].type != 'end_of_track' or
               sum(message.type == 'end_of_track' for message in track) != 1 for track in midi.tracks):
            raise PocketError('Malformed MIDI track must terminate with exactly one end_of_track')
        return midi
    except (ValueError, TypeError, OSError, EOFError, IndexError, KeyError) as error:
        raise PocketError(f'Malformed Standard MIDI file: {error}') from error


def import_smf(payload: bytes, store_root: str):
    midi = _parse(payload)
    sha = hashlib.sha256(payload).hexdigest()
    raw = put_bytes(payload, store_root, 'source.mid', 'pocket.smf-original/v1')
    if midi.ticks_per_beat <= 0:
        return new_material(sha, tracks=[], clips=[], notes=[], sources=[{'kind': 'smf', 'sha256': sha,
            'raw': raw, 'format': midi.type, 'division': midi.ticks_per_beat}],
            coverage={'editing_allowed': False, 'issues': [{'code': 'smpte_requires_mapping'}],
                      'raw_bytes': 'exact', 'notes': 'opaque', 'expression': 'opaque'},
            provenance={'provider': 'mido-import-v1', 'mido': str(_mido().version_info)})
    tracks, clips, notes, events, issues = [], [], [], [], []
    for track_index, track in enumerate(midi.tracks):
        track_id, clip_id = identifier('track', f'{sha}:{track_index}'), identifier('clip', f'{sha}:{track_index}')
        tracks.append({'id': track_id, 'name': track.name, 'source_track': track_index})
        active, track_notes, track_events = {}, [], []
        ticks = 0
        for order, message in enumerate(track):
            ticks += message.time
            if len(events) >= 200000:
                raise PocketError('MIDI import exceeds the 200000-event bound')
            event_id = identifier('event', f'{sha}:{track_index}:{order}')
            event = {'id': event_id, 'time': {'space': 'clip_qn', **qn(Fraction(ticks, midi.ticks_per_beat))},
                     'order': order, 'source_tick': ticks, 'track_index': track_index,
                     'message_type': message.type, 'is_meta': message.is_meta, 'bytes': message.bytes()}
            events.append(event)
            track_events.append(event_id)
            key = (getattr(message, 'channel', None), getattr(message, 'note', None))
            if message.type == 'note_on' and message.velocity > 0:
                if active.get(key):
                    issues.append({'code': 'ambiguous_same_pitch_overlap', 'track': track_index,
                                   'event_id': event_id, 'pairing': 'fifo_projection_only'})
                active.setdefault(key, []).append((ticks, message, event_id))
            elif message.type == 'note_off' or message.type == 'note_on' and message.velocity == 0:
                if not active.get(key):
                    issues.append({'code': 'unmatched_note_off', 'event_id': event_id})
                    continue
                start, attack, start_id = active[key].pop(0)
                if ticks <= start:
                    issues.append({'code': 'nonpositive_note_duration', 'event_id': event_id})
                    continue
                note = make_note(identifier('note', start_id), Fraction(start, midi.ticks_per_beat),
                    Fraction(ticks - start, midi.ticks_per_beat), attack.note, attack.velocity,
                    channel=attack.channel + 1, release=message.velocity, voice=f'voice:{track_index}:{attack.channel + 1}',
                    source={'kind': 'smf', 'source_sha256': sha, 'track_index': track_index,
                            'on_event_id': start_id, 'off_event_id': event_id,
                            'off_encoding': message.type})
                notes.append(note)
                track_notes.append(note['id'])
        for pending in active.values():
            for _, _, event_id in pending:
                issues.append({'code': 'unterminated_note', 'event_id': event_id})
        clips.append({'id': clip_id, 'track_id': track_id, 'origin': {'space': 'phrase_qn', **qn(0)},
                      'length_qn': qn(Fraction(ticks, midi.ticks_per_beat)), 'loop': False,
                      'note_ids': track_notes, 'event_ids': track_events, 'curve_ids': []})
    if midi.type == 2:
        issues.append({'code': 'asynchronous_smf_type_2', 'action': 'independent_sequence_selection_not_implemented'})
    record = new_material(sha, tracks=tracks, clips=clips, notes=notes, events=events,
        sources=[{'kind': 'smf', 'sha256': sha, 'raw': raw, 'format': midi.type, 'ppq': midi.ticks_per_beat}],
        coverage={'editing_allowed': not issues, 'issues': issues, 'raw_bytes': 'exact',
                  'controllers': 'ordered_wire_events', 'expression': 'wire_only_no_mpe_inference',
                  'notes': 'exact' if not issues else 'qualified_projection', 'native': 'not_verified'},
        provenance={'provider': 'mido-import-v1', 'mido': str(_mido().version_info)})
    tempo = [e for e in events if e['message_type'] == 'set_tempo']
    meter = [e for e in events if e['message_type'] == 'time_signature']
    for name, values in (('tempo_map_ref', tempo), ('meter_map_ref', meter)):
        if values:
            record[name] = put_record({'schema': 'pocket.' + name.replace('_ref', '').replace('_', '-') + '/v1',
                                       'basis': 'ordered_smf_events', 'events': values}, store_root)
    return finalize_material(record)


_LIVE_EMPTY_NOTE_BUILD = {
    'MajorVersion': '5', 'MinorVersion': '12.0_12402', 'SchemaChangeCount': '5',
    'Creator': 'Ableton Live 12.4.5', 'Revision': '225ce5e356e024356d5210512bae46fb466f6968',
}
_LIVE_EMPTY_NOTE_PROFILE = 'live-12.4.5-empty-note-metadata/v1'


def _qualified_empty_note_metadata(node, build):
    """Recognize only the observed conventional Notes tree; never strip unknown state.

    This is a saved-file reader qualification. Native note IDs and allocation
    counters remain in the original tree/ALS, not in the SMF derivative.
    """
    if node is None or build != _LIVE_EMPTY_NOTE_BUILD:
        return False
    if any((n.text or '').strip() or (n.tail or '').strip() for n in node.iter()):
        return False
    if node.tag != 'Notes' or node.attrib or [n.tag for n in node] != [
            'KeyTracks', 'PerNoteEventStore', 'NoteProbabilityGroups',
            'ProbabilityGroupIdGenerator', 'NoteIdGenerator']:
        return False
    keys, expressions, probabilities, probability_ids, note_ids = list(node)
    if any(n.attrib for n in (keys, expressions, probabilities, probability_ids, note_ids)):
        return False
    if (len(expressions) != 1 or expressions[0].tag != 'EventLists' or
            expressions[0].attrib or len(expressions[0]) or len(probabilities)):
        return False
    def counter(container):
        if (len(container) != 1 or container[0].tag != 'NextId' or len(container[0]) or
                set(container[0].attrib) != {'Value'}):
            return None
        value = container[0].get('Value')
        return int(value) if re.fullmatch(r'[1-9][0-9]{0,9}', value) and int(value) <= 2**31 - 1 else None
    if counter(probability_ids) != 1:
        return False
    seen_keys, seen_pitches, seen_notes = set(), set(), set()
    for key in keys:
        ident = key.get('Id')
        if (key.tag != 'KeyTrack' or set(key.attrib) != {'Id'} or
                not re.fullmatch(r'0|[1-9][0-9]{0,9}', ident) or int(ident) > 2**31 - 1 or
                ident in seen_keys or [n.tag for n in key] != ['Notes', 'MidiKey']):
            return False
        seen_keys.add(ident)
        events, pitch = list(key)
        value = pitch.get('Value')
        if (events.attrib or set(pitch.attrib) != {'Value'} or len(pitch) or
                not re.fullmatch(r'0|[1-9][0-9]{0,2}', value) or int(value) > 127 or value in seen_pitches):
            return False
        seen_pitches.add(value)
        for event in events:
            ident = event.get('NoteId')
            if (event.tag != 'MidiNoteEvent' or len(event) or set(event.attrib) != {
                    'Time', 'Duration', 'Velocity', 'OffVelocity', 'NoteId'} or
                    not re.fullmatch(r'[1-9][0-9]{0,9}', ident) or int(ident) >= 2**31 - 1 or ident in seen_notes):
                return False
            seen_notes.add(ident)
    return counter(note_ids) == max((int(i) for i in seen_notes), default=0) + 1


def import_live_clip(source, store_root):
    from .thread import _paths, _tree
    from .thread_queries import _load
    snapshot = _load(source.get('thread_handle'))
    set_map = snapshot['set_map']
    candidates = [c for c in set_map['clips'] if c['id'] == source.get('clip_id')]
    if len(candidates) != 1 or candidates[0]['type'] != 'midi':
        raise PocketError('Expected one exact saved MIDI clip identity')
    clip = candidates[0]
    if clip['loop'].get('LoopOn') is not False:
        raise PocketError('Looped native clips require occurrence mapping before material import')
    tree = clip.get('notes')
    source_path = Path(set_map['path'])
    source_bytes = source_path.read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != set_map['sha256']:
        raise PocketError('Stale saved set while retaining native source bytes')
    xml = gzip.decompress(source_bytes) if source_bytes[:2] == b'\x1f\x8b' else source_bytes
    if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
        raise PocketError('XML entity declarations are not supported')
    root = ET.fromstring(xml)
    matched = [node for node, path in _paths(root).items() if path == clip['xml_path']]
    if len(matched) != 1 or _tree(matched[0].find('Notes')) != tree:
        raise PocketError('Saved note tree does not match its exact snapshot location')
    selected_native = matched[0]
    envelopes = selected_native.find('Envelopes')
    empty_envelopes = envelopes is None or (
        not envelopes.attrib and len(envelopes) == 1 and envelopes[0].tag == 'Envelopes'
        and not envelopes[0].attrib and not len(envelopes[0])
        and all(not (n.text or '').strip() and not (n.tail or '').strip() for n in envelopes.iter()))
    qualified = (_qualified_empty_note_metadata(selected_native.find('Notes'), dict(root.attrib))
                 and clip['scope'] == 'arrangement' and clip['disabled'] is False and empty_envelopes
                 and clip.get('groove_id') in (None, '-1') and not clip.get('clip_envelopes')
                 and clip['loop'].get('StartRelative') == 0 and clip['loop'].get('LoopStart') == 0)
    original_set = put_bytes(source_bytes, store_root, 'source.als', 'pocket.saved-set/v1')
    raw = put_record({'schema': 'pocket.native-notes-tree/v1', 'set_sha256': set_map['sha256'],
                      'clip_id': clip['id'], 'tree': tree}, store_root)
    seed = set_map['sha256'] + ':' + clip['id']
    notes, issues = [], []

    metadata_nodes = {id(node) for node in tree['children'][1:]} if qualified else set()

    def walk(node, pitch=None):
        if not node or id(node) in metadata_nodes:
            return
        if node['tag'] not in {'Notes', 'KeyTracks', 'KeyTrack', 'MidiKey', 'MidiNoteEvent'}:
            issues.append({'code': 'opaque_native_note_structure', 'tag': node['tag']})
        if node['tag'] == 'KeyTrack':
            keys = [c for c in node['children'] if c['tag'] == 'MidiKey']
            pitch = int(keys[0]['attributes']['Value']) if len(keys) == 1 else None
        if node['tag'] == 'MidiNoteEvent':
            a = node['attributes']
            if pitch is None:
                raise PocketError('Saved MIDI note has no unambiguous key track')
            ordinal = len(notes)
            try:
                onset, duration = Fraction(a['Time']), Fraction(a['Duration'])
                velocity, release = Fraction(a['Velocity']), Fraction(a.get('OffVelocity', '64'))
                if velocity.denominator != 1 or release.denominator != 1:
                    raise PocketError('Fractional native velocity needs a qualified resolution profile')
                note = make_note(identifier('note', f'{seed}:{ordinal}'), onset, duration, pitch, int(velocity),
                    release=int(release), source={'kind': 'live_clip', 'set_sha256': set_map['sha256'],
                            'clip_id': clip['id'], 'native_note_id': a.get('NoteId'), 'raw_attributes': a})
            except (ValueError, KeyError, ZeroDivisionError) as error:
                raise PocketError(f'Malformed saved native note: {error}') from error
            note['mute'] = a.get('IsEnabled', 'true') == 'false'
            rich = set(a) - {'Time', 'Duration', 'Velocity', 'OffVelocity', 'NoteId', 'IsEnabled'}
            if rich:
                issues.append({'code': 'native_note_attributes_retained', 'note_id': note['id'], 'fields': sorted(rich)})
            notes.append(note)
        for child in node.get('children', []):
            walk(child, pitch)
    walk(tree)
    native_length = max([Fraction(str(clip['end_beat'] - clip['start_beat']))] +
                        [rational(n['onset']) + rational(n['duration_qn']) for n in notes])
    new_clip = {'id': identifier('clip', seed), 'track_id': identifier('track', seed),
        'origin': {'space': 'arrangement_qn', **qn(Fraction(str(clip['start_beat'])))},
        'length_qn': qn(native_length), 'loop': False, 'note_ids': [n['id'] for n in notes], 'event_ids': [], 'curve_ids': []}
    record = new_material(seed, tracks=[{'id': new_clip['track_id'], 'name': clip['name']}],
        clips=[new_clip], notes=notes, sources=[{'kind': 'live_clip', 'set_sha256': set_map['sha256'],
                                              'clip_id': clip['id'], 'raw': raw, 'original_set': original_set}],
        coverage={'editing_allowed': not issues, 'issues': issues, 'native_note_tree': 'exact_opaque',
                  'expression': 'absent_in_qualified_note_tree' if qualified else 'opaque_native_not_normalized',
                  'native_note_metadata_profile': _LIVE_EMPTY_NOTE_PROFILE if qualified else None,
                  'native_source_bytes': 'exact', 'native': 'saved_inspection_only'},
        provenance={'provider': 'saved-live-note-projection-v2', 'native_build': set_map.get('creator')})
    _load(source['thread_handle'])  # Recheck source/dependency freshness after retaining bytes.
    # A saved XML projection does not qualify a writer or establish reopened state.
    return record


def _message(event):
    try:
        cls = _mido().MetaMessage if event['is_meta'] else _mido().Message
        message = cls.from_bytes(event['bytes'])
        if message.type != event['message_type'] or message.is_meta != event['is_meta']:
            raise PocketError('Preserved MIDI event bytes disagree with declared type')
        return message
    except (ValueError, TypeError, KeyError) as error:
        raise PocketError(f'Invalid preserved MIDI event: {error}') from error


def _semantics(midi):
    result = []
    for track_index, track in enumerate(midi.tracks):
        tick = 0
        rows = []
        for message in track:
            tick += message.time
            if message.type != 'end_of_track':
                rows.append((Fraction(tick, midi.ticks_per_beat), bytes(message.bytes())))
        result.append(rows)
    return result


def _cc_steps(record, bindings, store_root, smf_format):
    """Validate the explicit CC1/11 derivative route without interpreting opaque IDs."""
    if bindings is None:
        return [], []
    if not isinstance(bindings, list) or not 1 <= len(bindings) <= 16:
        raise PocketError('cc_step_bindings requires 1–16 explicit bindings')
    curves = {curve['id']: curve for curve in record['curves']}
    owners = {curve_id: (index, clip) for index, clip in enumerate(record['clips'])
              for curve_id in clip['curve_ids']}
    clips = {clip['id']: (index, clip) for index, clip in enumerate(record['clips'])}
    seen_ids, destinations, steps, summaries = set(), set(), [], []
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) not in (
                {'curve_id', 'controller', 'same_tick_order'}, {'curve', 'clip_id', 'controller', 'same_tick_order'}):
            raise PocketError('CC binding requires embedded curve_id or external curve+clip_id, controller and same_tick_order')
        if 'curve' in binding:
            from .curves import _validate as validate_curve
            curve = read_record(binding['curve'], store_root, 'pocket.curve/v1')
            _verify_handles(curve, store_root)
            validate_curve(curve)
            clip_id = binding['clip_id']
            if not isinstance(clip_id, str) or not clip_id.strip() or len(clip_id) > 1024 or clip_id not in clips:
                raise PocketError('External CC curve requires an existing bounded clip_id')
            clip_index, clip = clips[clip_id]
            curve_id = curve['curve_id']
        else:
            curve_id = binding['curve_id']
            if not isinstance(curve_id, str) or curve_id not in curves:
                raise PocketError('CC curve binding is unresolved')
            curve = curves[curve_id]
            clip_index, clip = owners[curve_id]
        if not isinstance(curve_id, str) or not curve_id.strip() or len(curve_id) > 1024:
            raise PocketError('CC curve_id requires nonempty text of at most 1024 characters')
        if curve_id in seen_ids or ('curve' in binding and curve_id in curves):
            raise PocketError('CC curve binding is duplicate or ambiguously embedded and external')
        integer(binding['controller'], 'CC controller', 0, 127)
        if binding['controller'] not in (1, 11):
            raise PocketError('CC step export supports only explicit CC1 and CC11; no sustain, bank or channel-mode route')
        if binding['same_tick_order'] != 'before_existing':
            raise PocketError('CC step export requires explicit same_tick_order=before_existing')
        target = curve['target']
        expected_fields = {'kind', 'target_id', 'scope', 'unit', 'value_min', 'value_max', 'quantized',
                           'values', 'ownership', 'value_mode', 'channel'}
        expected = {'kind': 'cc', 'scope': 'channel', 'unit': 'midi1_7bit', 'value_min': 0, 'value_max': 127,
                    'quantized': True, 'values': list(range(128)), 'ownership': 'none', 'value_mode': 'absolute'}
        if (set(target) != expected_fields or any(target[key] != value for key, value in expected.items())
                or curve['space'] != 'clip_qn' or curve['interpolation'] != 'step'):
            raise PocketError('Bound CC curve must be unowned absolute channel clip_qn steps in the complete MIDI1 7-bit domain')
        # Context is an optional source reference, never an implicit coordinate conversion.
        for field, schema in (('context', 'pocket.context/v1'), ('parent', 'pocket.curve/v1')):
            if curve[field] is not None:
                read_record(curve[field], store_root, schema)
        destination = (target['channel'], binding['controller'])
        if destination in destinations:
            raise PocketError('CC channel/controller destination conflicts across exported clips')
        length = rational(clip['length_qn'])
        for point in curve['points']:
            time, value = rational(point['time']), point['value']
            if not 0 <= time <= length:
                raise PocketError('CC point exceeds its owning clip bounds')
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value) or not 0 <= value <= 127:
                raise PocketError('CC step value must be an exact MIDI1 7-bit integer')
            steps.append((clip_index, time, target['channel'] - 1, binding['controller'], int(value)))
        summaries.append({**binding, 'curve_id': curve_id, 'clip_id': clip['id'], 'target_id': target['target_id'],
                          'channel': target['channel'], 'point_count': len(curve['points'])})
        seen_ids.add(curve_id)
        destinations.add(destination)
    for event in record['events']:
        if event['message_type'] == 'control_change':
            wire = event['bytes']
            if ((wire[0] & 15) + 1, wire[1]) in destinations:
                raise PocketError('CC destination conflicts with retained raw controller events')
    if smf_format == 'smf1':
        # Separate SMF tracks have no portable cross-track equal-tick order.
        # Keep each bound MIDI channel wholly within its owning output track.
        channel_clips = {}
        for summary in summaries:
            channel_clips.setdefault(summary['channel'], set()).add(summary['clip_id'])
        if any(len(ids) != 1 for ids in channel_clips.values()):
            raise PocketError('SMF1 CC channel ownership cannot span different clips/tracks')
        note_by_id = {note['id']: note for note in record['notes']}
        event_by_id = {event['id']: event for event in record['events']}
        for clip in record['clips']:
            channels = {note_by_id[key]['channel'] for key in clip['note_ids']}
            channels.update((event_by_id[key]['bytes'][0] & 15) + 1 for key in clip['event_ids']
                            if 128 <= event_by_id[key]['bytes'][0] < 240)
            if any(channel in channel_clips and clip['id'] not in channel_clips[channel] for channel in channels):
                raise PocketError('SMF1 bound CC channel has note/raw activity in another clip/track; cross-track order is unqualified')
    return steps, summaries


def midi_export(material: ArtifactHandle | MaterialRecord, store_root: str, output_path: str,
                request_id: str, format: str = 'smf1', ppq: int | None = None,
                loss_policy: str = 'reject_unapproved', approved_losses: list[str] | None = None,
                cc_step_bindings: list[CCStepBinding] | None = None,
                expression: ExpressionConfiguration | ArtifactHandle | None = None) -> dict:
    """Export a new SMF derivative with exact tick timing, source retention and round-trip evidence.

    Default export rejects unrepresentable rational timing and rich-field losses.
    Imported wire events retain equal-time order; ambiguous lifecycles permit
    byte-identical original export only. No MPE interpretation is inferred.
    Explicit CC1/11 bindings encode eligible clip-local steps before existing
    equal-tick events within each SMF1 track or the merged SMF0 track. Bound
    SMF1 channels cannot have activity in other tracks. No other curves execute implicitly.
    """
    record = load_material(material, store_root)
    if expression is not None and cc_step_bindings is not None:
        raise PocketError('Expression and CC step export cannot be combined in this profile')
    cc_steps, cc_summaries = _cc_steps(record, cc_step_bindings, store_root, format)
    if format not in ('smf0', 'smf1'):
        raise PocketError('Export supports SMF 0 or SMF 1 only')
    if ppq is not None:
        integer(ppq, 'PPQ', 1, 32767)
    if loss_policy not in ('reject_unapproved', 'approved'):
        raise PocketError('loss_policy must be reject_unapproved or approved')
    approved = set(approved_losses or [])
    if approved and loss_policy != 'approved':
        raise PocketError('Named degradation approvals require loss_policy=approved')
    destination = Path(output_path).expanduser().resolve()
    inputs = {'material': material, 'output_path': str(destination), 'format': format,
              'ppq': ppq, 'loss_policy': loss_policy, 'approved_losses': sorted(approved)}
    if cc_step_bindings is not None:
        inputs['cc_step_bindings'] = cc_step_bindings
    if expression is not None:
        inputs['expression'] = expression
    # Reconcile an existing completed request before the new-only path check.
    def work():
        if destination.exists():
            raise PocketError('MIDI output destination already exists; export never overwrites')
        if expression is not None:
            from .midi_expression_io import encode_expression
            payload, chosen_ppq, losses, encoding = encode_expression(
                record, expression, store_root, format, ppq, approved)
            midi_handle = put_bytes(payload, store_root, 'export.mid', 'pocket.smf-derivative/v1')
            sidecar = {'schema': 'pocket.midi-export/v1', 'material_revision': record['revision_sha256'],
                'material': put_record(record, store_root), 'midi': midi_handle, 'format': format, 'ppq': chosen_ppq,
                'losses': losses, 'maximum_quantization_error_qn': qn(0),
                'roundtrip': {'status': 'verified_reparse', 'events_equal': True},
                'note_identity': 'retained_in_material_sidecar_not_smf', 'sequence_time_space': 'clip_qn',
                'clip_origins': [clip['origin'] for clip in record['clips']], 'expression_encoding': encoding,
                'native_roundtrip': 'not_performed', 'listening': 'not_performed'}
            sidecar_handle = put_record(sidecar, store_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open('xb') as output:
                output.write(payload)
            return receipt(request_id=request_id, artifacts={'midi': midi_handle, 'sidecar': sidecar_handle},
                midi=midi_handle, sidecar=sidecar_handle, output_path=str(destination),
                change_summary={'notes': len(record['notes']), 'bytes': len(payload)},
                coverage={'fidelity': sidecar, 'native': 'not_verified'}, warnings=losses)
        origins = {(c['origin']['space'], rational(c['origin'])) for c in record['clips']}
        if len(origins) > 1:
            raise PocketError('Multiple clip origins require explicit sequence placement before MIDI export')
        losses = []
        for field, kind in (('tempo_map_ref', 'set_tempo'), ('meter_map_ref', 'time_signature')):
            if record[field] is not None:
                map_record = read_record(record[field], store_root)
                wire = [event for event in record['events'] if event['message_type'] == kind]
                if map_record.get('basis') != 'ordered_smf_events' or map_record.get('events') != wire or not wire:
                    losses.append(field.replace('_ref', '_projection'))
        encoded_ids = {binding['curve_id'] for binding in cc_summaries}
        if any(curve['id'] not in encoded_ids for curve in record['curves']) or any(n['expression_refs'] for n in record['notes']):
            losses.append('canonical_expression_curves')
        if any(n['pitch']['cents_offset'] != 0 for n in record['notes']):
            losses.append('microtonal_pitch')
        if any(n['pitch'].get('tuning_ref') != 'tuning:12tet-a440' for n in record['notes']):
            losses.append('custom_tuning')
        if any(n['mute'] for n in record['notes']):
            losses.append('muted_notes_omitted')
        if any(s.get('kind') == 'live_clip' for s in record['sources']):
            losses.append('opaque_native_note_payload')
        if any(s.get('kind') == 'smf' and s.get('format') == 2 for s in record['sources']):
            raise PocketError('SMF type 2 requires explicit independent-sequence selection')
        if set(losses) - approved:
            raise PocketError('Unapproved MIDI degradation: ' + ', '.join(sorted(set(losses) - approved)))
        sources = [s for s in record['sources'] if s.get('kind') == 'smf']
        target_type = int(format[-1])
        untouched = False
        if len(sources) == 1 and record['parent_revision'] is None and record['provenance'].get('provider') == 'mido-import-v1':
            original = import_smf(read_bytes(sources[0]['raw'], store_root), store_root)
            untouched = original['revision_sha256'] == record['revision_sha256']
        if (cc_step_bindings is None and untouched and len(sources) == 1
                and ppq in (None, sources[0].get('ppq')) and target_type == sources[0]['format']):
            payload = read_bytes(sources[0]['raw'], store_root)
            parsed = _parse(payload)
            chosen_ppq = parsed.ticks_per_beat
            roundtrip = {'status': 'exact_original_bytes', 'events_equal': True}
        else:
            if record['coverage'].get('editing_allowed') is not True:
                raise PocketError('Ambiguous/opaque material only permits byte-identical original export')
            mido = _mido()
            times = [rational(n['onset']) for n in record['notes']]
            times += [rational(n['onset']) + rational(n['duration_qn']) for n in record['notes']]
            times += [rational(e['time']) for e in record['events']]
            times += [rational(c['length_qn']) for c in record['clips']]
            times += [step[1] for step in cc_steps]
            if any(t < 0 for t in times):
                raise PocketError('SMF cannot encode negative time; choose an explicit origin derivative')
            denominator = math.lcm(*(t.denominator for t in times)) if times else 1
            chosen_ppq = ppq if ppq is not None else (9600 if 9600 % denominator == 0 else denominator)
            if chosen_ppq > 32767 or chosen_ppq % denominator:
                raise PocketError('Unrepresentable timing at requested/available PPQ; no silent rounding')
            midi = mido.MidiFile(type=target_type, ticks_per_beat=chosen_ppq)
            events_by_id = {e['id']: e for e in record['events']}
            batches = [[] for _ in record['clips']] or [[]]
            note_by_id = {n['id']: n for n in record['notes']}
            for clip_index, clip in enumerate(record['clips']):
                batch = batches[clip_index]
                for event_id in clip['event_ids']:
                    event = events_by_id[event_id]
                    parsed_message = _message(event)
                    if event['message_type'] in ('note_on', 'note_off', 'end_of_track'):
                        continue
                    batch.append((rational(event['time']), event['order'] * 4 + 2, parsed_message))
                for ordinal, note_id in enumerate(clip['note_ids']):
                    note = note_by_id[note_id]
                    if note['mute']:
                        continue
                    binding = note['source_binding'] or {}
                    on_event = events_by_id.get(binding.get('on_event_id'))
                    off_event = events_by_id.get(binding.get('off_event_id'))
                    on_order = on_event['order'] * 4 + 2 if on_event else 4 * len(record['events']) + ordinal * 2 + 1
                    off_order = off_event['order'] * 4 + 2 if off_event else -4 * len(record['notes']) + ordinal * 2
                    onset, gate = rational(note['onset']), rational(note['duration_qn'])
                    off_kind = 'note_on' if binding.get('off_encoding') == 'note_on' and note['release_velocity']['value'] == 0 else 'note_off'
                    batch.extend([(onset, on_order, mido.Message('note_on', note=note['pitch']['midi_note'],
                        velocity=note['velocity']['value'], channel=note['channel'] - 1)),
                        (onset + gate, off_order, mido.Message(off_kind, note=note['pitch']['midi_note'],
                        velocity=note['release_velocity']['value'], channel=note['channel'] - 1))])
            # Globally distinct ranks keep binding/point order even after SMF0
            # merging. All original ranks and their relative order stay intact.
            first_cc_order = min((row[1] for batch in batches for row in batch), default=0) - 4 * (len(cc_steps) + 1)
            for ordinal, (clip_index, time, channel, controller, value) in enumerate(cc_steps):
                batches[clip_index].append((time, first_cc_order + 4 * ordinal,
                    mido.Message('control_change', channel=channel, control=controller, value=value)))
            if target_type == 0:
                batches = [[(time, order + Fraction(track_index, max(1, len(batches))), message)
                            for track_index, batch in enumerate(batches) for time, order, message in batch]]
            expected = []
            lengths = ([max((rational(c['length_qn']) for c in record['clips']), default=Fraction(0))]
                       if target_type == 0 else [rational(c['length_qn']) for c in record['clips']] or [Fraction(0)])
            for batch_index, batch in enumerate(batches):
                track, prior, expected_track = mido.MidiTrack(), 0, []
                for time, order, message in sorted(batch, key=lambda x: (x[0], x[1])):
                    absolute = int(time * chosen_ppq)
                    track.append(message.copy(time=absolute - prior))
                    expected_track.append((time, bytes(message.bytes())))
                    prior = absolute
                end_tick = int(lengths[batch_index] * chosen_ppq)
                if end_tick < prior:
                    raise PocketError('Material note/event exceeds its clip length; extend the clip explicitly')
                track.append(mido.MetaMessage('end_of_track', time=end_tick - prior))
                midi.tracks.append(track)
                expected.append(expected_track)
            stream = io.BytesIO()
            midi.save(file=stream)
            payload = stream.getvalue()
            if _semantics(_parse(payload)) != expected:
                raise PocketError('Independent export parse differs from expected ordered event semantics')
            roundtrip = {'status': 'verified_reparse', 'events_equal': True}
        midi_handle = put_bytes(payload, store_root, 'export.mid', 'pocket.smf-derivative/v1')
        sidecar = {'schema': 'pocket.midi-export/v1', 'material_revision': record['revision_sha256'],
                   'midi': midi_handle, 'format': format, 'ppq': chosen_ppq,
                   'losses': sorted(set(losses)), 'maximum_quantization_error_qn': qn(0),
                   'roundtrip': roundtrip, 'note_identity': 'retained_in_material_sidecar_not_smf',
                   'sequence_time_space': 'clip_qn', 'clip_origins': [c['origin'] for c in record['clips']],
                   'native_roundtrip': 'not_performed', 'listening': 'not_performed'}
        if cc_step_bindings is not None:
            sidecar['material'] = put_record(record, store_root)
            sidecar['cc_step_encoding'] = {
                'schema': 'pocket.cc-step-encoding/v1', 'bindings': cc_summaries,
                'binding_count': len(cc_summaries), 'point_count': len(cc_steps),
                'same_tick_order': 'before_existing',
                'same_tick_scope': 'merged_smf0' if target_type == 0 else 'within_each_smf_track',
                'maximum_quantization_error_qn': qn(0),
                'initialization': 'only_explicit_points', 'receiver_verification': 'not_performed'}
        sidecar_handle = put_record(sidecar, store_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open('xb') as output:
            output.write(payload)
        fidelity = sidecar
        if cc_step_bindings is not None:
            fidelity = {key: value for key, value in sidecar.items() if key != 'clip_origins'}
            fidelity.update(clip_count=len(record['clips']), cc_step_encoding={
                key: value for key, value in sidecar['cc_step_encoding'].items() if key != 'bindings'})
        return receipt(request_id=request_id, artifacts={'midi': midi_handle, 'sidecar': sidecar_handle},
            midi=midi_handle, sidecar=sidecar_handle, output_path=str(destination),
            change_summary={'notes': len(record['notes']), 'bytes': len(payload)},
            coverage={'fidelity': fidelity, 'native': 'not_verified'}, warnings=sorted(set(losses)))
    result = run_request(store_root, request_id, 'midi_export', inputs, work)
    if not destination.is_file() or hashlib.sha256(destination.read_bytes()).hexdigest() != result['midi']['sha256']:
        raise PocketError('Export destination changed or disappeared after publication')
    return result
