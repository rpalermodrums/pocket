# SPDX-License-Identifier: AGPL-3.0-only
"""Immutable, rational-time musical material; independent of any DAW or instrument."""
from __future__ import annotations

import copy
import hashlib
import math
import re
import uuid
from fractions import Fraction
from pathlib import Path

from .artifact_store import (
    _verify_handles,
    canonical_bytes,
    digest,
    put_record,
    read_record,
    receipt,
    run_request,
)
from .errors import PocketError
from .material_types import ArtifactHandle, MaterialRecord, MaterialSelection, MaterialSource

SCHEMA = 'pocket.material/v1'
MAX_NOTES = 100000
REQUIRED = {'schema', 'material_id', 'revision_sha256', 'parent_revision', 'sources', 'tracks',
            'clips', 'notes', 'events', 'curves', 'tempo_map_ref', 'meter_map_ref', 'coverage', 'provenance'}


def integer(value, name, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise PocketError(f'{name} must be an integer')
    if low is not None and value < low or high is not None and value > high:
        raise PocketError(f'{name} is outside its permitted range')
    return value


def rational(value, name='quarter notes') -> Fraction:
    if isinstance(value, int) and not isinstance(value, bool):
        return Fraction(value)
    if not isinstance(value, dict) or set(value) - {'n', 'd', 'space'} or not {'n', 'd'} <= value.keys():
        raise PocketError(f'{name} requires an integer or reduced rational {{n,d}}')
    n, d = integer(value['n'], name + ' numerator'), integer(value['d'], name + ' denominator', 1)
    if math.gcd(n, d) != 1:
        raise PocketError(f'{name} must be reduced')
    return Fraction(n, d)


def qn(value):
    value = Fraction(value)
    return {'n': value.numerator, 'd': value.denominator}


def identifier(kind, seed):
    return f'{kind}:{uuid.uuid5(uuid.NAMESPACE_URL, "pocket:" + kind + ":" + str(seed))}'


def material_digest(record):
    return digest({key: value for key, value in record.items() if key != 'revision_sha256'})


def finalize_material(record):
    result = copy.deepcopy(record)
    result['revision_sha256'] = material_digest(result)
    return validate_material(result)



def _validate_wire_event(event):
    payload = event['bytes']
    status = payload[0]
    if event['is_meta']:
        if status != 255 or len(payload) < 3:
            raise PocketError('Malformed MIDI meta event')
        length, index = 0, 2
        while True:
            if index >= len(payload) or index > 5:
                raise PocketError('Malformed MIDI meta length')
            byte = payload[index]
            length = length * 128 + (byte & 127)
            index += 1
            if byte < 128:
                break
        if len(payload) - index != length:
            raise PocketError('MIDI meta payload length mismatch')
        kinds = {0: 'sequence_number', 1: 'text', 2: 'copyright', 3: 'track_name', 4: 'instrument_name',
                 5: 'lyrics', 6: 'marker', 7: 'cue_marker', 9: 'device_name', 32: 'channel_prefix',
                 33: 'midi_port', 47: 'end_of_track', 81: 'set_tempo', 84: 'smpte_offset',
                 88: 'time_signature', 89: 'key_signature', 127: 'sequencer_specific'}
        expected = kinds.get(payload[1], 'unknown_meta')
        fixed = {0: 2, 32: 1, 33: 1, 47: 0, 81: 3, 84: 5, 88: 4, 89: 2}
        if payload[1] in fixed and length != fixed[payload[1]]:
            raise PocketError('MIDI meta event has invalid fixed length')
    elif status == 240:
        expected = 'sysex'
        if len(payload) < 2 or payload[-1] != 247 or any(x > 127 for x in payload[1:-1]):
            raise PocketError('Malformed MIDI SysEx event')
    else:
        channel = {8: ('note_off', 3), 9: ('note_on', 3), 10: ('polytouch', 3),
                   11: ('control_change', 3), 12: ('program_change', 2),
                   13: ('aftertouch', 2), 14: ('pitchwheel', 3)}
        system = {241: ('quarter_frame', 2), 242: ('songpos', 3), 243: ('song_select', 2),
                  246: ('tune_request', 1), 248: ('clock', 1), 250: ('start', 1),
                  251: ('continue', 1), 252: ('stop', 1), 254: ('active_sensing', 1), 255: ('reset', 1)}
        kind = channel.get(status >> 4) if status < 240 else system.get(status)
        if kind is None or len(payload) != kind[1] or any(x > 127 for x in payload[1:]):
            raise PocketError('Malformed MIDI channel/system event')
        expected = kind[0]
    if event['message_type'] != expected:
        raise PocketError('MIDI event bytes disagree with declared type')

def validate_material(record):
    if not isinstance(record, dict) or record.get('schema') != SCHEMA or REQUIRED != record.keys():
        raise PocketError('Expected a complete pocket.material/v1 record')
    canonical_bytes(record)  # Reject nonfinite numbers and non-JSON values everywhere, including opaque payloads.
    if record['revision_sha256'] != material_digest(record):
        raise PocketError('Material revision hash does not match its content')
    for key in ('sources', 'tracks', 'clips', 'notes', 'events', 'curves'):
        if not isinstance(record[key], list):
            raise PocketError(f'Material {key} must be an array')
    if any(not isinstance(source, dict) or not isinstance(source.get('kind'), str) or not source['kind']
           for source in record['sources']):
        raise PocketError('Material sources require typed source objects')
    if len(record['notes']) > MAX_NOTES:
        raise PocketError('Material exceeds the 100000-note bound')
    for key in ('material_id', 'revision_sha256'):
        if not isinstance(record[key], str) or not record[key]:
            raise PocketError(f'Material {key} is required')
    for field, schema in (('tempo_map_ref', 'pocket.tempo-map/v1'), ('meter_map_ref', 'pocket.meter-map/v1')):
        handle = record[field]
        if handle is not None and (not isinstance(handle, dict) or
                set(handle) != {'schema', 'artifact_uri', 'sha256', 'artifact_schema'} or
                handle.get('schema') != 'pocket.artifact-handle/v1' or handle.get('artifact_schema') != schema or
                not isinstance(handle.get('artifact_uri'), str) or
                not isinstance(handle.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', handle['sha256'])):
            raise PocketError(f'Material {field} requires a versioned map artifact handle or null')
    for key in ('coverage', 'provenance'):
        if not isinstance(record[key], dict):
            raise PocketError(f'Material {key} must be an object')
    indexes = {}
    for kind in ('tracks', 'clips', 'notes', 'events', 'curves'):
        rows = record[kind]
        if any(not isinstance(x, dict) or not isinstance(x.get('id'), str) or not x['id'] for x in rows):
            raise PocketError(f'Every {kind} item requires an ID')
        indexes[kind] = {x['id']: x for x in rows}
        if len(indexes[kind]) != len(rows):
            raise PocketError(f'Duplicate {kind} IDs')
    owned = {kind: [] for kind in ('notes', 'events', 'curves')}
    for clip in record['clips']:
        if clip.get('track_id') not in indexes['tracks']:
            raise PocketError('Clip track binding is unresolved')
        if clip.get('loop') is not False:
            raise PocketError('Material v1 editing profile requires explicit unlooped clip occurrences')
        if rational(clip.get('length_qn'), 'clip length') < 0:
            raise PocketError('Clip length cannot be negative')
        origin = clip.get('origin', {})
        if not isinstance(origin, dict):
            raise PocketError('Clip origin must be a coordinate object')
        if origin.get('space') not in ('arrangement_qn', 'phrase_qn'):
            raise PocketError('Clip origin must identify arrangement_qn or phrase_qn')
        rational(origin, 'clip origin')
        for kind, field in (('notes', 'note_ids'), ('events', 'event_ids'), ('curves', 'curve_ids')):
            ids = clip.get(field)
            if not isinstance(ids, list) or len(set(ids)) != len(ids) or any(x not in indexes[kind] for x in ids):
                raise PocketError(f'Clip {field} has duplicate or unresolved IDs')
            owned[kind].extend(ids)
    for kind, owned_ids in owned.items():
        if sorted(owned_ids) != sorted(indexes[kind]):
            raise PocketError(f'Every {kind} item must belong to exactly one clip')
    for note in record['notes']:
        fields = {'id', 'voice_id', 'role_ref', 'onset', 'duration_qn', 'pitch', 'velocity',
                  'release_velocity', 'channel', 'mute', 'expression_refs', 'source_binding', 'derived_from'}
        if fields != note.keys():
            raise PocketError('Note has missing or unsupported fields; retain rich native attributes in source_binding')
        for field in ('onset', 'pitch', 'velocity', 'release_velocity'):
            if not isinstance(note[field], dict):
                raise PocketError(f'Note {field} must be an object')
        if not isinstance(note['voice_id'], str) or not note['voice_id']:
            raise PocketError('Note voice_id must be a nonempty string')
        if note['role_ref'] is not None and not isinstance(note['role_ref'], str):
            raise PocketError('Note role_ref must be a string or null')
        if note['source_binding'] is not None and not isinstance(note['source_binding'], dict):
            raise PocketError('Note source_binding must be an object or null')
        if not isinstance(note['derived_from'], list) or any(not isinstance(x, str) for x in note['derived_from']):
            raise PocketError('Note derived_from must contain note IDs')
        if note['onset'].get('space') != 'clip_qn':
            raise PocketError('Stored note onset must use clip_qn')
        rational(note['onset'], 'note onset')
        if rational(note['duration_qn'], 'note duration') <= 0:
            raise PocketError('Note duration must be positive')
        integer(note['pitch'].get('midi_note'), 'MIDI pitch', 0, 127)
        if isinstance(note['pitch'].get('cents_offset'), bool) or not isinstance(note['pitch'].get('cents_offset'), (int, float)):
            raise PocketError('Pitch cents offset must be numeric')
        for field, low in (('velocity', 1), ('release_velocity', 0)):
            velocity = note[field]
            if not isinstance(velocity, dict) or velocity.get('domain') != 'midi1_7bit':
                raise PocketError('Velocity must declare midi1_7bit domain')
            integer(velocity.get('value'), field, low, 127)
        integer(note['channel'], 'MIDI channel', 1, 16)
        if (not isinstance(note['mute'], bool) or not isinstance(note['expression_refs'], list)
                or any(not isinstance(x, str) for x in note['expression_refs'])):
            raise PocketError('Invalid mute or expression references')
        if any(x not in indexes['curves'] for x in note['expression_refs']):
            raise PocketError('Unresolved note expression reference')
    for event in record['events']:
        if not isinstance(event.get('time'), dict) or event['time'].get('space') != 'clip_qn':
            raise PocketError('Event time must use clip_qn')
        rational(event['time'], 'event time')
        integer(event.get('order'), 'event order', 0)
        if not isinstance(event.get('bytes'), list) or not event['bytes']:
            raise PocketError('Supported material events require exact MIDI message bytes')
        for byte in event['bytes']:
            integer(byte, 'MIDI event byte', 0, 255)
        if not isinstance(event.get('is_meta'), bool) or not isinstance(event.get('message_type'), str):
            raise PocketError('MIDI event requires type and meta classification')
        _validate_wire_event(event)
    for curve in record['curves']:
        from .curves import _validate as validate_curve
        if curve.get('id') != curve.get('curve_id'):
            raise PocketError('Material curve id must equal canonical curve_id')
        validate_curve({key: value for key, value in curve.items() if key != 'id'})
    note_clips = {key: clip['id'] for clip in record['clips'] for key in clip['note_ids']}
    curve_clips = {key: clip['id'] for clip in record['clips'] for key in clip['curve_ids']}
    for note in record['notes']:
        for curve_id in note['expression_refs']:
            curve = indexes['curves'][curve_id]
            if (curve['target']['scope'] != 'note' or curve['target'].get('note_id') != note['id']
                    or curve_clips[curve_id] != note_clips[note['id']]):
                raise PocketError('Per-note expression target and clip occurrence must match its owning note')
    for curve in record['curves']:
        if curve['target']['scope'] == 'note':
            note_id = curve['target'].get('note_id')
            if note_id not in indexes['notes'] or curve['id'] not in indexes['notes'][note_id]['expression_refs']:
                raise PocketError('Per-note expression must be referenced by its exact target note')
    for clip in record['clips']:
        orders = [indexes['events'][key]['order'] for key in clip['event_ids']]
        if len(set(orders)) != len(orders):
            raise PocketError('Event order must be unique within its clip')
    if record['coverage'].get('editing_allowed') is True and record['events']:
        wire_notes = {e['id'] for e in record['events'] if e['message_type'] in ('note_on', 'note_off')}
        bindings = [key for n in record['notes'] for key in ((n['source_binding'] or {}).get('on_event_id'),
                    (n['source_binding'] or {}).get('off_event_id')) if key is not None]
        source_only = record['coverage'].get('source_only_note_event_ids', [])
        if not isinstance(source_only, list) or len(set(source_only)) != len(source_only) or set(source_only) & set(bindings):
            raise PocketError('Invalid source-only MIDI event disposition')
        if len(set(bindings)) != len(bindings) or set(bindings) | set(source_only) != wire_notes:
            raise PocketError('Editable raw MIDI notes require exact unique event lifecycle bindings')
    return copy.deepcopy(record)


def load_material(material: ArtifactHandle | MaterialRecord, store_root: str):
    if not isinstance(material, dict):
        raise PocketError('Material must be a record or artifact handle')
    record = read_record(material, store_root, SCHEMA) if material.get('schema') == 'pocket.artifact-handle/v1' else material
    # Full handles include the declared schema. Shared bounded traversal prevents
    # an earlier valid alias from concealing a later forged one or a deep graph.
    _verify_handles(record, store_root)
    return validate_material(record)


def new_material(seed, *, tracks, clips, notes, events=None, sources=None, coverage=None, provenance=None):
    return finalize_material({'schema': SCHEMA, 'material_id': identifier('material', seed),
        'revision_sha256': '', 'parent_revision': None, 'sources': sources or [], 'tracks': tracks,
        'clips': clips, 'notes': notes, 'events': events or [], 'curves': [], 'tempo_map_ref': None,
        'meter_map_ref': None, 'coverage': coverage or {'editing_allowed': True, 'issues': []},
        'provenance': provenance or {'provider': 'pocket.material/v1'}})


def make_note(note_id, onset, duration, pitch, velocity, *, channel=1, role=None, voice='voice:1',
              release=64, source=None):
    return {'id': note_id, 'voice_id': voice, 'role_ref': role,
            'onset': {'space': 'clip_qn', **qn(onset)}, 'duration_qn': qn(duration),
            'pitch': {'midi_note': pitch, 'cents_offset': 0, 'tuning_ref': 'tuning:12tet-a440'},
            'velocity': {'value': velocity, 'domain': 'midi1_7bit'},
            'release_velocity': {'value': release, 'domain': 'midi1_7bit'},
            'channel': channel, 'mute': False, 'expression_refs': [],
            'source_binding': source, 'derived_from': []}


def resolve_selection(record, selection=None, *, require_hash=False):
    selection = selection or {}
    if not isinstance(selection, dict):
        raise PocketError('Selection must be an object')
    for field in ('note_ids', 'voice_ids', 'clip_ids'):
        if field in selection and (not isinstance(selection[field], list) or any(not isinstance(x, str) for x in selection[field])):
            raise PocketError('Selection IDs must be arrays of strings')
    allowed = {'note_ids', 'voice_ids', 'clip_ids', 'span_qn', 'time_space', 'material_revision', 'selection_sha256'}
    if set(selection) - allowed:
        raise PocketError('Unknown material selection fields')
    if selection.get('time_space', 'clip_qn') != 'clip_qn':
        raise PocketError('Selection supports explicit clip_qn only')
    clip_notes = {n for c in record['clips'] if not selection.get('clip_ids') or c['id'] in selection['clip_ids']
                  for n in c['note_ids']}
    if selection.get('clip_ids') and set(selection['clip_ids']) - {x['id'] for x in record['clips']}:
        raise PocketError('Selection contains unknown clip IDs')
    if selection.get('voice_ids') and set(selection['voice_ids']) - {x['voice_id'] for x in record['notes']}:
        raise PocketError('Selection contains unknown voice IDs')
    requested = selection.get('note_ids')
    if requested is not None and (len(set(requested)) != len(requested) or set(requested) - {x['id'] for x in record['notes']}):
        raise PocketError('Selection contains duplicate or unknown note IDs')
    span = selection.get('span_qn')
    if span is not None and (not isinstance(span, list) or len(span) != 2 or rational(span[0]) >= rational(span[1])):
        raise PocketError('Selection span must be an increasing half-open pair')
    if span and not selection.get('clip_ids') and len(record['clips']) > 1:
        raise PocketError('Time selection across multiple clip-local spaces requires clip_ids')
    selected = [note['id'] for note in record['notes'] if note['id'] in clip_notes
                and (requested is None or note['id'] in requested)
                and (not selection.get('voice_ids') or note['voice_id'] in selection['voice_ids'])
                and (span is None or rational(span[0]) <= rational(note['onset']) < rational(span[1]))]
    result = {'material_revision': record['revision_sha256'], 'note_ids': selected}
    result['selection_sha256'] = digest(result)
    if require_hash and (selection.get('material_revision') != result['material_revision'] or
                         selection.get('selection_sha256') != result['selection_sha256']):
        raise PocketError('Stale or unresolved material selection; query exact IDs again')
    return result


def semantic_diff(before, after):
    left, right = {n['id']: n for n in before['notes']}, {n['id']: n for n in after['notes']}
    changes = [{'id': key, 'fields': {field: {'before': left[key].get(field), 'after': right[key].get(field)}
                 for field in sorted(set(left[key]) | set(right[key])) if left[key].get(field) != right[key].get(field)}}
               for key in left.keys() & right.keys() if left[key] != right[key]]
    return {'inserted': sorted(right.keys() - left.keys()), 'deleted': sorted(left.keys() - right.keys()),
            'changed': sorted(changes, key=lambda x: x['id']),
            'events_unchanged': before['events'] == after['events'], 'curves_unchanged': before['curves'] == after['curves']}


def material_import(source: MaterialSource, store_root: str, request_id: str) -> dict:
    """Import exact SMF bytes, a saved Live clip, or a validated external material record."""
    if not isinstance(source, dict):
        raise PocketError('source must be an explicit material source')
    expected_fields = {'smf': {'kind', 'path', 'expected_sha256'},
                       'material': {'kind', 'material'}, 'live_clip': {'kind', 'thread_handle', 'clip_id'}}
    if source.get('kind') not in expected_fields or set(source) != expected_fields[source['kind']]:
        raise PocketError('Source fields must exactly match its declared kind')
    if source.get('kind') == 'smf':
        if not isinstance(source.get('path'), str) or not source['path']:
            raise PocketError('SMF source path must be a nonempty string')
        path = Path(source.get('path', '')).expanduser().resolve()
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise PocketError(f'Cannot read MIDI source: {exc}') from exc
        if hashlib.sha256(payload).hexdigest() != source.get('expected_sha256'):
            raise PocketError('Stale or missing expected MIDI source hash')
    elif source.get('kind') == 'material':
        validate_material(source.get('material'))
    elif source.get('kind') == 'live_clip':
        from .thread_queries import _load
        _load(source.get('thread_handle'))  # Fresh source binding also on idempotent replay.
    else:
        raise PocketError('Source kind must be smf, live_clip or material')

    def work():
        if source['kind'] == 'smf':
            from .midi_io import import_smf
            record = import_smf(payload, store_root)
        elif source['kind'] == 'live_clip':
            from .midi_io import import_live_clip
            record = import_live_clip(source, store_root)
        else:
            record = validate_material(source['material'])
        handle = put_record(record, store_root)
        return receipt(request_id=request_id, artifacts={'material': handle},
                       change_summary={'notes': len(record['notes']), 'events': len(record['events'])},
                       coverage=record['coverage'], material=handle,
                       uncertainty=['No listening or native save/reopen is established by import.'])
    result = run_request(store_root, request_id, 'material_import', source, work)
    load_material(result['material'], store_root)
    return result


def material_query(material: ArtifactHandle | MaterialRecord, store_root: str, query: str = 'summary',
                   selection: MaterialSelection | None = None, limit: int = 64, cursor: str | None = None,
                   max_bytes: int = 16000, comparison: ArtifactHandle | MaterialRecord | None = None) -> dict:
    """Read bounded notes, voices, summary or semantic differences and resolve exact note selections."""
    record = load_material(material, store_root)
    integer(limit, 'limit', 1, 512)
    integer(max_bytes, 'max_bytes', 1024, 65536)
    resolved = resolve_selection(record, selection)
    ids = set(resolved['note_ids'])
    selected = [n for n in record['notes'] if n['id'] in ids]
    if query == 'summary':
        rows = [{'material_id': record['material_id'], 'revision_sha256': record['revision_sha256'],
                 'notes': len(selected), 'events': len(record['events']), 'curves': len(record['curves']),
                 'tracks': len(record['tracks']), 'clips': len(record['clips']), 'coverage': record['coverage']}]
    elif query == 'events':
        rows = [{'kind': 'note', **n} for n in selected]
        if selection is None:
            rows += [{'kind': 'event', **e} for e in record['events']]
    elif query == 'voices':
        voices = sorted({n['voice_id'] for n in selected})
        rows = [{'voice_id': v, 'note_count': sum(n['voice_id'] == v for n in selected)} for v in voices]
    elif query == 'diff' and comparison is not None:
        diff = semantic_diff(record, load_material(comparison, store_root))
        rows = ([{'kind': 'inserted', 'id': n} for n in diff['inserted']] +
                [{'kind': 'deleted', 'id': n} for n in diff['deleted']] +
                [{'kind': 'changed', **n} for n in diff['changed']])
    else:
        raise PocketError('query must be summary, events, voices or diff with comparison')
    identity = digest({'revision': record['revision_sha256'], 'query': query, 'selection': resolved,
                       'comparison': comparison})
    offset = 0
    if cursor is not None:
        try:
            token, raw = cursor.split(':')
            offset = int(raw)
        except (ValueError, AttributeError) as exc:
            raise PocketError('Invalid material query cursor') from exc
        if token != identity or offset < 0 or offset > len(rows):
            raise PocketError('Stale material query cursor')
    result = {'revision_sha256': record['revision_sha256'],
              'query': query, 'records': [], 'total': len(rows), 'omitted': len(rows) - offset,
              'next_cursor': None, 'selection': resolved}

    def envelope(value):
        following = offset + len(value['records'])
        return receipt(coverage={'basis': 'symbolic', 'read_only': True},
                       result_schema='pocket.material-query/v1',
                       **{**value, 'omitted': len(rows) - following,
                          'next_cursor': f'{identity}:{following}' if following < len(rows) else None})

    # Large exact selections must be narrowed before edits; never truncate IDs.
    if len(canonical_bytes(envelope(result))) > max_bytes - 256:
        result['selection'] = None
        result['selection_omitted'] = len(resolved['note_ids'])
    for row in rows[offset:offset + limit]:
        proposal = {**result, 'records': result['records'] + [row]}
        if len(canonical_bytes(envelope(proposal))) > max_bytes:
            break
        result['records'].append(row)
    if offset < len(rows) and not result['records']:
        raise PocketError('One record exceeds byte budget; use a larger budget or narrower query')
    bounded = envelope(result)
    if len(canonical_bytes(bounded)) > max_bytes:
        raise PocketError('Query metadata exceeds byte budget; narrow the selection')
    return bounded
