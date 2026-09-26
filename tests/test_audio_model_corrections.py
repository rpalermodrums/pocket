# SPDX-License-Identifier: AGPL-3.0-only
"""Authored correction contracts over mocked model artifacts; no learned accuracy claim."""
import copy
from unittest.mock import patch

import pytest
from test_audio_models import rehash

from pocket_music.artifact_store import canonical_bytes, digest, put_record, read_record
from pocket_music.audio_hypotheses import audio_hypothesis_correct, audio_hypothesis_query
from pocket_music.errors import PocketError


@pytest.fixture
def model():
    from test_audio_models import PulseModelTests

    fixture = PulseModelTests()
    fixture.setUp()
    runner = fixture.runner
    def with_events(*args, **kwargs):
        result, binding = runner(*args, **kwargs)
        if result['analysis'] is not None:
            raw = result['analysis']
            raw['beat'][20:22] = [1., 1.]
            raw['beat'][-1] = 2.
            raw['vendor_beats_seconds'] = [.41, 2.]
            rehash(raw)
        return result, binding
    with patch('pocket_music.audio_pulse_hypotheses._runner', side_effect=with_events):
        handle = fixture.call()['artifacts']['hypotheses']
    with patch('pocket_music.audio_pulse_hypotheses._runner', side_effect=AssertionError('Query/correction reran model')):
        yield fixture, handle
    fixture.doCleanups()


def correction(parent, store, key='correction'):
    row = read_record(parent, store)['annotations'][0]
    return {'correction_id': key, 'supersedes': [row['annotation_id']],
            'annotation': {'kind': 'attack', 'source_frame': 3417, 'strength_relative': None},
            'support': [{'kind': 'analysis_pointer', 'reference': '/analysis/projection/events/0'}],
            'uncertainty': ['Authored alternative, not model output.']}


def correct(fixture, parent, item=None, request='correct'):
    return audio_hypothesis_correct(store_root=fixture.store, request_id=request, parent=parent,
        expected_revision=parent['sha256'], corrections=[item or correction(parent, fixture.store)], attribution=fixture.actor)


def test_initial_fractional_query_and_correction_retains_raw_and_parent(model):
    f, parent = model
    initial = read_record(parent, f.store)
    view = audio_hypothesis_query(store_root=f.store, hypotheses=parent, view='annotations')
    assert view['items'][0]['annotation']['model_frame_q'] == {'n': 41, 'd': 2}
    assert len(read_record(initial['analysis'], f.store)['analysis']['projection']['excluded']) == 1
    result = correct(f, parent)
    child = result['artifacts']['hypotheses']
    assert read_record(child, f.store)['parent'] == parent
    assert read_record(parent, f.store) == initial
    rows = audio_hypothesis_query(store_root=f.store, hypotheses=child, view='annotations')['items']
    assert rows[:-1] == initial['annotations']
    assert rows[-1]['annotation']['kind'] == 'attack'
    assert rows[-1]['attribution'] == f.actor
    assert correct(f, parent) == result
    assert len(canonical_bytes(result)) < 4096


@pytest.mark.parametrize('mutate', [
    lambda x: x.update(supersedes=['stale']),
    lambda x: x.update(supersedes=x['supersedes'] * 2),
    lambda x: x['annotation'].update(source_frame=16137),
    lambda x: x['annotation'].update(source_frame=True),
    lambda x: x['annotation'].update(kind='learned_beat'),
    lambda x: x['support'][0].update(reference='/analysis/projection/events/99'),
    lambda x: x['support'][0].update(reference='/model'),
    lambda x: x['support'][0].update(kind='annotation_id', reference='stale'),
    lambda x: x.update(extra=True),
])
def test_invalid_authored_input_refuses(model, mutate):
    f, parent = model
    item = correction(parent, f.store)
    mutate(item)
    with pytest.raises(PocketError):
        correct(f, parent, item)


def test_stale_revision_and_cursor_and_bounded_pages(model):
    f, parent = model
    with pytest.raises(PocketError):
        audio_hypothesis_correct(store_root=f.store, request_id='stale', parent=parent,
            expected_revision='0' * 64, corrections=[correction(parent, f.store)], attribution=f.actor)
    child = correct(f, parent)['artifacts']['hypotheses']
    page = audio_hypothesis_query(store_root=f.store, hypotheses=child, view='annotations', limit=1, max_bytes=4096)
    assert len(canonical_bytes(page)) <= 4096 and page['next_cursor']
    next_page = audio_hypothesis_query(store_root=f.store, hypotheses=child, view='annotations', cursor=page['next_cursor'])
    assert next_page['complete'] and next_page['items'][0]['annotation']['kind'] == 'attack'
    with pytest.raises(PocketError):
        audio_hypothesis_query(store_root=f.store, hypotheses=parent, view='annotations', cursor=page['next_cursor'])


@pytest.mark.parametrize('field,value', [('revision_index', 2), ('revision_index', True), ('annotation_count', 100), ('schema', 'pocket.audio-hypotheses/v1')])
def test_resealed_child_semantics_refuse(model, field, value):
    f, parent = model
    child = correct(f, parent)['artifacts']['hypotheses']
    record = read_record(child, f.store)
    record[field] = value
    forged = put_record(record, f.store)
    with pytest.raises(PocketError):
        audio_hypothesis_query(store_root=f.store, hypotheses=forged)


def test_resealed_authored_row_and_ancestor_validate(model):
    f, parent = model
    child = correct(f, parent)['artifacts']['hypotheses']
    record = read_record(child, f.store)
    row = record['annotations'][0]
    row['attribution']['actor_kind'] = 'algorithm'
    body = {key: value for key, value in row.items() if key != 'annotation_id'}
    row['annotation_id'] = 'audio:' + digest([parent, body])
    forged = put_record(record, f.store)
    with pytest.raises(PocketError):
        audio_hypothesis_query(store_root=f.store, hypotheses=forged)
    base = copy.deepcopy(read_record(parent, f.store))
    base['annotations'][0]['annotation']['source_frame_q'] = {'n': 1, 'd': 1}
    fake_base = put_record(base, f.store)
    record = read_record(child, f.store)
    record['parent'] = fake_base
    forged = put_record(record, f.store)
    with pytest.raises(PocketError):
        audio_hypothesis_query(store_root=f.store, hypotheses=forged)


def test_prior_support_duplicate_id_and_history(model):
    f, parent = model
    child = correct(f, parent)['artifacts']['hypotheses']
    item = correction(child, f.store, 'next')
    item['support'] = [{'kind': 'annotation_id', 'reference': item['supersedes'][0]}]
    second = correct(f, child, item, 'second')['artifacts']['hypotheses']
    history = audio_hypothesis_query(store_root=f.store, hypotheses=second, view='history')
    assert [row['revision_index'] for row in history['items']] == [0, 1, 2]
    item['correction_id'] = 'correction'
    with pytest.raises(PocketError):
        correct(f, second, item, 'duplicate')


def test_empty_model_evidence_can_receive_authored_annotation():
    from test_audio_models import PulseModelTests

    f = PulseModelTests()
    f.setUp()
    try:
        with patch('pocket_music.audio_pulse_hypotheses._runner', side_effect=f.runner):
            parent = f.call()['artifacts']['hypotheses']
        initial = audio_hypothesis_query(store_root=f.store, hypotheses=parent, view='annotations')
        assert initial['items'] == [] and initial['complete']
        item = {'correction_id': 'authored', 'supersedes': [],
                'annotation': {'kind': 'phrase_anchor', 'start_frame': 137,
                               'end_frame_exclusive': 16137, 'label': 'Possible phrase'},
                'support': [{'kind': 'analysis_pointer', 'reference': '/analysis/raw/beat/0'}],
                'uncertainty': ['No model pulse detected.']}
        with patch('pocket_music.audio_pulse_hypotheses._runner', side_effect=AssertionError('inference')):
            child = correct(f, parent, item)['artifacts']['hypotheses']
            assert audio_hypothesis_query(store_root=f.store, hypotheses=child)['items'][0]['annotations'] == 1
    finally:
        f.doCleanups()


def test_revision_limit_and_all_prior_records_preserved(model):
    f, parent = model
    original = parent
    for index in range(32):
        item = correction(parent, f.store, f'fix-{index}')
        parent = correct(f, parent, item, f'revision-{index}')['artifacts']['hypotheses']
    assert audio_hypothesis_query(store_root=f.store, hypotheses=parent)['items'][0]['revision_index'] == 32
    with pytest.raises(PocketError, match='32 revisions'):
        correct(f, parent, correction(parent, f.store, 'too-many'), 'overflow')
    history = audio_hypothesis_query(store_root=f.store, hypotheses=parent, view='history', limit=128)
    assert history['items'][0]['hypotheses'] == original
    assert history['total'] == 33
