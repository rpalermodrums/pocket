"""Independent correction-chain adversaries over synthetic learned evidence."""
import copy
import hashlib
import struct
from pathlib import Path
from unittest.mock import patch

import pytest
from test_audio_models_qa import fake_runner, setup  # noqa: F401

import pocket_music.audio_pulse_hypotheses as pulse
from pocket_music.artifact_store import canonical_bytes, digest, put_record, read_record
from pocket_music.audio_hypotheses import audio_hypothesis_correct, audio_hypothesis_query
from pocket_music.errors import PocketError


@pytest.fixture
def base(setup):  # noqa: F811 - imported independent fixture
    args, _ = setup
    def runner(*a, **kw):
        result, binding = fake_runner(*a, **kw)
        if result['analysis']:
            raw = result['analysis']; raw['beat'][1:3] = [1., 1.]
            raw['vendor_beats_seconds'] = [.03]
            h = hashlib.sha256(struct.pack('<101f', *raw['beat'])).hexdigest()
            raw['repeat_sha256']['beat'] = [h, h]
        return result, binding
    with patch.object(pulse, '_runner', side_effect=runner):
        parent = pulse.audio_pulse_hypotheses(**args)['artifacts']['hypotheses']
    with patch.object(pulse, '_runner', side_effect=AssertionError('Must not run optional model')):
        yield args, parent


def item(key='authored'):
    return {'correction_id': key, 'supersedes': [],
            'annotation': {'kind': 'attack', 'source_frame': 941, 'strength_relative': None},
            'support': [{'kind': 'analysis_pointer', 'reference': '/analysis/raw/beat/1'}],
            'uncertainty': ['Alternative interpretation only']}


def append(args, parent, rows=None, request='correct'):
    return audio_hypothesis_correct(store_root=args['store_root'], request_id=request,
        parent=parent, expected_revision=parent['sha256'], corrections=rows or [item()], attribution=args['attribution'])['artifacts']['hypotheses']


def query(args, handle, **kw):
    return audio_hypothesis_query(store_root=args['store_root'], hypotheses=handle, **kw)


def test_source_replacement_keeps_original_proof_and_learned_attribution(base):
    args, parent = base
    original = read_record(parent, args['store_root'])
    Path(args['source']['path']).write_bytes(b'new external contents')
    row = item(); row['supersedes'] = [original['annotations'][0]['annotation_id']]
    child = append(args, parent, [row])
    rows = query(args, child, view='annotations')['items']
    assert rows[0] == original['annotations'][0]
    assert rows[0]['attribution']['actor_kind'] == 'algorithm'
    assert rows[0]['annotation']['source_frame_q'] == {'n': 941, 'd': 1}
    assert rows[1]['attribution'] == args['attribution']
    assert read_record(parent, args['store_root']) == original
    assert append(args, parent, [row]) == child


@pytest.mark.parametrize('change', ['negative', 'endpoint', 'fraction', 'unknown_ref', 'out_of_array', 'same_batch', 'duplicate'])
def test_invalid_frame_and_ambiguous_reference_refuse_atomically(base, change):
    args, parent = base
    row = item(); rows = [row]
    if change == 'negative': row['annotation']['source_frame'] = 700
    if change == 'endpoint': row['annotation']['source_frame'] = 16701
    if change == 'fraction': row['annotation']['source_frame'] = {'n': 1883, 'd': 2}
    if change == 'unknown_ref': row['support'] = [{'kind': 'annotation_id', 'reference': 'unknown'}]
    if change == 'out_of_array': row['support'][0]['reference'] = '/analysis/raw/beat/101'
    if change == 'duplicate': rows.append(copy.deepcopy(row))
    if change == 'same_batch':
        first = {**copy.deepcopy(row), 'attribution': args['attribution']}
        first_id = 'audio:' + digest([parent, first])
        second = item('second'); second['support'] = [{'kind': 'annotation_id', 'reference': first_id}]
        rows.append(second)
    before = set(Path(args['store_root']).glob('artifacts/*/record.json'))
    with pytest.raises(PocketError): append(args, parent, rows)
    assert set(Path(args['store_root']).glob('artifacts/*/record.json')) == before


@pytest.mark.parametrize('change', ['algorithm_actor', 'self_support', 'wrong_count', 'wrong_parent', 'duplicate_ids'])
def test_resealed_chain_recomputes_all_semantics(base, change):
    args, parent = base
    child = append(args, parent)
    record = read_record(child, args['store_root']); row = record['annotations'][0]
    if change == 'algorithm_actor': row['attribution']['actor_kind'] = 'algorithm'
    if change == 'self_support': row['support'] = [{'kind': 'annotation_id', 'reference': row['annotation_id']}]
    if change == 'wrong_count': record['annotation_count'] = 999
    if change == 'wrong_parent': record['parent'] = child
    if change == 'duplicate_ids': record['annotations'].append(copy.deepcopy(row)); record['annotation_count'] += 1
    body = {k: v for k, v in row.items() if k != 'annotation_id'}
    row['annotation_id'] = 'audio:' + digest([record['parent'], body])
    forged = put_record(record, args['store_root'])
    with pytest.raises(PocketError): query(args, forged, view='annotations')


def test_pages_bind_revision_and_view_and_preserve_all_alternatives(base):
    args, parent = base
    child = append(args, parent, [item(str(i)) for i in range(12)])
    cursor = None; rows = []
    while True:
        page = query(args, child, view='annotations', limit=3, cursor=cursor, max_bytes=4096)
        assert len(canonical_bytes(page)) <= 4096
        rows += page['items']; cursor = page['next_cursor']
        if page['complete']: break
    assert len(rows) == 13 and len({r['annotation_id'] for r in rows}) == 13
    cursor = query(args, child, view='annotations', limit=1)['next_cursor']
    for handle, view in [(parent, 'annotations'), (child, 'history')]:
        with pytest.raises(PocketError): query(args, handle, view=view, cursor=cursor)


def test_full_half_open_span_and_batch_bound(base):
    args, parent = base
    row = item(); row['annotation'] = {'kind': 'phrase_anchor', 'start_frame': 701,
                                      'end_frame_exclusive': 16701, 'label': 'Authored span'}
    child = append(args, parent, [row])
    assert query(args, child, view='annotations')['items'][-1]['annotation'] == row['annotation']
    with pytest.raises(PocketError): append(args, parent, [item(str(i)) for i in range(129)], 'too-many')


def test_query_without_optional_runtime_and_missing_external_source(base, monkeypatch):
    import builtins
    args, parent = base
    child = append(args, parent)
    Path(args['source']['path']).unlink()
    real_import = builtins.__import__
    def guarded(name, *a, **kw):
        if name.split('.')[0] in {'torch', 'beat_this', 'torchaudio', 'soxr'}:
            raise AssertionError('Optional runtime import from retained query')
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, '__import__', guarded)
    assert query(args, child)['items'][0]['annotations'] == 2
    next_child = append(args, child, [item('second')], 'second')
    assert query(args, next_child, view='history')['total'] == 3


def test_transitive_raw_corruption_refuses_every_revision(base):
    args, parent = base
    child = append(args, parent)
    record = read_record(parent, args['store_root'])
    proof = read_record(record['analysis'], args['store_root'])
    (Path(args['store_root']) / proof['arrays']['beat']['artifact_uri']).write_bytes(b'corrupted retained raw array')
    for handle in (parent, child):
        with pytest.raises(PocketError): query(args, handle)
