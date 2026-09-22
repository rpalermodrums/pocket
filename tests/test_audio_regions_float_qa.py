"""Independent wire/sample and real-interface checks for exact FLOAT32 passages."""
import asyncio
import hashlib
import importlib.util
import io
import json
import struct
import sys

import numpy as np
import pytest
import soundfile as sf
from test_midi_interfaces import cli_call, environment

from pocket_music.artifact_store import put_bytes, put_record, read_bytes
from pocket_music.audio_region_analysis import audio_region_hypotheses, audio_region_query
from pocket_music.audio_regions import audio_region_capture, load_audio_region
from pocket_music.errors import PocketError


def chunk(tag, payload):
    return tag + struct.pack('<I', len(payload)) + payload + (b'\0' if len(payload) % 2 else b'')


def float_wire(samples, channels=2, rate=44100, *, format_length=18, fact=None, extra=b''):
    fmt = struct.pack('<HHIIHH', 3, channels, rate, rate * channels * 4, channels * 4, 32)
    if format_length == 18:
        fmt += b'\0\0'
    body = (b'WAVE' + chunk(b'fact', struct.pack('<I', len(samples) // (channels * 4) if fact is None else fact))
            + chunk(b'JUNK', b'odd') + chunk(b'fmt ', fmt) + extra + chunk(b'data', samples))
    return b'RIFF' + struct.pack('<I', len(body)) + body


def args_for(tmp_path, payload, *, start=0, frames=1):
    path = tmp_path / 'float.wav'
    path.write_bytes(payload)
    return {'store_root': str(tmp_path / 'store'), 'request_id': 'float-capture',
            'source': {'path': str(path), 'expected_sha256': hashlib.sha256(payload).hexdigest(),
                       'start_frame': start, 'frames': frames, 'source_origin': 'user_recording'}}


@pytest.mark.parametrize('format_length', [16, 18])
def test_independent_raw_float_bits_and_libsndfile(tmp_path, format_length):
    # Includes subnormal, signed zero and finite headroom; retain bits, no gain/clipping.
    words = [0, 0x80000000, 1, 0x80000001, 0x40000000, 0xC0400000, 0x3F800000, 0xBF800000]
    samples = struct.pack('<8I', *words)
    args = args_for(tmp_path, float_wire(samples, format_length=format_length), start=1, frames=2)
    path = tmp_path / 'float.wav'
    before = path.stat()
    result = audio_region_capture(**args)
    record = load_audio_region(result['artifacts']['region'], args['store_root'])
    payload = read_bytes(record['crop'], args['store_root'])
    assert len(payload) == 58 + 16
    assert payload[12:20] == b'fmt \x12\0\0\0'
    assert payload[20:38] == struct.pack('<HHIIHHH', 3, 2, 44100, 352800, 8, 32, 0)
    assert payload[38:50] == b'fact\x04\0\0\0\x02\0\0\0'
    assert payload[50:58] == b'data\x10\0\0\0'
    assert payload[58:] == samples[8:24]
    values, rate = sf.read(io.BytesIO(payload), dtype='float32', always_2d=True)
    assert rate == 44100
    assert values.astype('<f4').tobytes() == samples[8:24]
    assert record['projection']['profile'] == 'riff_float32_region_v1'
    assert result['coverage']['profile'] == 'riff_float32_region_v1'
    assert record['sample_proof']['encoding'] == 'interleaved_ieee_float32_le'
    after = path.stat()
    assert (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (
        after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)


@pytest.mark.parametrize('word', [0x7F800000, 0xFF800000, 0x7FC00001, 0x7F800001])
def test_nonfinite_selected_payload_refuses_before_artifacts(tmp_path, word):
    args = args_for(tmp_path, float_wire(struct.pack('<2I', 0, word)))
    with pytest.raises(PocketError):
        audio_region_capture(**args)
    assert not list((tmp_path / 'store' / 'artifacts').glob('*/*'))


def test_nonfinite_outside_crop_is_not_relabelled_finite_original(tmp_path):
    args = args_for(tmp_path, float_wire(struct.pack('<4I', 0, 0, 0x7FC00001, 0)), frames=1)
    result = audio_region_capture(**args)
    record = load_audio_region(result['artifacts']['region'], args['store_root'])
    assert record['original']['sha256'] == args['source']['expected_sha256']
    assert record['original']['retention'] == 'external_identity_only'
    assert read_bytes(record['crop'], args['store_root'])[58:] == b'\0' * 8


def test_source_fmt16_minimum_header_is_distinct_from_canonical_fmt18(tmp_path):
    fmt = struct.pack('<HHIIHH', 3, 2, 44100, 352800, 8, 32)
    body = b'WAVE' + chunk(b'fmt ', fmt) + chunk(b'fact', struct.pack('<I', 1)) + chunk(b'data', b'\0' * 8)
    payload = b'RIFF' + struct.pack('<I', len(body)) + body
    assert len(payload) == 56 + 8
    args = args_for(tmp_path, payload)
    result = audio_region_capture(**args)
    record = load_audio_region(result['artifacts']['region'], args['store_root'])
    assert record['original']['bytes'] == 64
    assert len(read_bytes(record['crop'], args['store_root'])) == 66


@pytest.mark.parametrize('bad', ['count', 'duplicate', 'missing', 'nan_resealed'])
def test_resealed_float_crop_semantics_refuse(tmp_path, bad):
    args = args_for(tmp_path, float_wire(b'\0' * 16), frames=2)
    result = audio_region_capture(**args)
    record = load_audio_region(result['artifacts']['region'], args['store_root'])
    payload = read_bytes(record['crop'], args['store_root'])
    if bad == 'count':
        payload = payload[:46] + struct.pack('<I', 4) + payload[50:]
    elif bad == 'duplicate':
        payload += chunk(b'fact', struct.pack('<I', 2))
        payload = payload[:4] + struct.pack('<I', len(payload) - 8) + payload[8:]
    elif bad == 'missing':
        payload = payload[:38] + payload[50:]
        payload = payload[:4] + struct.pack('<I', len(payload) - 8) + payload[8:]
    else:
        payload = payload[:58] + struct.pack('<I', 0x7FC00001) + payload[62:]
        record['sample_proof']['pcm_sha256'] = hashlib.sha256(payload[58:]).hexdigest()
    record['crop'] = put_bytes(payload, args['store_root'], 'region.wav', 'pocket.audio-region-wav/v1')
    altered = put_record(record, args['store_root'])
    with pytest.raises(PocketError):
        load_audio_region(altered, args['store_root'])


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional MCP environment')
def test_float_actual_cli_mcp_capture_and_peek_composition(tmp_path):
    samples = np.zeros((32000, 2), dtype='<f4')
    samples[4000:4040] = 0.75
    args = args_for(tmp_path, float_wire(samples.tobytes(), rate=8000), start=100, frames=16000)
    captured = audio_region_capture(**args)
    assert cli_call(tmp_path, 'audio_region_capture', args) == captured
    analysis = {'store_root': args['store_root'], 'request_id': 'float-peek',
                'region': {'kind': 'inline', 'source': args['source']},
                'analysis': {'kind': 'peek', 'settings': {'bpm_hint': None, 'beats_per_bar': 4}},
                'attribution': {'actor': 'Independent float QA', 'actor_kind': 'agent',
                                'statement': 'Synthetic samples only.', 'uncertainty': []}}
    direct = audio_region_hypotheses(**analysis)
    composed = audio_region_hypotheses(**{**analysis, 'request_id': 'composed',
        'region': {'kind': 'captured', 'region': captured['artifacts']['region']}})
    assert direct['artifacts'] == composed['artifacts']
    page = {'store_root': args['store_root'], 'hypotheses': direct['artifacts']['hypotheses']}
    assert cli_call(tmp_path, 'audio_region_query', page) == audio_region_query(**page)

    async def run():
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'], env=environment())
        async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
            await session.initialize()
            result = await session.call_tool('audio_region_capture', {**args, 'request_id': 'mcp-float'})
            assert not result.isError
            assert json.loads(result.content[0].text)['artifacts'] == captured['artifacts']
            result = await session.call_tool('audio_region_hypotheses', {**analysis, 'request_id': 'mcp-peek'})
            assert not result.isError
            assert json.loads(result.content[0].text)['artifacts'] == direct['artifacts']
    asyncio.run(asyncio.wait_for(run(), timeout=45))
