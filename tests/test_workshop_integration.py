"""Real provider integration, enabled when the On Deck slice is installed."""
from __future__ import annotations

from itertools import pairwise

import pytest

pytest.importorskip('pocket_music.on_deck')

from pocket_music.record_bag import create_record_bag
from pocket_music.set_workshop import load_set_plan, plan_set_routes, record_plan_feedback, replan_set


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
    assert load_set_plan(a['handle'])['provenance']['ranking_version'] == '1.0.0'
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
