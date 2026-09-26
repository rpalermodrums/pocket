# SPDX-License-Identifier: MIT
"""Develop three supplied sections across a sparse, declared 77-minute span."""
import json
import tempfile
from pathlib import Path

from pocket_music import (
    material_import,
    material_structure,
    midi_arrangement_develop,
    midi_arrangement_query,
    musical_time,
)

inputs = Path(__file__).resolve().parent
output = Path(tempfile.mkdtemp(prefix='pocket-arrangement-'))
store = str(output / 'store')
attribution = {
    'actor': 'example author', 'actor_kind': 'agent',
    'statement': 'Synthetic supplied sections for file verification; no musical judgment.',
    'uncertainty': ['These sparse notes are not a full set or an accepted musical layer.'],
    'evidence': [],
}
clock = {
    'schema': 'pocket.time-context/v1', 'context_id': 'example:declared-120-bpm',
    'attribution': 'Synthetic clock declared by the example author; no audio alignment inferred.',
}
materials = []
nodes = []
for name in ('opening', 'response'):
    material = json.loads((inputs / f'{name}.json').read_text())
    imported = material_import(source={'kind': 'material', 'material': material},
                               store_root=store, request_id=f'import-{name}')
    materials.append({'key': name, 'material': imported['material']})
    nodes.append({
        'node_id': f'phrase:{name}', 'kind': 'phrase', 'label': name,
        'material_key': name, 'material_revision': material['revision_sha256'],
        'clip_id': 'clip:authored', 'space': 'clip_qn',
        'span_qn': {'start': {'n': 0, 'd': 1}, 'end': {'n': 8, 'd': 1}},
        'note_ids': ['note:0', 'note:1'], 'attribution': attribution,
    })
graph = material_structure(
    operation='create', store_root=store, request_id='graph',
    definition={'label': 'Supplied opening and response', 'materials': materials,
                'nodes': nodes, 'relations': [], 'attribution': attribution,
                'cycle_policy': 'reject'},
)['artifacts']['structure']
definition = {
    'label': 'Three sections across a declared 77-minute span',
    'structure': graph, 'expected_structure_revision': graph['sha256'],
    'clock': clock, 'origin': {'space': 'arrangement_qn', 'n': 0, 'd': 1},
    'length_qn': 9240,
    'sections': [
        {'section_id': 'opening', 'node_id': 'phrase:opening', 'at_qn': 0},
        {'section_id': 'middle', 'node_id': 'phrase:response', 'at_qn': 4600},
        {'section_id': 'return', 'node_id': 'phrase:opening', 'at_qn': 9224},
    ],
    'locked_section_ids': ['opening'],
    'variations': [
        {'section_id': 'middle', 'endpoint_note_id': 'note:1',
         'pitch_offsets_semitones': [-2], 'timing_offsets_qn': [{'n': 1, 'd': 4}],
         'pitch_min': 48, 'pitch_max': 84},
        {'section_id': 'return', 'endpoint_note_id': 'note:1',
         'pitch_offsets_semitones': [2], 'timing_offsets_qn': [0],
         'pitch_min': 48, 'pitch_max': 84},
    ],
    'seeds': {'structure': 11, 'pitch': 23, 'timing': 37},
    'attribution': attribution,
}
developed = midi_arrangement_develop(store_root=store, request_id='develop', definition=definition)
development = developed['artifacts']['development']
sections = midi_arrangement_query(store_root=store, development=development, view='sections')
changes = midi_arrangement_query(store_root=store, development=development, view='changes')
time_map = musical_time(
    operation='create', store_root=store, request_id='clock',
    definition={
        'source_context': clock, 'domain_qn': {'start': {'n': 0, 'd': 1}, 'end': {'n': 9240, 'd': 1}},
        'tempo': [{'at_qn': {'n': 0, 'd': 1}, 'bpm': {'n': 120, 'd': 1}, 'interpolation': 'step'}],
        'host_origin': {'arrangement_qn': {'n': 0, 'd': 1}, 'host_seconds': {'n': 0, 'd': 1}},
    },
)
report = {'output': str(output), 'definition': definition, 'developed': developed,
          'sections': sections, 'changes': changes, 'declared_time_map': time_map,
          'span_seconds_at_declared_tempo': 4620, 'native_verified': False,
          'listening': 'not_performed', 'musical_decision': 'none'}
(output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
