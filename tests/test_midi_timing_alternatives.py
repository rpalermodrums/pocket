"""Synthetic authored timing evidence; no listening or model execution."""
import copy
import shutil
from pathlib import Path

import pytest
from test_audio_region_analysis import ACTOR, run
from test_midi_qa import literal_material, seal_literal
from test_time_maps import create, q

from pocket_music.artifact_store import canonical_bytes, put_record, read_record
from pocket_music.audio_region_analysis import _load, audio_region_query
from pocket_music.audio_region_corrections import audio_region_correct
from pocket_music.errors import PocketError
from pocket_music.material import material_query
from pocket_music.midi_edit import midi_transform
from pocket_music.midi_timing_alternatives import (
    SCHEMA,
    load_timing_alternatives,
    midi_timing_alternatives,
    midi_timing_query,
)


@pytest.fixture
def fixture(tmp_path):
    store = str(tmp_path / 'store')
    raw = run(tmp_path)['artifacts']['hypotheses']
    support = audio_region_query(store_root=store, hypotheses=raw, view='annotations')['items'][0]['local']['annotation_id']
    corrections = [{'correction_id': 'attack', 'supersedes': [support],
        'annotation': {'kind': 'attack', 'source_frame': 3900, 'strength_relative': None},
        'support': [{'kind': 'annotation_id', 'reference': support}], 'uncertainty': ['Synthetic only']},
        {'correction_id': 'pulse', 'supersedes': [support], 'annotation': {'kind': 'pulse_candidate',
            'start_frame': 0, 'end_frame_exclusive': 16000, 'bpm': 123, 'source_lattice_origin_seconds': .123456789},
         'support': [{'kind': 'annotation_id', 'reference': support}], 'uncertainty': ['Alternative pulse'] }]
    hyp = audio_region_correct(store_root=store, request_id='correction', parent=raw, expected_revision=raw['sha256'],
        batch={'coordinate_space': 'local_crop_frame', 'corrections': corrections}, attribution=ACTOR)['artifacts']['hypotheses']
    _, region, _, rows, _ = _load(hyp, store)
    record = literal_material()
    record['clips'][0]['origin']['space'] = 'arrangement_qn'
    record = seal_literal(record)
    time_map, _ = create(tmp_path)
    selection = material_query(record, store, selection={'note_ids': ['note:0']})['selection']
    args = {'store_root': store, 'request_id': 'timing', 'material': record, 'selection': selection,
        'hypotheses': hyp, 'expected_hypotheses_revision': hyp['sha256'],
        'alignment': {'kind': 'declared_unwarped_source_clock', 'original_sha256': region['original']['sha256'],
            'sample_rate': 8000, 'source_anchor_frame_q': q(0), 'host_anchor_seconds_q': q(10),
            'time_map': time_map, 'clip_id': 'clip:external'},
        'alternatives': [{'alternative_id': 'authored-a', 'statement': 'Use supplied attack', 'uncertainty': ['Not heard'],
            'strength': q(1), 'maximum_shift_qn': q(2), 'matches': [{'note_id': 'note:0',
                'annotation_id': rows[-2]['annotation_id'], 'point': {'kind': 'annotation_attack'}}]}],
        'locks': {'outside_selection': 'all', 'selected_fields': ['pitch', 'velocity']}, 'attribution': ACTOR}
    return args, rows[-1]['annotation_id']


def test_public_external_material_composition_and_no_addition(fixture):
    args, _ = fixture
    result = midi_timing_alternatives(**args)
    manifest = load_timing_alternatives(result['artifacts']['manifest'], args['store_root'])
    assert result['unchanged'] == result['no_addition']
    step = manifest['outcomes'][0]['steps'][0]
    assert step['source_frame_q'] == q(4000) and step['target_clip_qn'] == q(1) and step['delta_qn'] == q(1)
    direct = midi_transform(step['before'], step['selection'], step['operations'], args['store_root'], 'external', locks=args['locks'])
    assert direct['material'] == step['after'] and direct['edit'] == step['edit']
    child = read_record(step['after'], args['store_root'])
    assert child['notes'][0]['onset'] == {'space': 'clip_qn', **q(1)}
    assert child['notes'][1:] == args['material']['notes'][1:]
    assert midi_timing_alternatives(**args) == result
    assert len(canonical_bytes(result)) < 16384


def test_declared_fractional_pulse_and_strength_not_float_lattice(fixture):
    args, pulse = fixture
    match = args['alternatives'][0]['matches'][0]
    match.update(annotation_id=pulse, point={'kind': 'declared_pulse_point', 'original_source_frame_q': q(8001, 2),
                                           'statement': 'Chosen independently of floating lattice'})
    args['alternatives'][0]['strength'] = q(1, 2)
    result = midi_timing_alternatives(**args)
    step = read_record(result['artifacts']['manifest'], args['store_root'])['outcomes'][0]['steps'][0]
    assert step['delta_qn'] == q(8001, 16000)
    assert step['original_projection']['lattice_origin']['local_estimate_seconds'] == .123456789


def test_zero_strength_onset_lock_no_revision(fixture):
    args, _ = fixture
    args['alternatives'][0]['strength'] = q(0)
    args['locks']['selected_fields'].append('onset')
    result = midi_timing_alternatives(**args)
    assert result['alternatives'][0]['material'] == result['unchanged']
    step = read_record(result['artifacts']['manifest'], args['store_root'])['outcomes'][0]['steps'][0]
    assert step['edit'] is None


@pytest.mark.parametrize('change', ['stale_hyp', 'stale_material', 'source_sha', 'rate', 'anchor', 'maxshift',
    'onset_lock', 'duplicate', 'unknown_row', 'phrase', 'noncanonical', 'unknown_field', 'expression'])
def test_preflight_refusals_do_not_publish(fixture, change):
    args, _ = fixture
    if change == 'stale_hyp': args['expected_hypotheses_revision'] = 'a'*64
    elif change == 'stale_material': args['selection']['material_revision'] = 'a'*64
    elif change == 'source_sha': args['alignment']['original_sha256'] = 'a'*64
    elif change == 'rate': args['alignment']['sample_rate'] = 44100
    elif change == 'anchor': args['alignment']['source_anchor_frame_q'] = q(100000)
    elif change == 'maxshift': args['alternatives'][0]['maximum_shift_qn'] = q(1, 2)
    elif change == 'onset_lock': args['locks']['selected_fields'].append('onset')
    elif change == 'duplicate': args['alternatives'][0]['matches'] *= 2
    elif change == 'unknown_row': args['alternatives'][0]['matches'][0]['annotation_id'] = 'missing'
    elif change == 'phrase':
        args['material']['clips'][0]['origin']['space'] = 'phrase_qn'
        args['material'] = seal_literal(args['material'])
        args['selection'] = material_query(args['material'], args['store_root'], selection={'note_ids': ['note:0']})['selection']
    elif change == 'noncanonical': args['alternatives'][0]['strength'] = {'n': 2, 'd': 2}
    elif change == 'unknown_field': args['alternatives'][0]['inferred'] = True
    elif change == 'expression': args['material']['notes'][0]['expression_refs'] = ['unbound']
    before = sorted(str(p) for p in Path(args['store_root']).rglob('*') if p.is_file())
    with pytest.raises(PocketError): midi_timing_alternatives(**args)
    assert before == sorted(str(p) for p in Path(args['store_root']).rglob('*') if p.is_file())


@pytest.mark.parametrize('field', ['delta_qn', 'target_clip_qn', 'annotation', 'after', 'locks', 'no_addition'])
def test_resealed_manifest_requires_semantic_public_replay(fixture, field):
    args, _ = fixture
    result = midi_timing_alternatives(**args)
    record = read_record(result['artifacts']['manifest'], args['store_root'])
    step = record['outcomes'][0]['steps'][0]
    if field in ('delta_qn', 'target_clip_qn'): step[field] = q(3)
    elif field == 'annotation': step[field]['attribution']['actor'] = 'forged actor'
    elif field == 'after': step[field] = step['before']
    elif field == 'locks': record['inputs']['locks']['selected_fields'].append('onset')
    else: record[field] = step['after']
    handle = put_record(record, args['store_root'])
    with pytest.raises(PocketError): midi_timing_query(store_root=args['store_root'], manifest=handle)


def test_artifact_only_relocation_and_query_read_only(fixture, tmp_path):
    args, _ = fixture
    result = midi_timing_alternatives(**args)
    handle = result['artifacts']['manifest']
    original = midi_timing_query(store_root=args['store_root'], manifest=handle, view='steps')
    moved = tmp_path / 'relocated'
    shutil.copytree(Path(args['store_root']) / 'artifacts', moved / 'artifacts')
    # Remove original source and original journals/store availability.
    Path(args['store_root']).rename(tmp_path / 'offline')
    for path in tmp_path.glob('*.wav'): path.unlink()
    before = {str(p): p.read_bytes() for p in moved.rglob('*') if p.is_file()}
    assert midi_timing_query(store_root=str(moved), manifest=handle, view='steps') == original
    assert before == {str(p): p.read_bytes() for p in moved.rglob('*') if p.is_file()}


def test_query_bounds_and_revision_cursor(fixture):
    args, _ = fixture
    args['alternatives'].append({**copy.deepcopy(args['alternatives'][0]), 'alternative_id': 'b'})
    handle = midi_timing_alternatives(**args)['artifacts']['manifest']
    first = midi_timing_query(store_root=args['store_root'], manifest=handle, view='matches', limit=1)
    assert first['next_cursor']
    second = midi_timing_query(store_root=args['store_root'], manifest=handle, view='matches', cursor=first['next_cursor'])
    assert len(second['items']) == 1 and second['next_cursor'] is None
    with pytest.raises(PocketError): midi_timing_query(store_root=args['store_root'], manifest=handle, cursor=first['next_cursor'])
    with pytest.raises(PocketError): midi_timing_query(store_root=args['store_root'], manifest=handle, view='steps', max_bytes=2048)


def test_late_failure_keeps_no_success_manifest(fixture, monkeypatch):
    args, _ = fixture
    args['alternatives'].append({**copy.deepcopy(args['alternatives'][0]), 'alternative_id': 'b'})
    import pocket_music.midi_timing_alternatives as module
    original = module.midi_transform
    calls = []
    def fail(*a, **kw):
        calls.append(1)
        if len(calls) == 2: raise PocketError('injected second alternative failure')
        return original(*a, **kw)
    monkeypatch.setattr(module, 'midi_transform', fail)
    with pytest.raises(PocketError, match='injected'): midi_timing_alternatives(**args)
    assert not any('"schema":"' + SCHEMA + '"' in p.read_text() for p in Path(args['store_root']).rglob('record.json'))


def test_maximum_sixteen_notes_two_alternatives_has_valid_budget_witness(fixture):
    args, _ = fixture
    record = args['material']
    record['notes'] = [{**copy.deepcopy(record['notes'][0]), 'id': f'note:{i}',
                        'pitch': {**record['notes'][0]['pitch'], 'midi_note': 48+i}} for i in range(16)]
    record['clips'][0]['note_ids'] = [n['id'] for n in record['notes']]
    args['material'] = seal_literal(record)
    args['selection'] = material_query(args['material'], args['store_root'], selection={'note_ids': record['clips'][0]['note_ids']})['selection']
    match = args['alternatives'][0]['matches'][0]
    args['alternatives'][0]['matches'] = [{**match, 'note_id': note['id']} for note in record['notes']]
    args['alternatives'].append({**copy.deepcopy(args['alternatives'][0]), 'alternative_id': 'b', 'strength': q(1, 2)})
    result = midi_timing_alternatives(**args)
    proof = load_timing_alternatives(result['artifacts']['manifest'], args['store_root'])
    assert sum(len(alt['steps']) for alt in proof['outcomes']) == 32
    assert all(len(read_record(alt['material'], args['store_root'])['notes']) == 16 for alt in proof['outcomes'])
    assert len(canonical_bytes(result)) <= 16384


def test_nondefault_json_filename_is_fully_replayed(fixture):
    from pocket_music.artifact_store import put_bytes
    args, _ = fixture
    args['material'] = put_bytes(canonical_bytes(args['material']), args['store_root'], 'external-material.json', 'pocket.material/v1')
    result = midi_timing_alternatives(**args)
    assert load_timing_alternatives(result['artifacts']['manifest'], args['store_root'])['unchanged'] == args['material']


def test_step_tempo_and_clip_origin_exact(fixture):
    args, pulse = fixture
    args['alignment']['host_anchor_seconds_q'] = q(12)
    args['material']['clips'][0]['origin'] = {'space': 'arrangement_qn', **q(4)}
    args['material'] = seal_literal(args['material'])
    args['selection'] = material_query(args['material'], args['store_root'], selection={'note_ids': ['note:0']})['selection']
    args['alternatives'][0]['matches'][0].update(annotation_id=pulse,
        point={'kind': 'declared_pulse_point', 'original_source_frame_q': q(8000), 'statement': 'One second after tempo change'})
    result = midi_timing_alternatives(**args)
    step = read_record(result['artifacts']['manifest'], args['store_root'])['outcomes'][0]['steps'][0]
    assert step['target_arrangement_qn'] == q(5) and step['target_clip_qn'] == q(1)
