---
name: pocket
description: Explore record bags, plan routes, inspect saved and open Ableton sessions, compare exact passages, and preserve explicitly kept saved trials with scoped evidence.
---

# Pocket workflow

Use this skill for Peek, Thread, Stitch, Weave, Whisker, Baste and Pipette. Read the provider documentation when an operation is unfamiliar. The [selection interfaces](../../docs/selection-interfaces.md) document matching CLI/MCP calls; the [workspace](../../docs/workspace.md) gives a local human interface.

## Selection and improvisation

1. Establish the record bag from exact catalog identities or local assets. Preserve editions, duplicate appearances, unavailable entries and user changes. Spotify import is deterministic metadata transfer; independently identify acquired local audio before treating it as the same recording. Keep unknown musical fields absent or null, and attribute subjective profiles as `agent_hypothesis` or `user`.
2. Write a setting, duration and musical intent. Use MCP `weave`, CLI `weave plan`, or Python `pocket_music.weave.plan_set_routes` to retain a deterministic annotation baseline and creative alternatives. Relative-order anchors do not pin an opener, closer or timestamp. Positional energy contours are planning targets. Check duration shortfalls and actual phrase possibilities before calling a route viable.
3. Try alternatives. MCP `weave_feedback` (Python `record_plan_feedback`) binds an exact route or adjacent directed pair to its bag/brief; `weave_replan` (Python `replan_set`) preserves the previous attempt. Do not call a mechanical test or your own inference listener feedback. Compare passages using the existing transition workflow below.
4. For improvisation, prepare a session before performance with MCP `whisker_prepare` or Python `pocket_music.whisker.prepare_session`. Read bounded MCP `whisker` (Python `session_options`); preserve hold, lift and left-turn choices and their unknowns. Use `whisker_update` (Python `update_session`) with the observed revision and SHA. Refresh with `whisker_snapshot` (Python `session_snapshot`) after a stale-view error rather than silently overwriting the human's newer choice. CLI `whisker` offers the same prepare, snapshot, options and update actions. Manual choices update history; a new session is an explicit reset.
5. Optional local embeddings add coarse similarity, not beat-one, key, cue or compatibility proof. Verify the pinned model/cache and exact source frames, audio hashes and provenance. Build vectors before the live loop. Keep imported Spotify content out of the model path. The model pilot and its limitations are in [music embeddings](../../docs/music-embeddings.md).
6. When Spotify export is authorized, prepare a new playlist plan and verify actual order/privacy through the API or an honestly labeled UI observation. A planned export is not a created playlist. Reconcile uncertain writes before retrying. Tokens stay in the process environment.
7. When acquisition is authorized, inspect candidates and choose a source explicitly. Retain original codecs and strict decode receipts. `inspect_source_formats` plus a newly sealed `format_id` plan supports a reviewed retry; a decoder error with exit zero still fails. No automatic normalization, fades or claim that a float WAV is a fidelity upgrade.

## Exact passages and transitions

1. Identify the exact saved project and recording version. Preserve the baseline. Start with MCP `thread` or CLI `thread`; retain the summary's immutable handle. Python `pocket_music.thread.inspect_set` still returns a full map; `pocket_music.thread_queries.inspect_set_summary` returns the bounded summary. Keep caches and full exports outside Git.
2. Use MCP `thread_find_clips` and `thread_region` for a timestamp and bounded duration; the corresponding Python functions remain `find_clips` and `query_set_region`. Inspect relevant controls, supported source mappings, omissions and pagination. Reinspect when a handle is stale; never silently reuse the old map. Export heavy raw state explicitly with `thread_export` when needed. Source frames, source seconds, arrangement seconds and arrangement beats are distinct clocks.
3. For each valid audio interval, pass its `analyze_region_frame_args` directly to MCP `peek` or Python `pocket_music.peek.analyze_region` with the exact source path. In the CLI use `peek --start-frame N --frames N`; do not convert back through decimal seconds or mix addressing modes. An unknown or out-of-bounds mapping must stay unknown until verified. Review competing acoustic phases, `crop_stability` and `phase_count_continuity` alongside tempo drift and local tonal evidence. Stable tempo or stable tested crops cannot establish musical beat one or an integer count. A score never authorizes a clip move.
4. Test a small musical hypothesis. Change one aspect where possible: media position, fine timing, tonal overlap or the phrase handoff approach. Name every additional change. Use identical monitoring gain unless level is explicitly the experiment.
5. Compare exact excerpts from the relevant renders with MCP `stitch`, Python `pocket_music.stitch.create_trial`, or CLI `stitch create`. No hidden fades, normalization, resampling or crop widening. Keep output and parent hashes with the source windows.
6. Record the listener's correction about the specific variant, interval and aspect with MCP `stitch_feedback` or Python `record_feedback`. Use MCP `stitch_feedback_list`, Python `query_feedback`, or CLI `stitch feedback-list --spec ...` to retrieve claims by exact output hash, local frame interval and scope. Preserve the original ranges, text and conflicting claims; an overlap match does not extend approval. Approval of bar position does not approve timing, harmony or the full mix.

MCP `stitch_prepare_native` (Python `prepare_native_trial`) prepares only its declared limited edit in a new destination. Version 2 collects active audio and supported Max files and records their hashes and reference rewrites. Run MCP `stitch_validate_native` or Python `validate_native_trial` after relocation. Filesystem readiness cannot prove Live loaded the media: explicitly inspect Live's missing/external-file state before exporting. Check exact range and settings, wait for completion, then use MCP `stitch_attach_render` (Python `attach_completed_render`) to attach the actual float WAV with a timestamped observation bound to the candidate hash. Never fabricate these observations. The provider labels them as operator reports.

`similar_phase_at_tested_midpoints` means only that the crop fits agreed at those instants. It does not check their boundary drift. Full Peek reports can be much larger than timestamp queries; store them locally and bring only the relevant fields into a decision.

Read artifact validity, signal disposition, native loading, export observation and `ready_to_compare` separately. Silent or near-silent expected music, non-finite samples and sample overload cannot become ready through a successful export alone. Intentional silence requires an explicit expectation and note at preparation. Readiness still carries no listening judgment. Do not overwrite an immutable candidate when Live wants to normalize it—save a separate file and preserve the chain of evidence. Re-preparing from a relocated source with stale absolute references currently requires deliberate relinking; old v1 native trials must remain preserved and be freshly prepared as v2.

Prefer existing local recordings. Follow the current task's authorization for optional model preparation, acquisition and playlist writes; these are not implicit in a read-only map request. General project editing and public sharing remain separate scopes. Keep generated outputs, recordings, personal notes, credentials and machine paths outside Git. Publishing the repository remains a separate owner decision.

## Open session observations and saved keep decisions

Use MCP `baste` or CLI `pocket baste` for the currently open, possibly unsaved Live
session. Build the real editable device with `baste_build_device` / `baste-device`
and deliberately load it with all adjacent scripts. Coordinate native use of Live
with other tasks first. See [Baste](../../docs/baste.md) for setup and failure
dispositions. Read observation timing and completeness; never reuse a runtime ID
as a durable handle, infer a saved revision, or call a failed/empty read success.
The observer has no Live mutation or save capability.

Use MCP `pipette` / CLI `pipette promote` only for an explicitly kept **saved**
Stitch v2 candidate and one selected usable render attachment. Supply exact trial,
candidate and attachment hashes, a new destination and the actual decision maker's
name, kind and reason. An agent's technical keep is not human listening approval.
Pipette preserves the parent/trial, collects the child, proves only its dependency
hints changed, seals lineage and returns a normal Thread handle. Use
`pipette_validate` / `pipette validate` after moving the whole project. Keep the
sealed baseline immutable and save later work separately. Native loading and
listening remain separate checks; mutable Baste observations cannot be promoted.
See [Pipette](../../docs/pipette.md) for rejection rules and evidence scope.
