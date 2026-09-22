"""Independent owned learned-pulse job failure boundaries; mocked inference only."""
import copy
import json
import os
from pathlib import Path

import pytest

from pocket_music import audio_hypothesis_jobs as jobs
from pocket_music.artifact_store import canonical_bytes, digest, put_record, read_record
from pocket_music.audio_pulse_hypotheses import _CONTEXT, SETTINGS
from pocket_music.errors import PocketError

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX ownership profile')


@pytest.fixture
def job(monkeypatch):
    from test_audio_models import PulseModelTests
    f = PulseModelTests()
    f.setUp()
    captured = {}
    def spawn(root, job_id, nonce, lease_fd):
        captured.update(root=root, job_id=job_id, nonce=nonce, lease_fd=os.dup(lease_fd))
    monkeypatch.setattr(jobs, '_spawn', spawn)
    monkeypatch.setattr('pocket_music.audio_pulse_hypotheses._runner', f.runner)
    args = {'store_root': f.store, 'request_id': 'submit', 'source': f.source, 'model': f.model,
            'settings': SETTINGS, 'attribution': f.actor}
    submitted = jobs.audio_pulse_submit(**args)
    yield f, args, submitted, captured
    try:
        os.close(captured['lease_fd'])
    except OSError:
        pass
    f.doCleanups()


def status(job):
    f, _, submitted, _ = job
    return jobs.job_status(f.store, submitted['job']['job_id'])


def reseal(job, mutate):
    f, _, submitted, _ = job
    path = Path(f.store) / 'jobs' / submitted['job']['job_id'] / 'journal.json'
    record = json.loads(path.read_bytes())
    mutate(record)
    record['revision_sha256'] = digest({k: v for k, v in record.items() if k != 'revision_sha256'})
    path.write_text(json.dumps(record))


def test_direct_worker_same_graph_and_owned_context(job, monkeypatch):
    f, args, submitted, captured = job
    observed = []
    def runner(*args, **kwargs):
        lease, check = _CONTEXT.get()
        observed.append((lease, check))
        assert lease == (captured['lease_fd'],)
        os.fstat(lease[0])
        check()
        return f.runner(*args, **kwargs)
    monkeypatch.setattr('pocket_music.audio_pulse_hypotheses._runner', runner)
    jobs._run_worker(**captured)
    assert observed and status(job)['state'] == 'completed'
    monkeypatch.setattr('pocket_music.audio_pulse_hypotheses._runner', f.runner)
    direct = f.call('direct')
    assert status(job)['result'] == direct['artifacts']['hypotheses']
    assert jobs.audio_pulse_submit(**args) == submitted
    assert len(canonical_bytes(status(job))) <= 16384
    assert _CONTEXT.get() == ((), None)


@pytest.mark.parametrize('phase', ['before', 'inside_runner'])
def test_cooperative_cancel_never_exposes_result(job, monkeypatch, phase):
    f, _, submitted, captured = job
    def cancel():
        current = status(job)
        return jobs.job_cancel(f.store, submitted['job']['job_id'], current['revision'])
    if phase == 'before':
        current = status(job)
        assert cancel()['state'] == 'cancel_requested'
        assert jobs.job_cancel(f.store, submitted['job']['job_id'], current['revision'])['status'] == 'conflict'
    else:
        def runner(*args, **kwargs):
            assert cancel()['state'] == 'cancel_requested'
            _CONTEXT.get()[1]()
            pytest.fail('Cancellation did not stop at cooperative boundary')
        monkeypatch.setattr('pocket_music.audio_pulse_hypotheses._runner', runner)
    jobs._run_worker(**captured)
    assert status(job)['state'] == 'cancelled'
    assert status(job)['result'] is None
    assert not list((Path(f.store) / 'artifacts').glob('*/record.json'))


@pytest.mark.parametrize('error', [RuntimeError('partial'), KeyboardInterrupt('crash')])
def test_partial_collection_or_interrupt_never_commits(job, monkeypatch, error):
    f, _, _, captured = job
    real = jobs._collect_graph
    def interrupted(result, staging, root):
        if Path(root).resolve() == Path(f.store).resolve():
            # Retain one copied object, then fail before the completed journal commit.
            handle = result['artifacts']['hypotheses']
            put_record(read_record(handle, staging), root)
            raise error
        return real(result, staging, root)
    monkeypatch.setattr(jobs, '_collect_graph', interrupted)
    with pytest.raises(type(error)):
        jobs._run_worker(**captured)
    assert status(job)['state'] == ('failed' if isinstance(error, Exception) else 'interrupted')
    assert status(job)['result'] is None
    assert list((Path(f.store) / 'artifacts').glob('*/record.json'))


def test_source_original_change_uses_captured_bytes(job):
    f, _, _, captured = job
    Path(f.source['path']).write_bytes(b'changed original after enqueue')
    # Source was captured, so external path mutation cannot affect worker input.
    jobs._run_worker(**captured)
    assert status(job)['state'] == 'completed'
    result = read_record(status(job)['result'], f.store)
    assert result['source']['sha256'] == f.source['expected_sha256']


def test_released_lease_no_retry(job):
    _, args, submitted, captured = job
    os.close(captured['lease_fd'])
    assert status(job)['state'] == 'interrupted'
    assert jobs.audio_pulse_submit(**args) == submitted
    changed = copy.deepcopy(args)
    changed['settings']['seed'] = 1
    with pytest.raises(PocketError):
        jobs.audio_pulse_submit(**changed)


@pytest.mark.parametrize('mutate', [
    lambda r: r.update(analysis_kind='audio_hypotheses'),
    lambda r: r.update(arguments={}),
    lambda r: r['arguments'].update(extra=True),
    lambda r: r['arguments']['source'].update(path='/unexpected/source.wav'),
    lambda r: r['arguments']['source'].update(expected_sha256='0' * 64),
    lambda r: r['arguments']['settings'].update(seed=True),
    lambda r: r['arguments'].update(model={'kind': 'invented'}),
])
def test_resealed_malformed_v2_journal_refuses(job, mutate):
    reseal(job, mutate)
    with pytest.raises(PocketError):
        status(job)


def test_completed_invalid_model_result_not_success_shaped(job):
    f, _, _, captured = job
    jobs._run_worker(**captured)
    original = status(job)['result']
    record = read_record(original, f.store)
    record['annotation_count'] = 123
    malformed = put_record(record, f.store)
    reseal(job, lambda row: row['result']['artifacts'].update(hypotheses=malformed))
    with pytest.raises(PocketError):
        status(job)


def test_model_child_inherits_exact_execution_lease(job, monkeypatch):
    import subprocess
    import sys
    f, _, _, captured = job
    def runner(*args, **kwargs):
        lease, check = _CONTEXT.get()
        code = ('import os,sys; fd=int(sys.argv[1]); s=os.fstat(fd); '
                'print(str(s.st_dev)+":"+str(s.st_ino))')
        result = subprocess.run([sys.executable, '-B', '-c', code, str(lease[0])],
            pass_fds=lease, capture_output=True, text=True, timeout=5, check=True)
        expected = os.fstat(captured['lease_fd'])
        assert result.stdout.strip() == f'{expected.st_dev}:{expected.st_ino}'
        check()
        return f.runner(*args, **kwargs)
    monkeypatch.setattr('pocket_music.audio_pulse_hypotheses._runner', runner)
    jobs._run_worker(**captured)
    assert status(job)['state'] == 'completed'


def test_weight_mutation_after_submission_refuses_dispatch(job):
    f, _, _, captured = job
    Path(f.declaration['weights']['path']).write_bytes(b'mutated after enqueue')
    with pytest.raises(PocketError):
        jobs._run_worker(**captured)
    assert status(job)['state'] in ('failed', 'interrupted')
    assert status(job)['result'] is None


def test_concurrent_owner_change_refuses_publication(job, monkeypatch):
    f, _, _, captured = job
    def runner(*args, **kwargs):
        result = f.runner(*args, **kwargs)
        reseal(job, lambda row: row.update(nonce='a' * 64))
        return result
    monkeypatch.setattr('pocket_music.audio_pulse_hypotheses._runner', runner)
    with pytest.raises(PocketError):
        jobs._run_worker(**captured)
    assert status(job)['state'] == 'interrupted'
    assert status(job)['result'] is None
    assert not list((Path(f.store) / 'artifacts').glob('*/record.json'))


def test_completed_other_valid_model_cannot_replace_declared_job_result(job):
    import hashlib
    f, _, _, captured = job
    jobs._run_worker(**captured)
    weights = Path(f.declaration['weights']['path'])
    weights.write_bytes(b'different explicitly mocked model weights')
    f.declaration['weights']['sha256'] = hashlib.sha256(weights.read_bytes()).hexdigest()
    other = f.call('different-model')['artifacts']['hypotheses']
    reseal(job, lambda row: row['result']['artifacts'].update(hypotheses=other))
    with pytest.raises(PocketError):
        status(job)
