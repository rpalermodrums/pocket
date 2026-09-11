"""Thread queries over bounded, immutable local saved-set snapshots.

Handles reference hash-bound disk artifacts, not process globals. Filesystem
identity and saved control intent never imply native media loading or audibility.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import uuid
import zlib
from collections import Counter
from itertools import pairwise
from pathlib import Path
from typing import Literal

from typing_extensions import TypedDict

from .assets import sha256_file
from .errors import PocketError
from .thread import _filesystem_version, inspect_set, source_position
from .source_frames import source_frame_interval
from .timing import TempoMap, finite


class SetHandle(TypedDict):
    schema: Literal['pocket.set-handle/v1']
    cache_path: str
    cache_sha256: str
    set_sha256: str


LIMITATION = ('Saved intent and filesystem evidence only; native loaded media, audible DSP '
              'and musical bar one are not established.')
DEFAULT_MAX_BYTES = 16000


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def _integer(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise PocketError(f'{label} must be an integer from {low} to {high}')
    return value


def parse_set_time(value: float | str) -> float:
    """Parse nonnegative seconds or an explicit mm:ss[.fraction]/hh:mm:ss time."""
    if isinstance(value, str) and ':' in value:
        if not re.fullmatch(r'\d+:\d{1,2}(?::\d{1,2})?(?:\.\d+)?', value):
            raise PocketError('Time must be seconds, mm:ss or hh:mm:ss')
        parts = [float(p) for p in value.split(':')]
        if any(p >= 60 for p in parts[1:]):
            raise PocketError('Time seconds/minutes components must be below 60')
        seconds = sum(p * 60 ** i for i, p in enumerate(reversed(parts)))
    else:
        seconds = finite(value, 'arrangement seconds')
    if not math.isfinite(seconds) or seconds < 0:
        raise PocketError('Arrangement seconds must be finite and at or after zero')
    return seconds


def _coverage(m):
    clips = m['clips']
    lanes = [e for t in m['tracks'] for e in t['automation']]
    lanes += [e for c in clips for e in c['clip_envelopes']]
    runtime = [d for d in m['dependencies'] if d['runtime_dependency'] is True]
    audio = [c for c in clips if c['type'] == 'audio' and c['scope'] == 'arrangement']
    return {'audio_mapping': dict(Counter(c['mapping']['status'] for c in audio)),
            'midi_arrangement_instances': sum(c['type'] == 'midi' and c['scope'] == 'arrangement' for c in clips),
            'controls': dict(Counter(e['target_status'] for e in lanes)),
            'runtime_filesystem_references': dict(Counter(d['status'] for d in runtime)),
            'unreadable_audio_headers': sum(bool(d.get('header_error')) for d in runtime),
            'relative_reference_absence': sum(any(c['origin'] == 'relative' and not c['exists']
                                                 for c in d['candidates']) for d in runtime),
            'unclassified_references': sum(d['runtime_dependency'] is None for d in m['dependencies']),
            'dormant_provenance': dict(Counter(d['status'] for d in m['dependencies']
                                             if d['runtime_dependency'] is False)),
            'tempo': m['tempo']['status'], 'native_loaded_media': 'not_verified',
            'audible_dsp': 'not_evaluated'}


def _warnings(m):
    groups = Counter(w['code'] for w in m['warnings'])
    cov = _coverage(m)
    groups['unsupported_audio_mapping'] += cov['audio_mapping'].get('unknown', 0)
    for status in ('ambiguous', 'unresolved'):
        groups['automation_target_' + status] += cov['controls'].get(status, 0)
    groups['relative_reference_absence'] += cov['relative_reference_absence']
    return [{'code': code, 'count': count} for code, count in sorted(groups.items()) if count]


def _file_state(m):
    states = {}
    for d in m['dependencies']:
        # Unclassified refs cannot safely be presumed dormant.
        if d['runtime_dependency'] is not False:
            for v in d.get('filesystem_snapshot', []):
                if v['path'] in states and states[v['path']] != v['version']:
                    raise PocketError('Dependency changed during snapshot creation')
                states[v['path']] = v['version']
    # A newly created/deleted project marker may alter relative resolution.
    for parent in Path(m['path']).parents:
        marker = parent / 'Ableton Project Info'
        states[str(marker)] = {'exists': marker.is_dir()}
    return states


def _verify_files(snapshot):
    m = snapshot['set_map']
    path = Path(m['path'])
    if _filesystem_version(path) != snapshot['set_version'] or sha256_file(path) != m['sha256']:
        raise PocketError('Stale set handle: saved set changed; inspect it again')
    for path_string, version in snapshot['dependency_versions'].items():
        path = Path(path_string)
        actual = {'exists': path.is_dir()} if isinstance(version, dict) and 'exists' in version else _filesystem_version(path)
        if actual != version:
            raise PocketError(f'Stale set handle: dependency changed ({path.name}); inspect it again')


def _load(handle: SetHandle):
    if (not isinstance(handle, dict) or handle.get('schema') != 'pocket.set-handle/v1'
            or not all(isinstance(handle.get(k), str) for k in ('cache_path', 'cache_sha256', 'set_sha256'))
            or not all(re.fullmatch('[0-9a-f]{64}', handle[k]) for k in ('cache_sha256', 'set_sha256'))):
        raise PocketError('Expected a complete pocket.set-handle/v1 returned by inspect_set_summary')
    try:
        path = Path(handle['cache_path']).expanduser()
        if not path.is_absolute():
            raise PocketError('Set handle cache_path must be absolute')
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != handle['cache_sha256']:
            raise PocketError('Set handle cache identity mismatch')
        snapshot = json.loads(gzip.decompress(payload))
        if (snapshot['schema'] != 'pocket.set-snapshot/v1'
                or snapshot['set_map']['schema'] != 'pocket.set-map/v1'
                or snapshot['set_map']['sha256'] != handle['set_sha256']):
            raise PocketError('Set handle and snapshot identity mismatch')
    except (OSError, EOFError, zlib.error, ValueError, KeyError, TypeError) as error:
        raise PocketError(f'Cannot read set handle snapshot: {error}') from error
    _verify_files(snapshot)
    return snapshot


def inspect_set_summary(path: str | Path, *, cache_dir: str | Path | None = None,
                        hash_sources: bool = False) -> dict:
    """Create a new immutable disk snapshot and return a small cross-process handle.

    Full source hashing is opt-in. Every subsequent query verifies ALS bytes and
    all active/unclassified reference versions (device/inode/size/mtime/ctime,
    symlink target and missing paths). Any version change invalidates the handle;
    it never silently reuses a changed source, even if its mtime is restored.
    """
    if not isinstance(hash_sources, bool):
        raise PocketError('hash_sources must be a boolean')
    m = inspect_set(path, hash_sources=hash_sources)
    snapshot = {'schema': 'pocket.set-snapshot/v1', 'set_map': m,
                'set_version': _filesystem_version(Path(m['path'])),
                'dependency_versions': _file_state(m)}
    _verify_files(snapshot)
    directory = (Path(cache_dir).expanduser() if cache_dir is not None else
                 Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'pocket' / 'set-maps').resolve()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        cache_path = directory / (m['sha256'][:16] + '-' + uuid.uuid4().hex + '.json.gz')
        payload = gzip.compress(_json(snapshot), mtime=0)
        with cache_path.open('xb') as stream:
            stream.write(payload)
        _verify_files(snapshot)
    except OSError as error:
        raise PocketError(f'Cannot write set snapshot: {error}') from error
    handle: SetHandle = {'schema': 'pocket.set-handle/v1', 'cache_path': str(cache_path),
                         'cache_sha256': hashlib.sha256(payload).hexdigest(), 'set_sha256': m['sha256']}
    return {'schema': 'pocket.set-summary/v1', 'handle': handle,
            'set': {'filename': m['filename'], 'sha256': m['sha256'], 'creator': m['creator']},
            'summary': m['summary'], 'coverage': _coverage(m), 'warnings': _warnings(m),
            'raw_state': 'Use export_thread(handle, output_path) for the full inventory', 'scope': LIMITATION}


def _clip_card(c, tempo):
    return {'id': c['id'], 'track_id': c['track_id'], 'name': c['name'], 'type': c['type'],
            'scope': c['scope'], 'disabled': c['disabled'],
            'start_beat': c['start_beat'], 'end_beat': c['end_beat'],
            'start_seconds': tempo.beat_to_seconds(c['start_beat']) if tempo and c['scope'] == 'arrangement' and c['start_beat'] is not None else None,
            'end_seconds': tempo.beat_to_seconds(c['end_beat']) if tempo and c['scope'] == 'arrangement' and c['end_beat'] is not None else None,
            'mapping': c['mapping'], 'note_count': c.get('note_count')}


def find_clips(handle: SetHandle, query: str = '', *, limit: int = 20, offset: int = 0) -> dict:
    """Find canonical clip instances by case-insensitive name, ID or track name."""
    if not isinstance(query, str) or len(query) > 256:
        raise PocketError('Clip query must be a string of at most 256 characters')
    _integer(limit, 'limit', 1, 100)
    _integer(offset, 'offset', 0, 1000000)
    snapshot = _load(handle)
    m = snapshot['set_map']
    names = {t['id']: t['name'] for t in m['tracks']}
    needle = query.casefold()
    matches = [c for c in m['clips'] if needle in ' '.join((c['id'], c['name'], names[c['track_id']])).casefold()]
    tempo = TempoMap(m['tempo']['points']) if m['tempo']['status'] == 'supported' else None
    chosen = matches[offset:offset + limit]
    _verify_files(snapshot)
    return {'schema': 'pocket.clip-search/v1', 'set_sha256': m['sha256'],
            'clips': [_clip_card(c, tempo) for c in chosen], 'total_matches': len(matches),
            'returned': len(chosen), 'offset': offset, 'limit': limit,
            'truncated': offset + len(chosen) < len(matches),
            'next_offset': offset + len(chosen) if offset + len(chosen) < len(matches) else None,
            'coverage': _coverage(m), 'warnings': _warnings(m)}


def _parameter_path(path):
    # Canonical owner track ID remains in the parent object; preserve the rest.
    return path.split('/DeviceChain/', 1)[-1]


def _event_window(lane, start, end, limit):
    events = lane['events']
    numeric = all(isinstance(e['time'], (int, float)) and not isinstance(e['time'], bool)
                  and math.isfinite(e['time']) for e in events)
    ordered = numeric and all(a['time'] < b['time'] for a, b in pairwise(events))
    if ordered:
        before = [e for e in events if e['time'] < start]
        inside = [e for e in events if start <= e['time'] <= end]
        after = [e for e in events if e['time'] > end]
        relevant = before[-1:] + inside + after[:1]
    else:
        relevant = events
    # Always retain boundary brackets where available. Interior omissions explicit.
    selected = relevant if len(relevant) <= limit else [relevant[0], *relevant[1:limit - 1], relevant[-1]]
    plain = ordered and all(e['type'] == 'FloatEvent' and not e['children']
                            and not (set(e['attributes']) - {'Id', 'Time', 'Value'})
                            and isinstance(e['value'], (int, float)) and not isinstance(e['value'], bool)
                            for e in events)
    def at(beat):
        if beat <= events[0]['time']:
            return events[0]['value']
        for left, right in pairwise(events):
            if beat <= right['time']:
                return left['value'] + (right['value'] - left['value']) * (beat - left['time']) / (right['time'] - left['time'])
        return events[-1]['value']
    known = plain and bool(events) and lane['target_status'] == 'resolved'
    values = [at(start), at(end), *[e['value'] for e in events if start < e['time'] < end]] if known else []
    types = sorted({e['type'] for e in events})
    return {'target_id': lane['target_id'], 'target_status': lane['target_status'],
            'parameters': [{'path': _parameter_path(t['parameter_path']), 'manual': t['manual']}
                           for t in lane['targets']],
            'event_interpretation': 'linear saved numeric knots; no DSP evaluation' if plain else 'raw/unsupported event shape',
            'window_values': {'start': values[0], 'end': values[1], 'minimum': min(values), 'maximum': max(values)} if known else None,
            'event_types': types,
            'events': [{'time': e['time'], 'value': e['value'],
                        **({'type': e['type']} if len(types) > 1 else {}),
                        **({'extra_attributes': {k: v for k, v in e['attributes'].items() if k not in ('Id', 'Time', 'Value')}}
                           if set(e['attributes']) - {'Id', 'Time', 'Value'} else {}),
                        **({'children_retained_in_raw_snapshot': True} if e['children'] else {})} for e in selected],
            'relevant_event_count': len(relevant), 'events_returned': len(selected),
            'events_truncated': len(selected) < len(relevant), 'ordered_times': ordered}


def _manual_controls(track):
    found = {}
    def walk(node, path):
        if not node:
            return
        suffix = ('[id=' + node['attributes']['Id'] + ']') if 'Id' in node['attributes'] else ''
        current = path + '/' + node['tag'] + suffix
        for child in node['children']:
            if child['tag'] == 'Manual':
                key = current.removeprefix('/Mixer/')
                value = child['attributes'].get('Value')
                if key in found:
                    prior = found[key] if isinstance(found[key], list) else [found[key]]
                    found[key] = [*prior, value]
                else:
                    found[key] = value
            elif child['tag'] not in ('AutomationTarget', 'ModulationTarget'):
                walk(child, current)
    walk(track['mixer'], '')
    return found


def _device_context(device, automated_targets):
    result = {'class': device['class'], 'native_id': device['native_id'], 'on': device['on'],
              'coverage': device['coverage'], 'parameter_count_in_raw_snapshot': len(device['parameters'])}
    params = {p['path'][len(device['id']) + 1:]: p for p in device['parameters']}
    if device['class'] == 'Eq8':
        bands = sorted({k.rsplit('/', 1)[0] for k, p in params.items() if '/Parameter' in k
                        and ((k.endswith('/IsOn') and p['manual'] is True)
                             or set(p['automation_targets']) & automated_targets)})
        result['eq_bands_saved_on_or_automated'] = {
            band: {k.rsplit('/', 1)[-1]: p['manual'] for k, p in params.items() if k.rsplit('/', 1)[0] == band}
            for band in bands}
        result['global_parameters'] = {k: p['manual'] for k, p in params.items() if '/' not in k}
        result['scalar_modes'] = {p['path'][len(device['id']) + 1:]: p['value']
                                  for p in device['saved_scalar_settings']
                                  if p['path'][len(device['id']) + 1:] in ('Mode', 'Precision', 'AdaptiveQ')}
        result['inactive_bands'] = 'Other band parameters retained in raw snapshot'
    elif device['class'] == 'StereoGain':
        result['manual_parameters'] = {k: p['manual'] for k, p in params.items() if '/' not in k}
    else:
        result['static_parameters'] = 'Retained in raw snapshot; device DSP not evaluated'
    return result


def _track_controls(track, start, end, limit):
    targets = {e['target_id'] for e in track['automation']}
    return {'track_id': track['id'], 'name': track['name'], 'type': track['type'],
            'mixer_manual_controls': _manual_controls(track),
            'automation': [_event_window(e, start, end, limit) for e in track['automation']],
            'devices': [_device_context(d, targets) for d in track['devices']]}


def _clip_region(m, c, tempo, start, end):
    first, last = max(c['start_beat'], start), min(c['end_beat'], end)
    result = {**_clip_card(c, tempo), 'intersection': {'start_beat': first, 'end_beat': last,
              'start_seconds': tempo.beat_to_seconds(first), 'end_seconds': tempo.beat_to_seconds(last)},
              'sample_gain_amplitude': c['sample_gain_amplitude'],
              'pitch_semitones': c['pitch_semitones'], 'pitch_cents': c['pitch_cents'],
              'clip_fade_enabled': c['clip_fade_enabled'],
              'fade_settings': {n['tag']: n['attributes'].get('Value') for n in c['fades']['children']}
                              if c['fades'] else None,
              'clip_envelopes': {'count': len(c['clip_envelopes']),
                                 'status': 'not_evaluated; full structures in raw snapshot' if c['clip_envelopes'] else 'none'},
              'source_interval': None}
    if c['type'] == 'midi':
        result['midi'] = {'note_count_in_clip': c.get('note_count', 0),
                          'loop': c['loop'], 'notes': 'Full notes in raw snapshot; no audio-source mapping'}
        return result
    dep = next((d for d in m['dependencies'] if d['id'] == c['source_dependency_id']), None)
    result['source'] = {'dependency_id': c['source_dependency_id'],
                        'path': c['source_identity']['resolved_path'],
                        'asset_id': c['source_identity']['asset_id'],
                        'filesystem_status': dep['status'] if dep else 'unresolved',
                        'native_loaded_media': 'not_verified',
                        'relative_reference_missing': bool(dep and any(p['origin'] == 'relative' and not p['exists']
                                                                      for p in dep['candidates']))}
    a, b = source_position(m, c['id'], first), source_position(m, c['id'], last)
    if a['status'] != 'mapped' or b['status'] != 'mapped':
        result['source_interval'] = {'status': 'unknown', 'reason': a.get('reason') or b.get('reason')}
        return result
    header = c['source_identity']['header']
    if not header:
        result['source_interval'] = {'status': 'unknown', 'reason': 'Decoded source header unavailable',
                                     'raw_start_seconds': a['source_seconds'], 'raw_end_seconds': b['source_seconds']}
    else:
        result['source_interval'] = source_frame_interval(a['source_seconds'], b['source_seconds'],
                                                         header['sample_rate'], header['frames'])
    result['mapping_basis'] = a['basis']
    result['dsp_sample_exact'] = False
    return result


def _budget(result, maximum):
    """Remove complete list entries with explicit omissions, never hide truncation."""
    while len(_json(result)) > maximum:
        if result.get('controls'):
            removed = result['controls'].pop()
            result['truncation']['control_track_ids_omitted'].append(removed['track_id'])
        elif result.get('clips'):
            result['clips'].pop()
            result['truncation']['clips_omitted_for_bytes'] += 1
        else:
            raise PocketError('max_bytes is too small even for query metadata; increase it')
    result['truncation']['any'] = bool(result['truncation']['control_track_ids_omitted']
                                      or result['truncation']['clips_omitted_for_bytes']
                                      or result['truncation']['clips_omitted_for_limit']
                                      or result['truncation']['event_lanes_truncated'])
    return result


def query_set_region(handle: SetHandle, start_seconds: float | str, duration_seconds: float = 32,
                     *, max_clips: int = 16, clip_offset: int = 0,
                     max_events_per_lane: int = 12, max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    """Query a half-open Arrangement time interval, returning explicit truncation.

    Known clip intersections are not requested source crop expansion. Each source
    interval reports inward complete frames and raw coordinates. Curved tempo
    cannot establish a seconds-based region and returns unknown, never a guess.
    """
    start_seconds = parse_set_time(start_seconds)
    duration = finite(duration_seconds, 'duration seconds')
    if not 0 < duration <= 600:
        raise PocketError('Duration must be >0 and at most 600 seconds')
    _integer(max_clips, 'max_clips', 1, 100)
    _integer(clip_offset, 'clip_offset', 0, 1000000)
    _integer(max_events_per_lane, 'max_events_per_lane', 2, 1000)
    _integer(max_bytes, 'max_bytes', 2000, 1000000)
    snapshot = _load(handle)
    m = snapshot['set_map']
    end_seconds = start_seconds + duration
    if not math.isfinite(end_seconds):
        raise PocketError('Query end must be finite')
    result = {'schema': 'pocket.set-region/v1', 'status': 'known', 'set_sha256': m['sha256'],
              'region': {'start_seconds': start_seconds, 'end_seconds': end_seconds},
              'coverage': _coverage(m), 'warnings': _warnings(m), 'clips': [], 'controls': [],
              'truncation': {'any': False, 'clips_omitted_for_limit': 0, 'clips_omitted_for_bytes': 0,
                             'control_track_ids_omitted': [], 'event_lanes_truncated': 0},
              'scope': LIMITATION}
    if m['tempo']['status'] != 'supported':
        return {**result, 'status': 'unknown', 'reason': m['tempo']['reason']}
    tempo = TempoMap(m['tempo']['points'])
    first, last = tempo.seconds_to_beat(start_seconds), tempo.seconds_to_beat(end_seconds)
    matches = [c for c in m['clips'] if c['scope'] == 'arrangement'
               and c['start_beat'] is not None and c['end_beat'] is not None
               and c['start_beat'] < last and c['end_beat'] > first]
    matches.sort(key=lambda c: (c['start_beat'], c['id']))
    chosen = matches[clip_offset:clip_offset + max_clips]
    result['region'].update(start_beat=first, end_beat=last)
    result['total_overlapping_clips'] = len(matches)
    result['clip_offset'] = clip_offset
    result['clips'] = [_clip_region(m, c, tempo, first, last) for c in chosen]
    relevant = {c['track_id'] for c in chosen}
    control_tracks = [t for t in m['tracks'] if t['id'] in relevant or t['type'] in ('MainTrack', 'MasterTrack')]
    result['controls'] = [_track_controls(t, first, last, max_events_per_lane) for t in control_tracks]
    result['truncation']['clips_omitted_for_limit'] = max(0, len(matches) - clip_offset - len(chosen))
    result['truncation']['event_lanes_truncated'] = sum(e['events_truncated'] for t in result['controls'] for e in t['automation'])
    result = _budget(result, max_bytes - 128)
    if chosen and not result['clips']:
        raise PocketError('Insufficient max_bytes for even one overlapping clip; increase the budget or export the raw map')
    result['returned_clips'] = len(result['clips'])
    result['next_clip_offset'] = clip_offset + len(result['clips']) if clip_offset + len(result['clips']) < len(matches) else None
    result['response_bytes'] = 0
    for _ in range(4):
        result['response_bytes'] = len(_json(result))
    _verify_files(snapshot)
    return result


def export_thread(handle: SetHandle, output_path: str | Path) -> dict:
    """Export full raw inventory to a new explicit path, without echoing it."""
    snapshot = _load(handle)
    destination = Path(output_path).expanduser().resolve()
    payload = _json(snapshot['set_map']) + b'\n'
    try:
        with destination.open('xb') as stream:
            stream.write(payload)
    except OSError as error:
        raise PocketError(f'Cannot create new raw map export: {error}') from error
    _verify_files(snapshot)
    return {'schema': 'pocket.set-export/v1', 'path': str(destination),
            'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload),
            'set_sha256': snapshot['set_map']['sha256']}


# Preserve the original public callable as the exact same function object.
export_set_map = export_thread
