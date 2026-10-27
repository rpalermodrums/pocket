# SPDX-License-Identifier: AGPL-3.0-only
"""Independent synthetic feedback retrieval QA; no actual export or listening evidence."""
import copy
import hashlib
import shutil
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import soundfile as sf
from test_auditions import attachment_args

import pocket_music.feedback_query as query_module
from pocket_music.artifact_store import canonical_bytes, put_bytes, put_record, read_record
from pocket_music.auditions import attach_candidate_render, audition_feedback
from pocket_music.errors import PocketError
from pocket_music.feedback_query import audition_feedback_query


@pytest.fixture(scope='module')
def qa_feedback(tmp_path_factory):
    root = tmp_path_factory.mktemp('feedback-query-independent-synthetic')
    args, _ = attachment_args(root)
    first = attach_candidate_render(**args)['artifacts']['attachment']
    # Independent float audio with identical basename; filename cannot establish identity.
    second_path = root / 'second' / Path(args['path']).name
    second_path.parent.mkdir()
    plan = read_record(args['render_plan'], args['store_root'])
    sf.write(second_path, np.full((plan['expected_frames'], 2), -.025, dtype='float64'), 8000, subtype='FLOAT')
    other = {**args, 'request_id': 'second-render', 'path': str(second_path),
             'expected_sha256': hashlib.sha256(second_path.read_bytes()).hexdigest()}
    second = attach_candidate_render(**other)['artifacts']['attachment']
    requests = [('A', 'agent', 'keep', first, [0, 101]),
                ('A', 'human', None, first, [101, 202]),
                ('a', 'agent', 'reject', second, [0, 101]),
                ('A', 'agent', 'no_addition', second, [101, 202])]
    handles = [audition_feedback(args['store_root'], f'qa-feedback-{i}', attachment, interval,
                                actor, kind, 'SYNTHETIC QA attribution; no actual listening occurred', decision)['artifacts']['feedback']
               for i, (actor, kind, decision, attachment, interval) in enumerate(requests)]
    return {'store': args['store_root'], 'handles': handles, 'attachments': [first, second],
            'audio_ids': [args['expected_sha256'], other['expected_sha256']], 'args': args}


def query(fixture, **options):
    return audition_feedback_query(**{'store_root': fixture['store'], 'feedback': fixture['handles'], **options})


def reseal_attachment(fixture, attachment_change):
    store = fixture['store']
    record = copy.deepcopy(read_record(fixture['attachments'][0], store))
    attachment_change(record)
    changed = put_record(record, store)
    feedback = read_record(fixture['handles'][0], store)
    feedback['attachment'] = changed
    return put_record(feedback, store)


def test_qa_identical_filename_different_audio_and_exact_attribution(qa_feedback):
    result = query(qa_feedback)
    assert qa_feedback['audio_ids'][0] != qa_feedback['audio_ids'][1]
    assert [row['feedback'] for row in result['items']] == qa_feedback['handles']
    assert [row['actor'] for row in result['items']] == ['A', 'A', 'a', 'A']
    assert [row['decision'] for row in result['items']] == ['keep', None, 'reject', 'no_addition']
    for row in result['items']:
        assert 'SYNTHETIC QA' in row['note']
        assert row['sample_rate'] == 8000
    filtered = query(qa_feedback, render_sha256=qa_feedback['audio_ids'][1])
    assert [row['feedback'] for row in filtered['items']] == qa_feedback['handles'][2:]
    assert query(qa_feedback, actor='a')['total'] == 1
    assert query(qa_feedback, actor='A', actor_kind='human')['items'][0]['decision'] is None
    assert result['coverage']['provider_playback'] is False
    assert result['coverage']['musical_preference_inference'] is False


@pytest.mark.parametrize('span,expected', [([100, 101], [0]), ([101, 102], [1]),
    ([100, 102], [0, 1]), ([202, 203], []), ([0, 202], [0, 1])])
def test_qa_half_open_filter_only_on_exact_audio(qa_feedback, span, expected):
    result = query(qa_feedback, render_sha256=qa_feedback['audio_ids'][0], interval_frames=span)
    assert [row['feedback'] for row in result['items']] == [qa_feedback['handles'][i] for i in expected]


@pytest.mark.parametrize('field,value', [('candidate_sha256', 'f' * 64), ('listening', 'reviewed'),
    ('musical_verdict', 'human_keep'), ('actual_settings', {}), ('signal', {'frames': 56000}),
    ('native_evidence', []), ('candidate', None), ('render_plan', None)])
def test_qa_resealed_false_attachment_refused_even_excluded(qa_feedback, field, value):
    forged = reseal_attachment(qa_feedback, lambda record: record.__setitem__(field, value))
    with pytest.raises(PocketError):
        query(qa_feedback, feedback=[*qa_feedback['handles'][2:], forged], render_sha256=qa_feedback['audio_ids'][1])


def test_qa_resealed_candidate_binding_from_other_render_refused(qa_feedback):
    record = read_record(qa_feedback['handles'][0], qa_feedback['store'])
    record['attachment'] = qa_feedback['attachments'][1]
    forged = put_record(record, qa_feedback['store'])
    with pytest.raises(PocketError, match='binding'):
        query(qa_feedback, feedback=[forged])


def test_qa_same_feedback_bytes_under_alias_still_duplicate(qa_feedback):
    record = read_record(qa_feedback['handles'][0], qa_feedback['store'])
    alias = put_bytes(canonical_bytes(record), qa_feedback['store'], 'alias.json', 'pocket.audition-feedback/v1')
    assert alias['sha256'] == qa_feedback['handles'][0]['sha256']
    assert alias['artifact_uri'] != qa_feedback['handles'][0]['artifact_uri']
    with pytest.raises(PocketError, match='Duplicate'):
        query(qa_feedback, feedback=[alias, qa_feedback['handles'][0]])


@pytest.mark.parametrize('field,value', [('actor', ''), ('actor_kind', []), ('evidence_kind', 'rendered_audio'),
    ('interval_frames', [-1, 1]), ('interval_frames', [1, 1]), ('interval_frames', [False, 5]),
    ('interval_frames', [0, 2**100]), ('decision', []), ('note', 'x' * 8001)])
def test_qa_resealed_feedback_malformed_domain_errors(qa_feedback, field, value):
    record = read_record(qa_feedback['handles'][0], qa_feedback['store'])
    record[field] = value
    forged = put_record(record, qa_feedback['store'])
    with pytest.raises(PocketError):
        query(qa_feedback, feedback=[forged], actor='filtered-out')


def test_qa_cursor_binds_input_and_filters_but_not_page_budget(qa_feedback):
    first = query(qa_feedback, limit=1, max_bytes=4096)
    second = query(qa_feedback, cursor=first['next_cursor'], limit=2, max_bytes=8192)
    assert [row['feedback'] for row in second['items']] == qa_feedback['handles'][1:3]
    for changed in ({'actor': 'A'}, {'decision': 'keep'}, {'actor_kind': 'agent'},
                    {'render_sha256': qa_feedback['audio_ids'][0]}, {'feedback': qa_feedback['handles'][::-1]}):
        with pytest.raises(PocketError):
            query(qa_feedback, cursor=first['next_cursor'], **changed)
    with pytest.raises(PocketError):
        query(qa_feedback, cursor=first['next_cursor'].split(':')[0] + ':999')


def test_qa_budget_preserves_unicode_and_progress(qa_feedback):
    record = read_record(qa_feedback['handles'][0], qa_feedback['store'])
    record['note'] = '🎹' * 7900
    long = put_record(record, qa_feedback['store'])
    result = query(qa_feedback, feedback=[long], max_bytes=4096)
    assert result['status'] == 'needs_input' and result['items'] == []
    assert len(canonical_bytes(result)) <= 4096 and result['next_cursor'].endswith(':0')
    repeated = query(qa_feedback, feedback=[long], max_bytes=4096, cursor=result['next_cursor'])
    assert repeated['next_cursor'] == result['next_cursor']
    full = query(qa_feedback, feedback=[long], max_bytes=65536, cursor=result['next_cursor'])
    assert full['complete'] and full['items'][0]['note'] == record['note']
    assert len(canonical_bytes(full)) <= 65536


def test_qa_attachment_cache_does_not_skip_feedback_validation(qa_feedback):
    with patch.object(query_module, '_validate_attachment', wraps=query_module._validate_attachment) as validate:
        query(qa_feedback)
        assert validate.call_count == 2
    record = read_record(qa_feedback['handles'][1], qa_feedback['store'])
    record['evidence_kind'] = 'agent_report'
    forged = put_record(record, qa_feedback['store'])
    with pytest.raises(PocketError):
        query(qa_feedback, feedback=[qa_feedback['handles'][0], forged], decision='keep')


def test_qa_artifact_only_relocation_without_mutation_and_filtered_audio_corruption(qa_feedback, tmp_path):
    moved = tmp_path / 'artifacts-only'
    shutil.copytree(Path(qa_feedback['store']) / 'artifacts', moved / 'artifacts')
    before = {str(p.relative_to(moved)): hashlib.sha256(p.read_bytes()).hexdigest() for p in moved.rglob('*') if p.is_file()}
    original_root = Path(qa_feedback['args']['path']).parent
    offline_root = original_root.with_name(original_root.name + '-offline')
    original_root.rename(offline_root)
    try:
        result = query(qa_feedback, store_root=str(moved))
    finally:
        offline_root.rename(original_root)
    assert result['total'] == 4 and not (moved / 'requests').exists()
    assert before == {str(p.relative_to(moved)): hashlib.sha256(p.read_bytes()).hexdigest() for p in moved.rglob('*') if p.is_file()}
    attachment = read_record(qa_feedback['attachments'][1], str(moved))
    (moved / attachment['audio']['artifact_uri']).write_bytes(b'corrupt excluded audio')
    with pytest.raises(PocketError):
        query(qa_feedback, store_root=str(moved), render_sha256=qa_feedback['audio_ids'][0])


def test_qa_handle_count_bound_without_store_scan(qa_feedback):
    with pytest.raises(PocketError, match='1–128'):
        query(qa_feedback, feedback=qa_feedback['handles'] * 33)
    single = query(qa_feedback, feedback=qa_feedback['handles'][:1])
    assert single['input_total'] == 1 and single['total'] == 1
