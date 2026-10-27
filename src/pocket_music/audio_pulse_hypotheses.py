# SPDX-License-Identifier: AGPL-3.0-only
"""Optional local learned-pulse inspection and immutable uncertain evidence.

Base imports never load torch. The owned subprocess receives explicit inherited
execution leases from a scoped caller context, never from a PID or environment.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
import re
import struct
import subprocess
import tempfile
import wave
from contextlib import contextmanager
from contextvars import ContextVar
from fractions import Fraction
from pathlib import Path
from typing import Literal

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
from .audio_model_types import AudioModelDeclaration, AudioPulseModel, AudioPulseSettings
from .errors import PocketError

TRANSFORM = {'source_downmix': 'arithmetic_mean', 'resampler': 'soxr_hq', 'model_rate': 22050,
             'model_fps': 50, 'n_fft': 1024, 'hop_length': 441, 'mel_bands': 128, 'center': True,
             'mapping': 'start_frame + model_frame_q * source_rate / 50'}
SCHEMA = 'pocket.audio-model-hypotheses/v1'
MODEL_SCHEMA = 'pocket.audio-model/v1'
ANALYSIS_SCHEMA = 'pocket.audio-model-analysis/v1'
SETTINGS = {'device': 'cpu', 'dtype': 'float32', 'threads': 1, 'postprocessor': 'minimal',
            'downmix': 'arithmetic_mean', 'resampler': 'soxr_hq', 'seed': 0}
COVERAGE = {'provider': 'audio-pulse-hypotheses-v1', 'native_execution': False,
            'learned_models': ['beat_this_cpu_v1'], 'transcription': False,
            'human_listening': 'not_performed', 'musical_accuracy_qualified': False}
_CONTEXT = ContextVar('pocket_model_execution', default=((), None))


@contextmanager
def model_execution_context(*, lease_fds: tuple[int, ...] = (), cancellation_check=None):
    """Private worker integration: propagate actual owned leases to model children."""
    if not isinstance(lease_fds, tuple) or any(type(fd) is not int or fd < 0 for fd in lease_fds):
        raise PocketError('Execution leases require explicit file descriptors')
    for fd in lease_fds:
        os.fstat(fd)
    if cancellation_check is not None and not callable(cancellation_check):
        raise PocketError('Cancellation check must be callable')
    token = _CONTEXT.set((lease_fds, cancellation_check))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def _cancel():
    check = _CONTEXT.get()[1]
    if check is not None:
        check()


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise PocketError('Expected lowercase SHA256')


def _file(binding, maximum):
    _fields(binding, {'path', 'sha256'}, 'model file')
    _sha(binding['sha256'])
    if not isinstance(binding['path'], str) or not Path(binding['path']).is_absolute() or len(binding['path']) > 4096:
        raise PocketError('Model file requires explicit bounded absolute path')
    path = Path(binding['path'])
    try:
        resolved = path.resolve(strict=True)
        before = resolved.stat()
        if not resolved.is_file() or before.st_size > maximum:
            raise PocketError('Model file exceeds regular-file bounds')
        with resolved.open('rb') as stream:
            data = stream.read(maximum + 1)
            opened = os.fstat(stream.fileno())
        stamp = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if (len(data) > maximum or path.resolve(strict=True) != resolved or stamp(before) != stamp(opened)
                or stamp(before) != stamp(resolved.stat())):
            raise PocketError('Model file changed during capture')
    except OSError as error:
        raise PocketError('Cannot read declared model file') from error
    if hashlib.sha256(data).hexdigest() != binding['sha256']:
        raise PocketError('Declared model file digest mismatch')
    return data, {'supplied_path': str(path), 'resolved_path': str(resolved), 'stamp': list(stamp(before)),
                  'sha256': binding['sha256']}


def _declaration(value):
    _fields(value, {'adapter', 'executable', 'weights', 'expected_profile'}, 'model declaration')
    if value['adapter'] != 'beat_this_cpu_v1':
        raise PocketError('Unsupported model adapter')
    if value['expected_profile'] is not None:
        _sha(value['expected_profile'])
    executable, execution_binding = _file(value['executable'], 128 * 1024**2)
    if executable[:4] not in (b'\x7fELF', b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf') or not os.access(execution_binding['resolved_path'], os.X_OK):
        raise PocketError('Declared runtime is not executable')
    weights, weight_binding = _file(value['weights'], 128 * 1024**2)
    return weights, {'executable': execution_binding, 'weights': weight_binding}


def _runner(declaration, qualification, weights_path, source=None):
    _cancel()
    _, before = _declaration(declaration)
    request = {'weights_path': str(weights_path), 'weights_sha256': declaration['weights']['sha256'],
               'expected_profile': declaration['expected_profile'], 'qualification': qualification,
               'source': source}
    runner = Path(__file__).with_name('audio_model_runner.py')
    runner_hash = hashlib.sha256(runner.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix='pocket-model-') as directory:
        request_path, response_path = Path(directory) / 'request.json', Path(directory) / 'response.json'
        request_path.write_bytes(canonical_bytes(request))
        environment = {key: value for key, value in os.environ.items()
                       if key not in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP')}
        environment.update({'PYTHONDONTWRITEBYTECODE': '1', 'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1',
                            'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'})
        try:
            # Bound output storage: diagnostics belong only to the bounded JSON
            # response, never unbounded dependency stdout/stderr files or pipes.
            process = subprocess.run([before['executable']['resolved_path'], '-I', '-B', str(runner),
                                      str(request_path), str(response_path)],
                                     env=environment, pass_fds=_CONTEXT.get()[0],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     timeout=60, check=False)
            if process.returncode != 0 or not response_path.is_file() or response_path.stat().st_size > 16 * 1024**2:
                raise PocketError('Optional model runner failed or exceeded response bounds')
            response = json.loads(response_path.read_bytes())
        except subprocess.TimeoutExpired as error:
            raise PocketError('Owned model runner exceeded 60 seconds and was terminated; no result published') from error
        except (OSError, ValueError) as error:
            raise PocketError('Optional model runner unavailable or malformed') from error
    _cancel()
    _, after = _declaration(declaration)
    if before != after or hashlib.sha256(runner.read_bytes()).hexdigest() != runner_hash:
        raise PocketError('Model execution binding changed')
    if not isinstance(response, dict) or response.get('status') != 'ok':
        reason = str(response.get('error', 'unknown error'))[:2048] if isinstance(response, dict) else 'invalid result'
        raise PocketError('Optional model runner refused: ' + reason)
    _fields(response, {'status', 'result'}, 'model runner response')
    result = response['result']
    _fields(result, {'profile', 'profile_sha256', 'qualification', 'analysis'}, 'model runner result')
    if not isinstance(result['profile'], dict):
        raise PocketError('Model profile must be an object')
    if digest(result['profile']) != result['profile_sha256']:
        raise PocketError('Model profile digest mismatch')
    if declaration['expected_profile'] is not None and result['profile_sha256'] != declaration['expected_profile']:
        raise PocketError('Expected model profile mismatch')
    return result, before


def _capture(declaration, qualification, store_root, source=None, *, qualified_model=None):
    if qualification not in ('none', 'synthetic_cpu_v1'):
        raise PocketError('Unknown model qualification')
    retained = load_audio_model(qualified_model, store_root) if qualified_model is not None else None
    if retained is not None and (qualification != 'none' or source is None
                                 or declaration['expected_profile'] != retained['profile_sha256']
                                 or declaration['weights']['sha256'] != retained['weights']['sha256']):
        raise PocketError('Retained qualification reuse requires exact model/profile binding and target source')
    weights, _ = _declaration(declaration)
    handle = put_bytes(weights, store_root, 'weights.ckpt', 'pocket.model-weights/v1')
    path = Path(store_root).expanduser().resolve() / handle['artifact_uri']
    result, binding = _runner(declaration, qualification, path, source)
    profile = put_record({'schema': 'pocket.audio-runtime-profile/v1', 'profile': result['profile'],
                          'profile_sha256': result['profile_sha256']}, store_root)
    qualification_handle = None
    if retained is not None:
        if (result['qualification'] is not None or profile != retained['profile'] or handle != retained['weights']
                or result['profile_sha256'] != retained['profile_sha256']):
            raise PocketError('Retained qualification differs from freshly checked runtime or weights')
        qualification_handle = retained['qualification']
    if result['qualification'] is not None:
        qualification_handle = put_record({'schema': 'pocket.audio-model-qualification/v1',
                                           'profile': profile, 'weights': handle,
                                           'qualification': result['qualification']}, store_root)
    model = put_record({'schema': MODEL_SCHEMA, 'declaration': {**copy.deepcopy(declaration), 'expected_profile': result['profile_sha256']},
                        'binding': binding, 'profile': profile, 'profile_sha256': result['profile_sha256'],
                        'weights': handle, 'qualification': qualification_handle,
                        'qualified': qualification_handle is not None,
                        'qualification_scope': 'Synthetic finite/repeatable CPU inference; no musical accuracy'}, store_root)
    return model, result


def audio_model_inspect(*, store_root: str, request_id: str, declaration: AudioModelDeclaration,
                        qualification: Literal['none', 'synthetic_cpu_v1'] = 'none') -> dict:
    """Capture an optional local model runtime; explicitly qualify synthetic execution."""
    _declaration(declaration)
    executed = False
    def work():
        nonlocal executed
        executed = True
        try:
            model, result = _capture(declaration, qualification, store_root)
        except PocketError as error:
            if 'ModuleNotFoundError' not in str(error) and 'PackageNotFoundError' not in str(error):
                raise
            return receipt(request_id, status='unsupported', coverage=COVERAGE,
                           change_summary={'available': False, 'qualified': False},
                           uncertainty=['Declared optional runtime has missing dependencies; no model execution capability established.'])
        return receipt(request_id, artifacts={'model': model}, coverage=COVERAGE,
                       change_summary={'available': True, 'qualified': result['qualification'] is not None,
                                       'profile_sha256': result['profile_sha256']},
                       uncertainty=['Synthetic qualification does not establish pulse/downbeat accuracy.'])
    result = run_request(store_root, request_id, 'audio_model_inspect',
                         {'declaration': declaration, 'qualification': qualification}, work)
    if not executed and 'model' in result.get('artifacts', {}):
        model = read_record(result['artifacts']['model'], store_root, MODEL_SCHEMA)
        _runner({**declaration, 'expected_profile': model['profile_sha256']}, 'none',
                Path(store_root).expanduser().resolve() / model['weights']['artifact_uri'])
    return result


def prepare_pulse_input(*, source: AudioHypothesisSource, settings: AudioPulseSettings,
                        attribution: AudioHypothesisAttribution, store_root: str):
    """Public-provider shared source capture without running any model."""
    if canonical_bytes(settings) != canonical_bytes(SETTINGS):
        raise PocketError('Unsupported pulse settings')
    prepared = prepare_hypothesis_input(source=source, settings={'bpm_hint': None, 'beats_per_bar': 4},
                                        attribution=attribution, store_root=store_root)
    meta = prepared['source_metadata']
    if (meta['format'] != 'WAV' or meta['subtype'] != 'PCM_16' or meta['channels'] not in (1, 2)
            or meta['sample_rate'] not in (8000, 44100, 48000) or source['frames'] < 2 * meta['sample_rate']):
        raise PocketError('Qualified source requires mono/stereo PCM16 WAV at 8000/44100/48000 Hz, 2–20 seconds')
    prepared['settings'] = copy.deepcopy(settings)
    return prepared


def _peaks(values):
    candidates = [i for i, value in enumerate(values)
                  if value > 0 and value == max(values[max(0, i - 3):i + 4])]
    groups = []
    for index in candidates:
        if groups and index - float(sum(groups[-1], Fraction()) / len(groups[-1])) <= 1:
            groups[-1].append(Fraction(index))
        else:
            groups.append([Fraction(index)])
    return [{'frame': sum(group, Fraction()) / len(group), 'support': [int(x) for x in group]} for group in groups]


def project_pulse_analysis(analysis, source):
    """Independent minimal-postprocessor projection, including fractional peaks."""
    _fields(analysis, {'beat', 'downbeat', 'vendor_beats_seconds', 'vendor_downbeats_seconds',
                       'input', 'repeat_sha256', 'two_repeats_sample_exact'}, 'learned analysis')
    arrays = [analysis[name] for name in ('beat', 'downbeat')]
    if any(not isinstance(values, list) or not 1 <= len(values) <= 1002 for values in arrays) or len(arrays[0]) != len(arrays[1]):
        raise PocketError('Invalid learned model output shapes')
    for values in arrays:
        for value in values:
            if type(value) not in (int, float) or not -1e6 <= value <= 1e6 or not math.isfinite(value):
                raise PocketError('Invalid learned logits')
            if struct.unpack('<f', struct.pack('<f', value))[0] != value:
                raise PocketError('Logit is not an exact float32 value')
    _fields(analysis['repeat_sha256'], {'beat', 'downbeat'}, 'repeat hashes')
    for name, values in zip(('beat', 'downbeat'), arrays):
        actual_sha = hashlib.sha256(struct.pack('<' + 'f' * len(values), *values)).hexdigest()
        if analysis['repeat_sha256'].get(name) != [actual_sha, actual_sha]:
            raise PocketError('Missing exact repeated raw-array hashes')
    if analysis['two_repeats_sample_exact'] is not True:
        raise PocketError('Missing repeat proof')
    inp = analysis['input']
    _fields(inp, {'decoded_float64_sha256', 'mono_float64_sha256', 'resampled_float64_sha256', 'model_float32_sha256', 'resampled_frames', 'spect_shape'}, 'model preprocessing')
    for name in ('decoded_float64_sha256', 'mono_float64_sha256', 'resampled_float64_sha256', 'model_float32_sha256'):
        _sha(inp[name])
    if (type(inp['resampled_frames']) is not int or not 44100 <= inp['resampled_frames'] <= 441000
            or inp['spect_shape'] != [len(arrays[0]), 128]
            or len(arrays[0]) != inp['resampled_frames'] // 441 + 1):
        raise PocketError('Model frame/preprocessing shape mismatch')
    groups = [_peaks(values) for values in arrays]
    selected = [groups[0], []]
    for item in groups[1]:
        destination = min(groups[0], key=lambda x: abs(x['frame'] - item['frame']))['frame'] if groups[0] else item['frame']
        previous = next((row for row in selected[1] if row['frame'] == destination), None)
        if previous is None:
            selected[1].append({'frame': destination, 'support': item['support'], 'raw_frames': [item['frame']]})
        else:
            previous['support'] += item['support']
            previous['raw_frames'].append(item['frame'])
    selected[1].sort(key=lambda x: x['frame'])
    events, excluded = [], []
    def rational(value):
        return {'n': value.numerator, 'd': value.denominator}
    for kind, rows, actual in zip(('learned_beat', 'learned_downbeat'), selected,
                                  (analysis['vendor_beats_seconds'], analysis['vendor_downbeats_seconds'])):
        if not isinstance(actual, list) or len(actual) != len(rows):
            raise PocketError('Vendor postprocessor count differs from retained logits')
        for item, time in zip(rows, actual):
            if type(time) not in (int, float) or not math.isfinite(time) or abs(time - float(item['frame'] / 50)) > 1e-9:
                raise PocketError('Vendor postprocessor differs from retained logits')
            coordinate = Fraction(source['start_frame']) + item['frame'] * source['sample_rate'] / 50
            row = {'kind': kind, 'model_frame_q': rational(item['frame']), 'source_frame_q': rational(coordinate),
                   'support_indices': item['support'], 'raw_peak_frames_q': [rational(x) for x in item.get('raw_frames', [item['frame']])],
                   'score_interpretation': 'Uncalibrated model logits; not probability of correctness'}
            if source['start_frame'] <= coordinate < source['end_frame_exclusive']:
                events.append(row)
            else:
                excluded.append({**row, 'reason': 'Outside half-open source crop; not clamped'})
    return {'events': events, 'excluded': excluded}


def _annotations(evidence, projection):
    rows = []
    for index, value in enumerate(projection['events']):
        row = {'annotation': copy.deepcopy(value), 'support': [{'kind': 'analysis_pointer', 'reference': f'/analysis/projection/events/{index}'}],
               'uncertainty': ['Learned pulse/downbeat hypotheses can be wrong, including at crop boundaries.'],
               'attribution': {'actor': 'BeatThis', 'actor_kind': 'algorithm', 'analysis_version': 'beat_this_cpu_v1'},
               'supersedes': []}
        row['annotation_id'] = 'audio:' + digest([evidence, row])
        rows.append(row)
    return rows


def load_audio_model(handle, store_root, *, require_qualified=True):
    """Validate retained profile and actual bounded synthetic proof, never caller flags."""
    _verify_handles(handle, store_root)
    try:
        model = read_record(handle, store_root, MODEL_SCHEMA)
        _fields(model, {'schema', 'declaration', 'binding', 'profile', 'profile_sha256', 'weights', 'qualification', 'qualified', 'qualification_scope'}, 'retained model')
        _fields(model['declaration'], {'adapter', 'executable', 'weights', 'expected_profile'}, 'retained model declaration')
        if model['declaration']['adapter'] != 'beat_this_cpu_v1' or model['declaration']['expected_profile'] != model['profile_sha256']:
            raise PocketError('Retained adapter/profile declaration mismatch')
        profile = read_record(model['profile'], store_root, 'pocket.audio-runtime-profile/v1')
        if digest(profile['profile']) != model['profile_sha256'] or profile['profile_sha256'] != model['profile_sha256']:
            raise PocketError('Model runtime profile mismatch')
        if model['weights']['sha256'] != model['declaration']['weights']['sha256']:
            raise PocketError('Model weight binding mismatch')
        if require_qualified:
            qualification = read_record(model['qualification'], store_root, 'pocket.audio-model-qualification/v1')
            if qualification['profile'] != model['profile'] or qualification['weights'] != model['weights'] or model['qualified'] is not True:
                raise PocketError('Qualification model/profile mismatch')
            proof = qualification['qualification']
            expected = [('silence', 8000, 1, 16000), ('impulses', 44100, 1, 176400),
                        ('competing', 48000, 1, 192000), ('offgrid', 8000, 1, 32000),
                        ('antiphase', 48000, 2, 192000), ('two_tones', 44100, 1, 176400),
                        ('max_crop', 48000, 1, 960000)]
            if proof['profile'] != 'synthetic_cpu_v1' or proof['passed'] is not True or len(proof['cases']) != len(expected):
                raise PocketError('Missing full synthetic qualification')
            for case, parameters in zip(proof['cases'], expected):
                if tuple(case[key] for key in ('name', 'rate', 'channels', 'frames')) != parameters:
                    raise PocketError('Synthetic case differs from qualification contract')
                project_pulse_analysis(case['result'], {'start_frame': 0, 'end_frame_exclusive': case['frames'], 'sample_rate': case['rate']})
        return model
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, struct.error) as error:
        raise PocketError('Malformed retained model qualification') from error


def _validate_source_evidence(source, payload, raw):
    _fields(source, {'sha256', 'frames', 'sample_rate', 'channels', 'format', 'subtype',
                     'start_frame', 'end_frame_exclusive', 'source_origin'}, 'learned source')
    if any(type(source[key]) is not int for key in ('frames', 'sample_rate', 'channels', 'start_frame', 'end_frame_exclusive')):
        raise PocketError('Source counts require strict integers')
    if hashlib.sha256(payload).hexdigest() != source['sha256']:
        raise PocketError('Source identity mismatch')
    try:
        with wave.open(io.BytesIO(payload), 'rb') as stream:
            rate, channels, frames = stream.getframerate(), stream.getnchannels(), stream.getnframes()
            if (stream.getsampwidth() != 2 or stream.getcomptype() != 'NONE' or channels not in (1, 2)
                    or rate not in (8000, 44100, 48000) or source['format'] != 'WAV' or source['subtype'] != 'PCM_16'
                    or (rate, channels, frames) != (source['sample_rate'], source['channels'], source['frames'])):
                raise PocketError('Source metadata differs from retained PCM')
            start, end = source['start_frame'], source['end_frame_exclusive']
            if (type(start) is not int or type(end) is not int or not 0 <= start < end <= frames
                    or not 2 * rate <= end - start <= 20 * rate
                    or source['source_origin'] not in ('independently_acquired', 'user_recording')):
                raise PocketError('Invalid retained source crop')
            stream.setpos(start)
            data = stream.readframes(end - start)
        decoded = [sample[0] / 32768 for sample in struct.iter_unpack('<h', data)]
        if len(decoded) != (end - start) * channels:
            raise PocketError('Truncated retained PCM')
        mono = [sum(decoded[i:i + channels]) / channels for i in range(0, len(decoded), channels)]
        decoded_sha = hashlib.sha256(struct.pack('<' + 'd' * len(decoded), *decoded)).hexdigest()
        mono_sha = hashlib.sha256(struct.pack('<' + 'd' * len(mono), *mono)).hexdigest()
        if raw['input']['decoded_float64_sha256'] != decoded_sha or raw['input']['mono_float64_sha256'] != mono_sha:
            raise PocketError('Retained model decoded crop differs from source bytes')
        expected_resampled = ((end - start) * 22050 * 2 + rate) // (2 * rate)
        if raw['input']['resampled_frames'] != expected_resampled:
            raise PocketError('Retained resampled length differs from exact source crop')
    except (wave.Error, EOFError) as error:
        raise PocketError('Invalid retained PCM source') from error


def load_pulse_hypotheses(handle, store_root):
    """Validate a full initial semantic record for query/correction dispatchers."""
    _verify_handles(handle, store_root)
    record = read_record(handle, store_root, SCHEMA)
    try:
        _fields(record, {'schema', 'source', 'original', 'analysis', 'model', 'annotations', 'parent', 'revision_index', 'annotation_count', 'request_attribution', 'settings', 'limitations'}, 'initial learned record')
        if canonical_bytes(record['settings']) != canonical_bytes(SETTINGS):
            raise PocketError('Retained learned settings changed')
        _attribution(record['request_attribution'])
        evidence = read_record(record['analysis'], store_root, ANALYSIS_SCHEMA)
        _fields(evidence, {'schema', 'analysis', 'arrays', 'original', 'model', 'transform'}, 'retained learned analysis')
        _fields(evidence['analysis'], {'raw', 'projection'}, 'analysis payload')
        _fields(evidence['arrays'], {'beat', 'downbeat'}, 'raw array handles')
        if canonical_bytes(evidence['transform']) != canonical_bytes(TRANSFORM):
            raise PocketError('Retained model transform changed')
        _validate_source_evidence(record['source'], read_bytes(record['original'], store_root), evidence['analysis']['raw'])
        projection = project_pulse_analysis(evidence['analysis']['raw'], record['source'])
        if evidence['analysis']['projection'] != projection or record['annotations'] != _annotations(record['analysis'], projection):
            raise PocketError('Learned annotations differ from retained raw evidence')
        if (record['parent'] is not None or type(record['revision_index']) is not int or record['revision_index'] != 0
                or type(record['annotation_count']) is not int or record['annotation_count'] != len(record['annotations'])):
            raise PocketError('Expected initial learned hypothesis record')
        model = load_audio_model(record['model'], store_root)
        if not model['qualified'] or model['qualification'] is None:
            raise PocketError('Learned evidence lacks qualification')
        profile = read_record(model['profile'], store_root, 'pocket.audio-runtime-profile/v1')
        if digest(profile['profile']) != model['profile_sha256'] or profile['profile_sha256'] != model['profile_sha256']:
            raise PocketError('Runtime profile invalid')
        if record['original']['sha256'] != record['source']['sha256'] or evidence['original'] != record['original'] or evidence['model'] != record['model']:
            raise PocketError('Learned source/model bindings disagree')
        for name in ('beat', 'downbeat'):
            raw = struct.pack('<' + 'f' * len(evidence['analysis']['raw'][name]), *evidence['analysis']['raw'][name])
            if read_bytes(evidence['arrays'][name], store_root) != raw:
                raise PocketError('Raw learned array bytes disagree')
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, struct.error) as error:
        raise PocketError('Malformed retained learned evidence') from error
    return record


def prepare_pulse_model(*, model: AudioPulseModel, store_root: str):
    """Validate explicit model inputs and local bytes without inference or publication."""
    if not isinstance(model, dict):
        raise PocketError('Expected explicit model declaration or handle')
    if model.get('kind') == 'inline':
        _fields(model, {'kind', 'declaration', 'qualification'}, 'inline model')
        if model['qualification'] != 'synthetic_cpu_v1':
            raise PocketError('Inline execution requires explicit synthetic qualification')
        declaration = model['declaration']
    elif model.get('kind') == 'inspected':
        _fields(model, {'kind', 'model'}, 'inspected model')
        _verify_handles(model['model'], store_root)
        prior = load_audio_model(model['model'], store_root)
        if not prior.get('qualified') or prior.get('qualification') is None:
            raise PocketError('Inspected model is not qualified')
        declaration = {**prior['declaration'], 'expected_profile': prior['profile_sha256']}
    else:
        raise PocketError('Unknown model input')
    _declaration(declaration)
    return copy.deepcopy(declaration)


def audio_pulse_hypotheses(*, store_root: str, request_id: str, source: AudioHypothesisSource,
                           model: AudioPulseModel, settings: AudioPulseSettings,
                           attribution: AudioHypothesisAttribution) -> dict:
    """Capture bounded uncertain learned pulse evidence from exact local source bytes."""
    _attribution(attribution)
    declaration = prepare_pulse_model(model=model, store_root=store_root)
    prepared = prepare_pulse_input(source=source, settings=settings, attribution=attribution, store_root=store_root)
    executed = False
    def work():
        nonlocal executed
        executed = True
        model_handle, result = _capture(declaration, 'none' if model['kind'] == 'inspected' else 'synthetic_cpu_v1',
                                        store_root, prepared['source'],
                                        qualified_model=model['model'] if model['kind'] == 'inspected' else None)
        raw = result['analysis']
        projection = project_pulse_analysis(raw, prepared['source_metadata'])
        arrays = {name: put_bytes(struct.pack('<' + 'f' * len(raw[name]), *raw[name]), store_root,
                                  name + '.f32', 'pocket.model-logits-float32le/v1') for name in ('beat', 'downbeat')}
        evidence = put_record({'schema': ANALYSIS_SCHEMA, 'analysis': {'raw': raw, 'projection': projection},
                               'arrays': arrays, 'original': prepared['source_handle'], 'model': model_handle,
                               'transform': copy.deepcopy(TRANSFORM)}, store_root)
        rows = _annotations(evidence, projection)
        _cancel()
        handle = put_record({'schema': SCHEMA, 'source': prepared['source_metadata'],
                             'original': prepared['source_handle'], 'analysis': evidence, 'model': model_handle,
                             'annotations': rows, 'parent': None, 'revision_index': 0, 'annotation_count': len(rows),
                             'request_attribution': copy.deepcopy(attribution), 'settings': copy.deepcopy(settings),
                             'limitations': ['Uncalibrated learned hypotheses; no listening, transcription or native qualification.']}, store_root)
        load_pulse_hypotheses(handle, store_root)
        return receipt(request_id, artifacts={'hypotheses': handle}, coverage=COVERAGE,
                       change_summary={'annotations': len(rows), 'excluded_boundary_events': len(projection['excluded']), 'revision_index': 0},
                       uncertainty=['Retain unchanged music and alternative pulse/meter interpretations.'])
    result = run_request(store_root, request_id, 'audio_pulse_hypotheses',
                         {'source': source, 'model': model, 'settings': settings, 'attribution': attribution}, work)
    validated = load_pulse_hypotheses(result['artifacts']['hypotheses'], store_root)
    if not executed:
        retained_model = read_record(validated['model'], store_root, MODEL_SCHEMA)
        _runner({**declaration, 'expected_profile': retained_model['profile_sha256']}, 'none',
                Path(store_root).expanduser().resolve() / retained_model['weights']['artifact_uri'])
    return result
