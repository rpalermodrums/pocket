# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded feedback selection and pagination, independent of native/render adapters."""
from __future__ import annotations

import re

from .artifact_store import _verify_handles, canonical_bytes, digest, receipt
from .errors import PocketError


def _integer(value, name, low=0, high=2**63 - 1):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise PocketError(f"{name} must be an integer from {low} through {high}")
    return value


def _text(value, field, limit=240):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise PocketError(f"{field} must be nonempty text, at most {limit} characters")
    return value


def _interval(value):
    if not isinstance(value, list) or len(value) != 2:
        raise PocketError('Expected two exact interval frames')
    start, end = (_integer(x, 'interval frame') for x in value)
    if end <= start:
        raise PocketError('Expected nonempty half-open interval')
    return start, end


def query_feedback_records(*, store_root, feedback, loader, schema, coverage,
                           actor=None, actor_kind=None, decision=None, render_sha256=None,
                           interval_frames=None, limit=16, cursor=None, max_bytes=16384,
                           identity_extra=None, extra_filter=None):
    """Validate every supplied record before filtering; preserve native receipt/cursor semantics."""
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
    schemas = (schema,) if isinstance(schema, str) else tuple(schema)
    if any(not isinstance(h, dict) or h.get('schema') != 'pocket.artifact-handle/v1'
           or h.get('artifact_schema') not in schemas for h in feedback):
        raise PocketError('Only ' + ' or '.join(s.removeprefix('pocket.') for s in schemas) + ' handles are supported')
    # One shared graph walk retains existing global verification budgets.
    _verify_handles(feedback, store_root)
    if len({h['sha256'] for h in feedback}) != len(feedback):
        raise PocketError('Duplicate feedback identities')
    identity = digest([feedback, actor, actor_kind, decision, render_sha256, interval_frames])
    if identity_extra is not None:
        identity = digest([identity, identity_extra])
    offset = 0
    if cursor is not None:
        if not isinstance(cursor, str) or len(cursor) > 80 or not re.fullmatch(identity + r':[0-9]+', cursor):
            raise PocketError('Stale or malformed feedback cursor')
        offset = int(cursor.split(':')[1])
    validated = [loader(h) for h in feedback]
    rows = [r for r in validated if (actor is None or r['actor'] == actor)
            and (actor_kind is None or r['actor_kind'] == actor_kind)
            and (decision is None or r['decision'] == decision)
            and (render_sha256 is None or r['render_sha256'] == render_sha256)
            and (interval is None or r['interval_frames'][0] < interval[1] and interval[0] < r['interval_frames'][1])
            and (extra_filter is None or extra_filter(r))]
    if offset > len(rows):
        raise PocketError('Feedback cursor exceeds filtered results')
    selected = rows[offset:offset + limit]
    while True:
        end = offset + len(selected)
        result = receipt(coverage=coverage, items=selected, input_total=len(feedback), total=len(rows),
                         complete=end == len(rows), omitted=len(rows) - end,
                         next_cursor=f'{identity}:{end}' if end < len(rows) else None)
        if len(canonical_bytes(result)) <= max_bytes:
            return result
        selected = selected[:-1]
        if not selected:
            return receipt(status='needs_input', coverage=coverage, items=[], input_total=len(feedback),
                           total=len(rows), complete=False, omitted=len(rows) - offset,
                           next_cursor=f'{identity}:{offset}',
                           next_actions=['Increase max_bytes or read the immutable feedback artifact'])
