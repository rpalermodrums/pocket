# Hear an audio-score timing change

Render a short overlap, move a named pickup with its linked response, and hear
the unchanged and revised versions in the same time window. Pocket keeps the
original score, exact source passages, processing recipe and listening reports
separate. It does not decide which landing sounds right.

This guide extends the [audio passage score](audio-score.md). An
[occurrence](concepts.md#time-has-more-than-one-clock) is one placement of one
source passage. Repeated passages have different occurrence IDs even when they
use the same source frames.

## Run a generated example

From the checkout, with Pocket's dependencies installed:

```sh
PYTHONPATH=src python examples/audio_score_audition.py private/audio-score-example
```

The [runnable example](../examples/audio_score_audition.py) refuses an existing
destination. It generates a backing, linked call/response and an unrelated
repeat, then verifies the exact 6,000-frame correction against an independent
PCM oracle. It writes `comparison.json`, browser preview receipts, an attributed
agent report, `verification.json`, and `review-spec.json`. Run the command in
`review-command.txt` to open the existing practice review. The script plays
nothing and records no human listening or musical decision.

## Prepare a bounded overlap

`audio_score_render` takes an exact score, the timeline clock to render, a
quarter-note interval, and an explicit amplitude recipe. It uses only the
score's member occurrences on that timeline. A member shared by two semantic
passages sounds once, not twice.

The `same-rate-overlay-linear/v1` profile supports matching original sample
rates and mono/stereo layouts, constant tempo, and placements that require no
time stretch. It refuses fractional sample placement. It does not resample,
convert channels, infer beat one, add effects, normalize, limit or clamp.

For example, a recipe for a backing and two linked voice occurrences is:

```json
{
  "profile": "same-rate-overlay-linear/v1",
  "controls": [
    {"occurrence_id": "backing", "gain": {"n": 1, "d": 2}, "points": []},
    {"occurrence_id": "call", "gain": {"n": 1, "d": 1}, "points": []},
    {"occurrence_id": "response", "gain": {"n": 1, "d": 1}, "points": []}
  ]
}
```

Supply one control for **every score member occurrence on this timeline**, even
those outside the chosen window. This keeps the same recipe meaningful when a
correction moves an occurrence across a window edge. IDs are unique in the
recipe; render summation follows the context's occurrence order.

`gain` is a reduced rational amplitude multiplier, not decibels. An empty
`points` list explicitly requests no envelope. Otherwise provide at least two
strictly ordered `{at_frame, gain}` points, starting at occurrence-local frame
zero and ending at its last frame, `N-1`. Pocket linearly interpolates between
those sample points and multiplies the result by the scalar gain. The points
move with the occurrence. Nothing is added at a crop or loop boundary.

Each scalar and point gain is bounded from zero through four; their product can
therefore reach sixteen. The DOUBLE WAV retains overloads for inspection.
Signal warnings do not authorize normalization. The existing browser PCM16
preview profile refuses values it cannot represent instead of clipping them.

Render bounds are 256 score occurrences/controls on the requested timeline,
32 active occurrences in the window, eight simultaneously sounding
occurrences, 32 points per envelope, 120 seconds and 64 MiB. All bounds and
placements are checked before mix allocation. The existing source capture
limits still apply; this profile does not acquire or prepare longer recordings.

## Correct one named audible anchor

`audio_score_audition` accepts the same score, window and recipe, plus:

- The semantic `passage_id` whose member occurrences move together.
- A named source-frame `onset` anchor and the exact `occurrence_id` containing it.
- The explicit `timeline_clock_id` and a nonzero, reduced rational `delta_qn`.
- Context field locks, attribution and the comparison question.

The chosen occurrence must belong to that passage, and every passage member
must use the named timeline. If the same source anchor occurs several times,
the occurrence ID identifies the intended appearance. Source frames and source
onset coordinates remain fixed; only the linked occurrences' timeline spans
move. Existing context locks protect unrelated placement or source fields.

In a quarter-note reference, one sixteenth later is:

```json
{"delta_qn": {"n": 1, "d": 4}, "timeline_clock_id": "set"}
```

At 120 BPM this is 0.125 seconds, or exactly 6,000 frames at 48 kHz. The tool
does not infer whether a musician is counting in double time. Declare the
intended displacement in the named timeline's quarter-note clock.

The fixed window must contain the named onset both before and after the shift.
Pocket preflights both versions, creates a literal context edit, revises the
score through that exact edit, then renders both versions with the same recipe
and window. It returns `score`, `context`, `edit`, `baseline`, `variant` and
`comparison` handles. The original score and media remain unchanged.

## Listen and retain a selection

Use the comparison with the existing [practice review](practice-review.md)
launcher, or call `practice_preview` and `practice_feedback` directly. The
review labels these files as processed overlays. Switching at the same output
frame means the same timeline position; the corrected source event deliberately
moves within it.

Feedback is bound to the exact compared render, preview where used, author and
reviewed interval. A `keep` report can be retrieved with
`practice_feedback_query`. Follow its render into `practice_query` to retain
the exact score and recipe; follow the score's parent/edit lineage to recover
the correction. A report on another render cannot select this candidate.

No new approval state is created. A human report remains an attributed human
report, an agent report remains an agent report, and playing a preview creates
neither. Selection of a short pickup does not approve a whole set.

## What is checked

On load, Pocket revalidates score/context/edit ancestry, window and source
maps, exact retained source bytes, amplitude controls and actual mixed samples.
A new hash on a forged manifest does not replace that reconstruction. A
comparison also requires the exact before/after scores, named onset, one
literal linked shift, common recipe and common window.

These checks establish what was rendered. They do not establish musical beat
one, stem isolation, harmonic compatibility, native playback, live scheduling
or a listener's preference. The existing exact-PCM practice profile keeps its
stricter no-overlap/no-mixing contract.
