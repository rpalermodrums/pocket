"""Opt-in synthetic local pulse evidence; no download or accuracy/listening claim."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import tempfile
import time
import wave
from pathlib import Path

from pocket_music import (
    audio_hypothesis_query,
    audio_model_inspect,
    audio_pulse_hypotheses,
    audio_pulse_submit,
    job_status,
)
from pocket_music.errors import PocketError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', required=True)
    parser.add_argument('--executable-sha256', required=True)
    parser.add_argument('--weights', required=True)
    parser.add_argument('--weights-sha256', required=True)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='pocket-pulse-example-'))
    source_path = root / 'synthetic.wav'
    pcm = b''.join(struct.pack('<h', int(1200 * math.sin(2 * math.pi * 220 * i / 8000)))
                   for i in range(32000))
    with wave.open(str(source_path), 'wb') as output:
        output.setparams((1, 2, 8000, 32000, 'NONE', 'not compressed'))
        output.writeframes(pcm)
    store = str(root / 'store')
    declaration = {'adapter': 'beat_this_cpu_v1',
                   'executable': {'path': str(Path(args.executable).resolve()), 'sha256': args.executable_sha256},
                   'weights': {'path': str(Path(args.weights).resolve()), 'sha256': args.weights_sha256},
                   'expected_profile': None}
    inspected = audio_model_inspect(store_root=store, request_id='inspect', declaration=declaration,
                                    qualification='synthetic_cpu_v1')
    if inspected['status'] != 'ok':
        print(json.dumps({'directory': str(root), 'inspection': inspected}, indent=2))
        return
    source = {'path': str(source_path), 'expected_sha256': hashlib.sha256(source_path.read_bytes()).hexdigest(),
              'start_frame': 0, 'frames': 32000, 'source_origin': 'independently_acquired'}
    arguments = {'store_root': store, 'source': source,
                 'model': {'kind': 'inspected', 'model': inspected['artifacts']['model']},
                 'settings': {'device': 'cpu', 'dtype': 'float32', 'threads': 1, 'postprocessor': 'minimal',
                              'downmix': 'arithmetic_mean', 'resampler': 'soxr_hq', 'seed': 0},
                 'attribution': {'actor': 'Pocket synthetic example', 'actor_kind': 'agent',
                                 'statement': 'A tone fixture, not a musical accuracy benchmark.',
                                 'uncertainty': ['No listening or musical decision.']}}
    direct = audio_pulse_hypotheses(request_id='direct', **arguments)
    submitted = audio_pulse_submit(request_id='owned', **arguments)
    deadline = time.monotonic() + 120
    while True:
        try:
            status = job_status(store_root=store, job_id=submitted['job']['job_id'])
        except PocketError as error:
            if 'busy' not in str(error):
                raise
        else:
            if status['state'] in ('completed', 'cancelled', 'failed', 'interrupted'):
                break
        if time.monotonic() >= deadline:
            raise RuntimeError('Example wait ended; inspect retained job status, do not resubmit automatically.')
        time.sleep(.2)
    assert status['state'] == 'completed', status
    assert status['result'] == direct['artifacts']['hypotheses']
    view = audio_hypothesis_query(store_root=store, hypotheses=status['result'], view='annotations')
    print(json.dumps({'directory': str(root), 'model': inspected['artifacts']['model'],
                      'hypotheses': status['result'], 'direct_worker_equal': True,
                      'source_unchanged': hashlib.sha256(source_path.read_bytes()).hexdigest() == source['expected_sha256'],
                      'annotations': view, 'musical_accuracy': 'not_established'}, indent=2))


if __name__ == '__main__':
    main()
