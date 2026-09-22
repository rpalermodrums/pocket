"""Durable-before-capture jobs, cooperative block cancellation and strict v3 proof."""
import copy
import hashlib
import json
import os
import struct
import threading
from pathlib import Path

import pytest
from test_audio_hypothesis_jobs import frozen_launch, wait_terminal
from test_audio_region_analysis import ACTOR, ANALYSIS, args
from test_audio_regions import wav

from pocket_music import audio_hypothesis_jobs as jobs
from pocket_music import audio_regions as regions
from pocket_music.artifact_store import canonical_bytes, digest, put_record, read_record
from pocket_music.audio_region_analysis import audio_region_hypotheses
from pocket_music.audio_regions import audio_region_capture
from pocket_music.errors import PocketError

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Owned POSIX region profile')


def inputs(tmp_path, region=None):
    return {'store_root': str(tmp_path / 'store'), 'request_id': 'submit',
            'region': region or {'kind': 'inline', 'source': args(tmp_path)},
            'analysis': copy.deepcopy(ANALYSIS), 'attribution': copy.deepcopy(ACTOR)}


@pytest.fixture
def pending(tmp_path, monkeypatch):
    captured = frozen_launch(monkeypatch)
    arguments = inputs(tmp_path)
    submitted = jobs.audio_region_submit(**arguments)
    yield arguments, submitted, captured
    try:
        os.close(captured['lease_fd'])
    except OSError:
        pass


def status(pending):
    arguments, submitted, _ = pending
    return jobs.job_status(arguments['store_root'], submitted['job']['job_id'])


def reseal(pending, mutate):
    arguments, submitted, _ = pending
    path = Path(arguments['store_root']) / 'jobs' / submitted['job']['job_id'] / 'journal.json'
    record = json.loads(path.read_bytes())
    mutate(record)
    record['revision_sha256'] = digest({k: v for k, v in record.items() if k != 'revision_sha256'})
    path.write_text(json.dumps(record))


def test_enqueue_has_durable_lease_before_any_inline_source_read(tmp_path, monkeypatch):
    captured = frozen_launch(monkeypatch)
    arguments = inputs(tmp_path)
    real = regions._capture
    monkeypatch.setattr(regions, '_capture', lambda *a: pytest.fail('capture before enqueue'))
    submitted = jobs.audio_region_submit(**arguments)
    assert jobs.job_status(arguments['store_root'], submitted['job']['job_id'])['state'] == 'queued'
    journal = jobs._read(Path(arguments['store_root']).resolve(), submitted['job']['job_id'])
    assert journal['schema'] == 'pocket.analysis-job/v3' and journal['source'] is None
    assert not (Path(arguments['store_root']) / 'artifacts').exists()
    monkeypatch.setattr(regions, '_capture', real)
    jobs._run_worker(**captured)
    direct = audio_region_hypotheses(**{**arguments, 'request_id': 'direct'})
    assert jobs.job_status(arguments['store_root'], submitted['job']['job_id'])['result'] == direct['artifacts']['hypotheses']


def test_actual_worker_direct_equivalence_and_bounded_status(tmp_path):
    arguments = inputs(tmp_path)
    submitted = jobs.audio_region_submit(**arguments)
    final = wait_terminal(arguments, submitted)
    assert final['state'] == 'completed', final
    direct = audio_region_hypotheses(**{**arguments, 'request_id': 'direct'})
    assert final['result'] == direct['artifacts']['hypotheses']
    assert jobs.audio_region_submit(**arguments) == submitted
    assert len(canonical_bytes(final)) < 16384
    Path(arguments['region']['source']['path']).unlink()
    assert jobs.job_status(arguments['store_root'], submitted['job']['job_id'])['state'] == 'completed'


def test_captured_region_graph_copied_to_staging(tmp_path, monkeypatch):
    captured = frozen_launch(monkeypatch)
    arguments = inputs(tmp_path)
    handle = audio_region_capture(store_root=arguments['store_root'], request_id='capture',
        source=arguments['region']['source'])['artifacts']['region']
    Path(arguments['region']['source']['path']).unlink()
    arguments['region'] = {'kind': 'captured', 'region': handle}
    submitted = jobs.audio_region_submit(**arguments)
    jobs._run_worker(**captured)
    final = jobs.job_status(arguments['store_root'], submitted['job']['job_id'])
    assert final['state'] == 'completed'
    assert read_record(final['result'], arguments['store_root'])['region'] == handle


def test_cancel_before_capture_and_stale_revision(pending):
    arguments, submitted, captured = pending
    current = status(pending)
    assert jobs.job_cancel(arguments['store_root'], submitted['job']['job_id'], current['revision'])['state'] == 'cancel_requested'
    assert jobs.job_cancel(arguments['store_root'], submitted['job']['job_id'], current['revision'])['status'] == 'conflict'
    jobs._run_worker(**captured)
    assert status(pending)['state'] == 'cancelled' and status(pending)['result'] is None
    assert not list(Path(arguments['store_root']).glob('jobs/*/staging'))


def test_actual_large_capture_cancel_between_blocks(tmp_path, monkeypatch):
    frames, rate = 16000 * 4201, 16000
    size = frames * 4
    path = tmp_path / 'long.wav'
    header = wav(b'', channels=2, rate=rate)[:40] + struct.pack('<I', size)
    header = header[:4] + struct.pack('<I', size + 36) + header[8:]
    with path.open('wb') as out:
        out.write(header)
        out.truncate(size+44)
    with path.open('rb') as stream:
        expected = hashlib.file_digest(stream, 'sha256').hexdigest()
    arguments = inputs(tmp_path, {'kind': 'inline', 'source': {'path': str(path), 'expected_sha256': expected,
        'start_frame': frames-32000, 'frames': 32000, 'source_origin': 'user_recording'}})
    captured = frozen_launch(monkeypatch)
    submitted = jobs.audio_region_submit(**arguments)
    paused, resume = threading.Event(), threading.Event()
    real = regions._exact
    reads = []
    def pause(stream, length):
        data = real(stream, length)
        if length == regions.BLOCK_BYTES:
            reads.append(length)
            paused.set()
            assert resume.wait(5)
        return data
    monkeypatch.setattr(regions, '_exact', pause)
    errors = []
    def worker():
        try:
            jobs._run_worker(**captured)
        except BaseException as error:  # noqa: BLE001 - return fixture thread failure to parent
            errors.append(error)
    thread = threading.Thread(target=worker)
    thread.start()
    assert paused.wait(5)
    current = jobs.job_status(arguments['store_root'], submitted['job']['job_id'])
    jobs.job_cancel(arguments['store_root'], submitted['job']['job_id'], current['revision'])
    resume.set()
    thread.join(10)
    assert not thread.is_alive() and not errors
    final = jobs.job_status(arguments['store_root'], submitted['job']['job_id'])
    assert final['state'] == 'cancelled' and final['result'] is None
    assert reads == [1048576]
    assert not list(Path(arguments['store_root']).glob('artifacts/*/record.json'))
    assert not list(Path(arguments['store_root']).glob('jobs/*/staging/artifacts/*/record.json'))


@pytest.mark.parametrize('mutate', [
    lambda r: r.update(analysis_kind='audio_pulse_hypotheses'),
    lambda r: r.update(source={}),
    lambda r: r.update(arguments={}),
    lambda r: r['arguments'].update(extra=True),
    lambda r: r['arguments']['region']['source'].update(start_frame=True),
    lambda r: r['arguments']['region']['source'].update(expected_sha256='bad'),
    lambda r: r['arguments']['analysis'].update(extra=True),
    lambda r: r['arguments']['analysis']['settings'].update(beats_per_bar=True),
])
def test_resealed_malformed_v3_refuses(pending, mutate):
    reseal(pending, mutate)
    with pytest.raises(PocketError):
        status(pending)


@pytest.mark.parametrize('field,value', [('start_frame', 99), ('expected_sha256', 'a'*64), ('source_origin', 'user_recording')])
def test_completed_original_input_binding(pending, field, value):
    jobs._run_worker(**pending[2])
    reseal(pending, lambda r: r['arguments']['region']['source'].update({field: value}))
    with pytest.raises(PocketError):
        status(pending)


def test_completed_wrapper_semantics_not_just_hash(pending):
    arguments, _, captured = pending
    jobs._run_worker(**captured)
    record = read_record(status(pending)['result'], arguments['store_root'])
    record['coordinate_mapping']['original_start_frame'] += 1
    bad = put_record(record, arguments['store_root'])
    reseal(pending, lambda r: r['result']['artifacts'].update(hypotheses=bad))
    with pytest.raises(PocketError):
        status(pending)


def test_released_lease_no_auto_retry(pending):
    arguments, submitted, captured = pending
    os.close(captured['lease_fd'])
    assert status(pending)['state'] == 'interrupted'
    assert jobs.audio_region_submit(**arguments) == submitted
    assert status(pending)['result'] is None


@pytest.mark.parametrize('failure', [RuntimeError('capture failed'), KeyboardInterrupt('crash')])
def test_worker_failure_no_result(pending, monkeypatch, failure):
    def fail(*a, **k):
        raise failure
    monkeypatch.setattr(regions, '_capture', fail)
    with pytest.raises(type(failure)):
        jobs._run_worker(**pending[2])
    assert status(pending)['state'] == ('failed' if isinstance(failure, Exception) else 'interrupted')
    assert status(pending)['result'] is None


def test_changed_original_after_enqueue_refuses(pending):
    arguments, _, captured = pending
    Path(arguments['region']['source']['path']).write_bytes(b'changed before capture')
    with pytest.raises(PocketError):
        jobs._run_worker(**captured)
    assert status(pending)['state'] == 'failed' and status(pending)['result'] is None


def test_concurrent_argument_change_refuses_at_next_boundary(pending, monkeypatch):
    real = regions._exact
    changed = False
    def change(stream, length):
        nonlocal changed
        value = real(stream, length)
        if not changed:
            changed = True
            reseal(pending, lambda r: r['arguments']['region']['source'].update(start_frame=99))
        return value
    monkeypatch.setattr(regions, '_exact', change)
    with pytest.raises(PocketError, match='changed'):
        jobs._run_worker(**pending[2])
    assert status(pending)['result'] is None


def test_learned_region_inherits_context_and_matches_direct(tmp_path, monkeypatch):
    from test_audio_models import PulseModelTests

    from pocket_music.audio_pulse_hypotheses import _CONTEXT, SETTINGS
    f = PulseModelTests()
    f.setUp()
    captured = frozen_launch(monkeypatch)
    seen = []
    def runner(*a, **k):
        lease, callback = _CONTEXT.get()
        if lease:
            assert lease == (captured['lease_fd'],)
            os.fstat(lease[0])
            callback()
            seen.append(True)
        return f.runner(*a, **k)
    monkeypatch.setattr('pocket_music.audio_pulse_hypotheses._runner', runner)
    try:
        arguments = {'store_root': str(tmp_path / 'store'), 'request_id': 'submit',
            'region': {'kind': 'inline', 'source': f.source},
            'analysis': {'kind': 'learned_pulse', 'model': f.model, 'settings': SETTINGS}, 'attribution': ACTOR}
        submitted = jobs.audio_region_submit(**arguments)
        jobs._run_worker(**captured)
        final = jobs.job_status(arguments['store_root'], submitted['job']['job_id'])
        direct = audio_region_hypotheses(**{**arguments, 'request_id': 'direct'})
        assert final['state'] == 'completed' and final['result'] == direct['artifacts']['hypotheses']
        assert seen and _CONTEXT.get() == ((), None)
        # Completed status is retained evidence only; external model can disappear.
        Path(f.declaration['weights']['path']).unlink()
        Path(f.source['path']).unlink()
        assert jobs.job_status(arguments['store_root'], submitted['job']['job_id'])['state'] == 'completed'
    finally:
        f.doCleanups()


def test_cancel_cannot_interleave_committed_result(pending, monkeypatch):
    arguments, submitted, captured = pending
    real = jobs._collect_graph
    checked = []
    def collect(result, staging, root):
        if Path(root).resolve() == Path(arguments['store_root']).resolve():
            with pytest.raises(PocketError, match='busy'):
                jobs.job_cancel(arguments['store_root'], submitted['job']['job_id'], '0' * 64)
            checked.append(True)
        return real(result, staging, root)
    monkeypatch.setattr(jobs, '_collect_graph', collect)
    jobs._run_worker(**captured)
    final = status(pending)
    assert checked and final['state'] == 'completed'
    assert jobs.job_cancel(arguments['store_root'], submitted['job']['job_id'], final['revision'])['status'] == 'conflict'


def test_partial_collection_failure_has_no_committed_result(pending, monkeypatch):
    arguments, _, captured = pending
    real = jobs._collect_graph
    def fail(result, staging, root):
        if Path(root).resolve() == Path(arguments['store_root']).resolve():
            put_record(read_record(result['artifacts']['hypotheses'], staging), root)
            raise RuntimeError('partial copy interruption')
        return real(result, staging, root)
    monkeypatch.setattr(jobs, '_collect_graph', fail)
    with pytest.raises(RuntimeError, match='partial'):
        jobs._run_worker(**captured)
    assert status(pending)['state'] == 'failed' and status(pending)['result'] is None


def test_launch_failure_retains_failed_without_capture(tmp_path, monkeypatch):
    arguments = inputs(tmp_path)
    def fail(*a, **k):
        raise OSError('launch fixture')
    monkeypatch.setattr(jobs, '_spawn', fail)
    with pytest.raises(OSError):
        jobs.audio_region_submit(**arguments)
    folder, = (tmp_path / 'store/jobs').iterdir()
    assert jobs.job_status(arguments['store_root'], folder.name)['state'] == 'failed'
    assert not (tmp_path / 'store/artifacts').exists()
    with pytest.raises(PocketError, match='did not complete'):
        jobs.audio_region_submit(**arguments)
