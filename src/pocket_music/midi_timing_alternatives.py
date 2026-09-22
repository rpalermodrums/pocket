"""Artifact-only authored timing choices composed through existing public editors.

Readback replays public edits in an owned temporary store, without original journals,
source files, model execution or writes to the caller's store. Pulse points are
caller assertions; model float lattices are never promoted to exact musical clocks.
"""
from __future__ import annotations

import copy
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from .artifact_store import (
    ArtifactHandle,
    canonical_bytes,
    digest,
    put_record,
    read_bytes,
    read_record,
    receipt,
    run_request,
)
from .audio_hypotheses import _attribution, _fields
from .audio_hypothesis_types import AudioHypothesisAttribution
from .audio_region_analysis import _load as load_region
from .audio_region_analysis import _project
from .errors import PocketError
from .material import integer, load_material, material_import, material_query, qn, rational, resolve_selection
from .material_types import MaterialLocks, MaterialRecord, MaterialSelection
from .midi_edit import midi_transform
from .time_maps import musical_time
from .timing_alternative_types import TimingAlignment, TimingAlternative

SCHEMA = 'pocket.midi-timing-alternatives/v1'
COVERAGE = {'symbolic_timing': 'explicit_authored_matches', 'native_execution': False,
            'human_listening': 'not_performed', 'pulse_lattice_inferred': False}
OPAQUE_SCHEMAS = {'pocket.audio-source-bytes/v1', 'pocket.audio-region-wav/v1',
                  'pocket.model-weights/v1', 'pocket.model-logits-float32le/v1',
                  'pocket.smf-original/v1', 'pocket.saved-set/v1', 'pocket.render-audio/v1',
                  'pocket.binary-asset/v1'}
LIMITATIONS = ['Sequential public shifts may refuse an intermediate overlap even when a simultaneous final edit would fit.',
               'Unchanged and no_addition both retain all original music; no silent layer is generated.',
               'Controller timelines stay fixed; moved notes may encounter different controller values.',
               'Declared source-clock alignment is not native synchronization or audible timing evidence.']


def _nodes(value):
    pending, count = [value], 0
    while pending:
        item = pending.pop()
        count += 1
        if isinstance(item, dict): pending.extend(item.values())
        elif isinstance(item, list): pending.extend(item)
    return count


def _graph(value, root, *, input_budget=False):
    """Bound complete retained metadata, retaining unique bytes for optional isolated replay."""
    pending, seen, count, metadata = [(value, 0)], {}, 0, 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > (70000 if input_budget else 100000) or depth > (40 if input_budget else 64):
            raise PocketError('Timing evidence graph exceeds node/depth budget')
        if isinstance(item, dict):
            if item.get('schema') == 'pocket.artifact-handle/v1':
                key = canonical_bytes(item)
                if key in seen: continue
                payload = read_bytes(item, root)
                seen[key] = (item, payload)
                if Path(item['artifact_uri']).name == 'record.json' or item['artifact_schema'] not in OPAQUE_SCHEMAS:
                    metadata += len(payload)
                    if metadata > (4 if input_budget else 8) * 1024 * 1024:
                        raise PocketError('Timing evidence exceeds JSON budget')
                    record = read_record(item, root)
                    pending.append((record, depth + 1))
                continue
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list): pending.extend((child, depth + 1) for child in item)
    return count, seen


def _q(value, name):
    _fields(value, {'n', 'd'}, name)
    integer(value['n'], name, -(2**53), 2**53)
    integer(value['d'], name, 1, 2**53)
    result = Fraction(value['n'], value['d'])
    if qn(result) != value: raise PocketError(f'{name} must be canonical reduced rational')
    return result


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise PocketError(f'{name} requires bounded nonempty text')


def _prepare(inputs, root):
    _fields(inputs, {'material', 'selection', 'hypotheses', 'expected_hypotheses_revision',
                     'alignment', 'alternatives', 'locks', 'attribution'}, 'timing inputs')
    try:
        TypeAdapter(TimingAlignment).validate_python(inputs['alignment'], strict=True)
        TypeAdapter(list[TimingAlternative]).validate_python(inputs['alternatives'], strict=True)
    except ValidationError as exc:
        raise PocketError('Malformed timing alignment/alternative fields') from exc
    _attribution(inputs['attribution'])
    try:
        TypeAdapter(ArtifactHandle).validate_python(inputs['hypotheses'], strict=True)
    except ValidationError as exc:
        raise PocketError('Malformed timing hypotheses handle') from exc
    node_count, _ = _graph(inputs, root, input_budget=True)
    record = load_material(inputs['material'], root)
    material_nodes = _nodes(record)
    if len(canonical_bytes(record)) > 32768 or material_nodes > 2048:
        raise PocketError('Timing material exceeds 32KiB/2048-node profile')
    selection = inputs['selection']
    _fields(selection, {'note_ids', 'material_revision', 'selection_sha256'}, 'fixed timing selection')
    resolved = resolve_selection(record, selection, require_hash=True)
    ids = resolved['note_ids']
    if not 1 <= len(ids) <= 16: raise PocketError('Timing requires 1–16 exact selected notes')
    alternatives = inputs['alternatives']
    if not 1 <= len(alternatives) <= 2: raise PocketError('Timing requires 1–2 alternatives')
    if node_count + len(alternatives) * len(ids) * (material_nodes + 700) > 90000:
        raise PocketError('Timing projected material/proof copies exceed node budget')
    if len({alt['alternative_id'] for alt in alternatives}) != len(alternatives):
        raise PocketError('Duplicate timing alternative identity')
    locks = inputs['locks']
    _fields(locks, {'outside_selection', 'selected_fields'}, 'timing locks')
    known = {'onset', 'duration', 'duration_qn', 'pitch', 'velocity', 'release_velocity', 'channel',
             'mute', 'expression_shape', 'expression_refs', 'voice_id', 'role_ref', 'source_binding', 'derived_from'}
    if (locks['outside_selection'] != 'all' or not isinstance(locks['selected_fields'], list)
            or any(not isinstance(f, str) or f not in known for f in locks['selected_fields'])
            or len(locks['selected_fields']) != len(set(locks['selected_fields']))):
        raise PocketError('Timing locks must name unique supported fields and preserve outside_selection')
    if inputs['hypotheses'].get('sha256') != inputs['expected_hypotheses_revision']:
        raise PocketError('Stale timing hypotheses revision')
    _, region, _, rows, _ = load_region(inputs['hypotheses'], root)
    superseded = {key for row in rows for key in row['supersedes']}
    active = {row['annotation_id']: row for row in rows if 'correction_id' in row
              and row['annotation_id'] not in superseded and row['annotation']['kind'] in ('attack', 'pulse_candidate')}
    alignment = inputs['alignment']
    if alignment['original_sha256'] != region['original']['sha256'] or alignment['sample_rate'] != region['original']['sample_rate']:
        raise PocketError('Timing alignment original source identity/rate mismatch')
    source_anchor = _q(alignment['source_anchor_frame_q'], 'source anchor')
    host_anchor = _q(alignment['host_anchor_seconds_q'], 'host anchor')
    if not 0 <= source_anchor < region['original']['frames']:
        raise PocketError('Source anchor outside original source')
    clips = [clip for clip in record['clips'] if clip['id'] == alignment['clip_id']]
    if len(clips) != 1 or clips[0]['origin']['space'] != 'arrangement_qn' or not set(ids) <= set(clips[0]['note_ids']):
        raise PocketError('Timing requires one selected arrangement-origin clip')
    clip = clips[0]
    if record['tempo_map_ref'] is not None and record['tempo_map_ref'] != alignment['time_map']:
        raise PocketError('Material tempo map conflicts with declared timing map')
    original = {note['id']: note for note in record['notes']}
    if any(original[key]['expression_refs'] for key in ids):
        raise PocketError('Timing profile requires ordinary notes without note-owned expression')
    plans = []
    for alt in alternatives:
        _text(alt['alternative_id'], 'alternative identity')
        _text(alt['statement'], 'alternative statement')
        if len(alt['uncertainty']) > 16: raise PocketError('Too many uncertainty statements')
        for value in alt['uncertainty']: _text(value, 'uncertainty')
        strength = _q(alt['strength'], 'strength')
        maximum = _q(alt['maximum_shift_qn'], 'maximum shift')
        if not 0 <= strength <= 1 or not 0 < maximum <= 16:
            raise PocketError('Timing strength/maximum shift outside supported bounds')
        matches = alt['matches']
        if len(matches) != len(ids) or {m['note_id'] for m in matches} != set(ids):
            raise PocketError('Each timing alternative must match every fixed selected note exactly once')
        plans_for_alt = []
        for match in matches:
            if match['annotation_id'] not in active:
                raise PocketError('Timing requires active authored attack/pulse hypothesis identity')
            row = active[match['annotation_id']]
            projected = _project(row, region)['original_projection']
            point = match['point']
            if point['kind'] == 'annotation_attack' and projected['kind'] == 'attack':
                frame = Fraction(projected['source_frame'])
            elif point['kind'] == 'declared_pulse_point' and projected['kind'] == 'pulse_candidate':
                _text(point['statement'], 'pulse point statement')
                frame = _q(point['original_source_frame_q'], 'pulse point')
                if not projected['start_frame'] <= frame < projected['end_frame_exclusive']:
                    raise PocketError('Declared pulse point outside half-open hypothesis interval')
            else: raise PocketError('Timing point kind differs from selected hypothesis')
            seconds = host_anchor + (frame - source_anchor) / alignment['sample_rate']
            conversion = musical_time('convert', root, time_map=alignment['time_map'],
                positions=[{'space': 'host_seconds', 'value': qn(seconds)}], target_space='arrangement_qn')
            target = rational(conversion['results'][0]['output']['value'])
            local = target - rational(clip['origin'])
            note = original[match['note_id']]
            delta = strength * (local - rational(note['onset']))
            onset = rational(note['onset']) + delta
            if abs(delta) > maximum: raise PocketError('Timing movement exceeds declared maximum shift')
            if not 0 <= onset or onset + rational(note['duration_qn']) > rational(clip['length_qn']):
                raise PocketError('Timing shifted gate outside selected clip')
            if delta and 'onset' in locks['selected_fields']: raise PocketError('Timing conflicts with onset lock')
            plans_for_alt.append({'match': copy.deepcopy(match), 'annotation': copy.deepcopy(row),
                'original_projection': projected, 'source_frame_q': qn(frame), 'host_seconds_q': qn(seconds),
                'target_arrangement_qn': qn(target), 'target_clip_qn': qn(local), 'delta_qn': qn(delta)})
        plans.append(plans_for_alt)
    return record, plans


def _compose(inputs, plans, root, *, verify_graph=True):
    outcomes = []
    for alternative, matches in zip(inputs['alternatives'], plans, strict=True):
        current = inputs['material']
        steps = []
        for index, plan in enumerate(matches):
            selection = material_query(current, root, selection={'note_ids': [plan['match']['note_id']]})['selection']
            step = {**plan, 'before': current, 'selection': selection, 'operations': [{'op': 'shift', 'delta_qn': plan['delta_qn']}]}
            if plan['delta_qn']['n']:
                edited = midi_transform(current, selection, step['operations'], root,
                    request_id='timing-step-' + digest([inputs, alternative['alternative_id'], index])[:40],
                    locks=inputs['locks'], expression_policy='reject', overlap_policy='reject_new')
                current = edited['material']
                step['edit'] = edited['edit']
            else: step['edit'] = None
            step['after'] = current
            steps.append(step)
            if verify_graph:
                # Each completed public step is durable even if a later edit, process
                # or final validation fails. This record never claims usable output.
                put_record({'schema': 'pocket.midi-timing-progress/v1', 'state': 'incomplete',
                    'inputs': inputs, 'completed_outcomes': outcomes,
                    'active': {'alternative_id': alternative['alternative_id'], 'steps': steps}}, root)
        outcomes.append({'alternative_id': alternative['alternative_id'], 'material': current, 'steps': steps})
    result = {'schema': SCHEMA, 'inputs': copy.deepcopy(inputs), 'unchanged': inputs['material'],
              'no_addition': inputs['material'], 'no_addition_meaning': 'leave_all_original_music_unchanged',
              'outcomes': outcomes, 'coverage': COVERAGE, 'limitations': LIMITATIONS}
    if len(canonical_bytes(result)) > 512 * 1024: raise PocketError('Timing manifest exceeds 512KiB budget')
    if verify_graph: _graph(result, root)
    return result


def _replay(record, root):
    """Recompute semantic edit proofs using the same public editor in isolated storage."""
    _fields(record, {'schema', 'inputs', 'unchanged', 'no_addition', 'no_addition_meaning',
                     'outcomes', 'coverage', 'limitations'}, 'timing manifest')
    _graph(record, root)
    if not isinstance(record.get('inputs'), dict) or not isinstance(record['inputs'].get('material'), dict) or record['inputs']['material'].get('schema') != 'pocket.artifact-handle/v1':
        raise PocketError('Retained timing inputs must bind an immutable material handle')
    _, plans = _prepare(record['inputs'], root)
    _, seed = _graph(record['inputs']['material'], root, input_budget=True)
    if sum(len(payload) for _, payload in seed.values()) > 16 * 1024 * 1024:
        raise PocketError('Timing replay material graph exceeds 16MiB copy budget')
    with tempfile.TemporaryDirectory(prefix='pocket-timing-proof-') as temporary:
        for handle, payload in seed.values():
            path = Path(temporary) / handle['artifact_uri']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        # The composer only edits material. Conversion and hypotheses already underwent
        # complete artifact-only validation in the original store before this copy.
        expected = _compose(record['inputs'], plans, temporary, verify_graph=False)
        if canonical_bytes(expected) != canonical_bytes(record):
            raise PocketError('Timing manifest differs from recomputed public conversion/edit proofs')


def load_timing_alternatives(manifest, store_root):
    """Validate complete graph and all semantic proofs without caller-store writes."""
    record = read_record(manifest, store_root, SCHEMA)
    if len(canonical_bytes(record)) > 512 * 1024: raise PocketError('Timing manifest exceeds 512KiB budget')
    _replay(record, store_root)
    return record


def midi_timing_alternatives(*, store_root: str, request_id: str,
        material: ArtifactHandle | MaterialRecord, selection: MaterialSelection,
        hypotheses: ArtifactHandle, expected_hypotheses_revision: str,
        alignment: TimingAlignment, alternatives: list[TimingAlternative],
        locks: MaterialLocks, attribution: AudioHypothesisAttribution) -> dict:
    """Compose explicit corrected-evidence timing alternatives and unchanged music.

    Inputs are capped at70k graph nodes/4MiB metadata/depth40; material32KiB/2048
    nodes; projected copies90k nodes; manifest512KiB and final graph100k nodes,
    8MiB metadata/depth64. Readback replays edits in disposable private storage.
    """
    inputs = {'material': material, 'selection': selection, 'hypotheses': hypotheses,
              'expected_hypotheses_revision': expected_hypotheses_revision, 'alignment': alignment,
              'alternatives': alternatives, 'locks': locks, 'attribution': attribution}
    # Resolve all stale identities and deterministic mapping constraints before writes.
    _prepare(inputs, store_root)
    _, seed = _graph(material, store_root, input_budget=True)
    if sum(len(payload) for _, payload in seed.values()) > 16 * 1024 * 1024:
        raise PocketError('Timing replay material graph exceeds 16MiB copy budget')
    def work():
        normalized = copy.deepcopy(inputs)
        if material.get('schema') != 'pocket.artifact-handle/v1':
            normalized['material'] = material_import({'kind': 'material', 'material': material}, store_root,
                'timing-import-' + digest([request_id, material])[:40])['material']
        _, plans = _prepare(normalized, store_root)
        record = _compose(normalized, plans, store_root)
        _replay(record, store_root)
        handle = put_record(record, store_root)
        result = receipt(request_id, artifacts={'manifest': handle}, unchanged=record['unchanged'],
            no_addition=record['no_addition'], alternatives=[{'alternative_id': o['alternative_id'], 'material': o['material']}
                for o in record['outcomes']], change_summary={'alternatives': len(record['outcomes']),
                'fixed_selected_notes': len(selection['note_ids'])}, coverage=COVERAGE, uncertainty=LIMITATIONS)
        if len(canonical_bytes(result)) > 16384: raise PocketError('Timing receipt exceeds16KiB')
        return result
    result = run_request(store_root, request_id, 'midi_timing_alternatives', inputs, work)
    load_timing_alternatives(result['artifacts']['manifest'], store_root)
    return result


def midi_timing_query(*, store_root: str, manifest: ArtifactHandle,
        view: Literal['summary', 'matches', 'steps'] = 'summary', cursor: str | None = None,
        limit: int = 16, max_bytes: int = 16000) -> dict:
    """Read revision-bound pages after independent public-edit proof replay."""
    integer(limit, 'limit', 1, 32)
    integer(max_bytes, 'max_bytes', 2048, 65536)
    if view not in ('summary', 'matches', 'steps'): raise PocketError('Unknown timing query view')
    record = load_timing_alternatives(manifest, store_root)
    if view == 'summary':
        rows = [{'unchanged': record['unchanged'], 'no_addition': record['no_addition'],
                 'no_addition_meaning': record['no_addition_meaning'], 'alternatives': [
                     {'alternative_id': alt['alternative_id'], 'material': alt['material'], 'steps': len(alt['steps'])}
                     for alt in record['outcomes']], 'attribution': record['inputs']['attribution'], 'limitations': LIMITATIONS}]
    else:
        rows = [{'alternative_id': alt['alternative_id'], 'step_index': index,
                 **(step if view == 'steps' else {'match': step['match'], 'delta_qn': step['delta_qn'],
                                                'source_frame_q': step['source_frame_q']})}
                for alt in record['outcomes'] for index, step in enumerate(alt['steps'])]
    identity = digest([manifest, view])
    offset = 0
    if cursor is not None:
        try:
            key, raw = cursor.split(':')
            offset = int(raw)
            if key != identity or str(offset) != raw or not 0 <= offset < len(rows): raise ValueError
        except (AttributeError, ValueError) as exc: raise PocketError('Invalid/stale timing query cursor') from exc
    result = {'schema': 'pocket.midi-timing-query/v1', 'manifest': manifest, 'revision': manifest['sha256'],
              'view': view, 'items': [], 'total': len(rows), 'next_cursor': None}
    for item in rows[offset:offset + limit]:
        candidate = {**result, 'items': [*result['items'], item]}
        count = offset + len(candidate['items'])
        candidate['next_cursor'] = f'{identity}:{count}' if count < len(rows) else None
        if len(canonical_bytes(candidate)) > max_bytes: break
        result = candidate
    if not result['items'] and offset < len(rows): raise PocketError('Timing row exceeds requested response byte budget')
    return result
