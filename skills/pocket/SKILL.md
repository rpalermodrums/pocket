---
name: pocket
description: Explore records, edit MIDI material, plan sounds, inspect Ableton sessions, compare exact passages, and preserve explicitly kept saved trials with scoped evidence.
---

# Pocket workflow

Use this skill for Peek, Thread, Stitch, Weave, Whisker, Baste and Pipette. Read the provider documentation when an operation is unfamiliar. The [selection interfaces](../../docs/selection-interfaces.md) document matching CLI/MCP calls; the [workspace](../../docs/workspace.md) gives a local human interface.

Pocket is a general music toolkit, including acoustic practice and composition.
Its post-v1 north star includes a responsive “living band in a box” practice partner;
a DAW, genre or fixed meter is not required for core musical context.

## Standalone musical context and practice

Use the [musical context guide](../../docs/musical-context.md) for explicit clocks,
internal anchors and repeated passages. Capture an independently sourced exact
audio region, declare a time map with `musical_time`, then call `context_create`.
Keep original source frames distinct from crop-local frames and timeline positions.
Anchors are attributed interpretations; bar one and phrase start are separate kinds.

Use `context_query` for bounded context pages and `context_resolve` with an exact
context handle plus clock identity. When a passage repeats, supply its occurrence
ID; an ambiguity error requires a musical choice, not a guessed first occurrence.
The source's internal anchor need not be at the capture or passage start.

`practice_render` realizes explicitly ordered, contiguous occurrences at original
rate. It refuses implicit stretch, gaps, mixing, fades or channel conversion.
Compare baseline and alternatives with `practice_compare`; query revalidated
evidence with `practice_query`. Record actual attributed reports using
`practice_feedback`, exact render handles and frame intervals. Agent reports stay
separate from human listening. Signal readiness never establishes musical approval.
The same public calls are available through CLI (`--spec`) and MCP.

Select retained evidence with `interpretation_create`, then bind that exact
interpretation through `context_bind_interpretation`. Keep abstention unresolved;
an onset is not automatically bar one. Use `context_edit` for explicit source
slips, timeline shifts and anchor rebindings, naming linked occurrences and locks.
Read the preservation receipt with `context_edit_query`. For child revisions use
`practice_compare_revisions` with exact edit lineage and full output occurrence
correspondence. Retrieve original, potentially contradictory reports through
`practice_feedback_query`; do not infer preferences from filtered notes.

Only when explicitly requested, `practice_envelope` applies a declared linear
fade-out/in at an actual occurrence join. It retains the exact baseline, timing
and input warnings. `practice_compare_processed` compares those derivatives;
5/15 ms example variants are technical probes, not approved musical defaults.
For a browser player, `practice_preview` creates a declared original-rate PCM16
copy of an exact render (nearest-even rounding, no dither; unrepresentable
samples are refused, never clamped). Its bytes are the player's input, not proof
of device output or listening.
See the [installed contracts guide](../../docs/contracts.md) for generated current
Python/CLI/MCP schemas and opt-in versioned errors. The design package's proposed
HTTP facade is not implemented.

The [synthetic demo](../../examples/practice_context.py) exercises the complete
file-only path. It does not qualify acoustic musical usefulness, native behavior
or real-time accompaniment. Discover installed operations through `capabilities_list`;
local development plans are not callable tool contracts.

For a real recording, the [reference exercise](../../examples/exercise_practice_recording.py)
uses explicit twenty-second captures and declared A/B boundary probes. Retain
abstention and competing pulse candidates; a nominal clock is not detected tempo.
Preserve decoded overs and mark signal readiness separately. The exercise records
agent technical reports only. Keep actual recordings and acceptance receipts
in the ignored `private/audio/` directory.

## Selection and improvisation

1. Establish the record bag from exact catalog identities or local assets. Preserve editions, duplicate appearances, unavailable entries and user changes. Spotify import is deterministic metadata transfer; independently identify acquired local audio before treating it as the same recording. Keep unknown musical fields absent or null, and attribute subjective profiles as `agent_hypothesis` or `user`.
2. Write a setting, duration and musical intent. Use MCP `weave`, CLI `weave plan`, or Python `pocket_music.weave.plan_set_routes` to retain a deterministic annotation baseline and creative alternatives. Relative-order anchors do not pin an opener, closer or timestamp. Positional energy contours are planning targets. Check duration shortfalls and actual phrase possibilities before calling a route viable.
3. Try alternatives. MCP `weave_feedback` (Python `record_plan_feedback`) binds an exact route or adjacent directed pair to its bag/brief; `weave_replan` (Python `replan_set`) preserves the previous attempt. Do not call a mechanical test or your own inference listener feedback. Compare passages using the existing transition workflow below.
4. For improvisation, prepare a session before performance with MCP `whisker_prepare` or Python `pocket_music.whisker.prepare_session`. Read bounded MCP `whisker` (Python `session_options`); preserve hold, lift and left-turn choices and their unknowns. Use `whisker_update` (Python `update_session`) with the observed revision and SHA. Refresh with `whisker_snapshot` (Python `session_snapshot`) after a stale-view error rather than silently overwriting the human's newer choice. CLI `whisker` offers the same prepare, snapshot, options and update actions. Manual choices update history; a new session is an explicit reset.
5. Optional local embeddings add coarse similarity, not beat-one, key, cue or compatibility proof. Verify the pinned model/cache and exact source frames, audio hashes and provenance. Build vectors before the live loop. Keep imported Spotify content out of the model path. The model pilot and its limitations are in [music embeddings](../../docs/music-embeddings.md).
6. When Spotify export is authorized, prepare a new playlist plan and verify actual order/privacy through the API or an honestly labeled UI observation. A planned export is not a created playlist. Reconcile uncertain writes before retrying. Tokens stay in the process environment.
7. When acquisition is authorized, use yt-dlp's best available audio by default (`bestaudio/best`, the acquisition provider's existing policy). Inspect candidates and choose the recording explicitly; “best audio” chooses an encoding, not the right performance or edition. Retain original codecs and strict decode receipts. `inspect_source_formats` plus a newly sealed `format_id` plan supports a reviewed retry; a decoder error with exit zero still fails. No automatic normalization, fades or claim that a float WAV is a fidelity upgrade.

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

## MIDI layers and sound exploration

Read the [MIDI guide](../../docs/midi.md), [capability matrix](../../docs/midi-capabilities.md)
and [composable examples](../../examples/midi-workflow/README.md) for the requested operation.
For an audio-to-MIDI handover, state the musical question, source uncertainty and
listening requirements explicitly.
Discover current availability with `capabilities_list`; a closed native gate is not a callable tool.
Use public providers directly, through matching MCP names, or through hyphenated CLI names with `--spec`.

1. Establish the musical question, exact source and passage. Keep no addition available. Ask for a role or pitch when it matters; key estimates and note statistics do not choose it for the musician.
2. Import supplied material with `material_import` or propose explicit patterns with `midi_generate`. Neither requires Live, Serum, or a complete arrangement. Preserve source bytes, uncertain events and fidelity reports. A supplied valid material handle can replace generation at any point.
3. Query a bounded selection with `material_query`; make a specific revision with `midi_transform`. Use field locks for what should stay fixed. Review the semantic difference and `midi_export` loss report before native import. A refused projection requires another supported route or an explicit revision, never dropping expression silently.
4. Explore sounds independently with `instrument_inspect`, `preset_catalog` and `sound_plan`, or their Serum-specific providers. Supplied descriptors are observations with limited coverage. Plans do not load presets or write parameters. Native Serum operations remain gated separately from file planning.
5. For the supported stock layer, use `candidate_prepare` to copy the audio-only parent and dependencies. Coordinate the single native session, import in the isolated copy, verify clip boundaries and sound controls, save and reopen. Use `candidate_seal` only with actual attributed observations. Unknown source differences block sealing. The original audio-only Stitch v2 API retains its own restrictions.
6. Use `audition_plan` and `attach_candidate_render` for baseline and alternatives with identical context, gain and tail. Compare the exact renders with public Stitch comparison tools or an external player. Stop after four repeats or 90 seconds by default. Record actual listener statements through `audition_feedback`; playback or successful rendering never supplies a verdict.
7. An explicit keep may use `promote_candidate`, then `validate_candidate_promotion`. Reopen the relocated child before claiming native portability. Record an agent technical keep as such, separately from any human choice. Keep artifact integrity, native verification, render measurements, listening and decision as five distinct evidence layers.

Retain handles and completed receipts to resume without replaying mutations. Use public `request_status` with the same store and request ID to inspect a failed or interrupted operation; never manufacture a fresh success or steal a live lock. Revalidate external state through the original provider before continuing. This recipe supplies musical judgment and sequencing only. All tool calls use independently available public providers; native save, reopen and render remain supervised steps.

Use `midi_analyze` with explicit `voice_leading` or `role_overlap` when symbolic relationships help answer the question. Retain chord, overlap and tuning abstentions; gate overlap is not acoustic masking. For a requested deletion, later copy, grid adjustment or dynamics map, use the corresponding typed `midi_transform` operation. State the selection, timing policy and locks explicitly. Do not quantize automatically or implement note copying inside a recipe. Copies get new identities and preserve the existing controller timeline, so they may sound different at their new positions. Query a copied child before editing its new notes.

For file-based controller movement, create a clip-local curve with `curve_transform`, then pass its artifact directly to `midi_export` using an explicit `cc_step_bindings` entry with the destination clip, CC1 or CC11, and `same_tick_order:"before_existing"`. Keep the original material and curve unchanged; do not insert curves or recompute revisions privately in a recipe. Read the sidecar's ordering scope and loss report. This route encodes exact absolute 7-bit steps only; it does not move a native controller, initialize a receiver, encode MPE or establish listening. The ordinary-note native candidate profile still rejects controller-bearing material.

For native notes, coordinate the session and deliberately load the separate device built by `build_native_midi_device`. Call `native_midi_read` on an explicit arrangement target; candidate preparation is unnecessary for a read. Follow returned page inputs against the retained observation, and use a new request ID for a fresh native read. Optional saved binding verifies identity and primary mapping, not complete unsaved expression. `native_midi_status` inspects the retained journal without redispatch. The reader device has no native write route.

For the separately qualified ordinary-note fixture, use `build_native_midi_writer` and the same public `native_midi_write` from either CLI or MCP. Supply the exact copied workspace/revision, saved binding, current observation, device handle and explicit session supervision. The current profile allows one to three nonoverlapping notes in an empty primary clip, or one velocity change bound to its same-session insertion terminal. Review every other field as locked. Save, reread and reopen through the native host; remove the writer before sealing. A recipe must not bypass the provider guards or expand this profile.

If a native outcome is unknown, inspect `native_midi_status` with the returned recovery arguments. Never redispatch to discover whether it worked. Use `candidate_native_reconcile` only with a verified matching terminal and unchanged files. Otherwise, coordinate closure of the old session, unload the writer and close the disposable project without saving; capture a fresh observation of a different stopped project, then call `candidate_native_abandon` with explicit attributed closure. This preserves the unknown result permanently and requires a fresh candidate. A lost connection is not proof that native work stopped. Prepared cancellation remains unqualified and unavailable on the public surface.

For longer material, use `material_structure` to retain explicit phrase spans, motif references, returns and their uncertainty. Bind every node to the exact material revision and note membership; attribute relationship claims to their source. Query members, then obtain a fresh `material_query` selection before editing. Keep old structures with old revisions and create a new attributed structure when the material changes. The graph organizes declared musical ideas; it does not certify similarity or execute an arrangement.

For literal note construction, use `midi_transform` with `add` into an exact existing clip and a revision-bound selection (which may be empty). Use `split` or `merge` only with explicit retrigger/removal intent and compatible locks, source and expression coverage. New identities require a new query before another edit. For timing templates, use the explicit `groove` operation: it preserves deviations from nominal anchors while applying declared offsets. Do not infer a groove or force notes to a grid from a vague request. These file edits do not expand native writer support.

Use `material_sequence` for declared whole-clip canonical occurrences: state the destination clock, placements and total length, preserve full parent handles, and inspect the returned identity proof. Source clip origins do not establish shared timing. Controller/expression/native-bearing sources refuse this narrow route. Compose subsequent edits through a fresh material query; keep earlier occurrences and the unchanged source available.

For a supplied harmonic alternative, use `midi_transform` with strict `revoice` destinations for every selected original note and an attributed hypothesis. Keep competing readings and the unchanged parent. Pitch destinations preserve declared voices and all non-pitch fields; no automatic tuning conversion, voice leading, audible contour or receiver bend-range claim follows.

Use `midi_develop` when the musician or agent explicitly supplies a seed phrase, later occurrence placements and permissible endpoint choices. Retain literal A, constrained B, unchanged seed and no-addition. Its symbolic identity proof is not a judgment that a motif still feels right; listen through the surrounding transition before deciding. For sustain/tail questions, `midi_lifecycle` requires explicit initial pedal values, horizon, ordering and a declared tail. An unresolved hold or same-pitch ambiguity is a reason to qualify the interpretation; the analyzer never authorizes channel reuse, panic or native playback.

Use `midi_arrangement_develop` for several explicitly placed whole-clip sections from an attributed phrase graph. Bind the exact graph revision, preserve locked sections and choose bounded endpoint domains for later unlocked sections. Keep the full unchanged graph and sources, literal A, varied B and the silent destination. Query sections and sparse changes with `midi_arrangement_query`; paginate rather than dropping proof to accommodate a long set. These tools preserve supplied structure and rests. They do not infer an arrangement, prove perceptual motif identity or control native playback. The [long-span recipe](../../examples/midi-arrangement/README.md) is sparse synthetic evidence, not musical acceptance.


For per-note pitch, pressure or slide, preserve the supplied rich material and use `midi_expression_plan` with explicit lifecycle, receiver assumptions and error tolerances. The tool allocates channels through declared sustain/tails, refuses exhaustion, and retains exact quantization/reset proof. `midi_export(expression=...)` consumes the same configuration or plan handle for a single-clip step realization. Read the file sidecar and retain the source. A declared bend range or zone is not an observed receiver configuration; no setup, native preservation, audible independence or transport recovery is established. Keep linear/performed gestures separate until a qualified route exists. The [expression recipe](../../examples/midi-expression/README.md) is synthetic file evidence only.

For audio-derived suggestions, identify exact local source bytes and frames before `audio_hypotheses`. Keep the full-set context separately; a bounded analysis crop cannot decide fit across a set. Inspect competing pulse/attack evidence and abstentions before proposing anything. Use `audio_hypothesis_correct` for explicitly attributed alternatives supported by retained evidence; never relabel an authored pitch or phrase as detector output. Retain originals and superseded interpretations. `audio_hypothesis_query` provides bounded revision-bound pages. No automatic kick, instrument, note, harmony or musical-approval inference is available.

For a passage in a long recording, use `audio_region_capture` then `audio_region_hypotheses`, or supply the explicit source inline to the latter. Confirm the supported PCM profile and exact frame range. The full original stays externally retained; Pocket hashes it during capture and stores only exact selected PCM and the original identity/mapping. `audio_region_query` keeps crop-local evidence beside original-source coordinates. Preserve gaps and competing interpretations; joining passage reports does not establish full-set coverage.

Use `audio_region_correct` for an attributed alternative in explicitly declared local or original coordinates. Support pointers still identify the local retained evidence. Preserve uncertain floating pulse estimates separately from exact crop offsets. Use `audio_region_submit` when a long capture needs an owned cancellable worker; its durable job precedes full-file hashing. Read fresh status and distinguish cancellation requested from acknowledged. The [synthetic hour recipe](../../examples/audio-regions/README.md) demonstrates three explicit passages; real musical acceptance still needs the musician's exact source, passages and task intent.

For optional learned pulse evidence, use an explicit local model declaration with `audio_model_inspect` or supply the inline declaration directly to `audio_pulse_hypotheses`. Synthetic qualification establishes the bounded execution profile, not accuracy. Inspect retained raw evidence, fractional source positions and excluded boundaries before proposing timing changes; preserve false or competing hypotheses. Retained model queries and authored corrections use the existing hypothesis tools without rerunning a model. Never install or download a model implicitly.

Use `audition_feedback_query` with explicit feedback handles when recalling earlier decisions. Match exact render bytes and frame intervals, retain the actor and evidence kind, and keep agent reports separate from human listening. An unchanged or no-addition decision remains valid; retrieval never creates approval or a listener preference.

Use `audio_pulse_submit` for the optional learned primitive or `audio_hypothesis_submit` for deterministic analysis only when an owned local worker is useful; synchronous analysis remains independently callable. Inspect fresh state with `job_status`; submit replay is a historical receipt. `job_cancel` needs the exact current revision. Pending cancellation is not acknowledgement, and a held execution lease is not proof of progress. Cancelled/failed/interrupted work has no committed result and must not be retried automatically. Staging artifacts are diagnostics. No worker controls the native host or establishes live-performance readiness. See the [audio recipe](../../examples/audio-hypotheses/README.md).

### Explicit audio interpretation to MIDI timing

Use public `midi_timing_alternatives` only after the caller has explicitly matched
selected note IDs to active authored attack/pulse corrections. Keep competing
interpretations and the unchanged music available. Declare the original recording
SHA, source frames, unwarped host alignment and musical-time map; do not infer warp,
latency, a pulse lattice, kick identity or masking improvement. Pulse points are
caller-authored rational choices, separate from retained floating model estimates.

Supply all selected-note matches in their intended order, exact strength/maximum
shift and locks. The provider composes the existing public editor and may refuse
an intermediate overlap. Do not bypass that refusal with a private editor or
weakened preservation policy. `unchanged` and `no_addition` both mean keeping the
original music. Query the retained proof with `midi_timing_query`; readback uses
temporary isolated public-edit replay and leaves the caller's store unchanged.
The profile supports at most 16 ordinary notes and two alternatives, with stricter
combined evidence budgets described in `docs/midi.md`. Broader ancestry may need a
smaller request. The synthetic recipe is `examples/midi-audio-timing/example.py`.

For floating-point source recordings, use the qualified exact FLOAT32 passage profile rather than silently converting to integer PCM. Selected finite samples retain their original bits; unsupported headers or selected NaN/infinity refuse. Capture support does not expand any analyzer’s separately qualified input profile.

When uncertain audio-derived pitch would answer a concrete musical question, use the separately optional `audio_note_model_inspect`/`audio_note_hypotheses` providers with explicit existing runtime and known-weight hashes. Do not install or fetch a model implicitly. Its fixed CPU profile accepts narrow PCM16/FLOAT32 mono/stereo source crops; arithmetic downmix can cancel opposite-phase material. Preserve raw windows, alternative note hypotheses, timing estimates, pitch-bin/tuning assumptions and the choice to abstain. Model amplitude is not MIDI velocity, and contour output is not MPE. Use ordinary `audio_hypothesis_query`/`audio_hypothesis_correct` or `audio_region_hypotheses` with `learned_notes` and exact crop mapping. Query/correction need only retained artifacts. Explicitly author corrections before treating model pitches or attacks as musical decisions. The built-in silence/tone qualification and technical model execution do not establish source harmony, bass ownership, actual human listening or E8 acceptance. Optional `audio_note_submit` has owned cancellation; it is not a live scheduling path.
