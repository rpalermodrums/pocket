"""Isolated known-weight ONNX CPU runner; invoked only by the owned provider.

Network guards are defense in depth, not an operating-system sandbox. No model
loader fallback, cache, downloader, custom graph, or pickle is used.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import socket
import sys
from importlib import metadata
from pathlib import Path

KNOWN_WEIGHT = '2c3c1d144bfa61ad236e92e169c13535c880469a12a047d4e73451f2c059a0ec'


def sha(data): return hashlib.sha256(data).hexdigest()
def canonical(value): return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
def denied(*args, **kwargs): raise RuntimeError('Network prohibited in local note runner')


def capture(path, maximum):
    path = Path(path)
    before = path.stat()
    if not path.is_file() or before.st_size > maximum: raise ValueError('File bound')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno()); data = stream.read(maximum + 1); final = os.fstat(stream.fileno())
    stamp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if len(data) > maximum or not stamp(before) == stamp(opened) == stamp(final) == stamp(path.stat()):
        raise ValueError('Concurrent file change')
    return data


def profile():
    # Reuse only the checked deterministic file fingerprint implementation; its
    # BeatThis loader is never called or imported into an optional framework.
    helper = Path(__file__).with_name('audio_model_runner.py')
    spec = importlib.util.spec_from_file_location('_pocket_profile', helper)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    packages = {}
    for name in ('onnxruntime', 'numpy', 'soxr', 'packaging', 'protobuf', 'flatbuffers'):
        dist = metadata.distribution(name)
        files = {}
        for item in sorted(dist.files or [], key=str):
            if str(item).endswith(('.pyc', '.pyo')) or '__pycache__' in str(item): continue
            path = Path(dist.locate_file(item))
            if path.is_file(): files[str(item)] = module.file_digest(path)
            if len(files) > 15000: raise ValueError('Runtime file count bound')
        if not files: raise ValueError('Missing runtime distribution files')
        packages[name] = {'version': dist.version, 'files': files}
    if packages['onnxruntime']['version'] != '1.26.0' or packages['soxr']['version'] != '1.1.0':
        raise ValueError('Unqualified ONNX/soxr version')
    import sysconfig
    return {'adapter': 'basic_pitch_onnx_cpu_v1', 'python': platform.python_version(),
            'platform': platform.platform(), 'machine': platform.machine(),
            'prefix': sys.prefix, 'base_prefix': sys.base_prefix, 'launch_executable': sys.executable,
            'executable': module.file_digest(Path(sys.executable).resolve()), 'packages': packages,
            'stdlib': module._stdlib_files(Path(sysconfig.get_path('stdlib'))),
            'runner': module.file_digest(__file__), 'fingerprint_helper': module.file_digest(helper),
            'scope': 'Listed distribution files and Python stdlib; excludes unenumerated OS shared libraries.'}


def session(weights):
    import onnxruntime as ort
    options = ort.SessionOptions(); options.intra_op_num_threads = 1; options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.enable_profiling = False
    options.add_session_config_entry('session.intra_op.allow_spinning', '0')
    options.add_session_config_entry('session.inter_op.allow_spinning', '0')
    result = ort.InferenceSession(weights, sess_options=options, providers=['CPUExecutionProvider'])
    inp = result.get_inputs(); out = result.get_outputs()
    if (result.get_providers() != ['CPUExecutionProvider'] or len(inp) != 1
            or inp[0].name != 'serving_default_input_2:0' or inp[0].type != 'tensor(float)'
            or inp[0].shape[1:] != [43844, 1]
            or {x.name: (x.shape[1:], x.type) for x in out} != {
                'StatefulPartitionedCall:1': ([172, 88], 'tensor(float)'),
                'StatefulPartitionedCall:2': ([172, 88], 'tensor(float)'),
                'StatefulPartitionedCall:0': ([172, 264], 'tensor(float)')}):
        raise ValueError('Unexpected known-model metadata')
    return result


def infer(engine, audio, output, prefix):
    import numpy as np
    if audio.dtype != np.float32 or audio.ndim != 1 or not 44100 <= len(audio) <= 441000 or not np.isfinite(audio).all():
        raise ValueError('Invalid resampled source')
    padded = np.concatenate([np.zeros(3840, dtype=np.float32), audio])
    starts = list(range(0, len(padded), 36164)); arrays = {}
    names = ('note', 'onset', 'contour'); outputs = ['StatefulPartitionedCall:1', 'StatefulPartitionedCall:2', 'StatefulPartitionedCall:0']
    for repeat in range(2):
        accumulated = {key: [] for key in names}
        for start in starts:
            window = np.zeros(43844, dtype=np.float32); segment = padded[start:start + 43844]; window[:len(segment)] = segment
            values = engine.run(outputs, {'serving_default_input_2:0': window.reshape(1, 43844, 1)})
            for key, value, bins in zip(names, values, (88, 88, 264), strict=True):
                if value.shape != (1, 172, bins) or value.dtype != np.float32 or not np.isfinite(value).all() or np.any(value < 0) or np.any(value > 1):
                    raise ValueError('Invalid raw model activation')
                accumulated[key].append(value)
        for key in names:
            value = np.concatenate(accumulated[key]); payload = value.astype('<f4', copy=False).tobytes()
            filename = f'{prefix}-{repeat}-{key}.f32'; (output / filename).write_bytes(payload)
            arrays[f'{repeat}:{key}'] = {'file': filename, 'sha256': sha(payload), 'bytes': len(payload), 'shape': list(value.shape), 'dtype': 'float32le'}
    for key in names:
        if arrays['0:' + key]['sha256'] != arrays['1:' + key]['sha256']: raise ValueError('Repeated inference differs')
    return {'resampled_frames': len(audio), 'arrays': arrays, 'two_repeats_sample_exact': True}


def execute(request, output):
    import numpy as np
    import soxr
    weights = capture(request['weights_path'], 1024 * 1024)
    if sha(weights) != KNOWN_WEIGHT or request['weights_sha256'] != KNOWN_WEIGHT: raise ValueError('Known model bytes required')
    before = profile(); fingerprint = sha(canonical(before))
    if request['expected_profile'] is not None and request['expected_profile'] != fingerprint: raise ValueError('Expected runtime profile changed')
    qualification = None; analysis = None
    if request['qualification'] not in ('none', 'synthetic_onnx_cpu_v1'): raise ValueError('Unknown qualification')
    engine = session(weights) if request['qualification'] != 'none' or request['source'] is not None else None
    if request['qualification'] != 'none':
        cases = {}
        for name in ('silence', 'tone'):
            audio = np.zeros(44100, dtype=np.float32)
            if name == 'tone':
                t = np.arange(44100, dtype=np.float64) / 22050
                audio = (.5 * np.sin(2 * np.pi * 440 * t) * ((t >= .25) & (t < 1.75))).astype(np.float32)
            cases[name] = {'input_sha256': sha(audio.tobytes()), **infer(engine, audio, output, 'qualification-' + name)}
        qualification = {'profile': 'synthetic_onnx_cpu_v1', 'cases': cases}
    if request['source'] is not None:
        source = request['source']; payload = capture(source['mono_path'], 4 * 96000 * 20)
        if sha(payload) != source['mono_sha256']: raise ValueError('Captured mono bytes changed')
        mono = np.frombuffer(payload, dtype='<f4')
        if len(mono) != source['frames'] or not np.isfinite(mono).all(): raise ValueError('Invalid mono source')
        audio = mono if source['sample_rate'] == 22050 else soxr.resample(mono, source['sample_rate'], 22050, quality='HQ')
        audio = np.asarray(audio, dtype=np.float32)
        data = audio.astype('<f4', copy=False).tobytes(); (output / 'resampled.f32').write_bytes(data)
        analysis = {**infer(engine, audio, output, 'source'), 'mono_sha256': sha(payload),
                    'resampled': {'file': 'resampled.f32', 'sha256': sha(data), 'bytes': len(data), 'shape': [len(audio)], 'dtype': 'float32le'}}
    if before != profile(): raise ValueError('Runtime changed during execution')
    return {'profile': before, 'profile_sha256': fingerprint, 'qualification': qualification, 'analysis': analysis}


if __name__ == '__main__':
    socket.socket.connect = denied; socket.create_connection = denied
    try:
        request = json.loads(capture(sys.argv[1], 65536))
        result = {'status': 'ok', 'result': execute(request, Path(sys.argv[2]).parent)}
    except Exception as error:  # noqa: BLE001 - isolated process returns a bounded diagnostic
        result = {'status': 'error', 'error': f'{type(error).__name__}: {error}'[:2048]}
    Path(sys.argv[2]).write_bytes(canonical(result))
