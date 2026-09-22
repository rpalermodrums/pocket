"""Owned standalone optional-runtime runner. Never imported by the base provider.

Run only with a bounded JSON request path; no shell, downloader or unsafe pickle.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import sys
import sysconfig
import wave
from importlib import metadata
from pathlib import Path


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def denied(*args, **kwargs):
    raise RuntimeError('Network is disabled in the local model runner')


def file_digest(path):
    path = Path(path)
    before = path.stat()
    if not path.is_file() or before.st_size > 2 * 1024**3:
        raise ValueError('Runtime file exceeds regular-file bounds')
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        initial = os.fstat(stream.fileno())
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(chunk)
        final = os.fstat(stream.fileno())
    stamp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if stamp(before) != stamp(initial) or stamp(initial) != stamp(final) or stamp(final) != stamp(path.stat()):
        raise ValueError('Runtime file changed during fingerprint')
    return {'sha256': hasher.hexdigest(), 'bytes': before.st_size}


def _stdlib_files(stdlib):
    """Hash the original included-file set without walking excluded subtrees.

    Like Path.rglob's default, directory symlinks are not traversed; regular
    file symlinks remain eligible and use the unchanged checked file reader.
    Global Path ordering preserves historical relative-key insertion order.
    """
    stdlib = Path(stdlib)
    included = []
    for directory, directories, filenames in os.walk(stdlib, topdown=True, followlinks=False):
        directories[:] = [name for name in directories if name not in ('site-packages', '__pycache__')]
        for name in filenames:
            path = Path(directory) / name
            if path.suffix in ('.py', '.so', '.dylib') and path.is_file():
                if len(included) >= 10000:
                    raise ValueError('Standard library fingerprint exceeds bounds')
                included.append(path)
    return {str(path.relative_to(stdlib)): file_digest(path) for path in sorted(included)}


def profile():
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    pending = ['beat-this', 'torch', 'torchaudio', 'numpy', 'soxr', 'einops', 'rotary-embedding-torch', 'packaging']
    packages = {}
    total = 0
    count = 0
    while pending:
        name = canonicalize_name(pending.pop())
        if name in packages:
            continue
        if len(packages) >= 64:
            raise ValueError('Runtime dependency closure exceeds 64 distributions')
        dist = metadata.distribution(name)
        files = {}
        for item in sorted(dist.files or [], key=str):
            if str(item).endswith(('.pyc', '.pyo')) or '__pycache__' in str(item):
                continue
            path = Path(dist.locate_file(item))
            if path.is_file():
                count += 1
                if count > 50000:
                    raise ValueError('Runtime file count exceeds bounds')
                value = file_digest(path)
                total += value['bytes']
                if total > 8 * 1024**3:
                    raise ValueError('Runtime dependency bytes exceed bounds')
                files[str(item)] = value
        if not files:
            raise ValueError('Distribution has no verifiable files')
        packages[name] = {'version': dist.version, 'files': files}
        for raw in dist.requires or []:
            requirement = Requirement(raw)
            if requirement.marker is None or requirement.marker.evaluate({'extra': ''}):
                pending.append(requirement.name)
    if packages['beat-this']['version'] != '1.1.0':
        raise ValueError('Only the BeatThis 1.1.0 adapter is supported')
    stdlib = Path(sysconfig.get_path('stdlib'))
    # Bind the interpreter's complete stdlib source and native extension support.
    stdfiles = _stdlib_files(stdlib)
    result = {'adapter': 'beat_this_cpu_v1', 'python': platform.python_version(),
              'implementation': platform.python_implementation(), 'platform': platform.system(),
              'release': platform.release(), 'machine': platform.machine(), 'packages': packages,
              'stdlib': stdfiles, 'executable': file_digest(Path(sys.executable).resolve()),
              'runner': file_digest(__file__)}
    return result


def load_model(path, expected):
    import torch
    from beat_this.inference import Audio2Frames
    from beat_this.model.beat_tracker import BeatThis
    from beat_this.preprocessing import LogMelSpect
    from beat_this.utils import replace_state_dict_key
    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    torch.manual_seed(0)
    torch.use_deterministic_algorithms(True)
    torch.hub.load_state_dict_from_url = denied
    if file_digest(path)['sha256'] != expected:
        raise ValueError('Weights digest mismatch')
    with open(path, 'rb') as stream:
        payload = stream.read(128 * 1024**2 + 1)
        if len(payload) > 128 * 1024**2 or sha(payload) != expected:
            raise ValueError('Weights changed or exceed bounds')
        # Deserialize the exact verified bytes, not a mutable reopened path.
        import io
        checkpoint = torch.load(io.BytesIO(payload), weights_only=True, map_location='cpu')
    if set(checkpoint) != {'state_dict', 'hyper_parameters', 'datamodule_hyper_parameters', 'pytorch-lightning_version'}:
        raise ValueError('Unsupported checkpoint structure')
    expected_params = {'spect_dim': 128, 'transformer_dim': 512, 'ff_mult': 4,
                       'n_layers': 6, 'stem_dim': 32, 'head_dim': 32,
                       'dropout': {'frontend': .1, 'transformer': .2}}
    actual = {key: checkpoint['hyper_parameters'].get(key) for key in expected_params}
    if actual != expected_params:
        raise ValueError('Unsupported checkpoint architecture')
    model = BeatThis(**actual).to('cpu').eval()
    model.load_state_dict(replace_state_dict_key(checkpoint['state_dict'], 'model.', ''), strict=True)
    predictor = Audio2Frames.__new__(Audio2Frames)
    predictor.device = torch.device('cpu')
    predictor.float16 = False
    predictor.model = model
    predictor.spect = LogMelSpect(device='cpu')
    return predictor


def infer(predictor, pcm, rate):
    import numpy as np
    import soxr
    import torch
    from beat_this.inference import Spect2Frames
    from beat_this.model.postprocessor import Postprocessor
    mono = pcm.mean(axis=1)
    resampled = soxr.resample(mono, rate, 22050, quality='HQ')
    tensor = torch.tensor(resampled, dtype=torch.float32, device='cpu')
    with torch.inference_mode():
        spect = predictor.spect(tensor)
        first = Spect2Frames.spect2frames(predictor, spect)
        second = Spect2Frames.spect2frames(predictor, spect)
    if not all(torch.equal(a, b) for a, b in zip(first, second)):
        raise ValueError('Inference failed exact repeat qualification')
    if first[0].shape != first[1].shape or first[0].shape != (spect.shape[0],) or not 1 <= spect.shape[0] <= 1002:
        raise ValueError('Unexpected model shape')
    arrays = [value.detach().cpu().numpy().astype('<f4') for value in first]
    if not all(np.isfinite(value).all() for value in arrays):
        raise ValueError('Nonfinite model output')
    beats, downbeats = Postprocessor(type='minimal', fps=50)(*first)
    return {'beat': arrays[0].tolist(), 'downbeat': arrays[1].tolist(),
            'vendor_beats_seconds': beats.tolist(), 'vendor_downbeats_seconds': downbeats.tolist(),
            'input': {'decoded_float64_sha256': sha(pcm.astype('<f8').tobytes()),
                      'mono_float64_sha256': sha(mono.astype('<f8').tobytes()),
                      'resampled_float64_sha256': sha(resampled.astype('<f8').tobytes()),
                      'model_float32_sha256': sha(tensor.numpy().astype('<f4').tobytes()),
                      'resampled_frames': len(resampled), 'spect_shape': list(spect.shape)},
            'repeat_sha256': {name: [sha(first[index].detach().cpu().numpy().astype('<f4').tobytes()), sha(second[index].detach().cpu().numpy().astype('<f4').tobytes())] for index, name in enumerate(('beat', 'downbeat'))},
            'two_repeats_sample_exact': True}


def qualify(predictor):
    import numpy as np
    cases = []
    for name, rate, channels, seconds in [('silence', 8000, 1, 2), ('impulses', 44100, 1, 4),
                                         ('competing', 48000, 1, 4), ('offgrid', 8000, 1, 4),
                                         ('antiphase', 48000, 2, 4), ('two_tones', 44100, 1, 4),
                                         ('max_crop', 48000, 1, 20)]:
        pcm = np.zeros((rate * seconds, channels), dtype='<i2')
        if name == 'two_tones':
            t = np.arange(len(pcm)) / rate
            pcm[:, 0] = np.round(6000 * np.sin(2 * np.pi * 261.625565 * t) + 6000 * np.sin(2 * np.pi * 391.995436 * t)).astype('<i2')
        elif name != 'silence':
            step = .25 if name == 'competing' else .5
            for position in np.arange(step, seconds, step):
                at = round((position + (.073 if name == 'offgrid' else 0)) * rate)
                pulse = np.array([24000 - 700 * i for i in range(32)], dtype='<i2')
                pcm[at:at + 32, 0] = pulse
                if channels == 2:
                    pcm[at:at + 32, 1] = -pulse
        result = infer(predictor, pcm.astype(np.float64) / 32768, rate)
        cases.append({'name': name, 'rate': rate, 'channels': channels, 'frames': len(pcm),
                      'result': result})
    return {'profile': 'synthetic_cpu_v1', 'passed': True, 'cases': cases,
            'meaning': 'Finite repeatable synthetic execution only; not musical accuracy'}


def execute(request):
    socket.create_connection = denied
    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    initial = profile()
    fingerprint = sha(canonical(initial))
    if request['expected_profile'] is not None and fingerprint != request['expected_profile']:
        raise ValueError('Runtime profile changed')
    result = {'profile': initial, 'profile_sha256': fingerprint, 'qualification': None, 'analysis': None}
    if request['qualification'] == 'synthetic_cpu_v1' or request.get('source') is not None:
        model = load_model(request['weights_path'], request['weights_sha256'])
        if request['qualification'] == 'synthetic_cpu_v1':
            result['qualification'] = qualify(model)
        if request.get('source') is not None:
            import numpy as np
            source = request['source']
            import io
            with open(source['path'], 'rb') as source_stream:
                source_bytes = source_stream.read(256 * 1024**2 + 1)
            if len(source_bytes) > 256 * 1024**2 or sha(source_bytes) != source['expected_sha256']:
                raise ValueError('Source digest changed or exceeds bounds')
            with wave.open(io.BytesIO(source_bytes), 'rb') as stream:
                if stream.getsampwidth() != 2 or stream.getcomptype() != 'NONE' or stream.getnchannels() not in (1, 2) or stream.getframerate() not in (8000, 44100, 48000):
                    raise ValueError('Unsupported qualified PCM source')
                rate, channels = stream.getframerate(), stream.getnchannels()
                stream.setpos(source['start_frame'])
                data = stream.readframes(source['frames'])
            if len(data) != source['frames'] * channels * 2 or not 2 * rate <= source['frames'] <= 20 * rate:
                raise ValueError('Qualified crop must be 2–20 seconds')
            result['analysis'] = infer(model, np.frombuffer(data, dtype='<i2').astype(np.float64).reshape(-1, channels) / 32768, rate)
    if profile() != initial:
        raise ValueError('Runtime changed during execution')
    return result


if __name__ == '__main__':
    try:
        raw = Path(sys.argv[1]).read_bytes()
        if len(raw) > 32768:
            raise ValueError('Runner request exceeds bound')
        response = {'status': 'ok', 'result': execute(json.loads(raw))}
    except Exception as error:  # noqa: BLE001 - child protocol must serialize model-specific failures
        response = {'status': 'error', 'error': type(error).__name__ + ': ' + str(error)[:2048]}
    payload = canonical(response)
    if len(payload) > 16 * 1024**2:
        payload = canonical({'status': 'error', 'error': 'Runner response exceeded 16 MiB'})
    Path(sys.argv[2]).write_bytes(payload)
