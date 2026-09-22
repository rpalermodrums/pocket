"""Immutable local attack/pulse evidence with explicit attributed alternatives.

This is bounded synchronous analysis, not transcription, listening or a job
scheduler. Workers may compose these public primitives in an isolated store.
"""
from __future__ import annotations

import copy
import hashlib
import math
import re
from pathlib import Path
from typing import Literal

from .artifact_store import (
    ArtifactHandle,
    _verify_handles,
    canonical_bytes,
    digest,
    put_bytes,
    put_record,
    read_bytes,
    read_record,
    receipt,
    run_request,
)
from .assets import identify_audio
from .audio_hypothesis_types import (
    AudioHypothesisAttribution,
    AudioHypothesisCorrection,
    AudioHypothesisSettings,
    AudioHypothesisSource,
)
from .errors import PocketError
from .material import integer
from .peek import analyze_region

SCHEMA = 'pocket.audio-hypotheses/v1'
ANALYSIS_SCHEMA = 'pocket.portable-audio-analysis/v1'
COVERAGE = {'provider': 'audio-hypotheses-v1', 'execution': 'bounded_synchronous',
            'background_job': False, 'native_execution': False, 'learned_models': [],
            'transcription': False, 'human_listening': 'not_performed'}


def _fields(value, names, label):
    if not isinstance(value, dict) or set(value) != set(names):
        raise PocketError(f'{label} has missing or unexpected fields')


def _text(value, label, maximum=256):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise PocketError(f'{label} requires nonblank text within {maximum} characters')
    return value


def _number(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high or not math.isfinite(value):
        raise PocketError(f'{label} requires a finite number in [{low}, {high}]')
    return value


def _strings(value, label, maximum=32):
    if not isinstance(value, list) or len(value) > maximum:
        raise PocketError(f'{label} requires at most {maximum} strings')
    for item in value:
        _text(item, label, 1024)
    return value


def _attribution(value):
    _fields(value, {'actor', 'actor_kind', 'statement', 'uncertainty'}, 'attribution')
    _text(value['actor'], 'actor')
    if value['actor_kind'] not in ('human', 'agent'):
        raise PocketError('Unknown actor kind')
    _text(value['statement'], 'statement', 4096)
    _strings(value['uncertainty'], 'attribution uncertainty')


def _stamp(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _source(source, settings):
    _fields(source, {'path', 'expected_sha256', 'start_frame', 'frames', 'source_origin'}, 'audio source')
    _text(source['path'], 'source path', 4096)
    if not isinstance(source['expected_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', source['expected_sha256']):
        raise PocketError('Expected lowercase SHA256 source identity')
    integer(source['start_frame'], 'start_frame', 0, 2**53)
    integer(source['frames'], 'frames', 1, 2000000)
    if source['source_origin'] not in ('independently_acquired', 'user_recording'):
        raise PocketError('Unknown source origin')
    _fields(settings, {'bpm_hint', 'beats_per_bar'}, 'audio settings')
    if settings['bpm_hint'] is not None:
        _number(settings['bpm_hint'], 'bpm_hint', 20, 400)
    integer(settings['beats_per_bar'], 'beats_per_bar', 2, 12)


def _annotation(value, source):
    if not isinstance(value, dict):
        raise PocketError('Annotation must be a tagged record')
    kind = value.get('kind')
    fields = {'attack': {'kind', 'source_frame', 'strength_relative'},
              'pulse_candidate': {'kind', 'start_frame', 'end_frame_exclusive', 'bpm', 'source_lattice_origin_seconds'},
              'phrase_anchor': {'kind', 'start_frame', 'end_frame_exclusive', 'label'},
              'note_hypothesis': {'kind', 'start_frame', 'end_frame_exclusive', 'midi_note', 'cents', 'tuning_ref'}}
    if not isinstance(kind, str) or kind not in fields:
        raise PocketError('Unknown correction annotation kind')
    _fields(value, fields[kind], 'annotation')
    low, high = source['start_frame'], source['end_frame_exclusive']
    if kind == 'attack':
        integer(value['source_frame'], 'attack frame', low, high - 1)
        if value['strength_relative'] is not None:
            _number(value['strength_relative'], 'strength', 0, 2**53)
    else:
        start = integer(value['start_frame'], 'annotation start', low, high - 1)
        integer(value['end_frame_exclusive'], 'annotation end', start + 1, high)
    if kind == 'pulse_candidate':
        _number(value['bpm'], 'pulse bpm', 20, 400)
        _number(value['source_lattice_origin_seconds'], 'pulse origin seconds',
                low / source['sample_rate'], high / source['sample_rate'])
    elif kind == 'phrase_anchor':
        _text(value['label'], 'phrase label')
    elif kind == 'note_hypothesis':
        integer(value['midi_note'], 'midi_note', 0, 127)
        _number(value['cents'], 'cents', -12000, 12000)
        _text(value['tuning_ref'], 'tuning_ref')


def _pointer(record, pointer):
    if not isinstance(pointer, str) or not pointer.startswith('/') or len(pointer) > 2048:
        raise PocketError('Support requires an absolute bounded JSON pointer')
    current = record
    try:
        for raw in pointer[1:].split('/'):
            if re.search('~(?![01])', raw):
                raise KeyError(raw)
            key = raw.replace('~1', '/').replace('~0', '~')
            if isinstance(current, list):
                if not re.fullmatch('0|[1-9][0-9]*', key):
                    raise KeyError(key)
                current = current[int(key)]
            elif isinstance(current, dict):
                current = current[key]
            else:
                raise KeyError(key)
    except (KeyError, IndexError, ValueError) as error:
        raise PocketError('Support pointer does not resolve to retained analysis evidence') from error
    return current


def _load(handle, store_root):
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
    initial = chain[0][1]
    _fields(initial, {'schema', 'source', 'original', 'analysis', 'annotations', 'parent',
                     'revision_index', 'annotation_count', 'request_attribution', 'settings', 'limitations'},
            'initial hypothesis record')
    source = initial['source']
    _fields(source, {'sha256', 'frames', 'sample_rate', 'channels', 'format', 'subtype',
                     'start_frame', 'end_frame_exclusive', 'source_origin'}, 'retained source')
    integer(source['frames'], 'retained frames', 1, 2**53)
    integer(source['sample_rate'], 'retained sample rate', 1, 2000000)
    integer(source['channels'], 'retained channels', 1, 8)
    integer(source['start_frame'], 'retained start', 0, source['frames'] - 1)
    integer(source['end_frame_exclusive'], 'retained end', source['start_frame'] + 1, source['frames'])
    _source({'path': 'retained.audio', 'expected_sha256': source['sha256'],
             'start_frame': source['start_frame'], 'frames': source['end_frame_exclusive'] - source['start_frame'],
             'source_origin': source['source_origin']}, initial['settings'])
    if source['end_frame_exclusive'] - source['start_frame'] > 20 * source['sample_rate']:
        raise PocketError('Retained crop exceeds 20 seconds')
    _attribution(initial['request_attribution'])
    evidence = read_record(initial['analysis'], store_root, ANALYSIS_SCHEMA)
    _fields(evidence, {'schema', 'analysis', 'projection', 'original'}, 'portable analysis')
    if not isinstance(evidence['analysis'], dict):
        raise PocketError('Portable analysis requires a record')
    if evidence.get('original') != initial['original'] or initial['original']['sha256'] != source['sha256']:
        raise PocketError('Retained audio identity does not match analysis')
    asset = evidence['analysis'].get('asset', {})
    if not isinstance(asset, dict) or initial['original']['artifact_schema'] != 'pocket.audio-source-bytes/v1':
        raise PocketError('Invalid retained original audio metadata')
    region = evidence['analysis'].get('region', {})
    if not isinstance(region, dict) or any(region.get(key) != source[key] for key in ('start_frame', 'end_frame_exclusive')):
        raise PocketError('Retained analysis crop metadata mismatch')
    if 'local_path' in asset or 'filename' in asset:
        raise PocketError('Retained portable analysis contains environment paths')
    if any(asset.get(key) != source[key] for key in ('sha256', 'frames', 'sample_rate', 'channels', 'format', 'subtype')):
        raise PocketError('Retained analysis source metadata mismatch')
    if initial['annotations'] != _automatic_annotations(initial['analysis'], evidence['analysis'], source):
        raise PocketError('Automatic annotations do not exactly project retained detector evidence')
    annotations, ids, correction_ids = [], set(), set()
    for index, (handle, record) in enumerate(chain):
        if index:
            _fields(record, {'schema', 'parent', 'annotations', 'revision_index', 'annotation_count'}, 'correction record')
        if record['revision_index'] != index or isinstance(record['revision_index'], bool):
            raise PocketError('Hypothesis revision index mismatch')
        rows = record['annotations']
        maximum = 128 if index else 1024
        if not isinstance(rows, list) or not 1 <= len(rows) <= maximum:
            raise PocketError('Invalid retained annotation count')
        prior_ids = ids.copy()
        for row in rows:
            keys = {'annotation', 'support', 'uncertainty', 'attribution', 'supersedes', 'annotation_id'}
            _fields(row, keys | ({'correction_id'} if index else set()), 'retained annotation')
            body = {key: value for key, value in row.items() if key != 'annotation_id'}
            basis = record['parent'] if index else initial['analysis']
            if row['annotation_id'] != 'audio:' + digest([basis, body]) or row['annotation_id'] in ids:
                raise PocketError('Retained annotation identity mismatch')
            ids.add(row['annotation_id'])
            _strings(row['uncertainty'], 'retained uncertainty')
            _strings(row['supersedes'], 'retained supersedes')
            if len(set(row['supersedes'])) != len(row['supersedes']) or any(key not in prior_ids for key in row['supersedes']):
                raise PocketError('Retained supersession identity mismatch')
            if index:
                _attribution(row['attribution'])
                key = _text(row['correction_id'], 'retained correction identity')
                if key in correction_ids:
                    raise PocketError('Duplicate retained correction identity')
                correction_ids.add(key)
                _annotation(row['annotation'], source)
            else:
                if row['attribution'] != {'actor': 'Peek', 'actor_kind': 'algorithm',
                                          'analysis_version': evidence['analysis']['provenance']['analysis_version']}:
                    raise PocketError('Retained detector attribution mismatch')
                value = row['annotation']
                if not isinstance(value, dict):
                    raise PocketError('Retained annotation must be a tagged record')
                if value.get('kind') == 'abstention':
                    _fields(value, {'kind', 'start_frame', 'end_frame_exclusive', 'reason'}, 'abstention')
                    if value['start_frame'] != source['start_frame'] or value['end_frame_exclusive'] != source['end_frame_exclusive']:
                        raise PocketError('Abstention span mismatch')
                    _text(value['reason'], 'abstention reason', 1024)
                elif value.get('kind') in ('attack', 'pulse_candidate'):
                    _annotation(value, source)
                else:
                    raise PocketError('Unknown automatic detector annotation')
            if not isinstance(row['support'], list) or not 1 <= len(row['support']) <= 32:
                raise PocketError('Invalid retained support')
            for ref in row['support']:
                _fields(ref, {'kind', 'reference'}, 'retained support')
                if ref['kind'] == 'analysis_pointer' and isinstance(ref['reference'], str) and ref['reference'].startswith('/analysis/'):
                    _pointer(evidence, ref['reference'])
                elif ref['kind'] != 'annotation_id' or not isinstance(ref['reference'], str) or ref['reference'] not in prior_ids:
                    raise PocketError('Retained support does not resolve')
        annotations.extend(rows)
        if record['annotation_count'] != len(annotations) or isinstance(record['annotation_count'], bool):
            raise PocketError('Retained cumulative annotation count mismatch')
    if len(annotations) > 4096:
        raise PocketError('Hypothesis annotation history exceeds 4096')
    return initial, annotations, chain


def prepare_hypothesis_input(*, source: AudioHypothesisSource, settings: AudioHypothesisSettings,
                             attribution: AudioHypothesisAttribution, store_root: str) -> dict:
    """Shared bounded independent source snapshot; does not run analysis."""
    _source(source, settings)
    _attribution(attribution)
    path = Path(source['path']).expanduser().resolve()
    try:
        before = _stamp(path)
        if before[2] > 256 * 1024 * 1024:
            raise PocketError('Audio source exceeds 256 MiB')
        asset = identify_audio(path)
        with path.open('rb') as stream:
            payload = stream.read(256 * 1024 * 1024 + 1)
        if _stamp(path) != before or len(payload) > 256 * 1024 * 1024:
            raise PocketError('Audio source changed while copying')
    except OSError as error:
        raise PocketError('Cannot copy audio source') from error
    if hashlib.sha256(payload).hexdigest() != source['expected_sha256'] or asset['sha256'] != source['expected_sha256']:
        raise PocketError('Audio source SHA256 mismatch')
    if (source['frames'] > 20 * asset['sample_rate'] or
            source['start_frame'] + source['frames'] > asset['frames'] or not 1 <= asset['channels'] <= 8):
        raise PocketError('Audio crop exceeds source, 20 seconds or 1–8 channel bounds')
    original = put_bytes(payload, store_root, 'source.audio', 'pocket.audio-source-bytes/v1')
    copied = Path(store_root).expanduser().resolve() / original['artifact_uri']
    metadata = {key: asset[key] for key in ('sha256', 'frames', 'sample_rate', 'channels', 'format', 'subtype')}
    metadata.update({'start_frame': source['start_frame'],
                     'end_frame_exclusive': source['start_frame'] + source['frames'],
                     'source_origin': source['source_origin']})
    return {'source': {**copy.deepcopy(source), 'path': str(copied)},
            'settings': copy.deepcopy(settings), 'attribution': copy.deepcopy(attribution),
            'source_handle': original, 'source_metadata': metadata}


def _automatic_annotations(evidence, portable, source_record):
    try:
        return _project_annotations(evidence, portable, source_record)
    except (KeyError, TypeError, IndexError, AttributeError, OverflowError, ValueError) as error:
        raise PocketError('Malformed retained detector evidence') from error


def _project_annotations(evidence, portable, source_record):
    annotations = []

    def append(value, pointer, uncertainty):
        row = {'annotation': value, 'support': [{'kind': 'analysis_pointer', 'reference': pointer}],
               'uncertainty': uncertainty, 'attribution': {'actor': 'Peek', 'actor_kind': 'algorithm',
               'analysis_version': portable['provenance']['analysis_version']}, 'supersedes': []}
        row['annotation_id'] = 'audio:' + digest([evidence, row])
        annotations.append(row)
    for index, event in enumerate(portable['onsets']['events']):
        append({'kind': 'attack', 'source_frame': event['source_frame'], 'strength_relative': event['strength_relative']},
               f'/analysis/onsets/events/{index}', ['Detected attack coordinate has finite timing resolution; instrument identity unknown.'])
    for index, candidate in enumerate(portable['rhythm']['tempo_candidates']):
        append({'kind': 'pulse_candidate', 'start_frame': source_record['start_frame'],
                'end_frame_exclusive': source_record['end_frame_exclusive'], 'bpm': candidate['bpm'],
                'source_lattice_origin_seconds': candidate['source_lattice_origin_seconds']},
               f'/analysis/rhythm/tempo_candidates/{index}', ['Pulse, phase and counting remain hypotheses; no declared musical clock.'])
    if not annotations or portable['rhythm']['status'] == 'insufficient_evidence':
        append({'kind': 'abstention', 'start_frame': source_record['start_frame'],
                'end_frame_exclusive': source_record['end_frame_exclusive'],
                'reason': 'Insufficient pulse evidence; no note transcription attempted.'},
               '/analysis/rhythm/status', ['No missing musical fact is supplied by this analysis.'])
    if len(annotations) > 1024:
        raise PocketError(f'Automatic annotations exceed 1024; retained analysis {evidence["artifact_uri"]}')
    return annotations


def audio_hypotheses(*, store_root: str, request_id: str, source: AudioHypothesisSource,
                     settings: AudioHypothesisSettings, attribution: AudioHypothesisAttribution) -> dict:
    """Retain exact source bytes and competing local Peek evidence; no note transcription."""
    _source(source, settings)
    _attribution(attribution)

    def work():
        prepared = prepare_hypothesis_input(source=source, settings=settings, attribution=attribution, store_root=store_root)
        original = prepared['source_handle']
        copied = prepared['source']['path']
        analysis = analyze_region(copied, start_frame=source['start_frame'], frames=source['frames'], **settings)
        read_bytes(original, store_root)
        portable = copy.deepcopy(analysis)
        portable['asset'].pop('local_path')
        portable['asset'].pop('filename')
        evidence = put_record({'schema': ANALYSIS_SCHEMA, 'analysis': portable,
                               'projection': {'omitted_fields': ['/asset/local_path', '/asset/filename'],
                                              'reason': 'Environment and basename are not audio identity'},
                               'original': original}, store_root)
        source_record = prepared['source_metadata']
        annotations = _automatic_annotations(evidence, portable, source_record)
        record = {'schema': SCHEMA, 'source': source_record, 'original': original, 'analysis': evidence,
                  'annotations': annotations, 'parent': None, 'revision_index': 0,
                  'annotation_count': len(annotations), 'request_attribution': copy.deepcopy(attribution),
                  'settings': copy.deepcopy(settings), 'limitations': portable['limitations']}
        handle = put_record(record, store_root)
        return receipt(request_id, artifacts={'hypotheses': handle},
                       change_summary={'annotations': len(annotations), 'revision_index': 0},
                       coverage=COVERAGE, uncertainty=['Audio-derived hypotheses require attributed correction and listening.'])
    return run_request(store_root, request_id, 'audio_hypotheses',
                       {'source': source, 'settings': settings, 'attribution': attribution}, work)


def audio_hypothesis_correct(*, store_root: str, request_id: str, parent: ArtifactHandle,
                             expected_revision: str, corrections: list[AudioHypothesisCorrection],
                             attribution: AudioHypothesisAttribution) -> dict:
    """Append explicit attributed alternatives; retain all superseded evidence."""
    if isinstance(parent, dict) and parent.get('artifact_schema') == 'pocket.audio-note-hypotheses/v1':
        from .audio_note_corrections import correct
        return correct(store_root=store_root, request_id=request_id, parent=parent,
                       expected_revision=expected_revision, corrections=corrections, attribution=attribution)
    if isinstance(parent, dict) and parent.get('artifact_schema') == 'pocket.audio-model-hypotheses/v1':
        from .audio_model_corrections import correct
        return correct(store_root=store_root, request_id=request_id, parent=parent,
                       expected_revision=expected_revision, corrections=corrections, attribution=attribution)
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
    return run_request(store_root, request_id, 'audio_hypothesis_correct',
                       {'parent': parent, 'expected_revision': expected_revision, 'corrections': corrections,
                        'attribution': attribution}, work)


def audio_hypothesis_query(*, store_root: str, hypotheses: ArtifactHandle,
                           view: Literal['summary', 'annotations', 'history'] = 'summary', limit: int = 32,
                           cursor: str | None = None, max_bytes: int = 16384) -> dict:
    """Return bounded revision-bound views without discarding competing records."""
    if isinstance(hypotheses, dict) and hypotheses.get('artifact_schema') == 'pocket.audio-note-hypotheses/v1':
        from .audio_note_corrections import query
        return query(store_root=store_root, hypotheses=hypotheses, view=view, limit=limit,
                     cursor=cursor, max_bytes=max_bytes)
    if isinstance(hypotheses, dict) and hypotheses.get('artifact_schema') == 'pocket.audio-model-hypotheses/v1':
        from .audio_model_corrections import query
        return query(store_root=store_root, hypotheses=hypotheses, view=view, limit=limit,
                     cursor=cursor, max_bytes=max_bytes)
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
        rows = [{'source': initial['source'], 'analysis': initial['analysis'],
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
