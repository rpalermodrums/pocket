"""Public crop analysis composition with separate exact original-frame projection."""
from __future__ import annotations

import copy
import re
from fractions import Fraction
from pathlib import Path
from typing import Literal

from .artifact_store import (
    ArtifactHandle,
    _verify_handles,
    canonical_bytes,
    digest,
    put_record,
    read_bytes,
    read_record,
    receipt,
    run_request,
)
from .audio_hypotheses import _attribution, _fields
from .audio_hypothesis_types import AudioHypothesisAttribution
from .audio_region_types import AudioRegionAnalysis, AudioRegionInput
from .audio_regions import _bounded_handle, audio_region_capture, load_audio_region
from .errors import PocketError
from .material import integer

SCHEMA = 'pocket.audio-region-hypotheses/v1'
COVERAGE = {'provider': 'audio-region-analysis-v1', 'native_execution': False,
            'human_listening': 'not_performed', 'transcription': False,
            'original_bytes_retained': False, 'query_reanalysis': False}


def _coverage(kind):
    if kind == 'learned_notes':
        return {**COVERAGE, 'transcription': 'uncertain_note_hypotheses'}
    return COVERAGE


def _tags(region, analysis):
    if not isinstance(region, dict) or region.get('kind') not in ('inline', 'captured'):
        raise PocketError('Expected inline or captured region input')
    _fields(region, {'kind', 'source' if region['kind'] == 'inline' else 'region'}, 'region input')
    if not isinstance(analysis, dict) or analysis.get('kind') not in ('peek', 'learned_pulse', 'learned_notes'):
        raise PocketError('Expected Peek, learned pulse or learned note region analysis')
    _fields(analysis, {'kind', 'settings'} | ({'model'} if analysis['kind'] != 'peek' else set()), 'region analysis')


def _load(handle, store_root):
    _bounded_handle(handle, store_root, 65536)
    _verify_handles(handle, store_root)
    record = read_record(handle, store_root, SCHEMA)
    _fields(record, {'schema', 'region', 'local_hypotheses', 'analysis_kind', 'coordinate_mapping', 'request_attribution'}, 'region hypotheses')
    region = load_audio_region(record['region'], store_root)
    if canonical_bytes(record['coordinate_mapping']) != canonical_bytes(region['mapping']):
        raise PocketError('Region analysis coordinate mapping differs from capture')
    _attribution(record['request_attribution'])
    if record['analysis_kind'] == 'peek':
        from .audio_hypotheses import _load as load_local
    elif record['analysis_kind'] == 'learned_pulse':
        from .audio_model_corrections import _load as load_local
    elif record['analysis_kind'] == 'learned_notes':
        from .audio_note_corrections import _load as load_local
    else:
        raise PocketError('Unknown region analysis family')
    initial, annotations, chain = load_local(record['local_hypotheses'], store_root)
    frames = region['mapping']['frame_count']
    expected_source = {'sha256': region['crop']['sha256'], 'frames': frames,
                       'sample_rate': region['original']['sample_rate'], 'channels': region['original']['channels'],
                       'format': 'WAV', 'subtype': region['original']['subtype'], 'start_frame': 0,
                       'end_frame_exclusive': frames, 'source_origin': region['original']['source_origin']}
    if canonical_bytes(initial['source']) != canonical_bytes(expected_source):
        raise PocketError('Local analysis does not cover the exact captured crop/source origin')
    if initial['request_attribution'] != record['request_attribution']:
        raise PocketError('Region request attribution differs from local analysis')
    if read_bytes(initial['original'], store_root) != read_bytes(region['crop'], store_root):
        raise PocketError('Local analysis original differs from retained crop bytes')
    return record, region, initial, annotations, chain


def load_audio_region_hypotheses(handle, store_root):
    """Read-only semantic validation, including all local correction ancestors."""
    return _load(handle, store_root)[0]


def _rational(value):
    return {'n': value.numerator, 'd': value.denominator}


def _project(row, region):
    annotation = row['annotation']
    kind = annotation['kind']
    offset = region['mapping']['original_start_frame']
    projected = {'kind': kind, 'original_sha256': region['original']['sha256'], 'coordinate_space': 'original_source_frame'}
    if kind == 'attack':
        projected['source_frame'] = annotation['source_frame'] + offset
    elif kind in ('learned_beat', 'learned_downbeat'):
        value = annotation['source_frame_q']
        projected['source_frame_q'] = _rational(Fraction(value['n'], value['d']) + offset)
    elif kind in ('pulse_candidate', 'phrase_anchor', 'note_hypothesis', 'abstention'):
        projected.update(start_frame=annotation['start_frame'] + offset,
                         end_frame_exclusive=annotation['end_frame_exclusive'] + offset)
        if kind == 'pulse_candidate':
            projected['lattice_origin'] = {'local_estimate_seconds': annotation['source_lattice_origin_seconds'],
                                          'original_offset_seconds_q': _rational(Fraction(offset, region['original']['sample_rate']))}
    else:
        raise PocketError('No original-coordinate projection for retained annotation kind')
    return {'local': copy.deepcopy(row), 'original_projection': projected}


def _analyze(captured, analysis, attribution, request_id, store_root):
    source = {'path': str(Path(store_root).expanduser().resolve() / captured['crop']['artifact_uri']),
              'expected_sha256': captured['crop']['sha256'], 'start_frame': 0,
              'frames': captured['mapping']['frame_count'], 'source_origin': captured['original']['source_origin']}
    kwargs = {'store_root': store_root, 'request_id': 'region-analysis-' + digest([request_id, 'analysis'])[:40],
              'source': source, 'settings': analysis['settings'], 'attribution': attribution}
    if analysis['kind'] == 'peek':
        from .audio_hypotheses import audio_hypotheses
        result = audio_hypotheses(**kwargs)
    elif analysis['kind'] == 'learned_pulse':
        from .audio_pulse_hypotheses import audio_pulse_hypotheses
        result = audio_pulse_hypotheses(**kwargs, model=analysis['model'])
    else:
        from .audio_note_hypotheses import audio_note_hypotheses
        result = audio_note_hypotheses(**kwargs, model=analysis['model'])
    if result['status'] != 'ok':
        raise PocketError('Local region analysis did not complete successfully')
    return result


def audio_region_hypotheses(*, store_root: str, request_id: str, region: AudioRegionInput,
                            analysis: AudioRegionAnalysis, attribution: AudioHypothesisAttribution) -> dict:
    """Analyze an explicit crop using the same standalone public primitives."""
    _tags(region, analysis)
    _attribution(attribution)
    executed = False
    def work():
        nonlocal executed
        executed = True
        if region['kind'] == 'inline':
            capture = audio_region_capture(store_root=store_root,
                request_id='region-capture-' + digest([request_id, 'capture'])[:40], source=region['source'])
            region_handle = capture['artifacts']['region']
        else:
            region_handle = region['region']
        captured = load_audio_region(region_handle, store_root)
        result = _analyze(captured, analysis, attribution, request_id, store_root)
        record = {'schema': SCHEMA, 'region': region_handle, 'local_hypotheses': result['artifacts']['hypotheses'],
                  'analysis_kind': analysis['kind'], 'coordinate_mapping': captured['mapping'],
                  'request_attribution': copy.deepcopy(attribution)}
        handle = put_record(record, store_root)
        load_audio_region_hypotheses(handle, store_root)
        return receipt(request_id, artifacts={'hypotheses': handle}, coverage=_coverage(analysis['kind']),
            change_summary={'original_interval': captured['interval'], 'analysis_kind': analysis['kind']},
            uncertainty=['Original coordinates project local hypotheses; no listening or transcription accuracy is implied.'])
    result = run_request(store_root, request_id, 'audio_region_hypotheses',
                         {'region': region, 'analysis': analysis, 'attribution': attribution}, work)
    retained = load_audio_region_hypotheses(result['artifacts']['hypotheses'], store_root)
    if not executed and region['kind'] == 'inline':
        # Reuse the public capture replay, which rechecks the external full original.
        audio_region_capture(store_root=store_root,
            request_id='region-capture-' + digest([request_id, 'capture'])[:40], source=region['source'])
    if not executed:
        local = _analyze(load_audio_region(retained['region'], store_root), analysis, attribution, request_id, store_root)
        if local['artifacts']['hypotheses'] != retained['local_hypotheses']:
            raise PocketError('Region analysis replay differs from retained local evidence')
    return result


def audio_region_query(*, store_root: str, hypotheses: ArtifactHandle,
                       view: Literal['summary', 'annotations'] = 'summary', limit: int = 32,
                       cursor: str | None = None, max_bytes: int = 16384) -> dict:
    """Bound local evidence and exact original-frame projections, without reanalysis."""
    integer(limit, 'limit', 1, 128)
    integer(max_bytes, 'max_bytes', 4096, 65536)
    if view not in ('summary', 'annotations'):
        raise PocketError('Unknown region query view')
    record, region, initial, annotations, chain = _load(hypotheses, store_root)
    if view == 'annotations':
        rows = [_project(row, region) for row in annotations]
    else:
        rows = [{'original': region['original'], 'interval': region['interval'],
                 'coordinate_mapping': region['mapping'], 'local_hypotheses': record['local_hypotheses'],
                 'analysis_kind': record['analysis_kind'], 'annotations': len(annotations),
                 'local_revision_index': len(chain)-1, 'analysis': initial['analysis'],
                 'model': initial.get('model')}]
    identity = digest([hypotheses, view])
    offset = 0
    if cursor is not None:
        if not isinstance(cursor, str) or len(cursor) > 80 or not re.fullmatch(identity + r':[0-9]+', cursor):
            raise PocketError('Stale or malformed region query cursor')
        offset = int(cursor.split(':')[1])
    if offset > len(rows):
        raise PocketError('Region query cursor exceeds view')
    selected = rows[offset:offset+limit]
    while True:
        following = offset + len(selected)
        result = receipt(artifacts={'hypotheses': hypotheses}, coverage=_coverage(record['analysis_kind']), view=view,
            items=selected, total=len(rows), complete=following == len(rows), omitted=len(rows)-following,
            next_cursor=f'{identity}:{following}' if following < len(rows) else None)
        if len(canonical_bytes(result)) <= max_bytes:
            return result
        if not selected:
            raise PocketError('Response budget cannot hold region query metadata')
        selected = selected[:-1]
        if not selected and offset < len(rows):
            return receipt(status='needs_input', artifacts={'hypotheses': hypotheses}, coverage=_coverage(record['analysis_kind']),
                view=view, items=[], total=len(rows), complete=False, omitted=len(rows)-offset,
                next_cursor=f'{identity}:{offset}', next_actions=['Increase max_bytes or inspect immutable evidence'])
