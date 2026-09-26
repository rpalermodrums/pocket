# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit deterministic pattern proposals, never a native scheduler or taste score."""
from __future__ import annotations

import random
from itertools import pairwise

from .artifact_store import digest, put_record, receipt, run_request
from .errors import PocketError
from .material import identifier, integer, make_note, new_material, qn, rational
from .material_types import PatternBrief, SeedMap


def midi_generate(brief: PatternBrief, store_root: str, request_id: str,
                  seeds: SeedMap | None = None, alternatives: int = 2) -> dict:
    """Repeat an explicit pitch/cell with bounded final-attack alternatives and an unchanged baseline.

    Every onset/gate uses rational quarter notes. The musician supplies the pitch;
    role and intentions remain annotations. No key, grid or sound is inferred.
    """
    allowed = {'role', 'pitch', 'cell_qn', 'cell', 'length_qn', 'enter_qn', 'exit_qn',
               'gate_qn', 'velocities', 'variation_qn', 'channel', 'intentions'}
    if not isinstance(brief, dict) or set(brief) - allowed:
        raise PocketError('Unknown pattern brief fields')
    if not isinstance(brief.get('role'), str) or not brief['role']:
        raise PocketError('An explicit musical role annotation is required')
    pitch = integer(brief.get('pitch'), 'pitch', 0, 127)
    channel = integer(brief.get('channel', 1), 'channel', 1, 16)
    integer(alternatives, 'alternatives', 1, 4)
    length, cell_length = rational(brief.get('length_qn')), rational(brief.get('cell_qn'))
    enter, exit_at = rational(brief.get('enter_qn', 0)), rational(brief.get('exit_qn', brief.get('length_qn')))
    gate = rational(brief.get('gate_qn', {'n': 1, 'd': 4}))
    variation = rational(brief.get('variation_qn', {'n': 1, 'd': 8}))
    if not (0 <= enter < exit_at <= length <= 4096) or cell_length <= 0 or gate <= 0 or variation < 0:
        raise PocketError('Unsatisfiable generation span/cell/gate constraints')
    cell_input = brief.get('cell')
    if not isinstance(cell_input, list) or not cell_input or len(cell_input) > 128:
        raise PocketError('An explicit cell of 1–128 attacks is required')
    cell = [rational(x, 'cell attack') for x in cell_input]
    if cell != sorted(set(cell)) or any(not 0 <= x < cell_length for x in cell):
        raise PocketError('Cell attacks must be strictly increasing within the cell')
    velocities = brief.get('velocities', [64, 76])
    if not isinstance(velocities, list) or not velocities:
        raise PocketError('Explicit velocities must be a nonempty list')
    for velocity in velocities:
        integer(velocity, 'velocity', 1, 127)
    seeds = {'structure': 0, 'timing': 0, 'velocity': 0} if seeds is None else seeds
    if not isinstance(seeds, dict):
        raise PocketError('Seeds must be an explicit domain-to-integer object')
    if set(seeds) - {'structure', 'timing', 'velocity'}:
        raise PocketError('Unknown seed domains')
    for value in seeds.values():
        integer(value, 'seed')
    inputs = {'brief': brief, 'seeds': seeds, 'alternatives': alternatives}

    def build(index, empty=False):
        content_seed = digest({'provider': 'pattern-v1', **inputs, 'alternative': index, 'empty': empty})
        track_id, clip_id = identifier('track', content_seed), identifier('clip', content_seed)
        randomizer = random.Random(digest({'seeds': seeds, 'alternative': index}))
        notes = []
        origin = enter
        cell_index = 0
        while not empty and origin < exit_at:
            for offset_index, offset in enumerate(cell):
                onset = origin + offset
                if index > 0 and cell_index > 0 and offset_index == len(cell) - 1:
                    onset += variation * randomizer.choice((-1, 1)) * index / max(1, alternatives - 1)
                if not enter <= onset < exit_at:
                    continue
                if onset + gate > length:
                    raise PocketError('A note gate exceeds the declared material length')
                notes.append(make_note(identifier('note', f'{content_seed}:{cell_index}:{offset_index}'),
                    onset, gate, pitch, velocities[offset_index % len(velocities)], channel=channel,
                    role=brief['role'], voice='voice:' + brief['role']))
                if len(notes) > 10000:
                    raise PocketError('Generation exceeds the 10000-note bound')
            origin += cell_length
            cell_index += 1
        notes.sort(key=lambda n: rational(n['onset']))
        if any(rational(a['onset']) + gate > rational(b['onset']) for a, b in pairwise(notes)):
            raise PocketError('Cell/gate/variation constraints introduce same-pitch overlaps')
        clip = {'id': clip_id, 'track_id': track_id, 'origin': {'space': 'phrase_qn', **qn(0)},
                'length_qn': qn(length), 'loop': False, 'note_ids': [n['id'] for n in notes],
                'event_ids': [], 'curve_ids': []}
        return new_material(content_seed, tracks=[{'id': track_id, 'name': brief['role']}], clips=[clip], notes=notes,
            provenance={'provider': 'pattern-v1', 'brief': brief, 'seeds': seeds, 'alternative': index,
                        'no_addition': empty, 'musical_judgment': 'not_evaluated'},
            coverage={'editing_allowed': True, 'issues': [], 'notes': 'exact', 'controllers': 'none',
                      'native_verification': 'not_performed', 'listening': 'not_performed'})

    def work():
        # Validate every proposal before publishing any of them.
        records = [build(i) for i in range(alternatives)]
        baseline = build(-1, empty=True)
        handles = [put_record(record, store_root) for record in records]
        no_addition = put_record(baseline, store_root)
        return receipt(request_id=request_id, artifacts={'alternatives': handles, 'no_addition': no_addition},
            alternatives=handles, no_addition=no_addition,
            change_summary={'alternatives': alternatives, 'notes': [len(x['notes']) for x in records]},
            constraint_result={'status': 'passed', 'pitch': pitch, 'monophonic': True,
                               'entrance_qn': qn(enter), 'last_attack_before_qn': qn(exit_at)},
            coverage={'provider': 'pattern-v1', 'native': False, 'listening': False},
            uncertainty=['Musical role and intentions are supplied annotations, not measured usefulness.'])
    return run_request(store_root, request_id, 'midi_generate', inputs, work)
