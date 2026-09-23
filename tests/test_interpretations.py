"""Source/revision-bound choices preserve abstention, corrections and exact coordinates."""
import copy

import pytest
from test_musical_context import fixture, q

from pocket_music.artifact_store import put_record, read_record
from pocket_music.audio_region_analysis import audio_region_hypotheses, audio_region_query
from pocket_music.audio_region_corrections import audio_region_correct
from pocket_music.errors import PocketError
from pocket_music.interpretations import (
    _claim,
    context_bind_interpretation,
    interpretation_create,
    interpretation_query,
)
from pocket_music.musical_context import context_create, context_query, context_resolve
from pocket_music.practice_audio import practice_render


def evidence_fixture(tmp_path):
    store, context, definition, source, _ = fixture(tmp_path)
    author = definition['attribution']
    hypothesis = audio_region_hypotheses(store_root=store, request_id='hypotheses',
        region={'kind': 'captured', 'region': definition['sources'][0]['region']},
        analysis={'kind': 'peek', 'settings': {'bpm_hint': None, 'beats_per_bar': 4}},
        attribution=author)['artifacts']['hypotheses']
    initial = audio_region_query(store_root=store, hypotheses=hypothesis, view='annotations')['items'][0]
    corrections = [{'correction_id': 'selected-attack', 'annotation': {'kind': 'attack', 'source_frame': 3000,
                    'strength_relative': None}, 'support': [{'kind': 'annotation_id',
                    'reference': initial['local']['annotation_id']}], 'supersedes': [], 'uncertainty': ['Fixture']}]
    corrected = audio_region_correct(store_root=store, request_id='correct', parent=hypothesis,
        expected_revision=hypothesis['sha256'], batch={'coordinate_space': 'local_crop_frame',
        'corrections': corrections}, attribution=author)['artifacts']['hypotheses']
    rows = audio_region_query(store_root=store, hypotheses=corrected, view='annotations', max_bytes=65536)['items']
    point = next(r for r in rows if r['local'].get('correction_id') == 'selected-attack')
    evidence = {'hypotheses': corrected, 'expected_revision': corrected['sha256'],
                'annotation_id': point['local']['annotation_id']}
    return store, context, definition, author, evidence, source, hypothesis


def choose(f, request='choose', **changes):
    store, context, _, author, evidence, _, _ = f
    return interpretation_create(**{'store_root': store, 'request_id': request, 'context': context,
        'source_clock_id': 'recording', 'claim': {'kind': 'onset', 'status': 'selected', 'source_frame_q': q(3700)},
        'attribution': author, 'evidence': evidence, **changes})


def test_nonzero_source_selection_v2_binding_roundtrip_and_original_preserved(tmp_path):
    f = evidence_fixture(tmp_path)
    store, context, definition, author, _, _, _ = f
    before = read_record(context, store)
    selected = choose(f)['artifacts']['interpretation']
    assert choose(f)['artifacts']['interpretation'] == selected
    query = interpretation_query(store, selected)
    assert query['selected_evidence']['original_projection']['source_frame'] == 3700
    assert query['selected_evidence']['local']['annotation']['source_frame'] == 3000
    args = {'store_root': store, 'request_id': 'bind', 'context': context, 'interpretation': selected,
            'binding': {'kind': 'anchor', 'binding_id': 'onset-choice', 'anchor_id': 'selected-onset', 'label': 'Attack'},
            'attribution': author}
    child = context_bind_interpretation(**args)['artifacts']['context']
    assert context_bind_interpretation(**args)['artifacts']['context'] == child
    assert child['artifact_schema'] == 'pocket.musical-context/v2'
    assert read_record(context, store) == before
    assert read_record(child, store)['definition']['timelines'] == definition['timelines']
    assert len(context_query(store, child, 'bindings')['rows']) == 1
    assert context_query(store, context, 'bindings')['rows'] == []
    resolved = context_resolve(store, child, 'practice', 'arrangement_qn', anchor_id='selected-onset', occurrence_id='again')
    assert resolved['output']['value'] == q(5, 2)
    assert practice_render(store, 'v2-render', child, ['first'])['status'] == 'ok'
    with pytest.raises(PocketError, match='context_edit'):
        context_create(store, 'downgrade', definition, parent=child)
    with pytest.raises(PocketError, match='exact origin'):
        context_bind_interpretation(**{**args, 'request_id': 'wrong-parent', 'context': child})
    with pytest.raises(PocketError, match='idempotency_conflict'):
        choose(f, claim={'kind': 'bar_one', 'status': 'authored', 'source_frame_q': q(3700)})


@pytest.mark.parametrize('change,match', [
    ({'claim': {'kind': 'bar_one', 'status': 'selected', 'source_frame_q': q(3700)}}, 'does not assert'),
    ({'claim': {'kind': 'onset', 'status': 'selected', 'source_frame_q': q(3701)}}, 'changes the candidate'),
    ({'claim': {'kind': 'onset', 'status': 'selected', 'source_frame_q': q(699)}}, 'exceeds retained'),
    ({'claim': {'kind': 'onset', 'status': 'selected', 'source_frame_q': {'n': 7400, 'd': 2}}}, 'reduced'),
    ({'evidence': None}, 'requires an exact evidence'),
    ({'source_clock_id': 'unknown'}, 'exact source clock'),
])
def test_invalid_or_promoted_claims_refused(tmp_path, change, match):
    with pytest.raises(PocketError, match=match):
        choose(evidence_fixture(tmp_path), **change)


def test_authored_fraction_retained_without_implicit_frame_rounding(tmp_path):
    f = evidence_fixture(tmp_path)
    store, context, _, author, _, _, _ = f
    selected = choose(f, claim={'kind': 'bar_one', 'status': 'authored', 'source_frame_q': q(7401, 2)})['artifacts']['interpretation']
    assert interpretation_query(store, selected)['summary']['claim']['source_frame_q'] == q(7401, 2)
    child = context_bind_interpretation(store, 'fraction', context, selected,
        {'kind': 'selection', 'binding_id': 'fractional-hypothesis'}, author)['artifacts']['context']
    assert read_record(child, store)['definition'] == read_record(context, store)['definition']
    with pytest.raises(PocketError, match='integer frame'):
        context_bind_interpretation(store, 'no-rounding', context, selected,
            {'kind': 'anchor', 'binding_id': 'fraction', 'anchor_id': 'fraction', 'label': 'Fraction'}, author)
    # A learned fractional downbeat remains exact in the claim validator too.
    row = {'local': {'annotation': {'kind': 'learned_downbeat'}},
           'original_projection': {'source_frame_q': q(7401, 2)}}
    _claim({'kind': 'bar_one', 'status': 'selected', 'source_frame_q': q(7401, 2)},
           {'interval': {'start_frame': 700, 'end_frame_exclusive': 20700}}, row)


def test_revision_crop_and_superseded_candidate_binding(tmp_path):
    f = evidence_fixture(tmp_path)
    store, _context, definition, author, evidence, source, _ = f
    with pytest.raises(PocketError, match='Stale'):
        choose(f, evidence={**evidence, 'expected_revision': '0'*64})
    with pytest.raises(PocketError, match='does not belong'):
        choose(f, 'absent', evidence={**evidence, 'annotation_id': 'other'})
    from pocket_music.assets import sha256_file
    from pocket_music.audio_regions import audio_region_capture
    crop = audio_region_capture(store_root=store, request_id='other-crop', source={'path': str(source),
        'expected_sha256': sha256_file(source), 'start_frame': 800, 'frames': 20000,
        'source_origin': 'independently_acquired'})['artifacts']['region']
    different = copy.deepcopy(definition)
    different['sources'][0]['region'] = crop
    other = context_create(store, 'other-context', different)['artifacts']['context']
    with pytest.raises(PocketError, match='another source or crop'):
        choose(f, 'wrong-crop', context=other)
    revised = audio_region_correct(store_root=store, request_id='supersede', parent=evidence['hypotheses'],
        expected_revision=evidence['expected_revision'], attribution=author,
        batch={'coordinate_space': 'local_crop_frame', 'corrections': [{'correction_id': 'new-point',
        'annotation': {'kind': 'attack', 'source_frame': 3001, 'strength_relative': None},
        'support': [{'kind': 'annotation_id', 'reference': evidence['annotation_id']}],
        'supersedes': [evidence['annotation_id']], 'uncertainty': []}]})['artifacts']['hypotheses']
    with pytest.raises(PocketError, match='superseded'):
        choose(f, 'superseded', evidence={**evidence, 'hypotheses': revised, 'expected_revision': revised['sha256']})
    # The original immutable snapshot remains addressable, never retroactively rewritten.
    assert choose(f, 'historical')['status'] == 'ok'


def test_unresolved_abstention_never_becomes_nominal_tempo(tmp_path):
    f = evidence_fixture(tmp_path)
    store, context, _, author, _, _, initial = f
    rows = audio_region_query(store_root=store, hypotheses=initial, view='annotations', max_bytes=65536)['items']
    abstained = next(r for r in rows if r['local']['annotation']['kind'] == 'abstention')
    evidence = {'hypotheses': initial, 'expected_revision': initial['sha256'], 'annotation_id': abstained['local']['annotation_id']}
    claim = {'kind': 'pulse', 'status': 'unresolved', 'interval_frames': [700, 20700]}
    result = choose(f, claim=claim, evidence=evidence)['artifacts']['interpretation']
    assert interpretation_query(store, result)['summary']['claim'] == claim
    with pytest.raises(PocketError, match='Abstention'):
        choose(f, 'no-guess', evidence=evidence, claim={'kind': 'pulse', 'status': 'authored', 'bpm': 120.0,
                                                      'interval_frames': [700, 20700]})
    child = context_bind_interpretation(store, 'unresolved', context, result,
        {'kind': 'selection', 'binding_id': 'pulse-unresolved'}, author)['artifacts']['context']
    assert read_record(child, store)['definition'] == read_record(context, store)['definition']


def test_rehashed_false_evidence_and_anchor_are_rejected(tmp_path):
    f = evidence_fixture(tmp_path)
    store, context, _, author, _, _, _ = f
    handle = choose(f)['artifacts']['interpretation']
    forged = read_record(handle, store)
    forged['claim']['source_frame_q'] = q(3701)
    with pytest.raises(PocketError, match='changes the candidate'):
        interpretation_query(store, put_record(forged, store))
    child = context_bind_interpretation(store, 'bind', context, handle,
        {'kind': 'anchor', 'binding_id': 'choice', 'anchor_id': 'chosen', 'label': 'Selected'}, author)['artifacts']['context']
    fake = read_record(child, store)
    fake['definition']['anchors'][-1]['position']['value'] += 1
    with pytest.raises(PocketError, match='anchor differs'):
        context_query(store, put_record(fake, store))
