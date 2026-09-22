"""Compose the public correction primitive with explicit crop coordinate translation."""
from __future__ import annotations

import copy
from fractions import Fraction

from .artifact_store import ArtifactHandle, digest, put_record, receipt, run_request
from .audio_hypotheses import _annotation, _attribution, _fields, audio_hypothesis_correct
from .audio_hypothesis_types import AudioHypothesisAttribution
from .audio_region_analysis import _load, load_audio_region_hypotheses
from .audio_region_correction_types import AudioRegionCorrectionBatch
from .errors import PocketError
from .material import integer

COVERAGE = {'provider': 'audio-region-corrections-v1', 'execution': 'retained_evidence_only',
            'native_execution': False, 'model_execution': False, 'query_reanalysis': False,
            'human_listening': 'not_performed', 'original_bytes_retained': False}


def _local_batch(batch, region, initial):
    _fields(batch, {'coordinate_space', 'corrections'}, 'region correction batch')
    if batch['coordinate_space'] not in ('local_crop_frame', 'original_source_frame'):
        raise PocketError('Unknown region correction coordinate space')
    corrections = batch['corrections']
    if not isinstance(corrections, list) or not 1 <= len(corrections) <= 128:
        raise PocketError('Region corrections require 1–128 entries')
    offset = region['mapping']['original_start_frame']
    frame_count = region['mapping']['frame_count']
    expected = Fraction(offset, region['original']['sample_rate'])
    local = copy.deepcopy(corrections)
    for item in local:
        _fields(item, {'correction_id', 'supersedes', 'annotation', 'support', 'uncertainty'}, 'region correction')
        annotation = item['annotation']
        if not isinstance(annotation, dict):
            raise PocketError('Correction annotation must be an object')
        if batch['coordinate_space'] == 'original_source_frame':
            kind = annotation.get('kind')
            if kind == 'attack':
                frame = integer(annotation.get('source_frame'), 'original attack frame', offset, offset + frame_count - 1)
                annotation['source_frame'] = frame - offset
            elif kind in ('phrase_anchor', 'note_hypothesis', 'pulse_candidate'):
                start = integer(annotation.get('start_frame'), 'original interval start', offset, offset + frame_count - 1)
                end = integer(annotation.get('end_frame_exclusive'), 'original interval end', start + 1, offset + frame_count)
                annotation['start_frame'], annotation['end_frame_exclusive'] = start - offset, end - offset
                if kind == 'pulse_candidate':
                    _fields(annotation, {'kind', 'start_frame', 'end_frame_exclusive', 'bpm', 'lattice_origin'}, 'original pulse')
                    origin = annotation.pop('lattice_origin')
                    _fields(origin, {'local_estimate_seconds', 'original_offset_seconds_q'}, 'original pulse lattice')
                    rational = origin['original_offset_seconds_q']
                    _fields(rational, {'n', 'd'}, 'original lattice offset')
                    integer(rational['n'], 'offset numerator', 0, 2**63 - 1)
                    integer(rational['d'], 'offset denominator', 1, 2**63 - 1)
                    if rational != {'n': expected.numerator, 'd': expected.denominator}:
                        raise PocketError('Pulse offset must be the canonical exact captured source offset')
                    annotation['source_lattice_origin_seconds'] = origin['local_estimate_seconds']
            else:
                raise PocketError('Unsupported original-coordinate authored annotation')
        _annotation(annotation, initial['source'])
    return local


def audio_region_correct(*, store_root: str, request_id: str, parent: ArtifactHandle,
                         expected_revision: str, batch: AudioRegionCorrectionBatch,
                         attribution: AudioHypothesisAttribution) -> dict:
    """Correct retained crop evidence in declared local or original-source coordinates.

    Support pointers and supersession IDs always address the underlying LOCAL
    evidence/history, including for an original-source-coordinate batch. Pulse
    lattice origins keep a local floating estimate plus the exact rational crop
    offset; this wrapper never adds or subtracts floating times. Original learned
    fractional rows remain evidence; authored correction forms are unchanged.
    No model, Peek, source-file acquisition, native execution or listening occurs.
    """
    # A stale wrapper revision must fail before even a request journal is written.
    record, region, initial, _, _ = _load(parent, store_root)
    if not isinstance(expected_revision, str) or expected_revision != parent['sha256']:
        raise PocketError('Stale region hypothesis revision')
    _attribution(attribution)
    local = _local_batch(batch, region, initial)

    def work():
        result = audio_hypothesis_correct(store_root=store_root,
            request_id='region-correction-' + digest([request_id, 'correction'])[:40],
            parent=record['local_hypotheses'], expected_revision=record['local_hypotheses']['sha256'],
            corrections=local, attribution=attribution)
        if result['status'] != 'ok':
            raise PocketError('Local correction did not complete successfully')
        child = {**record, 'local_hypotheses': result['artifacts']['hypotheses']}
        handle = put_record(child, store_root)
        load_audio_region_hypotheses(handle, store_root)
        return receipt(request_id,
            artifacts={'hypotheses': handle, 'local_hypotheses': result['artifacts']['hypotheses']},
            coverage=COVERAGE,
            change_summary={'added_annotations': len(local), 'coordinate_space': batch['coordinate_space'],
                            'original_interval': region['interval']},
            uncertainty=['Authored corrections remain attributed alternatives; local pulse estimates are not exact global times.'])

    result = run_request(store_root, request_id, 'audio_region_correct',
                         {'parent': parent, 'expected_revision': expected_revision, 'batch': batch,
                          'attribution': attribution}, work)
    retained = load_audio_region_hypotheses(result['artifacts']['hypotheses'], store_root)
    if retained['local_hypotheses'] != result['artifacts']['local_hypotheses']:
        raise PocketError('Region correction receipt differs from local child')
    return result
