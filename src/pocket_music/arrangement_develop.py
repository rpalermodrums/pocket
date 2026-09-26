# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded multi-section choices composed from explicit whole-clip graph nodes.

Publication uses public sequence/edit primitives. Read-only verification rebuilds
canonical sequence expectations and independently compares every protected note.
"""
from __future__ import annotations

import copy
import math
import re
from pathlib import Path
from typing import Literal

from .arrangement_types import ArrangementDefinition
from .artifact_store import (
    ArtifactHandle,
    _verify_handles,
    canonical_bytes,
    digest,
    put_record,
    read_bytes,
    read_record,
    receipt,
    run_request,
)
from .errors import PocketError
from .material import (
    finalize_material,
    identifier,
    integer,
    load_material,
    material_query,
    qn,
    rational,
    resolve_selection,
    semantic_diff,
)
from .material_sequence import COVERAGE as SEQUENCE_COVERAGE
from .material_sequence import _construct as sequence_expected
from .material_sequence import _prepare as validate_sequence
from .material_sequence import material_sequence
from .material_structure import _attribution, _context, material_structure
from .midi_develop import _fields, _list, _q, _text
from .midi_edit import _OVERLAP_COMPARISON_LIMIT, _bounded_receipt, _reject_new_overlaps, midi_transform

SCHEMA = 'pocket.arrangement-development/v1'
COVERAGE = {'provider': 'arrangement-development-v1', 'source_profile': 'canonical_ordinary_note_whole_clips/v1',
            'structure': 'caller_declared_membership_and_placement', 'roles': 'authored_not_inferred',
            'source_origins': 'replaced_by_declared_destination_placements', 'native_execution': False,
            'human_listening': 'not_performed', 'performance_readiness': 'not_established'}
LOCKS = ['duration_qn', 'velocity', 'release_velocity', 'channel', 'mute', 'expression_refs',
         'voice_id', 'role_ref', 'source_binding', 'derived_from']


def _values(value):
    count, pending = 0, [value]
    while pending:
        current = pending.pop()
        count += 1
        if isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return count


def _source_budget(value, store):
    _verify_handles(value, store)
    pending, seen, notes, size, nodes = [value], set(), 0, 0, 0
    while pending:
        current = pending.pop()
        if isinstance(current, dict) and current.get('schema') == 'pocket.artifact-handle/v1':
            key = canonical_bytes(current)
            if key in seen:
                continue
            seen.add(key)
            payload = read_bytes(current, store)
            if Path(current['artifact_uri']).name == 'record.json' or current['artifact_schema'] in {'pocket.material-structure/v1', 'pocket.material/v1'}:
                record = read_record(current, store)
                size += len(payload)
                nodes += _values(record)
                if record['schema'] == 'pocket.material/v1':
                    notes += len(record['notes'])
                pending.append(record)
            if size > 8 * 1024 * 1024 or nodes > 100000:
                raise PocketError('Arrangement source ancestry exceeds 8 MiB or 100000 metadata values')
        elif isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return notes, size, nodes


def _prepare(definition, store):
    _fields(definition, {'label', 'structure', 'expected_structure_revision', 'clock', 'origin', 'length_qn',
                         'sections', 'locked_section_ids', 'variations', 'seeds', 'attribution'}, 'arrangement definition')
    if len(canonical_bytes(definition)) > 1024 * 1024:
        raise PocketError('Arrangement definition exceeds 1 MiB')
    _text(definition['label'], 'arrangement label')
    source_notes, source_bytes, source_values = _source_budget(definition, store)
    material_structure('query', store, structure=definition['structure'])
    graph = read_record(definition['structure'], store, 'pocket.material-structure/v1')
    if definition['expected_structure_revision'] != definition['structure']['sha256']:
        raise PocketError('Stale arrangement structure revision')
    normalized = copy.deepcopy(definition)
    normalized['attribution'] = _attribution(definition['attribution'], store, _context())
    length = _q(definition['length_qn'], 'arrangement length')
    if not 0 < length <= 65536:
        raise PocketError('Arrangement length requires >0 through 65536 quarter notes')
    normalized['length_qn'] = qn(length)
    _fields(definition['seeds'], {'structure', 'pitch', 'timing'}, 'arrangement seeds')
    for value in definition['seeds'].values():
        integer(value, 'arrangement seed', -(2**53), 2**53)
    node_index = {row['node_id']: row for row in graph['nodes']}
    sections, materials, parents, note_total, end = [], {}, {}, 0, rational(0)
    for section in _list(definition['sections'], 2, 32, 'arrangement sections'):
        _fields(section, {'section_id', 'node_id', 'at_qn'}, 'arrangement section')
        key = _text(section['section_id'], 'section identity')
        node_id = _text(section['node_id'], 'node identity')
        if key in {row['section_id'] for row in sections} or node_id not in node_index:
            raise PocketError('Duplicate section identity or unknown graph node')
        node = node_index[node_id]
        material = load_material(node['material'], store)
        parents[node['material']['sha256']] = node['material']
        materials[node['material_key']] = node['material']
        clip = next(row for row in material['clips'] if row['id'] == node['clip_id'])
        if (rational(node['span_qn']['start']) != 0 or rational(node['span_qn']['end']) != rational(clip['length_qn'])
                or set(node['note_ids']) != set(clip['note_ids']) or len(node['note_ids']) != len(clip['note_ids'])):
            raise PocketError('Arrangement nodes must contain exact whole-clip span and membership')
        at = _q(section['at_qn'], 'section placement')
        if at < end or at + rational(clip['length_qn']) > length:
            raise PocketError('Ordered full section spans including rests must not overlap and must fit the arrangement')
        end = at + rational(clip['length_qn'])
        sections.append({**copy.deepcopy(section), 'at_qn': qn(at), 'end_qn': qn(end), 'node': node,
                         'material': material, 'clip': clip})
        note_total += len(clip['note_ids'])
    if not 1 <= note_total <= 1024:
        raise PocketError('Arrangement output requires 1–1024 notes')
    normalized['sections'] = [{key: row[key] for key in ('section_id', 'node_id', 'at_qn')} for row in sections]
    by_section = {row['section_id']: row for row in sections}
    locked = _list(definition['locked_section_ids'], 1, 32, 'locked sections')
    for key in locked:
        _text(key, 'locked section identity')
    if len(set(locked)) != len(locked) or set(locked) - by_section.keys():
        raise PocketError('Unknown or duplicate locked section')
    earliest = min(rational(by_section[key]['at_qn']) for key in locked)
    normalized['locked_section_ids'] = sorted(locked)
    choices, variations, varied = [], [], set()
    for variation in _list(definition['variations'], 1, 16, 'section variations'):
        _fields(variation, {'section_id', 'endpoint_note_id', 'pitch_offsets_semitones', 'timing_offsets_qn',
                           'pitch_min', 'pitch_max'}, 'section variation')
        section_id = _text(variation['section_id'], 'varied section identity')
        endpoint_id = _text(variation['endpoint_note_id'], 'endpoint note identity')
        if section_id not in by_section or section_id in locked or section_id in varied:
            raise PocketError('Variation section must be known, unique and unlocked')
        section = by_section[section_id]
        if rational(section['at_qn']) <= earliest:
            raise PocketError('Variation section must be later than the earliest locked section')
        varied.add(section_id)
        notes = {row['id']: row for row in section['material']['notes'] if row['id'] in section['clip']['note_ids']}
        if endpoint_id not in notes or rational(notes[endpoint_id]['onset']) != max(rational(row['onset']) for row in notes.values()):
            raise PocketError('Explicit endpoint must be a latest-onset member of the whole clip')
        low = integer(variation['pitch_min'], 'pitch minimum', 0, 127)
        high = integer(variation['pitch_max'], 'pitch maximum', 0, 127)
        if low > high or any(not low <= row['pitch']['midi_note'] <= high for row in notes.values()):
            raise PocketError('Source section violates declared pitch bounds')
        pitches = _list(variation['pitch_offsets_semitones'], 1, 16, 'pitch domain')
        for value in pitches:
            integer(value, 'pitch offset', -127, 127)
        times = [_q(value, 'timing offset') for value in _list(variation['timing_offsets_qn'], 1, 16, 'timing domain')]
        if len(set(pitches)) != len(pitches) or len(set(times)) != len(times) or any(abs(value) > 4096 for value in times):
            raise PocketError('Variation domains require unique bounded offsets')
        endpoint = notes[endpoint_id]
        feasible = [(pitch, timing) for pitch in pitches for timing in times if (pitch or timing)
                    and low <= endpoint['pitch']['midi_note'] + pitch <= high
                    and 0 <= rational(endpoint['onset']) + timing
                    and rational(endpoint['onset']) + timing + rational(endpoint['duration_qn']) <= rational(section['clip']['length_qn'])]
        if not feasible:
            raise PocketError('No nonzero feasible section endpoint variation')
        pitch, timing = min(feasible, key=lambda pair: digest([definition['seeds'], section_id, endpoint_id, pair[0], qn(pair[1])]))
        choices.append({'section_id': section_id, 'source_note_id': endpoint_id,
                        'pitch_offset_semitones': pitch, 'timing_offset_qn': qn(timing)})
        variations.append({**copy.deepcopy(variation), 'timing_offsets_qn': [qn(value) for value in times]})
    order = {row['section_id']: index for index, row in enumerate(sections)}
    choices.sort(key=lambda row: order[row['section_id']])
    normalized['variations'] = sorted(variations, key=lambda row: order[row['section_id']])
    sequence = {'label': normalized['label'], 'materials': [{'key': key, 'material': value} for key, value in materials.items()],
                'clock': normalized['clock'], 'origin': normalized['origin'], 'length_qn': normalized['length_qn'],
                'occurrences': [{'occurrence_id': row['section_id'], 'material_key': row['node']['material_key'],
                                 'material_revision': row['material']['revision_sha256'], 'clip_id': row['clip']['id'],
                                 'at_qn': row['at_qn']} for row in sections],
                'controller_policy': 'reject_present', 'expression_policy': 'reject_present', 'overlap_policy': 'reject_same_channel_pitch'}
    canonical_sequence, bindings = validate_sequence(sequence, store)
    expected, _occurrences, derivations = sequence_expected(canonical_sequence, bindings)
    deletion_copies = sum(max(0, note_total - offset) for offset in range(256, note_total + 256, 256))
    note_copies = source_notes + note_total * (len(choices) + 2) + deletion_copies
    multiplier = len(choices) + math.ceil(note_total / 256) + 10
    metadata_bytes = 2 * source_bytes + multiplier * len(canonical_bytes(expected)) + 4 * len(canonical_bytes(normalized))
    metadata_values = 2 * source_values + multiplier * _values(expected) + 4 * _values(normalized)
    if note_copies > 10000:
        raise PocketError('Arrangement evidence exceeds 10000 material-note copies: S+N*(V+2)+deletioncopies')
    if metadata_bytes > 48 * 1024 * 1024 or metadata_values > 750000:
        raise PocketError('Arrangement generated proof reserve exceeds 48 MiB or 750000 metadata values')
    budget = {'source_material_notes': source_notes, 'output_notes': note_total, 'variations': len(choices),
              'deletion_copies': deletion_copies, 'material_note_copies': note_copies, 'maximum': 10000,
              'formula': 'S+N*(V+2)+sum(remaining_notes_after_each_256_note_delete)',
              'source_metadata_bytes': source_bytes, 'source_metadata_values': source_values,
              'proof_metadata_bytes_reserve': metadata_bytes, 'proof_metadata_values_reserve': metadata_values}
    section_rows = [{'section_id': row['section_id'], 'node_id': row['node_id'], 'at_qn': row['at_qn'], 'end_qn': row['end_qn'],
                     'material': row['node']['material'], 'clip_id': row['clip']['id'], 'source_origin': row['clip']['origin'],
                     'note_count': len(row['clip']['note_ids']), 'locked': row['section_id'] in locked,
                     'variation': next((choice for choice in choices if choice['section_id'] == row['section_id']), None)} for row in sections]
    # Include all graph-bound parents, not merely sources chosen for output.
    unchanged = {'structure': definition['structure'], 'materials': list({row['material']['sha256']: row['material'] for row in graph['materials']}.values())}
    return normalized, sequence, expected, derivations, choices, budget, section_rows, unchanged


def _changes(first, second, mappings, choices):
    expected = copy.deepcopy(first['notes'])
    by_id = {row['id']: row for row in expected}
    changes = []
    for choice in choices:
        matches = [row for row in mappings if row['occurrence_id'] == choice['section_id'] and row['source_note_id'] == choice['source_note_id']]
        if len(matches) != 1:
            raise PocketError('Arrangement endpoint derivation mismatch')
        note = by_id[matches[0]['note_id']]
        before = copy.deepcopy(note)
        note['pitch']['midi_note'] += choice['pitch_offset_semitones']
        note['onset'] = {'space': 'clip_qn', **qn(rational(note['onset']) + rational(choice['timing_offset_qn']))}
        changes.append({**copy.deepcopy(choice), 'note_id': note['id'], 'before': before, 'after': copy.deepcopy(note)})
    if second['notes'] != expected:
        raise PocketError('Arrangement exact note/lock proof mismatch')
    for field in first.keys() - {'notes', 'revision_sha256', 'parent_revision', 'provenance'}:
        if first[field] != second[field]:
            raise PocketError('Arrangement protected material structure changed')
    if {key: value for key, value in second['provenance'].items() if key != 'last_edit'} != first['provenance']:
        raise PocketError('Arrangement protected clock/provenance changed')
    return changes


def _handle(record):
    sha = digest(record)
    return {'schema': 'pocket.artifact-handle/v1', 'artifact_uri': f'artifacts/{sha}/record.json',
            'sha256': sha, 'artifact_schema': record['schema']}


def _step(step, provider, arguments):
    _fields(step, {'provider', 'arguments', 'result'}, 'public step')
    if step['provider'] != provider or not isinstance(step['arguments'], dict):
        raise PocketError('Arrangement public step provider mismatch')
    request = step['arguments'].get('request_id')
    if not isinstance(request, str) or not re.fullmatch('arrange-[0-9a-f]{48}', request):
        raise PocketError('Arrangement public step request identity mismatch')
    if step['arguments'] != {**arguments, 'request_id': request}:
        raise PocketError('Arrangement public step arguments mismatch')
    return request


def _verify_edit_step(step, before, before_handle, ids, operations, locks, store):
    selection = resolve_selection(before, {'note_ids': ids})
    args = {'material': before_handle, 'selection': selection, 'operations': operations}
    if locks is not None:
        args['locks'] = locks
    request = _step(step, 'midi_transform', args)
    child = copy.deepcopy(before)
    selected = set(ids)
    deletion = operations[0]['op'] == 'delete'
    for operation in operations:
        if operation['op'] == 'delete':
            child['notes'] = [note for note in child['notes'] if note['id'] not in selected]
            for clip in child['clips']:
                clip['note_ids'] = [key for key in clip['note_ids'] if key not in selected]
        else:
            for note in child['notes']:
                if note['id'] in selected:
                    if operation['op'] == 'transpose':
                        note['pitch']['midi_note'] += operation['semitones']
                    else:
                        note['onset'] = {'space': 'clip_qn', **qn(rational(note['onset']) + rational(operation['delta_qn']))}
    child['parent_revision'] = before['revision_sha256']
    child['provenance'] = {**child['provenance'], 'last_edit': {'provider': 'midi-transform-v1',
                           'operations': operations, 'selection_sha256': selection['selection_sha256']}}
    child = finalize_material(child)
    comparisons = _reject_new_overlaps(before, child)
    child_handle = _handle(child)
    actual = load_material(step['result']['artifacts']['material'], store)
    if actual != child or step['result']['artifacts']['material'] != child_handle:
        raise PocketError('Arrangement linked edit child/parent/clock proof mismatch')
    active_locks = locks or {'outside_selection': 'all', 'selected_fields': []}
    fields = active_locks['selected_fields']
    difference = semantic_diff(before, child)
    invariants = {'outside_selection': 'exact', 'selected_locked_fields': fields,
                  'ordered_events': 'exact', 'curves': 'exact'}
    inputs = {'material': before_handle, 'selection': selection, 'operations': operations,
              'locks': active_locks, 'expression_policy': 'reject', 'overlap_policy': 'reject_new'}
    edit = {'schema': 'pocket.edit/v1', 'edit_id': identifier('edit', digest(inputs)),
            'parent': before_handle, 'selection_sha256': selection['selection_sha256'], 'operations': operations,
            'child': child_handle, 'semantic_diff': difference,
            'inverse': {'expected_child_revision': child['revision_sha256'], 'restore_parent': before_handle,
                        'execution': 'use_exact_parent_artifact'}, 'invariant_report': invariants}
    if deletion:
        edit.update(operation_reports=[{'operation_index': 0, 'op': 'delete', 'deleted': sorted(ids)}],
                    extension_profile='pocket.selected-note-extensions/v1',
                    overlap_validation={'comparisons': comparisons, 'budget': _OVERLAP_COMPARISON_LIMIT, 'result': 'no_new_overlap'})
    edit_handle = _handle(edit)
    if read_record(step['result']['artifacts']['edit'], store, 'pocket.edit/v1') != edit:
        raise PocketError('Arrangement linked edit artifact mismatch')
    compact = {**difference, 'inserted': difference['inserted'][:16], 'deleted': difference['deleted'][:16],
               'changed': difference['changed'][:8], 'total_inserted': len(difference['inserted']),
               'total_deleted': len(difference['deleted']), 'total_changed': len(difference['changed']),
               'omitted_changed': max(0, len(difference['changed']) - 8)}
    for key in ('inserted', 'deleted'):
        if compact['total_' + key] > len(compact[key]):
            compact['omitted_' + key] = compact['total_' + key] - len(compact[key])
    expected = receipt(request_id=request, artifacts={'material': child_handle, 'edit': edit_handle},
                       material=child_handle, edit=edit_handle, change_summary=compact, invariant_report=invariants,
                       coverage={'symbolic_edit': 'exact', 'native': 'not_performed', 'listening': 'not_performed'},
                       uncertainty=['Changes in note gate or velocity do not establish acoustic duration or loudness.'])
    if deletion:
        expected['uncertainty'].append('Controller timelines remain fixed; copied or moved notes may receive different expression.')
    if step['result'] != _bounded_receipt(expected):
        raise PocketError('Arrangement public edit receipt mismatch')
    return child, child_handle


def _verify_steps(record, sequence_definition, expected, mappings, choices, store):
    steps = record['public_steps']
    if not isinstance(steps, list) or len(steps) != 1 + len(choices) + math.ceil(len(expected['notes']) / 256):
        raise PocketError('Arrangement public steps are incomplete')
    canonical_sequence, bindings = validate_sequence(sequence_definition, store)
    _, occurrences, _ = sequence_expected(canonical_sequence, bindings)
    a_handle = _handle(expected)
    sequence = {'schema': 'pocket.material-sequence/v1', 'definition': canonical_sequence, 'material': a_handle,
                'occurrences': occurrences, 'note_derivations': mappings, 'coverage': copy.deepcopy(SEQUENCE_COVERAGE),
                'invariants': {'parents': 'unchanged', 'note_fields_except_id_onset_voice_derivation': 'exact',
                               'whole_clip_lengths_and_rests': 'retained', 'same_pitch_overlaps': 'absent'}}
    sequence_handle = _handle(sequence)
    if record['sequence'] != sequence_handle or read_record(record['sequence'], store) != sequence:
        raise PocketError('Arrangement exact sequence artifact mismatch')
    request = _step(steps[0], 'material_sequence', {'definition': sequence_definition})
    expected_result = receipt(request, artifacts={'material': a_handle, 'sequence': sequence_handle},
        change_summary={'source_materials': len(bindings), 'occurrences': len(occurrences), 'notes': len(expected['notes']), 'destination_clips': 1},
        coverage=copy.deepcopy(SEQUENCE_COVERAGE),
        uncertainty=['Declared placements do not establish audio alignment, native playback or musical usefulness.'])
    if steps[0]['result'] != expected_result:
        raise PocketError('Arrangement public sequence receipt mismatch')
    current, current_handle = expected, a_handle
    for step, choice in zip(steps[1:1 + len(choices)], choices, strict=True):
        mapping = next(row for row in mappings if row['occurrence_id'] == choice['section_id'] and row['source_note_id'] == choice['source_note_id'])
        operations = []
        if choice['pitch_offset_semitones']:
            operations.append({'op': 'transpose', 'semitones': choice['pitch_offset_semitones']})
        if rational(choice['timing_offset_qn']):
            operations.append({'op': 'shift', 'delta_qn': choice['timing_offset_qn']})
        current, current_handle = _verify_edit_step(step, current, current_handle, [mapping['note_id']], operations,
                                                   {'outside_selection': 'all', 'selected_fields': LOCKS}, store)
    if record['alternatives'] != [a_handle, current_handle]:
        raise PocketError('Arrangement alternatives do not match complete edit chain')
    current, current_handle = expected, a_handle
    ids = [row['id'] for row in expected['notes']]
    for offset, step in zip(range(0, len(ids), 256), steps[1 + len(choices):], strict=True):
        current, current_handle = _verify_edit_step(step, current, current_handle, ids[offset:offset + 256],
            [{'op': 'delete', 'controller_timeline': 'preserve_existing'}], None, store)
    if record['no_addition'] != current_handle:
        raise PocketError('Arrangement no-addition does not match complete deletion chain')


def _verify_report(record, store):
    try:
        return _verify_report_inner(record, store)
    except PocketError:
        raise
    except (KeyError, TypeError, IndexError, AttributeError, ValueError, OverflowError) as error:
        raise PocketError('Malformed arrangement proof record') from error


def _verify_report_inner(record, store):
    _fields(record, {'schema', 'definition', 'unchanged', 'alternatives', 'no_addition', 'sequence', 'sections', 'changes',
                     'evidence_budget', 'public_steps', 'step_environment', 'coverage'}, 'arrangement report')
    normalized, sequence_definition, expected, mappings, choices, budget, sections, unchanged = _prepare(record['definition'], store)
    if (record['definition'] != normalized or record['sections'] != sections or record['unchanged'] != unchanged
            or record['evidence_budget'] != budget or record['coverage'] != COVERAGE
            or record['step_environment'] != 'caller supplies store_root'):
        raise PocketError('Arrangement report derived section/source proofs mismatch')
    if not isinstance(record['alternatives'], list) or len(record['alternatives']) != 2:
        raise PocketError('Arrangement requires exactly A and B')
    _verify_steps(record, sequence_definition, expected, mappings, choices, store)
    a, b = [load_material(handle, store) for handle in record['alternatives']]
    if a != expected:
        raise PocketError('Arrangement A does not match declared source sequence')
    changes = _changes(a, b, mappings, choices)
    if changes != record['changes']:
        raise PocketError('Arrangement changes proof mismatch')
    silent = load_material(record['no_addition'], store)
    if silent['notes'] or silent['events'] or silent['curves']:
        raise PocketError('Arrangement no-addition is not empty')
    if silent['clips'] != [{**clip, 'note_ids': []} for clip in a['clips']]:
        raise PocketError('Arrangement no-addition span changed')
    for field in ('tracks', 'sources', 'tempo_map_ref', 'meter_map_ref'):
        if silent[field] != a[field]:
            raise PocketError('Arrangement no-addition context changed')
    sequence = read_record(record['sequence'], store, 'pocket.material-sequence/v1')
    if sequence['material'] != record['alternatives'][0] or sequence['note_derivations'] != mappings:
        raise PocketError('Arrangement sequence derivation proof mismatch')
    return record


def midi_arrangement_develop(*, store_root: str, request_id: str, definition: ArrangementDefinition) -> dict:
    """Compose explicit whole-clip sections with protected literal and varied alternatives."""
    stages, attempting = [], None
    supplied = copy.deepcopy(definition)

    def invoke(name, function, arguments):
        nonlocal attempting
        attempting = {'provider': name, 'arguments': {key: value for key, value in arguments.items() if key != 'store_root'}}
        result = function(**arguments)
        stages.append({**attempting, 'result': result})
        return result

    def request(stage):
        return 'arrange-' + digest([request_id, stage])[:48]

    def work():
        try:
            normalized, sequence, expected, mappings, choices, budget, sections, unchanged = _prepare(supplied, store_root)
            a = invoke('material_sequence', material_sequence, {'store_root': store_root, 'request_id': request('sequence'), 'definition': sequence})
            current = a['artifacts']['material']
            if load_material(current, store_root) != expected:
                raise PocketError('Public sequence differs from validated expectation')
            for index, choice in enumerate(choices):
                mapping = next(row for row in mappings if row['occurrence_id'] == choice['section_id'] and row['source_note_id'] == choice['source_note_id'])
                selection = material_query(current, store_root, selection={'note_ids': [mapping['note_id']]})['selection']
                operations = []
                if choice['pitch_offset_semitones']:
                    operations.append({'op': 'transpose', 'semitones': choice['pitch_offset_semitones']})
                if rational(choice['timing_offset_qn']):
                    operations.append({'op': 'shift', 'delta_qn': choice['timing_offset_qn']})
                changed = invoke('midi_transform', midi_transform, {'store_root': store_root, 'request_id': request(f'variation-{index}'),
                    'material': current, 'selection': selection, 'operations': operations,
                    'locks': {'outside_selection': 'all', 'selected_fields': LOCKS}})
                current = changed['artifacts']['material']
            silent = a['artifacts']['material']
            ids = [note['id'] for note in expected['notes']]
            for offset in range(0, len(ids), 256):
                selection = material_query(silent, store_root, selection={'note_ids': ids[offset:offset + 256]}, max_bytes=65536)['selection']
                if selection is None:
                    raise PocketError('Arrangement deletion selection exceeds query budget')
                deleted = invoke('midi_transform', midi_transform, {'store_root': store_root, 'request_id': request(f'no-addition-{offset}'),
                    'material': silent, 'selection': selection, 'operations': [{'op': 'delete', 'controller_timeline': 'preserve_existing'}]})
                silent = deleted['artifacts']['material']
            record = {'schema': SCHEMA, 'definition': normalized, 'unchanged': unchanged,
                      'alternatives': [a['artifacts']['material'], current], 'no_addition': silent,
                      'sequence': a['artifacts']['sequence'], 'sections': sections,
                      'changes': _changes(expected, load_material(current, store_root), mappings, choices),
                      'evidence_budget': budget, 'public_steps': stages, 'step_environment': 'caller supplies store_root',
                      'coverage': copy.deepcopy(COVERAGE)}
            _verify_report(record, store_root)
            handle = put_record(record, store_root)
            public_unchanged = {'structure': unchanged['structure'], 'materials': unchanged['materials'][:8]}
            result = receipt(request_id, artifacts={'development': handle, 'unchanged': public_unchanged,
                'alternatives': record['alternatives'], 'no_addition': silent}, coverage=copy.deepcopy(COVERAGE),
                change_summary={'sections': len(sections), 'changed_endpoints': len(choices), 'notes_per_alternative': len(expected['notes']),
                                'unchanged_materials_total': len(unchanged['materials']),
                                'unchanged_materials_omitted': max(0, len(unchanged['materials']) - 8)},
                uncertainty=['Long duration is explicit sparse placement, not performance readiness or perceptual musical development.'])
            if len(canonical_bytes(result)) > 16384:
                raise PocketError('Arrangement public receipt exceeds 16 KiB')
            _verify_handles(result, store_root)
            return result
        except BaseException as error:
            if not stages:
                raise
            try:
                failure = put_record({'schema': 'pocket.arrangement-development-failure/v1', 'complete': False,
                'valid_alternatives': [], 'completed_public_steps': stages, 'attempting': attempting,
                'reason': str(error)[:1500], 'interruption_kind': type(error).__name__,
                'recovery': 'Inspect journals and retained artifacts; failed requests cannot automatically retry.'}, store_root)
            except (OSError, PocketError):
                raise error
            if not isinstance(error, Exception):
                error.add_note('Incomplete arrangement evidence: ' + failure['artifact_uri'])
                raise
            raise PocketError('Arrangement incomplete; no valid alternatives advertised; evidence ' + failure['artifact_uri'] + ': ' + str(error)[:1000]) from error
    result = run_request(store_root, request_id, 'midi_arrangement_develop', {'definition': supplied}, work)
    _verify_report(read_record(result['artifacts']['development'], store_root, SCHEMA), store_root)
    return result


def midi_arrangement_query(*, store_root: str, development: ArtifactHandle,
                           view: Literal['summary', 'sections', 'changes'] = 'summary', limit: int = 32,
                           cursor: str | None = None, max_bytes: int = 16384) -> dict:
    """Read bounded declared sections and revalidated source/lock proofs."""
    integer(limit, 'limit', 1, 128)
    integer(max_bytes, 'max_bytes', 4096, 65536)
    if view not in ('summary', 'sections', 'changes'):
        raise PocketError('Unknown arrangement view')
    _verify_handles(development, store_root)
    record = _verify_report(read_record(development, store_root, SCHEMA), store_root)
    rows = record[view] if view != 'summary' else [{'label': record['definition']['label'],
        'length_qn': record['definition']['length_qn'], 'sections': len(record['sections']),
        'changes': len(record['changes']), 'evidence_budget': record['evidence_budget']}]
    identity, offset = digest([development, view]), 0
    if cursor is not None:
        if not isinstance(cursor, str) or len(cursor) > 80 or not cursor.startswith(identity + ':'):
            raise PocketError('Stale or malformed arrangement cursor')
        try:
            offset = int(cursor[len(identity) + 1:])
        except ValueError as error:
            raise PocketError('Malformed arrangement cursor') from error
        if cursor != f'{identity}:{offset}' or not 0 <= offset <= len(rows):
            raise PocketError('Invalid arrangement cursor offset')
    selected = rows[offset:offset + limit]
    while True:
        following = offset + len(selected)
        result = receipt(artifacts={'development': development}, coverage=copy.deepcopy(COVERAGE),
                         view=view, items=selected, total=len(rows), complete=following == len(rows),
                         next_cursor=f'{identity}:{following}' if following < len(rows) else None, omitted=len(rows) - following)
        if len(canonical_bytes(result)) <= max_bytes:
            return result
        if not selected:
            raise PocketError('Arrangement query budget cannot fit metadata')
        selected = selected[:-1]
        if not selected:
            return receipt(status='needs_input', artifacts={'development': development}, coverage=copy.deepcopy(COVERAGE),
                           view=view, items=[], total=len(rows), complete=False, omitted=len(rows) - offset,
                           next_cursor=f'{identity}:{offset}', next_actions=['Increase max_bytes or inspect the immutable artifact'])
