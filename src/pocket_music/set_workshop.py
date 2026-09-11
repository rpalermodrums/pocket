"""Reproducible exploratory set routes, not playback or musical approval."""
from __future__ import annotations

import math
import random
from copy import deepcopy
from datetime import UTC, datetime
from itertools import pairwise
from statistics import median
from typing import Literal

from .errors import PocketError
from .record_bag import (
    _digest_json,
    _load_manifest,
    _number,
    _strings,
    _text,
    _write_manifest,
    load_record_bag,
)
from .selection_types import BagHandle, PlanHandle, SetBrief

WORKSHOP_VERSION = '1.0.0'
SETTINGS = frozenset({'warm_up', 'peak_time', 'after_hours', 'open'})
DIRECTIONS = frozenset({'hold', 'lift', 'left_turn', 'explore'})
INTENT_KEYS = frozenset({'setting', 'direction', 'target_energy', 'tags', 'creativity',
                         'max_stretch_percent', 'require_local_audio', 'avoid_track_ids', 'avoid_pairs'})
MAX_SEARCH_NODES = 2500


def _integer(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise PocketError(f'{label} must be an integer from {low} to {high}')
    return value


def _pairs(value, ids, label):
    if not isinstance(value, list) or len(value) > 10000:
        raise PocketError(f'{label} must be a list of directed ID pairs')
    result = []
    for pair in value:
        if (not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(p, str) for p in pair)
                or pair[0] == pair[1] or any(p not in ids for p in pair)):
            raise PocketError(f'{label} requires two distinct known track IDs in each directed pair')
        if pair not in result:
            result.append(pair)
    return result


def _known_ids(value, ids, label):
    _strings(value, label, maximum=10000, item_maximum=200)
    if len(set(value)) != len(value) or any(v not in ids for v in value):
        raise PocketError(f'{label} contains duplicate or unknown track IDs')
    return list(value)


def _brief(brief, tracks):
    if not isinstance(brief, dict) or set(brief) - set(SetBrief.__annotations__):
        raise PocketError('Brief must use the shared SetBrief fields')
    result = deepcopy(brief)
    ids = {t['track_id'] for t in tracks}
    result.setdefault('title', 'Exploratory set')
    _text(result['title'], 'brief title', 200)
    intent = result.setdefault('intent', {})
    if not isinstance(intent, dict) or set(intent) - INTENT_KEYS:
        raise PocketError('Unsupported selection intent fields')
    result.setdefault('setting', intent.get('setting') or 'open')
    if result['setting'] not in SETTINGS:
        raise PocketError('Unsupported set setting')
    if intent.get('setting') is not None and intent['setting'] != result['setting']:
        raise PocketError('Brief and intent settings conflict')
    intent = {key: value for key, value in intent.items() if value is not None}
    result['intent'] = intent
    intent['setting'] = result['setting']
    intent.setdefault('direction', 'explore')
    if intent['direction'] not in DIRECTIONS:
        raise PocketError('Unsupported intent direction')
    for key in ('target_energy', 'creativity'):
        if intent.get(key) is not None:
            _number(intent[key], key, 0, 1)
    if intent.get('max_stretch_percent') is not None:
        _number(intent['max_stretch_percent'], 'max_stretch_percent', 0, 50)
    if intent.get('tags') is not None:
        _strings(intent['tags'], 'intent tags', maximum=100)
    if intent.get('require_local_audio') is not None and not isinstance(intent['require_local_audio'], bool):
        raise PocketError('require_local_audio must be a boolean')
    anchors = _known_ids(result.setdefault('anchor_track_ids', []), ids, 'anchor_track_ids')
    excluded = _known_ids(result.setdefault('excluded_track_ids', []), ids, 'excluded_track_ids')
    avoided = _known_ids(intent.setdefault('avoid_track_ids', []), ids, 'avoid_track_ids')
    unavailable = {t['track_id'] for t in tracks if t.get('available') is False
                   or (intent.get('require_local_audio') and t.get('audio', {}).get('status') != 'identified')}
    blocked = set(excluded) | set(avoided) | unavailable
    if set(anchors) & blocked:
        raise PocketError('An anchor is excluded, unavailable or lacks required local audio')
    eligible = [t for t in tracks if t['track_id'] not in blocked]
    if not eligible:
        raise PocketError('No eligible tracks remain')
    pairs = _pairs(result.setdefault('avoid_pairs', []), ids, 'avoid_pairs')
    for pair in _pairs(intent.setdefault('avoid_pairs', []), ids, 'intent avoid_pairs'):
        if pair not in pairs:
            pairs.append(pair)
    result['avoid_pairs'] = pairs
    result.setdefault('performance_fraction', .75)
    result.setdefault('overlap_seconds', 20)
    _number(result['performance_fraction'], 'performance_fraction', .05, 1)
    _number(result['overlap_seconds'], 'overlap_seconds', 0, 300)
    if result.get('target_minutes') is not None:
        _number(result['target_minutes'], 'target_minutes', .1, 1440)
    if 'track_count' in result:
        count = _integer(result['track_count'], 'track_count', 1, 100)
    elif result.get('target_minutes') is not None:
        durations = [d for t in eligible if (d := _full_duration(t)[0]) is not None]
        typical = median(durations) * result['performance_fraction'] if durations else 240.0
        count = min(len(eligible), 100, max(len(anchors), 1,
                    math.ceil(result['target_minutes'] * 60 / max(30, typical - result['overlap_seconds']))))
    else:
        count = min(len(eligible), max(12, len(anchors)))
    _integer(count, 'track_count', 1, 100)
    if count < len(anchors) or count > len(eligible):
        raise PocketError('track_count cannot omit mandatory anchors or exceed eligible unique tracks')
    result['track_count'] = count
    return result, eligible


def _ranking_provider():
    try:
        from .on_deck import ON_DECK_VERSION, rank_next_tracks
    except ImportError as error:
        raise PocketError('Set Workshop requires the On Deck ranking provider to generate routes') from error
    return rank_next_tracks, ON_DECK_VERSION


def _full_duration(track):
    audio = track.get('audio', {}).get('identity')
    if audio:
        return audio['frames'] / audio['sample_rate'], 'identified_local_recording'
    return track.get('duration_seconds'), 'catalog_metadata' if track.get('duration_seconds') is not None else 'unknown'


def _durations(sequence, tracks, brief):
    fraction, overlap = brief['performance_fraction'], brief['overlap_seconds']
    known = [_full_duration(tracks[key])[0] for key in sequence]
    known_performances = [d * fraction for d in known if d is not None]
    # Unknown durations use an explicitly labelled planning allocation, never a
    # fabricated recording length or measured musical section.
    fallback = (brief['target_minutes'] * 60 + overlap * max(0, len(sequence) - 1)) / len(sequence) \
        if brief.get('target_minutes') is not None else (median(known_performances) if known_performances else 240.0)
    cards = []
    for key in sequence:
        full, basis = _full_duration(tracks[key])
        performance = full * fraction if full is not None else fallback
        cards.append({'track_id': key, 'full_track_seconds': full, 'full_track_duration_basis': basis,
                      'estimated_performance_seconds': performance,
                      'performance_basis': 'fraction_of_known_recording_duration' if full is not None else 'unverified_planning_allocation'})
    overlaps = [min(overlap, a['estimated_performance_seconds'] / 2, b['estimated_performance_seconds'] / 2)
                for a, b in pairwise(cards)]
    estimate = sum(c['estimated_performance_seconds'] for c in cards) - sum(overlaps)
    return {'tracks': cards, 'full_tracks_total_seconds': sum(known) if all(d is not None for d in known) else None,
            'known_full_track_seconds_subtotal': sum(d for d in known if d is not None),
            'unknown_full_track_count': sum(d is None for d in known),
            'estimated_performance_seconds': estimate, 'estimated_overlaps_seconds': overlaps,
            'target_seconds': brief['target_minutes'] * 60 if brief.get('target_minutes') is not None else None,
            'estimate_minus_target_seconds': estimate - brief['target_minutes'] * 60 if brief.get('target_minutes') is not None else None,
            'status': 'planning_estimate_not_arrangement_or_render',
            'limitation': 'No source sections, live blend lengths, fades or native timing have been validated.'}


def _binding(track):
    return {'track_id': track['track_id'], 'spotify_uri': track.get('spotify_uri'),
            'audio_sha256': track.get('audio', {}).get('identity', {}).get('sha256')}


def _feedback_effects(parent, bag_handle, brief):
    brief_sha = _digest_json(brief)[1]
    applicable = [f for f in (parent or {}).get('feedback', [])
                  if f['bag_sha256'] == bag_handle['sha256'] and f['brief_sha256'] == brief_sha]
    avoid_pairs, prefer_pairs, avoid_routes, prefer_routes = set(), set(), set(), []
    # The most recent feedback on the exact same scoped target wins; history
    # remains retained and applicability is explicit.
    latest = {}
    for f in applicable:
        target = (f['scope'], tuple(f['pair'] if f['scope'] == 'pair' else f['track_ids']))
        latest[target] = f
    for (scope, target), f in latest.items():
        if scope == 'pair':
            (avoid_pairs if f['disposition'] == 'avoid' else prefer_pairs).add(target)
        elif f['disposition'] == 'avoid':
            avoid_routes.add(target)
        else:
            prefer_routes.append(target)
    return avoid_pairs, prefer_pairs, avoid_routes, prefer_routes, list(latest.values())


def _sequence_valid(sequence, eligible, brief, forbidden):
    return (len(sequence) == brief['track_count'] and len(set(sequence)) == len(sequence)
            and set(sequence) <= set(eligible)
            and [key for key in sequence if key in brief['anchor_track_ids']] == brief['anchor_track_ids']
            and not any((a, b) in forbidden for a, b in pairwise(sequence)))


def _search(tracks, brief, intent, rng, forbidden, preferred_pairs, blocked_routes, rank):
    ids = {t['track_id'] for t in tracks}
    anchors, count = brief['anchor_track_ids'], brief['track_count']
    nodes = 0

    def search(prefix):
        nonlocal nodes
        nodes += 1
        if nodes > MAX_SEARCH_NODES:
            return None
        if len(prefix) == count:
            return tuple(prefix) if tuple(prefix) not in blocked_routes else None
        remaining_anchors = [key for key in anchors if key not in prefix]
        remaining = count - len(prefix)
        allowed = ids - set(prefix) - set(remaining_anchors[1:])
        if remaining == len(remaining_anchors):
            allowed &= {remaining_anchors[0]}
        current = prefix[-1] if prefix else None
        allowed = {key for key in allowed if (current, key) not in forbidden}
        if not allowed:
            return None
        ranking_tracks = [t for t in tracks if t['track_id'] in allowed or t['track_id'] == current]
        options = rank(ranking_tracks, current, played_ids=prefix, intent=intent, limit=min(128, len(allowed)))
        options = [o for o in options if o['track_id'] in allowed]
        # Keep a mandatory next anchor in the frontier even if the normal top-K
        # list omitted it. The provider still supplies its reasons and unknowns.
        if remaining_anchors and remaining_anchors[0] in allowed and not any(o['track_id'] == remaining_anchors[0] for o in options):
            anchor_tracks = [t for t in ranking_tracks if t['track_id'] in {current, remaining_anchors[0]}]
            options += rank(anchor_tracks, current, played_ids=prefix, intent=intent, limit=1)
        # Weighted sampling without replacement; seed and provider version are
        # preserved. Score is a heuristic annotation fit, never a probability.
        temperature = .15 + .55 * (intent.get('creativity') if intent.get('creativity') is not None else .5)
        ordered = []
        for option in options:
            score = option.get('score', 0)
            if not isinstance(score, (int, float)) or not math.isfinite(score):
                raise PocketError('Ranking provider returned a nonfinite score')
            bonus = .35 if (current, option['track_id']) in preferred_pairs else 0
            if remaining_anchors and option['track_id'] == remaining_anchors[0] and remaining <= len(remaining_anchors) * 3:
                bonus += .2
            key = (score + bonus) / temperature - math.log(-math.log(max(1e-12, min(1 - 1e-12, rng.random()))))
            ordered.append((key, option['track_id']))
        for _, key in sorted(ordered, reverse=True):
            if nodes >= MAX_SEARCH_NODES:
                break
            result = search([*prefix, key])
            if result is not None:
                return result
        return None

    return search([]), nodes


def _route(sequence, lookup, bag_handle, brief, intent, rank, index, seed, from_preference):
    transitions, played = [], []
    for a, b in pairwise(sequence):
        played.append(a)
        options = rank([lookup[a], lookup[b]], a, played_ids=played, intent=intent, limit=1)
        option = next((o for o in options if o['track_id'] == b), None)
        if option is None:
            raise PocketError('Provider rejected a planned transition; no plan was published')
        transitions.append({'from_track_id': a, 'to_track_id': b, 'from_identity': _binding(lookup[a]),
                            'to_identity': _binding(lookup[b]), 'lane': option.get('lane'),
                            'reasons': deepcopy(option.get('reasons', [])), 'unknowns': deepcopy(option.get('unknowns', [])),
                            'tempo_options': deepcopy(option.get('tempo_options', [])),
                            'proposal': deepcopy(option.get('proposed_transition')),
                            'evidence': deepcopy(option.get('evidence')), 'status': 'proposal_not_auditioned'})
    route = {'variant_index': index, 'track_ids': list(sequence), 'intent': intent,
             'tracks': [{'track_id': key, 'title': lookup[key]['title'], 'artists': lookup[key]['artists'],
                         'spotify_uri': lookup[key].get('spotify_uri')} for key in sequence],
             'transitions': transitions, 'duration': _durations(sequence, lookup, brief),
             'origin': 'preferred_ordering_retained' if from_preference else 'seeded_exploration',
             'seed': seed, 'status': 'musical_hypothesis_not_performance',
             'anchor_semantics': 'Must appear in the given relative order; no fixed time or position is implied.'}
    route['route_id'] = 'route-' + _digest_json({'bag': bag_handle['sha256'], 'brief': brief,
                                               'track_ids': sequence, 'intent': intent})[1][:20]
    route['route_sha256'] = _digest_json(route)[1]
    return route


def plan_set_routes(bag_handle: BagHandle, brief: SetBrief, output_dir: str, *, seed: int = 0,
                    route_count: int = 3, parent_plan: PlanHandle | None = None) -> dict:
    """Save distinct constrained routes, with explicit estimates and uncertainty."""
    _integer(seed, 'seed', 0, 2 ** 63 - 1)
    _integer(route_count, 'route_count', 1, 8)
    bag = load_record_bag(bag_handle)
    normalized, eligible_tracks = _brief(brief, bag['tracks'])
    parent = load_set_plan(parent_plan) if parent_plan is not None else None
    if parent and parent['bag']['sha256'] != bag_handle['sha256']:
        raise PocketError('Parent plan uses a different bag revision; feedback cannot silently cross identities')
    effects = _feedback_effects(parent, bag_handle, normalized)
    avoided, preferred, rejected, preferred_routes, applied = effects
    forbidden = {tuple(pair) for pair in normalized['avoid_pairs']} | avoided
    rank, ranking_version = _ranking_provider()
    lookup = {t['track_id']: t for t in eligible_tracks}
    routes, used, searches = [], set(rejected), []
    preferred_routes = [s for s in preferred_routes if _sequence_valid(s, lookup, normalized, forbidden) and s not in rejected]
    for index in range(route_count):
        intent = deepcopy(normalized['intent'])
        intent['avoid_pairs'] = [list(pair) for pair in sorted(forbidden)]
        if intent['direction'] == 'explore':
            intent['direction'] = ('hold', 'lift', 'left_turn')[index % 3]
        if preferred_routes:
            sequence = preferred_routes.pop(0)
            nodes, from_preference = 0, True
        else:
            rng = random.Random(f'{seed}:{index}:' + _digest_json(normalized)[1])
            sequence, nodes = _search(eligible_tracks, normalized, intent, rng, forbidden, preferred, used, rank)
            from_preference = False
        searches.append({'variant_index': index, 'visited_nodes': nodes, 'limit': MAX_SEARCH_NODES})
        if sequence is None:
            continue
        if not _sequence_valid(sequence, lookup, normalized, forbidden) or sequence in used:
            raise PocketError('Internal route constraint failure; no plan published')
        routes.append(_route(sequence, lookup, bag_handle, normalized, intent, rank, index, seed, from_preference))
        used.add(sequence)
    if not routes:
        raise PocketError('Bounded search found no route meeting all constraints; reduce count or revise anchors/pairs')
    manifest = {'schema': 'pocket.set-plan/v1', 'created_at': datetime.now(UTC).isoformat(),
                'bag': deepcopy(bag_handle), 'brief': normalized, 'seed': seed, 'routes': routes,
                'requested_route_count': route_count, 'returned_route_count': len(routes),
                'diversity': {'unique_orderings': len(routes), 'limited': len(routes) < route_count,
                              'reason': 'Bounded search could not find another distinct valid ordering' if len(routes) < route_count else None},
                'parent': deepcopy(parent_plan), 'feedback': deepcopy((parent or {}).get('feedback', [])),
                'feedback_applied_ids': [f['feedback_id'] for f in applied],
                'provenance': {'workshop_version': WORKSHOP_VERSION, 'ranking_provider': 'on_deck.rank_next_tracks',
                               'ranking_version': ranking_version, 'searches': searches},
                'status': 'exploratory_plans_only', 'audition': 'not_performed', 'audio_processing': 'none'}
    # Search may outlive a local file edit; never publish a freshly stale plan.
    load_record_bag(bag_handle)
    handle = _write_manifest(manifest, output_dir, 'set-plan.json', 'pocket.set-plan-handle/v1')
    return {'handle': handle, 'routes': routes, 'summary': {'requested': route_count, 'returned': len(routes),
            'diversity': manifest['diversity'], 'feedback_applied_count': len(applied), 'status': manifest['status']}}


def load_set_plan(handle: PlanHandle) -> dict:
    manifest = _load_manifest(handle, 'pocket.set-plan-handle/v1', 'pocket.set-plan/v1')
    load_record_bag(manifest['bag'])
    return manifest


def record_plan_feedback(plan_handle: PlanHandle, route_id: str,
                         disposition: Literal['prefer', 'avoid'], output_dir: str, *,
                         from_track_id: str | None = None, to_track_id: str | None = None,
                         note: str = '') -> dict:
    """Save feedback as a new plan revision, bound to the exact route or pair."""
    _text(route_id, 'route_id', 100)
    _text(note, 'feedback note', 4000, empty=True)
    if disposition not in ('prefer', 'avoid'):
        raise PocketError('disposition must be prefer or avoid')
    manifest = load_set_plan(plan_handle)
    route = next((r for r in manifest['routes'] if r['route_id'] == route_id), None)
    if route is None:
        raise PocketError('Feedback route does not occur in the referenced plan')
    paired = from_track_id is not None or to_track_id is not None
    pair = [from_track_id, to_track_id] if paired else None
    if paired and not any(t['from_track_id'] == from_track_id and t['to_track_id'] == to_track_id for t in route['transitions']):
        raise PocketError('Pair feedback must identify an exact adjacent directed transition in this route')
    bag = load_record_bag(manifest['bag'])
    tracks = {t['track_id']: t for t in bag['tracks']}
    binding_ids = pair if paired else route['track_ids']
    feedback = {'source_plan': deepcopy(plan_handle), 'route_id': route_id, 'route_sha256': route['route_sha256'],
                'bag_sha256': manifest['bag']['sha256'], 'brief_sha256': _digest_json(manifest['brief'])[1],
                'scope': 'pair' if paired else 'route', 'pair': pair, 'track_ids': route['track_ids'],
                'track_bindings': [_binding(tracks[key]) for key in binding_ids],
                'disposition': disposition, 'note': note, 'created_at': datetime.now(UTC).isoformat(),
                'applicability': 'descendant attempts with the same bag and brief; no global track preference'}
    feedback['feedback_id'] = 'feedback-' + _digest_json(feedback)[1][:20]
    revised = deepcopy(manifest)
    revised.update(parent=deepcopy(plan_handle), created_at=datetime.now(UTC).isoformat(),
                   feedback=[*manifest['feedback'], feedback])
    handle = _write_manifest(revised, output_dir, 'set-plan.json', 'pocket.set-plan-handle/v1')
    return {'handle': handle, 'feedback': feedback, 'feedback_count': len(revised['feedback'])}


def replan_set(plan_handle: PlanHandle, output_dir: str, *, seed: int,
               brief: SetBrief | None = None, route_count: int = 3) -> dict:
    parent = load_set_plan(plan_handle)
    return plan_set_routes(parent['bag'], brief if brief is not None else parent['brief'], output_dir,
                           seed=seed, route_count=route_count, parent_plan=plan_handle)
