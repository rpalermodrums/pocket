# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded exact integer/float sample capture from an externally retained original."""
from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import struct
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .artifact_store import (
    _contained,
    _root,
    canonical_bytes,
    put_bytes,
    put_record,
    read_bytes,
    read_record,
    receipt,
    run_request,
)
from .audio_region_types import AudioRegionSource
from .errors import PocketError
from .material import integer

SCHEMA = 'pocket.audio-region-capture/v1'
CROP_SCHEMA = 'pocket.audio-region-wav/v1'
MAX_SOURCE_BYTES = 4 * 1024**3 - 1
MAX_CROP_BYTES = 32 * 1024**2
BLOCK_BYTES = 1024**2
_CONTEXT = ContextVar('audio_region_cancellation', default=None)
PROJECTION = {'profile': 'riff_pcm_region_v1', 'original_bytes_retained': False,
              'selected_pcm_bytes_exact': True, 'resampled': False,
              'channel_transform': 'none', 'ancillary_chunks_not_copied': True}
FLOAT_PROJECTION = {**PROJECTION, 'profile': 'riff_float32_region_v1'}


@contextmanager
def region_execution_context(*, cancellation_check=None):
    """Scoped cooperative check; no runtime, thread or process is created."""
    if cancellation_check is not None and not callable(cancellation_check):
        raise PocketError('Region cancellation check must be callable')
    token = _CONTEXT.set(cancellation_check)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def _cancel():
    check = _CONTEXT.get()
    if check is not None:
        check()


def _fields(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise PocketError(f'{label} has missing or unexpected fields')


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise PocketError('Expected lowercase SHA256 identity')
    return value


def _source(value):
    _fields(value, {'path', 'expected_sha256', 'start_frame', 'frames', 'source_origin'}, 'region source')
    if not isinstance(value['path'], str) or not value['path'] or len(value['path']) > 4096 or not Path(value['path']).is_absolute():
        raise PocketError('Region source requires an absolute local path')
    _sha(value['expected_sha256'])
    integer(value['start_frame'], 'start_frame', 0, 2**53)
    integer(value['frames'], 'frames', 1, 20 * 96000)
    if value['source_origin'] not in ('independently_acquired', 'user_recording'):
        raise PocketError('Unknown independently acquired source origin')


def _stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _exact(stream, length):
    if length > BLOCK_BYTES:
        raise PocketError('Internal PCM parser read exceeds bound')
    payload = stream.read(length)
    if len(payload) != length:
        raise PocketError('Truncated RIFF source or short read')
    return payload


def _parse(stream, size, *, check_cancel=True):
    """Read only bounded headers; selected PCM is copied in a separate full hash pass."""
    integer(size, 'RIFF bytes', 44, MAX_SOURCE_BYTES)
    stream.seek(0)
    header = _exact(stream, 12)
    if header[:4] != b'RIFF' or header[8:] != b'WAVE' or struct.unpack('<I', header[4:8])[0] + 8 != size:
        raise PocketError('Expected exact-sized little-endian RIFF/WAVE')
    fmt, data, position, chunks = None, None, 12, 0
    facts = []
    while position < size:
        if check_cancel:
            _cancel()
        chunks += 1
        if chunks > 4096 or position + 8 > size:
            raise PocketError('RIFF chunk bounds exceeded')
        stream.seek(position)
        tag, length = struct.unpack('<4sI', _exact(stream, 8))
        start = position + 8
        end = start + length
        position = end + (length & 1)
        if position > size:
            raise PocketError('RIFF chunk extends beyond source')
        if tag == b'fmt ':
            if fmt is not None or length not in (16, 18):
                raise PocketError('Expected one bounded PCM format chunk')
            payload = _exact(stream, length)
            code, channels, rate, byte_rate, align, bits = struct.unpack('<HHIIHH', payload[:16])
            if ((code, bits) not in ((1, 16), (1, 24), (3, 32))
                    or length == 18 and payload[16:] != b'\0\0'):
                raise PocketError('Only ordinary PCM16/PCM24 or IEEE FLOAT32 RIFF is qualified')
            integer(channels, 'channels', 1, 8)
            integer(rate, 'sample rate', 8000, 96000)
            if align != channels * (bits // 8) or byte_rate != rate * align:
                raise PocketError('PCM block alignment or byte rate mismatch')
            fmt = {'sample_rate': rate, 'channels': channels, 'bits': bits, 'block_align': align}
            if code == 3:
                fmt['format_code'] = 3
        elif tag == b'fact':
            # PCM historically treats fact as ancillary; preserve that behavior.
            facts.append(struct.unpack('<I', _exact(stream, 4))[0] if length == 4 else None)
        elif tag == b'data':
            if data is not None:
                raise PocketError('Multiple PCM data chunks are ambiguous')
            data = (start, length)
    if fmt is None or data is None or data[1] == 0 or data[1] % fmt['block_align']:
        raise PocketError('Missing or nonintegral PCM frame payload')
    frames = data[1] // fmt['block_align']
    if fmt.get('format_code') == 3 and facts != [frames]:
        raise PocketError('FLOAT32 requires one four-byte fact chunk matching frame count')
    if frames > fmt['sample_rate'] * 7200:
        raise PocketError('Source exceeds two-hour capture profile')
    return {**fmt, 'frames': frames, 'data_offset': data[0], 'data_bytes': data[1]}


def _wave(pcm, metadata):
    align, rate = metadata['block_align'], metadata['sample_rate']
    code = metadata.get('format_code', 1)
    fmt = struct.pack('<HHIIHH', code, metadata['channels'], rate, rate * align, align, metadata['bits'])
    if code == 3:
        fmt += b'\0\0'
    body = b'WAVEfmt ' + struct.pack('<I', len(fmt)) + fmt
    if code == 3:
        body += b'fact' + struct.pack('<II', 4, len(pcm) // align)
    body += b'data' + struct.pack('<I', len(pcm)) + pcm
    if len(pcm) & 1:
        body += b'\0'
    return b'RIFF' + struct.pack('<I', len(body)) + body


def _finite_float32(pcm, *, check_cancel=True):
    """Inspect exponent bits without decoding, rounding or normalizing sample bytes."""
    if len(pcm) % 4:
        raise PocketError('Nonintegral FLOAT32 sample payload')
    for offset in range(0, len(pcm), BLOCK_BYTES):
        if check_cancel:
            _cancel()
        for (bits,) in struct.iter_unpack('<I', pcm[offset:offset + BLOCK_BYTES]):
            if bits & 0x7f800000 == 0x7f800000:
                raise PocketError('Selected FLOAT32 samples contain NaN or infinity')


def _capture(source):
    """Hash one stable descriptor and spool only the selected interleaved bytes."""
    _source(source)
    _cancel()
    supplied = Path(source['path'])
    try:
        resolved = supplied.resolve(strict=True)
        initial_path = resolved.stat()
        if not stat.S_ISREG(initial_path.st_mode):
            raise PocketError('Region source must be a regular file')
        integer(initial_path.st_size, 'source bytes', 44, MAX_SOURCE_BYTES)
        with resolved.open('rb') as stream, tempfile.TemporaryFile() as cropped:
            before = os.fstat(stream.fileno())
            if _stamp(before) != _stamp(initial_path):
                raise PocketError('Source changed before capture')
            metadata = _parse(stream, before.st_size)
            if _stamp(os.fstat(stream.fileno())) != _stamp(before):
                raise PocketError('Source changed during metadata parsing')
            frames, start_frame = source['frames'], source['start_frame']
            if frames > 20 * metadata['sample_rate'] or start_frame + frames > metadata['frames']:
                raise PocketError('Crop exceeds source or twenty-second frame bound')
            length = frames * metadata['block_align']
            if length > MAX_CROP_BYTES:
                raise PocketError('Crop exceeds 32 MiB PCM bound')
            low = metadata['data_offset'] + start_frame * metadata['block_align']
            high = low + length
            hashed, position = hashlib.sha256(), 0
            stream.seek(0)
            while position < before.st_size:
                _cancel()
                block = _exact(stream, min(BLOCK_BYTES, before.st_size - position))
                hashed.update(block)
                left, right = max(low, position), min(high, position + len(block))
                if left < right:
                    cropped.write(block[left - position:right - position])
                position += len(block)
            if hashed.hexdigest() != source['expected_sha256']:
                raise PocketError('Original source SHA256 mismatch')
            if (_stamp(os.fstat(stream.fileno())) != _stamp(before)
                    or supplied.resolve(strict=True) != resolved or _stamp(resolved.stat()) != _stamp(before)):
                raise PocketError('Original source changed or was replaced during capture')
            if cropped.tell() != length:
                raise PocketError('Incomplete selected PCM capture')
            cropped.seek(0)
            pcm = cropped.read(MAX_CROP_BYTES + 1)
            _cancel()
            if len(pcm) != length:
                raise PocketError('Incomplete crop temporary bytes')
        floating = metadata.get('format_code') == 3
        if floating:
            _finite_float32(pcm)
        payload = _wave(pcm, metadata)
        decoded = _parse(io.BytesIO(payload), len(payload))
        if payload[decoded['data_offset']:decoded['data_offset'] + decoded['data_bytes']] != pcm or decoded['frames'] != frames:
            raise PocketError('Canonical crop sample validation failed')
        original = {'sha256': source['expected_sha256'], 'bytes': before.st_size, 'frames': metadata['frames'],
                    'sample_rate': metadata['sample_rate'], 'channels': metadata['channels'], 'format': 'WAV',
                    'subtype': 'FLOAT' if floating else f'PCM_{metadata["bits"]}', 'source_origin': source['source_origin'],
                    'retention': 'external_identity_only'}
        body = {'schema': SCHEMA, 'original': original,
                'interval': {'start_frame': start_frame, 'end_frame_exclusive': start_frame + frames},
                'mapping': {'kind': 'integer_frame_offset', 'crop_start_frame': 0,
                            'original_start_frame': start_frame, 'sample_rate': metadata['sample_rate'], 'frame_count': frames},
                'sample_proof': {'encoding': 'interleaved_ieee_float32_le' if floating else 'interleaved_signed_le_pcm', 'bytes_per_sample': metadata['bits'] // 8,
                                 'block_align': metadata['block_align'], 'pcm_sha256': hashlib.sha256(pcm).hexdigest(), 'pcm_bytes': length},
                'projection': dict(FLOAT_PROJECTION if floating else PROJECTION)}
        return body, payload, (supplied, resolved, _stamp(before))
    except (OSError, ValueError, RuntimeError) as error:
        if isinstance(error, PocketError):
            raise
        raise PocketError('Cannot stably capture local PCM region') from error


def _binding_current(binding):
    supplied, resolved, stamp = binding
    try:
        if supplied.resolve(strict=True) != resolved or _stamp(resolved.stat()) != stamp:
            raise PocketError('Original source changed before publication')
    except OSError as error:
        raise PocketError('Original source unavailable before publication') from error


def _bounded_handle(handle, store_root, maximum):
    # Validate canonical handle paths before stat/read, without loading an oversized artifact.
    _fields(handle, {'schema', 'artifact_uri', 'sha256', 'artifact_schema'}, 'artifact handle')
    sha = _sha(handle['sha256'])
    uri = handle['artifact_uri']
    if (handle['schema'] != 'pocket.artifact-handle/v1' or not isinstance(uri, str)
            or not re.fullmatch(rf'artifacts/{sha}/[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}', uri)):
        raise PocketError('Expected canonical artifact handle')
    try:
        if _contained(_root(store_root), uri).stat().st_size > maximum:
            raise PocketError('Region artifact exceeds bounded size')
    except OSError as error:
        raise PocketError('Region artifact unavailable') from error


def load_audio_region(handle, store_root):
    """Validate retained crop and source mapping without accessing the external original."""
    _bounded_handle(handle, store_root, 65536)
    record = read_record(handle, store_root, SCHEMA)
    _fields(record, {'schema', 'original', 'crop', 'interval', 'mapping', 'sample_proof', 'projection'}, 'region capture')
    original = record['original']
    _fields(original, {'sha256', 'bytes', 'frames', 'sample_rate', 'channels', 'format', 'subtype', 'source_origin', 'retention'}, 'original identity')
    _sha(original['sha256'])
    integer(original['bytes'], 'original bytes', 44, MAX_SOURCE_BYTES)
    rate = integer(original['sample_rate'], 'original sample rate', 8000, 96000)
    channels = integer(original['channels'], 'original channels', 1, 8)
    total = integer(original['frames'], 'original frames', 1, rate * 7200)
    if (original['format'] != 'WAV' or original['subtype'] not in ('PCM_16', 'PCM_24', 'FLOAT')
            or original['source_origin'] not in ('independently_acquired', 'user_recording')
            or original['retention'] != 'external_identity_only'):
        raise PocketError('Unknown original PCM identity/retention profile')
    floating = original['subtype'] == 'FLOAT'
    bits = 32 if floating else int(original['subtype'][4:])
    header_bytes = 56 if floating else 44
    projection = FLOAT_PROJECTION if floating else PROJECTION
    if total * channels * (bits // 8) + header_bytes > original['bytes']:
        raise PocketError('Original frame payload cannot fit declared bytes')
    _fields(record['interval'], {'start_frame', 'end_frame_exclusive'}, 'region interval')
    start = integer(record['interval']['start_frame'], 'region start', 0, total - 1)
    end = integer(record['interval']['end_frame_exclusive'], 'region end', start + 1, total)
    frames = end - start
    if frames > 20 * rate:
        raise PocketError('Retained region exceeds twenty seconds')
    expected_mapping = {'kind': 'integer_frame_offset', 'crop_start_frame': 0, 'original_start_frame': start,
                        'sample_rate': rate, 'frame_count': frames}
    if canonical_bytes(record['mapping']) != canonical_bytes(expected_mapping) or canonical_bytes(record['projection']) != canonical_bytes(projection):
        raise PocketError('Region mapping or projection mismatch')
    _bounded_handle(record['crop'], store_root, MAX_CROP_BYTES + (58 if floating else 45))
    if record['crop']['artifact_schema'] != CROP_SCHEMA:
        raise PocketError('Region crop has wrong byte schema')
    payload = read_bytes(record['crop'], store_root)
    metadata = _parse(io.BytesIO(payload), len(payload), check_cancel=False)
    pcm = payload[metadata['data_offset']:metadata['data_offset'] + metadata['data_bytes']]
    if ((metadata['frames'], metadata['sample_rate'], metadata['channels'], metadata['bits']) != (frames, rate, channels, bits)
            or (metadata.get('format_code') == 3) != floating):
        raise PocketError('Retained crop header differs from original frame mapping')
    if len(pcm) > MAX_CROP_BYTES or payload != _wave(pcm, metadata):
        raise PocketError('Retained crop is not canonical exact PCM projection')
    if floating:
        _finite_float32(pcm, check_cancel=False)
    proof = {'encoding': 'interleaved_ieee_float32_le' if floating else 'interleaved_signed_le_pcm', 'bytes_per_sample': bits // 8,
             'block_align': channels * (bits // 8), 'pcm_sha256': hashlib.sha256(pcm).hexdigest(), 'pcm_bytes': len(pcm)}
    if canonical_bytes(record['sample_proof']) != canonical_bytes(proof):
        raise PocketError('Retained crop sample proof mismatch')
    return record


def audio_region_capture(*, store_root: str, request_id: str, source: AudioRegionSource) -> dict:
    """Capture exact PCM16/24 or finite FLOAT32 samples; original bytes stay external."""
    _source(source)
    executed = False
    def work():
        nonlocal executed
        executed = True
        body, payload, binding = _capture(source)
        _cancel()
        _binding_current(binding)
        crop = put_bytes(payload, store_root, 'region.wav', CROP_SCHEMA)
        _cancel()
        _binding_current(binding)
        handle = put_record({**body, 'crop': crop}, store_root)
        load_audio_region(handle, store_root)
        return receipt(request_id, artifacts={'region': handle},
            change_summary={'frames': source['frames'], 'original_start_frame': source['start_frame']},
            coverage={'profile': body['projection']['profile'], 'original_bytes_retained': False,
                      'selected_pcm_bytes_exact': True, 'native_execution': False, 'human_listening': 'not_performed'},
            uncertainty=['Full original bytes remain external; crop identity does not prove musical interpretation.'])
    result = run_request(store_root, request_id, 'audio_region_capture', {'source': source}, work)
    retained = load_audio_region(result['artifacts']['region'], store_root)
    if not executed:
        body, payload, binding = _capture(source)
        _binding_current(binding)
        if {**body, 'crop': retained['crop']} != retained or hashlib.sha256(payload).hexdigest() != retained['crop']['sha256']:
            raise PocketError('Replay source differs from retained capture')
    return result
