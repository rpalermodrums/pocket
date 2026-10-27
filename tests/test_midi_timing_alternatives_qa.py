# SPDX-License-Identifier: AGPL-3.0-only
"""Independent exact clock bridge and retained authored-evidence adversaries."""
import copy
import hashlib
import shutil
from fractions import Fraction
from pathlib import Path

import pytest
from test_audio_models_qa import setup  # noqa: F401
from test_midi_qa import seal_literal
from test_midi_relationships_qa import material, note, q
from test_time_maps import definition

import pocket_music.midi_timing_alternatives as timing
from pocket_music.artifact_store import canonical_bytes, put_record, read_record
from pocket_music.audio_hypotheses import audio_hypothesis_correct
from pocket_music.audio_region_analysis import audio_region_hypotheses
from pocket_music.errors import PocketError
from pocket_music.material import material_query
from pocket_music.midi_edit import midi_transform
from pocket_music.time_maps import musical_time


@pytest.fixture
def context(setup):  # noqa: F811
    base, _ = setup
    store = base['store_root']
    result = audio_region_hypotheses(store_root=store, request_id='region',
        region={'kind': 'inline', 'source': base['source']},
        analysis={'kind': 'peek', 'settings': {'bpm_hint': None, 'beats_per_bar': 4}},
        attribution=base['attribution'])
    wrapper = read_record(result['artifacts']['hypotheses'], store)
    parent = wrapper['local_hypotheses']
    support = read_record(parent, store)['annotations'][0]['annotation_id']
    corrections = [{'correction_id': key, 'supersedes': [], 'annotation': row,
        'support': [{'kind': 'annotation_id', 'reference': support}], 'uncertainty': ['Synthetic only']}
        for key, row in [('attack', {'kind': 'attack', 'source_frame': 8000, 'strength_relative': None}),
            ('pulse', {'kind': 'pulse_candidate', 'start_frame': 0, 'end_frame_exclusive': 16000,
                       'bpm': 120, 'source_lattice_origin_seconds': .1})]]
    child = audio_hypothesis_correct(store_root=store, request_id='correct', parent=parent,
        expected_revision=parent['sha256'], corrections=corrections, attribution=base['attribution'])['artifacts']['hypotheses']
    rows = read_record(child, store)['annotations']
    wrapper['local_hypotheses'] = child
    hypotheses = put_record(wrapper, store)
    data = material([note('a', 0, Fraction(1, 4)), note('outside', 6, Fraction(1, 4), 73)])
    data['clips'][0]['origin'] = {'space': 'arrangement_qn', **q(0)}
    data = seal_literal(data)
    clock = musical_time('create', store, request_id='clock', definition=definition())['artifacts']['time_map']
    args = {'store_root': store, 'request_id': 'timing', 'material': data,
        'selection': material_query(data, store, selection={'note_ids': ['a']})['selection'],
        'hypotheses': hypotheses, 'expected_hypotheses_revision': hypotheses['sha256'],
        'alignment': {'kind': 'declared_unwarped_source_clock', 'original_sha256': base['source']['expected_sha256'],
            'sample_rate': 8000, 'source_anchor_frame_q': q(701), 'host_anchor_seconds_q': q(10),
            'time_map': clock, 'clip_id': data['clips'][0]['id']},
        'alternatives': [{'alternative_id': 'attack', 'statement': 'Explicit test', 'uncertainty': [],
            'strength': q(1), 'maximum_shift_qn': q(4), 'matches': [{'note_id': 'a',
                'annotation_id': rows[0]['annotation_id'], 'point': {'kind': 'annotation_attack'}}]},
            {'alternative_id': 'pulse', 'statement': 'Different authored point', 'uncertainty': ['Not lattice exactness'],
            'strength': q(Fraction(1, 2)), 'maximum_shift_qn': q(4), 'matches': [{'note_id': 'a',
                'annotation_id': rows[1]['annotation_id'], 'point': {'kind': 'declared_pulse_point',
                    'original_source_frame_q': q(Fraction(17403, 2)), 'statement': 'Caller point'}}]}],
        'locks': {'outside_selection': 'all', 'selected_fields': []}, 'attribution': base['attribution']}
    return args, base


def execute(args):
    result = timing.midi_timing_alternatives(**args)
    return result, read_record(result['artifacts']['manifest'], args['store_root'])


def test_exact_nonzero_crop_fractional_bridge_independent_public_edit(context):
    args, _ = context
    before = copy.deepcopy(args['material'])
    result, body = execute(args)
    assert result['unchanged'] == result['no_addition']
    assert len(canonical_bytes(result)) <= 16384
    assert args['material'] == before
    for index, expected in enumerate([q(2), q(Fraction(16001, 16000))]):
        out = body['outcomes'][index]
        step = out['steps'][0]
        assert step['delta_qn'] == expected
        child = read_record(out['material'], args['store_root'])
        assert child['notes'][0]['onset'] == {'space': 'clip_qn', **expected}
        assert child['notes'][1] == before['notes'][1]
        for key in ('events', 'curves', 'sources', 'tracks', 'clips'):
            assert child[key] == before[key]
        sel = material_query(body['unchanged'], args['store_root'], selection={'note_ids': ['a']})['selection']
        external = midi_transform(body['unchanged'], sel, [{'op': 'shift', 'delta_qn': expected}],
            args['store_root'], 'external-' + str(index), locks=args['locks'],
            expression_policy='reject', overlap_policy='reject_new')
        assert external['material'] == out['material']
    pulse = body['outcomes'][1]['steps'][0]
    assert pulse['source_frame_q'] == q(Fraction(17403, 2))
    assert pulse['host_seconds_q'] == q(Fraction(176001, 16000))
    assert pulse['original_projection']['lattice_origin'] == {
        'local_estimate_seconds': .1, 'original_offset_seconds_q': q(Fraction(701, 8000))}


@pytest.mark.parametrize('mutation', ['source_sha', 'rate', 'revision', 'selection', 'missing', 'duplicate_alternative',
    'onset_lock', 'unreduced', 'float_point', 'end_point', 'kind', 'unknown_annotation', 'max_shift'])
def test_preflight_refuses_without_published_timing(context, mutation):
    args, _ = context
    if mutation == 'source_sha': args['alignment']['original_sha256'] = '0' * 64
    elif mutation == 'rate': args['alignment']['sample_rate'] = 44100
    elif mutation == 'revision': args['expected_hypotheses_revision'] = '0' * 64
    elif mutation == 'selection': args['selection']['note_ids'] = ['outside']
    elif mutation == 'missing': args['alternatives'][0]['matches'] = []
    elif mutation == 'duplicate_alternative': args['alternatives'][1]['alternative_id'] = 'attack'
    elif mutation == 'onset_lock': args['locks']['selected_fields'] = ['onset']
    elif mutation == 'unreduced': args['alternatives'][0]['strength'] = {'n': 2, 'd': 2}
    elif mutation == 'float_point': args['alternatives'][1]['matches'][0]['point']['original_source_frame_q'] = .1
    elif mutation == 'end_point': args['alternatives'][1]['matches'][0]['point']['original_source_frame_q'] = q(16701)
    elif mutation == 'kind': args['alternatives'][0]['matches'][0]['point'] = {'kind': 'declared_pulse_point', 'original_source_frame_q': q(8701), 'statement': 'wrong'}
    elif mutation == 'unknown_annotation': args['alternatives'][0]['matches'][0]['annotation_id'] = 'unknown'
    else: args['alternatives'][0]['maximum_shift_qn'] = q(1)
    before = set(Path(args['store_root']).rglob('record.json'))
    with pytest.raises(PocketError): timing.midi_timing_alternatives(**args)
    assert set(Path(args['store_root']).rglob('record.json')) == before


@pytest.mark.parametrize('mutation', ['step_delta', 'step_selection', 'step_before', 'frame', 'clock', 'unchanged', 'order'])
def test_resealed_semantic_proofs_refuse(context, mutation):
    args, _ = context
    result, body = execute(args)
    step = body['outcomes'][0]['steps'][0]
    if mutation == 'step_delta': step['operations'][0]['delta_qn'] = q(1)
    elif mutation == 'step_selection': step['selection']['note_ids'] = ['outside']
    elif mutation == 'step_before': step['before'] = step['after']
    elif mutation == 'frame': step['source_frame_q'] = q(8702)
    elif mutation == 'clock': body['inputs']['alignment']['host_anchor_seconds_q'] = q(11)
    elif mutation == 'unchanged': body['no_addition'] = step['after']
    else: body['outcomes'].reverse()
    forged = put_record(body, args['store_root'])
    with pytest.raises(PocketError): timing.midi_timing_query(store_root=args['store_root'], manifest=forged)
    assert result['unchanged'] != step['after']


def test_relocation_no_journals_source_models_and_query_no_original_writes(context, tmp_path, monkeypatch):
    args, base = context
    result, _ = execute(args)
    moved = tmp_path / 'relocated'
    shutil.copytree(Path(args['store_root']) / 'artifacts', moved / 'artifacts')
    Path(base['source']['path']).unlink()
    before = {str(p.relative_to(moved)): hashlib.sha256(p.read_bytes()).hexdigest() for p in moved.rglob('*') if p.is_file()}
    page = timing.midi_timing_query(store_root=str(moved), manifest=result['artifacts']['manifest'], view='matches', limit=1)
    assert page['next_cursor'] and len(page['items']) == 1
    with pytest.raises(PocketError):
        timing.midi_timing_query(store_root=str(moved), manifest=result['artifacts']['manifest'], view='summary', cursor=page['next_cursor'])
    after = {str(p.relative_to(moved)): hashlib.sha256(p.read_bytes()).hexdigest() for p in moved.rglob('*') if p.is_file()}
    assert before == after


def test_intermediate_overlap_refuses_safe_simultaneous_swap(context):
    args, _ = context
    data = material([note('a', 0, 1), note('b', 2, 1)])
    data['clips'][0]['origin'] = {'space': 'arrangement_qn', **q(0)}
    data = seal_literal(data)
    args['material'] = data
    args['selection'] = material_query(data, args['store_root'], selection={'note_ids': ['a', 'b']})['selection']
    pulse_id = args['alternatives'][1]['matches'][0]['annotation_id']
    args['alternatives'] = [args['alternatives'][0]]
    args['alternatives'][0]['matches'].append({'note_id': 'b', 'annotation_id': pulse_id,
        'point': {'kind': 'declared_pulse_point', 'original_source_frame_q': q(701), 'statement': 'Swap second to zero'}})
    # Final [2,3),[0,1) would be disjoint, but first move reaches occupied b.
    with pytest.raises(PocketError, match='overlap'):
        timing.midi_timing_alternatives(**args)
    for path in Path(args['store_root']).rglob('record.json'):
        import json
        assert json.loads(path.read_text())['schema'] != timing.SCHEMA


def test_zero_strength_no_revision_and_selected_lock(context):
    args, _ = context
    args['locks']['selected_fields'] = ['onset']
    for alt in args['alternatives']:
        alt['strength'] = q(0)
    result, body = execute(args)
    assert all(o['material'] == result['unchanged'] and o['steps'][0]['edit'] is None for o in body['outcomes'])
    assert timing.midi_timing_alternatives(**args) == result
    args['alignment']['host_anchor_seconds_q'] = q(11)
    with pytest.raises(PocketError): timing.midi_timing_alternatives(**args)


def test_step_tempo_crossing_uses_public_clock_not_constant_bpm(context):
    args, _ = context
    args['alternatives'] = [args['alternatives'][0]]
    args['alignment']['host_anchor_seconds_q'] = q(12)
    args['alternatives'][0]['maximum_shift_qn'] = q(6)
    _, body = execute(args)
    # Corrected attack is1second after sourceanchor:host13, past map's tempo change at host12→QN4.
    assert body['outcomes'][0]['steps'][0]['target_arrangement_qn'] == q(5)
    assert body['outcomes'][0]['steps'][0]['delta_qn'] == q(5)


def test_second_public_edit_failure_has_no_success_manifest_or_retry(context, monkeypatch):
    args, _ = context
    real = timing.midi_transform
    calls = []
    def fail(*a, **kw):
        calls.append(kw.get('request_id'))
        if len(calls) == 2:
            raise KeyboardInterrupt('synthetic interruption after successful first alternative')
        return real(*a, **kw)
    monkeypatch.setattr(timing, 'midi_transform', fail)
    with pytest.raises(KeyboardInterrupt): timing.midi_timing_alternatives(**args)
    assert len(calls) == 2
    with pytest.raises(PocketError): timing.midi_timing_alternatives(**args)
    assert len(calls) == 2
    import json
    records = [json.loads(p.read_text()) for p in Path(args['store_root']).rglob('record.json')]
    assert not any(r['schema'] == timing.SCHEMA for r in records)
    progress = [r for r in records if r['schema'] == 'pocket.midi-timing-progress/v1']
    assert len(progress) == 1 and progress[0]['state'] == 'incomplete'
    assert len(progress[0]['active']['steps']) == 1
    assert progress[0]['active']['steps'][0]['edit']['schema'] == 'pocket.artifact-handle/v1'


def test_external_named_json_material_graph_replays(context):
    from pocket_music.artifact_store import put_bytes
    args, _ = context
    args['material'] = put_bytes(canonical_bytes(args['material']), args['store_root'], 'external-material.json', 'pocket.material/v1')
    result, body = execute(args)
    assert body['unchanged'] == args['material']
    timing.load_timing_alternatives(result['artifacts']['manifest'], args['store_root'])


def test_nonrecord_json_ancestry_nodes_cannot_evade_local_budget(context):
    from pocket_music.artifact_store import put_bytes
    args, _ = context
    child = put_bytes(canonical_bytes({'schema': 'pocket.synthetic-test/v1', 'rows': [0] * 70001}),
        args['store_root'], 'external.json', 'pocket.synthetic-test/v1')
    parent = put_bytes(canonical_bytes({'schema': 'pocket.synthetic-test/v1', 'child': child}),
        args['store_root'], 'parent.json', 'pocket.synthetic-test/v1')
    with pytest.raises(PocketError, match='node/depth budget'):
        timing._graph(parent, args['store_root'], input_budget=True)


def test_automatic_and_superseded_rows_never_become_approved_matches(context):
    args, _ = context
    wrapper = read_record(args['hypotheses'], args['store_root'])
    authored = read_record(wrapper['local_hypotheses'], args['store_root'])
    automatic = read_record(authored['parent'], args['store_root'])['annotations'][0]['annotation_id']
    original = args['alternatives'][0]['matches'][0]['annotation_id']
    args['alternatives'][0]['matches'][0]['annotation_id'] = automatic
    with pytest.raises(PocketError, match='active authored'): timing.midi_timing_alternatives(**args)
    args['alternatives'][0]['matches'][0]['annotation_id'] = original
    parent = wrapper['local_hypotheses']
    wrapper['local_hypotheses'] = audio_hypothesis_correct(store_root=args['store_root'], request_id='supersede',
        parent=parent, expected_revision=parent['sha256'], attribution=args['attribution'],
        corrections=[{'correction_id': 'replacement', 'supersedes': [original],
            'annotation': {'kind': 'attack', 'source_frame': 8100, 'strength_relative': None},
            'support': [{'kind': 'annotation_id', 'reference': original}], 'uncertainty': []}])['artifacts']['hypotheses']
    args['hypotheses'] = put_record(wrapper, args['store_root'])
    args['expected_hypotheses_revision'] = args['hypotheses']['sha256']
    with pytest.raises(PocketError, match='active authored'): timing.midi_timing_alternatives(**args)


@pytest.mark.parametrize('bad', [None, [], 'not-a-handle', {'sha256': '0' * 64}])
def test_direct_malformed_hypothesis_identity_is_domain_refusal(context, bad):
    args, _ = context
    args['hypotheses'] = bad
    with pytest.raises(PocketError): timing.midi_timing_alternatives(**args)


def test_retained_equal_time_controller_order_never_moves_with_notes(context):
    from test_midi_lifecycle_qa import control
    args, _ = context
    data = args['material']
    data['events'] = [control('cc-first', 1, 12, order=0, cc=11), control('cc-second', 1, 99, order=1, cc=11)]
    data['clips'][0]['event_ids'] = [event['id'] for event in data['events']]
    args['material'] = seal_literal(data)
    args['selection'] = material_query(args['material'], args['store_root'], selection={'note_ids': ['a']})['selection']
    _, body = execute(args)
    for out in body['outcomes']:
        child = read_record(out['material'], args['store_root'])
        assert child['events'] == data['events']
        assert child['clips'][0]['event_ids'] == ['cc-first', 'cc-second']
