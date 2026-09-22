"""Independent learned-pulse protocol and evidence adversaries; no optional inference."""
import copy
import hashlib
import json
import struct
import subprocess
import sys
import wave
from pathlib import Path
from unittest.mock import patch

import pytest

import pocket_music.audio_pulse_hypotheses as pulse
from pocket_music.artifact_store import digest, put_record, read_record
from pocket_music.errors import PocketError


def logits(seconds=2):
    count = seconds * 50 + 1
    values = [0.0] * count
    hashed = hashlib.sha256(struct.pack('<' + 'f' * count, *values)).hexdigest()
    return {'beat': values.copy(), 'downbeat': values.copy(), 'vendor_beats_seconds': [],
            'vendor_downbeats_seconds': [], 'repeat_sha256': {'beat': [hashed, hashed], 'downbeat': [hashed, hashed]},
            'input': {'decoded_float64_sha256': '0' * 64, 'mono_float64_sha256': '0' * 64,
                      'resampled_float64_sha256': '0' * 64, 'model_float32_sha256': '0' * 64,
                      'resampled_frames': seconds * 22050, 'spect_shape': [count, 128]},
            'two_repeats_sample_exact': True}


@pytest.fixture
def setup(tmp_path):
    executable = Path(sys.executable).resolve()
    weights = tmp_path / 'declared-weights'; weights.write_bytes(b'NOT A REAL MODEL')
    source = tmp_path / 'source.wav'
    with wave.open(str(source), 'wb') as out:
        out.setparams((1, 2, 8000, 18000, 'NONE', 'not compressed'))
        out.writeframes(b'\0\0' * 18000)
    declaration = {'adapter': 'beat_this_cpu_v1', 'expected_profile': None,
                   'executable': {'path': str(executable), 'sha256': hashlib.sha256(executable.read_bytes()).hexdigest()},
                   'weights': {'path': str(weights), 'sha256': hashlib.sha256(weights.read_bytes()).hexdigest()}}
    args = {'store_root': str(tmp_path / 'store'), 'request_id': 'independent',
            'source': {'path': str(source), 'expected_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                       'start_frame': 701, 'frames': 16000, 'source_origin': 'independently_acquired'},
            'model': {'kind': 'inline', 'declaration': declaration, 'qualification': 'synthetic_cpu_v1'},
            'settings': pulse.SETTINGS,
            'attribution': {'actor': 'independent QA', 'actor_kind': 'agent', 'statement': 'Synthetic only', 'uncertainty': []}}
    return args, declaration


def fake_runner(declaration, qualification, weights_path, source=None):
    profile = {'explicitly_mocked': True}
    cases = [{'name': name, 'rate': rate, 'channels': channels, 'frames': seconds * rate, 'result': logits(seconds)}
             for name, rate, channels, seconds in [('silence', 8000, 1, 2), ('impulses', 44100, 1, 4),
                ('competing', 48000, 1, 4), ('offgrid', 8000, 1, 4), ('antiphase', 48000, 2, 4),
                ('two_tones', 44100, 1, 4), ('max_crop', 48000, 1, 20)]]
    analysis = logits() if source else None
    if source:
        exact = hashlib.sha256(bytes(16000 * 8)).hexdigest()
        analysis['input']['decoded_float64_sha256'] = exact
        analysis['input']['mono_float64_sha256'] = exact
    return {'profile': profile, 'profile_sha256': digest(profile),
            'qualification': {'profile': 'synthetic_cpu_v1', 'passed': True, 'cases': cases} if qualification != 'none' else None,
            'analysis': analysis}, {'mocked': True}


@pytest.mark.parametrize('bad', [None, [], 'hash', 7])
def test_malformed_repeat_proof_is_domain_refusal(bad):
    raw = logits(); raw['repeat_sha256'] = bad
    with pytest.raises(PocketError):
        pulse.project_pulse_analysis(raw, {'start_frame': 701, 'end_frame_exclusive': 16701, 'sample_rate': 8000})


def test_fractional_plateau_and_false_boundary_remain_explicit():
    raw = logits(); raw['beat'][1:3] = [1., 1.]; raw['beat'][100] = 2.
    raw['vendor_beats_seconds'] = [.03, 2.]
    for name in ('beat', 'downbeat'):
        h = hashlib.sha256(struct.pack('<101f', *raw[name])).hexdigest()
        raw['repeat_sha256'][name] = [h, h]
    result = pulse.project_pulse_analysis(raw, {'start_frame': 701, 'end_frame_exclusive': 16701, 'sample_rate': 8000})
    assert result['events'][0]['model_frame_q'] == {'n': 3, 'd': 2}
    assert result['events'][0]['source_frame_q'] == {'n': 941, 'd': 1}
    assert result['excluded'][0]['source_frame_q'] == {'n': 16701, 'd': 1}
    assert result['excluded'][0]['support_indices'] == [100]
    assert raw['beat'][100] == 2.


@pytest.mark.parametrize('response', [{'status': 'ok'}, {'status': 'ok', 'result': None},
                                     {'status': 'ok', 'result': []}, {'status': 'ok', 'result': {'profile': {}}}])
def test_malformed_child_response_is_domain_refusal(setup, response):
    _, declaration = setup
    def child(argv, **kwargs):
        Path(argv[-1]).write_text(json.dumps(response))
        return subprocess.CompletedProcess(argv, 0)
    with patch.object(pulse.subprocess, 'run', side_effect=child), pytest.raises(PocketError):
        pulse._runner(declaration, 'none', declaration['weights']['path'])


def test_timeout_and_owned_lease_are_scoped(setup, tmp_path):
    _, declaration = setup
    with (tmp_path / 'lease').open('wb') as lease:
        def child(argv, **kwargs):
            assert kwargs['pass_fds'] == (lease.fileno(),)
            assert kwargs['timeout'] == 60
            assert argv[1:3] == ['-I', '-B']
            assert kwargs['env']['HF_HUB_OFFLINE'] == '1'
            raise subprocess.TimeoutExpired(argv, 60)
        with (pulse.model_execution_context(lease_fds=(lease.fileno(),)),
              patch.object(pulse.subprocess, 'run', side_effect=child),
              pytest.raises(PocketError, match='60 seconds')):
            pulse._runner(declaration, 'none', declaration['weights']['path'])
    assert pulse._CONTEXT.get() == ((), None)


@pytest.mark.parametrize('field', ['settings', 'source', 'transform', 'attribution'])
def test_resealed_semantic_context_cannot_lie(setup, field):
    args, _ = setup
    with patch.object(pulse, '_runner', side_effect=fake_runner):
        result = pulse.audio_pulse_hypotheses(**args)
    record = copy.deepcopy(read_record(result['artifacts']['hypotheses'], args['store_root']))
    if field == 'settings': record['settings']['seed'] = 999
    if field == 'source': record['source']['sample_rate'] = 44100
    if field == 'attribution': record['request_attribution']['actor_kind'] = 'made_up'
    if field == 'transform':
        evidence = read_record(record['analysis'], args['store_root'])
        evidence['transform']['model_fps'] = 99
        record['analysis'] = put_record(evidence, args['store_root'])
    forged = put_record(record, args['store_root'])
    with pytest.raises(PocketError): pulse.load_pulse_hypotheses(forged, args['store_root'])


def test_inline_and_inspected_exact_semantic_identity(setup):
    args, declaration = setup
    with patch.object(pulse, '_runner', side_effect=fake_runner):
        inspected = pulse.audio_model_inspect(store_root=args['store_root'], request_id='inspect', declaration=declaration, qualification='synthetic_cpu_v1')
        direct = pulse.audio_pulse_hypotheses(**args)
        composed = pulse.audio_pulse_hypotheses(**{**args, 'request_id': 'composed', 'model': {'kind': 'inspected', 'model': inspected['artifacts']['model']}})
    assert direct['artifacts']['hypotheses'] == composed['artifacts']['hypotheses']


@pytest.mark.parametrize('target', ['raw', 'array', 'model_weights', 'profile', 'crop', 'parent'])
def test_resealed_transitive_evidence_refuses(setup, target):
    args, _ = setup
    with patch.object(pulse, '_runner', side_effect=fake_runner):
        result = pulse.audio_pulse_hypotheses(**args)
    record = read_record(result['artifacts']['hypotheses'], args['store_root'])
    if target == 'crop': record['source']['start_frame'] = True
    elif target == 'parent': record['parent'] = result['artifacts']['hypotheses']
    elif target in ('raw', 'array'):
        evidence = read_record(record['analysis'], args['store_root'])
        if target == 'raw': evidence['analysis']['raw']['beat'][1] = 3.
        else: evidence['arrays']['beat'] = evidence['original']
        record['analysis'] = put_record(evidence, args['store_root'])
    else:
        model = read_record(record['model'], args['store_root'])
        if target == 'model_weights': model['declaration']['weights']['sha256'] = 'f' * 64
        else: model['profile_sha256'] = 'f' * 64
        record['model'] = put_record(model, args['store_root'])
    with pytest.raises(PocketError):
        pulse.load_pulse_hypotheses(put_record(record, args['store_root']), args['store_root'])


def test_runner_weight_race_refuses(setup):
    _, declaration = setup
    def child(argv, **kwargs):
        response, _ = fake_runner(declaration, 'none', None)
        Path(argv[-1]).write_text(json.dumps({'status': 'ok', 'result': response}))
        Path(declaration['weights']['path']).write_bytes(b'changed while child ran')
        return subprocess.CompletedProcess(argv, 0)
    with patch.object(pulse.subprocess, 'run', side_effect=child), pytest.raises(PocketError):
        pulse._runner(declaration, 'none', declaration['weights']['path'])


def test_cancel_after_child_never_returns_success(setup):
    _, declaration = setup
    checks = 0
    def check():
        nonlocal checks
        checks += 1
        if checks == 2: raise PocketError('cancel at result boundary')
    def child(argv, **kwargs):
        response, _ = fake_runner(declaration, 'none', None)
        Path(argv[-1]).write_text(json.dumps({'status': 'ok', 'result': response}))
        return subprocess.CompletedProcess(argv, 0)
    with (pulse.model_execution_context(cancellation_check=check),
          patch.object(pulse.subprocess, 'run', side_effect=child),
          pytest.raises(PocketError, match='cancel')):
        pulse._runner(declaration, 'none', declaration['weights']['path'])


def test_runner_decodes_verified_buffer_even_if_path_replaced(setup):
    import socket

    import pocket_music.audio_model_runner as runner
    args, declaration = setup
    source = args['source']
    original_wave_open = wave.open
    captured = []
    def racing_open(file, mode):
        Path(source['path']).write_bytes(b'concurrent replacement not valid audio')
        return original_wave_open(file, mode)
    def infer(model, pcm, rate):
        captured.append((pcm.shape, bool((pcm == 0).all()), rate))
        return {'mocked': True}
    request = {'qualification': 'none', 'expected_profile': None,
               'weights_path': declaration['weights']['path'], 'weights_sha256': declaration['weights']['sha256'],
               'source': source}
    with (patch.object(runner, 'profile', return_value={'mock': True}),
          patch.object(runner, 'load_model', return_value=object()),
          patch.object(runner, 'infer', side_effect=infer),
          patch.object(runner.wave, 'open', side_effect=racing_open),
          patch.object(socket, 'create_connection', socket.create_connection),
          patch.object(socket.socket, 'connect', socket.socket.connect),
          patch.object(socket.socket, 'connect_ex', socket.socket.connect_ex)):
        result = runner.execute(request)
    assert result['analysis'] == {'mocked': True}
    assert captured == [((16000, 1), True, 8000)]


def test_oversized_child_response_refuses(setup):
    _, declaration = setup
    def child(argv, **kwargs):
        with Path(argv[-1]).open('wb') as stream:
            stream.truncate(16 * 1024**2 + 1)
        return subprocess.CompletedProcess(argv, 0)
    with patch.object(pulse.subprocess, 'run', side_effect=child), pytest.raises(PocketError):
        pulse._runner(declaration, 'none', declaration['weights']['path'])


@pytest.mark.parametrize('change', ['relative', 'digest', 'adapter', 'profile', 'extra'])
def test_malformed_model_declaration_never_dispatches(setup, change):
    args, declaration = setup
    if change == 'relative': declaration['weights']['path'] = 'relative.ckpt'
    elif change == 'digest': declaration['weights']['sha256'] = 'F' * 64
    elif change == 'adapter': declaration['adapter'] = 'download_latest'
    elif change == 'profile': declaration['expected_profile'] = True
    else: declaration['network'] = True
    with patch.object(pulse, '_runner') as child, pytest.raises(PocketError):
        pulse.audio_pulse_hypotheses(**args)
    child.assert_not_called()


@pytest.fixture
def qualified(setup):
    args, declaration = setup
    with patch.object(pulse, '_runner', side_effect=fake_runner):
        result = pulse.audio_model_inspect(store_root=args['store_root'], request_id='reusable', declaration=declaration, qualification='synthetic_cpu_v1')
    return args, declaration, result['artifacts']['model']


def test_inspected_reuse_skips_qualification_but_keeps_target_repeats_and_lease(qualified, tmp_path):
    args, _, handle = qualified
    observed = []
    with (tmp_path / 'owned-lease').open('wb') as lease:
        def runner(declaration, qualification, weights_path, source=None):
            assert qualification == 'none' and source is not None
            assert source['start_frame'] == 701 and source['frames'] == 16000
            assert pulse._CONTEXT.get()[0] == (lease.fileno(),)
            observed.append(qualification)
            return fake_runner(declaration, qualification, weights_path, source)
        with pulse.model_execution_context(lease_fds=(lease.fileno(),)), patch.object(pulse, '_runner', side_effect=runner):
            result = pulse.audio_pulse_hypotheses(**{**args, 'model': {'kind': 'inspected', 'model': handle}})
    assert observed == ['none'] and pulse._CONTEXT.get() == ((), None)
    evidence = read_record(read_record(result['artifacts']['hypotheses'], args['store_root'])['analysis'], args['store_root'])
    assert evidence['analysis']['raw']['two_repeats_sample_exact'] is True
    for hashes in evidence['analysis']['raw']['repeat_sha256'].values():
        assert len(hashes) == 2 and hashes[0] == hashes[1]


@pytest.mark.parametrize('change', ['profile', 'qualification', 'target_repeat'])
def test_reused_model_still_rejects_changed_runtime_and_target_evidence(qualified, change):
    args, _, handle = qualified
    def runner(*a, **kw):
        result, binding = fake_runner(*a, **kw)
        if change == 'profile':
            result['profile'] = {'different': True}; result['profile_sha256'] = digest(result['profile'])
        elif change == 'qualification': result['qualification'] = {'unexpected': 'implicit requalification'}
        else: result['analysis']['repeat_sha256']['beat'][1] = 'f' * 64
        return result, binding
    with patch.object(pulse, '_runner', side_effect=runner), pytest.raises(PocketError):
        pulse.audio_pulse_hypotheses(**{**args, 'model': {'kind': 'inspected', 'model': handle}})
    assert not any(json.loads(p.read_text()).get('schema') == pulse.SCHEMA
                   for p in Path(args['store_root']).glob('artifacts/*/record.json'))


@pytest.mark.parametrize('change', ['missing_case', 'wrong_weights', 'unqualified'])
def test_resealed_reused_qualification_never_authorizes_runner(qualified, change):
    args, _, handle = qualified
    model = read_record(handle, args['store_root'])
    if change == 'unqualified': model['qualified'] = False
    else:
        proof = read_record(model['qualification'], args['store_root'])
        if change == 'missing_case': proof['qualification']['cases'].pop()
        else: proof['weights'] = model['profile']
        model['qualification'] = put_record(proof, args['store_root'])
    forged = put_record(model, args['store_root'])
    with patch.object(pulse, '_runner') as runner, pytest.raises(PocketError):
        pulse.audio_pulse_hypotheses(**{**args, 'model': {'kind': 'inspected', 'model': forged}})
    runner.assert_not_called()


def test_replacement_with_identical_weights_rebinds_without_requalification(setup):
    args, declaration = setup
    def bound_runner(*a, **kw):
        result, _ = fake_runner(*a, **kw)
        return result, pulse._declaration(a[0])[1]
    with patch.object(pulse, '_runner', side_effect=bound_runner):
        inspected = pulse.audio_model_inspect(store_root=args['store_root'], request_id='binding-inspect', declaration=declaration, qualification='synthetic_cpu_v1')
    old = read_record(inspected['artifacts']['model'], args['store_root'])
    path = Path(declaration['weights']['path']); replacement = path.with_name('replacement')
    replacement.write_bytes(path.read_bytes()); replacement.replace(path)
    with patch.object(pulse, '_runner', side_effect=bound_runner) as runner:
        result = pulse.audio_pulse_hypotheses(**{**args, 'model': {'kind': 'inspected', 'model': inspected['artifacts']['model']}})
    assert runner.call_args.args[1] == 'none'
    current = read_record(read_record(result['artifacts']['hypotheses'], args['store_root'])['model'], args['store_root'])
    assert current['weights'] == old['weights'] and current['qualification'] == old['qualification']
    assert current['binding']['weights']['stamp'] != old['binding']['weights']['stamp']


def test_reuse_cancel_after_target_analysis_publishes_no_hypotheses(qualified):
    args, _, handle = qualified
    finished = False
    def runner(*a, **kw):
        nonlocal finished
        finished = True
        return fake_runner(*a, **kw)
    def cancelled():
        if finished: raise PocketError('cancel after target')
    with (pulse.model_execution_context(cancellation_check=cancelled),
          patch.object(pulse, '_runner', side_effect=runner), pytest.raises(PocketError, match='cancel')):
        pulse.audio_pulse_hypotheses(**{**args, 'model': {'kind': 'inspected', 'model': handle}})
    assert not any(json.loads(p.read_text()).get('schema') == pulse.SCHEMA
                   for p in Path(args['store_root']).glob('artifacts/*/record.json'))
