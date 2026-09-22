"""Bounded motif choices composed from the same public sequence and edit tools.

The source clip and every realized choice are retained. This optional composer
does not supply another note editor, infer motif identity or schedule a host.
"""
from __future__ import annotations

import copy
from fractions import Fraction
from itertools import pairwise

from .artifact_store import (
    _verify_handles,
    canonical_bytes,
    digest,
    put_record,
    read_record,
    receipt,
    run_request,
)
from .develop_types import DevelopmentDefinition
from .errors import PocketError
from .material import integer, load_material, material_query, qn, rational
from .material_sequence import material_sequence
from .midi_edit import midi_transform

SCHEMA = 'pocket.motif-development/v1'
COVERAGE = {'provider': 'motif-development-v1', 'choices': 'explicit_domains_content_hash_ranked',
            'source_profile': 'canonical_ordinary_note_whole_clips/v1',
            'musical_identity': 'symbolic_proofs_not_perceptual_inference',
            'native_execution': False, 'listening': 'not_performed',
            'tuning_conversion': 'not_performed'}


def _fields(value, names, label):
    if not isinstance(value, dict) or set(value) != set(names):
        raise PocketError(f'{label} has missing or unexpected fields')


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 240:
        raise PocketError(f'{label} requires nonempty text within 240 characters')
    return value


def _list(value, minimum, maximum, label):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise PocketError(f'{label} requires {minimum}–{maximum} entries')
    return value


def _q(value, label):
    if isinstance(value, int) and not isinstance(value, bool):
        integer(value, label, -(2**53), 2**53)
        return rational(value)
    _fields(value, {'n', 'd'}, label)
    integer(value['n'], label + ' numerator', -(2**53), 2**53)
    integer(value['d'], label + ' denominator', 1, 2**53)
    return rational(value, label)


def _prepare(definition, store_root):
    _fields(definition, {'label', 'seed', 'clock', 'origin', 'length_qn', 'occurrences',
                         'locked_occurrence_id', 'endpoint_note_id', 'eligible_occurrence_ids',
                         'variation', 'seeds'}, 'development definition')
    _text(definition['label'], 'development label')
    _fields(definition['seed'], {'material', 'material_revision', 'clip_id'}, 'development seed')
    seed = load_material(definition['seed']['material'], store_root)
    if definition['seed']['material_revision'] != seed['revision_sha256']:
        raise PocketError('Stale development seed revision')
    clip_id = _text(definition['seed']['clip_id'], 'seed clip identity')
    clip = next((row for row in seed['clips'] if row['id'] == clip_id), None)
    if clip is None:
        raise PocketError('Unknown development seed clip')
    endpoint_id = _text(definition['endpoint_note_id'], 'endpoint note identity')
    notes = {row['id']: row for row in seed['notes'] if row['id'] in clip['note_ids']}
    if endpoint_id not in notes:
        raise PocketError('Endpoint note must belong to the exact seed clip')
    endpoint = notes[endpoint_id]
    if rational(endpoint['onset']) != max(rational(note['onset']) for note in notes.values()):
        raise PocketError('Declared endpoint must be a latest-onset note in the seed clip')
    _fields(definition['seeds'], {'structure', 'pitch', 'timing'}, 'development seeds')
    for value in definition['seeds'].values():
        integer(value, 'development seed integer', -(2**53), 2**53)
    variation = definition['variation']
    _fields(variation, {'pitch_offsets_semitones', 'timing_offsets_qn', 'max_changed_notes',
                        'pitch_min', 'pitch_max'}, 'development variation')
    pitches = _list(variation['pitch_offsets_semitones'], 1, 16, 'pitch offsets')
    for pitch in pitches:
        integer(pitch, 'pitch offset', -127, 127)
    times = [_q(value, 'timing offset') for value in _list(variation['timing_offsets_qn'], 1, 16, 'timing offsets')]
    if len(set(pitches)) != len(pitches) or len(set(times)) != len(times):
        raise PocketError('Development offset domains require unique values')
    if any(abs(value) > 4096 for value in times):
        raise PocketError('Development timing offsets exceed 4096 quarter notes')
    maximum = integer(variation['max_changed_notes'], 'maximum changed notes', 1, 32)
    low = integer(variation['pitch_min'], 'minimum pitch', 0, 127)
    high = integer(variation['pitch_max'], 'maximum pitch', 0, 127)
    if low > high or any(not low <= note['pitch']['midi_note'] <= high for note in notes.values()):
        raise PocketError('Seed notes do not satisfy the explicit pitch bounds')
    length = _q(definition['length_qn'], 'development length')
    if not 0 < length <= 4096:
        raise PocketError('Development length requires 0–4096 quarter notes')
    occurrences, by_id = [], {}
    for item in _list(definition['occurrences'], 2, 64, 'development occurrences'):
        _fields(item, {'occurrence_id', 'at_qn'}, 'development occurrence')
        occurrence_id = _text(item['occurrence_id'], 'occurrence identity')
        if occurrence_id in by_id:
            raise PocketError('Duplicate development occurrence identity')
        at = _q(item['at_qn'], 'occurrence placement')
        by_id[occurrence_id] = at
        occurrences.append({'occurrence_id': occurrence_id, 'at_qn': qn(at)})
    output_notes = len(notes) * len(occurrences)
    if output_notes > 1024:
        raise PocketError('Development output exceeds 1024 notes')
    locked = _text(definition['locked_occurrence_id'], 'locked occurrence identity')
    if locked not in by_id or occurrences[0]['occurrence_id'] != locked or by_id[locked] != min(by_id.values()):
        raise PocketError('First earliest occurrence must be explicitly locked')
    eligible = _list(definition['eligible_occurrence_ids'], 1, 32, 'eligible occurrences')
    for key in eligible:
        _text(key, 'eligible occurrence identity')
    if len(set(eligible)) != len(eligible) or any(key not in by_id or by_id[key] <= by_id[locked] for key in eligible):
        raise PocketError('Eligible occurrence identities must be unique and strictly later than the locked seed')
    feasible = [(pitch, time) for pitch in pitches for time in times if (pitch != 0 or time != 0)
                and low <= endpoint['pitch']['midi_note'] + pitch <= high
                and 0 <= rational(endpoint['onset']) + time
                and rational(endpoint['onset']) + time + rational(endpoint['duration_qn']) <= rational(clip['length_qn'])]
    if not feasible:
        raise PocketError('No nonzero endpoint choice satisfies pitch and whole-clip gate bounds')
    selected = sorted(eligible, key=lambda key: digest([definition['seeds']['structure'], key]))[:maximum]
    deletion_notes = sum(max(0, output_notes - offset) for offset in range(256, output_notes + 256, 256))
    evidence_note_copies = output_notes * (len(selected) + 1) + deletion_notes + 2 * len(seed['notes'])
    if evidence_note_copies > 10000:
        raise PocketError('Development evidence exceeds 10000 material-note copies: '
                          'output_notes*(changed_endpoints+1) + sum(remaining_notes_after_each_256_note_delete) '
                          '+ 2*all_source_notes; narrow occurrences or changes')
    choices = []
    for key in selected:
        pitch, time = min(feasible, key=lambda pair: (
            digest([definition['seeds']['pitch'], key, pair[0]]),
            digest([definition['seeds']['timing'], key, qn(pair[1])])) )
        choices.append({'occurrence_id': key, 'pitch_offset_semitones': pitch, 'timing_offset_qn': qn(time)})
    normalized = copy.deepcopy(definition)
    normalized['length_qn'] = qn(length)
    normalized['occurrences'] = occurrences
    normalized['variation']['timing_offsets_qn'] = [qn(value) for value in times]
    return seed, clip, normalized, choices, evidence_note_copies


def _identity_proofs(seed, clip, sequence, alternative):
    source = {note['id']: note for note in seed['notes']}
    result = {note['id']: note for note in alternative['notes']}
    rows = []
    for occurrence in sequence['occurrences']:
        mapping = [row for row in sequence['note_derivations'] if row['occurrence_id'] == occurrence['occurrence_id']]
        pairs = [(source[row['source_note_id']], result[row['note_id']]) for row in mapping]
        at = rational(occurrence['at_qn'])
        rhythm = all(rational(child['onset']) - at == rational(parent['onset']) for parent, child in pairs)
        pitches = all(child['pitch'] == parent['pitch'] for parent, child in pairs)
        # Interval identity is a symbolic claim only for the explicit ordered, conventional tuning profile.
        tuned = all(note['pitch']['tuning_ref'] == 'tuning:12tet-a440' for pair in pairs for note in pair)
        intervals = 'abstained_unknown_tuning'
        if tuned:
            before = [Fraction(note['pitch']['midi_note'] * 100) + Fraction(str(note['pitch']['cents_offset']))
                      for note, _ in pairs]
            after = [Fraction(note['pitch']['midi_note'] * 100) + Fraction(str(note['pitch']['cents_offset']))
                     for _, note in pairs]
            intervals = 'exact' if [b - a for a, b in pairwise(before)] == [b - a for a, b in pairwise(after)] else 'changed'
        rows.append({'occurrence_id': occurrence['occurrence_id'], 'source_clip_id': clip['id'],
                     'rhythm_onsets': 'exact' if rhythm else 'changed', 'pitches': 'exact' if pitches else 'changed',
                     'declared_note_order_pitch_intervals': intervals, 'gates': 'exact',
                     'attack_and_release_accents': 'exact', 'perceptual_similarity': 'not_inferred'})
    return rows


def midi_develop(*, store_root: str, request_id: str, definition: DevelopmentDefinition) -> dict:
    """Freeze literal and bounded endpoint-development alternatives through public tools.

    The first occurrence stays locked. Eligibility, pitches and timing domains
    are explicit; no model, DAW, sound or hidden conversation state is needed.
    """
    if len(canonical_bytes(definition)) > 8 * 1024 * 1024:
        raise PocketError('Development definition exceeds 8 MiB')
    supplied = copy.deepcopy(definition)
    stages = []
    attempting = []

    def compose():
        seed, clip, normalized, choices, evidence_note_copies = _prepare(supplied, store_root)

        def request(stage):
            return 'develop-' + digest([request_id, stage])[:48]

        def invoke(name, provider, args):
            retained = {key: value for key, value in args.items() if key != 'store_root'}
            attempting[:] = [{'tool': name, 'arguments': retained}]
            result = provider(**args)
            stages.append({'tool': name, 'arguments': retained, 'result': result})
            attempting.clear()
            return result

        sequence_definition = {
            'label': normalized['label'], 'materials': [{'key': 'seed', 'material': seed}],
            'clock': normalized['clock'], 'origin': normalized['origin'], 'length_qn': normalized['length_qn'],
            'occurrences': [{**row, 'material_key': 'seed', 'material_revision': seed['revision_sha256'],
                             'clip_id': clip['id']} for row in normalized['occurrences']],
            'controller_policy': 'reject_present', 'expression_policy': 'reject_present',
            'overlap_policy': 'reject_same_channel_pitch'}
        args = {'store_root': store_root, 'request_id': request('sequence'), 'definition': sequence_definition}
        a = invoke('material_sequence', material_sequence, args)
        sequence = read_record(a['artifacts']['sequence'], store_root)
        seed_handle = sequence['definition']['materials'][0]['material']
        normalized['seed']['material'] = seed_handle
        current = a['artifacts']['material']
        for index, choice in enumerate(choices):
            mapping = next(row for row in sequence['note_derivations']
                           if row['occurrence_id'] == choice['occurrence_id']
                           and row['source_note_id'] == normalized['endpoint_note_id'])
            selection = material_query(current, store_root, selection={'note_ids': [mapping['note_id']]})['selection']
            operations = []
            if choice['pitch_offset_semitones']:
                operations.append({'op': 'transpose', 'semitones': choice['pitch_offset_semitones']})
            if rational(choice['timing_offset_qn']):
                operations.append({'op': 'shift', 'delta_qn': choice['timing_offset_qn']})
            args = {'material': current, 'selection': selection, 'operations': operations,
                    'store_root': store_root, 'request_id': request(f'variation-{index}'),
                    'locks': {'outside_selection': 'all', 'selected_fields': [
                        'duration_qn', 'velocity', 'release_velocity', 'channel', 'mute', 'expression_refs',
                        'voice_id', 'role_ref', 'source_binding', 'derived_from']}}
            changed = invoke('midi_transform', midi_transform, args)
            current = changed['artifacts']['material']
        first = load_material(a['artifacts']['material'], store_root)
        second = load_material(current, store_root)
        mapped_changes = {row['note_id'] for row in sequence['note_derivations']
                          if row['source_note_id'] == normalized['endpoint_note_id']
                          and row['occurrence_id'] in {choice['occurrence_id'] for choice in choices}}
        actual_changes = {before['id'] for before, after in zip(first['notes'], second['notes'], strict=True) if before != after}
        if actual_changes != mapped_changes:
            raise PocketError('Development changed-note identity proof failed')
        no_addition_material = a['artifacts']['material']
        note_ids = [note['id'] for note in first['notes']]
        for offset in range(0, len(note_ids), 256):
            selection = material_query(no_addition_material, store_root,
                selection={'note_ids': note_ids[offset:offset + 256]}, max_bytes=65536)['selection']
            if selection is None:
                raise PocketError('Development no-addition selection exceeded the public query budget')
            args = {'material': no_addition_material, 'selection': selection,
                    'operations': [{'op': 'delete', 'controller_timeline': 'preserve_existing'}],
                    'store_root': store_root, 'request_id': request(f'no-addition-{offset}')}
            no_addition = invoke('midi_transform', midi_transform, args)
            no_addition_material = no_addition['artifacts']['material']
        silent = load_material(no_addition['artifacts']['material'], store_root)
        if silent['notes'] or silent['events'] or silent['curves']:
            raise PocketError('No-addition proof failed')
        report = {'schema': SCHEMA, 'definition': normalized, 'choices': choices,
                  'unchanged_seed': seed_handle, 'alternatives': [a['artifacts']['material'], current],
                  'no_addition': no_addition['artifacts']['material'], 'sequence': a['artifacts']['sequence'],
                  'identity_proofs': {'a': _identity_proofs(seed, clip, sequence, first),
                                      'b': _identity_proofs(seed, clip, sequence, second)},
                  'public_steps': stages, 'step_environment': 'caller supplies store_root',
                  'coverage': copy.deepcopy(COVERAGE),
                  'constraint_result': {'status': 'passed', 'locked_occurrence': normalized['locked_occurrence_id'],
                                        'changed_notes': len(actual_changes), 'maximum': normalized['variation']['max_changed_notes'],
                                        'outside_eligible_endpoints': 'exact', 'gates_and_accents': 'exact'},
                  'evidence_budget': {'material_note_copies': evidence_note_copies, 'maximum': 10000,
                      'formula': 'output_notes*(changed_endpoints+1) + sum(remaining_notes_after_each_256_note_delete) + 2*all_source_notes',
                      'purpose': 'Conservative bounded composition proof; shared artifact graph limits remain unchanged.'}}
        handle = put_record(report, store_root)
        return receipt(request_id, artifacts={'development': handle, 'unchanged': seed_handle,
                       'alternatives': report['alternatives'], 'no_addition': report['no_addition']},
                       change_summary={'alternatives': 2, 'occurrences': len(normalized['occurrences']),
                                       'notes_per_alternative': len(first['notes']), 'changed_endpoint_notes': len(actual_changes)},
                       coverage=copy.deepcopy(COVERAGE),
                       uncertainty=['Source origins are replaced by declared placements. Unchanged identifies the exact seed; no addition is destination silence.',
                                    'Symbolic identity and constrained alternatives do not establish musical usefulness.'])

    def work():
        try:
            result = compose()
            _verify_handles(result, store_root)
            return result
        except BaseException as error:
            if not stages:
                raise
            failed = {'schema': 'pocket.motif-development-failure/v1', 'complete': False,
                      'valid_alternatives': [], 'completed_public_steps': stages,
                      'attempting': attempting, 'step_environment': 'caller supplies store_root',
                      'reason': str(error)[:1500], 'interruption_kind': type(error).__name__,
                      'recovery': 'Inspect retained top-level and subordinate request journals; do not replay failed requests.'}
            try:
                retained = put_record(failed, store_root)
            except (OSError, PocketError):
                raise error
            if not isinstance(error, Exception):
                error.add_note(f'Incomplete development evidence: {retained["artifact_uri"]}; no valid alternatives advertised.')
                raise
            raise PocketError(f'Development incomplete; completed subordinate evidence retained at '
                              f'{retained["artifact_uri"]}; no valid alternatives advertised: {str(error)[:1000]}') from error

    return run_request(store_root, request_id, 'midi_develop', {'definition': supplied}, work)
