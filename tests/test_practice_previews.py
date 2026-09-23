"""Independent byte/sample oracle and provenance checks for declared browser previews."""
import copy
import io
import shutil
import wave
from fractions import Fraction

import numpy as np
import pytest
import soundfile as sf
from test_musical_context import fixture, q
from test_practice_envelopes import envelope_fixture

from pocket_music.artifact_store import put_bytes, put_record, read_bytes, read_record
from pocket_music.assets import sha256_file
from pocket_music.audio_regions import audio_region_capture
from pocket_music.errors import PocketError
from pocket_music.musical_context import context_create
from pocket_music.practice_audio import practice_compare, practice_query, practice_render
from pocket_music.practice_envelopes import practice_compare_processed, practice_envelope
from pocket_music.practice_previews import pcm16_wav, practice_preview, quantize_pcm16
from pocket_music.time_maps import musical_time

PROFILE = 'browser-pcm16-original-rate/v1'


def rendered(tmp_path, samples, rate=8000, subtype='FLOAT'):
    """Render one occurrence spanning explicitly generated source samples."""
    store = str(tmp_path / 'store')
    source = tmp_path / 'generated.wav'
    sf.write(source, samples, rate, subtype=subtype)
    frames = len(samples)
    region = audio_region_capture(store_root=store, request_id='capture', source={
        'path': str(source), 'expected_sha256': sha256_file(source), 'start_frame': 0, 'frames': frames,
        'source_origin': 'independently_acquired'})['artifacts']['region']
    end = Fraction(2 * frames, rate)
    time_map = musical_time('create', store, request_id='time', definition={
        'source_context': {'schema': 'pocket.time-context/v1', 'context_id': 'clock', 'attribution': 'Synthetic'},
        'domain_qn': {'start': q(0), 'end': q(end.numerator, end.denominator)},
        'tempo': [{'at_qn': q(0), 'bpm': q(120), 'interpolation': 'step'}],
        'host_origin': {'arrangement_qn': q(0), 'host_seconds': q(0)}})['artifacts']['time_map']
    author = {'actor': 'synthetic fixture', 'actor_kind': 'agent', 'statement': 'Generated values',
              'uncertainty': ['No musical claim']}
    context = context_create(store, 'context', {
        'context_id': 'preview', 'title': 'Preview fixture', 'attribution': author,
        'sources': [{'clock_id': 'recording', 'region': region}],
        'timelines': [{'clock_id': 'practice', 'time_map': time_map}],
        'occurrences': [{'occurrence_id': 'all', 'source_clock_id': 'recording', 'timeline_clock_id': 'practice',
                         'source_span_frames': [0, frames], 'timeline_span_qn': [q(0), q(end.numerator, end.denominator)]}],
        'anchors': [], 'materials': []})['artifacts']['context']
    render = practice_render(store, 'render', context, ['all'])['artifacts']['render']
    return store, render, source


def oracle_bytes(parent_samples, rate):
    """Exact rational nearest-even rounding and stdlib WAV writing, independent of the provider."""
    frames, channels = parent_samples.shape
    values = []
    for frame in range(frames):
        for channel in range(channels):
            value = round(Fraction(float(parent_samples[frame, channel])) * 32768)  # Fraction rounds half to even
            assert -32768 <= value <= 32767
            values.append(value)
    output = io.BytesIO()
    with wave.open(output, 'wb') as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(np.array(values, dtype='<i2').tobytes())
    return output.getvalue(), values


def parent_samples(store, render):
    record = read_record(render, store)
    return sf.read(io.BytesIO(read_bytes(record['audio'], store)), dtype='float64', always_2d=True)[0]


def test_ties_extrema_and_stereo_order_match_independent_oracle(tmp_path):
    left = np.array([0.5, 1.5, 2.5, -0.5, -1.5, -2.5, 0, 32767, -32768, 100.25, -100.75, 7.5], dtype=np.float64)
    right = -left[::-1]
    right[right == 32768] = 1  # keep the reversed right channel representable and asymmetric
    samples = np.stack([left, right], axis=1) / 32768
    store, render, _ = rendered(tmp_path, samples.astype(np.float32), rate=44100)
    result = practice_preview(store, 'preview', render, PROFILE)
    assert practice_preview(store, 'preview', render, PROFILE) == result
    record = read_record(result['artifacts']['preview'], store)
    expected, values = oracle_bytes(parent_samples(store, render), 44100)
    stored = read_bytes(record['audio'], store)
    assert stored == expected
    assert values[:12:2] == [0, 2, 2, 0, -2, -2]  # ties go to the even neighbour in both directions
    decoded, rate = sf.read(io.BytesIO(stored), dtype='int16', always_2d=True)
    assert rate == 44100 and decoded.shape == (12, 2)
    assert decoded[7, 0] == 32767 and decoded[8, 0] == -32768
    assert list(decoded[:, 1]) == values[1::2]
    stats = record['quantization']
    assert stats['samples'] == 24 and stats['max_abs_error_lsb'] == 0.5
    assert stats['exact_samples'] == 24 - 2 * 9  # nine non-integer values per channel
    assert result['coverage']['parent_samples_exact'] is False
    assert record['frame_mapping'] == {'kind': 'identity', 'parent_offset_frames': 0}
    assert record['conversion']['dither'] == 'none' and record['conversion']['clamping'] is False
    assert record['listening'] == 'not_reviewed' and record['musical_verdict'] is None


def test_pcm16_parent_is_exact_and_pcm24_rounding_is_bounded(tmp_path):
    store, context, _, _, _ = fixture(tmp_path)
    render = practice_render(store, 'raw', context, ['first', 'again'])['artifacts']['render']
    result = practice_preview(store, 'preview', render, PROFILE)
    assert result['coverage']['parent_samples_exact'] is True
    assert result['change_summary']['rounded_samples'] == 0
    preview = read_record(result['artifacts']['preview'], store)
    decoded = sf.read(io.BytesIO(read_bytes(preview['audio'], store)), dtype='float64', always_2d=True)[0]
    assert np.array_equal(decoded, parent_samples(store, render))
    rng = np.random.default_rng(7)
    fine = (tmp_path / 'pcm24')
    fine.mkdir()
    store24, render24, _ = rendered(fine, rng.uniform(-0.9, 0.9, (4000, 1)), subtype='PCM_24')
    stats = read_record(practice_preview(store24, 'preview', render24, PROFILE)['artifacts']['preview'], store24)['quantization']
    assert 0 < stats['max_abs_error_lsb'] <= 0.5 and stats['exact_samples'] < stats['samples']


@pytest.mark.parametrize('value,frame', [(32767.5, 3), (32768, 0), (-32769.5, 1), (40000, 2)])
def test_unrepresentable_values_are_refused_not_clamped(tmp_path, value, frame):
    samples = np.full((4, 1), 0.25)
    samples[frame, 0] = value / 32768
    store, render, _ = rendered(tmp_path, samples.astype(np.float32))
    with pytest.raises(PocketError, match=f'frame {frame}, channel 0 does not round into PCM16') as error:
        practice_preview(store, 'preview', render, PROFILE)
    assert error.value.code == 'unsupported_profile'
    assert not list((tmp_path / 'store' / 'artifacts').glob('*/preview.wav'))


def test_negative_full_scale_tie_is_representable(tmp_path):
    samples = np.array([[-32768.5 / 32768], [0.1]])
    store, render, _ = rendered(tmp_path, samples.astype(np.float32))
    record = read_record(practice_preview(store, 'preview', render, PROFILE)['artifacts']['preview'], store)
    assert sf.read(io.BytesIO(read_bytes(record['audio'], store)), dtype='int16')[0][0] == -32768


def test_quantizer_refuses_nonfinite_and_wrong_shapes():
    for bad in (np.array([[0.0], [np.nan]]), np.array([[np.inf, 0.0]]), np.array([[-np.inf]])):
        with pytest.raises(PocketError, match='nonfinite'):
            quantize_pcm16(bad)
    for bad in (np.zeros(4), np.zeros((2, 1), dtype=np.float32), [[0.0]]):
        with pytest.raises(PocketError, match='float64'):
            quantize_pcm16(bad)
    with pytest.raises(PocketError, match='does not round'):
        quantize_pcm16(np.array([[1e308]]))


def test_silence_impulse_and_signal_warnings_are_retained(tmp_path):
    silent_dir, impulse_dir = tmp_path / 'silent', tmp_path / 'impulse'
    silent_dir.mkdir()
    impulse_dir.mkdir()
    store, render, _ = rendered(silent_dir, np.zeros((800, 1), dtype=np.float32))
    result = practice_preview(store, 'preview', render, PROFILE)
    assert result['warnings'] == ['Parent: unexpected_silence', 'Preview: unexpected_silence']
    impulse = np.zeros((800, 2), dtype=np.float32)
    impulse[400, 1] = 0.5
    store, render, _ = rendered(impulse_dir, impulse)
    record = read_record(practice_preview(store, 'preview', render, PROFILE)['artifacts']['preview'], store)
    decoded = sf.read(io.BytesIO(read_bytes(record['audio'], store)), dtype='int16', always_2d=True)[0]
    assert np.count_nonzero(decoded) == 1 and decoded[400, 1] == 16384
    assert record['signal']['sample_peak'] == 0.5 and record['input_signal']['sample_peak'] == 0.5


def test_overloaded_parent_warnings_stay_visible_when_representable(tmp_path):
    samples = np.full((800, 1), 0.2)
    samples[10, 0] = -1.0  # decoded PCM16 minimum: representable, but measured as full-scale overload
    store, render, _ = rendered(tmp_path, samples, subtype='PCM_16')
    result = practice_preview(store, 'preview', render, PROFILE)
    assert result['warnings'] == ['Parent: sample_overload', 'Preview: sample_overload']
    assert result['coverage']['parent_samples_exact'] is True


def test_unsupported_rate_refuses_without_resampling(tmp_path):
    store, render, _ = rendered(tmp_path, np.full((1000, 1), 0.1, dtype=np.float32), rate=12345)
    with pytest.raises(PocketError, match='12345 Hz is not a qualified browser preview rate') as error:
        practice_preview(store, 'preview', render, PROFILE)
    assert error.value.code == 'unsupported_profile'


def test_envelope_parent_and_query_profiles(tmp_path):
    args, _ = envelope_fixture(tmp_path)
    store = args['store_root']
    envelope = practice_envelope(**args)['artifacts']['render']
    result = practice_preview(store, 'preview', envelope, PROFILE)
    preview = result['artifacts']['preview']
    assert result['coverage']['parent_profile'] == 'linear-loop-join-envelope/v1'
    summary = practice_query(store, preview)
    assert summary['coverage']['profile'] == PROFILE
    assert summary['coverage']['parent_profile'] == 'linear-loop-join-envelope/v1'
    assert summary['coverage']['frame_mapping'] == 'identity'
    assert summary['summary']['parent'] == envelope
    expected, _ = oracle_bytes(parent_samples(store, envelope), 8000)
    assert read_bytes(summary['summary']['audio'], store) == expected
    mappings = practice_query(store, preview, section='mappings', limit=1)
    assert mappings['rows'] == read_record(args['render'], store)['mappings'][:1]
    assert mappings['next_offset'] == 1 and mappings['total'] == 2


def test_previews_are_not_parents_baselines_or_comparison_members(tmp_path):
    args, _ = envelope_fixture(tmp_path)
    store, raw = args['store_root'], args['render']
    envelope = practice_envelope(**args)['artifacts']['render']
    preview = practice_preview(store, 'preview', raw, PROFILE)['artifacts']['preview']
    with pytest.raises(PocketError, match='not preview parents'):
        practice_preview(store, 'chained', preview, PROFILE)
    with pytest.raises(PocketError):
        practice_envelope(**{**args, 'request_id': 'env-of-preview', 'render': preview})
    with pytest.raises(PocketError):
        practice_compare(store, 'cmp', preview, [raw], 'Synthetic refusal')
    with pytest.raises(PocketError):
        practice_compare(store, 'cmp2', raw, [preview], 'Synthetic refusal')
    with pytest.raises(PocketError):
        practice_compare_processed(store, 'cmp3', raw, [preview], 'Synthetic refusal')
    with pytest.raises(PocketError):
        practice_compare_processed(store, 'cmp4', preview, [envelope], 'Synthetic refusal')
    comparison = practice_compare(store, 'cmp5', raw, [practice_render(
        store, 'alt', read_record(raw, store)['context'], ['alternative'])['artifacts']['render']],
        'Synthetic', allow_duration_mismatch=True)['artifacts']['comparison']
    with pytest.raises(PocketError, match='not preview parents'):
        practice_preview(store, 'from-comparison', comparison, PROFILE)
    with pytest.raises(PocketError, match='Unknown preview profile'):
        practice_preview(store, 'other-profile', raw, 'browser-float32/v1')


def test_relocation_and_deleted_original_keep_previews_valid(tmp_path):
    store, context, _, source, _ = fixture(tmp_path)
    render = practice_render(store, 'raw', context, ['first', 'again'])['artifacts']['render']
    preview = practice_preview(store, 'preview', render, PROFILE)['artifacts']['preview']
    before = practice_query(store, preview)
    moved = tmp_path / 'moved'
    shutil.copytree(store, moved)
    shutil.rmtree(store)
    source.unlink()
    assert practice_query(str(moved), preview) == before


def forged(store, preview, change):
    record = copy.deepcopy(read_record(preview, store))
    change(record, store)
    return put_record(record, store)


def _other_bytes(record, store):
    payload = bytearray(read_bytes(record['audio'], store))
    payload[-2:] = (int.from_bytes(payload[-2:], 'little', signed=True) ^ 1).to_bytes(2, 'little', signed=True)
    record['audio'] = put_bytes(bytes(payload), store, 'preview.wav', 'pocket.practice-preview-audio/v1')


@pytest.mark.parametrize('change', [
    _other_bytes,
    lambda r, s: r['quantization'].update(max_abs_error_lsb=0.25),
    lambda r, s: r['conversion'].update(dither='tpdf'),
    lambda r, s: r['conversion'].update(clamping=True),
    lambda r, s: r['frame_mapping'].update(parent_offset_frames=1),
    lambda r, s: r['format'].update(sample_rate=16000),
    lambda r, s: r['signal'].update(sample_peak=0.1),
    lambda r, s: r['input_signal'].update(rms=0.1),
    lambda r, s: r['playback'].update(device_output_verified=True),
    lambda r, s: r.update(listening='reviewed'),
    lambda r, s: r.update(musical_verdict='keep'),
    lambda r, s: r['provider'].update(implementation='other'),
    lambda r, s: r['audio'].update(artifact_schema='pocket.render-audio/v1'),
    lambda r, s: r.update(extra=True),
])
def test_rehashed_forged_manifests_are_refused(tmp_path, change):
    store, context, _, _, _ = fixture(tmp_path)
    render = practice_render(store, 'raw', context, ['first', 'again'])['artifacts']['render']
    preview = practice_preview(store, 'preview', render, PROFILE)['artifacts']['preview']
    with pytest.raises(PocketError):
        practice_query(store, forged(store, preview, change))


def test_wrong_parent_and_changed_ancestor_bytes_are_refused(tmp_path):
    store, context, _, _, _ = fixture(tmp_path)
    render = practice_render(store, 'raw', context, ['first', 'again'])['artifacts']['render']
    other = practice_render(store, 'other', context, ['alternative'])['artifacts']['render']
    preview = practice_preview(store, 'preview', render, PROFILE)['artifacts']['preview']
    swapped = forged(store, preview, lambda r, s: r.update(parent=other))
    with pytest.raises(PocketError, match='differ from its exact parent'):
        practice_query(store, swapped)
    region = read_record(read_record(context, store)['definition']['sources'][0]['region'], store)
    crop = tmp_path / 'store' / region['crop']['artifact_uri']
    payload = bytearray(crop.read_bytes())
    payload[-1] ^= 1
    crop.chmod(0o644)
    crop.write_bytes(bytes(payload))
    with pytest.raises(PocketError, match='integrity'):
        practice_query(store, preview)


def test_changed_request_inputs_conflict(tmp_path):
    store, context, _, _, _ = fixture(tmp_path)
    render = practice_render(store, 'raw', context, ['first', 'again'])['artifacts']['render']
    other = practice_render(store, 'other', context, ['alternative'])['artifacts']['render']
    practice_preview(store, 'preview', render, PROFILE)
    with pytest.raises(PocketError, match='idempotency_conflict') as error:
        practice_preview(store, 'preview', other, PROFILE)
    assert error.value.code == 'idempotency_conflict'


def test_canonical_wav_writer_matches_stdlib_for_mono_and_stereo():
    for channels in (1, 2):
        values = (np.arange(-6, 6).reshape(-1, channels) * 1000).astype('<i2')
        output = io.BytesIO()
        with wave.open(output, 'wb') as stream:
            stream.setnchannels(channels)
            stream.setsampwidth(2)
            stream.setframerate(22050)
            stream.writeframes(values.tobytes())
        assert pcm16_wav(values, 22050) == output.getvalue()
