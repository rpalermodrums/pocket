# SPDX-License-Identifier: AGPL-3.0-only
"""Exact source evidence, public Peek composition, and correction boundaries."""
import hashlib
import importlib
import json
import wave

import numpy as np
import pytest

from pocket_music.artifact_store import canonical_bytes, read_bytes, read_record, request_status
from pocket_music.audio_hypotheses import (
    audio_hypotheses,
    audio_hypothesis_correct,
    audio_hypothesis_query,
    prepare_hypothesis_input,
)
from pocket_music.errors import PocketError
from pocket_music.peek import analyze_region

ATTRIBUTION = {'actor': 'Fixture author', 'actor_kind': 'agent',
               'statement': 'Inspect synthetic attacks; no listening verdict.', 'uncertainty': ['Synthetic fixture.']}
SETTINGS = {'bpm_hint': 120, 'beats_per_bar': 4}


def fixture(tmp_path, silent=False, rate=8000):
    path = tmp_path / 'private-name.wav'
    samples = np.zeros(rate * 4, dtype='<i2')
    if not silent:
        for index in range(rate // 2, len(samples), rate // 2):
            samples[index:index + 40] = np.linspace(20000, 0, 40).astype('<i2')
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(samples.tobytes())
    source = {'path': str(path), 'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
              'start_frame': 100, 'frames': len(samples) - 200, 'source_origin': 'user_recording'}
    return source


def run(tmp_path, source=None, request='hypotheses'):
    source = fixture(tmp_path) if source is None else source
    return audio_hypotheses(store_root=str(tmp_path / 'store'), request_id=request, source=source,
                            settings=SETTINGS, attribution=ATTRIBUTION)


def correction(record, key='fix', frame=200):
    return {'correction_id': key, 'supersedes': [record['annotations'][0]['annotation_id']],
            'annotation': {'kind': 'attack', 'source_frame': frame, 'strength_relative': None},
            'support': [{'kind': 'analysis_pointer', 'reference': '/analysis/region/start_frame'}],
            'uncertainty': ['Human review still required.']}


def test_exact_original_portable_peek_and_relocated_replay(tmp_path):
    source = fixture(tmp_path)
    store = tmp_path / 'store'
    result = run(tmp_path, source)
    handle = result['artifacts']['hypotheses']
    record = read_record(handle, store)
    assert read_bytes(record['original'], store) == (tmp_path / 'private-name.wav').read_bytes()
    with wave.open(str(store / record['original']['artifact_uri'])) as stream:
        assert stream.getnframes() == 32000
        assert stream.getframerate() == 8000
    evidence = read_record(record['analysis'], store)
    direct = analyze_region(source['path'], start_frame=100, frames=31800, **SETTINGS)
    direct['asset'].pop('local_path')
    direct['asset'].pop('filename')
    assert evidence['analysis'] == direct
    assert str(tmp_path) not in canonical_bytes(record).decode()
    assert 'private-name.wav' not in canonical_bytes(evidence).decode()
    assert all(row['annotation']['kind'] in ('attack', 'pulse_candidate', 'abstention') for row in record['annotations'])
    assert run(tmp_path, source) == result
    other = tmp_path / 'renamed.wav'
    other.write_bytes((tmp_path / 'private-name.wav').read_bytes())
    assert run(tmp_path, {**source, 'path': str(other)}, 'renamed')['artifacts'] == result['artifacts']


def test_silence_abstains_not_transcription(tmp_path):
    record = read_record(run(tmp_path, fixture(tmp_path, True))['artifacts']['hypotheses'], tmp_path / 'store')
    assert [row['annotation']['kind'] for row in record['annotations']] == ['abstention']


@pytest.mark.parametrize('field,value', [('start_frame', True), ('frames', False), ('frames', 0),
    ('frames', 2000001), ('start_frame', -1), ('start_frame', 40000),
    ('expected_sha256', 'a' * 64), ('expected_sha256', 'A' * 64), ('source_origin', 'spotify')])
def test_bad_source_refuses(tmp_path, field, value):
    source = fixture(tmp_path)
    source[field] = value
    with pytest.raises(PocketError):
        run(tmp_path, source)


@pytest.mark.parametrize('settings', [{'bpm_hint': True, 'beats_per_bar': 4},
    {'bpm_hint': float('nan'), 'beats_per_bar': 4}, {'bpm_hint': None, 'beats_per_bar': True},
    {'bpm_hint': 19, 'beats_per_bar': 4}, {'bpm_hint': 120, 'beats_per_bar': 13},
    {'bpm_hint': None, 'beats_per_bar': 4, 'extra': 1}])
def test_bad_settings_refuses(tmp_path, settings):
    with pytest.raises(PocketError):
        audio_hypotheses(store_root=str(tmp_path / 'store'), request_id='bad', source=fixture(tmp_path),
                         settings=settings, attribution=ATTRIBUTION)


def test_source_snapshot_no_analysis_and_change_race(tmp_path, monkeypatch):
    module = importlib.import_module('pocket_music.audio_hypotheses')
    source = fixture(tmp_path)
    monkeypatch.setattr(module, 'analyze_region', lambda *a, **k: pytest.fail('snapshot ran analysis'))
    prepared = prepare_hypothesis_input(source=source, settings=SETTINGS, attribution=ATTRIBUTION, store_root=str(tmp_path / 'store'))
    assert prepared['source']['path'] != source['path']
    assert read_bytes(prepared['source_handle'], tmp_path / 'store') == (tmp_path / 'private-name.wav').read_bytes()
    real = module.identify_audio
    def racing(path):
        result = real(path)
        with path.open('ab') as stream:
            stream.write(b'changed')
        return result
    monkeypatch.setattr(module, 'identify_audio', racing)
    with pytest.raises(PocketError, match='changed'):
        prepare_hypothesis_input(source=source, settings=SETTINGS, attribution=ATTRIBUTION, store_root=str(tmp_path / 'race'))


def test_correction_retains_parent_attribution_and_support(tmp_path):
    store = tmp_path / 'store'
    parent = run(tmp_path)['artifacts']['hypotheses']
    original = read_record(parent, store)
    change = correction(original)
    child = audio_hypothesis_correct(store_root=str(store), request_id='correct', parent=parent,
                                     expected_revision=parent['sha256'], corrections=[change], attribution=ATTRIBUTION)
    record = read_record(child['artifacts']['hypotheses'], store)
    assert record['parent'] == parent
    assert record['annotations'][0]['attribution'] == ATTRIBUTION
    assert read_record(parent, store) == original
    view = audio_hypothesis_query(store_root=str(store), hypotheses=child['artifacts']['hypotheses'], view='annotations')
    assert view['items'][:-1] == original['annotations']
    assert view['items'][-1]['annotation'] == change['annotation']


@pytest.mark.parametrize('mutate', [
    lambda c: c.update(extra=True),
    lambda c: c.update(supersedes=['unknown']),
    lambda c: c.update(supersedes=c['supersedes'] * 2),
    lambda c: c['annotation'].update(source_frame=True),
    lambda c: c['annotation'].update(source_frame=99),
    lambda c: c['annotation'].update(source_frame=31900),
    lambda c: c.update(support=[]),
    lambda c: c['support'][0].update(reference='/analysis/onsets/events/999'),
    lambda c: c['support'][0].update(reference='/original/sha256'),
    lambda c: c['support'][0].update(reference='/analysis/region/~2bad'),
    lambda c: c['support'][0].update(kind='annotation_id', reference='unknown'),
])
def test_invalid_corrections_refuse(tmp_path, mutate):
    store = tmp_path / 'store'
    parent = run(tmp_path)['artifacts']['hypotheses']
    item = correction(read_record(parent, store))
    mutate(item)
    with pytest.raises(PocketError):
        audio_hypothesis_correct(store_root=str(store), request_id='bad-correction', parent=parent,
                                 expected_revision=parent['sha256'], corrections=[item], attribution=ATTRIBUTION)


def test_pagination_revision_binding_budget_and_tamper(tmp_path):
    store = tmp_path / 'store'
    parent = run(tmp_path)['artifacts']['hypotheses']
    page = audio_hypothesis_query(store_root=str(store), hypotheses=parent, view='annotations', limit=1, max_bytes=4096)
    assert len(canonical_bytes(page)) <= 4096
    assert not page['complete'] and page['next_cursor']
    all_rows = page['items'][:]
    while page['next_cursor']:
        page = audio_hypothesis_query(store_root=str(store), hypotheses=parent, view='annotations', limit=1, cursor=page['next_cursor'])
        all_rows.extend(page['items'])
    assert all_rows == read_record(parent, store)['annotations']
    with pytest.raises(PocketError, match='cursor'):
        audio_hypothesis_query(store_root=str(store), hypotheses=parent, view='history', cursor='bad')
    original = read_record(parent, store)['original']
    (store / original['artifact_uri']).write_bytes(b'tampered')
    with pytest.raises(PocketError, match='integrity'):
        request_status(str(store), 'hypotheses')


def test_interruption_failed_journal_without_hypothesis(tmp_path, monkeypatch):
    module = importlib.import_module('pocket_music.audio_hypotheses')
    monkeypatch.setattr(module, 'analyze_region', lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path)
    journal = json.loads((tmp_path / 'store/requests/hypotheses/journal.json').read_text())
    assert journal['state'] == 'failed' and 'receipt' not in journal


@pytest.mark.parametrize('annotation', [
    {'kind': 'phrase_anchor', 'start_frame': 100, 'end_frame_exclusive': 31900, 'label': 'possible phrase'},
    {'kind': 'note_hypothesis', 'start_frame': 100, 'end_frame_exclusive': 200, 'midi_note': 60, 'cents': 2.5, 'tuning_ref': 'caller:custom'},
    {'kind': 'pulse_candidate', 'start_frame': 100, 'end_frame_exclusive': 31900, 'bpm': 120, 'source_lattice_origin_seconds': .1},
])
def test_authored_annotation_union_not_detector_transcription(tmp_path, annotation):
    store = tmp_path / 'store'
    parent = run(tmp_path)['artifacts']['hypotheses']
    original = read_record(parent, store)
    item = correction(original)
    item['annotation'] = annotation
    item['support'] = [{'kind': 'annotation_id', 'reference': original['annotations'][0]['annotation_id']}]
    child = audio_hypothesis_correct(store_root=str(store), request_id='authored', parent=parent,
                                     expected_revision=parent['sha256'], corrections=[item], attribution=ATTRIBUTION)['artifacts']['hypotheses']
    queried = audio_hypothesis_query(store_root=str(store), hypotheses=child, view='annotations')
    assert queried['items'][-1]['annotation'] == annotation
    assert queried['items'][-1]['attribution']['actor_kind'] == 'agent'


def test_revision_cap_stale_binding_and_duplicate_identity(tmp_path):
    store = tmp_path / 'store'
    parent = run(tmp_path, fixture(tmp_path, silent=True))['artifacts']['hypotheses']
    original = read_record(parent, store)
    item = correction(original)
    with pytest.raises(PocketError, match='Stale'):
        audio_hypothesis_correct(store_root=str(store), request_id='stale', parent=parent,
                                 expected_revision='a' * 64, corrections=[item], attribution=ATTRIBUTION)
    for index in range(32):
        item['correction_id'] = f'fix-{index}'
        parent = audio_hypothesis_correct(store_root=str(store), request_id=f'fix-{index}', parent=parent,
                                          expected_revision=parent['sha256'], corrections=[item], attribution=ATTRIBUTION)['artifacts']['hypotheses']
    history = audio_hypothesis_query(store_root=str(store), hypotheses=parent, view='history', limit=128)
    assert history['total'] == 33
    with pytest.raises(PocketError, match='32 revisions'):
        audio_hypothesis_correct(store_root=str(store), request_id='overflow', parent=parent,
                                 expected_revision=parent['sha256'], corrections=[item], attribution=ATTRIBUTION)


def test_response_budget_big_annotation_explicit_omission(tmp_path):
    store = tmp_path / 'store'
    parent = run(tmp_path, fixture(tmp_path, silent=True))['artifacts']['hypotheses']
    item = correction(read_record(parent, store))
    item['uncertainty'] = ['x' * 1024] * 32
    child = audio_hypothesis_correct(store_root=str(store), request_id='large', parent=parent,
                                     expected_revision=parent['sha256'], corrections=[item], attribution=ATTRIBUTION)['artifacts']['hypotheses']
    first = audio_hypothesis_query(store_root=str(store), hypotheses=child, view='annotations', limit=1, max_bytes=4096)
    next_page = audio_hypothesis_query(store_root=str(store), hypotheses=child, view='annotations', cursor=first['next_cursor'], max_bytes=4096)
    assert next_page['status'] == 'needs_input' and not next_page['complete']
    assert next_page['items'] == [] and next_page['omitted'] == 1
    assert len(canonical_bytes(next_page)) <= 4096
    full = audio_hypothesis_query(store_root=str(store), hypotheses=child, view='annotations', cursor=first['next_cursor'], max_bytes=65536)
    assert full['complete'] and len(full['items']) == 1


def test_content_addressed_but_malformed_record_refuses(tmp_path):
    from pocket_music.artifact_store import put_record
    store = tmp_path / 'store'
    parent = run(tmp_path)['artifacts']['hypotheses']
    record = read_record(parent, store)
    record['annotations'][0]['annotation']['source_frame'] = True
    malformed = put_record(record, store)
    with pytest.raises(PocketError, match='project retained detector evidence'):
        audio_hypothesis_query(store_root=str(store), hypotheses=malformed)


@pytest.mark.parametrize('handle', [{}, {'schema': 'pocket.audio-hypotheses/v1'}, [], 'bad'])
def test_bad_handle_is_domain_error(tmp_path, handle):
    with pytest.raises(PocketError):
        audio_hypothesis_query(store_root=str(tmp_path), hypotheses=handle)


def test_oversized_cursor_is_domain_error(tmp_path):
    from pocket_music.artifact_store import digest
    handle = run(tmp_path)['artifacts']['hypotheses']
    with pytest.raises(PocketError, match='cursor'):
        audio_hypothesis_query(store_root=str(tmp_path / 'store'), hypotheses=handle,
                               cursor=digest([handle, 'summary']) + ':' + '1' * 5000)


@pytest.mark.parametrize('channels,rate,frames', [(9, 8000, 8000), (1, 8000, 160001)])
def test_decoded_channel_duration_bounds(tmp_path, channels, rate, frames):
    path = tmp_path / 'bounded.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(bytes(frames * channels * 2))
    source = {'path': str(path), 'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
              'start_frame': 0, 'frames': frames, 'source_origin': 'user_recording'}
    with pytest.raises(PocketError, match='bounds'):
        run(tmp_path, source)


def test_malformed_float_audio_fails_without_hypotheses(tmp_path):
    import soundfile as sf
    path = tmp_path / 'nonfinite.wav'
    values = np.zeros(8000, dtype=np.float32)
    values[100] = np.nan
    sf.write(path, values, 8000, subtype='FLOAT')
    source = {'path': str(path), 'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
              'start_frame': 0, 'frames': 8000, 'source_origin': 'user_recording'}
    with pytest.raises(PocketError, match='nonfinite'):
        run(tmp_path, source)
    assert request_status(str(tmp_path / 'store'), 'hypotheses')['journal_state'] == 'failed'
