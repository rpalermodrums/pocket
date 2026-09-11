# Weave

Weave explores several possible orders from a sealed [record bag](record-bag.md).
Every route is a musical hypothesis. It does not audition records, create an
Ableton arrangement or validate beat, phrase, key or audible transition quality.

CLI `pocket weave` retains the `plan`, `feedback` and `replan` actions. MCP exposes them as `weave`, `weave_feedback` and `weave_replan`; the Python function names below stay unchanged.

```python
from pocket_music.weave import plan_set_routes, record_plan_feedback, replan_set

plans = plan_set_routes(bag["handle"], {
    "title": "An opening hour",
    "setting": "warm_up",
    "target_minutes": 60,
    "anchor_track_ids": ["record-a", "record-f"],
    "excluded_track_ids": ["record-z"],
    "intent": {"direction": "explore", "creativity": 0.5},
}, "new-plan-directory", seed=12, route_count=3)
route = plans["routes"][0]
feedback = record_plan_feedback(
    plans["handle"], route["route_id"], "avoid", "new-feedback-directory",
    from_track_id=route["track_ids"][0], to_track_id=route["track_ids"][1],
    note="Try another connection between these records.",
)
next_plans = replan_set(feedback["handle"], "new-attempt-directory", seed=13)
```

The opening example assumes those IDs exist in the bag. The three initial brief
settings are `warm_up`, `peak_time` and `after_hours`; `open` is also supported.
These guide ranking against supplied annotations. They do not assign a measured
energy or genre to unknown recordings. Free-form intent belongs in attributed
profile tags, roles and notes; it is not silently converted into musical facts.

## Public contract

- `plan_set_routes(bag_handle, brief, output_dir, *, seed=0, route_count=3,
  parent_plan=None)` returns `handle`, `routes` and a compact `summary`.
- `load_set_plan(handle)` verifies the seal and the bag's local recording stamps.
- `record_plan_feedback(plan_handle, route_id, disposition, output_dir, *,
  from_track_id=None, to_track_id=None, note="")` saves a new feedback revision.
  Disposition is `prefer` or `avoid`; omit both track IDs for whole-route feedback.
- `replan_set(plan_handle, output_dir, *, seed, brief=None, route_count=3)` creates
  descendant alternatives without modifying the previous plan.

Handles use `pocket.set-plan-handle/v1`, an absolute manifest path and SHA-256.
Every destination directory must be new. Both manifest digest and adjacent seal
are verified. A plan also binds its bag revision; a changed or missing identified
local recording invalidates it. A different bag revision requires a fresh plan.
Keep manifests outside public source repositories because they contain library
metadata and local references.

Routes contain ordered `track_ids`, display `tracks`, detailed `transitions` and a
`duration` ledger. Transitions retain Whisker's reasons, unknowns, tempo options,
proposed treatment and evidence attribution. They are marked
`proposal_not_auditioned`. Their identity bindings contain the track ID, Spotify
URI and identified audio SHA when available; unknown audio identity stays null.

## Constraints and exploration

Anchors must occur in the supplied **relative order**. They are not fixed opening,
closing or time positions. Exclusions, unavailable entries, required local audio
and directed `avoid_pairs` remain hard constraints. A pair `["a", "b"]` forbids
only that adjacency in that direction. Contradictory constraints fail before any
plan is written. Each route contains unique tracks, with a maximum of 100.

The same bag, brief, seed, feedback history and provider version yield the same
route contents. Creation timestamps identify separate saved attempts. Different
seeds or intent can explore other orders. The first variant is a deterministic
annotation-fit baseline with constraint backtracking. Later variants use small,
score-scale perturbations (`0.004 + 0.035 × creativity²`); creativity zero removes
all stochastic perturbation and remains deterministic across seeds. Distinct
alternatives can still result from backtracking around an already-returned order.
The actual perturbation scale is saved with each search. This preserves ranking
support while exploring nearby alternatives; it is not a learned musical model. `explore` assigns hold, lift and
left-turn directions across variants; explicit directions are retained. Scores
are annotation-fit heuristics, not probabilities or audible approval.

Search uses a bounded 128-option frontier and at most 2,500 visited nodes per
variant. One to eight variants can be requested. If constraints or the search
bound permit fewer distinct routes, the result states that limit instead of
repeating an order. It does not prove exhaustive impossibility or global musical
optimality. If no valid route is found, it raises a clear error without a partial
published plan.

Default slot contours provide a transparent beginning, middle and ending. On the
0–1 intent scale, warm-up rises 0.22 → 0.35 → 0.50; peak time rises 0.62 → 0.85,
holds through the middle half, then releases to 0.72; after-hours tapers
0.55 → 0.40 → 0.25. Targets interpolate by route position, not clock time. A
single-record route uses the midpoint. `open` supplies no default contour, and an
explicit `intent.target_energy` overrides the entire contour. Every position and
transition records its target and basis. These are Pocket planning presets, not
measurements assigned to unknown tracks, promises of audible energy, or a check
that the selected order realizes the contour. Constraints and exploration can
still produce candidates with weak or missing supporting annotations.

## Duration and feedback

The ledger separates full recording duration from estimated performance time.
Local decoded frame counts take precedence over catalogue duration. Known lengths
use the requested `performance_fraction` (default 0.75), with a separately shown
estimated overlap (default 20 seconds, bounded by the two estimated performances).
Unknown lengths stay unknown in the full-track total; any planning allocation is
explicitly labeled unverified. `target_minutes` helps estimate a track count when
one is not supplied and reports the resulting deviation. It is not a guarantee
of a finished set length. No source sections or cue points are inferred.

Feedback binds the exact bag SHA, normalized brief SHA, route SHA and affected
recording identities. It applies only to descendant attempts with the same bag
and brief. Pair rejection changes that directed adjacency without banning either
record. Route rejection changes that exact order without excluding its tracks.
A preferred route may retain its order; a preferred pair receives a ranking bonus,
not a guaranteed position. The most recent feedback on the same scoped target
wins, while prior events remain in history. Changing the brief retains history
but does not silently transfer its preferences. Saved feedback does not constitute
a musical listening verdict unless the user's note explicitly supplies one.
