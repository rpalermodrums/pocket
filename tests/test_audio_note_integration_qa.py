"""Independent note integration and shared worker publication boundaries."""
import builtins
import copy
import hashlib
import json
import os
import shutil
import wave
from pathlib import Path

import pytest
from test_audio_note_hypotheses import args, note_fixture  # noqa: F401
from test_audio_pulse_jobs_qa import job, reseal, status  # noqa: F401

from pocket_music import audio_hypothesis_jobs as jobs
from pocket_music.artifact_store import canonical_bytes, digest, put_record, read_record
from pocket_music.audio_hypotheses import audio_hypothesis_correct, audio_hypothesis_query
from pocket_music.audio_note_hypotheses import audio_note_hypotheses
from pocket_music.audio_note_jobs import audio_note_submit
from pocket_music.audio_pulse_hypotheses import _CONTEXT
from pocket_music.audio_region_analysis import audio_region_hypotheses, audio_region_query
from pocket_music.audio_region_corrections import audio_region_correct
from pocket_music.errors import PocketError


@pytest.mark.parametrize('field', ['origin', 'settings', 'attribution'])
def test_shared_model_commit_refuses_valid_input_replacement_after_last_callback(job, monkeypatch, field):  # noqa: F811
    import pocket_music.audio_pulse_hypotheses as pulse
    _, _, _, captured = job
    primitive = pulse.audio_pulse_hypotheses

    def changed(**kwargs):
        result = primitive(**kwargs)
        def mutate(row):
            if field == 'origin':
                row['arguments']['source']['source_origin'] = 'user_recording'
            elif field == 'settings':
                # A no-op dictionary reorder remains identical and is accepted;
                # this real valid-profile change must be caught by full validation.
                row['arguments']['settings']['seed'] = 1
            else:
                row['arguments']['attribution']['statement'] = 'Another valid attributed request.'
        reseal(job, mutate)
        return result

    monkeypatch.setattr(pulse, 'audio_pulse_hypotheses', changed)
    with pytest.raises(PocketError):
        jobs._run_worker(**captured)
    if field != 'settings':
        assert status(job)['state'] == 'failed'
        assert status(job)['result'] is None
    else:
        with pytest.raises(PocketError):
            status(job)


def test_shared_model_commit_refuses_semantic_result_replacement(job, monkeypatch):  # noqa: F811
    import pocket_music.audio_pulse_hypotheses as pulse
    _, _, _, captured = job
    primitive = pulse.audio_pulse_hypotheses

    def changed(**kwargs):
        alternate = copy.deepcopy(kwargs)
        alternate['attribution']['statement'] = 'A valid separate request, never authorized for this job.'
        result = primitive(**alternate)
        assert read_record(result['artifacts']['hypotheses'], kwargs['store_root'])['request_attribution'] != kwargs['attribution']
        return result

    monkeypatch.setattr(pulse, 'audio_pulse_hypotheses', changed)
    with pytest.raises(PocketError, match='differs from job inputs'):
        jobs._run_worker(**captured)
    assert status(job)['state'] == 'failed'
    assert status(job)['result'] is None


def test_shared_model_cancel_after_primitive_before_commit_has_no_result(job, monkeypatch):  # noqa: F811
    import pocket_music.audio_pulse_hypotheses as pulse
    fixture, _, submitted, captured = job
    primitive = pulse.audio_pulse_hypotheses

    def cancelled(**kwargs):
        result = primitive(**kwargs)
        before = status(job)
        jobs.job_cancel(fixture.store, submitted['job']['job_id'], before['revision'])
        return result

    monkeypatch.setattr(pulse, 'audio_pulse_hypotheses', cancelled)
    jobs._run_worker(**captured)
    assert status(job)['state'] == 'cancelled'
    assert status(job)['result'] is None


def make_note(f):
    return audio_note_hypotheses(request_id='base', **args(f))['artifacts']['hypotheses']


def authored(parent, store, key='authored'):
    row = read_record(parent, store)['annotations'][0]
    return {'correction_id': key, 'supersedes': [row['annotation_id']],
            'annotation': {'kind': 'note_hypothesis', 'start_frame': 100, 'end_frame_exclusive': 500,
                           'midi_note': 36, 'cents': 0, 'tuning_ref': 'explicit-fixture-12tet'},
            'support': [{'kind': 'annotation_id', 'reference': row['annotation_id']}],
            'uncertainty': ['Agent alternative; no actual listening.']}


def correct_note(f, parent, rows, request='correct', **extra):
    return audio_hypothesis_correct(store_root=f['store_root'], request_id=request, parent=parent,
        expected_revision=parent['sha256'], corrections=rows, attribution=f['attribution'], **extra)


def test_note_correction_keeps_learned_row_and_relocated_no_runtime_query(note_fixture, monkeypatch, tmp_path):  # noqa: F811
    f = note_fixture; parent = make_note(f); initial = read_record(parent, f['store_root'])
    corrected = correct_note(f, parent, [authored(parent, f['store_root'])])
    child = corrected['artifacts']['hypotheses']
    moved = tmp_path / 'relocated'; shutil.copytree(f['store_root'], moved)
    Path(f['source']['path']).unlink()
    original_import = builtins.__import__
    def guarded(name, *a, **kw):
        if name.split('.')[0] in {'onnxruntime', 'tensorflow', 'torch', 'basic_pitch'}:
            raise AssertionError('Retained note query imported optional inference runtime')
        return original_import(name, *a, **kw)
    monkeypatch.setattr(builtins, '__import__', guarded)
    monkeypatch.setattr('pocket_music.audio_note_hypotheses._runner', lambda *a, **kw: pytest.fail('Retained query reexecuted model'))
    query = audio_hypothesis_query(store_root=str(moved), hypotheses=child, view='annotations')
    assert query['items'][:-1] == initial['annotations']
    assert query['items'][-1]['attribution'] == f['attribution']
    assert query['items'][-1]['annotation']['midi_note'] == 36
    assert read_record(parent, moved) == initial
    assert len(canonical_bytes(query)) <= 16384


@pytest.mark.parametrize('bad', ['duplicate', 'forward', 'supersedes_forward', 'false_kind', 'out_of_crop', 'bool'])
def test_note_correction_malformed_or_same_batch_references_refuse(note_fixture, bad):  # noqa: F811
    f = note_fixture; parent = make_note(f); first = authored(parent, f['store_root']); second = copy.deepcopy(first)
    second['correction_id'] = 'second'
    if bad == 'duplicate': second['correction_id'] = first['correction_id']
    elif bad in {'forward', 'supersedes_forward'}:
        future = 'audio:' + digest([parent, {**first, 'attribution': f['attribution']}])
        if bad == 'forward': second['support'] = [{'kind': 'annotation_id', 'reference': future}]
        else: second['supersedes'] = [future]
    elif bad == 'false_kind': second['annotation']['kind'] = 'learned_note'
    elif bad == 'out_of_crop': second['annotation']['end_frame_exclusive'] = 44101
    else: second['annotation']['start_frame'] = True
    before = set((Path(f['store_root']) / 'artifacts').glob('*/record.json'))
    with pytest.raises(PocketError): correct_note(f, parent, [first, second])
    assert set((Path(f['store_root']) / 'artifacts').glob('*/record.json')) == before


def test_note_query_cursor_bound_to_revision_and_view(note_fixture):  # noqa: F811
    f = note_fixture; parent = make_note(f)
    child = correct_note(f, parent, [authored(parent, f['store_root'])])['artifacts']['hypotheses']
    page = audio_hypothesis_query(store_root=f['store_root'], hypotheses=child, view='annotations', limit=1)
    assert page['next_cursor'] and len(canonical_bytes(page)) <= 16384
    for handle, view in [(parent, 'annotations'), (child, 'history')]:
        with pytest.raises(PocketError, match='cursor'):
            audio_hypothesis_query(store_root=f['store_root'], hypotheses=handle, view=view, cursor=page['next_cursor'])
    with pytest.raises(PocketError, match='Stale'):
        audio_hypothesis_correct(store_root=f['store_root'], request_id='stale', parent=child, expected_revision=parent['sha256'], corrections=[authored(parent, f['store_root'])], attribution=f['attribution'])


@pytest.fixture
def note_job(note_fixture, monkeypatch):  # noqa: F811
    f = note_fixture; captured = {}
    def spawn(root, job_id, nonce, lease_fd):
        captured.update(root=root, job_id=job_id, nonce=nonce, lease_fd=os.dup(lease_fd))
        assert (Path(root) / 'jobs' / job_id / 'journal.json').is_file()
    monkeypatch.setattr(jobs, '_spawn', spawn)
    submitted = audio_note_submit(request_id='note-job', **args(f))
    yield f, submitted, captured
    try: os.close(captured['lease_fd'])
    except OSError: pass


def note_status(f, submitted):
    return jobs.job_status(f['store_root'], submitted['job']['job_id'])


def test_note_v4_direct_worker_identity_and_retained_status(note_job, monkeypatch):
    f, submitted, captured = note_job
    observed = []
    def runner(*a, **kw):
        lease, check = _CONTEXT.get(); observed.append(lease)
        assert lease == (captured['lease_fd'],); check()
        return f['runner'](*a, **kw)
    monkeypatch.setattr('pocket_music.audio_note_hypotheses._runner', runner)
    jobs._run_worker(**captured)
    actual = note_status(f, submitted)
    assert observed and actual['state'] == 'completed'
    monkeypatch.setattr('pocket_music.audio_note_hypotheses._runner', f['runner'])
    direct = make_note(f)
    assert actual['result'] == direct
    Path(f['source']['path']).unlink()
    monkeypatch.setattr('pocket_music.audio_note_hypotheses._runner', lambda *a, **kw: pytest.fail('Status reexecuted runtime'))
    monkeypatch.setattr('pocket_music.audio_note_hypotheses._declaration', lambda *a, **kw: pytest.fail('Status accessed external model environment'))
    assert note_status(f, submitted)['result'] == direct
    assert len(canonical_bytes(actual)) <= 16384


@pytest.mark.parametrize('when', ['before', 'runner'])
def test_note_v4_cancel_no_completed_handle(note_job, monkeypatch, when):
    f, submitted, captured = note_job
    def cancel():
        state = note_status(f, submitted)
        return jobs.job_cancel(f['store_root'], submitted['job']['job_id'], state['revision'])
    if when == 'before': cancel()
    else:
        def runner(*a, **kw):
            assert cancel()['state'] == 'cancel_requested'
            _CONTEXT.get()[1](); pytest.fail('Cancellation callback returned')
        monkeypatch.setattr('pocket_music.audio_note_hypotheses._runner', runner)
    jobs._run_worker(**captured)
    state = note_status(f, submitted)
    assert state['state'] == 'cancelled' and state['result'] is None


def test_note_region_original_crop_projection_and_public_correction_equivalence(note_fixture):  # noqa: F811
    f = note_fixture
    path = Path(f['source']['path'])
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 22050, 44237, 'NONE', 'not compressed'))
        stream.writeframes(b'\0' * (44237 * 2))
    region_source = {**f['source'], 'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'start_frame': 137, 'frames': 44100}
    analysis = {'kind': 'learned_notes', 'model': f['model'], 'settings': f['settings']}
    receipt = audio_region_hypotheses(store_root=f['store_root'], request_id='region', region={'kind': 'inline', 'source': region_source}, analysis=analysis, attribution=f['attribution'])
    wrapper = receipt['artifacts']['hypotheses']; record = read_record(wrapper, f['store_root'])
    first = audio_region_query(store_root=f['store_root'], hypotheses=wrapper, view='annotations')['items'][0]
    assert first['original_projection']['start_frame'] == first['local']['annotation']['start_frame'] + 137
    assert first['original_projection']['end_frame_exclusive'] == first['local']['annotation']['end_frame_exclusive'] + 137
    item = authored(record['local_hypotheses'], f['store_root'])
    original_item = copy.deepcopy(item)
    original_item['annotation']['start_frame'] += 137; original_item['annotation']['end_frame_exclusive'] += 137
    corrected = audio_region_correct(store_root=f['store_root'], request_id='region-correct', parent=wrapper, expected_revision=wrapper['sha256'], batch={'coordinate_space': 'original_source_frame', 'corrections': [original_item]}, attribution=f['attribution'])
    direct = correct_note(f, record['local_hypotheses'], [item], request='direct-correct')
    assert corrected['artifacts']['hypotheses'] == put_record({**record, 'local_hypotheses': direct['artifacts']['hypotheses']}, f['store_root'])


@pytest.mark.parametrize('error', [RuntimeError('partial note collection'), KeyboardInterrupt('note crash')])
def test_note_v4_partial_collection_and_crash_do_not_commit(note_job, monkeypatch, error):
    f, submitted, captured = note_job
    collect = jobs._collect_graph
    def interrupted(value, source, destination):
        if Path(destination).resolve() == Path(f['store_root']).resolve():
            handle = value['artifacts']['hypotheses']
            put_record(read_record(handle, source), destination)
            raise error
        return collect(value, source, destination)
    monkeypatch.setattr(jobs, '_collect_graph', interrupted)
    with pytest.raises(type(error)): jobs._run_worker(**captured)
    current = note_status(f, submitted)
    assert current['state'] == ('failed' if isinstance(error, Exception) else 'interrupted')
    assert current['result'] is None


def test_note_correction_exact_4096_annotations_and_32_revisions(note_fixture):  # noqa: F811
    f = note_fixture; initial = make_note(f); parent = initial
    template = authored(initial, f['store_root'])
    for revision in range(32):
        count = 128 if revision < 31 else 127
        rows = [{**copy.deepcopy(template), 'correction_id': f'{revision}:{index}'} for index in range(count)]
        if revision == 31:
            with pytest.raises(PocketError, match='4096'):
                correct_note(f, parent, rows + [{**copy.deepcopy(template), 'correction_id': 'overflow'}], request='too-many')
        parent = correct_note(f, parent, rows, request=f'limit-{revision}')['artifacts']['hypotheses']
    record = read_record(parent, f['store_root'])
    assert record['annotation_count'] == 4096 and record['revision_index'] == 32
    page = audio_hypothesis_query(store_root=f['store_root'], hypotheses=parent, view='annotations', limit=128, max_bytes=4096)
    assert page['total'] == 4096 and len(canonical_bytes(page)) <= 4096
    with pytest.raises(PocketError, match='32 revisions'):
        correct_note(f, parent, [{**template, 'correction_id': 'revision33'}], request='revision33')


@pytest.mark.parametrize('change', ['arguments', 'result', 'nonce'])
def test_note_v4_final_boundary_replacement_refuses(note_job, monkeypatch, change):
    import pocket_music.audio_note_hypotheses as note_module
    f, submitted, captured = note_job
    primitive = note_module.audio_note_hypotheses
    def replacement(**kwargs):
        local = copy.deepcopy(kwargs)
        if change == 'result': local['attribution']['statement'] = 'Different valid request.'
        result = primitive(**local)
        if change != 'result':
            path = Path(f['store_root']) / 'jobs' / submitted['job']['job_id'] / 'journal.json'
            row = json.loads(path.read_bytes())
            if change == 'arguments': row['arguments']['attribution']['statement'] = 'Another valid request.'
            else: row['nonce'] = 'a' * 64
            row['revision_sha256'] = digest({key: value for key, value in row.items() if key != 'revision_sha256'})
            path.write_text(json.dumps(row))
        return result
    monkeypatch.setattr(note_module, 'audio_note_hypotheses', replacement)
    with pytest.raises(PocketError): jobs._run_worker(**captured)
    current = note_status(f, submitted)
    assert current['state'] == ('interrupted' if change == 'nonce' else 'failed')
    assert current['result'] is None


def test_note_v4_same_request_retry_and_stale_cancel(note_job):
    f, submitted, captured = note_job
    assert audio_note_submit(request_id='note-job', **args(f)) == submitted
    current = note_status(f, submitted)
    assert jobs.job_cancel(f['store_root'], submitted['job']['job_id'], current['revision'])['state'] == 'cancel_requested'
    assert jobs.job_cancel(f['store_root'], submitted['job']['job_id'], current['revision'])['status'] == 'conflict'
    jobs._run_worker(**captured)
    assert note_status(f, submitted)['state'] == 'cancelled'
    # Request replay never starts a replacement worker after cancellation.
    assert audio_note_submit(request_id='note-job', **args(f)) == submitted
    assert note_status(f, submitted)['result'] is None


@pytest.mark.parametrize('change', ['role', 'parent', 'count', 'support'])
def test_note_resealed_correction_history_does_not_bypass_validation(note_fixture, change):  # noqa: F811
    f = note_fixture; parent = make_note(f)
    child = correct_note(f, parent, [authored(parent, f['store_root'])])['artifacts']['hypotheses']
    record = read_record(child, f['store_root'])
    if change == 'role': record['annotations'][0]['attribution']['actor_kind'] = 'algorithm'
    elif change == 'parent': record['parent'] = child
    elif change == 'count': record['annotation_count'] += 1
    else: record['annotations'][0]['support'][0]['reference'] = 'audio:unknown'
    if change in {'role', 'support'}:
        row = record['annotations'][0]
        row['annotation_id'] = 'audio:' + digest([record['parent'], {key: value for key, value in row.items() if key != 'annotation_id'}])
    forged = put_record(record, f['store_root'])
    with pytest.raises(PocketError):
        audio_hypothesis_query(store_root=f['store_root'], hypotheses=forged)


def test_note_v4_external_source_replaced_after_enqueue_uses_captured_bytes(note_job):
    f, submitted, captured = note_job
    direct = make_note(f)
    Path(f['source']['path']).write_bytes(b'replaced external path after captured submission')
    jobs._run_worker(**captured)
    assert note_status(f, submitted)['result'] == direct


@pytest.mark.parametrize('kind', ['inline', 'captured'])
def test_note_region_worker_same_public_primitive_and_declared_mapping(note_fixture, monkeypatch, kind):  # noqa: F811
    from pocket_music.audio_regions import audio_region_capture
    f = note_fixture
    captured = {}
    def spawn(root, job_id, nonce, lease_fd):
        captured.update(root=root, job_id=job_id, nonce=nonce, lease_fd=os.dup(lease_fd))
    monkeypatch.setattr(jobs, '_spawn', spawn)
    region = {'kind': 'inline', 'source': f['source']}
    if kind == 'captured':
        handle = audio_region_capture(store_root=f['store_root'], request_id='capture', source=f['source'])['artifacts']['region']
        region = {'kind': 'captured', 'region': handle}
    arguments = {'store_root': f['store_root'], 'region': region,
                 'analysis': {'kind': 'learned_notes', 'model': f['model'], 'settings': f['settings']}, 'attribution': f['attribution']}
    direct = audio_region_hypotheses(request_id='direct-region', **arguments)
    submitted = jobs.audio_region_submit(request_id='region-job', **arguments)
    try:
        jobs._run_worker(**captured)
    finally:
        try: os.close(captured['lease_fd'])
        except OSError: pass
    state = jobs.job_status(f['store_root'], submitted['job']['job_id'])
    assert state['state'] == 'completed'
    assert state['result'] == direct['artifacts']['hypotheses']
