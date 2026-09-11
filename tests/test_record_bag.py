"""Public generated catalogue/media tests, with no private library fixtures."""
from __future__ import annotations

import hashlib
import json
import os

import numpy as np
import pytest
import soundfile as sf

from pocket_music.errors import PocketError
from pocket_music.record_bag import create_record_bag, load_record_bag, query_record_bag, revise_record_bag


def track(key='a', **extra):
    return {'track_id': key, 'title': 'Synthetic record ' + key, 'artists': ['Generated artist'], **extra}


def test_catalog_unknowns_profile_attribution_and_query(tmp_path):
    tracks = [track(), track('b', profile={'energy': .4, 'key': None, 'provenance': 'agent_hypothesis'},
                             available=False)]
    original = json.loads(json.dumps(tracks))
    created = create_record_bag(tracks, str(tmp_path / 'bag'), 'A test bag')
    bag = load_record_bag(created['handle'])
    assert bag['tracks'] == original == tracks
    assert 'profile' not in bag['tracks'][0]
    assert bag['summary']['measured_musical_properties_added'] == 0
    assert bag['summary']['unavailable'] == 1
    page = query_record_bag(created['handle'], 'generated', limit=1)
    assert page['total_matches'] == 2 and page['next_offset'] == 1
    assert len(query_record_bag(created['handle'], offset=1)['tracks']) == 1
    assert not query_record_bag(created['handle'], 'absent')['tracks']


@pytest.mark.parametrize('tracks', [
    [], [track(), track()], [track(profile={'energy': .7})],
    [track(profile={'energy': 2, 'provenance': 'measured'})],
    [track(profile={'bpm': True, 'provenance': 'user'})],
    [track(duration_seconds=float('nan'))], [track(audio={'invented': True})],
    [track(expected_audio_sha256='f' * 64)], [track(available='false')],
])
def test_invalid_inputs_do_not_publish(tmp_path, tracks):
    output = tmp_path / 'rejected'
    with pytest.raises(PocketError):
        create_record_bag(tracks, str(output), 'invalid')
    assert not output.exists()


def test_local_audio_identity_keeps_input_and_detects_replacement(tmp_path):
    audio = tmp_path / 'tone.wav'
    sf.write(audio, np.zeros((1000, 2)), 8000, subtype='FLOAT')
    original = audio.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    source = track(local_path=str(audio), expected_audio_sha256=digest,
                   profile={'notes': 'A user annotation', 'provenance': 'user'},
                   regions=[{'start_frame': 20, 'frames': 400, 'role': 'test excerpt'}])
    handle = create_record_bag([source], str(tmp_path / 'bag'), 'local')['handle']
    result = load_record_bag(handle)['tracks'][0]
    assert result['local_path'] == source['local_path']
    assert result['profile'] == source['profile']
    assert result['audio']['identity']['sha256'] == digest
    assert result['audio']['identity']['frames'] == 1000
    assert audio.read_bytes() == original
    stamp = audio.stat()
    with audio.open('r+b') as stream:
        stream.seek(-4, 2)
        stream.write(b'\1\0\0\0')
    os.utime(audio, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    with pytest.raises(PocketError, match='Stale local audio'):
        load_record_bag(handle)


def test_wrong_audio_hash_and_out_of_bounds_region_fail(tmp_path):
    audio = tmp_path / 'tone.wav'
    sf.write(audio, np.zeros((1000, 2)), 8000)
    for extra in ({'expected_audio_sha256': 'a' * 64},
                  {'regions': [{'start_frame': 900, 'frames': 101, 'role': 'too long'}]}):
        with pytest.raises(PocketError):
            create_record_bag([track(local_path=str(audio), **extra)], str(tmp_path / 'bad'), 'bad')
        assert not (tmp_path / 'bad').exists()


def test_seal_no_overwrite_and_immutable_revision(tmp_path):
    created = create_record_bag([track()], str(tmp_path / 'first'), 'first')
    parent = created['handle']
    before = load_record_bag(parent)
    child = revise_record_bag(parent, [track(), track('b')], str(tmp_path / 'second'))
    assert child['parent'] == parent
    assert load_record_bag(parent) == before
    assert load_record_bag(child['handle'])['summary']['track_count'] == 2
    with pytest.raises(PocketError, match='new immutable'):
        create_record_bag([track()], str(tmp_path / 'first'), 'cannot overwrite')
    path = tmp_path / 'second' / 'record-bag.json'
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(PocketError, match='identity or seal mismatch'):
        load_record_bag(child['handle'])


def test_relative_input_is_not_reinterpreted_under_new_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sf.write('tone.wav', np.zeros(1000), 8000)
    handle = create_record_bag([track(local_path='tone.wav')], str(tmp_path / 'bag'), 'relative')['handle']
    another = tmp_path / 'elsewhere'
    another.mkdir()
    monkeypatch.chdir(another)
    assert load_record_bag(handle)['tracks'][0]['local_path'] == 'tone.wav'


def test_stable_ids_and_bounded_bpm_candidates(tmp_path):
    with pytest.raises(PocketError, match='whitespace'):
        create_record_bag([{'track_id': ' padded ', 'title': 'a', 'artists': []}],
                          str(tmp_path / 'bad-id'), 'bad')
    with pytest.raises(PocketError, match='at most 8'):
        create_record_bag([{'track_id': 'a', 'title': 'a', 'artists': [],
                          'profile': {'provenance': 'user', 'bpm_candidates': list(range(120, 129))}}],
                          str(tmp_path / 'bad-profile'), 'bad')


def test_local_header_alone_counts_as_known_full_duration(tmp_path):
    audio = tmp_path / 'local.wav'
    sf.write(audio, np.zeros((8000, 1)), 8000, subtype='FLOAT')
    bag = create_record_bag([{'track_id': 'local', 'title': 'Generated', 'artists': [],
                             'local_path': str(audio)}], str(tmp_path / 'bag'), 'Local')
    assert bag['summary']['full_track_duration_known_count'] == 1
