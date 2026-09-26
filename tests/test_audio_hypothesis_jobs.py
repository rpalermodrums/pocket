# SPDX-License-Identifier: AGPL-3.0-only
"""Owned worker behavior, real subprocess completion and publication cancellation."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
import wave
from pathlib import Path

import pytest

from pocket_music import audio_hypothesis_jobs as jobs
from pocket_music.artifact_store import digest, read_record
from pocket_music.audio_hypotheses import audio_hypotheses
from pocket_music.errors import PocketError

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Explicit POSIX worker profile')


def arguments(tmp_path, request='submit'):
    source = tmp_path / 'source.wav'
    if not source.exists():
        with wave.open(str(source), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(8000)
            stream.writeframes(b'\0\0' * 2000)
    return {'store_root': str(tmp_path / 'store'), 'request_id': request,
            'source': {'path': str(source), 'expected_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                       'start_frame': 0, 'frames': 2000, 'source_origin': 'user_recording'},
            'settings': {'bpm_hint': None, 'beats_per_bar': 4},
            'attribution': {'actor': 'Synthetic fixture', 'actor_kind': 'agent',
                            'statement': 'Silence evidence only.', 'uncertainty': ['No listening.']}}


def frozen_launch(monkeypatch):
    captured = {}

    def spawn(root, job_id, nonce, lease_fd):
        captured.update(root=root, job_id=job_id, nonce=nonce, lease_fd=os.dup(lease_fd))
    monkeypatch.setattr(jobs, '_spawn', spawn)
    return captured


def status(args, submitted):
    return jobs.job_status(args['store_root'], submitted['job']['job_id'])


def wait_terminal(args, submitted):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            current = status(args, submitted)
        except PocketError as error:
            if 'busy' not in str(error):
                raise
        else:
            if current['state'] in jobs._TERMINAL:
                return current
        time.sleep(.03)
    pytest.fail('Owned fixture worker failed to reach a terminal boundary')


def test_actual_process_completes_with_portable_same_public_result(tmp_path):
    args = arguments(tmp_path)
    original = Path(args['source']['path']).read_bytes()
    submitted = jobs.audio_hypothesis_submit(**args)
    observed = wait_terminal(args, submitted)
    assert observed['state'] == 'completed', observed
    direct = audio_hypotheses(**{**args, 'request_id': 'direct', 'store_root': str(tmp_path / 'direct')})
    assert observed['result'] == direct['artifacts']['hypotheses']
    proof = read_record(observed['result'], args['store_root'])
    assert proof['source']['start_frame'] == 0
    assert Path(args['source']['path']).read_bytes() == original
    assert jobs.audio_hypothesis_submit(**args) == submitted  # Submission replay never relaunches.
    late = jobs.job_cancel(args['store_root'], submitted['job']['job_id'], observed['revision'])
    assert late['status'] == 'conflict' and late['state'] == 'completed'
    assert len((json.dumps(observed, indent=2) + '\n').encode()) <= 16384


def test_cancel_before_worker_analysis_leaves_no_result(tmp_path, monkeypatch):
    captured = frozen_launch(monkeypatch)
    args = arguments(tmp_path)
    submitted = jobs.audio_hypothesis_submit(**args)
    current = status(args, submitted)
    assert current['state'] == 'queued'
    cancelled = jobs.job_cancel(args['store_root'], submitted['job']['job_id'], current['revision'])
    assert cancelled['state'] == 'cancel_requested' and cancelled['result'] is None
    stale = jobs.job_cancel(args['store_root'], submitted['job']['job_id'], current['revision'])
    assert stale['status'] == 'conflict'
    jobs._run_worker(**captured)
    final = status(args, submitted)
    assert final['state'] == 'cancelled' and final['result'] is None
    assert not (Path(args['store_root']) / 'jobs' / submitted['job']['job_id'] / 'staging').exists()


def test_cancel_during_analysis_waits_for_boundary_and_keeps_staging_private(tmp_path, monkeypatch):
    import pocket_music.audio_hypotheses as primitive
    captured = frozen_launch(monkeypatch)
    args = arguments(tmp_path)
    submitted = jobs.audio_hypothesis_submit(**args)
    begun, resume = threading.Event(), threading.Event()
    real = primitive.audio_hypotheses

    def bounded_analysis(**kwargs):
        begun.set()
        assert resume.wait(5)
        return real(**kwargs)
    monkeypatch.setattr(primitive, 'audio_hypotheses', bounded_analysis)
    errors = []

    def run():
        try:
            jobs._run_worker(**captured)
        except BaseException as error:  # noqa: BLE001 - surface thread failures in the parent test
            errors.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    assert begun.wait(5)
    current = status(args, submitted)
    pending = jobs.job_cancel(args['store_root'], submitted['job']['job_id'], current['revision'])
    assert pending['state'] == 'cancel_requested'
    resume.set()
    thread.join(10)
    assert not thread.is_alive() and not errors
    final = status(args, submitted)
    assert final['state'] == 'cancelled' and final['result'] is None
    main_records = list((Path(args['store_root']) / 'artifacts').glob('*/record.json'))
    assert not main_records
    assert list((Path(args['store_root']) / 'jobs' / submitted['job']['job_id'] / 'staging/artifacts').glob('*/record.json'))


def test_released_execution_lease_reconciles_interrupted_without_dispatch(tmp_path, monkeypatch):
    captured = frozen_launch(monkeypatch)
    args = arguments(tmp_path)
    submitted = jobs.audio_hypothesis_submit(**args)
    os.close(captured['lease_fd'])  # Models an owner ending before its terminal commit.
    observed = status(args, submitted)
    assert observed['state'] == 'interrupted' and observed['result'] is None
    assert jobs.audio_hypothesis_submit(**args) == submitted
    assert status(args, submitted)['state'] == 'interrupted'
    changed = copy.deepcopy(args)
    changed['settings']['beats_per_bar'] = 3
    with pytest.raises(PocketError, match='idempotency_conflict'):
        jobs.audio_hypothesis_submit(**changed)


def test_launch_failure_retains_failed_job_and_request(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError('Synthetic launch failure')
    monkeypatch.setattr(jobs, '_spawn', fail)
    args = arguments(tmp_path)
    with pytest.raises(OSError, match='launch'):
        jobs.audio_hypothesis_submit(**args)
    folder, = (Path(args['store_root']) / 'jobs').iterdir()
    result = jobs.job_status(args['store_root'], folder.name)
    assert result['state'] == 'failed' and result['result'] is None
    with pytest.raises(PocketError, match='did not complete'):
        jobs.audio_hypothesis_submit(**args)


@pytest.mark.parametrize('error', [RuntimeError('Synthetic decoder failure'), KeyboardInterrupt('Synthetic interruption')])
def test_worker_failures_never_publish_result(tmp_path, monkeypatch, error):
    import pocket_music.audio_hypotheses as primitive
    captured = frozen_launch(monkeypatch)
    args = arguments(tmp_path)
    submitted = jobs.audio_hypothesis_submit(**args)

    def fail(**kwargs):
        raise error
    monkeypatch.setattr(primitive, 'audio_hypotheses', fail)
    with pytest.raises(type(error)):
        jobs._run_worker(**captured)
    final = status(args, submitted)
    assert final['state'] == ('failed' if isinstance(error, Exception) else 'interrupted')
    assert final['result'] is None


@pytest.mark.parametrize('job_id', ['', '../job-abc', 'job-' + 'a' * 39, True])
def test_malformed_job_identity(tmp_path, job_id):
    with pytest.raises(PocketError, match='identity'):
        jobs.job_status(str(tmp_path), job_id)


def test_journal_integrity_and_source_tamper_refuse(tmp_path, monkeypatch):
    captured = frozen_launch(monkeypatch)
    args = arguments(tmp_path)
    submitted = jobs.audio_hypothesis_submit(**args)
    os.close(captured['lease_fd'])
    journal = Path(args['store_root']) / 'jobs' / submitted['job']['job_id'] / 'journal.json'
    record = json.loads(journal.read_bytes())
    original = journal.read_bytes()
    record['state'] = 'cancel_requested'
    journal.write_text(json.dumps(record))
    with pytest.raises(PocketError, match='revision integrity'):
        status(args, submitted)
    journal.write_bytes(original)
    source = Path(args['store_root']) / record['source']['artifact_uri']
    source.write_bytes(b'changed')
    with pytest.raises(PocketError, match='integrity'):
        status(args, submitted)


def test_busy_commit_cannot_confirm_cancellation(tmp_path, monkeypatch):
    captured = frozen_launch(monkeypatch)
    args = arguments(tmp_path)
    submitted = jobs.audio_hypothesis_submit(**args)
    try:
        with (jobs._state_lock(Path(args['store_root']), submitted['job']['job_id']),
              pytest.raises(PocketError, match='busy')):
            jobs.job_cancel(args['store_root'], submitted['job']['job_id'], submitted['revision'])
    finally:
        os.close(captured['lease_fd'])


def test_self_consistent_oversized_journal_fields_refuse(tmp_path, monkeypatch):
    captured = frozen_launch(monkeypatch)
    args = arguments(tmp_path)
    submitted = jobs.audio_hypothesis_submit(**args)
    os.close(captured['lease_fd'])
    journal = Path(args['store_root']) / 'jobs' / submitted['job']['job_id'] / 'journal.json'
    record = json.loads(journal.read_bytes())
    record['phase'] = 'a' * 10000
    record['revision_sha256'] = digest({key: value for key, value in record.items() if key != 'revision_sha256'})
    journal.write_text(json.dumps(record))
    with pytest.raises(PocketError, match='bounds'):
        status(args, submitted)
