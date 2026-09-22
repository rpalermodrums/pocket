"""Independent native MIDI observation and a separate guarded ordinary-note writer.

The read device remains read-only. The writer has an exact package/host profile,
two guards, candidate quarantine and one host lease. Observations are sequential
reads, not proof of unsaved expression, current saved-state equivalence or listening.
"""
from __future__ import annotations

import gzip
import hashlib
import http.client
import io
import json
import math
import os
import re
import shutil
import struct
import tempfile
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .artifact_store import (
    ArtifactHandle,
    _verify_handles,
    canonical_bytes,
    digest,
    put_bytes,
    put_record,
    read_bytes,
    read_record,
    receipt,
    run_request,
)
from .errors import PocketError
from .native_midi_types import (
    NativeMidiWriteEdit,
    NativeReadTarget,
    NativeRequestReference,
    NativeSavedBinding,
    NativeSessionAcknowledgment,
)

PROTOCOL = 'pocket.native-midi-wire/v1'
OBSERVATION = 'pocket.native-midi-observation/v1'
MAX_BYTES = 2 * 1024 * 1024
SOURCE_NAMES = ('Native MIDI.maxpat', 'bridge.js', 'core.js', 'device.js')
NOTE_FIELDS = {'note_id', 'pitch', 'start_time', 'duration', 'velocity', 'mute',
               'probability', 'velocity_deviation', 'release_velocity'}
CLIP_FIELDS = {'index', 'runtime_id', 'name', 'is_midi_clip', 'start_time', 'end_time',
               'looping', 'loop_start', 'loop_end', 'start_marker', 'end_marker'}
GUARDS = {
    'song': {'is_playing', 'record_mode', 'arrangement_overdub', 'session_record',
             'session_automation_record', 'tempo', 'signature_numerator',
             'signature_denominator', 'file_path'},
    'track': {'is_frozen', 'is_grouped', 'is_foldable', 'mute', 'solo', 'arm'},
    'clip': {'has_envelopes', 'has_groove', 'muted', 'is_arrangement_clip', 'is_session_clip',
             'is_take_lane_clip', 'is_midi_clip', 'is_overdubbing', 'is_playing', 'is_recording',
             'is_triggered', 'looping', 'loop_start', 'loop_end', 'start_marker', 'end_marker',
             'start_time', 'end_time'},
}
NUMERIC_GUARDS = {'tempo', 'signature_numerator', 'signature_denominator', 'loop_start',
                  'loop_end', 'start_marker', 'end_marker', 'start_time', 'end_time'}
COVERAGE = {'consistency': 'sequential_not_atomic', 'note_expression': 'unobserved',
            'saved_equivalence': 'not_claimed'}


def _keys(value, fields, label='record'):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise PocketError(f'Invalid {label} fields')
    return value


def _number(value, low, high, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not low <= value <= high or not math.isfinite(value) or
            (integer and type(value) is not int)):
        raise PocketError('Expected bounded finite number')
    return value


def _text(value, maximum=1024, nonempty=True):
    if not isinstance(value, str) or len(value) > maximum or (nonempty and not value):
        raise PocketError('Expected bounded text')
    return value


def _hex(value, length=64):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{' + str(length) + '}', value):
        raise PocketError('Invalid native identity')
    return value


def _timestamp(value):
    _text(value, 64)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise PocketError('Invalid observation time') from error
    if parsed.tzinfo is None:
        raise PocketError('Observation time requires timezone')
    return parsed


def _json(payload):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PocketError('Duplicate JSON fields are unsupported')
            result[key] = value
        return result
    try:
        result = json.loads(payload, object_pairs_hook=unique)
        canonical_bytes(result)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise PocketError('Invalid finite JSON transport') from error
    return result


def _file(path: Path, maximum):
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise PocketError('Native evidence paths must not use symlinks')
    try:
        with path.open('rb') as stream:
            result = stream.read(maximum + 1)
        if len(result) > maximum:
            raise PocketError('Native evidence exceeds size bound')
        return result
    except OSError as error:
        raise PocketError(f'Cannot read native evidence: {error}') from error


def _target(value):
    _keys(value, {'location', 'track_index', 'clip_index'}, 'native target')
    if value['location'] != 'arrangement':
        raise PocketError('Only explicit arrangement targets are implemented')
    _number(value['track_index'], 0, 255, True)
    _number(value['clip_index'], 0, 255, True)
    return value


def _notes(value):
    if not isinstance(value, list) or len(value) > 2000:
        raise PocketError('Native note read exceeds2000-note bound')
    ids = set()
    for note in value:
        _keys(note, NOTE_FIELDS, 'native note')
        ident = _number(note['note_id'], 1, 2**31 - 1, True)
        if ident in ids:
            raise PocketError('Duplicate native note ID')
        ids.add(ident)
        _number(note['pitch'], 0, 127, True)
        _number(note['start_time'], -1e9, 1e9)
        _number(note['duration'], math.ulp(0.0), 1e9)
        for key in ('velocity', 'release_velocity'):
            _number(note[key], 0, 127)
        _number(note['mute'], 0, 1, True)
        _number(note['probability'], 0, 1)
        _number(note['velocity_deviation'], -127, 127)
    return sorted(value, key=lambda note: note['note_id'])


def _guards(value):
    _keys(value, GUARDS, 'guards')
    for scope, properties in GUARDS.items():
        _keys(value[scope], properties, 'guard properties')
        for name, guard in value[scope].items():
            if not isinstance(guard, dict):
                raise PocketError('Invalid native guard')
            if guard.get('status') == 'available':
                _keys(guard, {'status', 'value', 'raw'})
                observed = guard['value']
                raw = guard['raw']
                if canonical_bytes(raw[0] if isinstance(raw, list) and len(raw) == 1 else raw) != canonical_bytes(observed):
                    raise PocketError('Guard raw/value mismatch')
                if name == 'file_path':
                    _text(observed, 4096, False)
                elif name in NUMERIC_GUARDS:
                    _number(observed, -1e9, 1e9)
                else:
                    _number(observed, 0, 1, True)
            elif guard.get('status') == 'unsupported':
                if set(guard) == {'status', 'error'}:
                    _text(guard['error'], 1024)
                else:
                    _keys(guard, {'status', 'raw_type', 'raw_text'})
                    _text(guard['raw_type'], 32)
                    _text(guard['raw_text'], 4096, False)
            else:
                raise PocketError('Unknown native guard status')


def _core(value, requested):
    _keys(value, {'host', 'topology', 'target', 'notes', 'guards', 'raw_notes_json'}, 'native read')
    _keys(value['host'], {'runtime_version', 'file_path'}, 'host')
    _text(value['host']['runtime_version'], 100)
    if value['host']['file_path'] is not None:
        _text(value['host']['file_path'], 4096, False)
    topology = value['topology']
    _keys(topology, {'song', 'tracks', 'return_tracks'}, 'topology')
    _keys(topology['song'], {'runtime_id'})
    ids = set()

    def identity(ident):
        _number(ident, 1, 2**31 - 1, True)
        if ident in ids:
            raise PocketError('Ambiguous runtime identities')
        ids.add(ident)

    identity(topology['song']['runtime_id'])
    for collection, maximum in (('tracks', 256), ('return_tracks', 64)):
        tracks = topology[collection]
        if not isinstance(tracks, list) or len(tracks) > maximum:
            raise PocketError('Topology track bound exceeded')
        for index, track in enumerate(tracks):
            _keys(track, {'index', 'runtime_id', 'name', 'devices', 'arrangement_clips'})
            _number(track['index'], index, index, True)
            identity(track['runtime_id'])
            _text(track['name'], 1024, False)
            if not isinstance(track['devices'], list) or len(track['devices']) > 64:
                raise PocketError('Topology device bound exceeded')
            for device_index, device in enumerate(track['devices']):
                _keys(device, {'index', 'runtime_id', 'name', 'class_name'})
                _number(device['index'], device_index, device_index, True)
                identity(device['runtime_id'])
                _text(device['name'], 1024, False)
                _text(device['class_name'], 256)
            clips = track['arrangement_clips']
            if not isinstance(clips, list) or len(clips) > (256 if collection == 'tracks' else 0):
                raise PocketError('Topology clip bound exceeded')
            for clip_index, clip in enumerate(clips):
                _keys(clip, CLIP_FIELDS)
                _number(clip['index'], clip_index, clip_index, True)
                identity(clip['runtime_id'])
                _text(clip['name'], 1024, False)
                for key in ('is_midi_clip', 'looping'):
                    _number(clip[key], 0, 1, True)
                for key in CLIP_FIELDS - {'index', 'runtime_id', 'name', 'is_midi_clip', 'looping'}:
                    _number(clip[key], -1e9, 1e9)
    if len(ids) > 4096:
        raise PocketError('Topology total identity bound exceeded')
    target = value['target']
    _keys(target, {'location', 'track_index', 'clip_index', 'track_runtime_id', 'clip_runtime_id',
                   'path', 'device_track_runtime_id'})
    _target({key: target[key] for key in requested})
    for key in ('track_runtime_id', 'clip_runtime_id'):
        _number(target[key], 1, 2**31 - 1, True)
    if {key: target[key] for key in requested} != requested:
        raise PocketError('Native target mismatch')
    try:
        track = topology['tracks'][requested['track_index']]
        clip = track['arrangement_clips'][requested['clip_index']]
    except IndexError as error:
        raise PocketError('Target absent from topology') from error
    if (track['runtime_id'] != target['track_runtime_id'] or clip['runtime_id'] != target['clip_runtime_id']
            or clip['is_midi_clip'] != 1 or target['path'] !=
            f"live_set tracks {requested['track_index']} arrangement_clips {requested['clip_index']}"):
        raise PocketError('Native target ancestry mismatch')
    _number(target['device_track_runtime_id'], 1, 2**31 - 1, True)
    if target['device_track_runtime_id'] not in {track['runtime_id'] for key in ('tracks', 'return_tracks') for track in topology[key]}:
        raise PocketError('Adapter device owner absent from topology')
    if _notes(value['notes']) != value['notes']:
        raise PocketError('Native notes must be ordered by ID')
    raw = _json(_text(value['raw_notes_json'], MAX_BYTES))
    _keys(raw, {'notes'})
    if _notes(raw['notes']) != value['notes']:
        raise PocketError('Raw note evidence mismatch')
    _guards(value['guards'])
    path_guard = value['guards']['song']['file_path']
    if value['host']['file_path'] != (path_guard['value'] if path_guard['status'] == 'available' else None):
        raise PocketError('Native host path guard mismatch')
    for key in ('start_time', 'end_time', 'loop_start', 'loop_end', 'start_marker', 'end_marker', 'looping', 'is_midi_clip'):
        guard = value['guards']['clip'][key]
        if guard['status'] == 'available' and guard['value'] != clip[key]:
            raise PocketError('Native topology changed during guard read')
    return value


def _journal(value):
    if not isinstance(value, dict):
        raise PocketError('Expected native bridge journal object')
    fields = {'schema', 'protocol', 'session_nonce', 'package_sha256', 'request_key', 'request_json',
              'request_sha256', 'operation', 'state', 'dispatch_count', 'received_at',
              'result_json', 'result_sha256'}
    _keys(value, fields | ({'completed_at'} if value.get('state') == 'complete' else set()), 'bridge journal')
    if value['schema'] != 'pocket.native-midi-bridge-journal/v1' or value['protocol'] != PROTOCOL or value['operation'] != 'read' or value['state'] not in {'dispatched', 'complete'}:
        raise PocketError('Unsupported native bridge journal')
    _hex(value['session_nonce'], 32)
    for key in ('request_key', 'package_sha256', 'request_sha256'):
        _hex(value[key])
    _number(value['dispatch_count'], 1, 1, True)
    _timestamp(value['received_at'])
    raw = _text(value['request_json'], 8192)
    if hashlib.sha256(raw.encode()).hexdigest() != value['request_sha256']:
        raise PocketError('Native request hash mismatch')
    request = _json(raw)
    _keys(request, {'schema', 'request_key', 'session_nonce', 'target', 'timeout_ms'})
    if request['schema'] != 'pocket.native-midi-read-request/v1' or any(request[key] != value[key] for key in ('request_key', 'session_nonce')):
        raise PocketError('Native request identity mismatch')
    _target(request['target'])
    _number(request['timeout_ms'], 100, 60000, True)
    if value['state'] != 'complete':
        if value['result_json'] is not None or value['result_sha256'] is not None:
            raise PocketError('Incomplete native journal contains a result')
        return request, None
    _timestamp(value['completed_at'])
    raw_result = _text(value['result_json'], MAX_BYTES)
    if hashlib.sha256(raw_result.encode()).hexdigest() != value['result_sha256']:
        raise PocketError('Native result hash mismatch')
    result = _json(raw_result)
    base = {'schema', 'request_key', 'session_nonce', 'package_sha256', 'protocol',
            'read_started_at', 'read_ended_at', 'status', 'observation'}
    _keys(result, base | ({'error'} if result.get('status') == 'unsupported' else set()))
    if result['schema'] != OBSERVATION or any(result[key] != value[key] for key in ('request_key', 'session_nonce', 'package_sha256', 'protocol')):
        raise PocketError('Native result identity mismatch')
    if _timestamp(result['read_ended_at']) < _timestamp(result['read_started_at']):
        raise PocketError('Native read time order mismatch')
    if result['status'] == 'ok':
        _core(result['observation'], request['target'])
    elif result['status'] == 'unsupported' and result['observation'] is None:
        _text(result['error'], 2000)
    else:
        raise PocketError('Unsupported native result state')
    return request, result


def _xml(data):
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            expanded = stream.read(64 * 1024 * 1024 + 1)
        if len(expanded) > 64 * 1024 * 1024 or b'<!DOCTYPE' in expanded or b'<!ENTITY' in expanded:
            raise PocketError('Unsupported or oversized saved XML')
        root = ET.fromstring(expanded)
    except (OSError, EOFError, ET.ParseError) as error:
        raise PocketError('Invalid saved Live Set') from error
    if root.tag != 'Ableton' or root.find('LiveSet/Tracks') is None:
        raise PocketError('Unsupported saved Live Set structure')
    return root


def _saved_check(binding, data, observation):
    _keys(binding, {'saved_als', 'path', 'sha256', 'build', 'track_id', 'clip_id'}, 'saved binding')
    if hashlib.sha256(data).hexdigest() != binding['sha256'] or binding['saved_als']['sha256'] != binding['sha256']:
        raise PocketError('Saved binding hash mismatch')
    root = _xml(data)
    if dict(root.attrib) != binding['build']:
        raise PocketError('Saved build binding mismatch')
    saved_path = Path(_text(binding['path'], 4096))
    if not saved_path.is_absolute() or str(saved_path) != os.path.normpath(str(saved_path)) or observation['host']['file_path'] != str(saved_path):
        raise PocketError('Native Song.file_path differs from saved binding')
    for key in ('track_id', 'clip_id'):
        if not re.fullmatch(r'0|[1-9][0-9]*', _text(binding[key], 32)):
            raise PocketError('Invalid saved object ID')
    tracks = list(root.find('LiveSet/Tracks'))
    ordinary = [track for track in tracks if track.tag in {'AudioTrack', 'MidiTrack', 'GroupTrack'}]
    returns = [track for track in tracks if track.tag == 'ReturnTrack']
    if len(ordinary) + len(returns) != len(tracks):
        raise PocketError('Unknown saved track type')
    if len({track.get('Id') for track in tracks}) != len(tracks) or any(track.get('Id') is None for track in tracks):
        raise PocketError('Ambiguous saved track IDs')
    for saved, native in ((ordinary, observation['topology']['tracks']), (returns, observation['topology']['return_tracks'])):
        if len(saved) != len(native):
            raise PocketError('Saved/runtime track topology mismatch')
        for xml_track, runtime in zip(saved, native, strict=True):
            name = xml_track.find('Name/EffectiveName')
            if name is None or name.get('Value') != runtime['name']:
                raise PocketError('Saved/runtime ordered track name mismatch')
            devices = xml_track.findall('DeviceChain/DeviceChain/Devices/*')
            if len(devices) != len(runtime['devices']) or any(device.tag != actual['class_name'] for device, actual in zip(devices, runtime['devices'], strict=True)):
                raise PocketError('Saved/runtime ordered device class mismatch')
    target = observation['target']
    try:
        track = ordinary[target['track_index']]
    except IndexError as error:
        raise PocketError('Saved target absent') from error
    if track.tag != 'MidiTrack' or track.get('Id') != binding['track_id']:
        raise PocketError('Saved primary target track mismatch')
    clips = track.findall('DeviceChain/MainSequencer/ClipTimeable/ArrangerAutomation/Events/MidiClip')
    if len({clip.get('Id') for clip in clips}) != len(clips) or any(clip.get('Id') is None for clip in clips):
        raise PocketError('Ambiguous saved primary clip IDs')
    runtime_clips = observation['topology']['tracks'][target['track_index']]['arrangement_clips']
    if len(clips) != len(runtime_clips):
        raise PocketError('Saved primary clip topology mismatch')
    for clip, runtime in zip(clips, runtime_clips, strict=True):
        for key, xml_path in (('name', 'Name'), ('start_time', 'CurrentStart'), ('end_time', 'CurrentEnd')):
            node = clip.find(xml_path)
            try:
                value = node.get('Value') if key == 'name' else float(node.get('Value'))
            except (AttributeError, TypeError, ValueError) as error:
                raise PocketError('Malformed saved clip') from error
            if value != runtime[key]:
                raise PocketError('Saved/runtime primary clip mismatch')
    if target['clip_index'] >= len(clips) or clips[target['clip_index']].get('Id') != binding['clip_id']:
        raise PocketError('Saved primary clip identity mismatch')
    return binding


def load_native_midi_observation(handle: ArtifactHandle, store_root: str) -> dict:
    """Validate retained evidence. Does not contact Live or certify current state."""
    record = read_record(handle, store_root, OBSERVATION)
    _keys(record, {'schema', 'request_key', 'session_nonce', 'package_sha256', 'protocol',
                   'read_started_at', 'read_ended_at', 'host', 'topology', 'target', 'notes',
                   'guards', 'raw_notes', 'raw_guards', 'bridge_journal', 'saved_binding', 'coverage'})
    journal = read_record(record['bridge_journal'], store_root, 'pocket.native-midi-bridge-journal/v1')
    _, result = _journal(journal)
    if result is None or result['status'] != 'ok':
        raise PocketError('Observation requires complete successful native read')
    expected = {key: result[key] for key in ('schema', 'request_key', 'session_nonce', 'package_sha256',
                                           'protocol', 'read_started_at', 'read_ended_at')}
    expected.update({key: result['observation'][key] for key in ('host', 'topology', 'target', 'notes', 'guards')})
    if any(record[key] != value for key, value in expected.items()) or record['coverage'] != COVERAGE:
        raise PocketError('Observation disagrees with retained native journal')
    if record['raw_notes'].get('artifact_schema') != 'pocket.native-midi-raw-notes/v1':
        raise PocketError('Raw native notes artifact family mismatch')
    raw = read_bytes(record['raw_notes'], store_root)
    if raw.decode('utf8') != result['observation']['raw_notes_json']:
        raise PocketError('Raw native note artifact mismatch')
    guard_record = read_record(record['raw_guards'], store_root, 'pocket.native-midi-guards/v1')
    expected_guards = {'schema': 'pocket.native-midi-guards/v1', 'request_key': record['request_key'],
                       'session_nonce': record['session_nonce'], 'package_sha256': record['package_sha256'],
                       'guards': record['guards']}
    if guard_record != expected_guards:
        raise PocketError('Raw native guards artifact mismatch')
    if record['saved_binding'] is not None:
        if record['saved_binding']['saved_als'].get('artifact_schema') != 'pocket.live-set-bytes/v1':
            raise PocketError('Native saved binding artifact family mismatch')
        _saved_check(record['saved_binding'], read_bytes(record['saved_binding']['saved_als'], store_root), record)
    return record


def _build_native_device(output_dir, store_root, request_id, writer=False):
    destination = Path(_text(output_dir, 4096)).expanduser().absolute()
    inputs = {'output_dir': str(destination)}
    device_name = 'Native MIDI Writer' if writer else 'Native MIDI'
    names = ('Native MIDI Writer.maxpat', 'bridge.js', 'device.js', 'reader.js', 'writer.js') if writer else SOURCE_NAMES
    sources = Path(__file__).parent / 'devices' / ('native_midi_writer' if writer else 'native_midi')

    def work():
        if destination.exists():
            raise PocketError('Device output directory already exists')
        if any(path.is_symlink() for path in destination.parents):
            raise PocketError('Device destination must not use symlinks')
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix='.native-midi-', dir=destination.parent))
        try:
            source_hashes = {}
            for name in names:
                payload = _file(sources / name, MAX_BYTES)
                (stage / name).write_bytes(payload)
                source_hashes[name] = hashlib.sha256(payload).hexdigest()
            manifest = {'schema': 'pocket.native-midi-package/v1', 'protocol': PROTOCOL,
                        'sources': source_hashes, 'package_sha256': digest(source_hashes)}
            (stage / 'manifest.json').write_bytes(canonical_bytes(manifest))
            patch = (stage / (device_name + '.maxpat')).read_bytes() + b'\0'
            header = b'ampf' + struct.pack('<I', 4) + b'aaaa' + b'meta' + struct.pack('<II', 4, 0)
            (stage / (device_name + '.amxd')).write_bytes(header + b'ptch' + struct.pack('<I', len(patch)) + patch)
            files = {name: put_bytes((stage / name).read_bytes(), store_root,
                                     name.replace(' ', '-'), 'pocket.native-midi-device-file/v1')
                     for name in (*names, 'manifest.json', device_name + '.amxd')}
            artifact = put_record({'schema': 'pocket.native-midi-device/v1', 'protocol': PROTOCOL,
                                   'package_sha256': manifest['package_sha256'], 'files': files}, store_root)
            # mkdir wins exclusively; copy ordinary mutable package files, never hard-link.
            destination.mkdir()
            for name in files:
                shutil.copyfile(stage / name, destination / name)
            return receipt(request_id, artifacts={'device': artifact}, device_path=str(destination / (device_name + '.amxd')),
                           package_sha256=manifest['package_sha256'],
                           coverage={'loaded_in_live': False, 'native_verified': False, 'write_available': False},
                           next_actions=['Load the device on an audio-capable track; keep all adjacent files together'])
        finally:
            shutil.rmtree(stage)
    result = run_request(store_root, request_id, 'build_native_midi_writer' if writer else 'build_native_midi_device', inputs, work)
    package = read_record(result['artifacts']['device'], store_root, 'pocket.native-midi-device/v1')
    for name, handle in package['files'].items():
        if _file(destination / name, MAX_BYTES) != read_bytes(handle, store_root):
            raise PocketError('Published device package changed after build')
    return result


def build_native_midi_device(output_dir: str, store_root: str, request_id: str) -> dict:
    """Publish the independent read-only package; no installation/native dispatch."""
    return _build_native_device(output_dir, store_root, request_id)


def build_native_midi_writer(output_dir: str, store_root: str, request_id: str) -> dict:
    """Build the separate supervised writer package; does not install or load it."""
    return _build_native_device(output_dir, store_root, request_id, writer=True)


def _bridge_folder(bridge_dir):
    if bridge_dir is not None:
        _text(bridge_dir, 4096)
    return Path(bridge_dir or os.environ.get('POCKET_NATIVE_MIDI_DIR', '~/.pocket/native-midi')).expanduser().absolute()


def _http(descriptor, route, timeout, payload=None):
    connection = http.client.HTTPConnection('127.0.0.1', descriptor['port'], timeout=timeout)
    try:
        connection.request('POST' if payload is not None else 'GET', route,
                           payload, {'Authorization': 'Bearer ' + descriptor['token'], 'Content-Type': 'application/json'})
        response = connection.getresponse()
        raw = response.read(MAX_BYTES * 2 + 1)
        if len(raw) > MAX_BYTES * 2:
            raise PocketError('Native response exceeds transport bound')
        return response.status, _json(raw)
    finally:
        connection.close()


def _connection(folder, expected_nonce, timeout):
    paths = sorted(folder.glob('connection-*.json')) if folder.is_dir() else []
    if len(paths) > 64:
        raise PocketError('Too many native adapter descriptors')
    active = []
    deadline = time.monotonic() + timeout
    for path in paths:
        descriptor = _json(_file(path, 4096))
        _keys(descriptor, {'schema', 'protocol', 'host', 'port', 'token', 'session_nonce', 'package_sha256'})
        if descriptor['schema'] != 'pocket.native-midi-connection/v1' or descriptor['protocol'] != PROTOCOL or descriptor['host'] != '127.0.0.1':
            raise PocketError('Invalid native loopback descriptor')
        _number(descriptor['port'], 1, 65535, True)
        _hex(descriptor['token'])
        _hex(descriptor['session_nonce'], 32)
        _hex(descriptor['package_sha256'])
        if path.name != 'connection-' + descriptor['session_nonce'] + '.json':
            raise PocketError('Descriptor filename identity mismatch')
        if expected_nonce is not None and expected_nonce != descriptor['session_nonce']:
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PocketError('Native discovery timeout')
        try:
            status, health = _http(descriptor, '/health', min(0.5, remaining))
        except ConnectionRefusedError:
            continue
        _keys(health, {'schema', 'protocol', 'session_nonce', 'package_sha256', 'ready', 'write_available'})
        if status != 200 or health['schema'] != 'pocket.native-midi-health/v1' or health['ready'] is not True or type(health['write_available']) is not bool or any(health[key] != descriptor[key] for key in ('protocol', 'session_nonce', 'package_sha256')):
            raise PocketError('Native adapter health identity mismatch')
        active.append(descriptor)
    if len(active) > 1:
        raise PocketError('Ambiguous native devices: select expected_session_nonce')
    return active[0] if active else None


def _page(record, handle, request_id, offset, limit, byte_budget):
    if offset > len(record['notes']):
        raise PocketError('Native note offset exceeds retained observation')
    page = record['notes'][offset:offset + limit]
    while True:
        next_offset = offset + len(page) if offset + len(page) < len(record['notes']) else None
        answer = receipt(request_id, artifacts={'observation': handle}, notes=page, total_notes=len(record['notes']),
                         offset=offset, next_offset=next_offset, session_nonce=record['session_nonce'],
                         native_request={'session_nonce': record['session_nonce'], 'request_key': record['request_key']},
                         next_page={'observation': handle, 'offset': next_offset, 'limit': limit, 'byte_budget': byte_budget} if next_offset is not None else None,
                         coverage={**COVERAGE, 'write_available': False, 'read_kind': 'retained_read'},
                         uncertainty=['Native note expression is unobserved; this is historical evidence after return'])
        if len(canonical_bytes(answer)) <= byte_budget:
            return answer
        if not page:
            raise PocketError('Native receipt cannot fit byte_budget')
        page = page[:-1]
        if not page:
            raise PocketError('No native note fits byte_budget')


def native_midi_read(store_root: str, request_id: str, target: NativeReadTarget | None = None,
                     saved_binding: NativeSavedBinding | None = None,
                     expected_session_nonce: str | None = None, bridge_dir: str | None = None,
                     timeout_seconds: float = 15, limit: int = 64, offset: int = 0,
                     byte_budget: int = 16384, observation: ArtifactHandle | None = None) -> dict:
    """Read one explicit arrangement clip; optional saved evidence, no candidate required.

    Reusing request_id returns that historical observation. Use a new request_id
    for a fresh observation; status never redispatches an interrupted read.
    Pass observation for stored pages with no Live/device dependency.
    """
    _number(timeout_seconds, 0.1, 60)
    _number(limit, 1, 256, True)
    _number(offset, 0, 2000, True)
    _number(byte_budget, 2048, 131072, True)
    if observation is not None:
        if (target is not None or saved_binding is not None or expected_session_nonce is not None or
                bridge_dir is not None or timeout_seconds != 15):
            raise PocketError('Stored observation mode cannot include live read inputs')
        inputs = {'observation': observation, 'offset': offset, 'limit': limit, 'byte_budget': byte_budget}
        def retained():
            record = load_native_midi_observation(observation, store_root)
            return _page(record, observation, request_id, offset, limit, byte_budget)
        result = run_request(store_root, request_id, 'native_midi_read', inputs, retained)
        load_native_midi_observation(observation, store_root)
        return result
    _target(target)
    if offset != 0:
        raise PocketError('Further pages require the retained observation handle')
    if expected_session_nonce is not None:
        _hex(expected_session_nonce, 32)
    folder = _bridge_folder(bridge_dir)
    binding_input = None
    if saved_binding is not None:
        _keys(saved_binding, {'saved_als', 'expected_sha256', 'track_id', 'clip_id'})
        _hex(saved_binding['expected_sha256'])
        binding_input = {**saved_binding, 'saved_als': str(Path(_text(saved_binding['saved_als'], 4096)).expanduser().absolute())}
    inputs = {'target': target, 'saved_binding': binding_input, 'expected_session_nonce': expected_session_nonce,
              'bridge_dir': str(folder), 'timeout_seconds': timeout_seconds, 'limit': limit,
              'offset': offset, 'byte_budget': byte_budget}
    request_key = digest({'request_id': request_id, 'inputs': inputs, 'store_root': str(Path(store_root).expanduser().resolve())})

    def work():
        before_bytes = None
        if binding_input is not None:
            before_bytes = _file(Path(binding_input['saved_als']), 16 * 1024 * 1024)
            if hashlib.sha256(before_bytes).hexdigest() != binding_input['expected_sha256']:
                raise PocketError('Saved binding is stale')
        try:
            descriptor = _connection(folder, expected_session_nonce, timeout_seconds)
            if descriptor is None:
                return receipt(request_id, status='unsupported', reason='native_device_not_loaded',
                               coverage={'write_available': False, 'native_dispatched': False})
            wire_request = {'schema': 'pocket.native-midi-read-request/v1', 'request_key': request_key,
                            'session_nonce': descriptor['session_nonce'], 'target': target,
                            'timeout_ms': int(timeout_seconds * 1000)}
            status, response = _http(descriptor, '/read', timeout_seconds + 1, canonical_bytes(wire_request))
        except (OSError, http.client.HTTPException) as error:
            return receipt(request_id, status='outcome_unknown', reason=str(error)[:1024],
                           native_request={'request_key': request_key, 'session_nonce': descriptor['session_nonce']} if 'descriptor' in locals() and descriptor else None,
                           next_actions=['Inspect native_midi_status; a new read requires a new request ID'])
        if status != 200:
            return receipt(request_id, status='outcome_unknown' if status >= 500 else 'conflict',
                           reason=f'Native bridge HTTP {status}', native_request={'request_key': request_key, 'session_nonce': descriptor['session_nonce']},
                           next_actions=['Inspect native_midi_status; this request will not redispatch'])
        _keys(response, {'schema', 'replay', 'journal'})
        if response['schema'] != 'pocket.native-midi-response/v1' or type(response['replay']) is not bool:
            raise PocketError('Invalid native bridge response')
        journal = response['journal']
        wire, result = _journal(journal)
        if wire != wire_request or journal['package_sha256'] != descriptor['package_sha256']:
            raise PocketError('Native response does not match requested input')
        journal_handle = put_record(journal, store_root)
        if result is None or result['status'] != 'ok':
            return receipt(request_id, status='unsupported', artifacts={'bridge_journal': journal_handle},
                           reason=result['error'] if result else 'native_read_incomplete',
                           coverage={'write_available': False, **COVERAGE})
        core = result['observation']
        binding = None
        if binding_input is not None:
            after_bytes = _file(Path(binding_input['saved_als']), 16 * 1024 * 1024)
            if after_bytes != before_bytes:
                raise PocketError('Saved Set changed during native read')
            saved_handle = put_bytes(before_bytes, store_root, 'saved.als', 'pocket.live-set-bytes/v1')
            binding = {'saved_als': saved_handle, 'path': binding_input['saved_als'], 'sha256': saved_handle['sha256'],
                       'build': dict(_xml(before_bytes).attrib), 'track_id': binding_input['track_id'], 'clip_id': binding_input['clip_id']}
            _saved_check(binding, before_bytes, core)
        guards = {'schema': 'pocket.native-midi-guards/v1', 'request_key': request_key,
                  'session_nonce': result['session_nonce'], 'package_sha256': result['package_sha256'], 'guards': core['guards']}
        record = {key: result[key] for key in ('schema', 'request_key', 'session_nonce', 'package_sha256', 'protocol', 'read_started_at', 'read_ended_at')}
        record.update({key: core[key] for key in ('host', 'topology', 'target', 'notes', 'guards')})
        record.update({'raw_notes': put_bytes(core['raw_notes_json'].encode(), store_root, 'notes.json', 'pocket.native-midi-raw-notes/v1'),
                       'raw_guards': put_record(guards, store_root), 'bridge_journal': journal_handle,
                       'saved_binding': binding, 'coverage': COVERAGE})
        handle = put_record(record, store_root)
        load_native_midi_observation(handle, store_root)
        return _page(record, handle, request_id, offset, limit, byte_budget)
    result = run_request(store_root, request_id, 'native_midi_read', inputs, work)
    if result.get('artifacts', {}).get('observation') is not None:
        load_native_midi_observation(result['artifacts']['observation'], store_root)
    return result


def native_midi_status(store_root: str, request_id: str, native_request: NativeRequestReference,
                       bridge_dir: str | None = None) -> dict:
    """Inspect a retained local native journal; no Live dependency or redispatch."""
    _keys(native_request, {'session_nonce', 'request_key'})
    _hex(native_request['session_nonce'], 32)
    _hex(native_request['request_key'])
    folder = _bridge_folder(bridge_dir)
    inputs = {'native_request': native_request, 'bridge_dir': str(folder)}

    def work():
        file = folder / 'journals' / native_request['session_nonce'] / (native_request['request_key'] + '.json')
        if not file.exists():
            return receipt(request_id, status='needs_input', journal_state='not_found',
                           coverage={'native_dispatched': False, 'redispatched': False})
        journal = _json(_file(file, MAX_BYTES * 3))
        if isinstance(journal, dict) and journal.get('schema') == 'pocket.native-midi-write-journal/v1':
            _write_journal(journal, store_root)
            if any(journal[key] != native_request[key] for key in native_request):
                raise PocketError('Native writer journal filename identity mismatch')
            terminal = _write_terminal(journal, store_root)
            return receipt(request_id, status='ok' if terminal is not None else 'outcome_unknown',
                           artifacts={'bridge_journal': put_record(journal, store_root), **({'terminal': terminal} if terminal else {})},
                           journal_state=journal['state'], coverage={'redispatched': False, 'current_native_state': 'not_observed'},
                           next_actions=['Reconcile matching workspace with terminal'] if terminal else
                           ['Cancel only if prepared; otherwise retain quarantine and inspect native session'])
        _, result = _journal(journal)
        if any(journal[key] != native_request[key] for key in native_request):
            raise PocketError('Native journal filename identity mismatch')
        return receipt(request_id, status='ok' if result is not None else 'outcome_unknown',
                       artifacts={'bridge_journal': put_record(journal, store_root)}, journal_state=journal['state'],
                       native_result_status=result['status'] if result is not None else None,
                       coverage={'redispatched': False, 'current_native_state': 'not_observed', 'write_available': False})
    return run_request(store_root, request_id, 'native_midi_status', inputs, work)


def _writer_folder(bridge_dir):
    return _bridge_folder(bridge_dir or os.environ.get('POCKET_NATIVE_MIDI_WRITER_DIR', '~/.pocket/native-midi-writer'))


def _dyadic(value, positive=False):
    from fractions import Fraction
    _keys(value, {'n', 'd'}, 'native musical time')
    _number(value['n'], 0, 2**40, True)
    _number(value['d'], 1, 2**20, True)
    if math.gcd(value['n'], value['d']) != 1 or value['d'] & (value['d'] - 1):
        raise PocketError('Native note time requires a reduced exact dyadic rational')
    result = Fraction(value['n'], value['d'])
    if result > 1e6 or (positive and not result):
        raise PocketError('Native note time outside bounded profile')
    if Fraction.from_float(float(result)) != result:
        raise PocketError('Native note time cannot be represented exactly')
    return float(result)


def _write_edit(edit):
    if not isinstance(edit, dict):
        raise PocketError('Expected native note edit')
    if edit.get('kind') == 'insert_empty':
        _keys(edit, {'kind', 'notes'})
        if not isinstance(edit['notes'], list) or not 1 <= len(edit['notes']) <= 3:
            raise PocketError('Native insertion requires1–3 ordinary notes')
        notes = []
        for note in edit['notes']:
            _keys(note, {'pitch', 'onset_qn', 'duration_qn', 'velocity', 'release_velocity'})
            _number(note['pitch'], 0, 127, True)
            _number(note['velocity'], 1, 127, True)
            _number(note['release_velocity'], 0, 127, True)
            notes.append({'pitch': note['pitch'], 'start_time': _dyadic(note['onset_qn']),
                          'duration': _dyadic(note['duration_qn'], True), 'velocity': note['velocity'],
                          'release_velocity': note['release_velocity']})
        return {'kind': 'insert_empty', 'notes': notes}
    if edit.get('kind') == 'set_velocity':
        _keys(edit, {'kind', 'note_id', 'velocity', 'insertion_terminal'})
        _number(edit['note_id'], 1, 2**31 - 1, True)
        _number(edit['velocity'], 1, 127, True)
        return {key: edit[key] for key in ('kind', 'note_id', 'velocity')}
    raise PocketError('Native write operation is not implemented')


def _supervision(value):
    required = {'actor', 'actor_kind', 'observed_at', 'exclusive_native_session', 'no_ui_edits_after_native_read'}
    if not isinstance(value, dict) or set(value) not in (required, required | {'note'}):
        raise PocketError('Explicit supervised-session acknowledgment required')
    _text(value['actor'], 240)
    _timestamp(value['observed_at'])
    if value['actor_kind'] not in {'human', 'agent'} or value['exclusive_native_session'] is not True or value['no_ui_edits_after_native_read'] is not True:
        raise PocketError('Native write requires exclusive supervised session acknowledgment')
    if 'note' in value:
        _text(value['note'], 2000)


def _record_core(record, store_root):
    return {**{key: record[key] for key in ('host', 'topology', 'target', 'notes', 'guards')},
            'raw_notes_json': read_bytes(record['raw_notes'], store_root).decode('utf8')}


def _ordinary_guard(core, saved_path):
    if core['host'] != {'runtime_version': '12.4.5', 'file_path': saved_path}:
        raise PocketError('Native writer host/path outside qualified profile')
    if core['target']['device_track_runtime_id'] != core['target']['track_runtime_id']:
        raise PocketError('Native writer must be loaded on its owned target track')
    for name, expected in (('tempo', 120), ('signature_numerator', 4), ('signature_denominator', 4)):
        if core['guards']['song'][name].get('value') != expected or core['guards']['song'][name]['status'] != 'available':
            raise PocketError('Native tempo/meter outside qualified stock context')
    if core['guards']['song']['session_automation_record']['status'] != 'available':
        raise PocketError('Unknown automation arm state')
    zeros = {'song': {'is_playing', 'record_mode', 'arrangement_overdub', 'session_record'},
             'track': GUARDS['track'],
             'clip': {'has_envelopes', 'has_groove', 'muted', 'is_overdubbing', 'is_playing',
                      'is_recording', 'is_triggered', 'looping', 'loop_start', 'start_marker'}}
    for scope, names in zeros.items():
        for name in names:
            guard = core['guards'][scope][name]
            if guard['status'] != 'available' or guard['value'] != 0:
                raise PocketError(f'Unsupported native write guard: {scope}.{name}')
    for name in ('is_arrangement_clip', 'is_midi_clip'):
        if core['guards']['clip'][name].get('value') != 1 or core['guards']['clip'][name]['status'] != 'available':
            raise PocketError('Native write requires positively identified primary arrangement MIDI clip')
    for note in core['notes']:
        if note['mute'] != 0 or note['probability'] != 1 or note['velocity_deviation'] != 0:
            raise PocketError('Enriched notes are outside native write qualification')
        _number(note['velocity'], 1, 127, True)
        _number(note['release_velocity'], 0, 127, True)


def _native_plan(request, before):
    _ordinary_guard(before, request['saved_als_path'])
    fields = ('host', 'topology', 'target', 'notes', 'guards')
    if any(before[key] != request['expected'][key] for key in fields):
        raise PocketError('stale_state: complete native snapshot differs')
    edit = request['edit']
    if edit['kind'] == 'insert_empty':
        _keys(edit, {'kind', 'notes'})
        if before['notes'] or not isinstance(edit['notes'], list) or not 1 <= len(edit['notes']) <= 3:
            raise PocketError('Native insertion requires empty clip')
        length = before['guards']['clip']['end_marker'].get('value')
        _number(length, math.ulp(0.0), 1e6)
        notes = []
        for note in edit['notes']:
            _keys(note, {'pitch', 'start_time', 'duration', 'velocity', 'release_velocity'})
            _number(note['pitch'], 0, 127, True)
            _number(note['velocity'], 1, 127, True)
            _number(note['release_velocity'], 0, 127, True)
            _number(note['start_time'], 0, length)
            _number(note['duration'], math.ulp(0.0), length)
            if (note['start_time'] + note['duration'] > length or
                    any(value * 2**20 != int(value * 2**20) for value in (note['start_time'], note['duration']))):
                raise PocketError('Unsupported dyadic time or note outside clip')
            notes.append({**note, 'mute': 0, 'probability': 1, 'velocity_deviation': 0})
        for index, note in enumerate(notes):
            if any(note['start_time'] < other['start_time'] + other['duration']
                   and other['start_time'] < note['start_time'] + note['duration'] for other in notes[index + 1:]):
                raise PocketError('Temporal overlap outside native write profile')
        return 'add_new_notes', {'notes': notes}
    _keys(edit, {'kind', 'note_id', 'velocity'})
    if edit['kind'] != 'set_velocity':
        raise PocketError('Unsupported native method')
    _number(edit['note_id'], 1, 2**31 - 1, True)
    _number(edit['velocity'], 1, 127, True)
    selected = [note for note in before['notes'] if note['note_id'] == edit['note_id']]
    if len(selected) != 1:
        raise PocketError('Selected inserted note is absent')
    return 'apply_note_modifications', {'notes': [{**selected[0], 'velocity': edit['velocity']}]}


def _native_diff(request, before, after):
    method, dictionary = _native_plan(request, before)
    _ordinary_guard(after, request['saved_als_path'])
    if any(before[key] != after[key] for key in ('host', 'topology', 'target', 'guards')):
        raise PocketError('Native context changed during dispatch')
    if method == 'add_new_notes':
        fields = ('pitch', 'start_time', 'duration', 'velocity', 'release_velocity', 'mute', 'probability', 'velocity_deviation')
        actual = sorted(tuple(note[key] for key in fields) for note in after['notes'])
        expected = sorted(tuple(note[key] for key in fields) for note in dictionary['notes'])
        if actual != expected:
            raise PocketError('Inserted readback does not match exact requested notes')
    else:
        expected = [{**note, 'velocity': request['edit']['velocity']} if note['note_id'] == request['edit']['note_id'] else note for note in before['notes']]
        if after['notes'] != expected:
            raise PocketError('Velocity readback changed unselected fields')
    return method, dictionary


def _qualify_saved_write(record, store_root, edit, preparation):
    from .midi_io import _qualified_empty_note_metadata
    from .native_normalization import BUILD
    binding = record['saved_binding']
    if binding is None or binding['build'] != BUILD:
        raise PocketError('Native writer requires saved binding to exact qualified Live build')
    root = _xml(read_bytes(binding['saved_als'], store_root))
    prepared = _xml(read_bytes(preparation['prepared_als'], store_root))
    tracks = list(root.find('LiveSet/Tracks'))
    source_ids = {track.get('Id') for track in prepared.find('LiveSet/Tracks')}
    added = [track for track in tracks if track.get('Id') not in source_ids]
    if (len(added) != 1 or added[0].tag != 'MidiTrack' or added[0].get('Id') != binding['track_id'] or
            {track.get('Id') for track in tracks} != source_ids | {binding['track_id']}):
        raise PocketError('Native target is not the single explicitly owned added MIDI track')
    track = added[0]
    name = track.find('Name/EffectiveName')
    if name is None or name.get('Value') != preparation['layer']['track_name']:
        raise PocketError('Native added track differs from prepared layer name')
    devices = track.findall('DeviceChain/DeviceChain/Devices/*')
    if [device.tag for device in devices] != ['Operator', 'MxDeviceAudioEffect']:
        raise PocketError('Native profile requires exactly Operator followed by writer device')
    clips = track.findall('DeviceChain/MainSequencer/ClipTimeable/ArrangerAutomation/Events/MidiClip')
    if len(clips) != 1 or clips[0].get('Id') != binding['clip_id']:
        raise PocketError('Native target must be the sole primary clip on owned track')
    clip = clips[0]
    if not _qualified_empty_note_metadata(clip.find('Notes'), dict(root.attrib)):
        raise PocketError('Unknown saved per-note expression/probability metadata')
    if edit['kind'] == 'insert_empty' and clip.findall('Notes/KeyTracks/KeyTrack/Notes/MidiNoteEvent'):
        raise PocketError('Insert requires freshly saved empty clip')
    envelopes = clip.find('Envelopes')
    if envelopes is None or envelopes.attrib or [child.tag for child in envelopes] != ['Envelopes'] or envelopes[0].attrib or len(envelopes[0]):
        raise PocketError('Unknown or populated saved clip envelopes')
    if track.findall('.//TakeLane'):
        raise PocketError('Saved take lanes outside writer qualification')
    def value(path):
        node = clip.find(path)
        return node.get('Value') if node is not None else None
    if value('GrooveSettings/GrooveId') not in (None, '-1') or value('Loop/LoopOn') != 'false' or value('Loop/StartRelative') != '0' or value('Loop/LoopStart') != '0' or value('Disabled') not in (None, 'false'):
        raise PocketError('Saved playback transform outside writer qualification')
    _ordinary_guard(_record_core(record, store_root), binding['path'])


def _writer_package(handle, store_root, package_sha256):
    record = read_record(handle, store_root, 'pocket.native-midi-device/v1')
    _keys(record, {'schema', 'protocol', 'package_sha256', 'files'}, 'writer package')
    names = ('Native MIDI Writer.maxpat', 'bridge.js', 'device.js', 'reader.js', 'writer.js')
    _keys(record['files'], {*names, 'manifest.json', 'Native MIDI Writer.amxd'})
    if record['protocol'] != PROTOCOL or record['package_sha256'] != package_sha256:
        raise PocketError('Writer package differs from observed running package')
    for file_handle in record['files'].values():
        if not isinstance(file_handle, dict) or file_handle.get('artifact_schema') != 'pocket.native-midi-device-file/v1':
            raise PocketError('Writer package file artifact family mismatch')
    sources = {name: hashlib.sha256(read_bytes(record['files'][name], store_root)).hexdigest() for name in names}
    manifest = _json(read_bytes(record['files']['manifest.json'], store_root))
    if manifest != {'schema': 'pocket.native-midi-package/v1', 'protocol': PROTOCOL, 'sources': sources, 'package_sha256': digest(sources)} or digest(sources) != package_sha256:
        raise PocketError('Writer package source integrity mismatch')
    patch = read_bytes(record['files']['Native MIDI Writer.maxpat'], store_root) + b'\0'
    header = b'ampf' + struct.pack('<I', 4) + b'aaaa' + b'meta' + struct.pack('<II', 4, 0)
    if read_bytes(record['files']['Native MIDI Writer.amxd'], store_root) != header + b'ptch' + struct.pack('<I', len(patch)) + patch:
        raise PocketError('Writer device binary does not match retained patch')
    return record


def _qualified_writer_package(handle, store_root, package_sha256):
    record = _writer_package(handle, store_root, package_sha256)
    sources = Path(__file__).parent / 'devices' / 'native_midi_writer'
    names = ('Native MIDI Writer.maxpat', 'bridge.js', 'device.js', 'reader.js', 'writer.js')
    bundled = {name: hashlib.sha256(_file(sources / name, MAX_BYTES)).hexdigest() for name in names}
    if digest(bundled) != package_sha256:
        raise PocketError('Writer execution requires the exact current bundled package; rebuild and reload')
    return record


def _write_request(value, store_root=None):
    _keys(value, {'schema', 'request_key', 'session_nonce', 'package_sha256', 'target', 'expected',
                  'edit', 'saved_als_path', 'saved_als_sha256', 'workspace_path', 'workspace_sha256',
                  'binding', 'timeout_ms'}, 'write transport')
    if value['schema'] != 'pocket.native-midi-write-request/v1':
        raise PocketError('Unsupported native write request')
    for key in ('request_key', 'package_sha256', 'saved_als_sha256', 'workspace_sha256'):
        _hex(value[key])
    _hex(value['session_nonce'], 32)
    _number(value['timeout_ms'], 100, 60000, True)
    _target(value['target'])
    _core(value['expected'], value['target'])
    for key in ('saved_als_path', 'workspace_path'):
        path = Path(_text(value[key], 4096))
        if not path.is_absolute() or str(path) != os.path.normpath(str(path)):
            raise PocketError('Native write paths require normalized absolute paths')
    if Path(value['workspace_path']).name != 'workspace.json' or not Path(value['saved_als_path']).is_relative_to(Path(value['workspace_path']).parent):
        raise PocketError('Write target outside isolated workspace')
    binding = value['binding']
    _keys(binding, {'request_id', 'input_sha256', 'workspace_id', 'pending_revision',
                    'expected_observation', 'writer_device', 'public_edit', 'supervision'})
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', _text(binding['request_id'], 96)) or not re.fullmatch(r'workspace-[0-9a-f]{24}', _text(binding['workspace_id'], 64)):
        raise PocketError('Invalid native pending request/workspace identity')
    _hex(binding['input_sha256'])
    _number(binding['pending_revision'], 1, 2**31 - 1, True)
    _supervision(binding['supervision'])
    if _write_edit(binding['public_edit']) != value['edit']:
        raise PocketError('Native edit projection differs from canonical requested edit')
    if store_root is not None:
        expected = load_native_midi_observation(binding['expected_observation'], store_root)
        if _record_core(expected, store_root) != value['expected'] or expected['session_nonce'] != value['session_nonce'] or expected['package_sha256'] != value['package_sha256']:
            raise PocketError('Write expected observation correlation mismatch')
        saved = expected['saved_binding']
        if saved is None or saved['path'] != value['saved_als_path'] or saved['sha256'] != value['saved_als_sha256']:
            raise PocketError('Write saved binding correlation mismatch')
        _writer_package(binding['writer_device'], store_root, value['package_sha256'])
        _insert_lineage(binding['public_edit'], expected, store_root)
    return value


def _insert_lineage(edit, expected, store_root):
    if edit['kind'] != 'set_velocity':
        return
    raw = read_record(edit['insertion_terminal'], store_root, 'pocket.native-midi-terminal/v1')
    if raw.get('operation') != 'insert_empty':
        raise PocketError('Velocity edit requires direct insertion terminal')
    source_journal = read_record(raw.get('bridge_journal'), store_root, 'pocket.native-midi-write-journal/v1')
    source_request = _json(_text(source_journal.get('request_json'), MAX_BYTES))
    if (source_journal.get('operation') != 'insert_empty' or not isinstance(source_request, dict) or
            not isinstance(source_request.get('edit'), dict) or source_request['edit'].get('kind') != 'insert_empty'):
        raise PocketError('Velocity lineage must directly reference an actual insertion request')
    terminal = validate_native_terminal(edit['insertion_terminal'], store_root)
    if terminal['outcome'] != 'verified_readback' or terminal['session_nonce'] != expected['session_nonce'] or terminal['package_sha256'] != expected['package_sha256']:
        raise PocketError('Velocity source was not inserted in this device session')
    inserted = load_native_midi_observation(terminal['after_observation'], store_root)
    if inserted['target'] != expected['target'] or inserted['topology'] != expected['topology'] or inserted['notes'] != expected['notes']:
        raise PocketError('Velocity edit is restricted to the unchanged same-session inserted notes')
    if edit['note_id'] not in {note['note_id'] for note in inserted['notes']}:
        raise PocketError('Velocity note is not bound to insertion lineage')


def _write_result(value, request):
    fields = {'schema', 'request_key', 'session_nonce', 'package_sha256', 'protocol', 'stage',
              'outcome', 'dispatch_count', 'before', 'after', 'method', 'dictionary', 'call_return',
              'started_at', 'ended_at'}
    _keys(value, fields | ({'error'} if 'error' in value else set()), 'native write result')
    if value['schema'] != 'pocket.native-midi-write-result/v1' or value['protocol'] != PROTOCOL or any(value[key] != request[key] for key in ('request_key', 'session_nonce', 'package_sha256')):
        raise PocketError('Native write result correlation mismatch')
    if value['stage'] not in {'prepare', 'commit', 'cancel', 'transport_refusal'} or value['outcome'] not in {'ready', 'refused_before_dispatch', 'verified_readback', 'outcome_unknown'}:
        raise PocketError('Unknown native write result state')
    _number(value['dispatch_count'], 0, 1, True)
    if _timestamp(value['ended_at']) < _timestamp(value['started_at']):
        raise PocketError('Invalid write readback chronology')
    if 'error' in value:
        _text(value['error'], 2000)
    for key in ('before', 'after'):
        if value[key] is not None:
            _core(value[key], request['target'])
    if value['outcome'] == 'ready':
        if value['stage'] != 'prepare' or value['dispatch_count'] != 0 or value['after'] is not None:
            raise PocketError('Prepared native write has contradictory dispatch')
        method, dictionary = _native_plan(request, value['before'])
        if (method, dictionary) != (value['method'], value['dictionary']):
            raise PocketError('Prepared method/payload mismatch')
    elif value['outcome'] == 'refused_before_dispatch':
        if value['dispatch_count'] != 0 or value['after'] is not None or (value['before'] is None and value['stage'] != 'transport_refusal') or value['call_return'] is not None:
            raise PocketError('Refusal does not establish zero native calls')
        if 'error' not in value:
            raise PocketError('Native refusal requires explicit reason')
        if value['stage'] == 'transport_refusal' and (value['before'] is not None or value['method'] is not None or value['dictionary'] is not None or value['error'] != 'host_lease_conflict'):
            raise PocketError('Unrecognized transport refusal proof')
    elif value['outcome'] == 'verified_readback':
        if value['stage'] != 'commit' or value['dispatch_count'] != 1 or value['before'] is None or value['after'] is None:
            raise PocketError('Native write lacks complete verified readback')
        method, dictionary = _native_diff(request, value['before'], value['after'])
        if (method, dictionary) != (value['method'], value['dictionary']):
            raise PocketError('Dispatched method or note dictionary differs')
        if method == 'add_new_notes':
            returned = value['call_return']
            if (not isinstance(returned, list) or any(type(ident) is not int for ident in returned) or
                    len(returned) != len(value['after']['notes']) or len(set(returned)) != len(returned) or
                    set(returned) != {note['note_id'] for note in value['after']['notes']}):
                raise PocketError('Inserted native ID return disagrees with readback')
    return value


def _write_journal(value, store_root=None):
    if not isinstance(value, dict):
        raise PocketError('Expected write journal')
    fields = {'schema', 'protocol', 'session_nonce', 'package_sha256', 'request_key', 'request_json',
              'request_sha256', 'operation', 'state', 'host_lease_acquired', 'dispatch_intent_count', 'received_at',
              'prepare_result_json', 'prepare_result_sha256', 'result_json', 'result_sha256'}
    _keys(value, fields | ({'completed_at'} if value.get('state') == 'complete' else set()), 'write journal')
    if value['schema'] != 'pocket.native-midi-write-journal/v1' or value['protocol'] != PROTOCOL or value['state'] not in {'preparing', 'prepared', 'dispatched', 'complete'}:
        raise PocketError('Unsupported write journal')
    raw = _text(value['request_json'], MAX_BYTES)
    if hashlib.sha256(raw.encode()).hexdigest() != value['request_sha256']:
        raise PocketError('Write request hash mismatch')
    request = _write_request(_json(raw), store_root)
    if any(value[key] != request[key] for key in ('request_key', 'session_nonce', 'package_sha256')) or value['operation'] != request['edit']['kind']:
        raise PocketError('Write journal/request binding mismatch')
    if type(value['host_lease_acquired']) is not bool:
        raise PocketError('Native host lease proof must be explicit boolean')
    _number(value['dispatch_intent_count'], 0, 1, True)
    _timestamp(value['received_at'])
    prepared = None
    if value['prepare_result_json'] is not None:
        if hashlib.sha256(_text(value['prepare_result_json'], MAX_BYTES).encode()).hexdigest() != value['prepare_result_sha256']:
            raise PocketError('Native prepared response hash mismatch')
        prepared = _write_result(_json(value['prepare_result_json']), request)
        if prepared['stage'] not in {'prepare', 'transport_refusal'}:
            raise PocketError('Wrong preparation stage')
    elif value['prepare_result_sha256'] is not None:
        raise PocketError('Prepared response missing bytes')
    result = None
    if value['state'] == 'complete':
        _timestamp(value['completed_at'])
        if hashlib.sha256(_text(value['result_json'], MAX_BYTES).encode()).hexdigest() != value['result_sha256']:
            raise PocketError('Native terminal response hash mismatch')
        result = _write_result(_json(value['result_json']), request)
        if result['outcome'] == 'ready' or prepared is None:
            raise PocketError('Ready response is not a terminal outcome')
        if result['stage'] == 'commit':
            if value['dispatch_intent_count'] != 1 or prepared['outcome'] != 'ready':
                raise PocketError('Commit lacks durable prepared dispatch intent')
        elif value['dispatch_intent_count'] != 0:
            raise PocketError('Pre-dispatch result contradicts dispatch intent')
    elif value['result_json'] is not None or value['result_sha256'] is not None:
        raise PocketError('Incomplete journal claims terminal bytes')
    if value['state'] == 'preparing' and (prepared is not None or value['dispatch_intent_count'] != 0):
        raise PocketError('Preparing journal has contradictory state')
    if value['state'] in {'prepared', 'dispatched'} and (prepared is None or prepared['outcome'] != 'ready' or value['dispatch_intent_count'] != (1 if value['state'] == 'dispatched' else 0)):
        raise PocketError('Prepared/dispatch journal chronology mismatch')
    if not value['host_lease_acquired'] and value['state'] != 'preparing' and (result is None or result['stage'] != 'transport_refusal'):
        raise PocketError('Native dispatch lacks acquired global host lease')
    return request, prepared, result


def _write_observation(core, stage, request, result, store_root):
    """Retain a native read stage; the enclosing write journal binds its origin."""
    key = digest({'write_request_key': request['request_key'], 'stage': stage})
    wire = {'schema': 'pocket.native-midi-read-request/v1', 'request_key': key,
            'session_nonce': request['session_nonce'], 'target': request['target'], 'timeout_ms': request['timeout_ms']}
    raw = canonical_bytes(wire).decode()
    read_result = {'schema': OBSERVATION, 'request_key': key, 'session_nonce': request['session_nonce'],
                   'package_sha256': request['package_sha256'], 'protocol': PROTOCOL,
                   'read_started_at': result['started_at'], 'read_ended_at': result['ended_at'],
                   'status': 'ok', 'observation': core}
    result_json = canonical_bytes(read_result).decode()
    journal = {'schema': 'pocket.native-midi-bridge-journal/v1', 'protocol': PROTOCOL,
               'session_nonce': request['session_nonce'], 'package_sha256': request['package_sha256'],
               'request_key': key, 'request_json': raw, 'request_sha256': hashlib.sha256(raw.encode()).hexdigest(),
               'operation': 'read', 'state': 'complete', 'dispatch_count': 1, 'received_at': result['started_at'],
               'result_json': result_json, 'result_sha256': hashlib.sha256(result_json.encode()).hexdigest(),
               'completed_at': result['ended_at']}
    expected = load_native_midi_observation(request['binding']['expected_observation'], store_root)
    record = {key: read_result[key] for key in ('schema', 'request_key', 'session_nonce', 'package_sha256', 'protocol', 'read_started_at', 'read_ended_at')}
    record.update({key: core[key] for key in ('host', 'topology', 'target', 'notes', 'guards')})
    record.update({'raw_notes': put_bytes(core['raw_notes_json'].encode(), store_root, 'notes.json', 'pocket.native-midi-raw-notes/v1'),
                   'raw_guards': put_record({'schema': 'pocket.native-midi-guards/v1', 'request_key': key,
                                            'session_nonce': request['session_nonce'], 'package_sha256': request['package_sha256'], 'guards': core['guards']}, store_root),
                   'bridge_journal': put_record(journal, store_root), 'saved_binding': expected['saved_binding'], 'coverage': COVERAGE})
    return put_record(record, store_root)


def _write_terminal(journal, store_root):
    request, _, result = _write_journal(journal, store_root)
    if result is None or result['outcome'] not in {'refused_before_dispatch', 'verified_readback'}:
        return None
    binding = request['binding']
    terminal = {'schema': 'pocket.native-midi-terminal/v1', 'request_id': binding['request_id'],
                'request_key': request['request_key'], 'input_sha256': binding['input_sha256'],
                'session_nonce': request['session_nonce'], 'package_sha256': request['package_sha256'],
                'operation': request['edit']['kind'], 'workspace_id': binding['workspace_id'], 'pending_revision': binding['pending_revision'],
                'expected_observation': binding['expected_observation'],
                'before_observation': _write_observation(result['before'], 'before', request, result, store_root) if result['before'] is not None else binding['expected_observation'],
                'after_observation': _write_observation(result['after'], 'after', request, result, store_root) if result['after'] is not None else None,
                'bridge_journal': put_record(journal, store_root), 'saved_als_sha256': request['saved_als_sha256'],
                'outcome': result['outcome'], 'dispatch_count': result['dispatch_count']}
    handle = put_record(terminal, store_root)
    validate_native_terminal(handle, store_root)
    return handle


def validate_native_terminal(handle: ArtifactHandle, store_root: str) -> dict:
    """Verify exact method, dictionary, native reads and pending bindings; never dispatch."""
    _verify_handles(handle, store_root)
    terminal = read_record(handle, store_root, 'pocket.native-midi-terminal/v1')
    _keys(terminal, {'schema', 'request_id', 'request_key', 'input_sha256', 'session_nonce', 'package_sha256',
                     'operation', 'workspace_id', 'pending_revision', 'expected_observation', 'before_observation',
                     'after_observation', 'bridge_journal', 'saved_als_sha256', 'outcome', 'dispatch_count'})
    journal = read_record(terminal['bridge_journal'], store_root, 'pocket.native-midi-write-journal/v1')
    request, _, result = _write_journal(journal, store_root)
    if result is None or result['outcome'] not in {'refused_before_dispatch', 'verified_readback'}:
        raise PocketError('Unknown native outcome cannot release pending workspace')
    expected = {'request_id': request['binding']['request_id'], 'request_key': request['request_key'],
                'input_sha256': request['binding']['input_sha256'], 'session_nonce': request['session_nonce'],
                'package_sha256': request['package_sha256'], 'operation': request['edit']['kind'],
                'workspace_id': request['binding']['workspace_id'], 'pending_revision': request['binding']['pending_revision'],
                'expected_observation': request['binding']['expected_observation'], 'saved_als_sha256': request['saved_als_sha256'],
                'outcome': result['outcome'], 'dispatch_count': result['dispatch_count']}
    if any(terminal[key] != value for key, value in expected.items()) or type(terminal['dispatch_count']) is not int:
        raise PocketError('Native terminal binding mismatch')
    for stage in ('before', 'after'):
        observation = terminal[stage + '_observation']
        if result[stage] is None:
            if stage == 'before' and result['stage'] == 'transport_refusal' and observation == request['binding']['expected_observation']:
                continue
            if observation is not None:
                raise PocketError('Native terminal invents an absent read stage')
        else:
            record = load_native_midi_observation(observation, store_root)
            if _record_core(record, store_root) != result[stage] or record['request_key'] != digest({'write_request_key': request['request_key'], 'stage': stage}):
                raise PocketError('Native terminal read stage differs from actual bridge response')
    return terminal


def load_native_writer_device(handle: ArtifactHandle, store_root: str) -> dict:
    """Validate immutable writer package files without installing/contacting Live."""
    record = read_record(handle, store_root, 'pocket.native-midi-device/v1')
    return _writer_package(handle, store_root, record.get('package_sha256'))


def _host_lease_path():
    return Path.home() / '.pocket' / 'native-midi-writer' / 'host-lease.json'


@contextmanager
def _lease_release_lock():
    # Node only creates leases with O_EXCL. Every Python remover serializes on
    # this stable inode, which is never unlinked; process death releases flock.
    import fcntl
    path = _host_lease_path().parent / 'lease-release.lock'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise PocketError('Unsafe host lease release lock path')
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PocketError('Native lease reconciliation is busy; no lease was removed') from error
        yield
    finally:
        os.close(descriptor)


def _remove_matching_lease(expected):
    with _lease_release_lock():
        path = _host_lease_path()
        if not path.exists():
            return
        if _json(_file(path, 4096)) != expected:
            raise PocketError('Native host lease belongs to another request; no lock was removed')
        path.unlink()
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def release_native_host_lease(terminal: ArtifactHandle, store_root: str) -> None:
    """Release only a verified terminal's own lease, after workspace reconciliation."""
    record = validate_native_terminal(terminal, store_root)
    journal = read_record(record['bridge_journal'], store_root, 'pocket.native-midi-write-journal/v1')
    if not journal['host_lease_acquired']:
        return  # Proven transport refusal never owned another request's lease.
    expected = {'schema': 'pocket.native-midi-host-lease/v1', 'session_nonce': record['session_nonce'],
                'request_key': record['request_key'], 'package_sha256': record['package_sha256'],
                'request_sha256': journal['request_sha256'], 'workspace_id': record['workspace_id'],
                'pending_revision': record['pending_revision']}
    _remove_matching_lease(expected)


def _writer_response(descriptor, route, payload, timeout):
    status, response = _http(descriptor, route, timeout + 1, canonical_bytes(payload))
    if status != 200:
        detail = response.get('error', 'unrecognized bridge error') if isinstance(response, dict) else 'invalid error response'
        raise PocketError(f'Writer bridge HTTP {status}: {str(detail)[:2000]}; inspect status before any retry')
    _keys(response, {'schema', 'journal'})
    if response['schema'] != 'pocket.native-midi-write-response/v1':
        raise PocketError('Invalid native writer transport')
    _write_journal(response['journal'])
    return response['journal']


def native_midi_write(store_root: str, request_id: str, workspace_id: str, expected_revision: int,
                      saved_als: str, expected_sha256: str, expected_observation: ArtifactHandle,
                      writer_device: ArtifactHandle, edit: NativeMidiWriteEdit,
                      supervision: NativeSessionAcknowledgment, bridge_dir: str | None = None,
                      timeout_seconds: float = 15) -> dict:
    """Insert 1–3 nonoverlapping notes into a saved empty primary arrangement clip,
    or change one velocity using unchanged same-session insertion evidence.

    Requires the exact bundled writer, qualified Live 12.4.5, stopped 120 BPM/4/4,
    integer velocities and reduced dyadic quarter-note times (denominator ≤ 2**20).
    No automatic retry, save, transport, controller, groove or expression writes.

    The isolated workspace and host remain quarantined on an uncertain outcome.
    Same request IDs replay retained results; they never dispatch again.
    """
    from .native_candidates import native_workspace
    _number(expected_revision, 1, 2**31 - 1, True)
    _hex(expected_sha256)
    _number(timeout_seconds, 0.1, 60)
    _supervision(supervision)
    native_edit = _write_edit(edit)
    expected = load_native_midi_observation(expected_observation, store_root)
    load_native_writer_device(writer_device, store_root)
    _writer_package(writer_device, store_root, expected['package_sha256'])
    _insert_lineage(edit, expected, store_root)
    saved_path = str(Path(_text(saved_als, 4096)).expanduser().absolute())
    if expected['saved_binding'] is None or expected['saved_binding']['path'] != saved_path or expected['saved_binding']['sha256'] != expected_sha256:
        raise PocketError('Write requires exact expected saved observation binding')
    folder = _writer_folder(bridge_dir)
    inputs = {'workspace_id': workspace_id, 'expected_revision': expected_revision, 'saved_als': saved_path,
              'expected_sha256': expected_sha256, 'expected_observation': expected_observation,
              'writer_device': writer_device, 'edit': edit, 'supervision': supervision,
              'bridge_dir': str(folder), 'timeout_seconds': timeout_seconds}
    input_sha = digest({'operation': 'native_midi_write', 'inputs': inputs})
    key = digest({'request_id': request_id, 'input_sha256': input_sha, 'store_root': str(Path(store_root).expanduser().resolve())})

    def work():
        _qualified_writer_package(writer_device, store_root, expected['package_sha256'])
        if _host_lease_path().exists():
            return receipt(request_id, status='conflict', reason='Another native write holds the host lease',
                           coverage={'native_dispatched': False}, next_actions=['Inspect and reconcile the existing native request'])
        descriptor = _connection(folder, expected['session_nonce'], timeout_seconds)
        if descriptor is None or descriptor['package_sha256'] != expected['package_sha256']:
            return receipt(request_id, status='unsupported', reason='Matching writer device is not loaded', coverage={'native_dispatched': False})
        with native_workspace(store_root, workspace_id, expected_revision, saved_path, expected_sha256) as session:
            session.validate_observation(expected_observation, writer_device)
            preparation = read_record(session.state['preparation'], store_root, 'pocket.candidate-preparation/v1')
            _qualify_saved_write(expected, store_root, edit, preparation)
            core = _record_core(expected, store_root)
            provisional = {'saved_als_path': saved_path, 'expected': core, 'edit': native_edit}
            _native_plan(provisional, core)
            pending = session.begin(request_id=request_id, request_key=key, operation=native_edit['kind'],
                                    input_sha256=input_sha, session_nonce=expected['session_nonce'],
                                    package_sha256=expected['package_sha256'], expected_observation=expected_observation)
            workspace_path = session.folder / 'workspace.json'
            wire = {'schema': 'pocket.native-midi-write-request/v1', 'request_key': key,
                    'session_nonce': expected['session_nonce'], 'package_sha256': expected['package_sha256'],
                    'target': {name: expected['target'][name] for name in ('location', 'track_index', 'clip_index')},
                    'expected': core, 'edit': native_edit, 'saved_als_path': saved_path, 'saved_als_sha256': expected_sha256,
                    'workspace_path': str(workspace_path), 'workspace_sha256': hashlib.sha256(_file(workspace_path, 16 * 1024 * 1024)).hexdigest(),
                    'binding': {'request_id': request_id, 'input_sha256': input_sha, 'workspace_id': workspace_id,
                                'pending_revision': pending['pending_revision'], 'expected_observation': expected_observation,
                                'writer_device': writer_device, 'public_edit': edit, 'supervision': supervision},
                    'timeout_ms': int(timeout_seconds * 1000)}
            _write_request(wire, store_root)
            reference = {'session_nonce': expected['session_nonce'], 'request_key': key}
            try:
                journal = _writer_response(descriptor, '/prepare', wire, timeout_seconds)
                actual, prepared, result = _write_journal(journal, store_root)
                if actual != wire:
                    raise PocketError('Native prepare response input mismatch')
                if result is None and prepared is not None and prepared['outcome'] == 'ready':
                    # Source/workspace bytes are rechecked under the same physical
                    # lock before Node performs its fsynced commit admission.
                    session._check()
                    journal = _writer_response(descriptor, '/commit', {**reference, 'prepare_sha256': journal['prepare_result_sha256']}, timeout_seconds)
                    actual, _, result = _write_journal(journal, store_root)
                    if actual != wire:
                        raise PocketError('Native commit response input mismatch')
                journal_handle = put_record(journal, store_root)
                terminal = _write_terminal(journal, store_root)
                if terminal is None:
                    return receipt(request_id, status='outcome_unknown', artifacts={'bridge_journal': journal_handle, 'pending': pending['pending']},
                                   native_request=reference, workspace_id=workspace_id,
                                   recovery_arguments={'native_request': reference, 'bridge_dir': str(folder)},
                                   next_actions=['Inspect native_midi_status; never retry this request automatically'])
                outcome = validate_native_terminal(terminal, store_root)
                state = session.finish(terminal)
                release_native_host_lease(terminal, store_root)
                return receipt(request_id, status='ok' if outcome['outcome'] == 'verified_readback' else 'conflict',
                               artifacts={'terminal': terminal, 'before_observation': outcome['before_observation'],
                                          **({'after_observation': outcome['after_observation']} if outcome['after_observation'] else {})},
                               native_request=reference, workspace=state,
                               recovery_arguments={'native_request': reference, 'bridge_dir': str(folder)},
                               coverage={'artifact_integrity': 'verified', 'native_save_reopen': 'not_performed',
                                         'rendered_audio': 'not_performed', 'human_listening': 'none', 'musical_decision': None,
                                         'note_expression': 'unobserved', 'supervised_ordinary_note_scope': True},
                               next_actions=['Save and reopen natively, remove the writer device, then seal with final material evidence'])
            except (PocketError, OSError, http.client.HTTPException) as error:
                return receipt(request_id, status='outcome_unknown', reason=str(error)[:2000],
                               artifacts={'pending': pending['pending']}, native_request=reference, workspace_id=workspace_id,
                                   recovery_arguments={'native_request': reference, 'bridge_dir': str(folder)},
                               next_actions=['Inspect native_midi_status; cancel only a prepared request or reconcile a verified terminal'])
    result = run_request(store_root, request_id, 'native_midi_write', inputs, work)
    if result.get('artifacts', {}).get('terminal') is not None:
        validate_native_terminal(result['artifacts']['terminal'], store_root)
    return result


def native_midi_cancel(store_root: str, request_id: str, native_request: NativeRequestReference,
                       bridge_dir: str | None = None, timeout_seconds: float = 15) -> dict:
    """Durably cancel a prepared write only; never dispatch notes or undo a write."""
    _keys(native_request, {'session_nonce', 'request_key'})
    _hex(native_request['session_nonce'], 32)
    _hex(native_request['request_key'])
    _number(timeout_seconds, 0.1, 60)
    folder = _writer_folder(bridge_dir)
    inputs = {'native_request': native_request, 'bridge_dir': str(folder), 'timeout_seconds': timeout_seconds}

    def work():
        path = folder / 'journals' / native_request['session_nonce'] / (native_request['request_key'] + '.json')
        journal = _json(_file(path, MAX_BYTES * 3))
        _write_journal(journal, store_root)
        if journal['state'] != 'prepared' or any(journal[key] != native_request[key] for key in native_request):
            raise PocketError('Only this exact prepared, undispatched request can be cancelled')
        descriptor = _connection(folder, native_request['session_nonce'], timeout_seconds)
        if descriptor is None or descriptor['package_sha256'] != journal['package_sha256']:
            raise PocketError('Prepared writer session unavailable; no cancellation or retry performed')
        try:
            completed = _writer_response(descriptor, '/cancel', {**native_request, 'prepare_sha256': journal['prepare_result_sha256']}, timeout_seconds)
        except (PocketError, OSError, http.client.HTTPException) as error:
            return receipt(request_id, status='outcome_unknown', reason=str(error)[:2000],
                           native_request=native_request,
                           recovery_arguments={'native_request': native_request, 'bridge_dir': str(folder)},
                           coverage={'host_lease_retained': True},
                           next_actions=['Inspect status for a late terminal; this cancellation request will not redispatch'])
        if completed['request_json'] != journal['request_json']:
            raise PocketError('Cancellation response request identity mismatch')
        terminal = _write_terminal(completed, store_root)
        if terminal is None or validate_native_terminal(terminal, store_root)['dispatch_count'] != 0:
            raise PocketError('Cancellation did not establish a safe before-dispatch terminal')
        return receipt(request_id, status='cancelled', artifacts={'terminal': terminal},
                       native_request=native_request, recovery_arguments={'native_request': native_request, 'bridge_dir': str(folder)}, coverage={'native_dispatched': False, 'host_lease_retained': True},
                       next_actions=['Use candidate_native_reconcile with this terminal to release the matching workspace and host lease'])
    result = run_request(store_root, request_id, 'native_midi_cancel', inputs, work)
    if result.get('artifacts', {}).get('terminal') is not None:
        validate_native_terminal(result['artifacts']['terminal'], store_root)
    return result


def observe_native_abandonment(pending_handle: ArtifactHandle, fresh_observation: ArtifactHandle,
                               store_root: str, bridge_dir: str | None = None) -> ArtifactHandle:
    """Observe recovery evidence; supervision must separately establish old session closure."""
    _verify_handles(pending_handle, store_root)
    pending = read_record(pending_handle, store_root, 'pocket.native-pending/v1')
    fresh = load_native_midi_observation(fresh_observation, store_root)
    binding = fresh['saved_binding']
    if binding is None or fresh['session_nonce'] == pending['session_nonce']:
        raise PocketError('Abandonment requires a different freshly observed saved session')
    old_workspace = Path(store_root).expanduser().resolve() / 'workspaces' / pending['workspace_id']
    if Path(binding['path']).is_relative_to(old_workspace):
        raise PocketError('Recovery session must be outside abandoned workspace')
    if hashlib.sha256(_file(Path(binding['path']), 16 * 1024 * 1024)).hexdigest() != binding['sha256']:
        raise PocketError('Fresh recovery saved binding changed')
    stopped = fresh['guards']['song']['is_playing']
    if stopped['status'] != 'available' or stopped['value'] != 0:
        raise PocketError('Fresh recovery observation requires stopped transport')
    folder = _writer_folder(bridge_dir)
    with _lease_release_lock():
        lease_payload = _file(_host_lease_path(), 4096)
        lease = _json(lease_payload)
        _keys(lease, {'schema', 'session_nonce', 'request_key', 'package_sha256', 'request_sha256',
                      'workspace_id', 'pending_revision'}, 'native host lease')
        if lease['schema'] != 'pocket.native-midi-host-lease/v1' or any(lease[key] != pending[key] for key in ('session_nonce', 'request_key', 'package_sha256', 'workspace_id', 'pending_revision')):
            raise PocketError('Abandonment does not own the exact old host lease')
        _hex(lease['request_sha256'])
        # A failed health check is only support. It never establishes process
        # death; the required attributed supervision remains a separate fact.
        try:
            active = _connection(folder, pending['session_nonce'], 1)
            health = 'not_present' if active is None else 'healthy'
        except (PocketError, OSError, http.client.HTTPException):
            health = 'not_healthy'
        if health == 'healthy':
            raise PocketError('Old writer session remains healthy; unload it before supervised abandonment')
        journal_path = folder / 'journals' / pending['session_nonce'] / (pending['request_key'] + '.json')
        if journal_path.exists():
            journal = _json(_file(journal_path, MAX_BYTES * 3))
            _, _, result = _write_journal(journal, store_root)
            if journal['request_sha256'] != lease['request_sha256']:
                raise PocketError('Old lease differs from retained native request')
            if result is not None and result['outcome'] in {'verified_readback', 'refused_before_dispatch'}:
                raise PocketError('A verified terminal exists; reconcile it instead of abandoning')
        return put_record({'schema': 'pocket.native-abandonment-observation/v1', 'pending': pending_handle,
                           'fresh_observation': fresh_observation, 'observed_at': datetime.now(UTC).isoformat(),
                           'old_bridge_dir': str(folder), 'old_bridge_health': health,
                           'lease': put_bytes(lease_payload, store_root, 'host-lease.json', 'pocket.native-midi-host-lease-bytes/v1'),
                           'lease_record': lease}, store_root)


def release_native_abandoned_lease(abandonment_handle: ArtifactHandle, store_root: str) -> None:
    """Release after permanent attributed abandonment; never invent a native terminal."""
    _verify_handles(abandonment_handle, store_root)
    record = read_record(abandonment_handle, store_root, 'pocket.native-abandonment/v1')
    _keys(record, {'schema', 'workspace_id', 'pending_revision', 'abandoned_revision', 'pending',
                   'fresh_observation', 'recovery_observation', 'attribution', 'outcome', 'policy', 'source_preservation'})
    if record['outcome'] != 'unknown' or record['policy'] != 'permanently_unsealable_no_resume':
        raise PocketError('Abandonment cannot reclassify an unknown native outcome')
    observation = read_record(record['recovery_observation'], store_root, 'pocket.native-abandonment-observation/v1')
    _keys(observation, {'schema', 'pending', 'fresh_observation', 'observed_at', 'old_bridge_dir',
                        'old_bridge_health', 'lease', 'lease_record'})
    if observation['pending'] != record['pending'] or observation['fresh_observation'] != record['fresh_observation'] or observation['old_bridge_health'] not in {'not_present', 'not_healthy'}:
        raise PocketError('Abandonment observation scope mismatch')
    _timestamp(observation['observed_at'])
    attribution = record['attribution']
    confirmations = {'all_old_live_max_instances_stopped', 'old_writer_unloaded',
                     'old_candidate_closed_without_saving', 'no_native_dispatch_in_flight'}
    _keys(attribution, {'actor', 'actor_kind', 'observed_at', 'reason', *confirmations})
    if any(attribution[name] is not True for name in confirmations) or attribution['actor_kind'] not in {'human', 'agent'}:
        raise PocketError('Explicit attributed old session closure is required')
    _text(attribution['actor'], 240)
    _text(attribution['reason'], 2000)
    _timestamp(attribution['observed_at'])
    pending = read_record(record['pending'], store_root, 'pocket.native-pending/v1')
    lease = observation['lease_record']
    if observation['lease'].get('artifact_schema') != 'pocket.native-midi-host-lease-bytes/v1' or _json(read_bytes(observation['lease'], store_root)) != lease:
        raise PocketError('Abandonment retained lease bytes differ')
    if any(record[key] != pending[key] for key in ('workspace_id', 'pending_revision')) or any(lease[key] != pending[key] for key in ('session_nonce', 'request_key', 'package_sha256', 'workspace_id', 'pending_revision')):
        raise PocketError('Abandonment pending/lease identity mismatch')
    _number(record['abandoned_revision'], pending['pending_revision'] + 1, 2**31 - 1, True)
    fresh = load_native_midi_observation(record['fresh_observation'], store_root)
    if fresh['session_nonce'] == pending['session_nonce'] or fresh['saved_binding'] is None or fresh['guards']['song']['is_playing'].get('value') != 0:
        raise PocketError('Abandonment fresh session proof is invalid')
    _remove_matching_lease(lease)
