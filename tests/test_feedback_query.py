# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic attribution only; fixtures establish no actual native or listening evidence."""
import shutil
from pathlib import Path

import pytest
from test_auditions import attachment_args

from pocket_music.artifact_store import canonical_bytes, put_record, read_record
from pocket_music.auditions import attach_candidate_render, audition_feedback
from pocket_music.errors import PocketError
from pocket_music.feedback_query import audition_feedback_query


@pytest.fixture
def evidence(tmp_path):
    args, _ = attachment_args(tmp_path)
    attachment = attach_candidate_render(**args)['artifacts']['attachment']
    handles = [audition_feedback(args['store_root'], f'feedback-{i}', attachment, [i * 100, (i + 1) * 100],
               'Synthetic reviewer', kind, 'Synthetic fixture; no actual listening', decision)['artifacts']['feedback']
               for i, (kind, decision) in enumerate([('agent', 'keep'), ('human', 'revise'), ('agent', 'no_addition')])]
    return args['store_root'], handles, args


def test_exact_filters_input_order_half_open_and_readonly(evidence):
    store, handles, args = evidence
    original = {p: p.read_bytes() for p in Path(store).rglob('*') if p.is_file()}
    query = audition_feedback_query(store_root=store, feedback=handles)
    assert [r['feedback'] for r in query['items']] == handles
    assert [r['sample_rate'] for r in query['items']] == [8000] * 3
    assert [r['evidence_kind'] for r in query['items']] == ['agent_report', 'attributed_human_listening', 'agent_report']
    assert audition_feedback_query(store_root=store, feedback=handles, actor_kind='human')['items'][0]['decision'] == 'revise'
    assert audition_feedback_query(store_root=store, feedback=handles, decision='no_addition')['total'] == 1
    assert audition_feedback_query(store_root=store, feedback=handles, actor='synthetic reviewer')['total'] == 0
    selected = audition_feedback_query(store_root=store, feedback=handles, render_sha256=args['expected_sha256'], interval_frames=[100, 200])
    assert [r['feedback'] for r in selected['items']] == [handles[1]]
    assert {p: p.read_bytes() for p in Path(store).rglob('*') if p.is_file()} == original


@pytest.mark.parametrize('changes', [{'feedback': []}, {'feedback': 'scan'}, {'actor_kind': []}, {'decision': {}},
    {'limit': True}, {'max_bytes': 4095}, {'interval_frames': [0, 1]}, {'render_sha256': 'A' * 64},
    {'interval_frames': [0, True], 'render_sha256': 'a' * 64}, {'actor': ' '}])
def test_invalid_inputs_refuse(evidence, changes):
    store, handles, _ = evidence
    with pytest.raises(PocketError): audition_feedback_query(**{'store_root': store, 'feedback': handles, **changes})


def test_duplicate_and_stale_cursors(evidence):
    store, handles, _ = evidence
    with pytest.raises(PocketError): audition_feedback_query(store_root=store, feedback=handles + handles[:1])
    page = audition_feedback_query(store_root=store, feedback=handles, limit=1)
    cursor = page['next_cursor']
    assert page['omitted'] == 2
    assert audition_feedback_query(store_root=store, feedback=handles, cursor=cursor)['items'][0]['feedback'] == handles[1]
    for change in ({'feedback': handles[::-1]}, {'actor_kind': 'agent'}, {'feedback': handles[:2]}):
        with pytest.raises(PocketError): audition_feedback_query(**{'store_root': store, 'feedback': handles, 'cursor': cursor, **change})


@pytest.mark.parametrize('field,value', [('evidence_kind', 'attributed_human_listening'), ('candidate', None),
    ('render_sha256', 'f' * 64), ('actor_kind', 'algorithm'), ('note', None), ('interval_frames', [0, 1000000]),
    ('decision', 'approved'), ('extra', True)])
def test_resealed_false_feedback_refuses_even_filtered_out(evidence, field, value):
    store, handles, _ = evidence
    record = read_record(handles[0], store); record[field] = value
    forged = put_record(record, store)
    with pytest.raises(PocketError): audition_feedback_query(store_root=store, feedback=[forged], actor='not this actor')


def test_response_budget_needs_input_and_full_note_retained(evidence):
    store, handles, _ = evidence
    record = read_record(handles[0], store); record['note'] = 'x' * 7900
    long = put_record(record, store)
    small = audition_feedback_query(store_root=store, feedback=[long], max_bytes=4096)
    assert small['status'] == 'needs_input' and not small['items'] and small['omitted'] == 1
    assert len(canonical_bytes(small)) <= 4096
    full = audition_feedback_query(store_root=store, feedback=[long], cursor=small['next_cursor'], max_bytes=16384)
    assert full['complete'] and full['items'][0]['note'] == 'x' * 7900


def test_artifact_only_relocation_and_transitive_corruption(evidence, tmp_path):
    store, handles, args = evidence
    moved = tmp_path / 'relocated'
    shutil.copytree(Path(store) / 'artifacts', moved / 'artifacts')
    Path(args['path']).unlink()
    result = audition_feedback_query(store_root=str(moved), feedback=handles)
    assert result['total'] == 3
    attachment = read_record(read_record(handles[0], str(moved))['attachment'], str(moved))
    (moved / attachment['audio']['artifact_uri']).write_bytes(b'wrong render bytes')
    with pytest.raises(PocketError): audition_feedback_query(store_root=str(moved), feedback=handles)


@pytest.mark.parametrize('handle', [None, {}, {'artifact_schema': 'pocket.audition-feedback/v1'},
    {'schema': 'pocket.artifact-handle/v1', 'artifact_schema': 'pocket.audition-feedback/v1', 'sha256': []}])
def test_malformed_handle_is_domain_error(tmp_path, handle):
    with pytest.raises(PocketError): audition_feedback_query(store_root=str(tmp_path), feedback=[handle])
