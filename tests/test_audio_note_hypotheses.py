# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic protocol fixtures; mocks establish no model or musical qualification."""
import copy
import hashlib
import io
import wave

import numpy as np
import pytest

from pocket_music import audio_note_hypotheses as notes
from pocket_music.artifact_store import digest, put_record, read_record
from pocket_music.errors import PocketError


@pytest.fixture
def note_fixture(tmp_path, monkeypatch):
    """Faithful bounded binary runner fixture for base-only integration tests."""
    weight = b'synthetic-protocol-model-not-real-onnx'
    monkeypatch.setattr(notes, 'KNOWN_WEIGHT', hashlib.sha256(weight).hexdigest())
    declaration = {'adapter': 'basic_pitch_onnx_cpu_v1', 'executable': {'path': '/synthetic/python', 'sha256': '1' * 64},
                   'weights': {'path': '/synthetic/model.onnx', 'sha256': notes.KNOWN_WEIGHT}, 'expected_profile': None}
    binding = {'executable': {'supplied_path': '/synthetic/python', 'resolved_path': '/synthetic/python', 'stamp': [1, 2, 3, 4, 5], 'sha256': '1' * 64},
               'weights': {'supplied_path': '/synthetic/model.onnx', 'resolved_path': '/synthetic/model.onnx', 'stamp': [1, 2, len(weight), 4, 5], 'sha256': notes.KNOWN_WEIGHT}}
    monkeypatch.setattr(notes, '_declaration', lambda value: (weight, copy.deepcopy(binding)))
    calls = []
    def runner(declaration, qualification, weights_path, source=None):
        calls.append({'qualification': qualification, 'source': source is not None})
        binaries = {}
        def descriptor(name, values):
            data = values.astype('<f4').tobytes(); binaries[name] = data
            return {'file': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'shape': list(values.shape), 'dtype': 'float32le'}
        def arrays(prefix, count, sounding):
            n = len(range(0, count + 3840, 36164)); result = {}
            for repeat in (0, 1):
                for name, bins in [('note', 88), ('onset', 88), ('contour', 264)]:
                    values = np.zeros((n, 172, bins), dtype=np.float32)
                    if sounding and name == 'note': values[0, 35:65, 48] = .5
                    if sounding and name == 'onset': values[0, 35, 48] = .75
                    result[f'{repeat}:{name}'] = descriptor(f'{prefix}-{repeat}-{name}.f32', values)
            return {'resampled_frames': count, 'arrays': result, 'two_repeats_sample_exact': True}
        proof = None
        if qualification != 'none':
            cases = {}
            for name in ('silence', 'tone'):
                audio = np.zeros(44100, dtype=np.float32)
                if name == 'tone':
                    t = np.arange(44100, dtype=np.float64) / 22050
                    audio = (.5 * np.sin(2 * np.pi * 440 * t) * ((t >= .25) & (t < 1.75))).astype(np.float32)
                cases[name] = {'input_sha256': hashlib.sha256(audio.tobytes()).hexdigest(), **arrays('qualification-' + name, 44100, name == 'tone')}
            proof = {'profile': 'synthetic_onnx_cpu_v1', 'cases': cases}
        analysis = None
        if source is not None:
            mono, rate = source; assert rate == 22050
            values = np.frombuffer(mono, dtype='<f4')
            analysis = {**arrays('source', len(values), True), 'mono_sha256': hashlib.sha256(mono).hexdigest(),
                        'resampled': descriptor('resampled.f32', values)}
        file = {'sha256': '1' * 64, 'bytes': 3}
        profile = {'adapter': 'basic_pitch_onnx_cpu_v1', 'python': 'protocol-only-not-executed', 'platform': 'test', 'machine': 'test', 'prefix': '/synthetic', 'base_prefix': '/synthetic', 'launch_executable': '/synthetic/python', 'executable': file, 'runner': file, 'fingerprint_helper': file, 'stdlib': {'test.py': file}, 'scope': 'Listed distribution files and Python stdlib; excludes unenumerated OS shared libraries.', 'packages': {name: {'version': {'onnxruntime': '1.26.0', 'soxr': '1.1.0'}.get(name, 'test'), 'files': {'test.py': file}} for name in ('onnxruntime', 'soxr', 'numpy', 'packaging', 'protobuf', 'flatbuffers')}}
        if declaration['expected_profile'] is not None and declaration['expected_profile'] != digest(profile): raise PocketError('Profile mismatch')
        return {'profile': profile, 'profile_sha256': digest(profile), 'qualification': proof, 'analysis': analysis}, copy.deepcopy(binding), binaries
    monkeypatch.setattr(notes, '_runner', runner)
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(22050); stream.writeframes(b'\0' * 88200)
    path = tmp_path / 'source.wav'; path.write_bytes(buffer.getvalue())
    source = {'path': str(path), 'expected_sha256': hashlib.sha256(buffer.getvalue()).hexdigest(), 'start_frame': 0,
              'frames': 44100, 'source_origin': 'independently_acquired'}
    actor = {'actor': 'Synthetic protocol test', 'actor_kind': 'agent', 'statement': 'Not actual listening', 'uncertainty': []}
    return {'store_root': str(tmp_path / 'store'), 'source': source, 'model': {'kind': 'inline', 'declaration': declaration, 'qualification': 'synthetic_onnx_cpu_v1'},
            'settings': copy.deepcopy(notes.SETTINGS), 'attribution': actor, 'calls': calls, 'runner': runner}


def args(fixture): return {key: value for key, value in fixture.items() if key not in ('calls', 'runner')}


def test_direct_inspected_identity_and_retained_replay(note_fixture):
    f = note_fixture
    inspected = notes.audio_note_model_inspect(store_root=f['store_root'], request_id='inspect', declaration=f['model']['declaration'], qualification='synthetic_onnx_cpu_v1')
    direct = notes.audio_note_hypotheses(request_id='direct', **args(f))
    composed = notes.audio_note_hypotheses(request_id='composed', **{**args(f), 'model': {'kind': 'inspected', 'model': inspected['artifacts']['model']}})
    assert direct['artifacts']['hypotheses'] == composed['artifacts']['hypotheses']
    record = notes.load_note_hypotheses(direct['artifacts']['hypotheses'], f['store_root'])
    assert record['annotation_count'] == 1
    assert f['calls'][-1] == {'qualification': 'none', 'source': True}


@pytest.mark.parametrize('field,value', [('settings', {}), ('model', None), ('attribution', {})])
def test_invalid_input(note_fixture, field, value):
    with pytest.raises(PocketError): notes.audio_note_hypotheses(request_id='bad', **{**args(note_fixture), field: value})


def test_resealed_ledger_refuses(note_fixture):
    f = note_fixture; result = notes.audio_note_hypotheses(request_id='direct', **args(f))
    record = read_record(result['artifacts']['hypotheses'], f['store_root'])
    evidence = read_record(record['analysis'], f['store_root']); evidence['analysis']['projection']['events'] = []
    record['analysis'] = put_record(evidence, f['store_root']); record['annotations'] = []; record['annotation_count'] = 0
    with pytest.raises(PocketError): notes.load_note_hypotheses(put_record(record, f['store_root']), f['store_root'])


@pytest.mark.parametrize('field,value', [('qualified', False), ('qualification_scope', 'all audio qualified'), ('binding', {}), ('declaration', {}), ('profile_sha256', '0' * 64)])
def test_resealed_model_refuses(note_fixture, field, value):
    f = note_fixture
    result = notes.audio_note_model_inspect(store_root=f['store_root'], request_id='inspect', declaration=f['model']['declaration'], qualification='synthetic_onnx_cpu_v1')
    model = read_record(result['artifacts']['model'], f['store_root']); model[field] = value
    with pytest.raises(PocketError): notes.load_audio_note_model(put_record(model, f['store_root']), f['store_root'])


def test_owned_reader_refuses_fifo_symlink_and_size(tmp_path):
    import os
    fifo = tmp_path / 'fifo'; os.mkfifo(fifo)
    with pytest.raises(PocketError): notes._owned_bytes(fifo, 100)
    data = tmp_path / 'data'; data.write_bytes(b'abc')
    link = tmp_path / 'link'; link.symlink_to(data)
    with pytest.raises(PocketError): notes._owned_bytes(link, 100)
    with pytest.raises(PocketError): notes._owned_bytes(data, 2)
    assert notes._owned_bytes(data, 3) == b'abc'


def test_replay_rechecks_profile_without_inference(note_fixture):
    f = note_fixture
    first = notes.audio_note_hypotheses(request_id='same', **args(f))
    second = notes.audio_note_hypotheses(request_id='same', **args(f))
    assert first == second
    assert f['calls'][-1] == {'qualification': 'none', 'source': False}


def test_json_depth_bound():
    value = {}; parent = value
    for _ in range(34): parent['child'] = {}; parent = parent['child']
    with pytest.raises(PocketError): notes._structure(value)


def test_shared_execution_context_cancellation():
    def cancel(): raise PocketError('Synthetic cancellation')
    with notes.model_execution_context(cancellation_check=cancel), pytest.raises(PocketError, match='cancellation'):
        notes._cancel()


def test_new_base_provenance_and_exact_legacy_roundtrip(note_fixture):
    f = note_fixture; result = notes.audio_note_hypotheses(request_id='new', **args(f))
    record = read_record(result['artifacts']['hypotheses'], f['store_root'])
    evidence = read_record(record['analysis'], f['store_root'])
    assert evidence['projection_provenance'] == notes._projection_provenance(notes.SETTINGS['decoder'])
    del evidence['projection_provenance']
    legacy_evidence = put_record(evidence, f['store_root'])
    record['analysis'] = legacy_evidence
    record['annotations'] = notes._annotations(legacy_evidence, evidence['analysis']['projection'])
    legacy = put_record(record, f['store_root'])
    assert notes.load_note_hypotheses(legacy, f['store_root']) == record
    assert read_record(legacy_evidence, f['store_root']) == evidence


@pytest.mark.parametrize('field,value', [('schema', 'unknown'), ('profile', 'other'),
    ('implementation_sha256', '0' * 64), ('vendor_source_sha256', '0' * 64),
    ('numpy_version', None), ('scipy_version', ''), ('replay_semantics', 'cross-version exact'),
    ('unknown', 'extra')])
def test_provenance_closed_profile_refuses(note_fixture, field, value):
    f = note_fixture; result = notes.audio_note_hypotheses(request_id='new', **args(f))
    record = read_record(result['artifacts']['hypotheses'], f['store_root'])
    evidence = read_record(record['analysis'], f['store_root'])
    evidence['projection_provenance'][field] = value
    record['analysis'] = put_record(evidence, f['store_root'])
    with pytest.raises(PocketError): notes.load_note_hypotheses(put_record(record, f['store_root']), f['store_root'])


def test_provenance_historical_versions_need_no_old_environment(note_fixture):
    f = note_fixture; result = notes.audio_note_hypotheses(request_id='new', **args(f))
    record = read_record(result['artifacts']['hypotheses'], f['store_root'])
    evidence = read_record(record['analysis'], f['store_root'])
    evidence['projection_provenance']['numpy_version'] = '1.26.0'
    evidence['projection_provenance']['scipy_version'] = '1.12.0'
    record['analysis'] = put_record(evidence, f['store_root'])
    record['annotations'] = notes._annotations(record['analysis'], evidence['analysis']['projection'])
    assert notes.load_note_hypotheses(put_record(record, f['store_root']), f['store_root']) == record


V1, V2 = 'basic_pitch_0_4_0_false_false_v1', 'basic_pitch_0_4_0_false_false_v2'


def made_with(fixture, decoder, request_id):
    settings = copy.deepcopy(notes.DECODERS[decoder]['module'].SETTINGS)
    result = notes.audio_note_hypotheses(request_id=request_id, **{**args(fixture), 'settings': settings})
    return result, read_record(result['artifacts']['hypotheses'], fixture['store_root'])


def test_each_decoder_records_and_replays_under_its_own_profile(note_fixture):
    f = note_fixture
    first, v1_record = made_with(f, V1, 'v1')
    second, v2_record = made_with(f, V2, 'v2')
    assert first['artifacts']['hypotheses'] != second['artifacts']['hypotheses']
    for record, decoder, frames in ((v1_record, V1, 172), (v2_record, V2, 173)):
        evidence = read_record(record['analysis'], f['store_root'])
        assert record['settings']['decoder'] == evidence['analysis']['projection']['decoder'] == decoder
        assert evidence['transform'] == notes.DECODERS[decoder]['module'].TRANSFORM
        assert evidence['projection_provenance'] == notes._projection_provenance(decoder)
        assert evidence['projection_provenance']['implementation_sha256'] == notes.DECODERS[decoder]['sha256']
        # A 2 s region: basic-pitch 0.4.0 keeps 172 frames, upstream e989e40 keeps 173.
        assert evidence['analysis']['projection']['frame_count'] == frames
    assert notes.load_note_hypotheses(second['artifacts']['hypotheses'], f['store_root']) == v2_record
    assert made_with(f, V2, 'v2')[0] == second


@pytest.mark.parametrize('made,claimed', [(V2, V1), (V1, V2)])
@pytest.mark.parametrize('relabel,refusal', [
    (('settings',), 'Unknown projection provenance profile'),
    (('settings', 'provenance'), 'transform binding mismatch'),
    (('settings', 'provenance', 'transform'), 'differ from complete retained tensors')])
def test_a_ledger_cannot_be_relabeled_as_the_other_decoder(note_fixture, made, claimed, relabel, refusal):
    f = note_fixture
    _, record = made_with(f, made, 'made')
    evidence = read_record(record['analysis'], f['store_root'])
    other = notes.DECODERS[claimed]['module']
    if 'settings' in relabel: record['settings'] = copy.deepcopy(other.SETTINGS)
    if 'provenance' in relabel: evidence['projection_provenance'] = notes._projection_provenance(claimed)
    if 'transform' in relabel: evidence['transform'] = copy.deepcopy(other.TRANSFORM)
    record['analysis'] = put_record(evidence, f['store_root'])
    record['annotations'] = notes._annotations(record['analysis'], evidence['analysis']['projection'])
    # Even with every label rewritten, replaying the retained tensors gives the other frame count.
    with pytest.raises(PocketError) as refused:
        notes.load_note_hypotheses(put_record(record, f['store_root']), f['store_root'])
    assert refusal in str(refused.value.__cause__)


def test_only_v1_records_may_lack_decoder_provenance(note_fixture):
    f = note_fixture
    _, record = made_with(f, V2, 'v2')
    evidence = read_record(record['analysis'], f['store_root'])
    del evidence['projection_provenance']
    record['analysis'] = put_record(evidence, f['store_root'])
    record['annotations'] = notes._annotations(record['analysis'], evidence['analysis']['projection'])
    with pytest.raises(PocketError, match='Malformed retained note evidence'):
        notes.load_note_hypotheses(put_record(record, f['store_root']), f['store_root'])


@pytest.mark.parametrize('change', [{'decoder': 'basic_pitch_0_4_0_false_false_v3'}, {'min_note_frames': 12}])
def test_settings_must_match_a_decoder_exactly(note_fixture, change):
    settings = {**copy.deepcopy(notes.DECODERS[V2]['module'].SETTINGS), **change}
    with pytest.raises(PocketError, match='Unsupported note settings'):
        notes.audio_note_hypotheses(request_id='bad', **{**args(note_fixture), 'settings': settings})


def test_the_settings_type_accepts_both_decoders_strictly():
    from pydantic import TypeAdapter, ValidationError

    from pocket_music.audio_note_types import AudioNoteSettings
    adapter = TypeAdapter(AudioNoteSettings)
    for decoder in (V1, V2):
        settings = notes.DECODERS[decoder]['module'].SETTINGS
        assert adapter.validate_python(copy.deepcopy(settings), strict=True) == settings
    assert adapter.json_schema()['properties']['decoder']['enum'] == [V1, V2]
    with pytest.raises(ValidationError):
        adapter.validate_python({**notes.SETTINGS, 'decoder': 'basic_pitch_0_4_0_false_false_v3'}, strict=True)
