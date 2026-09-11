"""Real provider integration, enabled when the Whisker slice is installed."""
from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pytest

pytest.importorskip('pocket_music.whisker')

from pocket_music.whisker import ON_DECK_VERSION
from pocket_music.record_bag import create_record_bag
from pocket_music.weave import load_set_plan, plan_set_routes, record_plan_feedback, replan_set


def tracks():
    return [{'track_id': f't{i}', 'title': f'Generated record {i}', 'artists': ['Generated artist'],
             'duration_seconds': 270 + i * 10,
             'profile': {'energy': i / 11, 'bpm': 110 + i * 2, 'tags': ['percussion' if i % 2 else 'space'],
                         'provenance': 'agent_hypothesis'}} for i in range(12)]


@pytest.mark.parametrize('setting', ['warm_up', 'peak_time', 'after_hours'])
def test_three_briefs_actual_provider_constraints_and_reproduction(tmp_path, setting):
    bag = create_record_bag(tracks(), str(tmp_path / 'bag'), 'Generated directions')['handle']
    brief = {'setting': setting, 'track_count': 6, 'anchor_track_ids': ['t1', 't8'],
             'excluded_track_ids': ['t10'], 'avoid_pairs': [['t3', 't4']],
             'intent': {'tags': None, 'creativity': .4, 'require_local_audio': None}}
    a = plan_set_routes(bag, brief, str(tmp_path / 'a'), seed=3)
    b = plan_set_routes(bag, brief, str(tmp_path / 'b'), seed=3)
    assert a['routes'] == b['routes']
    assert len({tuple(r['track_ids']) for r in a['routes']}) == 3
    provenance = load_set_plan(a['handle'])['provenance']
    assert provenance['ranking_version'] == ON_DECK_VERSION
    assert provenance['ranking_provider'] == 'on_deck.rank_next_tracks'
    for route in a['routes']:
        ids = route['track_ids']
        assert ids.index('t1') < ids.index('t8') and 't10' not in ids
        assert ('t3', 't4') not in list(pairwise(ids))
        assert len(route['transitions']) == 5
        assert all(t['status'] == 'proposal_not_auditioned' and t['reasons'] for t in route['transitions'])
    route = a['routes'][0]
    left, right = route['track_ids'][:2]
    event = record_plan_feedback(a['handle'], route['route_id'], 'avoid', str(tmp_path / 'feedback'),
                                 from_track_id=left, to_track_id=right)
    revision = replan_set(event['handle'], str(tmp_path / 'revision'), seed=3)
    assert revision['summary']['feedback_applied_count'] == 1
    assert all((left, right) not in list(pairwise(r['track_ids'])) for r in revision['routes'])


def test_unknown_profiles_remain_unknown_across_providers(tmp_path):
    entries = [{'track_id': f'u{i}', 'title': f'Unknown {i}', 'artists': [], 'available': None,
                'profile': {'provenance': 'user', 'bpm': None, 'energy': None}} for i in range(4)]
    bag = create_record_bag(entries, str(tmp_path / 'bag'), 'Unknowns')['handle']
    result = plan_set_routes(bag, {'track_count': 4}, str(tmp_path / 'plan'))
    for route in result['routes']:
        assert route['duration']['unknown_full_track_count'] == 4
        assert all(t['unknowns'] and t['tempo_options'] == [] for t in route['transitions'])


@pytest.mark.parametrize('setting,expected', [
    ('warm_up', [.22, .285, .35, .425, .5]),
    ('peak_time', [.62, .85, .85, .85, .72]),
    ('after_hours', [.55, .475, .4, .325, .25]),
])
def test_slot_contours_are_explicit_position_intent_not_imputed_measurements(tmp_path, setting, expected):
    bag = create_record_bag(tracks(), str(tmp_path / 'bag'), 'Generated contour')['handle']
    before = Path(bag['path']).read_bytes()
    result = plan_set_routes(bag, {'track_count': 5, 'setting': setting}, str(tmp_path / 'plan'), route_count=1)
    route = result['routes'][0]
    assert [p['target_energy'] for p in route['position_targets']] == expected
    assert all(p['basis'] == 'pocket_slot_planning_preset' for p in route['position_targets'])
    assert [t['selection_intent']['target_energy'] for t in route['transitions']] == expected[1:]
    assert [t['planning_target']['target_energy'] for t in route['transitions']] == expected[1:]
    assert Path(bag['path']).read_bytes() == before
    override = plan_set_routes(bag, {'track_count': 5, 'setting': setting, 'intent': {'target_energy': .6}},
                               str(tmp_path / 'override'), route_count=1)['routes'][0]
    assert all(p['target_energy'] == .6 and p['basis'] == 'caller_explicit_target' for p in override['position_targets'])


def test_annotation_baseline_tracks_contour_and_zero_creativity_is_deterministic(tmp_path):
    rows = [{'track_id': f'e{i:02d}', 'title': f'Generated energy {i}', 'artists': [],
             'profile': {'energy': i / 20, 'provenance': 'user'}} for i in range(21)]
    bag = create_record_bag(rows, str(tmp_path / 'bag'), 'Generated gradient')['handle']
    brief = {'setting': 'warm_up', 'track_count': 5, 'intent': {'creativity': 0}}
    a = plan_set_routes(bag, brief, str(tmp_path / 'a'), seed=1)
    b = plan_set_routes(bag, brief, str(tmp_path / 'b'), seed=999)
    assert [r['track_ids'] for r in a['routes']] == [r['track_ids'] for r in b['routes']]
    route = a['routes'][0]
    assert route['origin'] == 'annotation_baseline'
    energy = {t['track_id']: t['profile']['energy'] for t in rows}
    selected = [energy[key] for key in route['track_ids']]
    targets = [p['target_energy'] for p in route['position_targets']]
    forward = sum(abs(x - y) for x, y in zip(selected, targets))
    reverse = sum(abs(x - y) for x, y in zip(reversed(selected), targets))
    assert selected[0] < selected[-1] and forward < reverse / 2
    assert len({tuple(r['track_ids']) for r in a['routes']}) == 3
    assert all(s['score_perturbation_scale'] == 0 for s in load_set_plan(a['handle'])['provenance']['searches'])


def test_baseline_stays_fixed_while_exploratory_seeds_change_close_alternatives(tmp_path):
    rows = [{'track_id': f'e{i:02d}', 'title': f'Generated near-neighbor {i}', 'artists': [],
             'profile': {'energy': .30 + i * .003, 'provenance': 'user'}} for i in range(20)]
    bag = create_record_bag(rows, str(tmp_path / 'bag'), 'Generated close scores')['handle']
    brief = {'setting': 'warm_up', 'track_count': 5, 'intent': {'creativity': .6}}
    a = plan_set_routes(bag, brief, str(tmp_path / 'a'), seed=1)
    b = plan_set_routes(bag, brief, str(tmp_path / 'b'), seed=29)
    assert a['routes'][0]['track_ids'] == b['routes'][0]['track_ids']
    assert [r['track_ids'] for r in a['routes'][1:]] != [r['track_ids'] for r in b['routes'][1:]]
    assert load_set_plan(a['handle'])['provenance']['searches'][1]['score_perturbation_scale'] < .02
