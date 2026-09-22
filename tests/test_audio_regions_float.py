"""Synthetic IEEE FLOAT32 capture: exact sample bits, not audio interpretation."""
import hashlib
import struct
from pathlib import Path

import pytest

from pocket_music import audio_regions as regions
from pocket_music.artifact_store import put_bytes, put_record, read_bytes
from pocket_music.errors import PocketError


def chunk(tag, payload):
    return tag + struct.pack('<I', len(payload)) + payload + (b'\0' if len(payload) & 1 else b'')


def wav(pcm, *, channels=2, rate=44100, code=3, bits=32, fmt18=True, fact='auto', order='before', extra=b''):
    align = channels * (bits // 8)
    fmt = struct.pack('<HHIIHH', code, channels, rate, rate * align, align, bits)
    if fmt18:
        fmt += b'\0\0'
    facts = chunk(b'fact', struct.pack('<I', len(pcm) // align)) if fact == 'auto' else fact
    body = b'WAVE' + chunk(b'fmt ', fmt) + extra
    if order == 'before':
        body += facts + chunk(b'data', pcm)
    else:
        body += chunk(b'data', pcm) + facts
    return b'RIFF' + struct.pack('<I', len(body)) + body


def args(tmp_path, payload, *, start=0, frames=1):
    source = tmp_path / 'float.wav'
    source.write_bytes(payload)
    return {'store_root': str(tmp_path / 'store'), 'request_id': 'capture', 'source': {
        'path': str(source), 'expected_sha256': hashlib.sha256(payload).hexdigest(),
        'start_frame': start, 'frames': frames, 'source_origin': 'independently_acquired'}}


def records(a):
    reply = regions.audio_region_capture(**a)
    record = regions.load_audio_region(reply['artifacts']['region'], a['store_root'])
    return reply, record, read_bytes(record['crop'], a['store_root'])


@pytest.mark.parametrize('fmt18', [False, True])
@pytest.mark.parametrize('order', ['before', 'after'])
def test_float_sample_bits_signed_zero_subnormal_overs_and_canonical_header(tmp_path, fmt18, order):
    values = [0x00000000, 0x80000000, 1, 0x80000001, 0x40000000, 0xc0400000, 0x7f7fffff, 0xff7fffff]
    pcm = struct.pack('<8I', *values)
    a = args(tmp_path, wav(pcm, fmt18=fmt18, order=order, extra=chunk(b'LIST', b'odd')), frames=3, start=1)
    source = Path(a['source']['path']);before = source.stat();reply, body, crop = records(a)
    selected = pcm[8:]
    assert crop == wav(selected)
    assert len(crop) == 58 + len(selected)
    assert struct.unpack('<I', crop[46:50])[0] == 3  # fact sample count
    assert crop[58:] == selected
    assert body['original']['subtype'] == 'FLOAT'
    assert body['projection'] == {**regions.PROJECTION, 'profile': 'riff_float32_region_v1'}
    assert body['sample_proof'] == {'encoding': 'interleaved_ieee_float32_le', 'bytes_per_sample': 4,
        'block_align': 8, 'pcm_sha256': hashlib.sha256(selected).hexdigest(), 'pcm_bytes': len(selected)}
    assert reply['coverage']['profile'] == 'riff_float32_region_v1'
    assert regions.audio_region_capture(**a) == reply
    after = source.stat()
    assert (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (
        after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    source.unlink()
    assert regions.load_audio_region(reply['artifacts']['region'], a['store_root']) == body


@pytest.mark.parametrize('facts', [b'', chunk(b'fact', b''), chunk(b'fact', b'123'),
    chunk(b'fact', b'12345'), chunk(b'fact', struct.pack('<I', 2)),
    chunk(b'fact', struct.pack('<I', 1)) * 2])
def test_missing_malformed_duplicate_or_scalar_sample_fact_refuses_before_artifacts(tmp_path, facts):
    a = args(tmp_path, wav(bytes(8), fact=facts))
    with pytest.raises(PocketError, match='fact'): regions.audio_region_capture(**a)
    assert not list(Path(a['store_root']).glob('artifacts/*/*'))


@pytest.mark.parametrize('bad_bits', [0x7f800000, 0xff800000, 0x7fc00001, 0x7f800001, 0xffc00001])
def test_selected_nonfinite_raw_bits_refuse_but_unselected_are_identity_only(tmp_path, bad_bits):
    pcm = struct.pack('<4I', bad_bits, 0, 0x80000000, 1)
    a = args(tmp_path, wav(pcm), start=1)
    _, body, crop = records(a)
    assert crop[58:] == pcm[8:]
    assert body['original']['frames'] == 2
    a['request_id'] = 'selected-nonfinite';a['source']['start_frame'] = 0
    before = set(Path(a['store_root']).rglob('record.json'))
    with pytest.raises(PocketError, match='NaN or infinity'): regions.audio_region_capture(**a)
    assert set(Path(a['store_root']).rglob('record.json')) == before


@pytest.mark.parametrize('change', ['nonfinite', 'fact', 'header', 'projection', 'proof', 'subtype'])
def test_resealed_retained_float_proof_and_sample_changes_refuse(tmp_path, change):
    a = args(tmp_path, wav(bytes(8)))
    reply, body, _crop = records(a)
    if change == 'nonfinite':
        raw = struct.pack('<II', 0x7fc00001, 0)
        body['crop'] = put_bytes(wav(raw), a['store_root'], 'region.wav', regions.CROP_SCHEMA)
        body['sample_proof']['pcm_sha256'] = hashlib.sha256(raw).hexdigest()
    elif change == 'fact':
        body['crop'] = put_bytes(wav(bytes(8), fact=b''), a['store_root'], 'region.wav', regions.CROP_SCHEMA)
    elif change == 'header':
        body['crop'] = put_bytes(wav(bytes(8), fmt18=False), a['store_root'], 'region.wav', regions.CROP_SCHEMA)
    elif change == 'projection': body['projection'] = dict(regions.PROJECTION)
    elif change == 'proof': body['sample_proof']['encoding'] = 'interleaved_signed_le_pcm'
    else: body['original']['subtype'] = 'PCM_24'
    with pytest.raises(PocketError): regions.load_audio_region(put_record(body, a['store_root']), a['store_root'])
    assert regions.load_audio_region(reply['artifacts']['region'], a['store_root'])


@pytest.mark.parametrize(('code', 'bits'), [(3, 64), (0xfffe, 32), (1, 32), (2, 32)])
def test_unqualified_encodings_refuse(tmp_path, code, bits):
    a = args(tmp_path, wav(bytes(bits // 8 * 2), code=code, bits=bits))
    with pytest.raises(PocketError, match='qualified'): regions.audio_region_capture(**a)


def test_pcm_fact_ancillary_behavior_and_exact_original_projection_unchanged(tmp_path):
    pcm = struct.pack('<hh', -32768, 32767)
    payload = wav(pcm, code=1, bits=16, fact=chunk(b'fact', b'bad') * 2)
    a = args(tmp_path, payload)
    _, body, crop = records(a)
    expected = b'RIFF' + struct.pack('<I', 36 + len(pcm)) + b'WAVE' + chunk(b'fmt ',
        struct.pack('<HHIIHH', 1, 2, 44100, 176400, 4, 16)) + chunk(b'data', pcm)
    assert crop == expected and len(crop) == 44 + len(pcm)
    assert body['projection'] == regions.PROJECTION
    assert body['sample_proof']['encoding'] == 'interleaved_signed_le_pcm'


def test_float_cancellation_before_publication(tmp_path):
    a = args(tmp_path, wav(bytes(8)))
    def cancel():
        raise PocketError('synthetic cancel')
    with regions.region_execution_context(cancellation_check=cancel), pytest.raises(PocketError, match='cancel'):
        regions.audio_region_capture(**a)
    assert not list(Path(a['store_root']).glob('artifacts/*/*'))


def test_float_finite_inspection_across_block_boundary():
    pcm = bytes(regions.BLOCK_BYTES) + struct.pack('<I', 0x7f800000)
    with pytest.raises(PocketError, match='NaN or infinity'):
        regions._finite_float32(pcm)


def test_minimal_fmt16_source_is56bytes_but_canonical_crop_is58(tmp_path):
    a = args(tmp_path, wav(bytes(8), fmt18=False))
    _, body, crop = records(a)
    assert body['original']['bytes'] == 64
    assert len(crop) == 66


def test_actual_public_float_worker_matches_direct_peek(tmp_path):
    from test_audio_hypothesis_jobs import wait_terminal
    from test_audio_region_analysis import ACTOR, ANALYSIS

    from pocket_music.audio_hypothesis_jobs import audio_region_submit, job_status
    from pocket_music.audio_region_analysis import audio_region_hypotheses
    # Two seconds stereo silent FLOAT; decoding may analyze samples, capture never converts them.
    capture = args(tmp_path, wav(bytes(2 * 44100 * 8)), frames=2 * 44100)
    source = Path(capture['source']['path'])
    before = source.stat()
    arguments = {'store_root': capture['store_root'], 'request_id': 'float-job',
        'region': {'kind': 'inline', 'source': capture['source']},
        'analysis': ANALYSIS, 'attribution': ACTOR}
    submitted = audio_region_submit(**arguments)
    terminal = wait_terminal(arguments, submitted)
    assert terminal['state'] == 'completed', terminal
    direct = audio_region_hypotheses(**{**arguments, 'request_id': 'float-direct'})
    assert terminal['result'] == direct['artifacts']['hypotheses']
    after = source.stat()
    assert (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (
        after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    source.unlink()
    assert job_status(arguments['store_root'], submitted['job']['job_id'])['state'] == 'completed'


def test_float_large_sparse_source_streams_small_late_exact_crop(tmp_path, monkeypatch):
    frames = 34 * 1024**2  #272MiB data; source cannot fit generic whole-artifact profile.
    size = 58 + frames * 8
    header = bytearray(wav(bytes(8)))[:58]
    struct.pack_into('<I', header, 4, size - 8)
    struct.pack_into('<I', header, 46, frames)
    struct.pack_into('<I', header, 54, frames * 8)
    source = tmp_path / 'large.wav'
    start = frames - 2
    selected = struct.pack('<4I', 0x80000000, 1, 0x40000000, 0xc0000000)
    with source.open('wb') as out:
        out.write(header)
        out.seek(58 + start * 8)
        out.write(selected)
    hashed = hashlib.sha256()
    with source.open('rb') as stream:
        while block := stream.read(1024**2):
            hashed.update(block)
    a = {'store_root': str(tmp_path / 'store'), 'request_id': 'large', 'source': {
        'path': str(source), 'expected_sha256': hashed.hexdigest(), 'start_frame': start,
        'frames': 2, 'source_origin': 'independently_acquired'}}
    reads = []
    real = regions._exact
    def bounded(stream, length):
        reads.append(length)
        assert length <= 1024**2
        return real(stream, length)
    monkeypatch.setattr(regions, '_exact', bounded)
    _, body, crop = records(a)
    assert body['original']['bytes'] == size > 256 * 1024**2
    assert crop[58:] == selected and len(crop) == 74
    assert max(reads) == 1024**2
