"""Independent literal edit oracles: source slips, placement shifts and exact locks."""
import copy

import pytest
from test_musical_context import fixture, q

from pocket_music.artifact_store import put_record, read_record
from pocket_music.context_edits import context_edit, context_edit_query
from pocket_music.errors import PocketError
from pocket_music.interpretations import context_bind_interpretation, interpretation_create
from pocket_music.musical_context import context_query, context_resolve
from pocket_music.practice_audio import practice_render


def apply(f, operations, locks=None, request='edit'):
    store, context, definition, _, _ = f
    return context_edit(store, request, context, operations, locks or [], definition['attribution'])


def slip(ids=None, delta=400):
    return {'kind': 'occurrence_slip_source', 'occurrence_ids': ids or ['first'], 'delta_frames': delta}


def shift(ids=None, delta=None):
    return {'kind': 'occurrence_shift_timeline', 'occurrence_ids': ids or ['first'], 'delta_qn': delta or q(1)}


def test_source_slip_and_timeline_shift_are_distinct_and_replayable(tmp_path):
    f = fixture(tmp_path)
    store, parent, definition, _, _ = f
    slipped = apply(f, [slip()])
    assert apply(f, [slip()]) == slipped
    shifted = apply(f, [shift()], request='shift')
    a = read_record(slipped['artifacts']['context'], store)['definition']
    b = read_record(shifted['artifacts']['context'], store)['definition']
    expected_a, expected_b = copy.deepcopy(definition), copy.deepcopy(definition)
    expected_a['occurrences'][0]['source_span_frames'] = [2100, 10100]
    expected_b['occurrences'][0]['timeline_span_qn'] = [q(1), q(3)]
    assert a == expected_a and b == expected_b
    assert read_record(parent, store)['definition'] == definition
    receipt = context_edit_query(store, slipped['artifacts']['edit'])['summary']
    assert receipt['changes'] == [{'path': '/definition/occurrences/0/source_span_frames', 'object_id': 'first',
                                   'before': [1700, 9700], 'after': [2100, 10100]}]
    assert receipt['renderability'][0]['exact_pcm_compatible']
    with pytest.raises(PocketError, match='idempotency_conflict'):
        apply(f, [slip(delta=401)])


def test_explicit_linked_shift_preserves_internal_offset_and_contiguity(tmp_path):
    f = fixture(tmp_path)
    store, parent, _, _, _ = f
    ops = [shift(['first', 'again'], q(-1))]
    result = apply(f, ops, locks=[{'section': 'occurrences', 'object_id': 'first',
                                 'fields': ['source_span_frames', 'source_clock_id']}])
    child = result['artifacts']['context']
    point = context_resolve(store, child, 'practice', 'arrangement_qn', anchor_id='internal-one', occurrence_id='first')
    assert point['output']['value'] == q(-1, 2)
    original = context_resolve(store, parent, 'practice', 'arrangement_qn', anchor_id='internal-one', occurrence_id='first')
    assert original['output']['value'] == q(1, 2)
    assert practice_render(store, 'linked-render', child, ['first', 'again'])['status'] == 'ok'
    isolated = apply(f, [shift()], request='isolated')['artifacts']['context']
    with pytest.raises(PocketError, match='contiguous'):
        practice_render(store, 'isolated-render', isolated, ['first', 'again'])


@pytest.mark.parametrize('operations,locks,match', [
    ([slip(delta=0)], [], 'nonzero'),
    ([slip(delta=True)], [], 'integer'),
    ([slip(delta=-2000)], [], 'bounded integer'),
    ([slip(delta=20000)], [], 'bounded integer'),
    ([slip(['missing'])], [], 'Unknown occurrence'),
    ([slip(['first', 'first'])], [], 'Duplicate'),
    ([slip(), slip()], [], 'only once'),
    ([slip()], [{'section': 'occurrences', 'object_id': 'first', 'fields': ['source_span_frames']}], 'locked field'),
    ([slip()], [{'section': 'occurrences', 'object_id': 'first', 'fields': ['absent']}], 'Unknown locked'),
    ([slip()], [{'section': 'anchors', 'object_id': 'absent', 'fields': ['position']}], 'unknown object'),
])
def test_bounds_locks_and_unknown_selection_fail_closed(tmp_path, operations, locks, match):
    with pytest.raises(PocketError, match=match):
        apply(fixture(tmp_path), operations, locks)


def test_rebind_keeps_tempo_and_other_anchors_and_checks_exact_parent(tmp_path):
    f = fixture(tmp_path)
    store, parent, definition, _, _ = f
    author = definition['attribution']
    chosen = interpretation_create(store, 'choose', parent, 'recording',
        {'kind': 'bar_one', 'status': 'authored', 'source_frame_q': q(4100)}, author)['artifacts']['interpretation']
    op = {'kind': 'anchor_rebind', 'anchor_id': 'internal-one', 'binding_id': 'one-choice', 'interpretation': chosen}
    result = apply(f, [op])
    child = result['artifacts']['context']
    expected = copy.deepcopy(definition)
    expected['anchors'][0]['position']['value'] = 4100
    assert read_record(child, store)['definition'] == expected
    assert len(context_query(store, child, 'bindings')['rows']) == 1
    with pytest.raises(PocketError, match='locked field'):
        apply(f, [op], [{'section': 'anchors', 'object_id': 'internal-one', 'fields': ['position']}], request='locked')
    with pytest.raises(PocketError, match='exact edit parent'):
        context_edit(store, 'stale-selection', child, [op], [], author)


def test_bound_context_edits_retain_selections_and_allow_explicit_replacement(tmp_path):
    f = fixture(tmp_path)
    store, parent, definition, _, _ = f
    author = definition['attribution']
    chosen = interpretation_create(store, 'choose', parent, 'recording',
        {'kind': 'onset', 'status': 'authored', 'source_frame_q': q(3700)}, author)['artifacts']['interpretation']
    bound = context_bind_interpretation(store, 'bind', parent, chosen,
        {'kind': 'anchor', 'binding_id': 'choice', 'anchor_id': 'chosen-onset', 'label': 'Chosen'}, author)['artifacts']['context']
    moved = context_edit(store, 'move-bound', bound, [slip()], [], author)['artifacts']['context']
    assert read_record(moved, store)['bindings'] == read_record(bound, store)['bindings']
    revised_choice = interpretation_create(store, 'new-choice', moved, 'recording',
        {'kind': 'onset', 'status': 'authored', 'source_frame_q': q(4100)}, author)['artifacts']['interpretation']
    rebound = context_edit(store, 'rebind', moved, [{'kind': 'anchor_rebind', 'anchor_id': 'chosen-onset',
        'binding_id': 'choice', 'interpretation': revised_choice}], [], author)['artifacts']['context']
    assert context_query(store, rebound, 'bindings')['rows'][0]['interpretation'] == revised_choice
    assert context_query(store, moved, 'bindings')['rows'][0]['interpretation'] == chosen


def test_forged_child_untargeted_drift_and_false_receipt_rejected(tmp_path):
    f = fixture(tmp_path)
    store = f[0]
    result = apply(f, [slip()])
    record = read_record(result['artifacts']['edit'], store)
    altered = read_record(record['child'], store)
    altered['definition']['title'] = 'Unrequested change'
    record['child'] = put_record(altered, store)
    with pytest.raises(PocketError, match='preservation evidence'):
        context_edit_query(store, put_record(record, store))
    record = read_record(result['artifacts']['edit'], store)
    record['preserved_definition_sha256'] = '0'*64
    with pytest.raises(PocketError, match='preservation evidence'):
        context_edit_query(store, put_record(record, store))


def test_edit_reports_unsupported_tempo_realization_without_rendering(tmp_path):
    from pocket_music.musical_context import context_create
    from pocket_music.time_maps import musical_time
    f = fixture(tmp_path)
    store, _, definition, _, timing = f
    timing['tempo'].append({'at_qn': q(2), 'bpm': q(150), 'interpolation': 'step'})
    definition['timelines'][0]['time_map'] = musical_time('create', store, request_id='step', definition=timing)['artifacts']['time_map']
    context = context_create(store, 'step-context', definition)['artifacts']['context']
    result = context_edit(store, 'step-shift', context, [shift(delta=q(2))], [], definition['attribution'])
    assert result['renderability'][0]['exact_pcm_compatible'] is False
    assert 'stretch' in result['renderability'][0]['reason']
    assert not result['coverage']['audio_rendered']
