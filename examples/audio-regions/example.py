"""Three explicit passages in a synthetic hour; public providers only."""
import hashlib
import json
import struct
import tempfile
import time
import wave
from pathlib import Path

from pocket_music import (
    audio_region_capture,
    audio_region_correct,
    audio_region_hypotheses,
    audio_region_query,
    audio_region_submit,
    job_status,
)
from pocket_music.errors import PocketError

output = Path(tempfile.mkdtemp(prefix='pocket-audio-regions-'))
path = output / 'synthetic-hour.wav'
rate = 8000
block = struct.pack('<' + 'h' * rate,
                    *[20000 - (i % 4000) * 500 if i % 4000 < 40 else 0 for i in range(rate)])
with wave.open(str(path), 'wb') as stream:
    stream.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
    for _ in range(3600):
        stream.writeframesraw(block)
with path.open('rb') as stream:
    source_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
before = path.stat()
store = str(output / 'store')
attribution = {'actor': 'Synthetic example author', 'actor_kind': 'agent',
               'statement': 'Explicit fixture passages; no listening or musical approval.',
               'uncertainty': ['Repeated test clicks do not establish real-set analysis quality.']}
analysis = {'kind': 'peek', 'settings': {'bpm_hint': 120, 'beats_per_bar': 4}}
passages = []
for label, seconds in [('early', 600), ('middle', 1800), ('late', 3480)]:
    source = {'path': str(path), 'expected_sha256': source_sha, 'start_frame': seconds * rate,
              'frames': 4 * rate, 'source_origin': 'independently_acquired'}
    captured = audio_region_capture(store_root=store, request_id='capture-' + label, source=source)
    arguments = {'store_root': store, 'analysis': analysis, 'attribution': attribution}
    direct = audio_region_hypotheses(request_id='direct-' + label,
                                    region={'kind': 'inline', 'source': source}, **arguments)
    composed = audio_region_hypotheses(request_id='composed-' + label,
        region={'kind': 'captured', 'region': captured['artifacts']['region']}, **arguments)
    assert direct['artifacts'] == composed['artifacts']
    handle = direct['artifacts']['hypotheses']
    corrected = audio_region_correct(store_root=store, request_id='authored-' + label,
        parent=handle, expected_revision=handle['sha256'], attribution=attribution,
        batch={'coordinate_space': 'original_source_frame', 'corrections': [{
            'correction_id': label + '-span', 'supersedes': [],
            'annotation': {'kind': 'phrase_anchor', 'start_frame': source['start_frame'],
                           'end_frame_exclusive': source['start_frame'] + source['frames'],
                           'label': 'Explicit four-second fixture passage'},
            'support': [{'kind': 'analysis_pointer', 'reference': '/analysis/region/start_frame'}],
            'uncertainty': ['Supplied span, not inferred phrase recognition.']} ]})
    page = audio_region_query(store_root=store, hypotheses=corrected['artifacts']['hypotheses'],
                              view='annotations', limit=32)
    passages.append({'label': label, 'source': source, 'capture': captured,
                     'analysis': direct, 'corrected': corrected, 'query': page})

# The inline worker establishes durable ownership before hashing the long file.
submitted = audio_region_submit(store_root=store, request_id='late-worker',
    region={'kind': 'inline', 'source': passages[-1]['source']}, analysis=analysis, attribution=attribution)
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
    if status['state'] in {'completed', 'failed', 'cancelled', 'interrupted'}:
        break
    time.sleep(.1)
assert status['state'] == 'completed'
assert status['result'] == passages[-1]['analysis']['artifacts']['hypotheses']
after = path.stat()
assert (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (
    after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
with path.open('rb') as stream:
    assert hashlib.file_digest(stream, 'sha256').hexdigest() == source_sha
report = {'output': str(output), 'source_sha256': source_sha, 'duration_seconds': 3600,
          'passages': passages, 'submitted': submitted, 'status': status,
          'source_preserved': True, 'human_listening': 'not_performed'}
(output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
