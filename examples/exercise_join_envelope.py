# SPDX-License-Identifier: MIT
"""Compare explicit 5/15 ms join envelopes on retained practice exercise outputs.

Run exercise_practice_recording first. This copies its store into a new private
folder, retains the baseline, verifies declared DSP against an independent gain
oracle and creates FLOAT32 browser previews with measured quantization error.
It never records a listening verdict or normalizes flagged audio.
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
from pathlib import Path

import numpy as np
import soundfile as sf

from pocket_music import practice_compare_processed, practice_envelope, practice_query
from pocket_music.artifact_store import read_record
from pocket_music.assets import sha256_file


def exercise(prior: Path, destination: Path):
    data = json.loads((prior/'results.json').read_text())
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copytree(prior/'store', destination/'store')
    store = str(destination/'store')
    (destination/'previews').mkdir()
    reports, cards = [], []
    for passage in data['passages']:
        baseline = passage['rendered'][0]['result']['artifacts']['render']
        raw = read_record(baseline, store)
        path = Path(store)/raw['audio']['artifact_uri']
        before_hash = sha256_file(path)
        samples, rate = sf.read(path, dtype='float64', always_2d=True)
        boundary = raw['mappings'][1]['output_span_frames'][0]
        records = [('Unchanged baseline', baseline, raw, samples)]
        for milliseconds in (5, 15):
            count, remainder = divmod(rate*milliseconds, 1000)
            if remainder:
                raise ValueError('Declared milliseconds must resolve to an exact frame count')
            result = practice_envelope(store, passage['name']+f'-join-{milliseconds}', baseline,
                [{'boundary_frame': boundary, 'fade_out_frames': count, 'fade_in_frames': count, 'curve': 'linear'}],
                {'actor': 'Pocket join experiment', 'actor_kind': 'agent',
                 'statement': f'Explicit {milliseconds} ms per side at a declared repetition join.',
                 'uncertainty': ['Technical boundary test; passage and envelope not approved by listening.']})
            handle = result['artifacts']['render']
            record = practice_query(store, handle)['summary']
            actual, actual_rate = sf.read(Path(store)/record['audio']['artifact_uri'], dtype='float64', always_2d=True)
            expected = samples.copy()
            # Independent scalar oracle, separate from the provider's gain vector.
            for index in range(count):
                expected[boundary-count+index] *= 1-index/(count-1)
                expected[boundary+index] *= index/(count-1)
            # linspace and scalar division may differ by a final rounding bit.
            error = float(np.max(np.abs(expected-actual)))
            assert actual_rate == rate and error <= 2*np.finfo(np.float64).eps
            assert np.array_equal(actual[:boundary-count], samples[:boundary-count])
            assert np.array_equal(actual[boundary+count:], samples[boundary+count:])
            records.append((f'{milliseconds} ms each side', handle, record, actual))
        comparison = practice_compare_processed(store, passage['name']+'-join-comparison', baseline,
            [r[1] for r in records[1:]],
            'Does either explicit envelope soften this technical repetition join without an unwanted dip? Listening pending.')
        previews = []
        for index, (label, handle, record, actual) in enumerate(records):
            name = f"previews/{passage['name']}-{index}.wav"
            sf.write(destination/name, actual, rate, subtype='FLOAT')
            preview, preview_rate = sf.read(destination/name, dtype='float64', always_2d=True)
            narrowed = actual.astype(np.float32).astype(np.float64)
            assert preview_rate == rate and np.array_equal(preview, narrowed)
            quantization = float(np.max(np.abs(preview-actual)))
            previews.append({'label': label, 'render': handle, 'audio': record['audio'], 'path': name,
                             'max_abs_quantization_error': quantization, 'frames': len(preview),
                             'signal': record['signal'], 'preview_sha256': sha256_file(destination/name)})
        assert sha256_file(path) == before_hash
        reports.append({'passage': passage['name'], 'capture_seconds': passage['capture_seconds'],
                        'boundary_frame': boundary, 'boundary_seconds': boundary/rate,
                        'baseline_unchanged': True, 'comparison': comparison, 'previews': previews,
                        'numerical_oracle': 'Within 2 float64 eps; untouched samples exactly equal'})
        ready = comparison['coverage']['signal_ready']
        cards.append('<section><h2>'+html.escape(passage['name'])+f" · source {passage['capture_seconds']:g}s</h2>"
                     +f'<p>Join at {boundary/rate:g}s · '+('Signal checks passed' if ready else 'Signal warning retained — overload in source')+'</p>'
                     +''.join('<article><h3>'+html.escape(p['label'])+'</h3><audio controls preload="metadata" src="'+p['path']+'"></audio></article>' for p in previews)
                     +'<p><button type="button" onclick="cue(this)">Cue all versions one second before the join</button></p></section>')
        (destination/'results.json').write_text(json.dumps({'source': data['source'], 'passages': reports,
            'human_listening': 'not_performed', 'musical_verdict': None}, indent=2)+'\n')
        print(json.dumps({'passage': passage['name'], 'variants': 2, 'signal_ready': ready,
                          'baseline_unchanged': True}), flush=True)
    (destination/'listen.html').write_text('''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pocket · explicit join experiment</title><style>body{font:17px/1.6 system-ui;background:#111614;color:#ebf0e9;margin:auto;max-width:940px;padding:clamp(16px,4vw,48px)}h1,h2,h3{line-height:1.2}h1{font-size:2.4rem}section{border-top:1px solid #657264;margin-top:36px;padding-top:20px}article{display:inline-block;vertical-align:top;width:30%;min-width:240px;margin-right:2%}audio{width:100%}button{font:inherit;padding:8px 14px;background:#d7edaa;color:#172114;border:0;border-radius:6px;cursor:pointer}a{color:#d7edaa}p{max-width:74ch}</style>
<h1>An explicit join envelope</h1><p>Unchanged audio alongside a 5 ms and 15 ms fade-out/in. The join is at 8 seconds in each 16-second technical repetition. Timing is unchanged; this is not a crossfade or a phrase-aligned loop.</p>
<p>These FLOAT32 browser previews have measured rounding error recorded in <a href="results.json">results.json</a>. Original DOUBLE evidence and input warnings remain in the store. No normalization, human listening or musical verdict is claimed.</p>
'''+''.join(cards)+'''<script>function cue(button){for(const a of document.querySelectorAll('audio'))a.pause();for(const a of button.closest('section').querySelectorAll('audio'))a.currentTime=7}document.addEventListener('play',e=>{if(e.target.tagName==='AUDIO')for(const a of document.querySelectorAll('audio'))if(a!==e.target)a.pause()},true)</script></html>''')
    return reports


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('prior', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    exercise(args.prior.expanduser().resolve(), args.destination.expanduser().resolve())
