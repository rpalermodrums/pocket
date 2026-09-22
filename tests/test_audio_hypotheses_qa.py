"""Independent PCM decoding and portable audio-evidence adversarial checks."""
import copy
import hashlib
import importlib
import struct
import wave
from pathlib import Path

import pytest

from pocket_music.artifact_store import canonical_bytes, digest, put_record, read_bytes, read_record
from pocket_music.audio_hypotheses import audio_hypotheses, audio_hypothesis_correct, audio_hypothesis_query
from pocket_music.errors import PocketError
from pocket_music.peek import analyze_region


def arguments(tmp_path, silent=False):
    rate, frames = 8000, 24000
    samples = [0] * frames
    if not silent:
        for start in range(2000, frames, 4000):
            for offset in range(32):
                samples[start + offset] = 24000 - 700 * offset
    path = tmp_path / 'independent-pcm.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, rate, frames, 'NONE', 'not compressed'))
        stream.writeframes(struct.pack('<' + 'h' * frames, *samples))
    return {'store_root': str(tmp_path / 'store'), 'request_id': 'analysis',
            'source': {'path': str(path), 'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                       'start_frame': 137, 'frames': 23000, 'source_origin': 'independently_acquired'},
            'settings': {'bpm_hint': None, 'beats_per_bar': 4},
            'attribution': {'actor': 'Independent QA', 'actor_kind': 'agent',
                            'statement': 'Synthetic PCM evidence; no human audition.', 'uncertainty': ['Not music.']}}


def initial(args):
    result = audio_hypotheses(**args)
    handle = result['artifacts']['hypotheses']
    return handle, read_record(handle, args['store_root'])


def corrections(args, handle, record, count=1):
    return {'store_root': args['store_root'], 'request_id': 'correct', 'parent': handle,
            'expected_revision': handle['sha256'], 'attribution': args['attribution'],
            'corrections': [{'correction_id': f'c{i}', 'supersedes': [record['annotations'][0]['annotation_id']],
                             'annotation': {'kind': 'note_hypothesis', 'start_frame': 200 + i,
                                            'end_frame_exclusive': 400 + i, 'midi_note': 60 + i % 12,
                                            'cents': 3.25, 'tuning_ref': 'declared hypothesis'},
                             'support': [{'kind': 'annotation_id', 'reference': record['annotations'][0]['annotation_id']}],
                             'uncertainty': ['Pitch authored by agent, not transcribed.']} for i in range(count)]}


def test_qa_independent_pcm_crop_and_peek_parity(tmp_path):
    args = arguments(tmp_path)
    handle, record = initial(args)
    raw = read_bytes(record['original'], args['store_root'])
    assert raw == Path(args['source']['path']).read_bytes()
    with wave.open(str(Path(args['store_root']) / record['original']['artifact_uri'])) as stream:
        assert (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getnframes()) == (1, 2, 8000, 24000)
        stream.setpos(137)
        decoded = struct.unpack('<' + 'h' * 23000, stream.readframes(23000))
    assert [i + 137 for i, value in enumerate(decoded) if value == 24000] == [2000, 6000, 10000, 14000, 18000, 22000]
    direct = analyze_region(args['source']['path'], start_frame=137, frames=23000, **args['settings'])
    direct['asset'].pop('local_path')
    direct['asset'].pop('filename')
    evidence = read_record(record['analysis'], args['store_root'])
    assert evidence['analysis'] == direct
    assert str(tmp_path).encode() not in canonical_bytes(evidence)
    assert b'independent-pcm.wav' not in canonical_bytes(evidence)
    for row in record['annotations']:
        if row['annotation']['kind'] == 'attack':
            index = int(row['support'][0]['reference'].rsplit('/', 1)[1])
            assert row['annotation']['source_frame'] == direct['onsets']['events'][index]['source_frame']
    assert audio_hypotheses(**args)['artifacts']['hypotheses'] == handle


def test_qa_silence_retains_abstention_and_corrections_are_not_detection(tmp_path):
    args = arguments(tmp_path, silent=True)
    handle, record = initial(args)
    assert [r['annotation']['kind'] for r in record['annotations']] == ['abstention']
    edit = corrections(args, handle, record, 2)
    result = audio_hypothesis_correct(**edit)
    query = audio_hypothesis_query(store_root=args['store_root'], hypotheses=result['artifacts']['hypotheses'], view='annotations')
    assert [r['annotation']['kind'] for r in query['items']] == ['abstention', 'note_hypothesis', 'note_hypothesis']
    assert query['items'][1]['attribution'] == args['attribution']
    assert read_record(handle, args['store_root']) == record
    assert audio_hypothesis_correct(**edit) == result


@pytest.mark.parametrize('pointer', ['/analysis/missing', '/analysis/onsets/events/00', '/analysis/onsets/events/-1',
                                    '/analysis/~2', '/original', 'analysis/region'])
def test_qa_support_pointer_refuses(tmp_path, pointer):
    args = arguments(tmp_path, True)
    handle, record = initial(args)
    edit = corrections(args, handle, record)
    edit['corrections'][0]['support'] = [{'kind': 'analysis_pointer', 'reference': pointer}]
    with pytest.raises(PocketError):
        audio_hypothesis_correct(**edit)


@pytest.mark.parametrize('change', ['low', 'high', 'empty', 'unknown', 'duplicate', 'stale', 'actor'])
def test_qa_correction_bounds_and_identity(tmp_path, change):
    args = arguments(tmp_path, True)
    handle, record = initial(args)
    edit = corrections(args, handle, record)
    item = edit['corrections'][0]
    if change == 'low':
        item['annotation']['start_frame'] = 136
    elif change == 'high':
        item['annotation']['end_frame_exclusive'] = 23138
    elif change == 'empty':
        item['annotation']['end_frame_exclusive'] = 200
    elif change == 'unknown':
        item['supersedes'] = ['audio:unknown']
    elif change == 'duplicate':
        item['supersedes'] *= 2
    elif change == 'stale':
        edit['expected_revision'] = '0' * 64
    else:
        edit['attribution'] = {**args['attribution'], 'actor_kind': 'algorithm'}
    with pytest.raises(PocketError):
        audio_hypothesis_correct(**edit)


def test_qa_pagination_bytebounds_revision_and_view_binding(tmp_path):
    args = arguments(tmp_path, True)
    handle, record = initial(args)
    result = audio_hypothesis_correct(**corrections(args, handle, record, 100))
    revised = result['artifacts']['hypotheses']
    query_args = {'store_root': args['store_root'], 'hypotheses': revised, 'view': 'annotations', 'limit': 128, 'max_bytes': 4096}
    rows, cursor = [], None
    while True:
        page = audio_hypothesis_query(**query_args, cursor=cursor)
        assert len(canonical_bytes(page)) <= 4096
        rows.extend(page['items'])
        if page['complete']:
            break
        assert page['next_cursor'] != cursor
        cursor = page['next_cursor']
    assert len(rows) == len({r['annotation_id'] for r in rows}) == 101
    for mismatch in [{'hypotheses': handle}, {'view': 'history'}]:
        with pytest.raises(PocketError):
            audio_hypothesis_query(**{**query_args, **mismatch}, cursor=cursor)


def test_qa_interruption_propagates_without_hypothesis_artifact(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    module = importlib.import_module('pocket_music.audio_hypotheses')
    def interrupt(*a, **k):
        raise KeyboardInterrupt('synthetic cancellation')
    monkeypatch.setattr(module, 'analyze_region', interrupt)
    with pytest.raises(KeyboardInterrupt):
        audio_hypotheses(**args)
    assert not any(b'pocket.audio-hypotheses/v1' in p.read_bytes() for p in Path(args['store_root']).rglob('*.json') if 'requests' not in p.parts)


def test_qa_resealed_false_algorithm_annotation_refuses(tmp_path):
    args = arguments(tmp_path, True)
    _, record = initial(args)
    altered = copy.deepcopy(record)
    row = altered['annotations'][0]
    row['annotation'] = {'kind': 'attack', 'source_frame': 999, 'strength_relative': 1}
    row['annotation_id'] = 'audio:' + digest([altered['analysis'], {k: v for k, v in row.items() if k != 'annotation_id'}])
    forged = put_record(altered, args['store_root'])
    with pytest.raises(PocketError):
        audio_hypothesis_query(store_root=args['store_root'], hypotheses=forged)


@pytest.mark.parametrize('mode', ['hash', 'crop', 'during_copy', 'artifact_tamper'])
def test_qa_source_mutation_and_integrity(tmp_path, monkeypatch, mode):
    args = arguments(tmp_path, True)
    module = importlib.import_module('pocket_music.audio_hypotheses')
    if mode == 'hash':
        args['source']['expected_sha256'] = '0' * 64
    elif mode == 'crop':
        args['source']['frames'] = 24000
    elif mode == 'during_copy':
        original = module.identify_audio
        def race(path):
            result = original(path)
            with path.open('ab') as stream:
                stream.write(b'race')
            return result
        monkeypatch.setattr(module, 'identify_audio', race)
    else:
        original = module.analyze_region
        def corrupt(path, **kwargs):
            result = original(path, **kwargs)
            Path(path).write_bytes(b'corrupt')
            return result
        monkeypatch.setattr(module, 'analyze_region', corrupt)
    with pytest.raises(PocketError):
        audio_hypotheses(**args)


@pytest.mark.parametrize('field,value', [('start_frame', 138), ('sample_rate', 16000), ('sha256', '0' * 64)])
def test_qa_resealed_source_mismatch_refuses(tmp_path, field, value):
    args = arguments(tmp_path, True)
    _, record = initial(args)
    record['source'][field] = value
    forged = put_record(record, args['store_root'])
    with pytest.raises(PocketError):
        audio_hypothesis_query(store_root=args['store_root'], hypotheses=forged)


def test_qa_raw_artifact_tamper_refuses_retry(tmp_path):
    args = arguments(tmp_path, True)
    _, record = initial(args)
    path = Path(args['store_root']) / record['original']['artifact_uri']
    path.write_bytes(path.read_bytes() + b'tampered')
    with pytest.raises(PocketError):
        audio_hypotheses(**args)


@pytest.mark.parametrize('value', [True, float('nan'), float('inf'), 10**1000, -1, 401],
                         ids=['boolean', 'nan', 'infinite', 'huge-integer', 'negative', 'above-range'])
def test_qa_bpm_numeric_bounds_refuse_as_domain_error(tmp_path, value):
    args = arguments(tmp_path, True)
    args['settings']['bpm_hint'] = value
    with pytest.raises(PocketError):
        audio_hypotheses(**args)


def test_qa_competing_corrections_history_and_supersession_retained(tmp_path):
    args = arguments(tmp_path, True)
    handle, record = initial(args)
    first = audio_hypothesis_correct(**corrections(args, handle, record, 2))['artifacts']['hypotheses']
    prior = read_record(first, args['store_root'])
    second_args = corrections(args, first, prior)
    second_args['request_id'] = 'second'
    second_args['corrections'][0]['correction_id'] = 'new-interpretation'
    second_args['corrections'][0]['annotation']['midi_note'] = 67
    second = audio_hypothesis_correct(**second_args)['artifacts']['hypotheses']
    page = audio_hypothesis_query(store_root=args['store_root'], hypotheses=second, view='annotations')
    assert len(page['items']) == 4
    assert page['items'][-1]['supersedes'] == [prior['annotations'][0]['annotation_id']]
    assert page['items'][-1]['annotation']['midi_note'] == 67
    assert page['items'][1]['annotation']['midi_note'] == 60
    history = audio_hypothesis_query(store_root=args['store_root'], hypotheses=second, view='history')
    assert [r['revision_index'] for r in history['items']] == [0, 1, 2]
    assert [r['hypotheses'] for r in history['items']] == [handle, first, second]


def test_qa_revision_limit_and_oversized_row_explicit_budget_refusal(tmp_path):
    args = arguments(tmp_path, True)
    handle, record = initial(args)
    edit = corrections(args, handle, record)
    edit['corrections'][0]['uncertainty'] = ['x' * 1024] * 32
    large = audio_hypothesis_correct(**edit)['artifacts']['hypotheses']
    query_args = {'store_root': args['store_root'], 'hypotheses': large, 'view': 'annotations', 'max_bytes': 4096}
    first = audio_hypothesis_query(**query_args)
    blocked = audio_hypothesis_query(**query_args, cursor=first['next_cursor'])
    assert blocked['status'] == 'needs_input' and blocked['items'] == []
    assert blocked['next_cursor'] == first['next_cursor']
    assert len(canonical_bytes(blocked)) <= 4096
    resumed = audio_hypothesis_query(**{**query_args, 'max_bytes': 65536}, cursor=blocked['next_cursor'])
    assert len(resumed['items']) == 1 and resumed['complete']
    for index in range(1, 33):
        edit = corrections(args, handle, record)
        edit['request_id'] = f'revision-{index}'
        edit['corrections'][0]['correction_id'] = f'revision-{index}'
        handle = audio_hypothesis_correct(**edit)['artifacts']['hypotheses']
    edit = corrections(args, handle, record)
    edit['request_id'] = 'revision-33'
    edit['corrections'][0]['correction_id'] = 'revision-33'
    with pytest.raises(PocketError, match='32 revisions'):
        audio_hypothesis_correct(**edit)


@pytest.mark.parametrize('field', ['provenance', 'onsets', 'rhythm'])
def test_qa_malformed_retained_detector_record_domain_error(tmp_path, field):
    args = arguments(tmp_path, True)
    _, record = initial(args)
    evidence = read_record(record['analysis'], args['store_root'])
    del evidence['analysis'][field]
    record['analysis'] = put_record(evidence, args['store_root'])
    forged = put_record(record, args['store_root'])
    with pytest.raises(PocketError):
        audio_hypothesis_query(store_root=args['store_root'], hypotheses=forged)
