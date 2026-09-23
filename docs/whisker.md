# Whisker

> **In brief.** A mouse feels its way with its whiskers. Whisker helps you feel
> out the next record during a set. It offers a few candidates in distinct
> lanes (hold the energy, lift it, or take a left turn), each with its reasons
> and what it doesn't know. You and your agent share one session, and it
> refuses to let either of you overwrite a newer choice by accident.
>
> **You need** a [record bag](record-bag.md). Whisker never routes audio,
> controls decks or picks a cue for you.

Whisker returns a small set of next-record proposals and preserves a shared human/agent session. It routes no audio, controls no decks, and does not automatically choose a cue or certify a transition.

CLI `pocket whisker` retains the `prepare`, `snapshot`, `options` and `update` actions. MCP exposes `whisker_prepare`, `whisker_snapshot`, `whisker` (session options) and `whisker_update`. The Python function names below stay unchanged.

## Pure ranking

```python
from pocket_music.whisker import rank_next_tracks
options = rank_next_tracks(
    tracks, current_track_id=None, played_ids=[],
    intent={"setting": "warm_up", "direction": "explore"}, limit=6,
)
```

`tracks` are prepared record-bag dictionaries. `None` permits an opening choice. The current record, played records, explicitly unavailable records, avoided IDs and exact directed avoided pairs are excluded. `require_local_audio=True` requires `audio.status="identified"` and its source SHA; bag loading owns actual file/stamp validation. A filtered Weave frontier may contain fewer records than the played/avoid history. The current record must still be present when one is specified. `limit` is 1–128; live interfaces should normally request 3–6.

Ranking uses only attributed `profile` fields and eligible cached local embeddings. Titles, artists and Spotify catalog metadata are display/identity fields, not inferred acoustic properties. Missing/null energy, tempo or tags stay unknown. Nonempty profiles require `user`, `agent_hypothesis` or `measured` provenance to count as evidence. Energy/vocal-density are 0–1; BPM is 20–400. No key compatibility score is manufactured.

Every option has `track_id`, title/artists, a bounded heuristic `score`, a `lane` (hold/lift/left_turn), reasons, unknowns, `tempo_options`, `proposed_transition` and evidence components. Scores are neither probabilities nor listener verdicts. Missing evidence lowers the amount of support rather than becoming a perfect match. Where viable candidates exist, the shortlist includes distinct lanes; it never invents a lane or duplicates a record. Sort/tie-breaking is deterministic. An explicit target energy always wins. Without one, live `hold` targets the current annotated energy and `lift` targets current +0.15 (capped at 1). Setting baselines apply to opening/explore/left-turn choices or when current energy is unknown; unknown current energy is never invented. A direction or target changes the ranking; it does not remove human override.

Tempo options include bounded same-pulse or half/double-pulse clock hypotheses. A clock ratio does not verify musical pulse or bar one. A natural-tempo reset remains available when the declared stretch limit cannot accommodate the supplied clocks. Treatment suggestions are qualitative; no source beat or control envelope is automatically written.

## Shared session state

```python
from pocket_music.whisker import prepare_session, session_snapshot, session_options, update_session
state = prepare_session(bag_handle, "new-session", current_track_id="record-a", intent={"direction": "hold"})
options = session_options(state["session_dir"], expected_revision=state["revision"],
                          expected_sha256=state["sha256"])
state = update_session(state["session_dir"], expected_revision=state["revision"],
                       expected_sha256=state["sha256"], action="choose", track_id="record-b")
state = session_snapshot(state["session_dir"])
```

`prepare_session` requires a new output directory and a sealed record-bag handle. Optional `embedding_index` is a sealed handle from `music_embeddings.build_embedding_index`; setup copies and hashes its contents. No model inference occurs during preparation or live options.

Snapshots contain `session_dir`, `revision`, `sha256`, bag handle, current track, played/skipped IDs, intent and append-only event history. Each mutation creates a new immutable JSON revision linked to the preceding SHA. Readers validate the chain and bag. Writers require both expected revision and SHA and use an exclusive lock; a stale human or agent view receives an error and must reload. Concurrent writes cannot silently overwrite each other.

Actions are `choose` (track ID), `skip` (track ID), and `intent` (a replacement intent dictionary). Optional notes record explicit input, not inferred preferences. Skipped records leave automatic options; an explicit manual choice can restore one or replay a previously played record. Unavailable records cannot be chosen. Each event is session-specific and does not train a model or alter a bag or global preference.

The hot path imports no torch, performs no audio inference/decoding, and calls no network service. It reads bounded prepared data and verifies local artifacts. Core operation needs no model installation; absent embeddings produce explicit annotation fallback. Embedding mutation/hash mismatch is an integrity error, not a silent fallback.

`ON_DECK_VERSION` records the heuristic implementation version. Generated tests cover constraints, uncertainty, filtered frontiers, diversity, stale views, immutable history and concurrent writes. Machine-specific performance measurements and real recordings remain outside the repository.
