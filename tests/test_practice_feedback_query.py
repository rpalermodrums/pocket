"""Scoped contradictory reports remain attributed facts, without inferred preference."""
import builtins
from pathlib import Path

import pytest
from test_practice_revisions import comparison_fixture

from pocket_music.artifact_store import canonical_bytes, put_record, read_record
from pocket_music.errors import PocketError
from pocket_music.practice_audio import practice_feedback
from pocket_music.practice_comparisons import practice_compare_revisions
from pocket_music.practice_feedback_query import practice_feedback_query


def reports(tmp_path):
    _, args = comparison_fixture(tmp_path)
    comparison = practice_compare_revisions(**args)['artifacts']['comparison']
    render = args['variants'][0]
    store = args['store_root']
    handles = [practice_feedback(store, f'report-{i}', comparison, render, [i*100, (i+1)*100],
        'Synthetic reviewer', kind, 'Synthetic attributed fixture; no listening actually performed', decision)['artifacts']['feedback']
        for i, (kind, decision) in enumerate([('human', 'keep'), ('human', 'reject'), ('agent', 'revise')])]
    return store, handles, render


def test_original_reports_half_open_filter_and_readonly(tmp_path):
    store, handles, render = reports(tmp_path)
    before = {p: p.read_bytes() for p in Path(store).rglob('*') if p.is_file()}
    result = practice_feedback_query(store, handles)
    assert [r['decision'] for r in result['items']] == ['keep', 'reject', 'revise']
    assert result['coverage']['musical_preference_inference'] is False
    assert practice_feedback_query(store, handles, actor_kind='human')['total'] == 2
    queried = practice_feedback_query(store, handles, render=render, interval_frames=[100, 200])
    assert len(queried['items']) == 1
    assert queried['items'][0]['feedback'] == handles[1]
    assert queried['items'][0]['interval_frames'] == [100, 200]
    assert before == {p: p.read_bytes() for p in Path(store).rglob('*') if p.is_file()}


def test_cursor_identity_and_full_note_budget(tmp_path):
    store, handles, render = reports(tmp_path)
    first = practice_feedback_query(store, handles, limit=1)
    assert first['omitted'] == 2
    second = practice_feedback_query(store, handles, cursor=first['next_cursor'])
    assert [r['feedback'] for r in second['items']] == handles[1:]
    for changes in ({'feedback': handles[::-1]}, {'actor_kind': 'human'}, {'render': render}):
        with pytest.raises(PocketError, match='cursor'):
            practice_feedback_query(**{'store_root': store, 'feedback': handles, 'cursor': first['next_cursor'], **changes})
    record = read_record(handles[0], store)
    record['note'] = 'x'*7900
    long = put_record(record, store)
    small = practice_feedback_query(store, [long], max_bytes=4096)
    assert small['status'] == 'needs_input' and len(canonical_bytes(small)) <= 4096
    full = practice_feedback_query(store, [long], cursor=small['next_cursor'], max_bytes=16384)
    assert full['items'][0]['note'] == record['note']


@pytest.mark.parametrize('changes', [{'feedback': []}, {'actor': ''}, {'actor_kind': []}, {'decision': 'like'},
    {'limit': True}, {'max_bytes': 4095}, {'interval_frames': [0, 100]}])
def test_invalid_filters_refuse(tmp_path, changes):
    store, handles, _ = reports(tmp_path)
    with pytest.raises(PocketError):
        practice_feedback_query(**{'store_root': store, 'feedback': handles, **changes})


def test_rehashed_false_feedback_fails_even_when_filtered_out(tmp_path):
    store, handles, _ = reports(tmp_path)
    record = read_record(handles[0], store)
    record['render_sha256'] = '0'*64
    with pytest.raises(PocketError, match='identity mismatch'):
        practice_feedback_query(store, [put_record(record, store)], actor='nobody')


def test_exact_render_filter_does_not_conflate_identical_pcm(tmp_path):
    from test_context_edits import shift
    from test_musical_context import q

    from pocket_music.context_edits import context_edit
    from pocket_music.practice_audio import practice_render
    f, args = comparison_fixture(tmp_path)
    store, _, definition, _, _ = f
    baseline_context = read_record(args['baseline'], store)['context']
    moved = context_edit(store, 'same-pcm', baseline_context, [shift(['first', 'again'], q(-1))], [], definition['attribution'])
    render = practice_render(store, 'same-pcm-render', moved['artifacts']['context'], ['first', 'again'])['artifacts']['render']
    # Container metadata may include a write timestamp. Reuse the same verified
    # audio artifact to exercise identical bytes under distinct render identities.
    same_audio = read_record(render, store)
    same_audio['audio'] = read_record(args['baseline'], store)['audio']
    render = put_record(same_audio, store)
    comparison = practice_compare_revisions(**{**args, 'request_id': 'same-pcm-compare', 'variants': [render],
        'edit_receipts': [moved['artifacts']['edit']],
        'correspondence': [{'variant': render, 'pairs': args['correspondence'][0]['pairs']}]})['artifacts']['comparison']
    report = practice_feedback(store, 'same-pcm-report', comparison, args['baseline'], [0, 8000], 'Fixture', 'agent',
                               'Baseline report')['artifacts']['feedback']
    assert read_record(args['baseline'], store)['audio'] == read_record(render, store)['audio']
    assert practice_feedback_query(store, [report], render=render)['total'] == 0
    assert practice_feedback_query(store, [report], render=args['baseline'])['total'] == 1


def test_standalone_query_does_not_import_native_adapters(tmp_path, monkeypatch):
    store, handles, _ = reports(tmp_path)
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if any(part in name for part in ('native_candidates', 'auditions', 'instruments', 'baste')):
            raise AssertionError('Unrelated native import '+name)
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    assert practice_feedback_query(store, handles)['total'] == 3
