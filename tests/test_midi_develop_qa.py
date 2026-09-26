# SPDX-License-Identifier: AGPL-3.0-only
"""Independent endpoint-development constraints and public-step replay."""
from __future__ import annotations

import copy
import json
from fractions import Fraction

import pytest
from test_midi_qa import seal_literal
from test_midi_relationships_qa import material, note, q

from pocket_music.artifact_store import put_record, read_record
from pocket_music.errors import PocketError
from pocket_music.material_sequence import material_sequence
from pocket_music.midi_develop import midi_develop
from pocket_music.midi_edit import midi_transform


def definition():
    record = material([note('opening', 0, 1, 60, velocity=79, release=29),
                       note('endpoint', 3, 1, 64, velocity=91, release=37)], length=4)
    return {'label': 'Declared later endpoint',
            'seed': {'material': record, 'material_revision': record['revision_sha256'],
                     'clip_id': record['clips'][0]['id']},
            'clock': {'schema': 'pocket.time-context/v1', 'context_id': 'clock:explicit',
                      'attribution': 'Independent fixture author supplies every placement and domain.'},
            'origin': {'space': 'phrase_qn', **q(0)}, 'length_qn': 12,
            'occurrences': [{'occurrence_id': 'first', 'at_qn': 0},
                            {'occurrence_id': 'middle', 'at_qn': 4},
                            {'occurrence_id': 'last', 'at_qn': 8}],
            'locked_occurrence_id': 'first', 'endpoint_note_id': 'endpoint',
            'eligible_occurrence_ids': ['last'],
            'variation': {'pitch_offsets_semitones': [2], 'timing_offsets_qn': [0],
                          'max_changed_notes': 1, 'pitch_min': 48, 'pitch_max': 80},
            'seeds': {'structure': 12, 'pitch': 34, 'timing': 56}}


def run(tmp_path, value=None, request='develop'):
    return midi_develop(store_root=str(tmp_path / 'store'), request_id=request,
                        definition=definition() if value is None else value)


def test_qa_develop_exact_endpoint_change_unchanged_and_no_addition(tmp_path):
    value = definition()
    before = copy.deepcopy(value)
    result = run(tmp_path, value)
    store = str(tmp_path / 'store')
    proof = read_record(result['artifacts']['development'], store)
    baseline, varied = [read_record(handle, store) for handle in result['artifacts']['alternatives']]
    assert len(baseline['notes']) == len(varied['notes']) == 6
    assert [n['pitch']['midi_note'] for n in baseline['notes']] == [60, 64, 60, 64, 60, 64]
    assert [n['pitch']['midi_note'] for n in varied['notes']] == [60, 64, 60, 64, 60, 66]
    assert [n['onset'] for n in baseline['notes']] == [{'space': 'clip_qn', **q(t)} for t in [0, 3, 4, 7, 8, 11]]
    assert varied['notes'][:5] == baseline['notes'][:5]
    for field in set(baseline['notes'][5]) - {'pitch'}:
        assert varied['notes'][5][field] == baseline['notes'][5][field]
    assert read_record(result['artifacts']['unchanged'], store) == value['seed']['material']
    empty = read_record(result['artifacts']['no_addition'], store)
    assert empty['notes'] == empty['events'] == empty['curves'] == []
    assert empty['clips'][0]['length_qn'] == q(12)
    assert proof['constraint_result']['changed_notes'] == 1
    assert proof['constraint_result']['outside_eligible_endpoints'] == 'exact'
    assert proof['identity_proofs']['b'][0]['pitches'] == 'exact'
    assert proof['identity_proofs']['b'][2]['pitches'] == 'changed'
    assert proof['identity_proofs']['b'][2]['rhythm_onsets'] == 'exact'
    assert value == before
    assert result['coverage']['native_execution'] is False and result['coverage']['listening'] == 'not_performed'


def test_qa_develop_public_steps_replay_and_inline_handle_determinism(tmp_path):
    value = definition()
    result = run(tmp_path, value)
    store = str(tmp_path / 'store')
    proof = read_record(result['artifacts']['development'], store)
    providers = {'material_sequence': material_sequence, 'midi_transform': midi_transform}
    for stage in proof['public_steps']:
        assert 'store_root' not in stage['arguments']
        args = {**stage['arguments'], 'store_root': store}
        assert providers[stage['tool']](**args) == stage['result']
    handles = copy.deepcopy(value)
    handles['seed']['material'] = put_record(value['seed']['material'], store)
    second = run(tmp_path, handles, 'handle-seed')
    assert second['artifacts']['alternatives'] == result['artifacts']['alternatives']
    assert second['artifacts']['unchanged'] == result['artifacts']['unchanged']
    assert second['artifacts']['no_addition'] == result['artifacts']['no_addition']
    assert run(tmp_path, value) == result


@pytest.mark.parametrize('path,replacement', [
    (('seed', 'material_revision'), '0' * 64), (('seed', 'clip_id'), 'missing'),
    (('endpoint_note_id',), 'opening'), (('endpoint_note_id',), 'missing'),
    (('locked_occurrence_id',), 'middle'), (('eligible_occurrence_ids',), ['first']),
    (('eligible_occurrence_ids',), ['last', 'last']), (('eligible_occurrence_ids',), ['missing']),
    (('variation', 'pitch_offsets_semitones'), [True]), (('variation', 'pitch_offsets_semitones'), [0]),
    (('variation', 'pitch_offsets_semitones'), [2, 2]),
    (('variation', 'timing_offsets_qn'), [1.0]),
    (('variation', 'timing_offsets_qn'), [{'n': 2, 'd': 4}]),
    (('variation', 'timing_offsets_qn'), [1]),
    (('variation', 'max_changed_notes'), True), (('variation', 'pitch_max'), 61),
    (('seeds', 'pitch'), 2.0), (('length_qn',), 4097),
])
def test_qa_develop_malformed_stale_lock_and_infeasible_choice_refuse(tmp_path, path, replacement):
    value = definition()
    target = value
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement
    with pytest.raises(PocketError):
        run(tmp_path, value)


def test_qa_develop_variant_cannot_bypass_primitive_overlap_refusal(tmp_path):
    value = definition()
    record = value['seed']['material']
    record['notes'].append(note('protected', 3, 1, 66))
    record['clips'][0]['note_ids'].append('protected')
    record = seal_literal(record)
    value['seed']['material'] = record
    value['seed']['material_revision'] = record['revision_sha256']
    with pytest.raises(PocketError, match='overlap'):
        run(tmp_path, value)
    journal = json.loads((tmp_path / 'store' / 'requests' / 'develop' / 'journal.json').read_text())
    assert journal['state'] == 'failed' and 'receipt' not in journal


def test_qa_develop_domain_changes_only_declared_endpoint_and_budget(tmp_path):
    value = definition()
    value['eligible_occurrence_ids'] = ['middle', 'last']
    value['variation']['pitch_offsets_semitones'] = [-2, 0, 2]
    value['variation']['timing_offsets_qn'] = [q(-1), 0]
    result = run(tmp_path, value)
    store = str(tmp_path / 'store')
    baseline, varied = [read_record(handle, store) for handle in result['artifacts']['alternatives']]
    pairs = [(old, new) for old, new in zip(baseline['notes'], varied['notes'], strict=True) if old != new]
    assert len(pairs) == 1
    old, new = pairs[0]
    assert old['derived_from'] == new['derived_from'] == ['endpoint']
    assert old['onset'] in [{'space': 'clip_qn', **q(7)}, {'space': 'clip_qn', **q(11)}]
    assert new['pitch']['midi_note'] - old['pitch']['midi_note'] in [-2, 0, 2]
    assert new['onset']['n'] - old['onset']['n'] in [-1, 0]
    assert varied['notes'][:2] == baseline['notes'][:2]


def test_qa_develop_retry_conflict_corrupt_seed_and_bounded_receipt(tmp_path):
    value = definition()
    result = run(tmp_path, value)
    assert len((json.dumps(result, ensure_ascii=True, indent=2) + '\n').encode()) <= 16384
    with pytest.raises(PocketError, match='idempotency_conflict'):
        run(tmp_path, {**value, 'label': 'changed'})
    (tmp_path / 'store' / result['artifacts']['unchanged']['artifact_uri']).write_bytes(b'corrupt')
    with pytest.raises(PocketError, match='integrity'):
        run(tmp_path, value)


def test_qa_develop_unknown_tuning_keeps_records_and_abstains_intervals(tmp_path):
    value = definition()
    record = value['seed']['material']
    for row in record['notes']:
        row['pitch']['tuning_ref'] = 'tuning:unmeasured-external'
    record = seal_literal(record)
    value['seed'].update(material=record, material_revision=record['revision_sha256'])
    result = run(tmp_path, value)
    proof = read_record(result['artifacts']['development'], str(tmp_path / 'store'))
    assert all(row['declared_note_order_pitch_intervals'] == 'abstained_unknown_tuning'
               for side in proof['identity_proofs'].values() for row in side)
    assert all(row['perceptual_similarity'] == 'not_inferred'
               for side in proof['identity_proofs'].values() for row in side)


def budget_definition(clip_notes, occurrences, extra_notes=0, changed=1):
    value = definition()
    record = material([note(f'beat:{i}', Fraction(i, clip_notes), Fraction(1, 2 * clip_notes), 60)
                       for i in range(clip_notes)], length=1)
    if extra_notes:
        record['notes'].extend(note(f'retained:{i}', i, 1, 72) for i in range(extra_notes))
        record['clips'].append({**copy.deepcopy(record['clips'][0]), 'id': 'retained-clip',
                               'length_qn': q(extra_notes),
                               'note_ids': [f'retained:{i}' for i in range(extra_notes)]})
    record = seal_literal(record)
    value['seed'] = {'material': record, 'material_revision': record['revision_sha256'],
                     'clip_id': record['clips'][0]['id']}
    value.update(length_qn=occurrences, endpoint_note_id=f'beat:{clip_notes - 1}',
                 locked_occurrence_id='occurrence:0',
                 occurrences=[{'occurrence_id': f'occurrence:{i}', 'at_qn': i} for i in range(occurrences)],
                 eligible_occurrence_ids=[f'occurrence:{i}' for i in range(1, changed + 1)])
    value['variation']['max_changed_notes'] = changed
    return value


def test_qa_develop_1024_output_boundary_and_1056_refusal(tmp_path):
    value = budget_definition(16, 64)
    result = run(tmp_path, value)
    store = str(tmp_path / 'store')
    for handle in result['artifacts']['alternatives']:
        assert len(read_record(handle, store)['notes']) == 1024
    assert read_record(result['artifacts']['no_addition'], store)['notes'] == []
    proof = read_record(result['artifacts']['development'], store)
    deletion_calls = [row for row in proof['public_steps']
                      if row['tool'] == 'midi_transform' and row['arguments']['operations'][0]['op'] == 'delete']
    assert len(deletion_calls) == 4
    assert all(len(row['arguments']['selection']['note_ids']) == 256 for row in deletion_calls)
    with pytest.raises(PocketError, match='1024'):
        run(tmp_path / 'overflow', budget_definition(32, 33))
    assert not list((tmp_path / 'overflow' / 'store' / 'artifacts').glob('*'))


@pytest.mark.parametrize('extra_notes,expected', [(772, 10000), (773, 10002)])
def test_qa_develop_evidence_budget_full_source_counts_and_32_changes(tmp_path, extra_notes, expected):
    # 256 output notes; 32 changed endpoints create 33 full revisions; no deletion
    # remainder; exact retained source has four performed notes plus extra clip.
    value = budget_definition(4, 64, extra_notes=extra_notes, changed=32)
    if expected > 10000:
        with pytest.raises(PocketError, match='10000 material-note copies'):
            run(tmp_path, value)
        assert not list((tmp_path / 'store' / 'artifacts').glob('*'))
        return
    result = run(tmp_path, value)
    store = str(tmp_path / 'store')
    proof = read_record(result['artifacts']['development'], store)
    assert proof['evidence_budget']['material_note_copies'] == expected
    assert result['change_summary']['changed_endpoint_notes'] == 32
    baseline, changed = [read_record(handle, store) for handle in result['artifacts']['alternatives']]
    assert sum(left != right for left, right in zip(baseline['notes'], changed['notes'], strict=True)) == 32
    assert read_record(result['artifacts']['unchanged'], store) == value['seed']['material']


def test_qa_develop_interruption_retains_stage_manifest_and_no_success(tmp_path, monkeypatch):
    import pocket_music.midi_develop as provider

    def interrupt(**kwargs):
        raise KeyboardInterrupt('Independent interruption after sequence publication')

    monkeypatch.setattr(provider, 'midi_transform', interrupt)
    with pytest.raises(KeyboardInterrupt) as captured:
        run(tmp_path)
    assert 'Incomplete development evidence' in '\n'.join(captured.value.__notes__)
    manifests = [json.loads(path.read_text()) for path in (tmp_path / 'store' / 'artifacts').glob('*/record.json')]
    failure = next(record for record in manifests if record['schema'] == 'pocket.motif-development-failure/v1')
    assert failure['complete'] is False and failure['valid_alternatives'] == []
    assert [stage['tool'] for stage in failure['completed_public_steps']] == ['material_sequence']
    assert failure['attempting'][0]['tool'] == 'midi_transform'
    journal = json.loads((tmp_path / 'store' / 'requests' / 'develop' / 'journal.json').read_text())
    assert journal['state'] == 'failed' and 'receipt' not in journal
    with pytest.raises(PocketError, match='did not complete'):
        run(tmp_path)
