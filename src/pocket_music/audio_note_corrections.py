# SPDX-License-Identifier: AGPL-3.0-only
"""Attributed revisions of retained learned note evidence; never reruns a model."""
from __future__ import annotations

import copy
import re
from typing import Literal

from .artifact_store import (
    ArtifactHandle,
    _verify_handles,
    canonical_bytes,
    digest,
    put_record,
    read_record,
    receipt,
    run_request,
)
from .audio_hypotheses import _annotation, _attribution, _fields, _pointer, _strings, _text
from .audio_hypothesis_types import AudioHypothesisAttribution, AudioHypothesisCorrection
from .errors import PocketError
from .material import integer

SCHEMA = 'pocket.audio-note-hypotheses/v1'
ANALYSIS_SCHEMA = 'pocket.audio-note-analysis/v1'
COVERAGE = {'provider': 'audio-note-hypothesis-corrections-v1', 'execution': 'retained_evidence_only',
            'background_job': False, 'native_execution': False,
            'learned_models': ['basic_pitch_onnx_cpu_v1'], 'transcription': 'uncertain_note_hypotheses',
            'human_listening': 'not_performed'}


def _load(handle, store_root):
    """Validate every revision and the initial model projection without inference."""
    from .audio_note_hypotheses import load_note_hypotheses

    _verify_handles(handle, store_root)
    chain, seen = [], set()
    while handle is not None:
        record = read_record(handle, store_root, SCHEMA)
        if len(chain) >= 33 or handle['sha256'] in seen:
            raise PocketError('Hypothesis revision history exceeds bounds or cycles')
        seen.add(handle['sha256'])
        if 'parent' not in record:
            raise PocketError('Missing hypothesis parent')
        chain.append((handle, record))
        handle = record['parent']
    chain.reverse()
    initial = load_note_hypotheses(chain[0][0], store_root)
    evidence = read_record(initial['analysis'], store_root, ANALYSIS_SCHEMA)
    annotations = copy.deepcopy(initial['annotations'])
    ids = {row['annotation_id'] for row in annotations}
    if len(ids) != len(annotations) or len(annotations) > 4096:
        raise PocketError('Invalid initial learned annotation identities or count')
    correction_ids = set()
    for index, (_, record) in enumerate(chain[1:], 1):
        _fields(record, {'schema', 'parent', 'annotations', 'revision_index', 'annotation_count'}, 'correction record')
        integer(record['revision_index'], 'revision_index', index, index)
        if record['parent'] != chain[index - 1][0]:
            raise PocketError('Hypothesis parent identity mismatch')
        rows = record['annotations']
        if not isinstance(rows, list) or not 1 <= len(rows) <= 128:
            raise PocketError('Invalid retained correction count')
        prior_ids = ids.copy()
        for row in rows:
            _fields(row, {'annotation', 'support', 'uncertainty', 'attribution', 'supersedes',
                          'annotation_id', 'correction_id'}, 'retained correction')
            body = {key: value for key, value in row.items() if key != 'annotation_id'}
            if row['annotation_id'] != 'audio:' + digest([record['parent'], body]) or row['annotation_id'] in ids:
                raise PocketError('Retained correction identity mismatch')
            ids.add(row['annotation_id'])
            _attribution(row['attribution'])
            key = _text(row['correction_id'], 'correction_id')
            if key in correction_ids:
                raise PocketError('Duplicate retained correction identity')
            correction_ids.add(key)
            _annotation(row['annotation'], initial['source'])
            _strings(row['uncertainty'], 'uncertainty')
            _strings(row['supersedes'], 'supersedes')
            if len(set(row['supersedes'])) != len(row['supersedes']) or any(key not in prior_ids for key in row['supersedes']):
                raise PocketError('Retained supersession identity mismatch')
            if not isinstance(row['support'], list) or not 1 <= len(row['support']) <= 32:
                raise PocketError('Invalid retained support count')
            for ref in row['support']:
                _fields(ref, {'kind', 'reference'}, 'support')
                if ref['kind'] == 'analysis_pointer' and isinstance(ref['reference'], str) and ref['reference'].startswith('/analysis/'):
                    _pointer(evidence, ref['reference'])
                elif ref['kind'] != 'annotation_id' or not isinstance(ref['reference'], str) or ref['reference'] not in prior_ids:
                    raise PocketError('Retained support does not resolve')
        annotations.extend(rows)
        integer(record['annotation_count'], 'annotation_count', len(annotations), len(annotations))
    if len(annotations) > 4096:
        raise PocketError('Hypothesis annotation history exceeds 4096')
    return initial, annotations, chain


def correct(*, store_root: str, request_id: str, parent: ArtifactHandle,
                             expected_revision: str, corrections: list[AudioHypothesisCorrection],
                             attribution: AudioHypothesisAttribution) -> dict:
    """Append explicit attributed alternatives; retain all superseded evidence."""
    _attribution(attribution)
    if not isinstance(corrections, list) or not 1 <= len(corrections) <= 128:
        raise PocketError('Corrections require 1–128 entries')

    def work():
        initial, prior, chain = _load(parent, store_root)
        if expected_revision != parent['sha256']:
            raise PocketError('Stale hypothesis revision')
        if len(chain) > 32 or len(prior) + len(corrections) > 4096:
            raise PocketError('Correction exceeds 32 revisions or 4096 annotations')
        evidence = read_record(initial['analysis'], store_root, ANALYSIS_SCHEMA)
        ids = {row['annotation_id'] for row in prior}
        correction_ids = {row['correction_id'] for row in prior if 'correction_id' in row}
        rows = []
        for item in corrections:
            _fields(item, {'correction_id', 'supersedes', 'annotation', 'support', 'uncertainty'}, 'correction')
            key = _text(item['correction_id'], 'correction_id')
            if key in correction_ids:
                raise PocketError('Duplicate correction identity')
            correction_ids.add(key)
            _strings(item['supersedes'], 'supersedes')
            if len(set(item['supersedes'])) != len(item['supersedes']) or any(key not in ids for key in item['supersedes']):
                raise PocketError('Supersedes contains duplicate or unknown annotation identities')
            _annotation(item['annotation'], initial['source'])
            _strings(item['uncertainty'], 'annotation uncertainty')
            support = item['support']
            if not isinstance(support, list) or not 1 <= len(support) <= 32:
                raise PocketError('Correction support requires 1–32 resolved references')
            for ref in support:
                _fields(ref, {'kind', 'reference'}, 'support')
                if ref['kind'] == 'analysis_pointer':
                    if not isinstance(ref['reference'], str) or not ref['reference'].startswith('/analysis/'):
                        raise PocketError('Support pointer must address retained analysis')
                    _pointer(evidence, ref['reference'])
                elif ref['kind'] == 'annotation_id':
                    if not isinstance(ref['reference'], str) or ref['reference'] not in ids:
                        raise PocketError('Support names an unknown prior annotation')
                else:
                    raise PocketError('Unknown support reference kind')
            row = copy.deepcopy(item)
            row['attribution'] = copy.deepcopy(attribution)
            row['annotation_id'] = 'audio:' + digest([parent, row])
            rows.append(row)
        record = {'schema': SCHEMA, 'parent': copy.deepcopy(parent), 'annotations': rows,
                  'revision_index': len(chain), 'annotation_count': len(prior) + len(rows)}
        handle = put_record(record, store_root)
        return receipt(request_id, artifacts={'hypotheses': handle},
                       change_summary={'added_annotations': len(rows), 'revision_index': len(chain)}, coverage=COVERAGE,
                       uncertainty=['Corrections are attributed hypotheses, not automatic musical approval.'])
    result = run_request(store_root, request_id, 'audio_hypothesis_correct',
                       {'parent': parent, 'expected_revision': expected_revision, 'corrections': corrections,
                        'attribution': attribution}, work)
    _load(result['artifacts']['hypotheses'], store_root)
    return result


def query(*, store_root: str, hypotheses: ArtifactHandle,
                           view: Literal['summary', 'annotations', 'history'] = 'summary', limit: int = 32,
                           cursor: str | None = None, max_bytes: int = 16384) -> dict:
    """Return bounded revision-bound views without discarding competing records."""
    integer(limit, 'limit', 1, 128)
    integer(max_bytes, 'max_bytes', 4096, 65536)
    if view not in ('summary', 'annotations', 'history'):
        raise PocketError('Unknown hypothesis query view')
    initial, annotations, chain = _load(hypotheses, store_root)
    identity = digest([hypotheses, view])
    offset = 0
    if cursor is not None:
        if not isinstance(cursor, str) or len(cursor) > 80 or not re.fullmatch(identity + r':[0-9]+', cursor):
            raise PocketError('Stale or malformed hypothesis cursor')
        offset = int(cursor.split(':')[1])
    if view == 'annotations':
        rows = annotations
    elif view == 'history':
        rows = [{'hypotheses': handle, 'revision_index': record['revision_index'],
                 'added_annotations': len(record['annotations'])} for handle, record in chain]
    else:
        evidence = read_record(initial['analysis'], store_root, ANALYSIS_SCHEMA)
        projection_provenance = evidence.get('projection_provenance')
        rows = [{'projection_provenance': projection_provenance,
                 'projection_provenance_status': 'retained' if projection_provenance is not None else 'legacy_not_retained',
                 'source': initial['source'], 'analysis': initial['analysis'], 'model': initial['model'],
                 'annotations': len(annotations), 'revision_index': len(chain) - 1,
                 'interpretation': 'Competing evidence and attributed hypotheses; no automatic musical approval'}]
    if offset > len(rows):
        raise PocketError('Cursor offset exceeds view')
    selected = rows[offset:offset + limit]
    while True:
        next_offset = offset + len(selected)
        result = receipt(artifacts={'hypotheses': hypotheses}, coverage=COVERAGE,
                         view=view, items=selected, total=len(rows), complete=next_offset == len(rows),
                         next_cursor=f'{identity}:{next_offset}' if next_offset < len(rows) else None,
                         omitted=len(rows) - next_offset)
        if len(canonical_bytes(result)) <= max_bytes:
            return result
        if not selected:
            raise PocketError('Response budget cannot hold query metadata')
        selected = selected[:-1]
        if not selected and offset < len(rows):
            return receipt(status='needs_input', artifacts={'hypotheses': hypotheses}, coverage=COVERAGE,
                           view=view, items=[], total=len(rows), complete=False, omitted=len(rows) - offset,
                           next_cursor=f'{identity}:{offset}', next_actions=['Increase max_bytes or read the immutable artifact'])
