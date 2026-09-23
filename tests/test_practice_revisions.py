"""Immutable edit ancestry and exact correspondence, independently checked output samples."""
import copy
import io
import shutil

import numpy as np
import pytest
import soundfile as sf
from test_context_edits import apply, shift, slip
from test_musical_context import fixture, q

from pocket_music.artifact_store import put_record, read_bytes, read_record
from pocket_music.context_edits import context_edit
from pocket_music.errors import PocketError
from pocket_music.musical_context import context_create
from pocket_music.practice_audio import practice_compare, practice_feedback, practice_query, practice_render
from pocket_music.practice_comparisons import practice_compare_revisions


def comparison_fixture(tmp_path):
    f = fixture(tmp_path)
    store, context, _, _, _ = f
    baseline = practice_render(store, 'baseline', context, ['first', 'again'])['artifacts']['render']
    edited = apply(f, [slip(['first', 'again'])])
    variant = practice_render(store, 'variant', edited['artifacts']['context'], ['first', 'again'])['artifacts']['render']
    correspondence = [{'variant': variant, 'pairs': [
        {'baseline_occurrence_id': name, 'variant_occurrence_id': name,
         'baseline_interval_frames': [i*8000, (i+1)*8000], 'variant_interval_frames': [i*8000, (i+1)*8000]}
        for i, name in enumerate(['first', 'again'])]}]
    args = {'store_root': store, 'request_id': 'compare', 'baseline': baseline, 'variants': [variant],
            'edit_receipts': [edited['artifacts']['edit']], 'correspondence': correspondence,
            'question': 'Generated 50 ms boundary comparison; no listening'}
    return f, args


def test_revision_comparison_independent_pcm_oracle_feedback_and_relocation(tmp_path):
    f, args = comparison_fixture(tmp_path)
    store, _, _, source, _ = f
    baseline = read_record(args['baseline'], store)
    old_audio = read_bytes(baseline['audio'], store)
    result = practice_compare_revisions(**args)
    assert practice_compare_revisions(**args) == result
    summary = practice_query(store, result['artifacts']['comparison'])['summary']
    assert summary['signal_ready'] and summary['musical_verdict'] is None
    source_samples, _ = sf.read(source, dtype='float64', always_2d=True)
    variant = read_record(args['variants'][0], store)
    output, _ = sf.read(io.BytesIO(read_bytes(variant['audio'], store)), dtype='float64', always_2d=True)
    assert np.array_equal(output, np.concatenate([source_samples[2100:10100]]*2))
    assert read_bytes(baseline['audio'], store) == old_audio
    report = practice_feedback(store, 'feedback', result['artifacts']['comparison'], args['variants'][0], [0, 8000],
                               'Synthetic fixture', 'agent', 'Numerical proof only; no listening')['artifacts']['feedback']
    expected = practice_query(store, report)
    assert expected['summary']['render_sha256'] == variant['audio']['sha256']
    moved = tmp_path / 'moved'
    shutil.copytree(store, moved)
    shutil.rmtree(store)
    source.unlink()
    assert practice_query(str(moved), report) == expected
    with pytest.raises(PocketError, match='one exact context'):
        practice_compare(str(moved), 'old-contract', args['baseline'], args['variants'], 'Still v1')


@pytest.mark.parametrize('change,match', [
    (lambda a: a['correspondence'][0]['pairs'][0].update(baseline_interval_frames=[1, 8000]), 'exact full'),
    (lambda a: a['correspondence'][0]['pairs'][0].update(variant_occurrence_id='unknown'), 'unique, known'),
    (lambda a: a['correspondence'][0]['pairs'].pop(), 'every compared'),
    (lambda a: a['correspondence'][0]['pairs'].append(a['correspondence'][0]['pairs'][0]), 'unique, known'),
    (lambda a: a['correspondence'].append(a['correspondence'][0]), 'Duplicate variant'),
    (lambda a: a.update(duration_policy='auto_align'), 'duration policy'),
    (lambda a: a['edit_receipts'].append(a['edit_receipts'][0]), 'Duplicate or conflicting'),
])
def test_invalid_correspondence_or_receipts_refuse(tmp_path, change, match):
    _, args = comparison_fixture(tmp_path)
    change(args)
    with pytest.raises(PocketError, match=match):
        practice_compare_revisions(**args)


def test_unrelated_same_named_context_cannot_claim_lineage(tmp_path):
    f, args = comparison_fixture(tmp_path)
    store, _, definition, _, _ = f
    different = copy.deepcopy(definition)
    different['title'] = 'Same context_id; different parent'
    parent = context_create(store, 'unrelated', different)['artifacts']['context']
    unrelated = context_edit(store, 'unrelated-edit', parent, [slip(['first', 'again'])], [], definition['attribution'])
    other = practice_render(store, 'other-render', unrelated['artifacts']['context'], ['first', 'again'])['artifacts']['render']
    args.update(variants=[other], edit_receipts=[unrelated['artifacts']['edit']])
    args['correspondence'][0]['variant'] = other
    with pytest.raises(PocketError, match='lineage'):
        practice_compare_revisions(**args)


def test_siblings_and_descendant_require_all_exact_edits(tmp_path):
    f, args = comparison_fixture(tmp_path)
    store, _, definition, _, _ = f
    first_edit = read_record(args['edit_receipts'][0], store)
    second = context_edit(store, 'second-edit', first_edit['child'], [shift(['first', 'again'], q(-1))], [], definition['attribution'])
    sibling = apply(f, [slip(['first', 'again'], delta=500)], request='sibling-edit')
    children = [practice_render(store, name, e['artifacts']['context'], ['first', 'again'])['artifacts']['render']
                for name, e in [('second-render', second), ('sibling-render', sibling)]]
    args['variants'] = children
    args['edit_receipts'] += [second['artifacts']['edit'], sibling['artifacts']['edit']]
    pairs = args['correspondence'][0]['pairs']
    args['correspondence'] = [{'variant': child, 'pairs': pairs} for child in children]
    assert practice_compare_revisions(**args)['status'] == 'ok'
    args['request_id'] = 'missing-ancestor'
    args['edit_receipts'] = args['edit_receipts'][1:]
    with pytest.raises(PocketError, match='lineage'):
        practice_compare_revisions(**args)


def test_explicit_unequal_duration_policy_and_full_pairs(tmp_path):
    f = fixture(tmp_path)
    store, _, definition, _, _ = f
    definition['occurrences'][2]['source_span_frames'] = [11000, 15000]
    definition['occurrences'][2]['timeline_span_qn'] = [q(4), q(5)]
    parent = context_create(store, 'short-context', definition)['artifacts']['context']
    baseline = practice_render(store, 'base', parent, ['first'])['artifacts']['render']
    edit = context_edit(store, 'short-edit', parent, [slip(['alternative'])], [], definition['attribution'])
    variant = practice_render(store, 'short-render', edit['artifacts']['context'], ['alternative'])['artifacts']['render']
    args = {'store_root': store, 'request_id': 'unequal', 'baseline': baseline, 'variants': [variant],
            'edit_receipts': [edit['artifacts']['edit']], 'question': 'Different explicitly paired excerpts',
            'correspondence': [{'variant': variant, 'pairs': [{'baseline_occurrence_id': 'first',
              'variant_occurrence_id': 'alternative', 'baseline_interval_frames': [0, 8000],
              'variant_interval_frames': [0, 4000]}]}]}
    with pytest.raises(PocketError, match='Unequal'):
        practice_compare_revisions(**args)
    assert practice_compare_revisions(**{**args, 'request_id': 'allowed', 'duration_policy': 'allow_mismatch'})['status'] == 'ok'


def test_rehashed_forged_comparison_or_edit_never_becomes_valid(tmp_path):
    _, args = comparison_fixture(tmp_path)
    result = practice_compare_revisions(**args)
    forged = read_record(result['artifacts']['comparison'], args['store_root'])
    forged['signal_ready'] = False
    with pytest.raises(PocketError, match='evidence mismatch'):
        practice_query(args['store_root'], put_record(forged, args['store_root']))
    fake_edit = read_record(args['edit_receipts'][0], args['store_root'])
    fake_edit['changes'] = []
    args['edit_receipts'] = [put_record(fake_edit, args['store_root'])]
    args['request_id'] = 'fake-edit'
    with pytest.raises(PocketError, match='preservation evidence'):
        practice_compare_revisions(**args)
