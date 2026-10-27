# SPDX-License-Identifier: AGPL-3.0-only
"""Independent publication, immutable input and competing cancellation expectations."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import wave
from pathlib import Path

import pytest

from pocket_music import audio_hypothesis_jobs as jobs
from pocket_music.artifact_store import read_record
from pocket_music.errors import PocketError

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Qualified POSIX lease profile only')


def fixture(tmp_path, monkeypatch, *, frozen=True):
    path = tmp_path / 'independent.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b'\0\0' * 4000)
    args = {'store_root': str(tmp_path / 'store'), 'request_id': 'independent-job',
            'source': {'path': str(path), 'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                       'start_frame': 800, 'frames': 1600, 'source_origin': 'user_recording'},
            'settings': {'bpm_hint': None, 'beats_per_bar': 4},
            'attribution': {'actor': 'Independent QA', 'actor_kind': 'agent',
                            'statement': 'Synthetic silence; no musical judgment.', 'uncertainty': ['Unheard.']}}
    invocation = {}
    def defer(root, job_id, nonce, lease_fd):
        assert not invocation
        invocation.update(root=root, job_id=job_id, nonce=nonce, lease_fd=os.dup(lease_fd))
    if frozen:
        monkeypatch.setattr(jobs, '_spawn', defer)
    submitted = jobs.audio_hypothesis_submit(**args)
    return args, submitted, invocation


def inspect(args, submitted):
    return jobs.job_status(args['store_root'], submitted['job']['job_id'])


def test_qa_worker_uses_captured_bytes_after_external_file_replacement(tmp_path, monkeypatch):
    args, submitted, invocation = fixture(tmp_path, monkeypatch)
    Path(args['source']['path']).write_bytes(b'unrelated replacement')
    jobs._run_worker(**invocation)
    status = inspect(args, submitted)
    assert status['state'] == 'completed'
    proof = read_record(status['result'], args['store_root'])
    assert proof['source']['start_frame'] == 800 and proof['source']['frames'] == 4000
    assert proof['source']['end_frame_exclusive'] == 2400
    assert proof['source']['sha256'] == args['source']['expected_sha256']
    assert Path(args['source']['path']).read_bytes() == b'unrelated replacement'
    assert jobs.audio_hypothesis_submit(**args) == submitted
    assert len(json.dumps(status, indent=2).encode()) <= 16384


def test_qa_two_cancels_same_revision_have_one_accepted_transition(tmp_path, monkeypatch):
    args, submitted, invocation = fixture(tmp_path, monkeypatch)
    barrier = threading.Barrier(2)
    results = []
    def cancel():
        barrier.wait()
        try:
            results.append(jobs.job_cancel(args['store_root'], submitted['job']['job_id'], submitted['revision']))
        except PocketError as error:
            results.append(error)
    threads = [threading.Thread(target=cancel) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert sum(isinstance(row, dict) and row['status'] == 'ok' for row in results) == 1
    assert all((isinstance(row, dict) and row['status'] in ('ok', 'conflict'))
               or isinstance(row, PocketError) and 'busy' in str(row) for row in results)
    jobs._run_worker(**invocation)
    final = inspect(args, submitted)
    assert final['state'] == 'cancelled' and final['result'] is None
    assert jobs.audio_hypothesis_submit(**args) == submitted


def test_qa_partial_collection_failure_has_no_committed_result_or_retry(tmp_path, monkeypatch):
    args, submitted, invocation = fixture(tmp_path, monkeypatch)
    original = jobs._collect_graph
    def collect_then_fail(result, staging, root):
        original(result, staging, root)
        raise OSError('Independent fault before journal commit')
    monkeypatch.setattr(jobs, '_collect_graph', collect_then_fail)
    with pytest.raises(OSError, match='before journal'):
        jobs._run_worker(**invocation)
    final = inspect(args, submitted)
    assert final['state'] == 'failed' and final['result'] is None
    assert list((Path(args['store_root']) / 'artifacts').glob('*/record.json'))
    assert jobs.audio_hypothesis_submit(**args) == submitted
    assert inspect(args, submitted)['state'] == 'failed'


def test_qa_worker_nonce_mismatch_cannot_execute_or_claim_completion(tmp_path, monkeypatch):
    args, submitted, invocation = fixture(tmp_path, monkeypatch)
    invocation['nonce'] = '0' * 64
    with pytest.raises(PocketError, match='ownership'):
        jobs._run_worker(**invocation)
    final = inspect(args, submitted)
    assert final['state'] == 'interrupted' and final['result'] is None
    assert not (Path(args['store_root']) / 'jobs' / submitted['job']['job_id'] / 'staging').exists()


def test_qa_execution_lease_does_not_claim_progress_and_is_not_inheritable(tmp_path, monkeypatch):
    import pocket_music.audio_hypotheses as primitive
    args, submitted, invocation = fixture(tmp_path, monkeypatch)
    original = primitive.audio_hypotheses
    def observe(**kwargs):
        assert not os.get_inheritable(invocation['lease_fd'])
        status = inspect(args, submitted)
        assert status['state'] == 'running' and status['result'] is None
        assert status['coverage']['lease_means'] == 'execution_ownership_not_progress_or_decoder_liveness'
        return original(**kwargs)
    monkeypatch.setattr(primitive, 'audio_hypotheses', observe)
    jobs._run_worker(**invocation)
    assert inspect(args, submitted)['state'] == 'completed'


def test_qa_real_worker_nonzero_crop_completes_without_polling_progress_claim(tmp_path, monkeypatch):
    args, submitted, _ = fixture(tmp_path, monkeypatch, frozen=False)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            current = inspect(args, submitted)
        except PocketError as error:
            if 'busy' not in str(error):
                raise
        else:
            if current['state'] in {'completed', 'cancelled', 'failed', 'interrupted'}:
                break
        time.sleep(.05)
    else:
        pytest.fail('Independent actual worker did not reach terminal state within fixture budget')
    assert current['state'] == 'completed', current
    proof = read_record(current['result'], args['store_root'])
    assert proof['source']['start_frame'] == 800
    assert proof['source']['end_frame_exclusive'] == 2400
    assert current['coverage']['automatic_retry'] is False
    assert jobs.audio_hypothesis_submit(**args) == submitted


@pytest.mark.parametrize('field,value', [
    ('state', []), ('history', [{'state': {}, 'phase': 'invalid', 'at': 'now'}]),
    ('result', []), ('result', {'schema': 'pocket.operation-receipt/v1', 'status': 'ok',
                             'artifacts': {'hypotheses': []}}),
])
def test_qa_malformed_self_hashed_journal_refuses_with_domain_error(tmp_path, monkeypatch, field, value):
    from pocket_music.artifact_store import digest
    args, submitted, invocation = fixture(tmp_path, monkeypatch)
    os.close(invocation['lease_fd'])
    path = Path(args['store_root']) / 'jobs' / submitted['job']['job_id'] / 'journal.json'
    journal = json.loads(path.read_bytes())
    journal[field] = value
    if field == 'result':
        journal['state'] = 'completed'
    journal['revision_sha256'] = digest({key: item for key, item in journal.items() if key != 'revision_sha256'})
    path.write_text(json.dumps(journal))
    with pytest.raises(PocketError):
        inspect(args, submitted)
