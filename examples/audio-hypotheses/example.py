# SPDX-License-Identifier: MIT
"""A synthetic local-source example using only public analysis/correction/job tools."""
import hashlib
import json
import struct
import tempfile
import time
import wave
from pathlib import Path

from pocket_music import (
    audio_hypotheses,
    audio_hypothesis_correct,
    audio_hypothesis_query,
    audio_hypothesis_submit,
    job_status,
)
from pocket_music.errors import PocketError

output = Path(tempfile.mkdtemp(prefix='pocket-audio-hypotheses-'))
path = output / 'synthetic-clicks.wav'
rate = 8000
samples = [20000 - (index % 4000) * 500 if index % 4000 < 40 else 0 for index in range(rate * 4)]
with wave.open(str(path), 'wb') as stream:
    stream.setnchannels(1)
    stream.setsampwidth(2)
    stream.setframerate(rate)
    stream.writeframes(struct.pack('<' + 'h' * len(samples), *samples))
store = str(output / 'store')
source = {'path': str(path), 'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
          'start_frame': 0, 'frames': len(samples), 'source_origin': 'independently_acquired'}
settings = {'bpm_hint': 120, 'beats_per_bar': 4}
attribution = {'actor': 'Synthetic example author', 'actor_kind': 'agent',
               'statement': 'Inspect generated test clicks; no musician judgment.',
               'uncertainty': ['This synthetic source does not establish performance on musical mixes.']}
arguments = {'store_root': store, 'source': source, 'settings': settings, 'attribution': attribution}
direct = audio_hypotheses(request_id='direct', **arguments)
handle = direct['artifacts']['hypotheses']
corrected = audio_hypothesis_correct(store_root=store, request_id='authored-alternative',
    parent=handle, expected_revision=handle['sha256'], attribution=attribution,
    corrections=[{'correction_id': 'phrase-start', 'supersedes': [],
        'annotation': {'kind': 'phrase_anchor', 'start_frame': 0, 'end_frame_exclusive': len(samples),
                       'label': 'Supplied four-second fixture span'},
        'support': [{'kind': 'analysis_pointer', 'reference': '/analysis/region/start_frame'}],
        'uncertainty': ['Authored span; not inferred phrase recognition.']}])
query = audio_hypothesis_query(store_root=store, hypotheses=corrected['artifacts']['hypotheses'],
                               view='annotations', limit=32)
submitted = audio_hypothesis_submit(request_id='worker', **arguments)
deadline = time.monotonic() + 60
while True:
    if time.monotonic() >= deadline:
        raise RuntimeError('Example wait ended; inspect the retained job without automatic retry.')
    try:
        status = job_status(store_root=store, job_id=submitted['job']['job_id'])
    except PocketError as error:
        if str(error) != 'Job state is busy; refresh status before retry':
            raise
        time.sleep(.1)
        continue
    if status['state'] in {'completed', 'cancelled', 'failed', 'interrupted'}:
        break
    time.sleep(.1)
assert status['state'] == 'completed' and status['result'] == handle
report = {'output': str(output), 'direct': direct, 'corrected': corrected, 'query': query,
          'submitted': submitted, 'status': status}
(output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
