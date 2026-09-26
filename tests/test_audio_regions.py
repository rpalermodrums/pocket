# SPDX-License-Identifier: AGPL-3.0-only
"""Independent PCM readback, bounded streaming, source races and capture semantics."""
import copy
import hashlib
import io
import os
import struct
import wave
from pathlib import Path

import pytest

from pocket_music import audio_regions as regions
from pocket_music.artifact_store import canonical_bytes, put_bytes, put_record, read_bytes, request_status
from pocket_music.errors import PocketError


def wav(pcm, channels=1, rate=8000, bits=16, ancillary=b''):
    align = channels * (bits // 8)
    fmt = struct.pack('<HHIIHH', 1, channels, rate, rate * align, align, bits)
    body = b'WAVEfmt ' + struct.pack('<I', 16) + fmt + ancillary + b'data' + struct.pack('<I', len(pcm)) + pcm
    if len(pcm) & 1:
        body += b'\0'
    return b'RIFF' + struct.pack('<I', len(body)) + body


def source(tmp_path, payload=None, frames=10, start=1):
    path = tmp_path / 'source.wav'
    path.write_bytes(payload or wav(struct.pack('<16h', *range(-8, 8))))
    return {'path': str(path), 'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'start_frame': start, 'frames': frames, 'source_origin': 'independently_acquired'}


def capture(tmp_path, args, request='capture'):
    return regions.audio_region_capture(store_root=str(tmp_path / 'store'), request_id=request, source=args)


def load(tmp_path, result):
    return regions.load_audio_region(result['artifacts']['region'], tmp_path / 'store')


@pytest.mark.parametrize('bits,channels', [(16, 1), (16, 2), (24, 1), (24, 8)])
def test_exact_pcm_independent_wave_decoder(tmp_path, bits, channels):
    width = bits // 8
    values = [-(2**(bits-1)), -1, 0, 2**(bits-1)-1] * channels
    pcm = b''.join(v.to_bytes(width, 'little', signed=True) for v in values)
    args = source(tmp_path, wav(pcm, channels=channels, bits=bits, ancillary=b'JUNK\x03\0\0\0abc\0'), frames=2, start=1)
    original = Path(args['path']).read_bytes()
    stamp = regions._stamp(Path(args['path']).stat())
    result = capture(tmp_path, args)
    record = load(tmp_path, result)
    crop = read_bytes(record['crop'], tmp_path / 'store')
    with wave.open(io.BytesIO(crop), 'rb') as reader:
        assert (reader.getnframes(), reader.getnchannels(), reader.getsampwidth()) == (2, channels, width)
        assert reader.readframes(2) == pcm[channels * width:3 * channels * width]
    assert record['original']['retention'] == 'external_identity_only'
    assert record['original']['sha256'] == args['expected_sha256']
    assert record['mapping']['original_start_frame'] == 1
    assert record['sample_proof']['pcm_bytes'] == 2 * channels * width
    assert Path(args['path']).read_bytes() == original
    assert regions._stamp(Path(args['path']).stat()) == stamp
    assert str(tmp_path) not in canonical_bytes(record).decode()
    assert len(canonical_bytes(result)) < 4096


def test_one_frame_odd_pcm24_and_exact_end(tmp_path):
    args = source(tmp_path, wav(b'\x00\x00\x80\xff\xff\x7f', bits=24), frames=1, start=1)
    record = load(tmp_path, capture(tmp_path, args))
    assert record['interval'] == {'start_frame': 1, 'end_frame_exclusive': 2}
    assert read_bytes(record['crop'], tmp_path / 'store')[-4:] == b'\xff\xff\x7f\0'


def test_replay_relocation_and_self_contained_load(tmp_path):
    args = source(tmp_path)
    result = capture(tmp_path, args)
    assert capture(tmp_path, args) == result
    other = tmp_path / 'relocated.wav'
    other.write_bytes(Path(args['path']).read_bytes())
    moved = capture(tmp_path, {**args, 'path': str(other)}, 'moved')
    assert moved['artifacts'] == result['artifacts']
    Path(args['path']).unlink()
    assert load(tmp_path, result)['original']['sha256'] == args['expected_sha256']
    with pytest.raises(PocketError):
        capture(tmp_path, args)


@pytest.mark.parametrize('field,value', [('frames', True), ('frames', 0), ('start_frame', True),
    ('start_frame', -1), ('frames', 1920001), ('frames', 17), ('start_frame', 16),
    ('expected_sha256', '0'*64), ('source_origin', 'spotify'), ('path', 'relative.wav')])
def test_strict_source_and_bounds(tmp_path, field, value):
    args = source(tmp_path)
    args[field] = value
    with pytest.raises(PocketError):
        capture(tmp_path, args)
    assert not list((tmp_path / 'store/artifacts').glob('*/record.json'))


@pytest.mark.parametrize('mutate', [
    lambda b: b'RF64' + b[4:],
    lambda b: b[:4] + struct.pack('<I', len(b)) + b[8:],
    lambda b: b[:20] + struct.pack('<H', 3) + b[22:],
    lambda b: b[:22] + struct.pack('<H', 9) + b[24:],
    lambda b: b[:24] + struct.pack('<I', 7999) + b[28:],
    lambda b: b[:28] + struct.pack('<I', 1) + b[32:],
    lambda b: b[:32] + struct.pack('<H', 1) + b[34:],
    lambda b: b[:-1],
])
def test_riff_malformed_refuses(tmp_path, mutate):
    args = source(tmp_path, mutate(wav(b'\0\0'*16)))
    with pytest.raises(PocketError):
        capture(tmp_path, args)


def test_multiple_data_and_nonintegral_frame_refuse(tmp_path):
    first = wav(b'\0\0' * 16)
    double = first + b'data' + struct.pack('<I', 2) + b'\0\0'
    double = double[:4] + struct.pack('<I', len(double)-8) + double[8:]
    for index, payload in enumerate([double, wav(b'\0'*3)]):
        args = source(tmp_path, payload, frames=1, start=0)
        with pytest.raises(PocketError):
            capture(tmp_path, args, f'malformed-{index}')


@pytest.mark.parametrize('mode', ['replace', 'in_place', 'truncate', 'symlink'])
def test_source_change_during_streaming_refuses(tmp_path, monkeypatch, mode):
    args = source(tmp_path)
    original = Path(args['path'])
    if mode == 'symlink':
        alias = tmp_path / 'alias.wav'
        alias.symlink_to(original)
        args['path'] = str(alias)
    real = regions._exact
    changed = False
    def read(stream, length):
        nonlocal changed
        data = real(stream, length)
        if length == original.stat().st_size and not changed:
            changed = True
            if mode == 'replace':
                other = tmp_path / 'new.wav'
                other.write_bytes(data)
                os.replace(other, original)
            elif mode == 'in_place':
                with original.open('r+b') as output:
                    output.seek(44)
                    output.write(b'\xff\xff')
            elif mode == 'truncate':
                original.write_bytes(b'truncated')
            else:
                other = tmp_path / 'other.wav'
                other.write_bytes(data)
                alias.unlink()
                alias.symlink_to(other)
        return data
    monkeypatch.setattr(regions, '_exact', read)
    with pytest.raises(PocketError, match='changed|replaced'):
        capture(tmp_path, args)
    assert changed
    assert not list((tmp_path / 'store/artifacts').glob('*/record.json'))


@pytest.mark.parametrize('at', [1, 4, 6, 8])
def test_cooperative_cancel_no_complete_capture(tmp_path, at):
    args = source(tmp_path)
    count = 0
    def cancel():
        nonlocal count
        count += 1
        if count == at:
            raise KeyboardInterrupt('cancel fixture')
    with regions.region_execution_context(cancellation_check=cancel), pytest.raises(KeyboardInterrupt):
        capture(tmp_path, args)
    assert request_status(str(tmp_path / 'store'), 'capture')['journal_state'] == 'failed'
    assert not list((tmp_path / 'store/artifacts').glob('*/record.json'))
    assert regions._CONTEXT.get() is None
    with pytest.raises(PocketError, match='did not complete'):
        capture(tmp_path, args)


@pytest.mark.parametrize('mutate', [
    lambda r: r['mapping'].update(original_start_frame=2),
    lambda r: r['mapping'].update(crop_start_frame=False),
    lambda r: r['sample_proof'].update(pcm_sha256='0'*64),
    lambda r: r['original'].update(retention='full_bytes'),
    lambda r: r['original'].update(frames=True),
    lambda r: r['interval'].update(end_frame_exclusive=99),
    lambda r: r['projection'].update(resampled=True),
])
def test_resealed_semantic_proofs_refuse(tmp_path, mutate):
    args = source(tmp_path)
    result = capture(tmp_path, args)
    record = copy.deepcopy(load(tmp_path, result))
    mutate(record)
    forged = put_record(record, tmp_path / 'store')
    with pytest.raises(PocketError):
        regions.load_audio_region(forged, tmp_path / 'store')


def test_noncanonical_crop_and_byte_tamper_refuse(tmp_path):
    args = source(tmp_path)
    result = capture(tmp_path, args)
    record = load(tmp_path, result)
    payload = read_bytes(record['crop'], tmp_path / 'store')
    altered = wav(payload[44:], ancillary=b'JUNK\0\0\0\0')
    record['crop'] = put_bytes(altered, tmp_path / 'store', 'region.wav', regions.CROP_SCHEMA)
    with pytest.raises(PocketError, match='canonical'):
        regions.load_audio_region(put_record(record, tmp_path / 'store'), tmp_path / 'store')
    original = load(tmp_path, result)
    (tmp_path / 'store' / original['crop']['artifact_uri']).write_bytes(b'tamper')
    with pytest.raises(PocketError):
        load(tmp_path, result)


def test_actual_long_source_streaming_and_late_frame_mapping(tmp_path, monkeypatch):
    # 70 minutes 1 second at 16 kHz stereo: actual logical input >256 MiB.
    rate, channels, seconds = 16000, 2, 4201
    frames = rate * seconds
    size = frames * channels * 2
    path = tmp_path / 'long.wav'
    header = wav(b'', channels=channels, rate=rate)[:40] + struct.pack('<I', size)
    header = header[:4] + struct.pack('<I', size+36) + header[8:]
    selected = struct.pack('<8h', -32768, 32767, -1, 1, 222, -333, 0, 0)
    with path.open('wb') as stream:
        stream.write(header)
        stream.truncate(44 + size)
        stream.seek(44 + size - len(selected))
        stream.write(selected)
    with path.open('rb') as stream:
        expected = hashlib.file_digest(stream, 'sha256').hexdigest()
    assert path.stat().st_size > 256 * 1024**2
    args = {'path': str(path), 'expected_sha256': expected, 'start_frame': frames-4,
            'frames': 4, 'source_origin': 'user_recording'}
    real = regions._exact
    reads = []
    def bounded(stream, length):
        reads.append(length)
        assert length <= 1024**2
        return real(stream, length)
    monkeypatch.setattr(regions, '_exact', bounded)
    result = capture(tmp_path, args)
    record = load(tmp_path, result)
    assert record['original']['frames'] == frames
    assert record['mapping']['original_start_frame'] == frames-4
    with wave.open(io.BytesIO(read_bytes(record['crop'], tmp_path / 'store')), 'rb') as reader:
        assert reader.readframes(4) == selected
    assert max(reads) == 1024**2 and sum(reads) >= path.stat().st_size
    assert sum(p.stat().st_size for p in (tmp_path / 'store/artifacts').glob('*/*')) < 8192


def test_mutation_after_crop_before_capture_publication_refuses(tmp_path, monkeypatch):
    args = source(tmp_path)
    real = regions.put_bytes
    def replace(*values, **kwargs):
        handle = real(*values, **kwargs)
        Path(args['path']).write_bytes(b'changed before region commit')
        return handle
    monkeypatch.setattr(regions, 'put_bytes', replace)
    with pytest.raises(PocketError, match='before publication'):
        capture(tmp_path, args)
    assert not list((tmp_path / 'store/artifacts').glob('*/record.json'))


def test_cancel_after_crop_keeps_only_uncommitted_bytes(tmp_path):
    args = source(tmp_path)
    count = 0
    def cancel():
        nonlocal count
        count += 1
        if count == 9:
            raise KeyboardInterrupt('before capture record')
    with regions.region_execution_context(cancellation_check=cancel), pytest.raises(KeyboardInterrupt):
        capture(tmp_path, args)
    assert list((tmp_path / 'store/artifacts').glob('*/region.wav'))
    assert not list((tmp_path / 'store/artifacts').glob('*/record.json'))
    assert request_status(str(tmp_path / 'store'), 'capture')['journal_state'] == 'failed'


def test_twenty_second_bound_exact_and_one_frame_over(tmp_path):
    args = source(tmp_path, wav(b'\0\0' * 160001), frames=160000, start=0)
    assert load(tmp_path, capture(tmp_path, args))['mapping']['frame_count'] == 160000
    args['frames'] += 1
    with pytest.raises(PocketError, match='twenty-second'):
        capture(tmp_path, args, 'too-long')


@pytest.mark.parametrize('kind', ['file_bytes', 'duration', 'crop_bytes'])
def test_limits_reject_before_hashing_large_sparse_sources(tmp_path, monkeypatch, kind):
    rate, channels, bits = (96000, 8, 24) if kind == 'crop_bytes' else (8000, 1, 16)
    frames = rate * (21 if kind == 'crop_bytes' else 7201)
    size = frames * channels * (bits // 8)
    path = tmp_path / 'oversize.wav'
    header = wav(b'', channels=channels, rate=rate, bits=bits)[:40] + struct.pack('<I', size)
    header = header[:4] + struct.pack('<I', size + 36) + header[8:]
    with path.open('wb') as out:
        out.write(header)
        out.truncate(2**32 if kind == 'file_bytes' else size + 44)
    args = {'path': str(path), 'expected_sha256': '0'*64, 'start_frame': 0,
            'frames': rate*20 if kind == 'crop_bytes' else 1, 'source_origin': 'user_recording'}
    real = regions._exact
    def headers_only(stream, length):
        assert length <= 18, 'Oversize profile started whole-file hashing'
        return real(stream, length)
    monkeypatch.setattr(regions, '_exact', headers_only)
    with pytest.raises(PocketError):
        capture(tmp_path, args)


def test_chunk_count_and_short_read_are_bounded(tmp_path):
    extras = b'JUNK\0\0\0\0' * 4096
    args = source(tmp_path, wav(b'\0\0'*16, ancillary=extras))
    with pytest.raises(PocketError, match='chunk bounds'):
        capture(tmp_path, args)
    class Short(io.BytesIO):
        def read(self, count=-1):
            return super().read(max(0, count-1))
    with pytest.raises(PocketError, match='short read'):
        regions._parse(Short(wav(b'\0\0')), 46)
