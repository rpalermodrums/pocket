# SPDX-License-Identifier: AGPL-3.0-only
"""Independent original-coordinate correction equivalence and hard-boundary QA."""
import copy
import hashlib
import importlib
import shutil
import struct
from pathlib import Path

import pytest
from test_audio_region_analysis import run

from pocket_music.artifact_store import canonical_bytes, digest, put_record, read_record
from pocket_music.audio_hypotheses import audio_hypothesis_correct
from pocket_music.audio_region_analysis import audio_region_query
from pocket_music.audio_region_corrections import audio_region_correct
from pocket_music.errors import PocketError

ACTOR = {'actor': 'Independent test reviewer', 'actor_kind': 'agent',
         'statement': 'Synthetic alternative only.', 'uncertainty': ['No human audition.']}


@pytest.fixture
def original(tmp_path):
    store = str(tmp_path / 'store')
    parent = run(tmp_path)['artifacts']['hypotheses']
    wrapper = read_record(parent, store)
    initial = read_record(wrapper['local_hypotheses'], store)
    return store, parent, wrapper, initial


def item(identity='independent', frame=101):
    return {'correction_id': identity, 'supersedes': [],
        'annotation': {'kind': 'attack', 'source_frame': frame, 'strength_relative': None},
        'support': [{'kind': 'analysis_pointer', 'reference': '/analysis/region/start_frame'}],
        'uncertainty': ['Interpretation, not measured acoustic truth.']}


def correct(original, changes=None, parent=None, request='correct', coordinate='original_source_frame', actor=ACTOR):
    store, old_parent, _, _ = original
    parent = parent or old_parent
    return audio_region_correct(store_root=store, request_id=request, parent=parent,
        expected_revision=parent['sha256'], batch={'coordinate_space': coordinate, 'corrections': changes or [item()]}, attribution=actor)


def artifacts(store):
    return {str(p.relative_to(store)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(store).rglob('*') if p.is_file()}


def test_original_pulse_exact_float_bits_and_public_composition(original):
    store, _, wrapper, _ = original
    estimate = .10000000000000002
    change = item()
    change['annotation'] = {'kind': 'pulse_candidate', 'start_frame': 100, 'end_frame_exclusive': 16100, 'bpm': 123.5,
        'lattice_origin': {'local_estimate_seconds': estimate, 'original_offset_seconds_q': {'n': 1, 'd': 80}}}
    snapshot = copy.deepcopy(change)
    result = correct(original, [change])
    local_change = copy.deepcopy(change)
    local_change['annotation'] = {'kind': 'pulse_candidate', 'start_frame': 0, 'end_frame_exclusive': 16000,
        'bpm': 123.5, 'source_lattice_origin_seconds': estimate}
    direct = audio_hypothesis_correct(store_root=store, request_id='direct', parent=wrapper['local_hypotheses'],
        expected_revision=wrapper['local_hypotheses']['sha256'], corrections=[local_change], attribution=ACTOR)
    assert direct['artifacts']['hypotheses'] == result['artifacts']['local_hypotheses']
    assert put_record({**wrapper, 'local_hypotheses': direct['artifacts']['hypotheses']}, store) == result['artifacts']['hypotheses']
    row = audio_region_query(store_root=store, hypotheses=result['artifacts']['hypotheses'], view='annotations')['items'][-1]
    assert struct.pack('!d', row['local']['annotation']['source_lattice_origin_seconds']) == struct.pack('!d', estimate)
    assert row['original_projection']['lattice_origin']['original_offset_seconds_q'] == {'n': 1, 'd': 80}
    assert change == snapshot


@pytest.mark.parametrize('rational', [{'n': True, 'd': 80}, {'n': 1, 'd': True}, {'n': 2, 'd': 160},
    {'n': 1, 'd': 0}, {'n': -1, 'd': -80}, {'n': 2**63, 'd': 80}, {'n': 1, 'd': 2**63}, {'n': 1, 'd': 80, 'x': 0}])
def test_bad_original_pulse_offsets_refuse_before_write(original, rational):
    change = item()
    change['annotation'] = {'kind': 'pulse_candidate', 'start_frame': 100, 'end_frame_exclusive': 16100, 'bpm': 120,
        'lattice_origin': {'local_estimate_seconds': .1, 'original_offset_seconds_q': rational}}
    before = artifacts(original[0])
    with pytest.raises(PocketError):
        correct(original, [change])
    assert artifacts(original[0]) == before


@pytest.mark.parametrize('frame', [99, 16100, True, {'n': 201, 'd': 2}])
def test_original_attack_boundary_refusal_before_write(original, frame):
    before = artifacts(original[0])
    with pytest.raises(PocketError):
        correct(original, [item(frame=frame)])
    assert artifacts(original[0]) == before


def test_stale_revision_has_no_journal_or_artifact_side_effect(original):
    store, parent, _, _ = original
    before = artifacts(store)
    with pytest.raises(PocketError, match='Stale'):
        audio_region_correct(store_root=store, request_id='never-created', parent=parent, expected_revision='0'*64,
            batch={'coordinate_space': 'original_source_frame', 'corrections': [item()]}, attribution=ACTOR)
    assert artifacts(store) == before


def test_relocated_store_without_original_or_analysis_runtime(original, tmp_path, monkeypatch):
    store, parent, wrapper, initial = original
    destination = tmp_path / 'moved'
    shutil.copytree(Path(store) / 'artifacts', destination / 'artifacts')
    (tmp_path / 'source.wav').unlink()
    module = importlib.import_module('pocket_music.audio_hypotheses')
    monkeypatch.setattr(module, 'analyze_region', lambda *a, **k: pytest.fail('Reanalysis'))
    before = artifacts(destination)
    changed = correct((str(destination), parent, wrapper, initial))
    assert audio_region_query(store_root=str(destination), hypotheses=changed['artifacts']['hypotheses'])['status'] == 'ok'
    assert all(artifacts(destination)[key] == value for key, value in before.items())


def test_full_batch_revision_and_cumulative_annotation_limits(original):
    store, parent, _, initial = original
    count = initial['annotation_count']
    for revision in range(31):
        changes = [item(f'fix-{revision}-{i}') for i in range(128)]
        result = correct(original, changes, parent, f'rev-{revision}')
        parent = result['artifacts']['hypotheses']
        count += 128
        assert len(canonical_bytes(result)) < 4096
    # 32nd full batch would exceed total4096 even though revision32 is allowed.
    changes = [item(f'over-{i}') for i in range(128)]
    with pytest.raises(PocketError, match='4096'):
        correct(original, changes, parent, 'too-many-annotations')
    remaining = 4096-count
    assert 1 <= remaining < 128
    result = correct(original, [item(f'last-{i}') for i in range(remaining)], parent, 'exact-count')
    parent = result['artifacts']['hypotheses']
    summary = audio_region_query(store_root=store, hypotheses=parent)['items'][0]
    assert summary['annotations'] == 4096 and summary['local_revision_index'] == 32
    with pytest.raises(PocketError, match='32 revisions'):
        correct(original, [item('one-more')], parent, 'too-many-revisions')


def test_resealed_corrected_row_cannot_impersonate_algorithm(original):
    store, _, _, _ = original
    result = correct(original)
    child = read_record(result['artifacts']['local_hypotheses'], store)
    row = child['annotations'][0]
    row['attribution'] = {'actor': 'Peek', 'actor_kind': 'algorithm', 'analysis_version': 'invented'}
    body = {k: v for k, v in row.items() if k != 'annotation_id'}
    row['annotation_id'] = 'audio:' + digest([child['parent'], body])
    wrapper = read_record(result['artifacts']['hypotheses'], store)
    wrapper['local_hypotheses'] = put_record(child, store)
    with pytest.raises(PocketError):
        audio_region_query(store_root=store, hypotheses=put_record(wrapper, store))


def test_same_batch_forward_reference_and_duplicate_ids_no_child(original):
    store, _, _, _ = original
    before = set(Path(store).glob('artifacts/*/record.json'))
    first, second = item('same'), item('same', frame=102)
    with pytest.raises(PocketError):
        correct(original, [first, second])
    assert set(Path(store).glob('artifacts/*/record.json')) == before
    second['correction_id'] = 'second'
    second['support'] = [{'kind': 'annotation_id', 'reference': 'not-yet-published'}]
    with pytest.raises(PocketError):
        correct(original, [first, second], request='forward')
    assert set(Path(store).glob('artifacts/*/record.json')) == before


def test_learned_fractional_prior_survives_original_correction_without_model(tmp_path, monkeypatch):
    import builtins

    from test_audio_models import PulseModelTests, rehash

    from pocket_music.audio_pulse_hypotheses import SETTINGS
    from pocket_music.audio_region_analysis import audio_region_hypotheses
    f = PulseModelTests()
    f.setUp()
    def runner(*a, **k):
        result, binding = f.runner(*a, **k)
        if result['analysis'] is not None:
            raw = result['analysis']
            raw['beat'][20:22] = [1., 1.]
            raw['vendor_beats_seconds'] = [.41]
            rehash(raw)
        return result, binding
    monkeypatch.setattr('pocket_music.audio_pulse_hypotheses._runner', runner)
    store = str(tmp_path / 'store')
    try:
        parent = audio_region_hypotheses(store_root=store, request_id='model-region',
            region={'kind': 'inline', 'source': f.source},
            analysis={'kind': 'learned_pulse', 'model': f.model, 'settings': SETTINGS}, attribution=ACTOR)['artifacts']['hypotheses']
        wrapper = read_record(parent, store)
        initial = read_record(wrapper['local_hypotheses'], store)
        prior = initial['annotations'][0]
        assert prior['annotation']['model_frame_q'] == {'n': 41, 'd': 2}
        Path(f.source['path']).unlink()
        Path(f.declaration['weights']['path']).unlink()
        monkeypatch.setattr('pocket_music.audio_pulse_hypotheses._runner', lambda *a, **k: pytest.fail('inference'))
        real_import = builtins.__import__
        def guarded(name, *a, **k):
            if name.split('.')[0] in {'torch', 'torchaudio', 'beat_this', 'soxr'}:
                pytest.fail('Optional model import during retained correction')
            return real_import(name, *a, **k)
        monkeypatch.setattr(builtins, '__import__', guarded)
        change = item('model-alternative', frame=3418)
        change['supersedes'] = [prior['annotation_id']]
        change['support'] = [{'kind': 'annotation_id', 'reference': prior['annotation_id']}]
        before = artifacts(store)
        result = correct((store, parent, wrapper, initial), [change])
        rows = audio_region_query(store_root=store, hypotheses=result['artifacts']['hypotheses'], view='annotations')['items']
        assert rows[0]['local'] == prior and rows[0]['original_projection']['source_frame_q'] == {'n': 3417, 'd': 1}
        assert rows[-1]['local']['attribution'] == ACTOR
        assert rows[-1]['local']['annotation']['source_frame'] == 3281
        assert all(artifacts(store)[key] == value for key, value in before.items())
    finally:
        f.doCleanups()
