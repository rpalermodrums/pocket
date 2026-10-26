# SPDX-License-Identifier: AGPL-3.0-only
"""Generated-source score membership, exact edit lineage and retained dependency oracles."""
import copy
import shutil
from pathlib import Path

import pytest
from test_musical_context import fixture, q

from pocket_music.artifact_store import put_record, read_bytes, read_record
from pocket_music.audio_score import audio_score_create, audio_score_query, audio_score_revise, load_audio_score
from pocket_music.context_edits import context_edit
from pocket_music.errors import PocketError
from pocket_music.musical_context import context_create


def score_fixture(tmp_path):
    store, _, context_definition, source, _ = fixture(tmp_path)
    # A second occurrence overlaps the opening; the first passage also returns later.
    context_definition['occurrences'].append({
        'occurrence_id': 'backing', 'source_clock_id': 'recording', 'timeline_clock_id': 'practice',
        'source_span_frames': [11000, 19000], 'timeline_span_qn': [q(0), q(2)]})
    context = context_create(store, 'overlapping-context', context_definition)['artifacts']['context']
    author = context_definition['attribution']
    definition = {'score_id': 'generated-score', 'title': 'Generated passage relationships',
        'attribution': author, 'passages': [
            {'passage_id': 'linked-figure', 'label': 'Repeated figure', 'members': [
                {'occurrence_id': 'first', 'role': 'recurring figure'},
                {'occurrence_id': 'again', 'role': 'returning figure'}], 'attribution': author},
            {'passage_id': 'opening-composite', 'label': 'Opening composite', 'members': [
                {'occurrence_id': 'first', 'role': 'foreground'},
                {'occurrence_id': 'backing', 'role': 'foundation'}], 'attribution': author},
            {'passage_id': 'later', 'label': 'Later passage', 'members': [
                {'occurrence_id': 'alternative', 'role': 'next development'}], 'attribution': author}],
        'relations': [{'relation_id': 'figure-return', 'from_passage': 'opening-composite',
            'to_passage': 'linked-figure', 'kind': 'return', 'attribution': author}]}
    args = {'store_root': store, 'request_id': 'score', 'context': context, 'definition': definition}
    return args, context_definition, source


def test_shared_membership_overlaps_repeated_sources_and_bounded_query(tmp_path):
    args, context_definition, source = score_fixture(tmp_path)
    before = source.read_bytes()
    result = audio_score_create(**args)
    assert result == audio_score_create(**args)
    handle = result['artifacts']['score']
    record = load_audio_score(handle, args['store_root'])
    assert record['definition'] == args['definition']
    assert record['context'] == args['context'] and record['parent'] is None and record['context_edit'] is None
    assert not record['coverage']['audio_rendered'] and not record['coverage']['native_execution']
    assert record['coverage']['performance_readiness'] == 'not_established'
    assert read_record(args['context'], args['store_root'])['definition'] == context_definition
    page = audio_score_query(args['store_root'], handle, 'members', ['linked-figure'], limit=1)
    assert page['rows'] == [{'passage_id': 'linked-figure', 'occurrence_id': 'first', 'role': 'recurring figure'}]
    assert page['next_offset'] == 1 and page['total'] == 2
    assert audio_score_query(args['store_root'], handle, 'members', ['linked-figure'], offset=1)['rows'] == [
        {'passage_id': 'linked-figure', 'occurrence_id': 'again', 'role': 'returning figure'}]
    assert audio_score_query(args['store_root'], handle, 'relations', ['linked-figure'])['filter_scope'] == 'either_endpoint'
    assert source.read_bytes() == before


def test_exact_parent_rebind_preserves_definition_and_all_original_bytes(tmp_path):
    args, _, source = score_fixture(tmp_path)
    store = args['store_root']
    original = audio_score_create(**args)['artifacts']['score']
    parent_bytes, source_bytes = read_bytes(original, store), source.read_bytes()
    author = args['definition']['attribution']
    change = context_edit(store, 'group-shift', args['context'], [
        {'kind': 'occurrence_shift_timeline', 'occurrence_ids': ['first', 'again'], 'delta_qn': q(1, 4)}],
        [{'section': 'occurrences', 'object_id': 'backing', 'fields': ['timeline_span_qn', 'source_span_frames']}], author)
    inputs = {'store_root': store, 'request_id': 'score-revise', 'score': original,
              'context_edit': change['artifacts']['edit']}
    result = audio_score_revise(**inputs)
    assert audio_score_revise(**inputs) == result
    revised = load_audio_score(result['artifacts']['score'], store)
    assert revised['definition'] == args['definition']
    assert revised['context'] == change['artifacts']['context'] and revised['parent'] == original
    assert revised['context_edit'] == change['artifacts']['edit']
    child = read_record(revised['context'], store)['definition']['occurrences']
    assert child[0]['timeline_span_qn'] == [q(1, 4), q(9, 4)]
    assert child[1]['timeline_span_qn'] == [q(9, 4), q(17, 4)]
    assert child[3]['timeline_span_qn'] == [q(0), q(2)]
    assert read_bytes(original, store) == parent_bytes and source.read_bytes() == source_bytes


def test_wrong_same_named_context_cannot_rebind(tmp_path):
    args, context_definition, _ = score_fixture(tmp_path)
    store = args['store_root']
    score = audio_score_create(**args)['artifacts']['score']
    context_definition['title'] = 'Independent context with the same ID'
    other = context_create(store, 'different', context_definition)['artifacts']['context']
    edit = context_edit(store, 'unrelated-edit', other, [
        {'kind': 'occurrence_slip_source', 'occurrence_ids': ['first'], 'delta_frames': 10}],
        [], args['definition']['attribution'])['artifacts']['edit']
    with pytest.raises(PocketError, match='exact context'):
        audio_score_revise(store, 'wrong-parent', score, edit)


@pytest.mark.parametrize('change,match', [
    (lambda d: d['passages'][0]['members'][0].update(occurrence_id='unknown'), 'unknown context occurrence'),
    (lambda d: d['passages'][0]['members'].append(d['passages'][0]['members'][0]), 'Duplicate occurrence_id'),
    (lambda d: d['passages'].append(d['passages'][0]), 'Duplicate passage_id'),
    (lambda d: d['passages'][0].update(members=[]), 'list bound'),
    (lambda d: d['passages'][0].update(members=d['passages'][0]['members'] * 17), 'list bound'),
    (lambda d: d['passages'][0]['members'][0].update(role=''), 'authored occurrence role'),
    (lambda d: d['passages'][0]['members'][0].update(source_span_frames=[0, 1]), 'musical-time fields'),
    (lambda d: d['relations'][0].update(to_passage='unknown'), 'unknown audio passage'),
    (lambda d: d['relations'][0].update(kind='launch'), 'relation kind'),
    (lambda d: d['relations'].append({**d['relations'][0], 'relation_id': 'back',
        'from_passage': 'linked-figure', 'to_passage': 'opening-composite'}), 'acyclic'),
    (lambda d: d['relations'][0].update(to_passage='opening-composite'), 'acyclic'),
    (lambda d: d.update(passages=[]), 'list bound'),
    (lambda d: d.update(passages=d['passages'] * 86), 'list bound'),
    (lambda d: d.update(relations=d['relations'] * 513), 'list bound'),
])
def test_closed_inputs_unknown_ids_cycles_and_budgets_fail(tmp_path, change, match):
    args, _, _ = score_fixture(tmp_path)
    change(args['definition'])
    with pytest.raises(PocketError, match=match):
        audio_score_create(**args)


@pytest.mark.parametrize('options,match', [
    ({'limit': True}, 'bounded integer'),
    ({'limit': 65}, 'bounded integer'),
    ({'offset': -1}, 'bounded integer'),
    ({'section': 'audio'}, 'query section'),
    ({'section': 'members', 'passage_ids': ['unknown']}, 'passage filter'),
    ({'section': 'members', 'passage_ids': ['later', 'later']}, 'passage filter'),
    ({'passage_ids': ['later']}, 'summary accepts'),
    ({'offset': 1}, 'summary accepts'),
])
def test_query_refuses_unknown_selection_and_unbounded_pages(tmp_path, options, match):
    args, _, _ = score_fixture(tmp_path)
    score = audio_score_create(**args)['artifacts']['score']
    with pytest.raises(PocketError, match=match):
        audio_score_query(args['store_root'], score, **options)


def test_forged_rehashed_revision_cannot_change_definition_or_context(tmp_path):
    args, context_definition, _ = score_fixture(tmp_path)
    store = args['store_root']
    parent = audio_score_create(**args)['artifacts']['score']
    edit = context_edit(store, 'move', args['context'], [
        {'kind': 'occurrence_shift_timeline', 'occurrence_ids': ['first'], 'delta_qn': q(1, 4)}],
        [], args['definition']['attribution'])['artifacts']['edit']
    score = audio_score_revise(store, 'revision', parent, edit)['artifacts']['score']
    record = read_record(score, store)
    record['definition']['passages'][0]['members'][0]['role'] = 'Unrequested role change'
    with pytest.raises(PocketError, match='differs from its proof'):
        audio_score_query(store, put_record(record, store))
    record = read_record(score, store)
    context_definition['title'] = 'Unrequested context replacement'
    record['context'] = context_create(store, 'unrequested-context', context_definition)['artifacts']['context']
    with pytest.raises(PocketError, match='differs from its proof'):
        load_audio_score(put_record(record, store), store)
    record = read_record(score, store)
    fake_edit = read_record(edit, store)
    fake_edit['changes'] = []
    record['context_edit'] = put_record(fake_edit, store)
    with pytest.raises(PocketError, match='preservation evidence'):
        audio_score_query(store, put_record(record, store))
    record = read_record(parent, store)
    record['coverage']['native_execution'] = True
    with pytest.raises(PocketError, match='coverage mismatch'):
        audio_score_query(store, put_record(record, store))


def test_replay_integrity_idempotency_and_store_relocation(tmp_path):
    args, _, source = score_fixture(tmp_path)
    store = args['store_root']
    score = audio_score_create(**args)['artifacts']['score']
    changed = copy.deepcopy(args)
    changed['definition']['title'] = 'Changed request'
    with pytest.raises(PocketError, match='idempotency_conflict'):
        audio_score_create(**changed)
    context = read_record(args['context'], store)
    region_handle = context['definition']['sources'][0]['region']
    region = read_record(region_handle, store)
    crop = Path(store) / region['crop']['artifact_uri']
    original = crop.read_bytes()
    crop.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    with pytest.raises(PocketError, match='integrity mismatch'):
        audio_score_create(**args)
    crop.write_bytes(original)
    expected = audio_score_query(store, score)
    moved = tmp_path / 'moved-store'
    shutil.copytree(store, moved)
    shutil.rmtree(store)
    source.unlink()
    assert audio_score_query(str(moved), score) == expected
    assert audio_score_create(**{**args, 'store_root': str(moved)})['artifacts']['score'] == score
