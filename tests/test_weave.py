"""Generated route/feedback mechanics; scoring itself belongs to Whisker tests."""
from __future__ import annotations

import hashlib
from copy import deepcopy
from itertools import pairwise

import numpy as np
import pytest
import soundfile as sf

from pocket_music.errors import PocketError
from pocket_music.record_bag import create_record_bag, load_record_bag, revise_record_bag
from pocket_music.weave import load_set_plan, plan_set_routes, record_plan_feedback, replan_set


def records(count=10):
    return [{'track_id': f't{i}', 'title': f'Generated {i}', 'artists': ['Test artist'],
             'duration_seconds': 240 + i * 30,
             'profile': {'energy': i / max(1, count - 1), 'provenance': 'user'}} for i in range(count)]


def fake_rank(tracks, current_track_id=None, *, played_ids=(), intent=None, limit=6):
    intent = intent or {}
    forbidden = {tuple(p) for p in intent.get('avoid_pairs', [])}
    eligible = [t for t in tracks if t['track_id'] not in {*played_ids, current_track_id, *intent.get('avoid_track_ids', [])}
                and t.get('available') is not False and (current_track_id, t['track_id']) not in forbidden]
    target = intent.get('target_energy', {'warm_up': .25, 'peak_time': .85, 'after_hours': .45}.get(intent.get('setting'), .5))
    options = [{'track_id': t['track_id'], 'title': t['title'], 'artists': t['artists'],
                'score': 1 - abs((t.get('profile') or {}).get('energy', .5) - target),
                'lane': intent.get('direction', 'hold'), 'reasons': ['Generated annotation ranking'],
                'unknowns': ['No audition'], 'tempo_options': [],
                'proposed_transition': {'treatment': 'Keep as a planning hypothesis', 'basis': 'generated-test', 'status': 'proposal'},
                'evidence': {'profile_provenance': (t.get('profile') or {}).get('provenance')}} for t in eligible]
    return sorted(options, key=lambda o: (-o['score'], o['track_id']))[:limit]


@pytest.fixture(autouse=True)
def provider(monkeypatch):
    monkeypatch.setattr('pocket_music.weave._ranking_provider', lambda: (fake_rank, 'generated-test-v1'))


def make_bag(tmp_path, tracks=None):
    return create_record_bag(tracks or records(), str(tmp_path / 'bag'), 'Generated bag')['handle']


def sequences(result):
    return [r['track_ids'] for r in result['routes']]


def test_same_seed_reproducibility_and_real_variant_diversity(tmp_path):
    bag = make_bag(tmp_path)
    brief = {'setting': 'warm_up', 'track_count': 6, 'anchor_track_ids': ['t0', 't8'],
             'excluded_track_ids': ['t9'], 'avoid_pairs': [['t0', 't1']]}
    first = plan_set_routes(bag, brief, str(tmp_path / 'a'), seed=17)
    again = plan_set_routes(bag, brief, str(tmp_path / 'b'), seed=17)
    different = plan_set_routes(bag, brief, str(tmp_path / 'c'), seed=19)
    assert first['routes'] == again['routes']
    assert sequences(first) != sequences(different)
    assert len({tuple(s) for s in sequences(first)}) == 3
    for route in first['routes']:
        ids = route['track_ids']
        assert len(ids) == len(set(ids)) == 6
        assert ids.index('t0') < ids.index('t8') and 't9' not in ids
        assert ('t0', 't1') not in list(pairwise(ids))
        assert len(route['transitions']) == 5
        assert all(t['status'] == 'proposal_not_auditioned' for t in route['transitions'])


@pytest.mark.parametrize('brief', [
    {'anchor_track_ids': ['missing']}, {'anchor_track_ids': ['t0', 't0']},
    {'anchor_track_ids': ['t0'], 'excluded_track_ids': ['t0']},
    {'track_count': 1, 'anchor_track_ids': ['t0', 't1']}, {'track_count': 11},
    {'avoid_pairs': [['t0']]}, {'avoid_pairs': [['t0', 'missing']]},
    {'performance_fraction': 1.1}, {'intent': {'target_energy': float('nan')}},
    {'setting': 'warm_up', 'intent': {'setting': 'peak_time'}},
])
def test_conflicts_fail_without_output(tmp_path, brief):
    with pytest.raises(PocketError):
        plan_set_routes(make_bag(tmp_path), brief, str(tmp_path / 'bad'))
    assert not (tmp_path / 'bad').exists()


def test_full_and_estimated_durations_are_separate_and_unknowns_not_invented(tmp_path):
    tracks = records(3)
    for t, duration in zip(tracks, (240, 300, 360)):
        t['duration_seconds'] = duration
    plan = plan_set_routes(make_bag(tmp_path, tracks), {'track_count': 3, 'performance_fraction': .5,
                           'overlap_seconds': 20}, str(tmp_path / 'plan'), route_count=1)
    duration = plan['routes'][0]['duration']
    assert duration['full_tracks_total_seconds'] == 900
    assert duration['estimated_performance_seconds'] == 410
    assert duration['status'] == 'planning_estimate_not_arrangement_or_render'
    for t in tracks:
        t.pop('duration_seconds')
    unknown_bag = create_record_bag(tracks, str(tmp_path / 'unknown-bag'), 'unknown')['handle']
    unknown = plan_set_routes(unknown_bag, {'track_count': 2, 'target_minutes': 10}, str(tmp_path / 'unknown'), route_count=1)
    duration = unknown['routes'][0]['duration']
    assert duration['full_tracks_total_seconds'] is None
    assert duration['estimated_performance_seconds'] == 600
    assert all(t['performance_basis'] == 'unverified_planning_allocation' for t in duration['tracks'])


def test_exact_pair_feedback_replans_without_globally_banning_either_record(tmp_path):
    bag = make_bag(tmp_path, records(5))
    brief = {'track_count': 5, 'setting': 'after_hours'}
    original = plan_set_routes(bag, brief, str(tmp_path / 'original'), seed=4, route_count=1)
    route = original['routes'][0]
    a, b = route['track_ids'][:2]
    old = load_set_plan(original['handle'])
    feedback = record_plan_feedback(original['handle'], route['route_id'], 'avoid', str(tmp_path / 'feedback'),
                                    from_track_id=a, to_track_id=b, note='Only this directed pair')
    assert feedback['feedback']['bag_sha256'] == bag['sha256']
    assert feedback['feedback']['route_sha256'] == route['route_sha256']
    assert load_set_plan(original['handle']) == old
    replanned = replan_set(feedback['handle'], str(tmp_path / 'next'), seed=4)
    assert replanned['summary']['feedback_applied_count'] == 1
    for ids in sequences(replanned):
        assert set(ids) == {t['track_id'] for t in records(5)}
        assert (a, b) not in list(pairwise(ids))
    # Other briefs do not silently inherit a global preference.
    changed = replan_set(feedback['handle'], str(tmp_path / 'changed'), seed=4,
                         brief={**brief, 'setting': 'peak_time'})
    assert changed['summary']['feedback_applied_count'] == 0


def test_route_feedback_scope_and_parent_identity(tmp_path):
    bag = make_bag(tmp_path, records(5))
    original = plan_set_routes(bag, {'track_count': 4}, str(tmp_path / 'plan'), seed=2)
    route = original['routes'][0]
    bad = record_plan_feedback(original['handle'], route['route_id'], 'avoid', str(tmp_path / 'avoid'))
    replanned = replan_set(bad['handle'], str(tmp_path / 'next'), seed=2)
    assert route['track_ids'] not in sequences(replanned)
    preferred = record_plan_feedback(original['handle'], route['route_id'], 'prefer', str(tmp_path / 'prefer'))
    kept = replan_set(preferred['handle'], str(tmp_path / 'kept'), seed=200)
    assert kept['routes'][0]['track_ids'] == route['track_ids']
    assert kept['routes'][0]['origin'] == 'preferred_ordering_retained'
    revised = revise_record_bag(bag, records(6), str(tmp_path / 'newbag'))
    with pytest.raises(PocketError, match='different bag revision'):
        plan_set_routes(revised['handle'], {'track_count': 4}, str(tmp_path / 'bad-parent'), parent_plan=bad['handle'])


def test_feedback_requires_actual_adjacent_pair_and_preserves_audio_binding(tmp_path):
    audio = tmp_path / 'generated.wav'
    sf.write(audio, np.zeros((8000, 2)), 8000, subtype='FLOAT')
    tracks = records(3)
    tracks[0]['local_path'] = str(audio)
    bag = make_bag(tmp_path, tracks)
    plan = plan_set_routes(bag, {'track_count': 3}, str(tmp_path / 'plan'), route_count=1)
    route = plan['routes'][0]
    ids = route['track_ids']
    with pytest.raises(PocketError, match='adjacent directed'):
        record_plan_feedback(plan['handle'], route['route_id'], 'avoid', str(tmp_path / 'bad'),
                             from_track_id=ids[0], to_track_id=ids[2])
    event = record_plan_feedback(plan['handle'], route['route_id'], 'prefer', str(tmp_path / 'feedback'))['feedback']
    binding = next(t for t in event['track_bindings'] if t['track_id'] == 't0')
    assert binding['audio_sha256'] == hashlib.sha256(audio.read_bytes()).hexdigest()
    assert load_record_bag(bag)['tracks'][0]['local_path'] == str(audio)


def test_limited_diversity_does_not_fabricate_duplicate_routes(tmp_path):
    bag = make_bag(tmp_path, records(2))
    plan = plan_set_routes(bag, {'track_count': 2, 'anchor_track_ids': ['t0', 't1']}, str(tmp_path / 'plan'))
    assert len(plan['routes']) == 1
    assert plan['summary']['diversity']['limited']
    with pytest.raises(PocketError, match='new immutable'):
        plan_set_routes(bag, {'track_count': 2}, str(tmp_path / 'plan'))


def test_unavailable_and_required_local_audio_constraints(tmp_path):
    tracks = records(3)
    tracks[0]['available'] = False
    bag = make_bag(tmp_path, tracks)
    plan = plan_set_routes(bag, {'track_count': 2}, str(tmp_path / 'plan'), route_count=1)
    assert 't0' not in plan['routes'][0]['track_ids']
    with pytest.raises(PocketError, match='No eligible tracks'):
        plan_set_routes(bag, {'intent': {'require_local_audio': True}}, str(tmp_path / 'no-local'))


def test_inputs_are_not_mutated(tmp_path):
    bag = make_bag(tmp_path)
    brief = {'track_count': 3, 'intent': {'avoid_pairs': [['t0', 't1']]}}
    before = deepcopy(brief)
    plan_set_routes(bag, brief, str(tmp_path / 'plan'))
    assert brief == before


def test_nested_setting_is_accepted_without_top_level_duplication(tmp_path):
    result = plan_set_routes(make_bag(tmp_path), {'track_count': 3, 'intent': {'setting': 'peak_time'}},
                             str(tmp_path / 'plan'), route_count=1)
    assert result['routes'][0]['intent']['setting'] == 'peak_time'


def test_no_route_fails_without_publishing_partial_plan(tmp_path):
    bag = make_bag(tmp_path, records(2))
    with pytest.raises(PocketError, match='no route'):
        plan_set_routes(bag, {'track_count': 2, 'avoid_pairs': [['t0', 't1'], ['t1', 't0']]},
                        str(tmp_path / 'plan'))
    assert not (tmp_path / 'plan').exists()


def test_local_source_change_invalidates_saved_plan_before_feedback(tmp_path):
    audio = tmp_path / 'source.wav'
    sf.write(audio, np.zeros((8000, 1)), 8000, subtype='FLOAT')
    tracks = records(2)
    tracks[0]['local_path'] = str(audio)
    plan = plan_set_routes(make_bag(tmp_path, tracks), {'track_count': 2},
                          str(tmp_path / 'plan'), route_count=1)
    sf.write(audio, np.ones((8000, 1)) * .1, 8000, subtype='FLOAT')
    with pytest.raises(PocketError, match='Stale local audio'):
        record_plan_feedback(plan['handle'], plan['routes'][0]['route_id'], 'prefer',
                             str(tmp_path / 'feedback'))
    assert not (tmp_path / 'feedback').exists()


def test_source_edited_during_search_is_rejected_before_publish(tmp_path, monkeypatch):
    audio = tmp_path / 'source.wav'
    sf.write(audio, np.zeros((8000, 1)), 8000, subtype='FLOAT')
    tracks = records(2)
    tracks[0]['local_path'] = str(audio)
    bag = make_bag(tmp_path, tracks)
    def edited_rank(*args, **kwargs):
        sf.write(audio, np.ones((8000, 1)) * .1, 8000, subtype='FLOAT')
        return fake_rank(*args, **kwargs)
    monkeypatch.setattr('pocket_music.weave._ranking_provider', lambda: (edited_rank, 'test'))
    with pytest.raises(PocketError, match='Stale local audio'):
        plan_set_routes(bag, {'track_count': 2}, str(tmp_path / 'plan'), route_count=1)
    assert not (tmp_path / 'plan').exists()
