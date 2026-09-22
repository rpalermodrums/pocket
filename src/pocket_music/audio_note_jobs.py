"""Owned optional note workers using the existing revisioned job engine."""
from __future__ import annotations

import os

from .artifact_store import _contained, _root, canonical_bytes, digest, read_bytes, run_request
from .audio_hypothesis_types import AudioHypothesisAttribution, AudioHypothesisSource
from .audio_note_types import AudioNoteModel, AudioNoteSettings
from .errors import PocketError


def validate_retained_note_model(model, root):
    """Resolve retained declarations without touching the optional environment."""
    from .audio_hypotheses import _fields
    from .audio_note_hypotheses import load_audio_note_model, validate_note_declaration

    if not isinstance(model, dict):
        raise PocketError('Invalid retained note model input')
    if model.get('kind') == 'inspected':
        _fields(model, {'kind', 'model'}, 'retained inspected note model')
        retained = load_audio_note_model(model['model'], str(root))
        declaration = retained['declaration']
        expected_profile = retained['profile_sha256']
    elif model.get('kind') == 'inline':
        _fields(model, {'kind', 'declaration', 'qualification'}, 'retained inline note model')
        if model['qualification'] != 'synthetic_onnx_cpu_v1':
            raise PocketError('Invalid retained note qualification')
        declaration = model['declaration']
        expected_profile = declaration.get('expected_profile') if isinstance(declaration, dict) else None
    else:
        raise PocketError('Unknown retained note model kind')
    return validate_note_declaration(declaration), expected_profile


def validate_note_job_arguments(record, root):
    """Validate v4 immutable source and settings, without optional execution."""
    from .assets import identify_audio
    from .audio_hypotheses import _attribution, _fields, _source
    from .audio_note_hypotheses import SETTINGS, validate_note_source_bytes

    args = record['arguments']
    _fields(args, {'source', 'settings', 'attribution', 'model'}, 'note job arguments')
    _source(args['source'], {'bpm_hint': None, 'beats_per_bar': 4})
    _attribution(args['attribution'])
    if canonical_bytes(args['settings']) != canonical_bytes(SETTINGS):
        raise PocketError('Unsupported retained note settings')
    source, handle = args['source'], record['source']
    if (not isinstance(handle, dict) or handle.get('artifact_schema') != 'pocket.audio-source-bytes/v1'
            or source['expected_sha256'] != handle.get('sha256')
            or source['path'] != str(_contained(root, handle['artifact_uri']))):
        raise PocketError('Note job source is not its immutable snapshot')
    payload = read_bytes(handle, root)
    # This is the owned artifact, never the user's now-external recording path.
    asset = identify_audio(_contained(root, handle['artifact_uri']))
    metadata = {key: asset[key] for key in ('sha256', 'frames', 'sample_rate', 'channels', 'format', 'subtype')}
    metadata.update(start_frame=source['start_frame'],
                    end_frame_exclusive=source['start_frame'] + source['frames'],
                    source_origin=source['source_origin'])
    validate_note_source_bytes(payload, metadata)
    validate_retained_note_model(args['model'], root)


def validate_note_model_result(model, actual_handle, root):
    from .audio_note_hypotheses import load_audio_note_model

    declaration, expected_profile = validate_retained_note_model(model, root)
    actual = load_audio_note_model(actual_handle, str(root))
    if (actual['declaration']['weights'] != declaration['weights']
            or actual['declaration']['executable'] != declaration['executable']
            or actual['declaration']['adapter'] != declaration['adapter']
            or expected_profile is not None and actual['profile_sha256'] != expected_profile):
        raise PocketError('Committed note result differs from declared model')


def validate_note_job_result(record, root):
    from .audio_note_hypotheses import load_note_hypotheses

    retained = load_note_hypotheses(record['result']['artifacts']['hypotheses'], str(root))
    args = record['arguments']
    validate_note_model_result(args['model'], retained['model'], root)
    if (retained['original'] != record['source']
            or canonical_bytes(retained['settings']) != canonical_bytes(args['settings'])
            or retained['request_attribution'] != args['attribution']
            or retained['source']['start_frame'] != args['source']['start_frame']
            or retained['source']['end_frame_exclusive'] != args['source']['start_frame'] + args['source']['frames']
            or retained['source']['source_origin'] != args['source']['source_origin']):
        raise PocketError('Committed note result differs from job inputs')


def audio_note_submit(*, store_root: str, request_id: str, source: AudioHypothesisSource,
                      model: AudioNoteModel, settings: AudioNoteSettings,
                      attribution: AudioHypothesisAttribution) -> dict:
    """Submit the identical standalone note primitive with owned cancellation."""
    from .audio_hypothesis_jobs import _folder, _posix, _spawn, _state_lock, _status, _time, _write
    from .audio_note_hypotheses import prepare_note_input, prepare_note_model

    fcntl = _posix()
    root = _root(store_root)
    inputs = {'source': source, 'model': model, 'settings': settings, 'attribution': attribution}

    def work():
        prepare_note_model(model=model, store_root=str(root))
        prepared = prepare_note_input(source=source, settings=settings,
                                      attribution=attribution, store_root=str(root))
        job_id = 'job-' + digest(['audio_note_hypotheses', request_id, inputs])[:40]
        folder = _folder(root, job_id)
        folder.mkdir(parents=True, exist_ok=False)
        fd = os.open(folder / 'execution.lease', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            now, nonce = _time(), os.urandom(32).hex()
            record = {'schema': 'pocket.analysis-job/v4', 'analysis_kind': 'audio_note_hypotheses',
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
    return run_request(root, request_id, 'audio_note_submit', inputs, work)
