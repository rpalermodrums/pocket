# SPDX-License-Identifier: AGPL-3.0-only
"""Exact, selected note edits with revision-bound locks and semantic differences."""
from __future__ import annotations

import copy
import heapq
import json
import math
import re
from itertools import pairwise

from .artifact_store import digest, put_record, read_bytes, read_record, receipt, run_request
from .errors import PocketError
from .material import (
    MAX_NOTES,
    finalize_material,
    identifier,
    integer,
    load_material,
    qn,
    rational,
    resolve_selection,
    semantic_diff,
)
from .material_types import (
    ArtifactHandle,
    MaterialEditOperation,
    MaterialLocks,
    MaterialRecord,
    MaterialSelection,
)
from .midi_groove import GROOVE_FIELDS, apply_groove

_NEW_OPERATIONS = {
    'groove': GROOVE_FIELDS,
    'revoice': {'op', 'destinations', 'hypothesis', 'controller_timeline', 'pitch_expression'},
    'add': {'op', 'clip_id', 'notes', 'controller_timeline', 'time_space'},
    'split': {'op', 'offsets_qn', 'controller_timeline', 'time_space', 'articulation'},
    'merge': {'op', 'controller_timeline', 'time_space', 'articulation'},
    'delete': {'op', 'controller_timeline'},
    'duplicate': {'op', 'delta_qn', 'controller_timeline'},
    'grid_quantize': {'op', 'grid_qn', 'phase_qn', 'strength', 'threshold_qn', 'ties',
                      'note_off', 'controller_timeline', 'time_space'},
    'velocity_map': {'op', 'field', 'mapping', 'unmapped'},
}
_OVERLAP_COMPARISON_LIMIT = 250000


def _edit_rational(value, field):
    if isinstance(value, dict) and set(value) != {'n', 'd'}:
        raise PocketError(f'{field} requires an untagged rational with exactly n and d')
    return rational(value, field)


def _reject_new_overlaps(before, after):
    """Check changed intervals without allocating the quadratic set of old pairs."""
    def intervals(record):
        clips = {note_id: clip['id'] for clip in record['clips'] for note_id in clip['note_ids']}
        return {n['id']: (clips[n['id']], n['channel'], n['pitch']['midi_note'], rational(n['onset']),
                         rational(n['onset']) + rational(n['duration_qn'])) for n in record['notes']}

    old, current = intervals(before), intervals(after)
    changed = {key for key, value in current.items() if old.get(key) != value}
    if not changed:
        return 0
    groups = {}
    for note_id, interval in current.items():
        groups.setdefault(interval[:3], []).append((interval[3], interval[4], note_id))
    comparisons = 0
    for rows in groups.values():
        active, affected, heap = {}, set(), []
        for start, end, note_id in sorted(rows):
            while heap and heap[0][0] <= start:
                _, expired = heapq.heappop(heap)
                active.pop(expired)
                affected.discard(expired)
            candidates = active if note_id in changed else affected
            for other_id in candidates:
                comparisons += 1
                if comparisons > _OVERLAP_COMPARISON_LIMIT:
                    raise PocketError('Overlap validation exceeds its bounded comparison budget; use a smaller edit')
                left, right = old.get(note_id), old.get(other_id)
                if not (left is not None and right is not None and left[:3] == right[:3]
                        and left[3] < right[4] and right[3] < left[4]):
                    raise PocketError('Edit introduces same-channel same-pitch overlap')
            active[note_id] = end
            if note_id in changed:
                affected.add(note_id)
            heapq.heappush(heap, (end, note_id))
    return comparisons


def _remove_notes(child, delete):
    source_only = list(child['coverage'].get('source_only_note_event_ids', []))
    for note in child['notes']:
        if note['id'] in delete:
            source_only.extend(value for value in ((note['source_binding'] or {}).get('on_event_id'),
                               (note['source_binding'] or {}).get('off_event_id')) if value is not None)
    if source_only:
        child['coverage']['source_only_note_event_ids'] = source_only
    child['notes'] = [note for note in child['notes'] if note['id'] not in delete]
    for clip in child['clips']:
        clip['note_ids'] = [note_id for note_id in clip['note_ids'] if note_id not in delete]


def _duplicate_source(note, record, event_ids, events, verified_sources, store_root):
    binding = note['source_binding']
    if binding is None:
        return
    fields = {'kind', 'source_sha256', 'track_index', 'on_event_id', 'off_event_id', 'off_encoding'}
    if not isinstance(binding, dict) or set(binding) != fields or binding['kind'] != 'smf':
        raise PocketError('Duplicate supports null or ordinary SMF source bindings; opaque/native bindings are unqualified')
    source_hash = binding['source_sha256']
    if not isinstance(source_hash, str) or not re.fullmatch('[0-9a-f]{64}', source_hash):
        raise PocketError('Duplicate SMF source binding requires an exact source hash')
    if source_hash not in verified_sources:
        sources = [source for source in record['sources'] if isinstance(source, dict)
                   and source.get('kind') == 'smf' and source.get('sha256') == source_hash
                   and isinstance(source.get('raw'), dict) and source['raw'].get('sha256') == source_hash
                   and source['raw'].get('artifact_schema') == 'pocket.smf-original/v1']
        if not sources:
            raise PocketError('Duplicate SMF source binding has no matching retained original')
        for source in sources:
            read_bytes(source['raw'], store_root)
        verified_sources.add(source_hash)
    track = integer(binding['track_index'], 'SMF source track', 0)
    on, off = (events.get(binding[key]) if isinstance(binding[key], str) else None
               for key in ('on_event_id', 'off_event_id'))
    if (on is None or off is None or on['id'] not in event_ids or off['id'] not in event_ids
            or type(on.get('track_index')) is not int or type(off.get('track_index')) is not int
            or on['track_index'] != track or off['track_index'] != track
            or on['message_type'] != 'note_on' or off['message_type'] not in ('note_off', 'note_on')
            or on['bytes'][0] >> 4 != 9 or on['bytes'][2] == 0
            or off['bytes'][0] >> 4 not in (8, 9) or (off['bytes'][0] >> 4 == 9 and off['bytes'][2] != 0)
            or (on['bytes'][0] & 15) != (off['bytes'][0] & 15) or on['bytes'][1] != off['bytes'][1]
            or binding['off_encoding'] != off['message_type'] or on['order'] >= off['order']
            or rational(on['time']) >= rational(off['time'])):
        raise PocketError('Duplicate requires a complete ordinary SMF source lifecycle')




def _validate_pitch(pitch):
    if not isinstance(pitch, dict) or set(pitch) != {'midi_note', 'cents_offset', 'tuning_ref'}:
        raise PocketError('Literal pitch requires exactly midi_note, cents_offset and tuning_ref')
    integer(pitch['midi_note'], 'literal MIDI pitch', 0, 127)
    cents = pitch['cents_offset']
    if (isinstance(cents, bool) or not isinstance(cents, (int, float))
            or isinstance(cents, float) and not math.isfinite(cents)):
        raise PocketError('Literal pitch cents offset must be finite numeric data')
    if not isinstance(pitch['tuning_ref'], str) or not pitch['tuning_ref']:
        raise PocketError('Literal pitch requires an explicit nonempty tuning reference')

def _literal_note(value):
    fields = {'onset_qn', 'duration_qn', 'pitch', 'velocity', 'release_velocity',
              'channel', 'mute', 'voice_id', 'role_ref'}
    if not isinstance(value, dict) or set(value) != fields:
        raise PocketError('Literal note requires exactly the declared ordinary note fields')
    onset, gate = (_edit_rational(value[field], field) for field in ('onset_qn', 'duration_qn'))
    if gate <= 0:
        raise PocketError('Literal note gate must be positive')
    _validate_pitch(value['pitch'])
    for field, low in (('velocity', 1), ('release_velocity', 0)):
        velocity = value[field]
        if (not isinstance(velocity, dict) or set(velocity) != {'value', 'domain'}
                or velocity['domain'] != 'midi1_7bit'):
            raise PocketError('Literal velocity requires exactly value and midi1_7bit domain')
        integer(velocity['value'], field, low, 127)
    integer(value['channel'], 'literal MIDI channel', 1, 16)
    if not isinstance(value['mute'], bool):
        raise PocketError('Literal mute must be boolean')
    if not isinstance(value['voice_id'], str) or not value['voice_id']:
        raise PocketError('Literal voice identity must be nonempty text')
    if value['role_ref'] is not None and not isinstance(value['role_ref'], str):
        raise PocketError('Literal role reference must be text or null')
    return {**{key: copy.deepcopy(value[key]) for key in fields - {'onset_qn', 'duration_qn'}},
            'onset': {'space': 'clip_qn', **qn(onset)}, 'duration_qn': qn(gate),
            'expression_refs': [], 'source_binding': None, 'derived_from': []}


def _construct_notes(operation, chosen, child, parent, operation_index, locked_fields, store_root):
    """Add or replace ordinary notes without touching retained controller/source evidence."""
    op = operation['op']
    if operation['controller_timeline'] != 'preserve_existing':
        raise PocketError('Explicit controller_timeline=preserve_existing is required')
    owners = {key: clip for clip in child['clips'] for key in clip['note_ids']}
    current_ids = {note['id'] for note in child['notes']}
    additions, removed = [], []
    if op == 'add':
        if operation['time_space'] != 'clip_qn' or not isinstance(operation['clip_id'], str):
            raise PocketError('Literal addition requires an exact clip identity and clip_qn')
        clips = [clip for clip in child['clips'] if clip['id'] == operation['clip_id']]
        if len(clips) != 1:
            raise PocketError('Literal addition clip identity does not resolve exactly')
        clip = clips[0]
        literals = operation['notes']
        if not isinstance(literals, list) or not 1 <= len(literals) <= 4096:
            raise PocketError('Literal addition requires 1–4096 explicit notes')
        if len(child['notes']) + len(literals) > MAX_NOTES:
            raise PocketError('Literal addition exceeds the 100000-note material bound')
        for row, literal in enumerate(literals):
            note = _literal_note(literal)
            onset = rational(note['onset'])
            if onset < 0 or onset + rational(note['duration_qn']) > rational(clip['length_qn']):
                raise PocketError('Literal note must fit its clip; no implicit extension or wrapping')
            note['id'] = identifier('note', digest({'profile': 'literal-add-v1',
                'parent_revision': parent['revision_sha256'], 'operation_index': operation_index,
                'row': row, 'clip_id': clip['id'], 'note': note}))
            additions.append((clip, note))
    else:
        if locked_fields:
            raise PocketError('Split/merge replaces locked note fields; use an unlocked explicit selection')
        if not chosen:
            raise PocketError('Split/merge requires surviving selected original notes')
        if any(note['expression_refs'] for note in chosen):
            raise PocketError('Split/merge of note-owned expression requires a qualified curve ownership route')
        events = {event['id']: event for event in child['events']}
        clip_events = {clip['id']: set(clip['event_ids']) for clip in child['clips']}
        verified_sources = set()
        for note in chosen:
            _duplicate_source(note, child, clip_events[owners[note['id']]['id']],
                              events, verified_sources, store_root)
        if op == 'split':
            if operation['time_space'] != 'note_relative_qn' or operation['articulation'] != 'retrigger':
                raise PocketError('Split requires note_relative_qn and explicit retrigger articulation')
            values = operation['offsets_qn']
            if not isinstance(values, list) or not 1 <= len(values) <= 64:
                raise PocketError('Split requires 1–64 increasing internal cut offsets')
            offsets = [_edit_rational(value, 'split offset') for value in values]
            if offsets[0] <= 0 or any(left >= right for left, right in pairwise(offsets)):
                raise PocketError('Split offsets must be positive and strictly increasing')
            if len(child['notes']) + len(chosen) * len(offsets) > MAX_NOTES:
                raise PocketError('Split exceeds the 100000-note material bound')
            for note in chosen:
                gate = rational(note['duration_qn'])
                if offsets[-1] >= gate:
                    raise PocketError('Split offsets must lie strictly inside every selected gate')
                boundaries = [0, *offsets, gate]
                for segment, (start, end) in enumerate(pairwise(boundaries)):
                    note_id = identifier('note', digest({'profile': 'split-v1',
                        'parent_revision': parent['revision_sha256'], 'operation_index': operation_index,
                        'source_note_id': note['id'], 'segment': segment,
                        'offsets_qn': [qn(value) for value in offsets]}))
                    replacement = {**copy.deepcopy(note), 'id': note_id,
                        'onset': {'space': 'clip_qn', **qn(rational(note['onset']) + start)},
                        'duration_qn': qn(end - start), 'source_binding': None, 'derived_from': [note['id']]}
                    additions.append((owners[note['id']], replacement))
        else:
            if operation['time_space'] != 'clip_qn' or operation['articulation'] != 'remove_retriggers':
                raise PocketError('Merge requires clip_qn and explicit remove_retriggers articulation')
            if not 2 <= len(chosen) <= 4096:
                raise PocketError('Merge requires 2–4096 surviving selected original notes')
            ordered = sorted(chosen, key=lambda note: (rational(note['onset']), note['id']))
            first, last = ordered[0], ordered[-1]
            clip = owners[first['id']]
            equal_fields = ('pitch', 'channel', 'voice_id', 'role_ref', 'mute', 'velocity', 'release_velocity')
            if any(owners[note['id']]['id'] != clip['id'] or any(note[field] != first[field]
                   for field in equal_fields) for note in ordered):
                raise PocketError('Merge requires one clip and identical pitch, dynamics, channel, voice, role and mute')
            if any(rational(left['onset']) + rational(left['duration_qn']) != rational(right['onset'])
                   for left, right in pairwise(ordered)):
                raise PocketError('Merge requires exactly contiguous gates without gaps or overlaps')
            lineage = [note['id'] for note in ordered]
            note_id = identifier('note', digest({'profile': 'merge-v1',
                'parent_revision': parent['revision_sha256'], 'operation_index': operation_index,
                'source_note_ids': lineage}))
            replacement = {**copy.deepcopy(first), 'id': note_id,
                'duration_qn': qn(rational(last['onset']) + rational(last['duration_qn']) - rational(first['onset'])),
                'source_binding': None, 'derived_from': lineage}
            additions.append((clip, replacement))
        removed = [note['id'] for note in chosen]
    for _, note in additions:
        if note['id'] in current_ids:
            raise PocketError('Constructed note identity collides with existing material')
        current_ids.add(note['id'])
    if removed:
        _remove_notes(child, set(removed))
    for clip, note in additions:
        child['notes'].append(note)
        clip['note_ids'].append(note['id'])
    return {'operation_index': operation_index, 'op': op,
            'inserted': [note['id'] for _, note in additions], 'deleted': removed,
            'articulation': operation.get('articulation', 'new_literal_attacks'),
            'controller_timeline': 'preserve_existing', 'controller_expression': 'not_inferred',
            'source_binding_validation': 'retained_hash_and_declared_lifecycle', 'source_reparse': 'not_performed'}

def _quantize_expression(note, curves, store_root):
    for curve_id in note['expression_refs']:
        curve = curves[curve_id]
        target = curve['target']
        if (curve['space'] != 'note_relative_qn' or target['scope'] != 'note'
                or target.get('note_id') != note['id'] or target['ownership'] != 'none'
                or target.get('resize_policy') not in ('stretch_with_gate', 'preserve_fraction', 'crop')
                or any(not 0 <= rational(point['time']) <= rational(note['duration_qn']) for point in curve['points'])):
            raise PocketError('Quantize requires unowned fixed-gate note-relative expression; absolute/clock-dependent scope is unqualified')
        for field, schema in (('context', 'pocket.context/v1'), ('parent', 'pocket.curve/v1')):
            if curve[field] is not None:
                read_record(curve[field], store_root, schema)



def _revoice_notes(operation, chosen, child, store_root):
    if (operation['controller_timeline'] != 'preserve_existing'
            or operation['pitch_expression'] != 'preserve_relative'):
        raise PocketError('Revoice requires fixed controllers and explicit preserve_relative pitch expression')
    hypothesis = operation['hypothesis']
    if (not isinstance(hypothesis, dict)
            or set(hypothesis) != {'label', 'actor', 'actor_kind', 'statement', 'uncertainty'}):
        raise PocketError('Revoice requires a complete attributed harmonic hypothesis')
    for field, bound in (('label', 256), ('actor', 256), ('statement', 4096)):
        value = hypothesis[field]
        if not isinstance(value, str) or not value.strip() or len(value) > bound:
            raise PocketError(f'Harmonic hypothesis {field} must be nonempty text within {bound} characters')
    if hypothesis['actor_kind'] not in ('human', 'agent'):
        raise PocketError('Harmonic hypothesis requires human or agent attribution')
    uncertainty = hypothesis['uncertainty']
    if (not isinstance(uncertainty, list) or len(uncertainty) > 32
            or any(not isinstance(value, str) or not value.strip() or len(value) > 1024 for value in uncertainty)):
        raise PocketError('Harmonic uncertainty requires up to 32 nonempty bounded statements')
    destinations = operation['destinations']
    if not isinstance(destinations, list) or not 1 <= len(destinations) <= 4096:
        raise PocketError('Revoice requires 1–4096 exact note destinations')
    targets = {}
    for destination in destinations:
        if not isinstance(destination, dict) or set(destination) != {'note_id', 'pitch'}:
            raise PocketError('Revoice destination requires exactly note_id and full pitch')
        note_id = destination['note_id']
        if not isinstance(note_id, str) or not note_id or note_id in targets:
            raise PocketError('Revoice destination identities must be nonempty unique text')
        _validate_pitch(destination['pitch'])
        targets[note_id] = destination['pitch']
    if set(targets) != {note['id'] for note in chosen}:
        raise PocketError('Revoice destinations must exactly cover surviving selected original note IDs')
    owners = {key: clip for clip in child['clips'] for key in clip['note_ids']}
    events = {event['id']: event for event in child['events']}
    clip_events = {clip['id']: set(clip['event_ids']) for clip in child['clips']}
    verified_sources, changes = set(), []
    for note in chosen:
        _duplicate_source(note, child, clip_events[owners[note['id']]['id']], events, verified_sources, store_root)
        before = copy.deepcopy(note['pitch'])
        note['pitch'] = copy.deepcopy(targets[note['id']])
        changes.append({'note_id': note['id'], 'before': before, 'after': copy.deepcopy(note['pitch'])})
    return {'op': 'revoice', 'selected': len(chosen), 'changed': sum(row['before'] != row['after'] for row in changes),
            'destinations': changes, 'hypothesis': copy.deepcopy(hypothesis),
            'hypothesis_basis': 'supplied_attribution_not_inferred', 'tuning_conversion': 'not_performed',
            'voice_assignment': 'preserved_declared', 'expression_shape': 'exact_relative',
            'controller_timeline': 'preserve_existing', 'native': 'not_performed',
            'listening': 'not_performed', 'audible_contour': 'not_inferred',
            'source_binding_validation': 'retained_hash_and_declared_lifecycle', 'source_reparse': 'not_performed'}

def _reply_fits(value):
    # Walk iterators first, without materializing nested arrays or escaped strings.
    # Large opaque fields stay in the immutable edit, never in a formatter buffer.
    pending, size = [iter((value,))], 0
    while pending:
        try:
            item = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        size += 1
        if isinstance(item, str):
            size += len(item)
        elif isinstance(item, dict):
            pending.append(iter(item.keys()))
            pending.append(iter(item.values()))
        elif isinstance(item, list):
            pending.append(iter(item))
        if size > 16383 or len(pending) > 256:
            return False
    size = 1  # The actual CLI adds a trailing newline.
    try:
        for part in json.JSONEncoder(ensure_ascii=True, indent=2, allow_nan=False).iterencode(value):
            size += len(part.encode())
            if size > 16384:
                return False
    except (ValueError, RecursionError) as exc:
        raise PocketError('Edit receipt contains unsupported metadata') from exc
    return True


def _bounded_receipt(result):
    """Retain full proof in the edit artifact and budget only the response preview."""
    result = {**result, 'change_summary': dict(result['change_summary']),
              'invariant_report': dict(result['invariant_report'])}
    summary = result['change_summary']
    for key in ('changed', 'inserted', 'deleted'):
        summary[key] = list(summary[key])
    if _reply_fits(result):
        return result
    fields = result['invariant_report']['selected_locked_fields']
    if len(fields) > 32:
        result['invariant_report']['selected_locked_fields'] = fields[:32]
        result['invariant_report']['omitted_selected_locked_fields'] = len(fields) - 32
    while not _reply_fits(result):
        for key in ('changed', 'inserted', 'deleted'):
            if summary[key]:
                summary[key].pop()
                summary['omitted_' + key] = summary['total_' + key] - len(summary[key])
                break
        else:
            fields = result['invariant_report']['selected_locked_fields']
            if fields:
                result['invariant_report']['selected_locked_fields'] = []
                result['invariant_report']['omitted_selected_locked_fields'] = (
                    result['invariant_report'].get('omitted_selected_locked_fields', 0) + len(fields))
            else:
                raise PocketError('Edit receipt metadata exceeds its 16 KiB response bound; full artifacts remain available')
    return result


def midi_transform(material: ArtifactHandle | MaterialRecord, selection: MaterialSelection,
                   operations: list[MaterialEditOperation], store_root: str, request_id: str,
                   locks: MaterialLocks | None = None, expression_policy: str = 'reject',
                   overlap_policy: str = 'reject_new') -> dict:
    """Create a child revision with explicit selected-note and exact grid/dynamics edits.

    Selection must carry the hash returned by material_query. Outside notes,
    ordered raw events and curves stay unchanged. Expression-sensitive changes
    refuse unless their explicit supported preservation policy is supplied.
    """
    record = load_material(material, store_root)
    selected = resolve_selection(record, selection, require_hash=True)
    if record['coverage'].get('editing_allowed') is not True:
        raise PocketError('Material fidelity profile does not permit strict note editing')
    if not isinstance(operations, list) or not 1 <= len(operations) <= 64:
        raise PocketError('Provide 1–64 explicit note operations')
    locks = locks or {'outside_selection': 'all', 'selected_fields': []}
    if set(locks) - {'outside_selection', 'selected_fields'} or locks.get('outside_selection', 'all') != 'all':
        raise PocketError('Only exact outside_selection=all is supported')
    aliases = {'duration': 'duration_qn', 'expression_shape': 'expression_refs'}
    known = {'onset', 'duration_qn', 'pitch', 'velocity', 'release_velocity', 'channel', 'mute',
             'expression_refs', 'voice_id', 'role_ref', 'source_binding', 'derived_from'}
    selected_fields = locks.get('selected_fields', [])
    if not isinstance(selected_fields, list) or any(not isinstance(x, str) for x in selected_fields):
        raise PocketError('selected_fields must be an array of field paths')
    fields = [aliases.get(x, x) for x in selected_fields]
    locked_fields = set(fields)
    if any(x not in known for x in locked_fields):
        raise PocketError('Lock field does not resolve to a note field')
    if expression_policy not in ('reject', 'preserve_relative') or overlap_policy != 'reject_new':
        raise PocketError('Unsupported expression or overlap policy')
    inputs = {'material': material, 'selection': selection, 'operations': operations, 'locks': locks,
              'expression_policy': expression_policy, 'overlap_policy': overlap_policy}

    def work():
        child = copy.deepcopy(record)
        ids = set(selected['note_ids'])
        before = {n['id']: n for n in record['notes']}
        supported = {'shift': {'op', 'delta_qn'}, 'transpose': {'op', 'semitones'},
                     'repitch': {'op', 'pitch'}, 'velocity': {'op', 'value'},
                     'release_velocity': {'op', 'value'}, 'resize': {'op', 'duration_qn'},
                     'thin': {'op', 'every', 'offset'}}
        reports = []
        has_extensions = any(isinstance(operation, dict) and isinstance(operation.get('op'), str)
                             and operation['op'] in _NEW_OPERATIONS
                             for operation in operations)
        for operation_index, operation in enumerate(operations):
            if not isinstance(operation, dict):
                raise PocketError('Every note operation must be an object')
            op = operation.get('op')
            if not isinstance(op, str):
                raise PocketError('Note operation discriminator must be text')
            if op in _NEW_OPERATIONS:
                if set(operation) != _NEW_OPERATIONS[op]:
                    raise PocketError('New note operation has missing or unsupported fields')
            elif op not in supported or set(operation) - supported[op]:
                raise PocketError('Unknown operation or fields')
            chosen = [n for n in child['notes'] if n['id'] in ids]
            if op == 'revoice':
                curves = {curve['id']: curve for curve in child['curves']}
                for note in chosen:
                    if note['expression_refs'] and expression_policy != 'preserve_relative':
                        raise PocketError('Revoice of note expression requires expression_policy=preserve_relative')
                    _quantize_expression(note, curves, store_root)
                    for curve_id in note['expression_refs']:
                        target = curves[curve_id]['target']
                        if target['kind'] == 'per_note_pitch' and (target['value_mode'] != 'additive'
                                or target['unit'] not in ('cents', 'semitones')):
                            raise PocketError('Revoice pitch expression requires additive cents or semitones')
                reports.append({'operation_index': operation_index, **_revoice_notes(operation, chosen, child, store_root)})
                continue
            if op == 'groove':
                curves = {curve['id']: curve for curve in child['curves']}
                for note in chosen:
                    if note['expression_refs'] and expression_policy != 'preserve_relative':
                        raise PocketError('Groove of note expression requires expression_policy=preserve_relative')
                    _quantize_expression(note, curves, store_root)
                reports.append({'operation_index': operation_index, **apply_groove(chosen, operation)})
                continue
            if op in ('add', 'split', 'merge'):
                reports.append(_construct_notes(operation, chosen, child, record, operation_index,
                                                locked_fields, store_root))
                continue
            if op in ('delete', 'duplicate', 'grid_quantize') and operation['controller_timeline'] != 'preserve_existing':
                raise PocketError('Explicit controller_timeline=preserve_existing is required')
            if op in ('delete', 'duplicate') and any(note['expression_refs'] for note in chosen):
                raise PocketError('Delete/duplicate of note-owned expression requires a qualified curve ownership route')
            if op == 'delete':
                if fields:
                    raise PocketError('Deletion removes locked note fields; use an unlocked explicit selection')
                delete = {note['id'] for note in chosen}
                _remove_notes(child, delete)
                reports.append({'operation_index': operation_index, 'op': op, 'deleted': sorted(delete)})
                continue
            if op == 'duplicate':
                delta = _edit_rational(operation['delta_qn'], 'duplicate offset')
                if delta == 0:
                    raise PocketError('Duplicate requires a nonzero offset')
                if len(child['notes']) + len(chosen) > MAX_NOTES:
                    raise PocketError('Duplicate exceeds the 100000-note material bound')
                owners = {key: clip for clip in child['clips'] for key in clip['note_ids']}
                current_ids = {note['id'] for note in child['notes']}
                events = {event['id']: event for event in child['events']}
                clip_events = {clip['id']: set(clip['event_ids']) for clip in child['clips']}
                verified_sources = set()
                copies = []
                for note in chosen:
                    clip = owners[note['id']]
                    _duplicate_source(note, child, clip_events[clip['id']], events, verified_sources, store_root)
                    onset = rational(note['onset']) + delta
                    if onset < 0 or onset + rational(note['duration_qn']) > rational(clip['length_qn']):
                        raise PocketError('Duplicated note must fit its original clip; no implicit extension or wrapping')
                    note_id = identifier('note', digest({'profile': 'duplicate-v1', 'parent_revision': record['revision_sha256'],
                        'operation_index': operation_index, 'source_note_id': note['id'], 'delta_qn': qn(delta)}))
                    if note_id in current_ids:
                        raise PocketError('Duplicate derived note identity collides with existing material')
                    copied = {**copy.deepcopy(note), 'id': note_id, 'onset': {'space': 'clip_qn', **qn(onset)},
                              'source_binding': None, 'derived_from': [note['id']]}
                    child['notes'].append(copied)
                    clip['note_ids'].append(note_id)
                    current_ids.add(note_id)
                    copies.append({'source_note_id': note['id'], 'new_note_id': note_id, 'clip_id': clip['id']})
                reports.append({'operation_index': operation_index, 'op': op, 'copies': copies,
                                'controller_expression_at_new_times': 'not_inferred',
                                'copied_fields': 'exact_except_id_onset_source_binding_derived_from',
                                'source_binding_validation': 'retained_hash_and_declared_lifecycle',
                                'source_reparse': 'not_performed'})
                continue
            if op == 'grid_quantize':
                grid, phase, strength, threshold = (_edit_rational(operation[field], field)
                    for field in ('grid_qn', 'phase_qn', 'strength', 'threshold_qn'))
                if grid <= 0 or not 0 <= strength <= 1 or not 0 <= threshold <= grid / 2:
                    raise PocketError('Quantize requires positive grid, strength 0..1 and threshold 0..grid/2')
                if (operation['ties'] not in ('earlier', 'later') or operation['note_off'] != 'follow_onset'
                        or operation['time_space'] != 'clip_qn'):
                    raise PocketError('Quantize requires explicit earlier/later ties, clip_qn and note_off=follow_onset')
                curves = {curve['id']: curve for curve in child['curves']}
                moved, outside_threshold, largest = 0, 0, qn(0)
                for note in chosen:
                    if note['expression_refs'] and expression_policy != 'preserve_relative':
                        raise PocketError('Quantize of note expression requires expression_policy=preserve_relative')
                    _quantize_expression(note, curves, store_root)
                    onset = rational(note['onset'])
                    lower = phase + ((onset - phase) // grid) * grid
                    upper = lower + grid
                    nearest = lower if (onset - lower < upper - onset or
                        onset - lower == upper - onset and operation['ties'] == 'earlier') else upper
                    if abs(nearest - onset) > threshold:
                        outside_threshold += 1
                        continue
                    delta = strength * (nearest - onset)
                    note['onset'] = {'space': 'clip_qn', **qn(onset + delta)}
                    moved += delta != 0
                    largest = qn(max(rational(largest), abs(delta)))
                reports.append({'operation_index': operation_index, 'op': op, 'selected': len(chosen), 'moved': moved,
                                'outside_threshold': outside_threshold, 'maximum_onset_change_qn': largest,
                                'gate': 'unchanged', 'note_relative_curves': 'exact', 'clip_length': 'unchanged'})
                continue
            if op == 'velocity_map':
                field, mapping, unmapped = operation['field'], operation['mapping'], operation['unmapped']
                if field not in ('velocity', 'release_velocity') or unmapped not in ('preserve', 'reject'):
                    raise PocketError('Velocity map requires an attack/release field and explicit unmapped policy')
                if not isinstance(mapping, list) or not 1 <= len(mapping) <= 128:
                    raise PocketError('Velocity mapping requires 1–128 explicit entries')
                table = {}
                for entry in mapping:
                    if not isinstance(entry, dict) or set(entry) != {'from_value', 'to_value'}:
                        raise PocketError('Velocity mapping entry requires exactly from_value and to_value')
                    low = 1 if field == 'velocity' else 0
                    source = integer(entry['from_value'], 'mapped source velocity', low, 127)
                    target = integer(entry['to_value'], 'mapped destination velocity', low, 127)
                    if source in table:
                        raise PocketError('Velocity mapping source values must be unique')
                    table[source] = target
                missing, changed = 0, 0
                for note in chosen:
                    value = note[field]['value']
                    if value not in table:
                        if unmapped == 'reject':
                            raise PocketError('Selected velocity has no explicit mapping')
                        missing += 1
                        continue
                    changed += value != table[value]
                    note[field]['value'] = table[value]
                reports.append({'operation_index': operation_index, 'op': op, 'field': field,
                                'mapped': len(chosen) - missing, 'unmapped_preserved': missing, 'changed': changed})
                continue
            if op == 'thin':
                every = integer(operation.get('every'), 'thin every', 2, 1024)
                offset = integer(operation.get('offset', 0), 'thin offset', 0, every - 1)
                if fields:
                    raise PocketError('Thinning removes locked note fields; use an unlocked explicit selection')
                delete = {n['id'] for index, n in enumerate(chosen) if index % every != offset}
                _remove_notes(child, delete)
                continue
            for note in chosen:
                if note['expression_refs'] and (op == 'resize' or op in ('transpose', 'repitch') and expression_policy != 'preserve_relative'):
                    raise PocketError('Expressive resize/repitch needs a qualified expression policy')
                if op == 'shift':
                    onset = rational(note['onset']) + rational(operation.get('delta_qn'))
                    note['onset'] = {'space': 'clip_qn', **qn(onset)}
                elif op == 'transpose':
                    note['pitch']['midi_note'] += integer(operation.get('semitones'), 'semitones', -127, 127)
                elif op == 'repitch':
                    note['pitch']['midi_note'] = integer(operation.get('pitch'), 'pitch', 0, 127)
                elif op in ('velocity', 'release_velocity'):
                    note[op]['value'] = integer(operation.get('value'), op, 1 if op == 'velocity' else 0, 127)
                elif op == 'resize':
                    note['duration_qn'] = qn(rational(operation.get('duration_qn')))
        after = {n['id']: n for n in child['notes']}
        for note_id, prior in before.items():
            if note_id not in ids and after.get(note_id) != prior:
                raise PocketError('Outside-selection invariant failed')
            if note_id in ids and any(after.get(note_id, {}).get(field) != prior[field] for field in locked_fields):
                raise PocketError('Edit conflicts with selected field lock')
        comparisons = _reject_new_overlaps(record, child)
        child['parent_revision'] = record['revision_sha256']
        child['provenance'] = {**child['provenance'], 'last_edit': {'provider': 'midi-transform-v1',
                                'operations': operations, 'selection_sha256': selected['selection_sha256']}}
        child = finalize_material(child)
        parent_handle, child_handle = put_record(record, store_root), put_record(child, store_root)
        difference = semantic_diff(record, child)
        invariants = {'outside_selection': 'exact', 'selected_locked_fields': fields,
                      'ordered_events': 'exact', 'curves': 'exact'}
        edit = {'schema': 'pocket.edit/v1', 'edit_id': identifier('edit', digest(inputs)),
                'parent': parent_handle, 'selection_sha256': selected['selection_sha256'],
                'operations': operations, 'child': child_handle, 'semantic_diff': difference,
                'inverse': {'expected_child_revision': child['revision_sha256'], 'restore_parent': parent_handle,
                            'execution': 'use_exact_parent_artifact'}, 'invariant_report': invariants}
        if has_extensions:
            edit['operation_reports'] = reports
            edit['extension_profile'] = 'pocket.selected-note-extensions/v1'
            edit['overlap_validation'] = {'comparisons': comparisons, 'budget': _OVERLAP_COMPARISON_LIMIT,
                                           'result': 'no_new_overlap'}
        edit_handle = put_record(edit, store_root)
        compact_diff = {**difference, 'inserted': difference['inserted'][:16],
                        'deleted': difference['deleted'][:16], 'changed': difference['changed'][:8],
                        'total_inserted': len(difference['inserted']), 'total_deleted': len(difference['deleted']),
                        'total_changed': len(difference['changed']),
                        'omitted_changed': max(0, len(difference['changed']) - 8)}
        for key in ('inserted', 'deleted'):
            if compact_diff['total_' + key] > len(compact_diff[key]):
                compact_diff['omitted_' + key] = compact_diff['total_' + key] - len(compact_diff[key])
        result = receipt(request_id=request_id, artifacts={'material': child_handle, 'edit': edit_handle},
            material=child_handle, edit=edit_handle, change_summary=compact_diff, invariant_report=invariants,
            coverage={'symbolic_edit': 'exact', 'native': 'not_performed', 'listening': 'not_performed'},
            uncertainty=['Changes in note gate or velocity do not establish acoustic duration or loudness.'])
        if has_extensions:
            result['uncertainty'].append('Controller timelines remain fixed; copied or moved notes may receive different expression.')
        if any(operation['op'] in ('add', 'split', 'merge') for operation in operations):
            result['uncertainty'].append('New or removed attacks/releases do not establish acoustic continuity or receiver behavior.')
        return _bounded_receipt(result)
    return run_request(store_root, request_id, 'midi_transform', inputs, work)
