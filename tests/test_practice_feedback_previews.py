"""Preview-aware reports: exact heard preview, parent render membership and unchanged v1 records."""
import copy
import shutil

import pytest
from test_practice_envelopes import envelope_fixture

from pocket_music.artifact_store import digest, put_record, read_record
from pocket_music.errors import PocketError
from pocket_music.practice_audio import practice_compare, practice_feedback, practice_query, practice_render
from pocket_music.practice_envelopes import practice_compare_processed, practice_envelope
from pocket_music.practice_feedback_query import practice_feedback_query
from pocket_music.practice_previews import practice_preview

PROFILE = 'browser-pcm16-original-rate/v1'
NOTE = 'Synthetic attributed fixture; no listening actually performed'
V1_FIELDS = {'schema', 'comparison', 'render', 'interval_frames', 'actor', 'actor_kind', 'note', 'decision',
             'evidence_kind', 'render_sha256'}


def processed(tmp_path):
    args, source = envelope_fixture(tmp_path)
    store, raw = args['store_root'], args['render']
    joined = practice_envelope(**args)['artifacts']['render']
    comparison = practice_compare_processed(store, 'compare', raw, [joined], 'Synthetic join question')
    previews = {name: practice_preview(store, f'preview-{name}', handle, PROFILE)['artifacts']['preview']
                for name, handle in (('raw', raw), ('joined', joined))}
    return store, comparison['artifacts']['comparison'], raw, joined, previews, source


def report(store, comparison, render, interval, preview=None, request='report', kind='human', decision=None,
           note=NOTE):
    extra = {} if preview is None else {'preview': preview}
    return practice_feedback(store, request, comparison, render, interval, 'Synthetic reviewer', kind, note,
                             decision, **extra)


def test_v1_calls_keep_their_record_journal_identity_and_receipt(tmp_path):
    store, comparison, raw, _, _, _ = processed(tmp_path)
    result = report(store, comparison, raw, [100, 200], decision='keep')
    handle = result['artifacts']['feedback']
    record = read_record(handle, store)
    assert handle['artifact_schema'] == 'pocket.practice-feedback/v1' and set(record) == V1_FIELDS
    assert result['coverage'] == {'listening': 'attributed_human_listening', 'provider_playback': False}
    journal = read_record_journal(store, 'report')
    assert journal['input_sha256'] == digest({'operation': 'practice_feedback', 'inputs': {
        'comparison': comparison, 'render': raw, 'interval_frames': [100, 200], 'actor': 'Synthetic reviewer',
        'actor_kind': 'human', 'note': NOTE, 'decision': 'keep'}})
    assert report(store, comparison, raw, [100, 200], decision='keep') == result
    assert practice_feedback(store, 'report', comparison, raw, [100, 200], 'Synthetic reviewer', 'human', NOTE,
                             'keep', preview=None) == result


def read_record_journal(store, request_id):
    import json
    from pathlib import Path
    return json.loads((Path(store) / 'requests' / request_id / 'journal.json').read_text())


def test_v2_binds_heard_preview_and_maps_interval_to_parent_render(tmp_path):
    store, comparison, _, joined, previews, source = processed(tmp_path)
    preview_record = read_record(previews['joined'], store)
    result = report(store, comparison, joined, [7990, 8010], previews['joined'], decision='revise')
    handle = result['artifacts']['feedback']
    assert handle['artifact_schema'] == 'pocket.practice-feedback/v2'
    record = read_record(handle, store)
    assert record['render'] == joined and record['comparison'] == comparison
    assert record['interval_frames'] == [7990, 8010]
    assert record['render_sha256'] == read_record(joined, store)['audio']['sha256']
    assert record['reviewed_audio'] == {'kind': 'declared_preview', 'preview': previews['joined'],
                                        'preview_sha256': preview_record['audio']['sha256'], 'profile': PROFILE,
                                        'interval_frames': [7990, 8010], 'frame_mapping': 'identity'}
    assert record['evidence_kind'] == 'attributed_human_listening'
    assert result['coverage'] == {'listening': 'attributed_human_listening', 'provider_playback': False,
                                  'reviewed_audio': 'declared_preview', 'preview_profile': PROFILE}
    assert report(store, comparison, joined, [7990, 8010], previews['joined'], decision='revise') == result
    query = practice_query(store, handle)
    assert query['coverage'] == {'profile': PROFILE, 'parent_profile': 'linear-loop-join-envelope/v1',
                                 'reviewed_audio': 'declared_preview', 'provider_playback': False}
    assert query['summary'] == record
    moved = tmp_path / 'moved'
    shutil.copytree(store, moved)
    shutil.rmtree(store)
    source.unlink()
    assert practice_query(str(moved), handle) == query


def test_query_keeps_v1_and_v2_contradictions_side_by_side(tmp_path):
    store, comparison, raw, joined, previews, _ = processed(tmp_path)
    old = report(store, comparison, joined, [0, 16000], request='old', decision='keep')['artifacts']['feedback']
    heard = report(store, comparison, joined, [7990, 8010], previews['joined'], 'heard',
                   decision='reject')['artifacts']['feedback']
    agent = report(store, comparison, joined, [7990, 8010], previews['joined'], 'agent', kind='agent',
                   note='Agent technical report about the preview bytes; not listening')['artifacts']['feedback']
    baseline = report(store, comparison, raw, [0, 100], previews['raw'], 'base')['artifacts']['feedback']
    result = practice_feedback_query(store, [old, heard, agent, baseline])
    rows = result['items']
    assert [r['decision'] for r in rows] == ['keep', 'reject', None, None]
    assert 'report_schema' not in rows[0] and 'reviewed_audio' not in rows[0]
    assert set(rows[0]) == {'feedback', 'actor', 'actor_kind', 'note', 'decision', 'evidence_kind', 'interval_frames',
                            'render', 'comparison', 'render_sha256', 'sample_rate'}
    assert rows[1]['report_schema'] == 'pocket.practice-feedback/v2'
    assert rows[1]['reviewed_audio']['preview'] == previews['joined']
    assert rows[2]['evidence_kind'] == 'agent_report' and rows[2]['actor_kind'] == 'agent'
    filtered = practice_feedback_query(store, [old, heard, agent, baseline], render=joined,
                                       interval_frames=[8000, 8001])
    assert [r['feedback'] for r in filtered['items']] == [old, heard, agent]
    humans = practice_feedback_query(store, [old, heard, agent, baseline], actor_kind='human', decision='reject')
    assert [r['feedback'] for r in humans['items']] == [heard]
    assert result['coverage']['musical_preference_inference'] is False


def test_same_context_comparisons_accept_previews(tmp_path):
    args, _ = envelope_fixture(tmp_path)
    store, raw = args['store_root'], args['render']
    alt = practice_render(store, 'alt', read_record(raw, store)['context'], ['alternative'])['artifacts']['render']
    comparison = practice_compare(store, 'same', raw, [alt], 'Synthetic', True)['artifacts']['comparison']
    preview = practice_preview(store, 'pv', alt, PROFILE)['artifacts']['preview']
    handle = report(store, comparison, alt, [0, 8000], preview)['artifacts']['feedback']
    query = practice_query(store, handle)
    assert query['coverage']['parent_profile'] == 'exact-pcm-occurrences/v1'
    with pytest.raises(PocketError, match='outside'):
        report(store, comparison, alt, [0, 8001], preview, 'too-long')


@pytest.mark.parametrize('interval', [[0, 0], [10, 5], [-1, 5], [0, 16001], [True, 5], [0.0, 5]])
def test_invalid_preview_intervals_refuse(tmp_path, interval):
    store, comparison, _, joined, previews, _ = processed(tmp_path)
    with pytest.raises(PocketError):
        report(store, comparison, joined, interval, previews['joined'])


def test_wrong_parent_stale_variant_and_non_preview_handles_refuse(tmp_path):
    store, comparison, raw, joined, previews, _ = processed(tmp_path)
    with pytest.raises(PocketError, match='not the selected render') as error:
        report(store, comparison, joined, [0, 100], previews['raw'], 'stale')
    assert error.value.code == 'source_mismatch'
    with pytest.raises(PocketError, match='practice preview'):
        report(store, comparison, joined, [0, 100], joined, 'not-a-preview')
    other = practice_render(store, 'other', read_record(raw, store)['context'], ['alternative'])['artifacts']['render']
    outside = practice_preview(store, 'pv-other', other, PROFILE)['artifacts']['preview']
    with pytest.raises(PocketError, match='not in this comparison'):
        report(store, comparison, other, [0, 100], outside, 'outside')
    report(store, comparison, joined, [0, 100], previews['joined'], 'same-id')
    with pytest.raises(PocketError, match='idempotency_conflict'):
        report(store, comparison, joined, [0, 101], previews['joined'], 'same-id')
    with pytest.raises(PocketError, match='idempotency_conflict'):
        report(store, comparison, joined, [0, 100], None, 'same-id')


def forged(store, handle, change):
    record = copy.deepcopy(read_record(handle, store))
    change(record)
    return put_record(record, store)


@pytest.mark.parametrize('change', [
    lambda r: r['reviewed_audio'].update(preview_sha256='0' * 64),
    lambda r: r['reviewed_audio'].update(interval_frames=[7990, 8011]),
    lambda r: r.update(interval_frames=[7991, 8010]),
    lambda r: r['reviewed_audio'].update(frame_mapping='offset'),
    lambda r: r['reviewed_audio'].update(kind='retained_render'),
    lambda r: r['reviewed_audio'].update(profile='browser-float32/v1'),
    lambda r: r.update(evidence_kind='agent_report'),
    lambda r: r.update(actor_kind='agent'),
    lambda r: r.update(render_sha256='0' * 64),
    lambda r: r.update(decision='winner'),
    lambda r: r.update(extra=True),
    lambda r: r['reviewed_audio'].update(extra=True),
])
def test_rehashed_forged_v2_reports_refuse(tmp_path, change):
    store, comparison, _, joined, previews, _ = processed(tmp_path)
    handle = report(store, comparison, joined, [7990, 8010], previews['joined'])['artifacts']['feedback']
    bad = forged(store, handle, change)
    with pytest.raises(PocketError):
        practice_query(store, bad)
    with pytest.raises(PocketError):
        practice_feedback_query(store, [bad])


def test_forged_v2_with_other_members_preview_refuses(tmp_path):
    store, comparison, _, joined, previews, _ = processed(tmp_path)
    handle = report(store, comparison, joined, [0, 100], previews['joined'])['artifacts']['feedback']
    raw_preview = read_record(previews['raw'], store)
    def swap(record):
        record['reviewed_audio'].update(preview=previews['raw'], preview_sha256=raw_preview['audio']['sha256'])
    with pytest.raises(PocketError, match='not the selected render'):
        practice_query(store, forged(store, handle, swap))
