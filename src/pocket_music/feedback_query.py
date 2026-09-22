"""Bounded explicit-handle retrieval of attributed audition feedback."""
from __future__ import annotations

import re
from typing import Literal

from .artifact_store import ArtifactHandle, _verify_handles, canonical_bytes, digest, read_record, receipt
from .auditions import _integer, _validate_attachment
from .errors import PocketError
from .native_candidates import _text

_FIELDS = {'schema', 'attachment', 'interval_frames', 'actor', 'actor_kind', 'note',
           'decision', 'evidence_kind', 'render_sha256', 'candidate'}
_COVERAGE = {'provider': 'audition-feedback-query-v1', 'execution': 'retained_evidence_only',
             'provider_playback': False, 'native_execution': False,
             'musical_preference_inference': False}


def _interval(value):
    if not isinstance(value, list) or len(value) != 2:
        raise PocketError('Expected two exact interval frames')
    start, end = (_integer(x, 'interval frame') for x in value)
    if end <= start:
        raise PocketError('Expected nonempty half-open interval')
    return start, end


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
    if not isinstance(feedback, list) or not 1 <= len(feedback) <= 128:
        raise PocketError('Feedback requires 1–128 explicit unique handles')
    _integer(limit, 'limit', 1, 128)
    _integer(max_bytes, 'max_bytes', 4096, 65536)
    if actor is not None:
        _text(actor, 'actor')
    if actor_kind not in (None, 'human', 'agent') or decision not in (None, 'keep', 'revise', 'reject', 'no_addition'):
        raise PocketError('Unknown feedback filter')
    if render_sha256 is not None and (not isinstance(render_sha256, str) or not re.fullmatch('[0-9a-f]{64}', render_sha256)):
        raise PocketError('Expected exact lowercase render SHA256')
    interval = None
    if interval_frames is not None:
        if render_sha256 is None:
            raise PocketError('Interval filter requires exact render identity')
        interval = _interval(interval_frames)
    if any(not isinstance(h, dict) or h.get('schema') != 'pocket.artifact-handle/v1'
           or h.get('artifact_schema') != 'pocket.audition-feedback/v1' for h in feedback):
        raise PocketError('Only audition-feedback/v1 handles are supported')
    # One shared graph walk retains existing global verification budgets.
    _verify_handles(feedback, store_root)
    if len({h['sha256'] for h in feedback}) != len(feedback):
        raise PocketError('Duplicate feedback identities')
    identity = digest([feedback, actor, actor_kind, decision, render_sha256, interval_frames])
    offset = 0
    if cursor is not None:
        if not isinstance(cursor, str) or len(cursor) > 80 or not re.fullmatch(identity + r':[0-9]+', cursor):
            raise PocketError('Stale or malformed feedback cursor')
        offset = int(cursor.split(':')[1])
    attachments = {}
    validated = [_load(h, store_root, attachments) for h in feedback]
    rows = [r for r in validated if (actor is None or r['actor'] == actor)
            and (actor_kind is None or r['actor_kind'] == actor_kind)
            and (decision is None or r['decision'] == decision)
            and (render_sha256 is None or r['render_sha256'] == render_sha256)
            and (interval is None or r['interval_frames'][0] < interval[1] and interval[0] < r['interval_frames'][1])]
    if offset > len(rows):
        raise PocketError('Feedback cursor exceeds filtered results')
    selected = rows[offset:offset + limit]
    while True:
        end = offset + len(selected)
        result = receipt(coverage=_COVERAGE, items=selected, input_total=len(feedback), total=len(rows),
                         complete=end == len(rows), omitted=len(rows) - end,
                         next_cursor=f'{identity}:{end}' if end < len(rows) else None)
        if len(canonical_bytes(result)) <= max_bytes:
            return result
        selected = selected[:-1]
        if not selected:
            return receipt(status='needs_input', coverage=_COVERAGE, items=[], input_total=len(feedback),
                           total=len(rows), complete=False, omitted=len(rows) - offset,
                           next_cursor=f'{identity}:{offset}',
                           next_actions=['Increase max_bytes or read the immutable feedback artifact'])
