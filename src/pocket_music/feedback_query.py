"""Bounded explicit-handle retrieval of attributed audition feedback."""
from __future__ import annotations

from typing import Literal

from .artifact_store import ArtifactHandle, read_record
from .auditions import _validate_attachment
from .errors import PocketError
from .feedback_selection import _interval, query_feedback_records
from .native_candidates import _text

_FIELDS = {'schema', 'attachment', 'interval_frames', 'actor', 'actor_kind', 'note',
           'decision', 'evidence_kind', 'render_sha256', 'candidate'}
_COVERAGE = {'provider': 'audition-feedback-query-v1', 'execution': 'retained_evidence_only',
             'provider_playback': False, 'native_execution': False,
             'musical_preference_inference': False}


def _load(handle, store_root, attachments):
    try:
        record = read_record(handle, store_root, 'pocket.audition-feedback/v1')
        if not isinstance(record, dict) or set(record) != _FIELDS:
            raise PocketError('Invalid audition feedback fields')
        _text(record['actor'], 'actor')
        _text(record['note'], 'note', 8000)
        if record['actor_kind'] not in ('human', 'agent'):
            raise PocketError('Invalid feedback actor kind')
        if record['decision'] not in (None, 'keep', 'revise', 'reject', 'no_addition'):
            raise PocketError('Invalid feedback decision')
        expected = 'attributed_human_listening' if record['actor_kind'] == 'human' else 'agent_report'
        if record['evidence_kind'] != expected:
            raise PocketError('Feedback evidence kind differs from attribution')
        key = record['attachment']['sha256']
        if key not in attachments:
            attachments[key] = _validate_attachment(record['attachment'], store_root)
        attachment = attachments[key]
        start, end = _interval(record['interval_frames'])
        if (end > attachment['signal']['frames'] or record['render_sha256'] != attachment['audio']['sha256']
                or record['candidate'] != attachment['candidate']):
            raise PocketError('Feedback render, candidate or interval binding mismatch')
        return {'feedback': handle, 'actor': record['actor'], 'actor_kind': record['actor_kind'],
                'note': record['note'], 'decision': record['decision'], 'evidence_kind': record['evidence_kind'],
                'interval_frames': [start, end], 'candidate': record['candidate'],
                'attachment': record['attachment'], 'render_sha256': record['render_sha256'],
                'sample_rate': attachment['actual_settings']['sample_rate']}
    except PocketError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as error:
        raise PocketError('Malformed retained audition feedback') from error


def audition_feedback_query(*, store_root: str, feedback: list[ArtifactHandle],
        actor: str | None = None, actor_kind: Literal['human', 'agent'] | None = None,
        decision: Literal['keep', 'revise', 'reject', 'no_addition'] | None = None,
        render_sha256: str | None = None, interval_frames: list[int] | None = None,
        limit: int = 16, cursor: str | None = None, max_bytes: int = 16384) -> dict:
    """Query supplied feedback records; preserve reported judgments without inference."""
    attachments = {}
    return query_feedback_records(store_root=store_root, feedback=feedback,
        loader=lambda h: _load(h, store_root, attachments), schema='pocket.audition-feedback/v1',
        coverage=_COVERAGE, actor=actor, actor_kind=actor_kind, decision=decision,
        render_sha256=render_sha256, interval_frames=interval_frames, limit=limit, cursor=cursor, max_bytes=max_bytes)
