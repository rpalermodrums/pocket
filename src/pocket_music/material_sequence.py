"""Construct explicit ordinary-note clip occurrences without inferring clocks.

This is a file transformation, not a native arrangement writer or scheduler.
Every parent remains retained, including source clip origins and declared rests.
"""
from __future__ import annotations

import copy

from .artifact_store import (
    _verify_handles,
    canonical_bytes,
    digest,
    put_record,
    receipt,
    run_request,
)
from .errors import PocketError
from .material import identifier, integer, load_material, new_material, qn, rational
from .sequence_types import SequenceDefinition
from .time_maps import _context as validate_time_context

SCHEMA = 'pocket.material-sequence/v1'
CLIP_FIELDS = {'id', 'track_id', 'origin', 'length_qn', 'loop', 'note_ids', 'event_ids', 'curve_ids'}
COVERAGE = {
    'profile': 'canonical_ordinary_note_whole_clips/v1',
    'source_origins': 'replaced_by_explicit_destination_clip_local_placements',
    'time': 'exact_rational_translation_no_tempo_conversion',
    'source_materials': 'whole_immutable_parents_retained',
    'controllers': 'sources_with_events_rejected',
    'expression': 'sources_with_curves_or_note_bindings_rejected',
    'voice_identity': 'namespaced_by_occurrence',
    'role_identity': 'preserved_as_authored_not_inferred',
    'native_execution': False,
    'human_listening': 'not_established',
}


def _fields(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise PocketError(f'{label} has missing or unexpected fields')


def _text(value, label, maximum=240):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise PocketError(f'{label} requires bounded nonempty text')
    return value


def _list(value, label, minimum, maximum):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise PocketError(f'{label} requires {minimum}–{maximum} entries')
    return value


def _q(value, label):
    _fields(value, {'n', 'd'}, label)
    integer(value['n'], label + ' numerator', -(2**53), 2**53)
    integer(value['d'], label + ' denominator', 1, 2**53)
    return rational(value, label)


def _input_q(value, label):
    if isinstance(value, int) and not isinstance(value, bool):
        integer(value, label, -(2**53), 2**53)
        return rational(value, label)
    return _q(value, label)


def _position(value, label, spaces):
    _fields(value, {'space', 'n', 'd'}, label)
    if not isinstance(value['space'], str) or value['space'] not in spaces:
        raise PocketError(f'{label} has an unsupported coordinate space')
    return _q({key: value[key] for key in ('n', 'd')}, label)


def _ordinary(record):
    if record['coverage'].get('editing_allowed') is not True:
        raise PocketError('Sequence sources require explicitly editable canonical material')
    if record['events'] or record['curves']:
        raise PocketError('Sequence source events/controllers and expression curves are unsupported')
    if record['tempo_map_ref'] is not None or record['meter_map_ref'] is not None:
        raise PocketError('Sequence sources with tempo/meter maps require a separately qualified clock conversion')
    if any(source['kind'] != 'material_sequence_source' for source in record['sources']):
        raise PocketError('Sequence source has unsupported raw/native source semantics; retain it without projection')
    notes = {note['id']: note for note in record['notes']}
    for note in record['notes']:
        if note['source_binding'] is not None or note['expression_refs']:
            raise PocketError('Sequence sources with opaque or wire note bindings or expression are unsupported')
        _text(note['voice_id'], 'source voice identity')
    for clip in record['clips']:
        _fields(clip, CLIP_FIELDS, 'source clip')
        _position(clip['origin'], 'source clip origin', {'phrase_qn', 'arrangement_qn'})
        length = _q(clip['length_qn'], 'source clip length')
        for note_id in clip['note_ids']:
            note = notes[note_id]
            start = _position(note['onset'], 'source note onset', {'clip_qn'})
            duration = _q(note['duration_qn'], 'source note duration')
            if start < 0 or start + duration > length:
                raise PocketError('Whole-clip sequence requires every source gate inside its clip bounds')


def _prepare(definition, store_root):
    _fields(definition, {'label', 'materials', 'clock', 'origin', 'length_qn', 'occurrences',
                         'controller_policy', 'expression_policy', 'overlap_policy'}, 'sequence definition')
    _text(definition['label'], 'sequence label')
    _fields(definition['clock'], {'schema', 'context_id', 'attribution'} |
            ({'source'} if isinstance(definition['clock'], dict) and 'source' in definition['clock'] else set()),
            'declared sequence clock')
    validate_time_context(definition['clock'], store_root)
    _verify_handles(definition['clock'], store_root)
    _position(definition['origin'], 'sequence origin', {'phrase_qn', 'arrangement_qn'})
    length = _input_q(definition['length_qn'], 'sequence length')
    if length <= 0:
        raise PocketError('Sequence length must be positive')
    for name, policy in (('controller_policy', 'reject_present'), ('expression_policy', 'reject_present'),
                         ('overlap_policy', 'reject_same_channel_pitch')):
        if definition[name] != policy:
            raise PocketError(f'Unsupported sequence {name}')
    materials = {}
    normalized = copy.deepcopy(definition)
    normalized['materials'] = []
    normalized['length_qn'] = qn(length)
    normalized['occurrences'] = []
    total_notes = 0
    for binding in _list(definition['materials'], 'sequence materials', 1, 32):
        _fields(binding, {'key', 'material'}, 'sequence material binding')
        key = _text(binding['key'], 'sequence material key')
        if key in materials:
            raise PocketError('Duplicate sequence material key')
        record = load_material(binding['material'], store_root)
        total_notes += len(record['notes'])
        if total_notes > 32768:
            raise PocketError('Sequence source inspection exceeds 32768 notes')
        _ordinary(record)
        sha = digest(record)
        handle = {'schema': 'pocket.artifact-handle/v1', 'artifact_uri': f'artifacts/{sha}/record.json',
                  'sha256': sha, 'artifact_schema': record['schema']}
        materials[key] = (record, handle)
        normalized['materials'].append({'key': key, 'material': handle})
    occurrences = _list(definition['occurrences'], 'sequence occurrences', 1, 256)
    seen, used = set(), set()
    count = 0
    for occurrence in occurrences:
        _fields(occurrence, {'occurrence_id', 'material_key', 'material_revision', 'clip_id', 'at_qn'},
                'sequence occurrence')
        occurrence_id = _text(occurrence['occurrence_id'], 'sequence occurrence identity')
        if occurrence_id in seen:
            raise PocketError('Duplicate sequence occurrence identity')
        seen.add(occurrence_id)
        key = _text(occurrence['material_key'], 'occurrence material key')
        if key not in materials:
            raise PocketError('Unresolved occurrence material key')
        used.add(key)
        record = materials[key][0]
        if occurrence['material_revision'] != record['revision_sha256']:
            raise PocketError('Stale occurrence material revision')
        clip_id = _text(occurrence['clip_id'], 'occurrence clip identity')
        clip = next((clip for clip in record['clips'] if clip['id'] == clip_id), None)
        if clip is None:
            raise PocketError('Unresolved occurrence clip identity')
        at = _input_q(occurrence['at_qn'], 'occurrence placement')
        if at < 0 or at + rational(clip['length_qn']) > length:
            raise PocketError('Whole source clip including rests must fit the sequence')
        count += len(clip['note_ids'])
        if count > 8192:
            raise PocketError('Sequence output exceeds 8192 notes')
        normalized['occurrences'].append({**copy.deepcopy(occurrence), 'at_qn': qn(at)})
    if used != set(materials):
        raise PocketError('Unused sequence material binding')
    return normalized, materials


def _construct(normalized, materials):
    seed = digest(normalized)
    notes, proof, occurrences = [], [], []
    track_id, clip_id = identifier('track', seed), identifier('clip', seed)
    for occurrence in normalized['occurrences']:
        parent, handle = materials[occurrence['material_key']]
        source_clip = next(clip for clip in parent['clips'] if clip['id'] == occurrence['clip_id'])
        by_id = {note['id']: note for note in parent['notes']}
        at = rational(occurrence['at_qn'])
        occurrences.append({**copy.deepcopy(occurrence), 'material': handle,
                            'source_origin': copy.deepcopy(source_clip['origin']),
                            'source_length_qn': copy.deepcopy(source_clip['length_qn']),
                            'note_count': len(source_clip['note_ids'])})
        for source_id in source_clip['note_ids']:
            source = by_id[source_id]
            note = copy.deepcopy(source)
            identity = [seed, occurrence['occurrence_id'], parent['revision_sha256'], source_id]
            note['id'] = identifier('note', digest(identity))
            note['voice_id'] = identifier('voice', digest([seed, occurrence['occurrence_id'], source['voice_id']]))
            moved = qn(at + rational(source['onset']))
            _q(moved, 'constructed note onset')
            note['onset'] = {'space': 'clip_qn', **moved}
            note['derived_from'] = [source_id]
            notes.append(note)
            proof.append({'note_id': note['id'], 'source_note_id': source_id, 'material': handle,
                          'material_revision': parent['revision_sha256'], 'source_clip_id': source_clip['id'],
                          'occurrence_id': occurrence['occurrence_id'], 'source_voice_id': source['voice_id'],
                          'voice_id': note['voice_id']})
    # Half-open gates; endpoint contact is permitted, including across occurrences.
    active = {}
    for note in sorted(notes, key=lambda note: (rational(note['onset']), note['id'])):
        key = (note['channel'], note['pitch']['midi_note'])
        start = rational(note['onset'])
        if key in active and start < active[key]:
            raise PocketError('Sequence introduces ambiguous same-channel/same-pitch overlapping gates')
        active[key] = start + rational(note['duration_qn'])
    child = new_material(seed, tracks=[{'id': track_id, 'name': normalized['label']}],
        clips=[{'id': clip_id, 'track_id': track_id, 'origin': copy.deepcopy(normalized['origin']),
                'length_qn': copy.deepcopy(normalized['length_qn']), 'loop': False,
                'note_ids': [note['id'] for note in notes], 'event_ids': [], 'curve_ids': []}], notes=notes,
        sources=[{'kind': 'material_sequence_source', 'key': key, 'material': handle}
                 for key, (_, handle) in materials.items()],
        coverage={'editing_allowed': True, 'issues': [], **copy.deepcopy(COVERAGE)},
        provenance={'provider': 'material-sequence-v1', 'clock': copy.deepcopy(normalized['clock']),
                    'sequence_definition_sha256': seed})
    return child, occurrences, proof


def material_sequence(*, store_root: str, request_id: str, definition: SequenceDefinition) -> dict:
    """Place whole ordinary-note clips in an explicit new declared sequence.

    All input materials are retained. This narrow profile refuses raw events,
    curves, opaque note bindings, source maps and unknown clip semantics.
    Source clip origins are replaced, never interpreted as a shared clock.
    """
    if len(canonical_bytes(definition)) > 8 * 1024 * 1024:
        raise PocketError('Sequence definition exceeds 8 MiB')
    supplied = copy.deepcopy(definition)

    def work():
        normalized, materials = _prepare(supplied, store_root)
        child, occurrences, proof = _construct(normalized, materials)
        # Publish no result until all source, range and lifecycle checks have passed.
        for record, _ in materials.values():
            put_record(record, store_root)
        material_handle = put_record(child, store_root)
        sequence = {'schema': SCHEMA, 'definition': normalized, 'material': material_handle,
                    'occurrences': occurrences, 'note_derivations': proof, 'coverage': copy.deepcopy(COVERAGE),
                    'invariants': {'parents': 'unchanged', 'note_fields_except_id_onset_voice_derivation': 'exact',
                                   'whole_clip_lengths_and_rests': 'retained', 'same_pitch_overlaps': 'absent'}}
        sequence_handle = put_record(sequence, store_root)
        return receipt(request_id, artifacts={'material': material_handle, 'sequence': sequence_handle},
                       change_summary={'source_materials': len(materials), 'occurrences': len(occurrences),
                                       'notes': len(child['notes']), 'destination_clips': 1},
                       coverage=copy.deepcopy(COVERAGE),
                       uncertainty=['Declared placements do not establish audio alignment, native playback or musical usefulness.'])

    result = run_request(store_root, request_id, 'material_sequence', {'definition': supplied}, work)
    load_material(result['artifacts']['material'], store_root)
    return result
