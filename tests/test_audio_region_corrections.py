# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic coordinate-wrapper proofs; no native/model execution or listening."""
import builtins
import copy
import importlib
import shutil
from pathlib import Path

import pytest
from test_audio_region_analysis import run

from pocket_music.artifact_store import canonical_bytes, put_record, read_record
from pocket_music.audio_hypotheses import audio_hypothesis_correct
from pocket_music.audio_region_analysis import audio_region_query, load_audio_region_hypotheses
from pocket_music.audio_region_corrections import audio_region_correct
from pocket_music.errors import PocketError

ATTRIBUTION = {'actor': 'Synthetic correction reviewer', 'actor_kind': 'agent',
               'statement': 'Fixture coordinate hypothesis only', 'uncertainty': ['No actual listening']}


@pytest.fixture
def region_fixture(tmp_path):
    receipt = run(tmp_path)
    handle = receipt['artifacts']['hypotheses']
    store = str(tmp_path / 'store')
    wrapper = read_record(handle, store)
    annotation = audio_region_query(store_root=store, hypotheses=handle, view='annotations')['items'][0]['local']
    return {'handle': handle, 'store': store, 'wrapper': wrapper, 'support': annotation['annotation_id'], 'root': tmp_path}


def batch(fixture, original=False):
    rows = [{'kind': 'attack', 'source_frame': 10, 'strength_relative': None},
            {'kind': 'phrase_anchor', 'start_frame': 0, 'end_frame_exclusive': 16000, 'label': 'Fixture phrase'},
            {'kind': 'note_hypothesis', 'start_frame': 100, 'end_frame_exclusive': 8000,
             'midi_note': 64, 'cents': 0, 'tuning_ref': 'caller-declared-12tet'},
            {'kind': 'pulse_candidate', 'start_frame': 0, 'end_frame_exclusive': 16000,
             'bpm': 120, 'source_lattice_origin_seconds': .1}]
    if original:
        for row in rows:
            for key in ('source_frame', 'start_frame', 'end_frame_exclusive'):
                if key in row: row[key] += 100
            if row['kind'] == 'pulse_candidate':
                row['lattice_origin'] = {'local_estimate_seconds': row.pop('source_lattice_origin_seconds'),
                                         'original_offset_seconds_q': {'n': 1, 'd': 80}}
    return {'coordinate_space': 'original_source_frame' if original else 'local_crop_frame',
            'corrections': [{'correction_id': f'fixture-{i}', 'supersedes': [fixture['support']], 'annotation': value,
                             'support': [{'kind': 'annotation_id', 'reference': fixture['support']}],
                             'uncertainty': ['Synthetic alternative']} for i, value in enumerate(rows)]}


def correct(fixture, request='correct', **changes):
    values = {'store_root': fixture['store'], 'request_id': request, 'parent': fixture['handle'],
              'expected_revision': fixture['handle']['sha256'], 'batch': batch(fixture), 'attribution': ATTRIBUTION}
    values.update(changes)
    return audio_region_correct(**values)


def test_all_forms_original_local_and_external_composition_exact_identity(region_fixture):
    f = region_fixture
    local = batch(f)
    original = batch(f, True)
    before = copy.deepcopy(original)
    a = correct(f, 'local', batch=local)
    b = correct(f, 'original', batch=original)
    assert a['artifacts'] == b['artifacts'] and original == before
    direct = audio_hypothesis_correct(store_root=f['store'], request_id='external-direct',
        parent=f['wrapper']['local_hypotheses'], expected_revision=f['wrapper']['local_hypotheses']['sha256'],
        corrections=local['corrections'], attribution=ATTRIBUTION)
    composed = put_record({**f['wrapper'], 'local_hypotheses': direct['artifacts']['hypotheses']}, f['store'])
    assert composed == a['artifacts']['hypotheses']
    rows = audio_region_query(store_root=f['store'], hypotheses=composed, view='annotations')['items'][-4:]
    assert rows[0]['original_projection']['source_frame'] == 110
    assert rows[1]['original_projection']['start_frame'] == 100
    assert rows[1]['original_projection']['end_frame_exclusive'] == 16100
    assert rows[-1]['original_projection']['lattice_origin'] == {'local_estimate_seconds': .1, 'original_offset_seconds_q': {'n': 1, 'd': 80}}
    wrapper = load_audio_region_hypotheses(composed, f['store'])
    assert wrapper['request_attribution'] == f['wrapper']['request_attribution'] != ATTRIBUTION
    assert all(row['local']['attribution'] == ATTRIBUTION for row in rows)
    assert len(canonical_bytes(a)) < 4096


def test_stale_wrapper_fails_before_any_write(region_fixture):
    f = region_fixture
    before = {str(p): p.read_bytes() for p in Path(f['store']).rglob('*') if p.is_file()}
    with pytest.raises(PocketError, match='Stale'):
        correct(f, expected_revision='f' * 64)
    assert before == {str(p): p.read_bytes() for p in Path(f['store']).rglob('*') if p.is_file()}


@pytest.mark.parametrize('rational', [{'n': 2, 'd': 160}, {'n': 1, 'd': 0}, {'n': 1, 'd': -80},
    {'n': True, 'd': 80}, {'n': 0, 'd': 1}, {'n': 1, 'd': 80, 'units': 'seconds'}, {'n': 10**100, 'd': 80}])
def test_original_pulse_requires_canonical_exact_offset(region_fixture, rational):
    value = batch(region_fixture, True)
    value['corrections'][-1]['annotation']['lattice_origin']['original_offset_seconds_q'] = rational
    with pytest.raises(PocketError): correct(region_fixture, batch=value)


@pytest.mark.parametrize('index,key,value', [(0, 'source_frame', 99), (0, 'source_frame', 16100),
    (0, 'source_frame', {'n': 201, 'd': 2}), (0, 'source_frame', True), (1, 'start_frame', 99),
    (1, 'end_frame_exclusive', 16101), (1, 'end_frame_exclusive', 100)])
def test_original_half_open_boundary_and_fraction_refusal(region_fixture, index, key, value):
    change = batch(region_fixture, True)
    change['corrections'][index]['annotation'][key] = value
    with pytest.raises(PocketError): correct(region_fixture, batch=change)


@pytest.mark.parametrize('change', ['extra', 'unknown_space', 'empty', '129', 'unknown_annotation', 'global_pulse_float', 'support'])
def test_closed_union_and_existing_support_validation(region_fixture, change):
    value = batch(region_fixture, True)
    if change == 'extra': value['hidden'] = True
    if change == 'unknown_space': value['coordinate_space'] = 'seconds'
    if change == 'empty': value['corrections'] = []
    if change == '129': value['corrections'] = value['corrections'] * 33
    if change == 'unknown_annotation': value['corrections'][0]['annotation']['kind'] = 'learned_beat'
    if change == 'global_pulse_float': value['corrections'][-1]['annotation']['lattice_origin'] = {'original_seconds': .1125}
    if change == 'support': value['corrections'][0]['support'] = [{'kind': 'annotation_id', 'reference': 'invented'}]
    with pytest.raises(PocketError): correct(region_fixture, batch=value)


def test_corrected_wrapper_chain_replay_and_source_raw_lock(region_fixture):
    f = region_fixture
    before = {str(p): p.read_bytes() for p in (Path(f['store']) / 'artifacts').rglob('*') if p.is_file()}
    first = correct(f)
    assert correct(f) == first
    parent = first['artifacts']['hypotheses']
    second_batch = batch(f)
    for item in second_batch['corrections']: item['correction_id'] += '-second'
    second = correct(f, 'second', parent=parent, expected_revision=parent['sha256'], batch=second_batch)
    assert audio_region_query(store_root=f['store'], hypotheses=second['artifacts']['hypotheses'])['items'][0]['local_revision_index'] == 2
    assert all(Path(p).read_bytes() == data for p, data in before.items())
    with pytest.raises(PocketError):
        correct(f, 'duplicate', parent=parent, expected_revision=parent['sha256'])


def test_artifact_only_relocated_correction_and_query_no_analysis(region_fixture, tmp_path, monkeypatch):
    f = region_fixture
    moved = tmp_path / 'relocated'
    shutil.copytree(Path(f['store']) / 'artifacts', moved / 'artifacts')
    (tmp_path / 'source.wav').unlink()
    module = importlib.import_module('pocket_music.audio_hypotheses')
    monkeypatch.setattr(module, 'analyze_region', lambda *a, **k: pytest.fail('Correction reran Peek'))
    real_import = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name.split('.')[0] in {'torch', 'torchaudio', 'beat_this', 'soxr'}:
            pytest.fail('Correction imported optional model runtime')
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    result = correct(f, store_root=str(moved), batch=batch(f, True))
    assert audio_region_query(store_root=str(moved), hypotheses=result['artifacts']['hypotheses'])['status'] == 'ok'


def test_local_partial_failure_has_no_success_wrapper(region_fixture, monkeypatch):
    f = region_fixture
    module = importlib.import_module('pocket_music.audio_region_corrections')
    before = set((Path(f['store']) / 'artifacts').rglob('record.json'))
    def fail(**kwargs): raise RuntimeError('simulated local correction failure')
    monkeypatch.setattr(module, 'audio_hypothesis_correct', fail)
    with pytest.raises(RuntimeError): correct(f)
    assert set((Path(f['store']) / 'artifacts').rglob('record.json')) == before


def test_learned_fractional_model_evidence_preserved_without_runtime(tmp_path, monkeypatch):
    from test_audio_models import PulseModelTests, rehash

    from pocket_music.audio_pulse_hypotheses import SETTINGS
    fixture = PulseModelTests()
    fixture.setUp()
    def runner(*args, **kwargs):
        result, binding = fixture.runner(*args, **kwargs)
        if result['analysis'] is not None:
            raw = result['analysis']
            raw['beat'][20:22] = [1., 1.]
            raw['vendor_beats_seconds'] = [.41]
            rehash(raw)
        return result, binding
    monkeypatch.setattr('pocket_music.audio_pulse_hypotheses._runner', runner)
    try:
        initial = run(tmp_path, {'kind': 'inline', 'source': fixture.source},
                      {'kind': 'learned_pulse', 'model': fixture.model, 'settings': SETTINGS})
        handle = initial['artifacts']['hypotheses']
        store = str(tmp_path / 'store')
        old = audio_region_query(store_root=store, hypotheses=handle, view='annotations')['items'][0]
        assert old['local']['annotation']['model_frame_q'] == {'n': 41, 'd': 2}
        monkeypatch.setattr('pocket_music.audio_pulse_hypotheses._runner', lambda *a, **k: pytest.fail('Correction reran model'))
        value = {'coordinate_space': 'original_source_frame', 'corrections': [{
            'correction_id': 'learned-alternative', 'supersedes': [old['local']['annotation_id']],
            'annotation': {'kind': 'attack', 'source_frame': 3418, 'strength_relative': None},
            'support': [{'kind': 'annotation_id', 'reference': old['local']['annotation_id']}],
            'uncertainty': ['Synthetic authored alternative, not listening']}]}
        child = audio_region_correct(store_root=store, request_id='learned-correct', parent=handle,
            expected_revision=handle['sha256'], batch=value, attribution=ATTRIBUTION)
        rows = audio_region_query(store_root=store, hypotheses=child['artifacts']['hypotheses'], view='annotations')['items']
        assert rows[0] == old
        assert rows[-1]['local']['annotation']['source_frame'] == 3281
        assert rows[-1]['original_projection']['source_frame'] == 3418
    finally:
        fixture.doCleanups()
