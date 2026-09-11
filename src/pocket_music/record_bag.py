"""Immutable local catalogues; attributed annotation is not measured sound."""
from __future__ import annotations

import hashlib
import json
import math
import shutil
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from .assets import identify_audio
from .errors import PocketError
from .selection_types import BagHandle, BagTrackInput

TRACK_KEYS = frozenset(BagTrackInput.__annotations__)
PROFILE_KEYS = frozenset({'tags', 'roles', 'energy', 'bpm', 'bpm_candidates', 'key',
                          'vocal_density', 'notes', 'provenance'})


def _text(value, label, maximum=300, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise PocketError(f'{label} must be a nonempty string of at most {maximum} characters')
    return value


def _number(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise PocketError(f'{label} must be a finite number from {low} to {high}')
    return value


def _strings(value, label, *, maximum=100, item_maximum=300):
    if not isinstance(value, list) or len(value) > maximum:
        raise PocketError(f'{label} must be a list of at most {maximum} strings')
    for item in value:
        _text(item, label, item_maximum)
    return value


def _version(path):
    try:
        stat = path.stat()
    except OSError as error:
        raise PocketError(f'Cannot inspect local audio: {error}') from error
    return {'device': stat.st_dev, 'inode': stat.st_ino, 'size': stat.st_size,
            'mtime_ns': stat.st_mtime_ns, 'ctime_ns': stat.st_ctime_ns}


def _digest_json(value):
    payload = (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode()
    return payload, hashlib.sha256(payload).hexdigest()


def _write_manifest(manifest, output_dir, filename, handle_schema):
    """Publish only in a newly created directory; never overwrite a prior trial."""
    payload, digest = _digest_json(manifest)
    directory = Path(output_dir).expanduser().resolve()
    created = False
    try:
        directory.mkdir(parents=True, exist_ok=False)
        created = True
        path = directory / filename
        with path.open('xb') as stream:
            stream.write(payload)
        with (directory / (filename + '.sha256')).open('x') as stream:
            stream.write(digest + '\n')
    except OSError as error:
        if created:
            shutil.rmtree(directory)
        raise PocketError(f'Cannot create a new immutable output directory: {error}') from error
    return {'schema': handle_schema, 'path': str(path), 'sha256': digest}


def _load_manifest(handle, handle_schema, manifest_schema):
    if (not isinstance(handle, dict) or handle.get('schema') != handle_schema
            or not isinstance(handle.get('path'), str) or not isinstance(handle.get('sha256'), str)):
        raise PocketError(f'Expected a {handle_schema} handle')
    path = Path(handle['path']).expanduser()
    if not path.is_absolute():
        raise PocketError('Manifest handle path must be absolute')
    try:
        before = _version(path)
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        seal = path.with_name(path.name + '.sha256').read_text().strip()
        if digest != handle['sha256'] or seal != digest:
            raise PocketError('Manifest identity or seal mismatch')
        manifest = json.loads(payload)
        if not isinstance(manifest, dict) or manifest.get('schema') != manifest_schema:
            raise PocketError('Unsupported manifest schema')
        if _version(path) != before:
            raise PocketError('Manifest changed while loading')
        return manifest
    except (OSError, ValueError) as error:
        raise PocketError(f'Cannot load sealed manifest: {error}') from error


def _validate_profile(profile):
    if not isinstance(profile, dict) or set(profile) - PROFILE_KEYS:
        raise PocketError('Profile must use the shared attributed musical fields')
    if not profile:
        return
    if profile.get('provenance') not in ('user', 'agent_hypothesis', 'measured'):
        raise PocketError('A nonempty profile needs user, agent_hypothesis or measured provenance')
    for key in ('energy', 'vocal_density'):
        if profile.get(key) is not None:
            _number(profile[key], key, 0, 1)
    if profile.get('bpm') is not None:
        _number(profile['bpm'], 'profile bpm', 20, 400)
    if profile.get('bpm_candidates') is not None:
        values = profile['bpm_candidates']
        if not isinstance(values, list) or len(values) > 30:
            raise PocketError('bpm_candidates must be a list of at most 30 numbers')
        for value in values:
            _number(value, 'bpm candidate', 20, 400)
    for key in ('tags', 'roles'):
        if profile.get(key) is not None:
            _strings(profile[key], key, maximum=50)
    for key, maximum in (('key', 100), ('notes', 4000)):
        if profile.get(key) is not None:
            _text(profile[key], key, maximum, empty=True)


def _prepare_tracks(tracks):
    if not isinstance(tracks, list) or not tracks or len(tracks) > 10000:
        raise PocketError('tracks must contain from 1 to 10000 record entries')
    prepared, seen = [], set()
    for input_track in tracks:
        if not isinstance(input_track, dict) or set(input_track) - TRACK_KEYS:
            raise PocketError('Track input contains unsupported fields; derived audio is not caller input')
        track = deepcopy(input_track)
        key = _text(track.get('track_id'), 'track_id', 200)
        if key in seen:
            raise PocketError(f'Duplicate track_id: {key}')
        seen.add(key)
        _text(track.get('title'), 'title', 500)
        _strings(track.get('artists'), 'artists', maximum=50)
        for name, maximum in (('spotify_uri', 1024), ('album', 500), ('version_note', 4000), ('local_path', 4096)):
            if track.get(name) is not None:
                _text(track[name], name, maximum)
        for name in ('explicit', 'available'):
            if track.get(name) is not None and not isinstance(track[name], bool):
                raise PocketError(f'{name} must be a boolean or null')
        if track.get('catalog_source') is not None and track['catalog_source'] not in ('spotify_ui', 'spotify_api', 'user_list', 'local_manifest'):
            raise PocketError('Unsupported catalog_source')
        if track.get('duration_seconds') is not None:
            _number(track['duration_seconds'], 'duration_seconds', .001, 86400)
        if track.get('profile') is not None:
            _validate_profile(track['profile'])
        if track.get('expected_audio_sha256') is not None:
            digest = track['expected_audio_sha256']
            if (not isinstance(digest, str) or len(digest) != 64
                    or any(c not in '0123456789abcdef' for c in digest)):
                raise PocketError('expected_audio_sha256 must be a lowercase SHA-256 digest')
            if not track.get('local_path'):
                raise PocketError('expected_audio_sha256 requires local_path')
        if track.get('local_path'):
            reference = Path(track['local_path']).expanduser().absolute()
            source = reference.resolve()
            before = _version(source)
            identity = identify_audio(source)
            if _version(source) != before:
                raise PocketError('Audio changed during record-bag identification')
            if track.get('expected_audio_sha256') not in (None, identity['sha256']):
                raise PocketError(f'Local recording identity mismatch: {key}')
            track['audio'] = {'status': 'identified', 'identity': identity, 'file_version': before,
                              'reference_path': str(reference)}
        if track.get('regions') is not None:
            if not isinstance(track['regions'], list) or len(track['regions']) > 100 or 'audio' not in track:
                raise PocketError('Source regions require identified local audio and at most 100 regions')
            for region in track['regions']:
                if not isinstance(region, dict) or set(region) - {'start_frame', 'frames', 'role', 'note'}:
                    raise PocketError('Unsupported source-region fields')
                start, frames = region.get('start_frame'), region.get('frames')
                if (isinstance(start, bool) or not isinstance(start, int) or start < 0
                        or isinstance(frames, bool) or not isinstance(frames, int) or frames <= 0
                        or start + frames > track['audio']['identity']['frames']):
                    raise PocketError('Source region must lie inside complete recording frames')
                _text(region.get('role'), 'region role', 300)
                if region.get('note') is not None:
                    _text(region['note'], 'region note', 4000, empty=True)
        prepared.append(track)
    return prepared


def _check_audio_versions(tracks):
    for track in tracks:
        if 'audio' in track:
            audio = track['audio']
            if _version(Path(audio['identity']['local_path'])) != audio['file_version']:
                raise PocketError(f'Stale local audio for {track["track_id"]}; create a new bag revision')
            if Path(audio['reference_path']).resolve() != Path(audio['identity']['local_path']):
                raise PocketError(f'Local reference retargeted for {track["track_id"]}; create a new bag revision')


def _bag_summary(tracks):
    return {'track_count': len(tracks), 'identified_local_audio': sum('audio' in t for t in tracks),
            'catalog_only': sum('audio' not in t for t in tracks),
            'unavailable': sum(t.get('available') is False for t in tracks),
            'with_attributed_profile': sum(bool(t.get('profile')) for t in tracks),
            'full_track_duration_known_count': sum(t.get('duration_seconds') is not None for t in tracks),
            'measured_musical_properties_added': 0}


def create_record_bag(tracks: list[BagTrackInput], output_dir: str, title: str) -> dict:
    """Preserve input catalogue/profile fields; derive only verified local identity."""
    return _create_bag(tracks, output_dir, title, None)


def _create_bag(tracks, output_dir, title, parent):
    _text(title, 'bag title', 200)
    prepared = _prepare_tracks(tracks)
    _check_audio_versions(prepared)
    manifest = {'schema': 'pocket.record-bag/v1', 'title': title,
                'created_at': datetime.now(UTC).isoformat(), 'parent': parent,
                'tracks': prepared, 'summary': _bag_summary(prepared),
                'profile_scope': 'Values and attribution are supplied annotations, not measurements performed by this tool.',
                'recording_identity': 'Only audio.identity binds local recording bytes; title and Spotify metadata do not.',
                'read_only_sources': True}
    handle = _write_manifest(manifest, output_dir, 'record-bag.json', 'pocket.record-bag-handle/v1')
    return {'handle': handle, 'title': title, 'summary': manifest['summary'], 'parent': parent}


def load_record_bag(handle: BagHandle) -> dict:
    """Verify a sealed bag and its identified local versions; never mutate it."""
    manifest = _load_manifest(handle, 'pocket.record-bag-handle/v1', 'pocket.record-bag/v1')
    _check_audio_versions(manifest['tracks'])
    return manifest


def revise_record_bag(handle: BagHandle, tracks: list[BagTrackInput], output_dir: str,
                      title: str | None = None) -> dict:
    parent = load_record_bag(handle)
    return _create_bag(tracks, output_dir, title if title is not None else parent['title'], deepcopy(handle))


def query_record_bag(handle: BagHandle, query: str = '', *, limit: int = 20, offset: int = 0) -> dict:
    _text(query, 'query', 256, empty=True)
    for number, label, lower, upper in ((limit, 'limit', 1, 100), (offset, 'offset', 0, 1000000)):
        if isinstance(number, bool) or not isinstance(number, int) or not lower <= number <= upper:
            raise PocketError(f'{label} must be an integer from {lower} to {upper}')
    bag = load_record_bag(handle)
    needle = query.casefold()
    matches = [t for t in bag['tracks'] if needle in ' '.join((t['track_id'], t['title'], *t['artists'],
               *((t.get('profile') or {}).get('tags') or []))).casefold()]
    cards = []
    for track in matches[offset:offset + limit]:
        profile = track.get('profile') or {}
        cards.append({k: deepcopy(v) for k, v in track.items() if k in
                      ('track_id', 'title', 'artists', 'spotify_uri', 'album', 'duration_seconds', 'available', 'explicit')})
        cards[-1].update(profile={k: deepcopy(v) for k, v in profile.items() if k != 'notes'},
                         profile_note_available=bool(profile.get('notes')),
                         audio_asset_id=track.get('audio', {}).get('identity', {}).get('asset_id'))
    return {'schema': 'pocket.record-bag-query/v1', 'bag_sha256': handle['sha256'], 'title': bag['title'],
            'summary': bag['summary'], 'tracks': cards, 'total_matches': len(matches), 'offset': offset,
            'returned': len(cards), 'next_offset': offset + len(cards) if offset + len(cards) < len(matches) else None,
            'truncated': offset + len(cards) < len(matches)}
