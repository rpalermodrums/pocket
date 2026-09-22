"""Independent exact placement, parent retention and refusal expectations."""
from __future__ import annotations

import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction

import pytest
from test_midi_qa import expressed_material, seal_literal
from test_midi_relationships_qa import material, note, q

from pocket_music.artifact_store import put_record, read_record
from pocket_music.errors import PocketError
from pocket_music.material_sequence import material_sequence


def sources():
    first = material([note('same-note', Fraction(1, 3), Fraction(1, 4), 60)], length=4)
    second = material([note('same-note', Fraction(1, 2), Fraction(1, 4), 67)], length=3)
    first['clips'][0]['origin'] = {'space': 'arrangement_qn', **q(10)}
    second['clips'][0]['origin'] = {'space': 'phrase_qn', **q(-30)}
    return seal_literal(first), seal_literal(second)


def definition():
    first, second = sources()
    return {'label': 'Externally placed whole clips',
            'materials': [{'key': 'A', 'material': first}, {'key': 'B', 'material': second}],
            'clock': {'schema': 'pocket.time-context/v1', 'context_id': 'clock:author',
                      'attribution': 'Independent fixture explicitly supplies placements.'},
            'origin': {'space': 'phrase_qn', **q(-2)}, 'length_qn': 13,
            'occurrences': [
                {'occurrence_id': 'opening', 'material_key': 'A', 'material_revision': first['revision_sha256'],
                 'clip_id': first['clips'][0]['id'], 'at_qn': 0},
                {'occurrence_id': 'answer', 'material_key': 'B', 'material_revision': second['revision_sha256'],
                 'clip_id': second['clips'][0]['id'], 'at_qn': 6},
                {'occurrence_id': 'return', 'material_key': 'A', 'material_revision': first['revision_sha256'],
                 'clip_id': first['clips'][0]['id'], 'at_qn': 9}],
            'controller_policy': 'reject_present', 'expression_policy': 'reject_present',
            'overlap_policy': 'reject_same_channel_pitch'}


def run(tmp_path, value=None, request='sequence'):
    return material_sequence(store_root=str(tmp_path / 'store'), request_id=request,
                             definition=definition() if value is None else value)


def test_qa_sequence_same_source_ids_explicit_placements_and_full_rests(tmp_path):
    value = definition()
    before = copy.deepcopy(value)
    result = run(tmp_path, value)
    store = str(tmp_path / 'store')
    child = read_record(result['artifacts']['material'], store)
    proof = read_record(result['artifacts']['sequence'], store)
    assert value == before
    assert [n['onset'] for n in child['notes']] == [
        {'space': 'clip_qn', **q(Fraction(1, 3))}, {'space': 'clip_qn', **q(Fraction(13, 2))},
        {'space': 'clip_qn', **q(Fraction(28, 3))}]
    assert [n['pitch']['midi_note'] for n in child['notes']] == [60, 67, 60]
    assert len({n['id'] for n in child['notes']}) == len({n['voice_id'] for n in child['notes']}) == 3
    assert all(n['derived_from'] == ['same-note'] for n in child['notes'])
    assert child['clips'][0]['length_qn'] == q(13) and child['clips'][0]['origin'] == value['origin']
    assert child['events'] == child['curves'] == [] and child['parent_revision'] is None
    parents = {x['key']: read_record(x['material'], store) for x in child['sources']}
    assert parents == {'A': sources()[0], 'B': sources()[1]}
    by_id = {n['id']: n for n in child['notes']}
    for row in proof['note_derivations']:
        parent = read_record(row['material'], store)
        source = next(n for n in parent['notes'] if n['id'] == row['source_note_id'])
        derived = by_id[row['note_id']]
        assert row['material_revision'] == parent['revision_sha256']
        for field in set(source) - {'id', 'onset', 'voice_id', 'derived_from'}:
            assert derived[field] == source[field]
    assert [row['source_length_qn'] for row in proof['occurrences']] == [q(4), q(3), q(4)]
    assert [row['source_origin'] for row in proof['occurrences']] == [
        {'space': 'arrangement_qn', **q(10)}, {'space': 'phrase_qn', **q(-30)}, {'space': 'arrangement_qn', **q(10)}]
    assert result['coverage']['native_execution'] is False
    assert result['coverage']['human_listening'] == 'not_established'


def test_qa_sequence_inline_handle_and_time_spelling_equivalence(tmp_path):
    value = definition()
    first = run(tmp_path, value)
    other = copy.deepcopy(value)
    for binding in other['materials']:
        binding['material'] = put_record(binding['material'], str(tmp_path / 'store'))
    other['length_qn'] = q(13)
    for occurrence in other['occurrences']:
        occurrence['at_qn'] = q(occurrence['at_qn'])
    second = run(tmp_path, other, 'handles')
    assert second['artifacts'] == first['artifacts']
    assert run(tmp_path, value, 'again')['artifacts'] == first['artifacts']
    assert run(tmp_path, value) == first


@pytest.mark.parametrize('path,replacement', [
    (('length_qn',), True), (('length_qn',), 13.0), (('length_qn',), {'n': 26, 'd': 2}),
    (('origin', 'space'), 'clip_qn'), (('origin', 'n'), False),
    (('clock', 'attribution'), ''), (('clock', 'schema'), 'pocket.context/v1'),
    (('occurrences', 0, 'at_qn'), -1), (('occurrences', 0, 'at_qn'), {'space': 'seconds', 'n': 0, 'd': 1}),
    (('occurrences', 0, 'at_qn'), 11),
    (('occurrences', 0, 'material_revision'), '0' * 64),
    (('occurrences', 0, 'clip_id'), 'unknown'), (('occurrences', 0, 'material_key'), 'unknown'),
    (('occurrences', 0, 'occurrence_id'), 'answer'),
    (('controller_policy',), 'preserve'), (('expression_policy',), 'drop'),
    (('overlap_policy',), 'allow'),
])
def test_qa_sequence_malformed_clock_coordinates_identity_and_rest_fit(tmp_path, path, replacement):
    value = definition()
    target = value
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement
    with pytest.raises(PocketError):
        run(tmp_path, value)
    assert not list((tmp_path / 'store' / 'artifacts').glob('*'))


@pytest.mark.parametrize('variant', ['expression', 'binding', 'raw_source', 'clip_extra', 'pickup', 'tail'])
def test_qa_sequence_rich_or_unsafe_source_refuses_without_projection(tmp_path, variant):
    value = definition()
    source = value['materials'][0]['material']
    if variant == 'expression':
        source = expressed_material()
    elif variant == 'binding':
        source['notes'][0]['source_binding'] = {'kind': 'live', 'native_note_id': 1}
    elif variant == 'raw_source':
        source['sources'] = [{'kind': 'smf', 'unknown_source': 'retained'}]
    elif variant == 'clip_extra':
        source['clips'][0]['groove_amount'] = 0.4
    elif variant == 'pickup':
        source['notes'][0]['onset']['n'] = -1
    elif variant == 'tail':
        source['notes'][0]['duration_qn'] = q(5)
    source = seal_literal(source)
    value['materials'][0]['material'] = source
    for occurrence in value['occurrences']:
        if occurrence['material_key'] == 'A':
            occurrence['material_revision'] = source['revision_sha256']
    with pytest.raises(PocketError):
        run(tmp_path, value)


def test_qa_sequence_cross_occurrence_overlap_and_touching(tmp_path):
    value = definition()
    value['occurrences'][2]['at_qn'] = 0
    with pytest.raises(PocketError, match='overlap'):
        run(tmp_path, value)
    value['occurrences'][2]['at_qn'] = q(Fraction(1, 4))
    result = run(tmp_path, value, 'touch')
    assert result['change_summary']['notes'] == 3


def test_qa_sequence_unused_binding_output_bounds_and_receipt_bound(tmp_path):
    value = definition()
    value['materials'].append({'key': 'unused', 'material': sources()[0]})
    with pytest.raises(PocketError, match='Unused'):
        run(tmp_path, value, 'unused')
    value = definition()
    value['occurrences'] = [value['occurrences'][0]] * 257
    with pytest.raises(PocketError):
        run(tmp_path, value, 'occurrence-limit')
    value = definition()
    huge = material([note(f'n:{i}', i, 1, 60) for i in range(8193)], length=8193)
    value['materials'] = [{'key': 'A', 'material': huge}]
    value['occurrences'] = [{**value['occurrences'][0], 'material_revision': huge['revision_sha256']}]
    value['length_qn'] = 8193
    with pytest.raises(PocketError, match='8192'):
        run(tmp_path, value, 'note-limit')
    result = run(tmp_path, request='bounded')
    assert len((json.dumps(result, ensure_ascii=True, indent=2) + '\n').encode()) <= 16384


def test_qa_sequence_tampered_parent_retry_conflict_and_concurrent_request(tmp_path, monkeypatch):
    import pocket_music.material_sequence as provider

    value = definition()
    result = run(tmp_path, value)
    with pytest.raises(PocketError, match='idempotency_conflict'):
        run(tmp_path, {**value, 'label': 'changed'})
    child = read_record(result['artifacts']['material'], str(tmp_path / 'store'))
    (tmp_path / 'store' / child['sources'][0]['material']['artifact_uri']).write_bytes(b'tampered')
    with pytest.raises(PocketError, match='integrity'):
        run(tmp_path, value)
    entered, release = threading.Event(), threading.Event()
    original = provider._construct

    def paused(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(provider, '_construct', paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(run, tmp_path / 'concurrency', definition(), 'same-request')
        try:
            assert entered.wait(10)
            with pytest.raises(PocketError, match='active|interrupted'):
                run(tmp_path / 'concurrency', definition(), 'same-request')
        finally:
            release.set()
        assert pending.result(timeout=10)['status'] == 'ok'
