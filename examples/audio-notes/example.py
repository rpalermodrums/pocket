# SPDX-License-Identifier: MIT
"""Explicit local note hypotheses; synthetic protocol example, never an audition."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import struct
import tempfile
import time
import wave
from pathlib import Path

from pocket_music import (
    audio_hypothesis_correct,
    audio_hypothesis_query,
    audio_note_hypotheses,
    audio_note_model_inspect,
    audio_note_submit,
    job_status,
)
from pocket_music.errors import PocketError

RATE = 22050
FRAMES = 2 * RATE
START = 5513
END = 38587
SETTINGS = {'device': 'cpu', 'dtype': 'float32', 'threads': 1,
            'downmix': 'arithmetic_mean', 'resampler': 'soxr_hq',
            'decoder': 'basic_pitch_0_4_0_false_false_v1',
            'onset_threshold': .5, 'frame_threshold': .3,
            'min_note_frames': 11, 'energy_tol': 11,
            'infer_onsets': False, 'melodia_trick': False}


def existing_absolute(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise argparse.ArgumentTypeError('Supply an existing absolute file path.')
    # Preserve the venv launcher path: resolving its symlink can select a different
    # Python environment even when the executable bytes have the same hash.
    return str(path)


def sha256(value):
    if not re.fullmatch('[0-9a-f]{64}', value):
        raise argparse.ArgumentTypeError('Supply a lowercase 64-character SHA256.')
    return value


def emit(value):
    encoded = json.dumps(value, indent=2, allow_nan=False)
    if len(encoded.encode()) > 65536:
        raise PocketError('Example output exceeds its 64KiB budget; use the retained bounded queries.')
    print(encoded)


def query(store, handle, view='summary'):
    return audio_hypothesis_query(store_root=store, hypotheses=handle, view=view,
                                  limit=4, max_bytes=8192)


def wait_owned(store, job_id):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            status = job_status(store_root=store, job_id=job_id)
        except PocketError as error:
            if 'busy' not in str(error):
                raise
        else:
            if status['state'] in ('completed', 'cancelled', 'failed', 'interrupted'):
                if status['state'] != 'completed':
                    raise PocketError(f'Owned job {job_id} ended {status["state"]}; inspect its retained evidence.')
                return status
        time.sleep(.2)
    raise PocketError(f'Wait ended for {job_id}; this does not establish cancellation. Inspect status; do not resubmit automatically.')


def run(args):
    root = Path(tempfile.mkdtemp(prefix='pocket-note-example-'))
    store = str(root / 'store')
    try:
        source_path = root / 'synthetic.wav'
        pcm = b''.join(struct.pack('<h', round(16000 * math.sin(2 * math.pi * 440 * frame / RATE))
                                  if START <= frame < END else 0)
                       for frame in range(FRAMES))
        with wave.open(str(source_path), 'wb') as output:
            output.setparams((1, 2, RATE, FRAMES, 'NONE', 'not compressed'))
            output.writeframes(pcm)
        source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
        declaration = {'adapter': 'basic_pitch_onnx_cpu_v1',
                       'executable': {'path': args.executable, 'sha256': args.executable_sha256},
                       'weights': {'path': args.weights, 'sha256': args.weights_sha256},
                       'expected_profile': None}
        inspected = audio_note_model_inspect(store_root=store, request_id='inspect', declaration=declaration,
                                             qualification='synthetic_onnx_cpu_v1')
        source = {'path': str(source_path), 'expected_sha256': source_sha, 'start_frame': 0,
                  'frames': FRAMES, 'source_origin': 'independently_acquired'}
        attribution = {'actor': 'Pocket synthetic example', 'actor_kind': 'agent',
                       'statement': 'Explicit synthetic 440Hz fixture; no human listening or musical approval.',
                       'uncertainty': ['A model may miss or invent notes; synthetic correctness is not musical acceptance.']}
        arguments = {'store_root': store, 'source': source,
                     'model': {'kind': 'inspected', 'model': inspected['artifacts']['model']},
                     'settings': SETTINGS, 'attribution': attribution}
        direct = audio_note_hypotheses(request_id='direct', **arguments)
        original = direct['artifacts']['hypotheses']
        initial_rows = query(store, original, 'annotations')
        worker = None
        if args.worker:
            submitted = audio_note_submit(request_id='owned-worker', **arguments)
            status = wait_owned(store, submitted['job']['job_id'])
            if status['result'] != original:
                raise PocketError('Direct/worker artifact identity differs; preserve both results for review.')
            worker = {'job_id': submitted['job']['job_id'], 'state': status['state'], 'direct_worker_equal': True}
        # This is an explicit authored alternative based on this script's exact
        # synthesis interval. It is not inferred from amplitude or model certainty.
        first_id = initial_rows['items'][0]['annotation_id'] if initial_rows['items'] else None
        corrected = audio_hypothesis_correct(store_root=store, request_id='synthetic-correction',
            parent=original, expected_revision=original['sha256'],
            corrections=[{'correction_id': 'declared-synthetic-tone', 'supersedes': [first_id] if first_id else [],
                          'annotation': {'kind': 'note_hypothesis', 'start_frame': START,
                                         'end_frame_exclusive': END, 'midi_note': 69, 'cents': 0,
                                         'tuning_ref': 'synthetic:12tet-a440'},
                          'support': [{'kind': 'analysis_pointer', 'reference': '/analysis/raw'}],
                          'uncertainty': ['Declared synthesis interval; not an acoustic onset measurement or model accuracy assessment.']}],
            attribution={**attribution, 'statement': 'Authored synthetic alternative using the explicitly generated 440Hz gate; all raw evidence remains retained.'})
        revised = corrected['artifacts']['hypotheses']
        unchanged = hashlib.sha256(source_path.read_bytes()).hexdigest() == source_sha
        if not unchanged:
            raise PocketError('Synthetic source changed; preserve the output directory for review.')
        manifest = {'schema': 'pocket.audio-note-example/v1', 'store_root': store,
                    'source_sha256': source_sha, 'model': inspected['artifacts']['model'],
                    'original': original, 'corrected': revised,
                    'synthetic_truth': {'sample_rate': RATE, 'frames': FRAMES, 'start_frame': START,
                                        'end_frame_exclusive': END, 'frequency_hz': 440},
                    'worker': worker, 'human_listening': 'not_performed'}
        manifest_path = root / 'example.json'
        manifest_path.write_text(json.dumps(manifest, indent=2, allow_nan=False) + '\n')
        emit({'status': 'ok', 'directory': str(root), 'manifest': str(manifest_path),
              'source_unchanged': unchanged, 'worker': worker,
              'inspection': inspected, 'direct': direct, 'correction': corrected,
              'original_summary': query(store, original), 'corrected_annotations': query(store, revised, 'annotations'),
              'musical_acceptance': 'not_established'})
    except (PocketError, OSError, ValueError) as error:
        emit({'status': 'failed', 'directory': str(root), 'store_root': store,
              'error': str(error)[:2048], 'action': 'Retain evidence and inspect the failure; do not automatically retry.'})
        return 1
    return 0


def retained(args):
    path = Path(args.manifest)
    if path.stat().st_size > 65536:
        raise PocketError('Example manifest exceeds 64KiB.')
    payload = path.read_bytes()
    if len(payload) > 65536:
        raise PocketError('Example manifest exceeds 64KiB.')
    manifest = json.loads(payload)
    if not isinstance(manifest, dict) or manifest.get('schema') != 'pocket.audio-note-example/v1':
        raise PocketError('Expected a retained note-example manifest.')
    # Override the store after copying its complete artifact graph. Neither the
    # external model environment nor the original WAV is consulted by queries.
    store = args.store or manifest['store_root']
    emit({'status': 'ok', 'store_root': store,
          'original': query(store, manifest['original']),
          'corrected': query(store, manifest['corrected'], args.view)})
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    execute = modes.add_parser('run', help='Opt in to bounded local model execution on a new synthetic source.')
    execute.add_argument('--executable', required=True, type=existing_absolute)
    execute.add_argument('--executable-sha256', required=True, type=sha256)
    execute.add_argument('--weights', required=True, type=existing_absolute)
    execute.add_argument('--weights-sha256', required=True, type=sha256)
    execute.add_argument('--worker', action='store_true', help='Also require direct/owned-worker artifact identity.')
    inspect = modes.add_parser('query', help='Read retained evidence only; no optional runtime or inference.')
    inspect.add_argument('--manifest', required=True, type=existing_absolute)
    inspect.add_argument('--store', help='Absolute copied artifact-store path for relocation.')
    inspect.add_argument('--view', choices=('summary', 'annotations', 'history'), default='annotations')
    args = parser.parse_args()
    if args.mode == 'query' and args.store and not Path(args.store).is_absolute():
        parser.error('--store requires an absolute artifact-store path.')
    return run(args) if args.mode == 'run' else retained(args)


if __name__ == '__main__':
    raise SystemExit(main())
