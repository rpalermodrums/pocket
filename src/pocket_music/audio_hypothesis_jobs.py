# SPDX-License-Identifier: AGPL-3.0-only
"""Owned local audio analysis with cooperative, publication-boundary cancellation.

POSIX execution leases are inherited through exec, never inferred from a PID.
The same public primitive runs in a private staging store. Its result becomes a
job result only after graph verification and a serialized uncancelled commit.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import wave
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .artifact_store import (
    _atomic_json,
    _contained,
    _root,
    _verify_handles,
    canonical_bytes,
    digest,
    put_bytes,
    read_bytes,
    receipt,
    run_request,
)
from .audio_hypothesis_types import (
    AudioHypothesisAttribution,
    AudioHypothesisSettings,
    AudioHypothesisSource,
)
from .audio_model_types import AudioPulseModel, AudioPulseSettings
from .audio_region_types import AudioRegionAnalysis, AudioRegionInput
from .errors import PocketError

_ACTIVE = {'queued', 'running', 'cancel_requested'}
_TERMINAL = {'completed', 'cancelled', 'failed', 'interrupted'}
_FIELDS = {'schema', 'job_id', 'revision', 'revision_sha256', 'state', 'nonce', 'arguments',
           'source', 'created_at', 'updated_at', 'phase', 'result', 'error', 'history'}


class _JobCancellation(Exception):
    """An owned model reached a cooperative boundary after cancellation."""


def _posix():
    if os.name != 'posix':
        raise PocketError('Owned audio workers require POSIX execution leases on this profile')
    import fcntl
    return fcntl


def _time():
    return datetime.now(UTC).isoformat()


def _folder(root, job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r'job-[0-9a-f]{40}', job_id):
        raise PocketError('Expected an exact store-relative job identity')
    return _contained(root, f'jobs/{job_id}')


@contextmanager
def _state_lock(root, job_id, wait_seconds=0):
    fcntl = _posix()
    folder = _folder(root, job_id)
    if not folder.is_dir():
        raise PocketError('Job does not exist in this store')
    path = _contained(root, f'jobs/{job_id}/state.lock')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        # A bounded conflict is preferable to waiting behind a decoder/commit.
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise PocketError('Job state is busy; refresh status before retry') from error
                time.sleep(.01)
        yield
    finally:
        os.close(fd)


def _read(root, job_id, *, verify_evidence=True):
    path = _contained(root, f'jobs/{job_id}/journal.json')
    try:
        with path.open('rb') as stream:
            payload = stream.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            raise PocketError('Job journal exceeds its bounded record size')
        record = json.loads(payload)
    except (OSError, ValueError, RecursionError) as error:
        raise PocketError('Job journal is unreadable') from error
    model_job = isinstance(record, dict) and record.get('schema') == 'pocket.analysis-job/v2'
    region_job = isinstance(record, dict) and record.get('schema') == 'pocket.analysis-job/v3'
    note_job = isinstance(record, dict) and record.get('schema') == 'pocket.analysis-job/v4'
    expected_fields = _FIELDS | {'analysis_kind'} if model_job or region_job or note_job else _FIELDS
    if (not isinstance(record, dict) or set(record) != expected_fields
            or record['schema'] not in ('pocket.analysis-job/v1', 'pocket.analysis-job/v2', 'pocket.analysis-job/v3', 'pocket.analysis-job/v4')
            or model_job and record['analysis_kind'] != 'audio_pulse_hypotheses'
            or region_job and record['analysis_kind'] != 'audio_region_hypotheses'
            or note_job and record['analysis_kind'] != 'audio_note_hypotheses'
            or record['job_id'] != job_id
            or type(record['revision']) is not int or record['revision'] < 0
            or not isinstance(record['state'], str) or record['state'] not in _ACTIVE | _TERMINAL
            or not isinstance(record['nonce'], str) or not re.fullmatch(r'[0-9a-f]{64}', record['nonce'])
            or not isinstance(record['history'], list) or len(record['history']) > 32):
        raise PocketError('Invalid job journal identity or state')
    if record['revision_sha256'] != digest({k: v for k, v in record.items() if k != 'revision_sha256'}):
        raise PocketError('Job journal revision integrity mismatch')
    if (record['state'] == 'completed') != (record['result'] is not None):
        raise PocketError('Job result is valid only in its committed completed state')
    if (not isinstance(record['phase'], str) or len(record['phase']) > 96
            or any(not isinstance(record[key], str) or len(record[key]) > 64
                   for key in ('created_at', 'updated_at'))
            or record['error'] is not None and (not isinstance(record['error'], str) or len(record['error']) > 1500)):
        raise PocketError('Job observation fields exceed their declared bounds')
    for row in record['history']:
        if (not isinstance(row, dict) or set(row) != {'state', 'phase', 'at'}
                or not isinstance(row['state'], str) or row['state'] not in _ACTIVE | _TERMINAL
                or not isinstance(row['phase'], str) or len(row['phase']) > 96
                or not isinstance(row['at'], str) or len(row['at']) > 64):
            raise PocketError('Invalid bounded job history')
    if not verify_evidence:
        return record
    _verify_handles(record['source'], root)
    if model_job:
        _validate_model_job_arguments(record, root)
    if region_job:
        _validate_region_job_arguments(record, root)
    if note_job:
        from .audio_note_jobs import validate_note_job_arguments
        validate_note_job_arguments(record, root)
    if record['result'] is not None:
        result = record['result']
        expected_schema = ('pocket.audio-note-hypotheses/v1' if note_job else
                           'pocket.audio-region-hypotheses/v1' if region_job else
                           'pocket.audio-model-hypotheses/v1' if model_job else 'pocket.audio-hypotheses/v1')
        if (not isinstance(result, dict) or result.get('schema') != 'pocket.operation-receipt/v1'
                or result.get('status') != 'ok' or not isinstance(result.get('artifacts'), dict)
                or set(result['artifacts']) != {'hypotheses'}
                or not isinstance(result['artifacts']['hypotheses'], dict)
                or result['artifacts']['hypotheses'].get('artifact_schema') != expected_schema):
            raise PocketError('Invalid committed audio hypothesis result')
        _verify_handles(record['result'], root)
        if region_job:
            _validate_region_job_result(record, root)
        if note_job:
            from .audio_note_jobs import validate_note_job_result
            validate_note_job_result(record, root)
        if model_job:
            _validate_model_job_result(record, root)
    return record


def _validate_model_job_result(record, root):
    from .audio_pulse_hypotheses import load_audio_model, load_pulse_hypotheses
    retained = load_pulse_hypotheses(record['result']['artifacts']['hypotheses'], str(root))
    args = record['arguments']
    actual_model = load_audio_model(retained['model'], str(root))
    requested_model = args['model']
    declaration = (requested_model['declaration'] if requested_model['kind'] == 'inline'
                   else load_audio_model(requested_model['model'], str(root))['declaration'])
    if (actual_model['declaration']['weights'] != declaration['weights']
            or actual_model['declaration']['executable'] != declaration['executable']
            or declaration['expected_profile'] is not None
            and actual_model['profile_sha256'] != declaration['expected_profile']):
        raise PocketError('Committed learned result differs from declared model')
    if (retained['original'] != record['source']
            or retained['settings'] != args['settings']
            or retained['request_attribution'] != args['attribution']
            or retained['source']['start_frame'] != args['source']['start_frame']
            or retained['source']['end_frame_exclusive'] != args['source']['start_frame'] + args['source']['frames']
            or retained['source']['source_origin'] != args['source']['source_origin']):
        raise PocketError('Committed learned result differs from job inputs')


def _validate_model_job_arguments(record, root):
    """Validate retained v2 inputs without requiring the optional runtime to exist."""
    from .audio_hypotheses import _attribution, _fields, _source
    from .audio_pulse_hypotheses import SETTINGS

    args = record['arguments']
    _fields(args, {'source', 'settings', 'attribution', 'model'}, 'learned job arguments')
    _source(args['source'], {'bpm_hint': None, 'beats_per_bar': 4})
    _attribution(args['attribution'])
    if canonical_bytes(args['settings']) != canonical_bytes(SETTINGS):
        raise PocketError('Unsupported retained pulse settings')
    source = args['source']
    handle = record['source']
    if (not isinstance(handle, dict) or handle.get('artifact_schema') != 'pocket.audio-source-bytes/v1'
            or source['expected_sha256'] != handle.get('sha256')
            or source['path'] != str(_contained(root, handle['artifact_uri']))):
        raise PocketError('Learned job source is not its immutable snapshot')
    try:
        with wave.open(io.BytesIO(read_bytes(handle, root)), 'rb') as stream:
            if (stream.getsampwidth() != 2 or stream.getcomptype() != 'NONE'
                    or stream.getnchannels() not in (1, 2) or stream.getframerate() not in (8000, 44100, 48000)
                    or not 2 * stream.getframerate() <= source['frames'] <= 20 * stream.getframerate()
                    or source['start_frame'] + source['frames'] > stream.getnframes()):
                raise PocketError('Retained source exceeds the qualified PCM profile')
    except (OSError, EOFError, wave.Error) as error:
        raise PocketError('Retained source is not qualified PCM audio') from error
    _validate_retained_model(args['model'], root)


def _validate_retained_model(model, root):
    from .audio_hypotheses import _fields
    from .audio_pulse_hypotheses import load_audio_model

    if not isinstance(model, dict):
        raise PocketError('Invalid retained model input')
    if model.get('kind') == 'inspected':
        _fields(model, {'kind', 'model'}, 'retained inspected model')
        declaration = load_audio_model(model['model'], str(root))['declaration']
    elif model.get('kind') == 'inline':
        _fields(model, {'kind', 'declaration', 'qualification'}, 'retained inline model')
        if model['qualification'] != 'synthetic_cpu_v1':
            raise PocketError('Invalid retained model qualification')
        declaration = model['declaration']
    else:
        raise PocketError('Unknown retained model kind')
    _fields(declaration, {'adapter', 'executable', 'weights', 'expected_profile'}, 'retained model declaration')
    if (declaration['adapter'] != 'beat_this_cpu_v1' or declaration['expected_profile'] is not None
            and (not isinstance(declaration['expected_profile'], str)
                 or not re.fullmatch('[0-9a-f]{64}', declaration['expected_profile']))):
        raise PocketError('Invalid retained model adapter/profile')
    for name in ('executable', 'weights'):
        item = declaration[name]
        _fields(item, {'path', 'sha256'}, 'retained model file')
        if (not isinstance(item['path'], str) or len(item['path']) > 4096 or not Path(item['path']).is_absolute()
                or not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
            raise PocketError('Invalid retained model file binding')



def _validate_region_job_arguments(record, root):
    """Strict v3 declaration validation; never reads external originals/runtimes."""
    from .audio_hypotheses import _attribution, _fields
    from .audio_hypotheses import _source as validate_peek
    from .audio_region_analysis import _tags
    from .audio_regions import _source as validate_source
    from .audio_regions import load_audio_region

    args = record['arguments']
    _fields(args, {'region', 'analysis', 'attribution'}, 'region job arguments')
    _tags(args['region'], args['analysis'])
    _attribution(args['attribution'])
    region, analysis = args['region'], args['analysis']
    if region['kind'] == 'inline':
        validate_source(region['source'])
        if record['source'] is not None:
            raise PocketError('Inline region job has no pre-capture source artifact')
    else:
        load_audio_region(region['region'], root)
        if record['source'] != region['region']:
            raise PocketError('Captured region job source identity mismatch')
    if analysis['kind'] == 'peek':
        validate_peek({'path': 'retained-region', 'expected_sha256': '0'*64, 'start_frame': 0,
                       'frames': 1, 'source_origin': 'independently_acquired'}, analysis['settings'])
    elif analysis['kind'] == 'learned_notes':
        from .audio_note_hypotheses import SETTINGS
        from .audio_note_jobs import validate_retained_note_model
        if canonical_bytes(analysis['settings']) != canonical_bytes(SETTINGS):
            raise PocketError('Unsupported region learned note settings')
        validate_retained_note_model(analysis['model'], root)
    else:
        from .audio_pulse_hypotheses import SETTINGS
        if canonical_bytes(analysis['settings']) != canonical_bytes(SETTINGS):
            raise PocketError('Unsupported region learned settings')
        _validate_retained_model(analysis['model'], root)


def _validate_region_job_result(record, root):
    from .audio_region_analysis import _load

    wrapper, region, initial, _, chain = _load(record['result']['artifacts']['hypotheses'], root)
    args = record['arguments']
    requested = args['region']
    if requested['kind'] == 'captured':
        if wrapper['region'] != requested['region']:
            raise PocketError('Committed region differs from captured input')
    else:
        source = requested['source']
        if (region['original']['sha256'] != source['expected_sha256']
                or region['original']['source_origin'] != source['source_origin']
                or region['interval'] != {'start_frame': source['start_frame'],
                    'end_frame_exclusive': source['start_frame'] + source['frames']}):
            raise PocketError('Committed region differs from declared original interval')
    if (wrapper['analysis_kind'] != args['analysis']['kind']
            or canonical_bytes(initial['settings']) != canonical_bytes(args['analysis']['settings'])
            or wrapper['request_attribution'] != args['attribution'] or len(chain) != 1):
        raise PocketError('Committed region analysis differs from job inputs')
    if args['analysis']['kind'] == 'learned_notes':
        from .audio_note_jobs import validate_note_model_result
        validate_note_model_result(args['analysis']['model'], initial['model'], root)
    if args['analysis']['kind'] == 'learned_pulse':
        from .audio_pulse_hypotheses import load_audio_model
        model = args['analysis']['model']
        actual = load_audio_model(initial['model'], root)
        requested_model = load_audio_model(model['model'], root) if model['kind'] == 'inspected' else None
        declaration = requested_model['declaration'] if requested_model else model['declaration']
        expected_profile = requested_model['profile_sha256'] if requested_model else declaration['expected_profile']
        if (actual['declaration']['weights'] != declaration['weights']
                or actual['declaration']['executable'] != declaration['executable']
                or expected_profile is not None and actual['profile_sha256'] != expected_profile):
            raise PocketError('Committed region learned model differs from declared model')


def _write(root, record, **changes):
    updated = {**record, **changes, 'revision': record['revision'] + 1, 'updated_at': _time()}
    if 'state' in changes or 'phase' in changes:
        updated['history'] = [*record['history'], {'state': updated['state'], 'phase': updated['phase'],
                                                  'at': updated['updated_at']}]
    updated['revision_sha256'] = digest({k: v for k, v in updated.items() if k != 'revision_sha256'})
    _atomic_json(_contained(root, f'jobs/{record["job_id"]}/journal.json'), updated)
    return updated


def _lease_held(root, job_id):
    fcntl = _posix()
    path = _contained(root, f'jobs/{job_id}/execution.lease')
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except OSError as error:
        raise PocketError('Job execution lease is missing or unreadable; outcome unknown') from error
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def _reconcile(root, record):
    if record['state'] in _ACTIVE and not _lease_held(root, record['job_id']):
        return _write(root, record, state='interrupted', phase='owner_lease_released',
                      error='Execution owner ended before a terminal commit; inspect evidence and submit a new request.')
    return record


def _status(record, *, status='ok'):
    result = receipt(status=status, job={'schema': 'pocket.job-handle/v1', 'job_id': record['job_id']},
        revision=record['revision_sha256'], state=record['state'], phase=record['phase'],
        source=record['source'], result=(record['result']['artifacts']['hypotheses']
                                        if record['result'] is not None else None), error=record['error'],
        history=record['history'][-8:], omitted_history=max(0, len(record['history']) - 8),
        last_checkpoint_at=record['updated_at'],
        coverage={'native_execution': False, 'cancellation': 'cooperative_at_analysis_and_commit_boundaries',
                  'lease_means': 'execution_ownership_not_progress_or_decoder_liveness',
                  'crop_limit': '20_seconds_of_source_audio_not_a_wall_clock_deadline',
                  'staging': 'retained_private_diagnostics_not_a_committed_job_result', 'automatic_retry': False})
    if record['schema'] == 'pocket.analysis-job/v2':
        result['coverage'].update(analysis_kind='audio_pulse_hypotheses',
            model_environment='optional_explicit_local_runtime',
            crop_limit='2_to_20_seconds_in_the_qualified_PCM16_profile',
            cancellation='cooperative_at_model_preparation_inference_and_commit_boundaries',
            model_subprocess_timeout_seconds=60)
    if record['schema'] == 'pocket.analysis-job/v4':
        result['coverage'].update(analysis_kind='audio_note_hypotheses',
            model_environment='optional_explicit_local_runtime',
            crop_limit='2_to_20_seconds_in_the_qualified_PCM16_or_FLOAT32_profile',
            cancellation='cooperative_at_model_preparation_inference_and_commit_boundaries',
            model_subprocess_timeout_seconds=60)
    if record['schema'] == 'pocket.analysis-job/v3':
        result['coverage'].update(analysis_kind='audio_region_hypotheses',
            original_bytes_retained=False, capture='inline_after_durable_job_and_lease',
            cancellation='cooperative_between_capture_blocks_analysis_and_commit_boundaries')
    return result


def job_status(store_root: str, job_id: str) -> dict:
    """Inspect an owned analysis job; reconcile an ended execution lease without retry."""
    root = _root(store_root)
    with _state_lock(root, job_id):
        return _status(_reconcile(root, _read(root, job_id)))


def job_cancel(store_root: str, job_id: str, expected_revision: str) -> dict:
    """Request cancellation at the next safe boundary; never signal or kill a PID."""
    if not isinstance(expected_revision, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_revision):
        raise PocketError('Cancellation requires the exact current job revision')
    root = _root(store_root)
    with _state_lock(root, job_id):
        record = _reconcile(root, _read(root, job_id))
        if record['revision_sha256'] != expected_revision or record['state'] in _TERMINAL:
            return _status(record, status='conflict')
        if record['state'] != 'cancel_requested':
            record = _write(root, record, state='cancel_requested', phase='cancel_pending_safe_boundary')
        return _status(record)


def _spawn(root, job_id, nonce, lease_fd):
    log_path = _contained(root, f'jobs/{job_id}/worker.log')
    with log_path.open('xb') as log:
        process = subprocess.Popen([sys.executable, '-m', 'pocket_music.audio_hypothesis_jobs',
            str(root), job_id, nonce, str(lease_fd)], stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, pass_fds=(lease_fd,), start_new_session=True,
            env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])})
    # Reaping is housekeeping only; correctness uses the durable lease/journal.
    threading.Thread(target=process.wait, daemon=True).start()


def audio_hypothesis_submit(store_root: str, request_id: str, source: AudioHypothesisSource,
                            settings: AudioHypothesisSettings, attribution: AudioHypothesisAttribution) -> dict:
    """Snapshot source bytes and start an optional owned local analysis worker."""
    fcntl = _posix()
    root = _root(store_root)
    inputs = {'source': source, 'settings': settings, 'attribution': attribution}

    def work():
        from .audio_hypotheses import prepare_hypothesis_input
        prepared = prepare_hypothesis_input(source=source, settings=settings,
                                            attribution=attribution, store_root=str(root))
        job_id = 'job-' + digest(['audio_hypotheses', request_id, inputs])[:40]
        folder = _folder(root, job_id)
        folder.mkdir(parents=True, exist_ok=False)
        fd = os.open(folder / 'execution.lease', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            now, nonce = _time(), os.urandom(32).hex()
            record = {'schema': 'pocket.analysis-job/v1', 'job_id': job_id, 'revision': -1,
                'state': 'queued', 'nonce': nonce,
                'arguments': {key: prepared[key] for key in ('source', 'settings', 'attribution')},
                'source': prepared['source_handle'], 'created_at': now, 'updated_at': now,
                'phase': 'immutable_source_captured', 'result': None, 'error': None, 'history': []}
            with _state_lock(root, job_id):
                record = _write(root, record, state='queued')
                try:
                    _spawn(root, job_id, nonce, fd)
                except BaseException as error:
                    _write(root, record, state='failed', phase='launch_failed', error=str(error)[:1500])
                    raise
            # This receipt records submission, not a claim about current progress.
            result = _status(record)
            result['request_id'] = request_id
            result['coverage']['observation'] = 'submission_snapshot;call_job_status_for_current_state'
            return result
        finally:
            # Closing (not LOCK_UN) keeps the shared open-description lock held
            # by the inherited worker fd. No arbitrary descendants inherit it.
            os.close(fd)
    return run_request(root, request_id, 'audio_hypothesis_submit', inputs, work)


def audio_pulse_submit(*, store_root: str, request_id: str, source: AudioHypothesisSource,
                       model: AudioPulseModel, settings: AudioPulseSettings,
                       attribution: AudioHypothesisAttribution) -> dict:
    """Submit the same learned-pulse primitive with explicit optional model inputs."""
    from .audio_pulse_hypotheses import prepare_pulse_input, prepare_pulse_model
    fcntl = _posix()
    root = _root(store_root)
    inputs = {'source': source, 'model': model, 'settings': settings, 'attribution': attribution}

    def work():
        prepare_pulse_model(model=model, store_root=str(root))
        prepared = prepare_pulse_input(source=source, settings=settings,
                                       attribution=attribution, store_root=str(root))
        job_id = 'job-' + digest(['audio_pulse_hypotheses', request_id, inputs])[:40]
        folder = _folder(root, job_id)
        folder.mkdir(parents=True, exist_ok=False)
        fd = os.open(folder / 'execution.lease', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            now, nonce = _time(), os.urandom(32).hex()
            record = {'schema': 'pocket.analysis-job/v2', 'analysis_kind': 'audio_pulse_hypotheses',
                'job_id': job_id, 'revision': -1, 'state': 'queued', 'nonce': nonce,
                'arguments': {**{key: prepared[key] for key in ('source', 'settings', 'attribution')},
                              'model': model},
                'source': prepared['source_handle'], 'created_at': now, 'updated_at': now,
                'phase': 'immutable_source_captured', 'result': None, 'error': None, 'history': []}
            with _state_lock(root, job_id):
                record = _write(root, record, state='queued')
                try:
                    _spawn(root, job_id, nonce, fd)
                except BaseException as error:
                    _write(root, record, state='failed', phase='launch_failed', error=str(error)[:1500])
                    raise
            result = _status(record)
            result['request_id'] = request_id
            result['coverage']['observation'] = 'submission_snapshot;call_job_status_for_current_state'
            return result
        finally:
            os.close(fd)
    return run_request(root, request_id, 'audio_pulse_submit', inputs, work)


def audio_region_submit(*, store_root: str, request_id: str, region: AudioRegionInput,
                        analysis: AudioRegionAnalysis, attribution: AudioHypothesisAttribution) -> dict:
    """Queue before inline capture; run the same public region analysis under an owned lease."""
    fcntl = _posix()
    root = _root(store_root)
    inputs = {'region': region, 'analysis': analysis, 'attribution': attribution}
    source = region.get('region') if isinstance(region, dict) and region.get('kind') == 'captured' else None
    _validate_region_job_arguments({'arguments': inputs, 'source': source}, root)

    def work():
        job_id = 'job-' + digest(['audio_region_hypotheses', request_id, inputs])[:40]
        folder = _folder(root, job_id)
        folder.mkdir(parents=True, exist_ok=False)
        fd = os.open(folder / 'execution.lease', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            now, nonce = _time(), os.urandom(32).hex()
            record = {'schema': 'pocket.analysis-job/v3', 'analysis_kind': 'audio_region_hypotheses',
                'job_id': job_id, 'revision': -1, 'state': 'queued', 'nonce': nonce,
                'arguments': inputs,
                'source': source, 'created_at': now, 'updated_at': now,
                'phase': 'original_declared_capture_pending' if source is None else 'immutable_region_selected', 'result': None, 'error': None, 'history': []}
            with _state_lock(root, job_id):
                record = _write(root, record, state='queued')
                try:
                    _spawn(root, job_id, nonce, fd)
                except BaseException as error:
                    _write(root, record, state='failed', phase='launch_failed', error=str(error)[:1500])
                    raise
            result = _status(record)
            result['request_id'] = request_id
            result['coverage']['observation'] = 'submission_snapshot;call_job_status_for_current_state'
            return result
        finally:
            os.close(fd)
    return run_request(root, request_id, 'audio_region_submit', inputs, work)


def _collect_graph(result, staging, root):
    _verify_handles(result, staging)
    pending, seen = [result], set()
    while pending:
        value = pending.pop()
        if isinstance(value, dict) and value.get('schema') == 'pocket.artifact-handle/v1':
            identity = canonical_bytes(value)
            if identity in seen:
                continue
            seen.add(identity)
            payload = read_bytes(value, staging)
            copied = put_bytes(payload, root, Path(value['artifact_uri']).name, value['artifact_schema'])
            if copied != value:
                raise PocketError('Job artifact collection identity mismatch')
            if Path(value['artifact_uri']).name == 'record.json':
                pending.append(json.loads(payload))
        elif isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    _verify_handles(result, root)


def _run_worker(root, job_id, nonce, lease_fd):
    from .audio_hypotheses import audio_hypotheses
    root = _root(root)
    path = _contained(root, f'jobs/{job_id}/execution.lease')
    expected, inherited = path.stat(), os.fstat(lease_fd)
    if (expected.st_dev, expected.st_ino) != (inherited.st_dev, inherited.st_ino):
        raise PocketError('Worker did not inherit its exact execution lease')
    os.set_inheritable(lease_fd, False)
    try:
        with _state_lock(root, job_id, wait_seconds=5):
            record = _read(root, job_id)
            if record['nonce'] != nonce or record['state'] not in {'queued', 'cancel_requested'}:
                raise PocketError('Worker ownership or initial state mismatch')
            if record['state'] == 'cancel_requested':
                _write(root, record, state='cancelled', phase='cancelled_before_analysis')
                return
            record = _write(root, record, state='running', phase='analysis_started')
        staging = _contained(root, f'jobs/{job_id}/staging')
        if record['schema'] in ('pocket.analysis-job/v2', 'pocket.analysis-job/v3', 'pocket.analysis-job/v4'):
            from .audio_pulse_hypotheses import audio_pulse_hypotheses, model_execution_context

            def check_cancelled():
                with _state_lock(root, job_id, wait_seconds=5):
                    fresh = _read(root, job_id, verify_evidence=record['schema'] != 'pocket.analysis-job/v3')
                    if (fresh['schema'] != record['schema']
                            or fresh.get('analysis_kind') != record.get('analysis_kind')
                            or canonical_bytes(fresh['arguments']) != canonical_bytes(record['arguments'])
                            or fresh['source'] != record['source']):
                        raise PocketError('Worker arguments changed outside its protocol')
                    if fresh['nonce'] != nonce or fresh['state'] not in {'running', 'cancel_requested'}:
                        raise PocketError('Model worker state changed outside its protocol')
                    if fresh['state'] == 'cancel_requested':
                        raise _JobCancellation('Cancellation observed at a model boundary')

            # Retained model handles remain byte-identical in the staging store.
            # This operation copies artifacts only; it does not execute a model.
            if record['schema'] == 'pocket.analysis-job/v3':
                from .audio_region_analysis import audio_region_hypotheses
                from .audio_regions import region_execution_context
                _collect_graph(record['arguments'], root, staging)
                with region_execution_context(cancellation_check=check_cancelled), model_execution_context(
                        lease_fds=(lease_fd,), cancellation_check=check_cancelled):
                    result = audio_region_hypotheses(store_root=str(staging), request_id='worker-analysis',
                                                    **record['arguments'])
            else:
                _collect_graph(record['arguments'].get('model'), root, staging)
                with model_execution_context(lease_fds=(lease_fd,), cancellation_check=check_cancelled):
                    if record['schema'] == 'pocket.analysis-job/v4':
                        from .audio_note_hypotheses import audio_note_hypotheses
                        result = audio_note_hypotheses(store_root=str(staging), request_id='worker-analysis',
                                                      **record['arguments'])
                    else:
                        result = audio_pulse_hypotheses(store_root=str(staging), request_id='worker-analysis',
                                                       **record['arguments'])
        else:
            result = audio_hypotheses(store_root=str(staging), request_id='worker-analysis', **record['arguments'])
        if result.get('status') != 'ok':
            raise PocketError('Audio primitive did not produce a complete successful hypothesis result')
        _verify_handles(result, staging)
        with _state_lock(root, job_id, wait_seconds=5):
            fresh = _read(root, job_id)
            if record['schema'] in ('pocket.analysis-job/v2', 'pocket.analysis-job/v3', 'pocket.analysis-job/v4') and (
                    fresh['schema'] != record['schema']
                    or fresh.get('analysis_kind') != record.get('analysis_kind')
                    or canonical_bytes(fresh['arguments']) != canonical_bytes(record['arguments'])
                    or fresh['source'] != record['source']):
                raise PocketError('Model/region worker inputs changed before commit')
            record = fresh
            if record['nonce'] != nonce or record['state'] not in {'running', 'cancel_requested'}:
                raise PocketError('Worker state changed outside its protocol')
            if record['state'] == 'cancel_requested':
                _write(root, record, state='cancelled', phase='cancelled_after_analysis')
                return
            _collect_graph(result, staging, root)
            if record['schema'] == 'pocket.analysis-job/v3':
                _validate_region_job_result({**record, 'result': result}, root)
            elif record['schema'] == 'pocket.analysis-job/v2':
                _validate_model_job_result({**record, 'result': result}, root)
            elif record['schema'] == 'pocket.analysis-job/v4':
                from .audio_note_jobs import validate_note_job_result
                validate_note_job_result({**record, 'result': result}, root)
            _write(root, record, state='completed', phase='verified_result_committed', result=result)
    except BaseException as error:
        try:
            with _state_lock(root, job_id, wait_seconds=5):
                record = _read(root, job_id)
                if record['nonce'] == nonce and record['state'] in _ACTIVE:
                    cancelled = isinstance(error, _JobCancellation) and record['state'] == 'cancel_requested'
                    _write(root, record, state='cancelled' if cancelled else
                           ('failed' if isinstance(error, Exception) else 'interrupted'),
                           phase=('cancelled_at_region_boundary' if record['schema'] == 'pocket.analysis-job/v3'
                                  else 'cancelled_at_model_boundary') if cancelled else 'worker_failed_before_commit',
                           error=None if cancelled else str(error)[:1500])
        except (OSError, PocketError):
            # A lost/corrupt journal is not repaired by inventing a terminal.
            pass
        if not isinstance(error, _JobCancellation):
            raise
    finally:
        os.close(lease_fd)


if __name__ == '__main__':
    if len(sys.argv) != 5:
        raise SystemExit('Expected store root, job ID, invocation nonce and inherited lease fd')
    _run_worker(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]))
