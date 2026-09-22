"""Explicit audio interpretation to supplied MIDI timing, without a model or DAW."""
import hashlib
import json
import struct
import tempfile
import wave
from pathlib import Path

from pocket_music import (
    audio_region_correct,
    audio_region_hypotheses,
    audio_region_query,
    material_import,
    material_query,
    midi_timing_alternatives,
    midi_timing_query,
    midi_transform,
    musical_time,
)

output = Path(tempfile.mkdtemp(prefix='pocket-audio-timing-'))
store = str(output / 'store')
path = output / 'synthetic-click.wav'
rate = 8000
with wave.open(str(path), 'wb') as stream:
    stream.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
    stream.writeframes(b''.join(struct.pack('<h', 16000 if 4000 <= i < 4010 else 0)
                              for i in range(24000)))
source_sha = hashlib.sha256(path.read_bytes()).hexdigest()
before = path.stat()
actor = {'actor': 'Synthetic example author', 'actor_kind': 'agent',
         'statement': 'Compare two explicitly chosen timing strengths.',
         'uncertainty': ['No listening; no kick identity or masking improvement established.']}
source = {'path': str(path), 'expected_sha256': source_sha, 'start_frame': 100,
          'frames': 16000, 'source_origin': 'independently_acquired'}
observed = audio_region_hypotheses(store_root=store, request_id='audio',
    region={'kind': 'inline', 'source': source},
    analysis={'kind': 'peek', 'settings': {'bpm_hint': None, 'beats_per_bar': 4}}, attribution=actor)
raw = observed['artifacts']['hypotheses']
corrected = audio_region_correct(store_root=store, request_id='authored-attack', parent=raw,
    expected_revision=raw['sha256'], attribution=actor,
    batch={'coordinate_space': 'original_source_frame', 'corrections': [{
        'correction_id': 'declared-click', 'supersedes': [],
        'annotation': {'kind': 'attack', 'source_frame': 4000, 'strength_relative': None},
        'support': [{'kind': 'analysis_pointer', 'reference': '/analysis/region/start_frame'}],
        'uncertainty': ['Known fixture event supplied by its author, not an inferred instrument.']} ]})
hypotheses = corrected['artifacts']['hypotheses']
page = audio_region_query(store_root=store, hypotheses=hypotheses, view='annotations', limit=128)
authored = next(row['local'] for row in page['items']
                if row['local'].get('correction_id') == 'declared-click')
record = json.loads(Path(__file__).with_name('material.json').read_text())
material = material_import(source={'kind': 'material', 'material': record}, store_root=store,
                           request_id='external-notes')['material']
selection = material_query(material=material, store_root=store, selection={'note_ids': ['note:0']})['selection']
time_map = musical_time(operation='create', store_root=store, request_id='clock', definition={
    'source_context': {'schema': 'pocket.time-context/v1', 'context_id': 'declared-fixture-clock',
                       'attribution': 'Synthetic unwarped source begins at host second zero.'},
    'domain_qn': {'start': {'n': 0, 'd': 1}, 'end': {'n': 8, 'd': 1}},
    'tempo': [{'at_qn': {'n': 0, 'd': 1}, 'bpm': {'n': 120, 'd': 1}, 'interpolation': 'step'}],
    'host_origin': {'arrangement_qn': {'n': 0, 'd': 1}, 'host_seconds': {'n': 0, 'd': 1}},
})['artifacts']['time_map']
arguments = {'store_root': store, 'request_id': 'timing', 'material': material, 'selection': selection,
    'hypotheses': hypotheses, 'expected_hypotheses_revision': hypotheses['sha256'],
    'alignment': {'kind': 'declared_unwarped_source_clock', 'original_sha256': source_sha,
        'sample_rate': rate, 'source_anchor_frame_q': {'n': 0, 'd': 1},
        'host_anchor_seconds_q': {'n': 0, 'd': 1}, 'time_map': time_map, 'clip_id': 'clip:authored'},
    'alternatives': [{'alternative_id': name, 'statement': name + ' of the declared move',
        'uncertainty': ['Timing preference has not been auditioned.'],
        'strength': strength, 'maximum_shift_qn': {'n': 2, 'd': 1},
        'matches': [{'note_id': 'note:0', 'annotation_id': authored['annotation_id'],
                     'point': {'kind': 'annotation_attack'}}]}
        for name, strength in [('full', {'n': 1, 'd': 1}), ('half', {'n': 1, 'd': 2})]],
    'locks': {'outside_selection': 'all', 'selected_fields': ['pitch', 'velocity', 'release_velocity']},
    'attribution': actor}
result = midi_timing_alternatives(**arguments)
assert result['unchanged'] == result['no_addition'] == material
steps = midi_timing_query(store_root=store, manifest=result['artifacts']['manifest'], view='steps')
assert [row['delta_qn'] for row in steps['items']] == [{'n': 1, 'd': 1}, {'n': 1, 'd': 2}]
for row in steps['items']:
    direct = midi_transform(material=row['before'], selection=row['selection'], operations=row['operations'],
        store_root=store, request_id='direct-' + row['alternative_id'], locks=arguments['locks'],
        expression_policy='reject', overlap_policy='reject_new')
    assert direct['material'] == row['after'] and direct['edit'] == row['edit']
after = path.stat()
assert hashlib.sha256(path.read_bytes()).hexdigest() == source_sha
assert (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (
    after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
(output / 'timing-input.json').write_text(json.dumps(arguments, indent=2) + '\n')
report = {'output': str(output), 'result': result, 'steps': steps, 'source_preserved': True,
          'direct_composed_equal': True, 'native_verified': False, 'listening': 'not_performed'}
(output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
