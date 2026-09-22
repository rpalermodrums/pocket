"""Owner contracts; real optional model runs are retained separately outside fixtures."""
import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from pocket_music.artifact_store import digest, read_record
from pocket_music.audio_pulse_hypotheses import (
    _CONTEXT,
    ANALYSIS_SCHEMA,
    SETTINGS,
    audio_model_inspect,
    audio_pulse_hypotheses,
    load_pulse_hypotheses,
    model_execution_context,
    project_pulse_analysis,
)
from pocket_music.errors import PocketError


def raw(frames=101):
    values = [0.] * frames
    h = hashlib.sha256(struct.pack('<' + 'f' * frames, *values)).hexdigest()
    return {'beat': values.copy(), 'downbeat': values.copy(), 'vendor_beats_seconds': [],
            'vendor_downbeats_seconds': [], 'repeat_sha256': {'beat': [h, h], 'downbeat': [h, h]},
            'input': {'decoded_float64_sha256': '0' * 64, 'mono_float64_sha256': '0' * 64,
                      'resampled_float64_sha256': '0' * 64, 'model_float32_sha256': '0' * 64,
                      'resampled_frames': (frames - 1) * 441, 'spect_shape': [frames, 128]},
            'two_repeats_sample_exact': True}


def rehash(value):
    for name in ('beat', 'downbeat'):
        h = hashlib.sha256(struct.pack('<' + 'f' * len(value[name]), *value[name])).hexdigest()
        value['repeat_sha256'][name] = [h, h]
    return value


class PulseModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = str(self.root / 'store')
        weights = self.root / 'weights.ckpt'
        weights.write_bytes(b'explicit mocked checkpoint, never deserialized')
        executable = Path(sys.executable).resolve()
        self.declaration = {'adapter': 'beat_this_cpu_v1',
                            'executable': {'path': str(executable), 'sha256': hashlib.sha256(executable.read_bytes()).hexdigest()},
                            'weights': {'path': str(weights), 'sha256': hashlib.sha256(weights.read_bytes()).hexdigest()},
                            'expected_profile': None}
        path = self.root / 'source.wav'
        with wave.open(str(path), 'wb') as out:
            out.setparams((1, 2, 8000, 16137, 'NONE', 'not compressed'))
            out.writeframes(b'\0\0' * 16137)
        self.source = {'path': str(path), 'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                       'start_frame': 137, 'frames': 16000, 'source_origin': 'independently_acquired'}
        self.actor = {'actor': 'test', 'actor_kind': 'agent', 'statement': 'Explicit synthetic fixture', 'uncertainty': []}
        self.model = {'kind': 'inline', 'declaration': self.declaration, 'qualification': 'synthetic_cpu_v1'}

    def runner(self, declaration, qualification, weights_path, source=None):
        profile = {'test': 'mock runtime, not scientific evidence'}
        cases = []
        for name, rate, channels, seconds in [('silence', 8000, 1, 2), ('impulses', 44100, 1, 4),
                ('competing', 48000, 1, 4), ('offgrid', 8000, 1, 4), ('antiphase', 48000, 2, 4),
                ('two_tones', 44100, 1, 4), ('max_crop', 48000, 1, 20)]:
            cases.append({'name': name, 'rate': rate, 'channels': channels, 'frames': rate * seconds,
                          'result': raw(seconds * 50 + 1)})
        result = {'profile': profile, 'profile_sha256': digest(profile),
                  'qualification': {'profile': 'synthetic_cpu_v1', 'passed': True, 'cases': cases} if qualification != 'none' else None,
                  'analysis': raw() if source else None}
        if source:
            source_hash = hashlib.sha256(b'\0' * (source['frames'] * 8)).hexdigest()
            result['analysis']['input']['decoded_float64_sha256'] = source_hash
            result['analysis']['input']['mono_float64_sha256'] = source_hash
        return result, {'test': 'mock binding'}

    def call(self, request='pulse', **changes):
        args = {'store_root': self.store, 'request_id': request, 'source': self.source,
                'model': self.model, 'settings': SETTINGS, 'attribution': self.actor}
        args.update(changes)
        return audio_pulse_hypotheses(**args)

    def test_external_inline_and_inspected_equivalent(self):
        with patch('pocket_music.audio_pulse_hypotheses._runner', side_effect=self.runner) as runner:
            inspected = audio_model_inspect(store_root=self.store, request_id='inspect', declaration=self.declaration, qualification='synthetic_cpu_v1')
            first = self.call('inline')
            second = self.call('inspected', model={'kind': 'inspected', 'model': inspected['artifacts']['model']})
            self.assertEqual(runner.call_args.args[1], 'none')
            self.assertIsNotNone(runner.call_args.args[3])
            self.assertEqual([call.args[1] for call in runner.call_args_list], ['synthetic_cpu_v1', 'synthetic_cpu_v1', 'none'])
        left = load_pulse_hypotheses(first['artifacts']['hypotheses'], self.store)
        right = load_pulse_hypotheses(second['artifacts']['hypotheses'], self.store)
        self.assertEqual(first['artifacts'], second['artifacts'])
        self.assertEqual(left['source'], right['source'])
        self.assertEqual(left['annotations'], right['annotations'])
        self.assertLess(len(json.dumps(first)), 4096)

    def test_retained_qualification_profile_mismatch_refuses(self):
        with patch('pocket_music.audio_pulse_hypotheses._runner', side_effect=self.runner):
            inspected = audio_model_inspect(store_root=self.store, request_id='qualified', declaration=self.declaration, qualification='synthetic_cpu_v1')
        def changed_profile(*args):
            result, binding = self.runner(*args)
            result['profile'] = {'test': 'changed runtime'}
            result['profile_sha256'] = digest(result['profile'])
            return result, binding
        with (patch('pocket_music.audio_pulse_hypotheses._runner', side_effect=changed_profile),
              self.assertRaisesRegex(PocketError, 'qualification differs')):
            self.call(model={'kind': 'inspected', 'model': inspected['artifacts']['model']})

    def test_replay_rechecks_runtime_without_inference(self):
        with patch('pocket_music.audio_pulse_hypotheses._runner', side_effect=self.runner) as runner:
            result = self.call()
            self.assertEqual(self.call(), result)
            self.assertEqual(runner.call_args.args[1], 'none')
        with (patch('pocket_music.audio_pulse_hypotheses._runner', side_effect=PocketError('Runtime profile changed')),
              self.assertRaisesRegex(PocketError, 'profile changed')):
            self.call()

    def test_source_mutation_refuses_replay(self):
        with patch('pocket_music.audio_pulse_hypotheses._runner', side_effect=self.runner):
            self.call()
            with Path(self.source['path']).open('ab') as stream:
                stream.write(b'changed')
            with self.assertRaisesRegex(PocketError, 'SHA256'):
                self.call()

    def test_unqualified_handle_refused(self):
        with patch('pocket_music.audio_pulse_hypotheses._runner', side_effect=self.runner):
            result = audio_model_inspect(store_root=self.store, request_id='inspect', declaration=self.declaration)
            with self.assertRaises(PocketError):
                self.call(model={'kind': 'inspected', 'model': result['artifacts']['model']})

    def test_missing_optional_runtime_is_unavailable(self):
        with patch('pocket_music.audio_pulse_hypotheses._runner', side_effect=PocketError('Optional model runner refused: PackageNotFoundError: torch')):
            result = audio_model_inspect(store_root=self.store, request_id='inspect', declaration=self.declaration)
        self.assertEqual(result['status'], 'unsupported')
        self.assertFalse(result['change_summary']['available'])
        self.assertFalse(result['artifacts'])

    def test_real_base_environment_does_not_require_torch(self):
        result = audio_model_inspect(store_root=self.store, request_id='base-no-model', declaration=self.declaration)
        self.assertEqual(result['status'], 'unsupported')

    def test_explicit_lease_context_is_scoped(self):
        with (self.root / 'lease').open('wb') as stream:
            with model_execution_context(lease_fds=(stream.fileno(),)):
                self.assertEqual(_CONTEXT.get()[0], (stream.fileno(),))
                os.fstat(stream.fileno())
            self.assertEqual(_CONTEXT.get(), ((), None))
        with self.assertRaises(PocketError), model_execution_context(lease_fds=('pid',)):
            pass

    def test_preparation_cancel_never_dispatches(self):
        with (model_execution_context(cancellation_check=lambda: (_ for _ in ()).throw(PocketError('cancelled'))),
              self.assertRaisesRegex(PocketError, 'cancelled')):
            self.call()
        self.assertFalse(list(Path(self.store).glob('artifacts/*/record.json')))

    def test_projection_fractional_and_half_open_endpoint(self):
        value = raw()
        value['beat'][20:22] = [1., 1.]
        value['beat'][-1] = 2.
        value['downbeat'][23] = 1.
        value['vendor_beats_seconds'] = [.41, 2.]
        value['vendor_downbeats_seconds'] = [.41]
        result = project_pulse_analysis(rehash(value), {'start_frame': 137, 'end_frame_exclusive': 16137, 'sample_rate': 8000})
        self.assertEqual(result['events'][0]['model_frame_q'], {'n': 41, 'd': 2})
        self.assertEqual(result['events'][0]['source_frame_q'], {'n': 3417, 'd': 1})
        self.assertEqual(result['events'][1]['raw_peak_frames_q'], [{'n': 23, 'd': 1}])
        self.assertEqual(len(result['excluded']), 1)

    def test_malformed_logits_and_repeat_refuse(self):
        for change in ('nan', 'huge', 'not_float32', 'repeat', 'shape', 'vendor'):
            with self.subTest(change=change):
                value = raw()
                if change == 'nan': value['beat'][0] = float('nan')
                if change == 'huge': value['beat'][0] = 10**500
                if change == 'not_float32': value['beat'][0] = .1
                if change == 'repeat': value['repeat_sha256']['beat'][1] = 'f' * 64
                if change == 'shape': value['input']['spect_shape'] = [100, 128]
                if change == 'vendor': value['vendor_beats_seconds'] = [0.]
                with self.assertRaises(PocketError):
                    project_pulse_analysis(value, {'start_frame': 0, 'end_frame_exclusive': 16000, 'sample_rate': 8000})

    def test_extra_fields_and_settings_refuse(self):
        for change in ({'model': {**self.model, 'hidden': True}}, {'settings': {**SETTINGS, 'seed': True}},
                       {'model': {**self.model, 'qualification': 'none'}}):
            with self.subTest(change=change), self.assertRaises(PocketError):
                self.call(**change)

    def test_changed_weight_refuses_before_runner(self):
        Path(self.declaration['weights']['path']).write_bytes(b'changed')
        with patch('pocket_music.audio_pulse_hypotheses._runner') as runner:
            with self.assertRaisesRegex(PocketError, 'digest'):
                self.call()
            runner.assert_not_called()

    def test_raw_array_binding_and_source_preservation(self):
        before = Path(self.source['path']).read_bytes()
        with patch('pocket_music.audio_pulse_hypotheses._runner', side_effect=self.runner):
            receipt = self.call()
        self.assertEqual(Path(self.source['path']).read_bytes(), before)
        record = load_pulse_hypotheses(receipt['artifacts']['hypotheses'], self.store)
        evidence = read_record(record['analysis'], self.store, ANALYSIS_SCHEMA)
        path = Path(self.store) / evidence['arrays']['beat']['artifact_uri']
        path.write_bytes(b'forged')
        with self.assertRaises(PocketError):
            load_pulse_hypotheses(receipt['artifacts']['hypotheses'], self.store)
