# SPDX-License-Identifier: AGPL-3.0-only
"""Independent wire fixtures and streaming/failure checks for external-source regions."""
import hashlib
import io
import struct
import tracemalloc
import wave

import pytest

import pocket_music.audio_regions as region
from pocket_music.artifact_store import put_bytes, put_record, read_bytes
from pocket_music.errors import PocketError


def encoded(samples, width=2, channels=1):
    pcm = b''.join(v.to_bytes(width, 'little', signed=True) for v in samples)
    fmt = struct.pack('<HHIIHH', 1, channels, 44100, 44100 * channels * width, channels * width, width * 8)
    chunks = b'JUNK' + struct.pack('<I', 3) + b'xyz\0' + b'fmt ' + struct.pack('<I', 16) + fmt
    chunks += b'data' + struct.pack('<I', len(pcm)) + pcm + (b'\0' if len(pcm) % 2 else b'')
    return b'RIFF' + struct.pack('<I', len(chunks) + 4) + b'WAVE' + chunks


def args_for(path, start=0, frames=1):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024): h.update(block)
    return {'path': str(path), 'expected_sha256': h.hexdigest(), 'start_frame': start,
            'frames': frames, 'source_origin': 'user_recording'}


def capture(tmp_path, source, request='capture'):
    result = region.audio_region_capture(store_root=str(tmp_path / 'store'), request_id=request, source=source)
    record = region.load_audio_region(result['artifacts']['region'], str(tmp_path / 'store'))
    return result, record


@pytest.mark.parametrize('width,channels', [(2, 1), (2, 2), (3, 1), (3, 2)])
def test_one_frame_signed_extremes_odd_chunk_and_selfcontained(tmp_path, width, channels):
    maximum = 2 ** (8 * width - 1) - 1
    samples = [-maximum - 1, maximum, -1, 0] * channels
    path = tmp_path / 'source.wav'; path.write_bytes(encoded(samples, width, channels))
    source = args_for(path, 1, 1);before = path.stat()
    result, record = capture(tmp_path, source)
    payload = read_bytes(record['crop'], tmp_path / 'store')
    with wave.open(io.BytesIO(payload), 'rb') as stream:
        actual = stream.readframes(1)
        assert stream.getnframes() == 1 and stream.getnchannels() == channels and stream.getsampwidth() == width
    assert actual == b''.join(v.to_bytes(width, 'little', signed=True) for v in samples[channels:2*channels])
    assert record['mapping']['original_start_frame'] == 1
    assert record['original']['retention'] == 'external_identity_only'
    assert path.stat().st_mtime_ns == before.st_mtime_ns and path.stat().st_ctime_ns == before.st_ctime_ns
    path.unlink()
    assert region.load_audio_region(result['artifacts']['region'], tmp_path / 'store') == record


def test_actual_hour_source_crosses_stream_block_without_total_allocation(tmp_path, monkeypatch):
    frames = 44100 * 3600; pcm_bytes = frames * 4
    header = b'RIFF' + struct.pack('<I', pcm_bytes + 36) + b'WAVEfmt ' + struct.pack('<IHHIIHH', 16, 1, 2, 44100, 176400, 4, 16) + b'data' + struct.pack('<I', pcm_bytes)
    path = tmp_path / 'one-hour.wav'
    start = (500 * 1024**2 - 44) // 4 - 1
    expected = struct.pack('<6h', -32768, 32767, -123, 456, -1, 0)
    with path.open('wb') as stream:
        stream.write(header);stream.truncate(44 + pcm_bytes);stream.seek(44 + start * 4);stream.write(expected)
    source = args_for(path, start, 3);before = path.stat();sizes=[]
    original = region._exact
    def bounded(stream, length):
        sizes.append(length);assert 0 <= length <= 1024**2
        return original(stream, length)
    monkeypatch.setattr(region, '_exact', bounded)
    tracemalloc.start()
    try:
        result, record = capture(tmp_path, source)
        _, peak = tracemalloc.get_traced_memory()
    finally: tracemalloc.stop()
    assert peak < 48 * 1024**2
    assert before.st_size > 256 * 1024**2 and record['original']['frames'] == frames
    assert record['original']['sha256'] == source['expected_sha256']
    assert max(sizes) == 1024**2 and sum(sizes) >= before.st_size
    with wave.open(io.BytesIO(read_bytes(record['crop'], tmp_path / 'store')), 'rb') as stream:
        assert stream.readframes(3) == expected
    assert record['interval'] == {'start_frame': start, 'end_frame_exclusive': start + 3}
    assert (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns) == (path.stat().st_ino,path.stat().st_size,path.stat().st_mtime_ns,path.stat().st_ctime_ns)
    assert sum(p.stat().st_size for p in (tmp_path / 'store/artifacts').glob('*/*')) < 8192
    assert result['coverage']['original_bytes_retained'] is False


@pytest.mark.parametrize('target', ['mapping', 'proof', 'retention', 'projection', 'header'])
def test_resealed_inconsistent_proofs_refuse(tmp_path, target):
    path=tmp_path/'source.wav';path.write_bytes(encoded([10,20,30,40]))
    _,record=capture(tmp_path,args_for(path,1,2))
    if target=='mapping':record['mapping']['original_start_frame']=0
    elif target=='proof':record['sample_proof']['pcm_sha256']='f'*64
    elif target=='retention':record['original']['retention']='full_original_retained'
    elif target=='projection':record['projection']['resampled']=True
    else:
        payload=bytearray(read_bytes(record['crop'],tmp_path/'store'));payload[24:28]=struct.pack('<I',48000)
        record['crop']=put_bytes(bytes(payload),tmp_path/'store','other.wav',region.CROP_SCHEMA)
    forged=put_record(record,tmp_path/'store')
    with pytest.raises(PocketError):region.load_audio_region(forged,tmp_path/'store')


@pytest.mark.parametrize('mode', ['cancel','replace','truncate','alias'])
def test_mid_hash_interruption_refuses_publication_and_retry(tmp_path,monkeypatch,mode):
    path=tmp_path/'source.wav';path.write_bytes(encoded([0]*600000));source=args_for(path)
    if mode=='alias':
        alias=tmp_path/'alias.wav';alias.symlink_to(path);source['path']=str(alias)
    original=region._exact;changed=False
    def interrupted(stream,length):
        nonlocal changed
        payload=original(stream,length)
        if length==1024**2 and not changed:
            changed=True
            if mode=='cancel':raise KeyboardInterrupt('bounded cancellation')
            if mode=='replace':
                other=tmp_path/'replacement.wav';other.write_bytes(path.read_bytes());other.replace(path)
            if mode=='truncate':path.write_bytes(b'not the source')
            if mode=='alias':
                other=tmp_path/'other.wav';other.write_bytes(path.read_bytes());alias.unlink();alias.symlink_to(other)
        return payload
    monkeypatch.setattr(region,'_exact',interrupted)
    with pytest.raises((PocketError,KeyboardInterrupt)):capture(tmp_path,source)
    assert not list((tmp_path/'store/artifacts').glob('*/record.json'))
    with pytest.raises(PocketError):capture(tmp_path,source)


def test_prepublication_failure_keeps_no_usable_record(tmp_path,monkeypatch):
    path=tmp_path/'source.wav';path.write_bytes(encoded([1,2,3]));source=args_for(path)
    real=region.put_bytes
    def cancelled(*args,**kwargs):
        value=real(*args,**kwargs);path.write_bytes(b'mutated');return value
    monkeypatch.setattr(region,'put_bytes',cancelled)
    with pytest.raises(PocketError):capture(tmp_path,source)
    assert list((tmp_path/'store/artifacts').glob('*/region.wav'))
    assert not list((tmp_path/'store/artifacts').glob('*/record.json'))


@pytest.mark.parametrize('mode', ['riff_size','missing_pad','duplicate_data','compressed','align','truncated'])
def test_malformed_riff_refuses(tmp_path,mode):
    payload=bytearray(encoded([1,2,3,4]))
    if mode=='riff_size':payload[4:8]=struct.pack('<I',len(payload))
    if mode=='missing_pad':del payload[23];payload[4:8]=struct.pack('<I',len(payload)-8)
    if mode=='duplicate_data':payload+=b'data\x02\0\0\0\x01\0';payload[4:8]=struct.pack('<I',len(payload)-8)
    if mode=='compressed':payload[32:34]=struct.pack('<H',3)
    if mode=='align':payload[44:46]=struct.pack('<H',7)
    if mode=='truncated':payload=payload[:-1]
    path=tmp_path/'malformed.wav';path.write_bytes(payload)
    with pytest.raises(PocketError):capture(tmp_path,args_for(path))


def test_replay_rechecks_external_bytes_but_load_remains_independent(tmp_path):
    path=tmp_path/'source.wav';path.write_bytes(encoded([1,2,3,4]));source=args_for(path,1,2)
    result,record=capture(tmp_path,source)
    assert capture(tmp_path,source)[0]==result
    replacement=tmp_path/'same.wav';replacement.write_bytes(path.read_bytes())
    assert capture(tmp_path,args_for(replacement,1,2),'relocated')[0]['artifacts']==result['artifacts']
    path.write_bytes(encoded([1,20,30,4]))
    with pytest.raises(PocketError):capture(tmp_path,source)
    assert region.load_audio_region(result['artifacts']['region'],tmp_path/'store')==record
