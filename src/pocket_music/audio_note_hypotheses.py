# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit optional ONNX note hypotheses with artifact-only semantic replay."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Literal

import numpy as np
import scipy

from .artifact_store import (
    _verify_handles,
    canonical_bytes,
    digest,
    put_bytes,
    put_record,
    read_bytes,
    read_record,
    receipt,
    run_request,
)
from .audio_hypotheses import _attribution, _fields, prepare_hypothesis_input
from .audio_hypothesis_types import AudioHypothesisAttribution, AudioHypothesisSource
from .audio_note_projection import SETTINGS, TRANSFORM, project_note_arrays
from .audio_note_types import AudioNoteModel, AudioNoteModelDeclaration, AudioNoteSettings
from .audio_pulse_hypotheses import _CONTEXT, _cancel, _file, _sha, model_execution_context
from .audio_regions import _parse
from .errors import PocketError

__all__ = [
    'audio_note_hypotheses',
    'audio_note_model_inspect',
    'load_audio_note_model',
    'load_note_hypotheses',
    'model_execution_context',
    'prepare_note_input',
    'prepare_note_model',
    'validate_note_declaration',
    'validate_note_source_bytes',
]
KNOWN_WEIGHT = '2c3c1d144bfa61ad236e92e169c13535c880469a12a047d4e73451f2c059a0ec'
SCHEMA = 'pocket.audio-note-hypotheses/v1'
MODEL_SCHEMA = 'pocket.audio-note-model/v1'
ANALYSIS_SCHEMA = 'pocket.audio-note-analysis/v1'
QUAL_SCHEMA = 'pocket.audio-note-qualification/v1'
QUAL_SCOPE = 'Two synthetic2-second mono22050 silence/tone finite/repeat cases; no format/rate/long-input or musical accuracy certification'
PROFILE_SCHEMA = 'pocket.audio-note-runtime-profile/v1'
ARRAY_SCHEMA = 'pocket.model-activations-float32le/v1'
PROJECTION_SHA256 = 'a1f962248b7ba2a735c1bf8b3da090dc3e04b5a66f956861461b088ef11ba70e'
VENDOR_DECODER_SHA256 = '9c813509acf57ed9b902d2b9fcd9f8118b2c5ffe568a06df9cfa60f5c5ee2572'
PROJECTION_REPLAY = 'Complete exact ledger replay in current base runtime; no cross-version numerical equivalence claim'
LIMITATIONS = ['Uncalibrated note hypotheses; no human listening, voice, tuning, instrument, MIDI or native qualification.',
               'Vendor timestamps are float estimates; outward source envelopes are not measured acoustic onset intervals.',
               'Retained resampling bytes are verified; base replay recomputes decoding and the complete note ledger, not optional soxr execution.']
COVERAGE = {'provider': 'audio-note-hypotheses-v1', 'native_execution': False, 'learned_models': ['basic_pitch_onnx_cpu_v1'],
            'transcription': 'uncertain_note_hypotheses', 'human_listening': 'not_performed', 'musical_accuracy_qualified': False}


def validate_note_declaration(declaration):
    """Pure strict declaration validation; never inspect external files."""
    _fields(declaration, {'adapter', 'executable', 'weights', 'expected_profile'}, 'note declaration')
    if declaration['adapter'] != 'basic_pitch_onnx_cpu_v1': raise PocketError('Unknown note adapter')
    for key in ('executable', 'weights'):
        value = declaration[key]; _fields(value, {'path', 'sha256'}, key); _sha(value['sha256'])
        if not isinstance(value['path'], str) or not Path(value['path']).is_absolute() or len(value['path']) > 4096:
            raise PocketError('Explicit bounded absolute model path required')
    if declaration['weights']['sha256'] != KNOWN_WEIGHT: raise PocketError('Only the qualified known ONNX bytes are supported')
    if declaration['expected_profile'] is not None: _sha(declaration['expected_profile'])
    return copy.deepcopy(declaration)


def _declaration(value):
    validate_note_declaration(value)
    executable, binding = _file(value['executable'], 128 * 1024**2)
    if executable[:4] not in (b'\x7fELF', b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf') or not os.access(binding['resolved_path'], os.X_OK):
        raise PocketError('Declared runtime is not executable')
    weights, weight_binding = _file(value['weights'], 1024 * 1024)
    return weights, {'executable': binding, 'weights': weight_binding}


def _decoded(payload, source):
    _fields(source, {'sha256', 'frames', 'sample_rate', 'channels', 'format', 'subtype', 'start_frame', 'end_frame_exclusive', 'source_origin'}, 'note source')
    if not isinstance(payload, bytes) or len(payload) > 256 * 1024**2 or hashlib.sha256(payload).hexdigest() != source['sha256']:
        raise PocketError('Source byte identity mismatch')
    if any(type(source[key]) is not int for key in ('frames', 'sample_rate', 'channels', 'start_frame', 'end_frame_exclusive')):
        raise PocketError('Strict source frame integers required')
    meta = _parse(io.BytesIO(payload), len(payload), check_cancel=False)
    floating = meta.get('format_code') == 3
    subtype = 'FLOAT' if floating else 'PCM_16'
    if (meta['bits'] != (32 if floating else 16) or source['subtype'] != subtype or source['format'] != 'WAV'
            or meta['channels'] not in (1, 2) or meta['sample_rate'] not in (22050, 44100, 48000)
            or any(meta[key] != source[key] for key in ('frames', 'sample_rate', 'channels'))):
        raise PocketError('Qualified note source requires mono/stereo PCM16/FLOAT32 WAV at22050/44100/48000Hz')
    start, end, rate = source['start_frame'], source['end_frame_exclusive'], source['sample_rate']
    if not 0 <= start < end <= source['frames'] or not 2 * rate <= end - start <= 20 * rate or source['source_origin'] not in ('independently_acquired', 'user_recording'):
        raise PocketError('Qualified note crop requires2–20 seconds within source')
    lo = meta['data_offset'] + start * meta['block_align']; hi = meta['data_offset'] + end * meta['block_align']
    values = np.frombuffer(payload[lo:hi], dtype='<f4' if floating else '<i2').reshape(-1, source['channels'])
    values = values.astype(np.float32)
    if not floating: values = values / np.float32(32768)
    if not np.isfinite(values).all(): raise PocketError('Nonfinite source sample')
    with np.errstate(over='raise', invalid='raise'):
        try: mono = values.mean(axis=1, dtype=np.float32)
        except FloatingPointError as error: raise PocketError('Nonfinite arithmetic downmix') from error
    if not np.isfinite(mono).all(): raise PocketError('Nonfinite arithmetic downmix')
    return mono.astype('<f4', copy=False).tobytes()


def validate_note_source_bytes(payload, source):
    """Validate exact retained source/crop without optional runtime or source paths."""
    _decoded(payload, source)
    return copy.deepcopy(source)


def prepare_note_input(*, source: AudioHypothesisSource, settings: AudioNoteSettings,
                       attribution: AudioHypothesisAttribution, store_root: str):
    if canonical_bytes(settings) != canonical_bytes(SETTINGS): raise PocketError('Unsupported note settings')
    prepared = prepare_hypothesis_input(source=source, settings={'bpm_hint': None, 'beats_per_bar': 4}, attribution=attribution, store_root=store_root)
    validate_note_source_bytes(read_bytes(prepared['source_handle'], store_root), prepared['source_metadata'])
    prepared['settings'] = copy.deepcopy(settings)
    return prepared


def _owned_bytes(path, maximum):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > maximum: raise PocketError('Owned file bound')
            data = stream.read(maximum + 1); after = os.fstat(stream.fileno())
        stamp = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if len(data) > maximum or stamp(before) != stamp(after) or stamp(after) != stamp(os.stat(path, follow_symlinks=False)):
            raise PocketError('Owned file changed during read')
        return data
    except OSError as error:
        raise PocketError('Owned runner file unavailable or unsafe') from error


def _structure(value):
    pending = [(value, 0)]; count = 0
    while pending:
        item, depth = pending.pop(); count += 1
        if count > 100000 or depth > 32: raise PocketError('Runner JSON structure bound')
        if isinstance(item, dict): pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list): pending.extend((child, depth + 1) for child in item)


def _binary(directory, descriptor):
    _fields(descriptor, {'file', 'sha256', 'bytes', 'shape', 'dtype'}, 'runner binary')
    name = descriptor['file']
    allowed = {'resampled.f32'} | {f'{prefix}-{repeat}-{key}.f32' for prefix in ('source', 'qualification-silence', 'qualification-tone') for repeat in (0, 1) for key in ('note', 'onset', 'contour')}
    if not isinstance(name, str) or name not in allowed:
        raise PocketError('Invalid runner binary filename')
    if descriptor['dtype'] != 'float32le' or type(descriptor['bytes']) is not int or not 4 <= descriptor['bytes'] <= 16 * 1024**2:
        raise PocketError('Runner binary bounds')
    shape = descriptor['shape']
    if not isinstance(shape, list) or not 1 <= len(shape) <= 3 or any(type(n) is not int or not 1 <= n <= 441000 for n in shape) or math.prod(shape) * 4 != descriptor['bytes']:
        raise PocketError('Runner binary shape bounds')
    path = Path(directory) / name
    if path.is_symlink() or not path.is_file(): raise PocketError('Runner binary must be owned regular file')
    payload = _owned_bytes(path, 16 * 1024**2)
    if hashlib.sha256(payload).hexdigest() != descriptor['sha256']: raise PocketError('Runner binary digest mismatch')
    if len(payload) != descriptor['bytes']: raise PocketError('Runner binary byte count mismatch')
    values = np.frombuffer(payload, dtype='<f4').reshape(shape)
    if not np.isfinite(values).all(): raise PocketError('Nonfinite runner binary')
    return payload


def _runner(declaration, qualification, weights_path, source=None):
    _cancel(); _, before = _declaration(declaration)
    runner = Path(__file__).with_name('audio_note_runner.py'); runner_bytes = runner.read_bytes(); runner_hash = hashlib.sha256(runner_bytes).hexdigest()
    helper = runner.with_name('audio_model_runner.py'); helper_bytes = helper.read_bytes()
    with tempfile.TemporaryDirectory(prefix='pocket-note-') as directory:
        directory = Path(directory)
        source_request = None
        if source is not None:
            mono, rate = source
            path = directory / 'mono.f32'; path.write_bytes(mono)
            source_request = {'mono_path': str(path), 'mono_sha256': hashlib.sha256(mono).hexdigest(), 'frames': len(mono) // 4, 'sample_rate': rate}
        request = {'weights_path': str(weights_path), 'weights_sha256': KNOWN_WEIGHT, 'expected_profile': declaration['expected_profile'], 'qualification': qualification, 'source': source_request}
        request_path, response_path = directory / 'request.json', directory / 'response.json'
        request_path.write_bytes(canonical_bytes(request))
        environment = {key: value for key, value in os.environ.items() if key not in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP')}
        environment.update(PYTHONDONTWRITEBYTECODE='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
        process = None
        try:
            # Launch the declared venv entry path, not its resolved base interpreter:
            # this preserves the explicit isolated package environment.
            process = subprocess.Popen([declaration['executable']['path'], '-I', '-B', str(runner), str(request_path), str(response_path)], env=environment, pass_fds=_CONTEXT.get()[0], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 60
            while process.poll() is None:
                _cancel()
                if time.monotonic() >= deadline: raise PocketError('Owned note runner exceeded60 seconds; no result published')
                time.sleep(.05)
            if process.returncode != 0 or not response_path.is_file() or response_path.is_symlink() or response_path.stat().st_size > 8 * 1024**2:
                raise PocketError('Optional note runner failed or exceeded response bounds')
            payload = _owned_bytes(response_path, 8 * 1024**2)
            if len(payload) > 8 * 1024**2: raise PocketError('Runner response bound')
            response = json.loads(payload)
            _structure(response)
            if not isinstance(response, dict) or response.get('status') != 'ok':
                reason = str(response.get('error', 'malformed response'))[:2048] if isinstance(response, dict) else 'malformed response'
                raise PocketError('Optional note runner refused: ' + reason)
            _fields(response, {'status', 'result'}, 'runner response'); result = response['result']
            _fields(result, {'profile', 'profile_sha256', 'qualification', 'analysis'}, 'runner result')
            if not isinstance(result['profile'], dict) or digest(result['profile']) != result['profile_sha256'] or declaration['expected_profile'] is not None and declaration['expected_profile'] != result['profile_sha256']:
                raise PocketError('Note runtime profile mismatch')
            expected_runner = {'sha256': runner_hash, 'bytes': len(runner_bytes)}
            expected_helper = {'sha256': hashlib.sha256(helper_bytes).hexdigest(), 'bytes': len(helper_bytes)}
            if result['profile'].get('runner') != expected_runner or result['profile'].get('fingerprint_helper') != expected_helper:
                raise PocketError('Runner-reported code identity differs from executed bytes')
            _profile(result['profile'], {**declaration, 'expected_profile': result['profile_sha256']}, before)
            binaries = {}; total = 0
            def collect(value):
                nonlocal total
                if isinstance(value, dict):
                    if set(value) == {'file', 'sha256', 'bytes', 'shape', 'dtype'}:
                        raw = _binary(directory, value); total += len(raw)
                        if total > 64 * 1024**2 or value['file'] in binaries: raise PocketError('Runner binary aggregate bound/duplicate')
                        binaries[value['file']] = raw
                    else:
                        for child in value.values(): collect(child)
                elif isinstance(value, list):
                    for child in value: collect(child)
            collect(result['qualification']); collect(result['analysis'])
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
            raise PocketError('Optional note runner unavailable or malformed') from error
        finally:
            if process is not None and process.poll() is None: process.kill(); process.wait(timeout=5)
    _cancel(); _, after = _declaration(declaration)
    if before != after or runner.read_bytes() != runner_bytes or helper.read_bytes() != helper_bytes: raise PocketError('Note execution binding changed')
    return result, before, binaries


def _retain(value, binaries, root):
    if isinstance(value, dict):
        if set(value) == {'file', 'sha256', 'bytes', 'shape', 'dtype'}:
            handle = put_bytes(binaries[value['file']], root, value['file'], ARRAY_SCHEMA)
            return {key: copy.deepcopy(value[key]) for key in ('shape', 'dtype')} | {'handle': handle}
        return {key: _retain(child, binaries, root) for key, child in value.items()}
    if isinstance(value, list): return [_retain(child, binaries, root) for child in value]
    return copy.deepcopy(value)


def _array(descriptor, root):
    _fields(descriptor, {'shape', 'dtype', 'handle'}, 'retained array')
    shape = descriptor['shape']
    if descriptor['dtype'] != 'float32le' or not isinstance(shape, list) or not 1 <= len(shape) <= 3 or any(type(n) is not int or not 1 <= n <= 441000 for n in shape) or math.prod(shape) * 4 > 16 * 1024**2:
        raise PocketError('Retained array shape bounds')
    if descriptor['handle'].get('artifact_schema') != ARRAY_SCHEMA: raise PocketError('Unexpected activation schema')
    data = read_bytes(descriptor['handle'], root)
    if len(data) != math.prod(shape) * 4: raise PocketError('Retained activation byte count mismatch')
    value = np.frombuffer(data, dtype='<f4').reshape(shape)
    if not np.isfinite(value).all(): raise PocketError('Nonfinite activation')
    return value


def _windows(raw, root, *, source=False):
    fields = {'resampled_frames', 'arrays', 'two_repeats_sample_exact'} | ({'mono_sha256', 'resampled'} if source else set())
    _fields(raw, fields, 'note raw proof')
    if type(raw['resampled_frames']) is not int or not 44100 <= raw['resampled_frames'] <= 441000 or raw['two_repeats_sample_exact'] is not True:
        raise PocketError('Invalid repeated model proof')
    _fields(raw['arrays'], {f'{repeat}:{name}' for repeat in (0, 1) for name in ('note', 'onset', 'contour')}, 'raw windows')
    result = {}
    for name in ('note', 'onset', 'contour'):
        first, second = (_array(raw['arrays'][f'{i}:{name}'], root) for i in (0, 1))
        if first.shape != second.shape or first.tobytes() != second.tobytes(): raise PocketError('Retained model repeats disagree')
        result[name] = first
    return result


def _qualify(proof, root):
    _fields(proof, {'profile', 'cases'}, 'qualification proof')
    if proof['profile'] != 'synthetic_onnx_cpu_v1': raise PocketError('Unexpected qualification profile')
    _fields(proof['cases'], {'silence', 'tone'}, 'synthetic cases')
    for name, case in proof['cases'].items():
        _fields(case, {'input_sha256', 'resampled_frames', 'arrays', 'two_repeats_sample_exact'}, 'synthetic case')
        audio = np.zeros(44100, dtype=np.float32)
        if name == 'tone':
            t = np.arange(44100, dtype=np.float64) / 22050
            audio = (.5 * np.sin(2 * np.pi * 440 * t) * ((t >= .25) & (t < 1.75))).astype(np.float32)
        if case['input_sha256'] != hashlib.sha256(audio.tobytes()).hexdigest() or case['resampled_frames'] != 44100:
            raise PocketError('Synthetic input mismatch')
        raw = {key: value for key, value in case.items() if key != 'input_sha256'}
        project_note_arrays(_windows(raw, root), {'start_frame': 0, 'end_frame_exclusive': 44100, 'sample_rate': 22050}, 44100)


def _profile(prof, declaration, binding):
    _fields(prof, {'adapter', 'python', 'platform', 'machine', 'prefix', 'base_prefix', 'launch_executable', 'executable', 'packages', 'stdlib', 'runner', 'fingerprint_helper', 'scope'}, 'runtime profile')
    for key in ('python', 'platform', 'machine', 'prefix', 'base_prefix', 'launch_executable'):
        if not isinstance(prof[key], str) or not 1 <= len(prof[key]) <= 4096: raise PocketError('Invalid runtime descriptor')
    if prof['adapter'] != declaration['adapter'] or prof['launch_executable'] != declaration['executable']['path'] or prof['scope'] != 'Listed distribution files and Python stdlib; excludes unenumerated OS shared libraries.':
        raise PocketError('Runtime launch/scope mismatch')
    def file(value):
        _fields(value, {'sha256', 'bytes'}, 'runtime file fingerprint'); _sha(value['sha256'])
        if type(value['bytes']) is not int or not 0 <= value['bytes'] <= 2 * 1024**3: raise PocketError('Runtime file byte bound')
    for key in ('executable', 'runner', 'fingerprint_helper'): file(prof[key])
    if prof['executable']['sha256'] != declaration['executable']['sha256']: raise PocketError('Runtime executable digest mismatch')
    if not isinstance(prof['stdlib'], dict) or not 1 <= len(prof['stdlib']) <= 10000: raise PocketError('Runtime stdlib bound')
    for key, value in prof['stdlib'].items():
        if not isinstance(key, str) or not 1 <= len(key) <= 4096: raise PocketError('Runtime relative key bound')
        file(value)
    _fields(prof['packages'], {'onnxruntime', 'numpy', 'soxr', 'packaging', 'protobuf', 'flatbuffers'}, 'runtime packages')
    for name, package in prof['packages'].items():
        _fields(package, {'version', 'files'}, 'runtime package')
        if not isinstance(package['version'], str) or not 1 <= len(package['version']) <= 100 or not isinstance(package['files'], dict) or not 1 <= len(package['files']) <= 15000:
            raise PocketError('Runtime package bound')
        for key, value in package['files'].items():
            if not isinstance(key, str) or not 1 <= len(key) <= 4096: raise PocketError('Runtime package key bound')
            file(value)
        if name in ('onnxruntime', 'soxr') and package['version'] != {'onnxruntime': '1.26.0', 'soxr': '1.1.0'}[name]: raise PocketError('Unsupported runtime version')
    _fields(binding, {'executable', 'weights'}, 'model binding')
    for name in ('executable', 'weights'):
        value = binding[name]; _fields(value, {'supplied_path', 'resolved_path', 'stamp', 'sha256'}, 'file binding')
        if value['supplied_path'] != declaration[name]['path'] or value['sha256'] != declaration[name]['sha256'] or not isinstance(value['resolved_path'], str) or not Path(value['resolved_path']).is_absolute(): raise PocketError('Declared file binding mismatch')
        if not isinstance(value['stamp'], list) or len(value['stamp']) != 5 or any(type(n) is not int or n < 0 for n in value['stamp']): raise PocketError('Invalid file binding stamp')


def load_audio_note_model(handle, store_root, *, require_qualified=True):
    """Validate complete retained proof without optional imports or external paths."""
    _verify_handles(handle, store_root)
    try:
        model = read_record(handle, store_root, MODEL_SCHEMA)
        _fields(model, {'schema', 'declaration', 'binding', 'profile', 'profile_sha256', 'weights', 'qualification', 'qualified', 'qualification_scope'}, 'note model')
        declaration = validate_note_declaration(model['declaration'])
        if model['qualification_scope'] != QUAL_SCOPE: raise PocketError('Qualification scope mismatch')
        if declaration['expected_profile'] != model['profile_sha256'] or model['weights']['sha256'] != KNOWN_WEIGHT:
            raise PocketError('Note model declaration mismatch')
        weights_payload = read_bytes(model['weights'], store_root)
        if hashlib.sha256(weights_payload).hexdigest() != KNOWN_WEIGHT: raise PocketError('Known weight bytes differ')
        profile = read_record(model['profile'], store_root, PROFILE_SCHEMA)
        _fields(profile, {'schema', 'profile', 'profile_sha256'}, 'profile record')
        _profile(profile['profile'], declaration, model['binding'])
        if model['binding']['weights']['stamp'][2] != len(weights_payload) or model['binding']['executable']['stamp'][2] != profile['profile']['executable']['bytes']:
            raise PocketError('Retained file binding size mismatch')
        if digest(profile['profile']) != model['profile_sha256'] or profile['profile_sha256'] != model['profile_sha256'] or profile['profile'].get('adapter') != declaration['adapter']:
            raise PocketError('Note runtime profile mismatch')
        if type(model['qualified']) is not bool or model['qualified'] != (model['qualification'] is not None): raise PocketError('Qualification flag mismatch')
        if model['qualification'] is not None:
            proof = read_record(model['qualification'], store_root, QUAL_SCHEMA)
            _fields(proof, {'schema', 'profile', 'weights', 'qualification'}, 'note qualification')
            if proof['profile'] != model['profile'] or proof['weights'] != model['weights']: raise PocketError('Qualification model binding mismatch')
            _qualify(proof['qualification'], store_root)
        elif require_qualified: raise PocketError('Explicit synthetic qualification required')
        return model
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as error:
        raise PocketError('Malformed retained note model') from error


def prepare_note_model(*, model: AudioNoteModel, store_root: str):
    if not isinstance(model, dict): raise PocketError('Explicit note model declaration or handle required')
    if model.get('kind') == 'inline':
        _fields(model, {'kind', 'declaration', 'qualification'}, 'inline note model')
        if model['qualification'] != 'synthetic_onnx_cpu_v1': raise PocketError('Explicit qualification required')
        declaration = model['declaration']
    elif model.get('kind') == 'inspected':
        _fields(model, {'kind', 'model'}, 'inspected note model')
        declaration = load_audio_note_model(model['model'], store_root)['declaration']
    else: raise PocketError('Unknown note model input')
    _declaration(declaration)
    return copy.deepcopy(declaration)


def _capture(declaration, qualification, root, source=None, retained=None):
    prior = load_audio_note_model(retained, root) if retained is not None else None
    weights, _ = _declaration(declaration)
    handle = put_bytes(weights, root, 'weights.onnx', 'pocket.note-model-weights/v1')
    result, binding, binaries = _runner(declaration, qualification, Path(root).expanduser().resolve() / handle['artifact_uri'], source)
    profile = put_record({'schema': PROFILE_SCHEMA, 'profile': result['profile'], 'profile_sha256': result['profile_sha256']}, root)
    proof = None
    if result['qualification'] is not None:
        retained_proof = _retain(result['qualification'], binaries, root); _qualify(retained_proof, root)
        proof = put_record({'schema': QUAL_SCHEMA, 'profile': profile, 'weights': handle, 'qualification': retained_proof}, root)
    if prior is not None:
        if qualification != 'none' or proof is not None or profile != prior['profile'] or handle != prior['weights'] or result['profile_sha256'] != prior['profile_sha256']:
            raise PocketError('Inspected qualification reuse differs from current model/profile')
        proof = prior['qualification']
    model = put_record({'schema': MODEL_SCHEMA, 'declaration': {**copy.deepcopy(declaration), 'expected_profile': result['profile_sha256']},
                        'binding': binding, 'profile': profile, 'profile_sha256': result['profile_sha256'], 'weights': handle,
                        'qualification': proof, 'qualified': proof is not None,
                        'qualification_scope': QUAL_SCOPE}, root)
    load_audio_note_model(model, root, require_qualified=proof is not None)
    return model, _retain(result['analysis'], binaries, root)


def audio_note_model_inspect(*, store_root: str, request_id: str, declaration: AudioNoteModelDeclaration,
                             qualification: Literal['none', 'synthetic_onnx_cpu_v1'] = 'none') -> dict:
    """Inspect an explicitly installed local ONNX CPU runtime; optionally qualify it."""
    _declaration(declaration)
    if qualification not in ('none', 'synthetic_onnx_cpu_v1'): raise PocketError('Unknown qualification')
    executed = False
    def work():
        nonlocal executed
        executed = True
        model, _ = _capture(declaration, qualification, store_root)
        return receipt(request_id, artifacts={'model': model}, coverage=COVERAGE,
                       change_summary={'available': True, 'qualified': qualification != 'none'}, uncertainty=LIMITATIONS)
    result = run_request(store_root, request_id, 'audio_note_model_inspect', {'declaration': declaration, 'qualification': qualification}, work)
    retained = load_audio_note_model(result['artifacts']['model'], store_root, require_qualified=qualification != 'none')
    if not executed:
        _runner(retained['declaration'], 'none', Path(store_root).expanduser().resolve() / retained['weights']['artifact_uri'])
    return result


def _annotations(evidence, projection):
    rows = []
    for index, value in enumerate(projection['events']):
        row = {'annotation': copy.deepcopy(value), 'support': [{'kind': 'analysis_pointer', 'reference': f'/analysis/projection/events/{index}'}],
               'uncertainty': ['Uncalibrated learned note, gate and pitch hypotheses can be wrong.'],
               'attribution': {'actor': 'Basic Pitch', 'actor_kind': 'algorithm', 'analysis_version': 'basic_pitch_onnx_cpu_v1'}, 'supersedes': []}
        row['annotation_id'] = 'audio:' + digest([evidence, row]); rows.append(row)
    return rows


def _projection_provenance():
    """Capture the base decoder independently from the optional model runtime."""
    implementation = Path(__file__).with_name('audio_note_projection.py').read_bytes()
    if hashlib.sha256(implementation).hexdigest() != PROJECTION_SHA256:
        raise PocketError('Decoder source differs from this versioned projection profile')
    result = {'schema': 'pocket.note-projection-provenance/v1',
              'profile': SETTINGS['decoder'], 'implementation_sha256': PROJECTION_SHA256,
              'vendor_source_sha256': VENDOR_DECODER_SHA256,
              'numpy_version': np.__version__, 'scipy_version': scipy.__version__,
              'replay_semantics': PROJECTION_REPLAY}
    _validate_projection_provenance(result)
    return result


def _validate_projection_provenance(value):
    _fields(value, {'schema', 'profile', 'implementation_sha256', 'vendor_source_sha256',
                    'numpy_version', 'scipy_version', 'replay_semantics'}, 'projection provenance')
    if (value['schema'] != 'pocket.note-projection-provenance/v1' or value['profile'] != SETTINGS['decoder']
            or value['implementation_sha256'] != PROJECTION_SHA256
            or value['vendor_source_sha256'] != VENDOR_DECODER_SHA256
            or value['replay_semantics'] != PROJECTION_REPLAY):
        raise PocketError('Unknown projection provenance profile')
    for key in ('numpy_version', 'scipy_version'):
        if not isinstance(value[key], str) or not 1 <= len(value[key]) <= 128 or not re.fullmatch(r'[A-Za-z0-9.+_-]+', value[key]):
            raise PocketError('Malformed base decoder version')
    # Historical artifacts do not require their original environment or source
    # path. The caller still recomputes and compares the entire current ledger.


def _projection(raw, source, payload, root):
    mono = _decoded(payload, source)
    windows = _windows(raw, root, source=True)
    if raw['mono_sha256'] != hashlib.sha256(mono).hexdigest(): raise PocketError('Decoded source/downmix proof mismatch')
    resampled = _array(raw['resampled'], root)
    expected = ((source['end_frame_exclusive'] - source['start_frame']) * 22050 * 2 + source['sample_rate']) // (2 * source['sample_rate'])
    if resampled.shape != (expected,) or raw['resampled_frames'] != expected: raise PocketError('Resampled frame count mismatch')
    if source['sample_rate'] == 22050 and resampled.tobytes() != mono: raise PocketError('Identity-rate source was altered')
    return project_note_arrays(windows, source, expected)


def load_note_hypotheses(handle, store_root):
    """Initial-only semantic validator; artifact relocation needs no original files."""
    _verify_handles(handle, store_root)
    try:
        record = read_record(handle, store_root, SCHEMA)
        _fields(record, {'schema', 'source', 'original', 'analysis', 'model', 'annotations', 'parent', 'revision_index', 'annotation_count', 'request_attribution', 'settings', 'limitations'}, 'initial note hypotheses')
        if canonical_bytes(record['settings']) != canonical_bytes(SETTINGS) or record['limitations'] != LIMITATIONS: raise PocketError('Note settings/limitations mismatch')
        _attribution(record['request_attribution']); load_audio_note_model(record['model'], store_root)
        evidence = read_record(record['analysis'], store_root, ANALYSIS_SCHEMA)
        base_fields = {'schema', 'analysis', 'original', 'model', 'transform'}
        if isinstance(evidence, dict) and 'projection_provenance' in evidence:
            _fields(evidence, base_fields | {'projection_provenance'}, 'note analysis')
            _validate_projection_provenance(evidence['projection_provenance'])
        else:
            # Exact legacy v1 records remain valid; their base decoder provenance
            # was not retained and must not be inferred from the model profile.
            _fields(evidence, base_fields, 'legacy note analysis')
        _fields(evidence['analysis'], {'raw', 'projection'}, 'note analysis payload')
        if canonical_bytes(evidence['transform']) != canonical_bytes(TRANSFORM) or evidence['original'] != record['original'] or evidence['model'] != record['model']:
            raise PocketError('Note source/model/transform binding mismatch')
        projection = _projection(evidence['analysis']['raw'], record['source'], read_bytes(record['original'], store_root), store_root)
        if canonical_bytes(projection) != canonical_bytes(evidence['analysis']['projection']) or record['annotations'] != _annotations(record['analysis'], projection):
            raise PocketError('Note ledger/annotations differ from complete retained tensors')
        if record['parent'] is not None or type(record['revision_index']) is not int or record['revision_index'] != 0 or type(record['annotation_count']) is not int or record['annotation_count'] != len(record['annotations']):
            raise PocketError('Expected initial note hypotheses')
        return record
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as error:
        raise PocketError('Malformed retained note evidence') from error


def audio_note_hypotheses(*, store_root: str, request_id: str, source: AudioHypothesisSource,
                          model: AudioNoteModel, settings: AudioNoteSettings, attribution: AudioHypothesisAttribution) -> dict:
    """Retain uncertain note hypotheses from an explicit local source and model."""
    declaration = prepare_note_model(model=model, store_root=store_root)
    prepared = prepare_note_input(source=source, settings=settings, attribution=attribution, store_root=store_root)
    executed = False
    def work():
        nonlocal executed
        executed = True
        payload = read_bytes(prepared['source_handle'], store_root)
        mono = _decoded(payload, prepared['source_metadata'])
        retained = model['model'] if model['kind'] == 'inspected' else None
        model_handle, raw = _capture(declaration, 'none' if retained else 'synthetic_onnx_cpu_v1', store_root,
                                      (mono, prepared['source_metadata']['sample_rate']), retained)
        projection = _projection(raw, prepared['source_metadata'], payload, store_root)
        evidence = put_record({'schema': ANALYSIS_SCHEMA, 'analysis': {'raw': raw, 'projection': projection},
                               'original': prepared['source_handle'], 'model': model_handle, 'transform': copy.deepcopy(TRANSFORM),
                               'projection_provenance': _projection_provenance()}, store_root)
        rows = _annotations(evidence, projection); _cancel()
        handle = put_record({'schema': SCHEMA, 'source': prepared['source_metadata'], 'original': prepared['source_handle'],
                             'analysis': evidence, 'model': model_handle, 'annotations': rows, 'parent': None,
                             'revision_index': 0, 'annotation_count': len(rows), 'request_attribution': copy.deepcopy(attribution),
                             'settings': copy.deepcopy(settings), 'limitations': copy.deepcopy(LIMITATIONS)}, store_root)
        load_note_hypotheses(handle, store_root)
        return receipt(request_id, artifacts={'hypotheses': handle}, coverage=COVERAGE,
                       change_summary={'annotations': len(rows), 'excluded_boundary_events': len(projection['excluded']), 'revision_index': 0}, uncertainty=LIMITATIONS)
    result = run_request(store_root, request_id, 'audio_note_hypotheses', {'source': source, 'model': model, 'settings': settings, 'attribution': attribution}, work)
    validated = load_note_hypotheses(result['artifacts']['hypotheses'], store_root)
    if not executed:
        retained_model = load_audio_note_model(validated['model'], store_root)
        _runner(retained_model['declaration'], 'none', Path(store_root).expanduser().resolve() / retained_model['weights']['artifact_uri'])
    return result
