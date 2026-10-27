# SPDX-License-Identifier: AGPL-3.0-only
"""Numeric spelling parity and preserved pre-normalization feedback evidence."""
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from pocket_music import weave
from pocket_music.errors import PocketError
from pocket_music.record_bag import create_record_bag
from pocket_music.selection_types import SetBrief


@pytest.fixture
def bag(tmp_path):
    return create_record_bag(
        [{'track_id': f't{i}', 'title': f'Generated {i}', 'artists': ['Fixture'],
          'duration_seconds': 120} for i in range(3)], str(tmp_path / 'bag'), 'Fixture')['handle']


def brief():
    return {'track_count': 3, 'anchor_track_ids': ['t0', 't1', 't2'], 'target_minutes': 6,
            'performance_fraction': 1, 'overlap_seconds': 0,
            'intent': {'target_energy': 0, 'creativity': 0, 'max_stretch_percent': 0}}


def plan(bag, value, directory, **kwargs):
    return weave.plan_set_routes(bag, value, str(directory), route_count=1, seed=2, **kwargs)


def test_all_declared_float_fields_typed_transport_and_signed_zero(bag, tmp_path):
    supplied = brief()
    before = deepcopy(supplied)
    typed = TypeAdapter(SetBrief).validate_python(supplied)
    a = plan(bag, supplied, tmp_path / 'a')
    b = plan(bag, typed, tmp_path / 'b')
    assert a['routes'] == b['routes']
    negative_zero = deepcopy(typed)
    negative_zero['overlap_seconds'] = -0.0
    negative_zero['intent'].update(target_energy=-0.0, creativity=-0.0, max_stretch_percent=-0.0)
    assert plan(bag, negative_zero, tmp_path / 'c')['routes'] == a['routes']
    assert supplied == before
    normalized = weave.load_set_plan(a['handle'])['brief']
    for key in ('target_minutes', 'performance_fraction', 'overlap_seconds'):
        assert type(normalized[key]) is float
    assert all(type(normalized['intent'][key]) is float
               for key in ('target_energy', 'creativity', 'max_stretch_percent'))


def test_default_overlap_and_fraction_match_explicit_float_values(bag, tmp_path):
    base = {'track_count': 3, 'anchor_track_ids': ['t0', 't1', 't2']}
    explicit = {**base, 'performance_fraction': .75, 'overlap_seconds': 20.0}
    assert plan(bag, base, tmp_path / 'a')['routes'] == plan(bag, explicit, tmp_path / 'b')['routes']


@pytest.mark.parametrize('field', ['target_minutes', 'performance_fraction', 'overlap_seconds',
                                   'target_energy', 'creativity', 'max_stretch_percent'])
@pytest.mark.parametrize('value', [True, False, float('inf'), float('-inf'), float('nan')])
def test_invalid_numbers_refuse_before_publication(bag, tmp_path, field, value):
    supplied = brief()
    target = supplied['intent'] if field in supplied['intent'] else supplied
    target[field] = value
    with pytest.raises(PocketError):
        plan(bag, supplied, tmp_path / 'refused')
    assert not (tmp_path / 'refused').exists()


def legacy_feedback(bag, tmp_path, monkeypatch):
    # The old provider passed validated numeric spellings through unchanged.
    with monkeypatch.context() as scope:
        scope.setattr(weave, '_canonical_brief_numbers', deepcopy)
        old = plan(bag, brief(), tmp_path / 'old')
    result = weave.record_plan_feedback(old['handle'], old['routes'][0]['route_id'],
                                       'prefer', str(tmp_path / 'feedback'))
    return old, result


def test_legacy_bytes_load_feedback_and_descendant_binding(bag, tmp_path, monkeypatch):
    old, feedback = legacy_feedback(bag, tmp_path, monkeypatch)
    path = Path(old['handle']['path'])
    original = path.read_bytes()
    loaded = weave.load_set_plan(old['handle'])
    assert type(loaded['brief']['performance_fraction']) is int
    assert loaded['routes'] == old['routes']
    assert feedback['feedback']['brief_sha256'] == weave._digest_json(loaded['brief'])[1]
    new = weave.replan_set(feedback['handle'], str(tmp_path / 'new'), seed=2, route_count=1)
    assert new['summary']['feedback_applied_count'] == 1
    again = weave.replan_set(new['handle'], str(tmp_path / 'again'), seed=2, route_count=1)
    assert again['summary']['feedback_applied_count'] == 1
    changed = brief()
    changed['intent']['creativity'] = .1
    other = weave.replan_set(feedback['handle'], str(tmp_path / 'changed'), seed=2,
                            route_count=1, brief=changed)
    assert other['summary']['feedback_applied_count'] == 0
    assert path.read_bytes() == original


def test_resealed_legacy_feedback_wrong_original_binding_refuses(bag, tmp_path, monkeypatch):
    _, feedback = legacy_feedback(bag, tmp_path, monkeypatch)
    record = weave.load_set_plan(feedback['handle'])
    record['feedback'][0]['brief_sha256'] = '0' * 64
    forged = weave._write_manifest(record, str(tmp_path / 'forged'), 'set-plan.json',
                                   'pocket.set-plan-handle/v1')
    with pytest.raises(PocketError, match='exact source plan binding'):
        weave.replan_set(forged, str(tmp_path / 'refused'), seed=2, route_count=1)
    assert not (tmp_path / 'refused').exists()


@pytest.mark.parametrize('mutation', ['nonadjacent_pair', 'reversed_track_ids', 'wrong_binding',
                                      'unknown_scope', 'unknown_disposition', 'route_with_pair',
                                      'missing_null_binding'])
def test_resealed_legacy_scoped_target_refuses(bag, tmp_path, monkeypatch, mutation):
    _, result = legacy_feedback(bag, tmp_path, monkeypatch)
    record = weave.load_set_plan(result['handle'])
    feedback = record['feedback'][0]
    if mutation == 'nonadjacent_pair':
        feedback.update(scope='pair', pair=['t0', 't2'], disposition='avoid')
        feedback['track_bindings'] = [feedback['track_bindings'][0], feedback['track_bindings'][2]]
    elif mutation == 'reversed_track_ids':
        feedback['track_ids'].reverse()
    elif mutation == 'wrong_binding':
        feedback['track_bindings'][0]['audio_sha256'] = '0' * 64
    elif mutation == 'unknown_scope':
        feedback['scope'] = 'anything'
    elif mutation == 'unknown_disposition':
        feedback['disposition'] = 'anything'
    elif mutation == 'route_with_pair':
        feedback['pair'] = ['t0', 't1']
    else:
        del feedback['track_bindings'][0]['audio_sha256']
    feedback['feedback_id'] = 'feedback-' + weave._digest_json(
        {key: value for key, value in feedback.items() if key != 'feedback_id'})[1][:20]
    forged = weave._write_manifest(record, str(tmp_path / 'forged-target'), 'set-plan.json',
                                   'pocket.set-plan-handle/v1')
    with pytest.raises(PocketError, match='Legacy feedback'):
        weave.replan_set(forged, str(tmp_path / 'refused'), seed=2, route_count=1)
    assert not (tmp_path / 'refused').exists()


def test_genuine_legacy_adjacent_pair_preserved(bag, tmp_path, monkeypatch):
    old, _ = legacy_feedback(bag, tmp_path, monkeypatch)
    feedback = weave.record_plan_feedback(old['handle'], old['routes'][0]['route_id'],
                                         'prefer', str(tmp_path / 'pair-feedback'),
                                         from_track_id='t0', to_track_id='t1')
    result = weave.replan_set(feedback['handle'], str(tmp_path / 'pair-replan'), seed=2, route_count=1)
    assert result['summary']['feedback_applied_count'] == 1
