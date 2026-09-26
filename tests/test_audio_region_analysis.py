# SPDX-License-Identifier: AGPL-3.0-only
"""Public region composition and exact original coordinates, without inferred musical truth."""
import builtins
import hashlib
import importlib
import io
import struct
import wave
from pathlib import Path

import pytest
from test_audio_regions import source, wav

from pocket_music.artifact_store import canonical_bytes, put_record, read_bytes, read_record
from pocket_music.audio_hypotheses import audio_hypotheses, audio_hypothesis_correct, audio_hypothesis_query
from pocket_music.audio_region_analysis import (
    audio_region_hypotheses,
    audio_region_query,
    load_audio_region_hypotheses,
)
from pocket_music.audio_regions import audio_region_capture, load_audio_region
from pocket_music.errors import PocketError

ACTOR = {'actor': 'Fixture author', 'actor_kind': 'agent', 'statement': 'Synthetic region evidence', 'uncertainty': []}
ANALYSIS = {'kind': 'peek', 'settings': {'bpm_hint': None, 'beats_per_bar': 4}}


def args(tmp_path):
    samples = bytearray(16100 * 2)
    for index in range(4100, 16100, 4000):
        samples[index*2:index*2+2] = struct.pack('<h', 25000)
    return source(tmp_path, wav(bytes(samples)), start=100, frames=16000)


def run(tmp_path, region=None, analysis=None, request='analysis'):
    return audio_region_hypotheses(store_root=str(tmp_path / 'store'), request_id=request,
        region=region or {'kind': 'inline', 'source': args(tmp_path)}, analysis=analysis or ANALYSIS, attribution=ACTOR)


def test_inline_captured_direct_public_equivalence(tmp_path):
    input_source = args(tmp_path)
    store = str(tmp_path / 'store')
    region = audio_region_capture(store_root=store, request_id='capture', source=input_source)['artifacts']['region']
    inline = run(tmp_path, {'kind': 'inline', 'source': input_source})
    composed = run(tmp_path, {'kind': 'captured', 'region': region}, request='composed')
    assert inline['artifacts'] == composed['artifacts']
    record = load_audio_region_hypotheses(inline['artifacts']['hypotheses'], store)
    captured = load_audio_region(region, store)
    local = audio_hypotheses(store_root=store, request_id='direct',
        source={'path': str(Path(store) / captured['crop']['artifact_uri']), 'expected_sha256': captured['crop']['sha256'],
                'start_frame': 0, 'frames': 16000, 'source_origin': input_source['source_origin']},
        settings=ANALYSIS['settings'], attribution=ACTOR)
    assert local['artifacts']['hypotheses'] == record['local_hypotheses']
    rows = audio_region_query(store_root=store, hypotheses=inline['artifacts']['hypotheses'], view='annotations')['items']
    local_rows = audio_hypothesis_query(store_root=store, hypotheses=record['local_hypotheses'], view='annotations')['items']
    assert [row['local'] for row in rows] == local_rows
    for row in rows:
        annotation = row['local']['annotation']
        projection = row['original_projection']
        assert projection['original_sha256'] == input_source['expected_sha256']
        if annotation['kind'] == 'attack':
            assert projection['source_frame'] == annotation['source_frame'] + 100
        else:
            assert projection['start_frame'] == annotation['start_frame'] + 100
    assert run(tmp_path, {'kind': 'inline', 'source': input_source}) == inline


@pytest.mark.parametrize('mutation', [
    lambda r: r['coordinate_mapping'].update(original_start_frame=101),
    lambda r: r.update(analysis_kind='learned_pulse'),
    lambda r: r['request_attribution'].update(actor='different'),
    lambda r: r.update(extra=True),
])
def test_resealed_wrapper_semantics_refuse(tmp_path, mutation):
    result = run(tmp_path)
    record = load_audio_region_hypotheses(result['artifacts']['hypotheses'], tmp_path / 'store')
    mutation(record)
    handle = put_record(record, tmp_path / 'store')
    with pytest.raises(PocketError):
        audio_region_query(store_root=str(tmp_path / 'store'), hypotheses=handle)


def test_other_valid_crop_cannot_replace_binding(tmp_path):
    source_input = args(tmp_path)
    first = run(tmp_path, {'kind': 'inline', 'source': source_input})
    other = run(tmp_path, {'kind': 'inline', 'source': {**source_input, 'start_frame': 99}}, request='other')
    record = read_record(first['artifacts']['hypotheses'], tmp_path / 'store')
    record['local_hypotheses'] = read_record(other['artifacts']['hypotheses'], tmp_path / 'store')['local_hypotheses']
    with pytest.raises(PocketError):
        load_audio_region_hypotheses(put_record(record, tmp_path / 'store'), tmp_path / 'store')


def test_corrected_local_rows_and_float_lattice_offset_remain_separate(tmp_path):
    result = run(tmp_path)
    store = str(tmp_path / 'store')
    record = read_record(result['artifacts']['hypotheses'], store)
    local = record['local_hypotheses']
    correction = {'correction_id': 'pulse', 'supersedes': [],
        'annotation': {'kind': 'pulse_candidate', 'start_frame': 0, 'end_frame_exclusive': 16000,
                       'bpm': 120, 'source_lattice_origin_seconds': .1},
        'support': [{'kind': 'analysis_pointer', 'reference': '/analysis/region/start_frame'}], 'uncertainty': []}
    child = audio_hypothesis_correct(store_root=store, request_id='local-correct', parent=local,
        expected_revision=local['sha256'], corrections=[correction], attribution=ACTOR)['artifacts']['hypotheses']
    record['local_hypotheses'] = child
    wrapper = put_record(record, store)
    rows = audio_region_query(store_root=store, hypotheses=wrapper, view='annotations')['items']
    assert rows[-1]['local']['annotation'] == correction['annotation']
    projected = rows[-1]['original_projection']
    assert projected['start_frame'] == 100 and projected['end_frame_exclusive'] == 16100
    assert projected['lattice_origin'] == {'local_estimate_seconds': .1, 'original_offset_seconds_q': {'n': 1, 'd': 80}}
    assert audio_region_query(store_root=store, hypotheses=wrapper)['items'][0]['local_revision_index'] == 1


def test_pagination_revision_binding_and_no_optional_runtime_for_query(tmp_path, monkeypatch):
    result = run(tmp_path)
    handle = result['artifacts']['hypotheses']
    store = str(tmp_path / 'store')
    first = audio_region_query(store_root=store, hypotheses=handle, view='annotations', limit=1, max_bytes=4096)
    assert len(canonical_bytes(first)) <= 4096
    if first['next_cursor']:
        with pytest.raises(PocketError):
            audio_region_query(store_root=store, hypotheses=handle, view='summary', cursor=first['next_cursor'])
    Path(tmp_path / 'source.wav').unlink()
    real_import = builtins.__import__
    def guarded(name, *a, **k):
        if name.split('.')[0] in {'torch', 'torchaudio', 'beat_this', 'soxr'}:
            raise AssertionError('Retained query imported optional runtime')
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, '__import__', guarded)
    module = importlib.import_module('pocket_music.audio_hypotheses')
    monkeypatch.setattr(module, 'analyze_region', lambda *a, **k: pytest.fail('query reran Peek'))
    assert audio_region_query(store_root=store, hypotheses=handle)['status'] == 'ok'
    with pytest.raises(PocketError):
        audio_hypothesis_query(store_root=store, hypotheses=handle)


@pytest.mark.parametrize('region,analysis', [
    ({'kind': 'invented'}, ANALYSIS),
    ({'kind': 'captured', 'region': {}, 'extra': True}, ANALYSIS),
    ({'kind': 'captured', 'region': {}}, {'kind': 'peek', 'settings': {}, 'extra': True}),
    ({'kind': 'captured', 'region': {}}, {'kind': 'learned_pulse', 'settings': {}}),
])
def test_strict_union_fields(tmp_path, region, analysis):
    with pytest.raises(PocketError):
        run(tmp_path, region, analysis)


def test_learned_rational_projection_model_binding_and_no_query_inference(tmp_path, monkeypatch):
    from test_audio_models import PulseModelTests, rehash

    from pocket_music.audio_pulse_hypotheses import SETTINGS
    fixture = PulseModelTests()
    fixture.setUp()
    def runner(*a, **k):
        result, binding = fixture.runner(*a, **k)
        if result['analysis'] is not None:
            raw = result['analysis']
            raw['beat'][20:22] = [1., 1.]
            raw['beat'][-1] = 2.
            raw['vendor_beats_seconds'] = [.41, 2.]
            rehash(raw)
        return result, binding
    monkeypatch.setattr('pocket_music.audio_pulse_hypotheses._runner', runner)
    try:
        result = run(tmp_path, {'kind': 'inline', 'source': fixture.source},
                     {'kind': 'learned_pulse', 'model': fixture.model, 'settings': SETTINGS})
        record = read_record(result['artifacts']['hypotheses'], tmp_path / 'store')
        local = read_record(record['local_hypotheses'], tmp_path / 'store')
        assert local['annotations'][0]['annotation']['model_frame_q'] == {'n': 41, 'd': 2}
        raw = read_record(local['analysis'], tmp_path / 'store')
        assert len(raw['analysis']['projection']['excluded']) == 1
        monkeypatch.setattr('pocket_music.audio_pulse_hypotheses._runner', lambda *a, **k: pytest.fail('query inference'))
        rows = audio_region_query(store_root=str(tmp_path / 'store'), hypotheses=result['artifacts']['hypotheses'], view='annotations')['items']
        assert rows[0]['original_projection']['source_frame_q'] == {'n': 3417, 'd': 1}
        assert rows[0]['local'] == local['annotations'][0]
    finally:
        fixture.doCleanups()


def test_long_source_capture_to_actual_peek_and_independent_crop_decode(tmp_path):
    rate, channels, frames = 16000, 2, 16000 * 4201
    size = frames * channels * 2
    path = tmp_path / 'long.wav'
    header = wav(b'', channels=channels, rate=rate)[:40] + struct.pack('<I', size)
    header = header[:4] + struct.pack('<I', size + 36) + header[8:]
    with path.open('wb') as stream:
        stream.write(header)
        stream.truncate(size+44)
    with path.open('rb') as stream:
        expected = hashlib.file_digest(stream, 'sha256').hexdigest()
    input_source = {'path': str(path), 'expected_sha256': expected, 'start_frame': frames-32000,
                    'frames': 32000, 'source_origin': 'user_recording'}
    result = run(tmp_path, {'kind': 'inline', 'source': input_source})
    wrapper = load_audio_region_hypotheses(result['artifacts']['hypotheses'], tmp_path / 'store')
    region = load_audio_region(wrapper['region'], tmp_path / 'store')
    with wave.open(io.BytesIO(read_bytes(region['crop'], tmp_path / 'store')), 'rb') as reader:
        assert reader.getnframes() == 32000 and reader.readframes(32000) == b'\0' * 128000
    rows = audio_region_query(store_root=str(tmp_path / 'store'), hypotheses=result['artifacts']['hypotheses'], view='annotations')['items']
    assert rows[-1]['original_projection']['start_frame'] == frames-32000
    assert rows[-1]['original_projection']['end_frame_exclusive'] == frames
    assert path.stat().st_size > 256 * 1024**2


def test_peek_inline_analysis_without_optional_runtime_imports(tmp_path, monkeypatch):
    real_import = builtins.__import__
    def guarded(name, *a, **k):
        if name.split('.')[0] in {'torch', 'torchaudio', 'beat_this', 'soxr'}:
            raise AssertionError('Peek imported optional model environment')
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, '__import__', guarded)
    assert run(tmp_path)['status'] == 'ok'


@pytest.mark.parametrize('failure', [RuntimeError('analysis failed'), KeyboardInterrupt('interrupted')])
def test_partial_public_composition_does_not_publish_wrapper(tmp_path, monkeypatch, failure):
    module = importlib.import_module('pocket_music.audio_hypotheses')
    def fail(**kwargs):
        raise failure
    monkeypatch.setattr(module, 'audio_hypotheses', fail)
    with pytest.raises(type(failure)):
        run(tmp_path)
    records = list((tmp_path / 'store/artifacts').glob('*/record.json'))
    assert records  # Captured source evidence remains inspectable.
    assert all(b'pocket.audio-region-hypotheses/v1' not in item.read_bytes() for item in records)


def test_inline_replay_refuses_changed_external_source(tmp_path):
    source_input = args(tmp_path)
    region = {'kind': 'inline', 'source': source_input}
    result = run(tmp_path, region)
    Path(source_input['path']).write_bytes(b'changed external original')
    with pytest.raises(PocketError):
        run(tmp_path, region)
    assert audio_region_query(store_root=str(tmp_path / 'store'), hypotheses=result['artifacts']['hypotheses'])['status'] == 'ok'


def test_resealed_wrong_source_origin_refuses(tmp_path):
    result = run(tmp_path)
    store = tmp_path / 'store'
    wrapper = read_record(result['artifacts']['hypotheses'], store)
    initial = read_record(wrapper['local_hypotheses'], store)
    initial['source']['source_origin'] = 'user_recording'
    wrapper['local_hypotheses'] = put_record(initial, store)
    with pytest.raises(PocketError, match='origin'):
        load_audio_region_hypotheses(put_record(wrapper, store), store)


@pytest.mark.parametrize('kwargs', [{'limit': True}, {'limit': 129}, {'max_bytes': 4095},
    {'max_bytes': 65537}, {'view': 'history'}, {'cursor': 'stale'}])
def test_query_bounds(tmp_path, kwargs):
    result = run(tmp_path)
    with pytest.raises(PocketError):
        audio_region_query(store_root=str(tmp_path / 'store'), hypotheses=result['artifacts']['hypotheses'], **kwargs)
