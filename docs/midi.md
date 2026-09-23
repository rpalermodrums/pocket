# Musical material, curves and sound experiments

Pocket's symbolic tools work without Live, Serum, a renderer or a workflow runner. Each public Python provider is also a CLI command and an MCP tool. Recipes compose those same functions; there is no hidden selected material or conversation state.

This implementation provides offline MIDI material, deterministic alternatives, guarded edits, file curves, sound plans, and a supervised native-candidate evidence pipeline. One narrow synthetic stock-instrument workflow has been saved/reopened, rendered, technically promoted and reopened after relocation. General native MIDI fidelity failed specific probes, Serum is deferred, and no human musical approval is claimed. See the [capability matrix](midi-capabilities.md) and the [local development notes](README.md#local-development-material), which separates proposed music from completed delivery and listening evidence.

## Calling a capability

CLI commands use hyphens; MCP/public function names use underscores. Both receive the same named arguments and return the same parsed result. Every artifact publisher needs a unique `request_id` and an explicit local `store_root`. `preset_catalog(operation="query")` and other read-only queries need no request ID.

```sh
pocket capabilities-list --spec examples/midi-workflow/discovery.json
pocket midi-generate --spec examples/midi-workflow/generate.json
pocket material-import --spec examples/midi-workflow/external-import.json
pocket material-structure --spec examples/midi-workflow/structure-create.json
pocket sound-plan --spec examples/midi-workflow/stock-sound.json
pocket curve-transform --spec examples/midi-workflow/curve-create.json
pocket musical-time --spec examples/midi-workflow/time-create.json
```

`discovery.json` may contain `{"domain":"midi","limit":12}`. MCP calls the same tools with these JSON objects directly. The server entrypoint is `pocket-mcp`. The optional project `midi` extra supplies Mido for SMF input/output; generation, canonical material import, querying, editing, analysis and curves do not require it. The `agent` extra supplies MCP.

Each immutable handle contains:

```json
{
  "schema": "pocket.artifact-handle/v1",
  "artifact_uri": "artifacts/<actual-sha256>/record.json",
  "sha256": "<actual-sha256>",
  "artifact_schema": "pocket.material/v1"
}
```

Use complete handles returned by tools. The placeholders above are explanatory and cannot be called. Artifacts live under `artifacts/<sha256>/`; request journals live under `requests/<request_id>/`; mutable candidate copies live under `workspaces/<workspace_id>/`. A relocated store uses the same relative artifact handles. Retrying a completed request checks the same canonical inputs and output identities. Changed inputs conflict; interrupted/failed journals require inspection and a new request ID. A request lock is not a native DAW lease.

`request_status(store_root, request_id)` is a read-only public tool for that inspection. It checks journal identity and completed artifact integrity, reports retained state and lock presence, and never redispatches work or steals a lock. A completed journal does not revalidate an external project or output path; use the original provider's validation before resuming.

## Material capabilities

The table lists exact argument names; `store_root` is required on every row. `request_id` is required on all artifact-publishing rows, including sequence, development and lifecycle analysis.

| Provider | Required domain inputs | Optional inputs and defaults |
|---|---|---|
| `material_import` | `source` | Source kind `smf`, `material`, or `live_clip`; see below |
| `material_query` | `material` | `query="summary"`, `selection=None`, `limit=64`, `cursor=None`, `max_bytes=16000`, `comparison=None` |
| `material_sequence` | `request_id`, `definition` | Explicit whole-clip canonical construction; see below |
| `material_structure` | `operation` (`create` or `query`) | Create: `request_id`, `definition`; query: `structure`, `section="summary"`, `node_ids=None`, `limit=64`, `cursor=None`, `max_bytes=16000` |
| `midi_analyze` | `material` | `selection=None`, `analyses=None` (integrity/rhythm/pitch); explicit voice_leading/role_overlap |
| `midi_generate` | `brief` | `seeds=None`, `alternatives=2` |
| `midi_develop` | `request_id`, `definition` | Two bounded whole-clip alternatives; see below |
| `midi_arrangement_develop` | `request_id`, `definition` | Supplied graph sections, exact locks, A/B and silence; see below |
| `midi_arrangement_query` | `development` | `view`, `limit`, `cursor`, `max_bytes`; revalidated bounded section/change pages |
| `midi_lifecycle` | `request_id`, `material`, `clip_id`, `initial_state`, `horizon_qn`, `release_tail_qn`, `equal_time_order`, `source_basis` | Declared symbolic reservations only; see below |
| `midi_transform` | `material`, revision-bound `selection`, `operations` | `locks=None`, `expression_policy="reject"`, `overlap_policy="reject_new"` |
| `midi_export` | `material`, new `output_path` | `format="smf1"`, `ppq=None`, `loss_policy="reject_unapproved"`, `approved_losses=None`, `cc_step_bindings=None`, `expression=None` |

`material` accepts an immutable handle or a complete validated `pocket.material/v1` record. An SMF source is `{ "kind":"smf", "path":"phrase.mid", "expected_sha256":"<actual hash>" }`; an external record source is `{ "kind":"material", "material":{...} }`. A saved Live source uses `kind:"live_clip"`, `thread_handle` and an exact `clip_id` from Thread. Saved inspection is not proof of native reopening. Unknown/richer native note payloads remain opaque and restrict editing.

The `live-12.4.5-empty-note-metadata/v1` saved-reader profile recognizes only the observed empty note-expression/probability shape in Live 12.4.5 revision `225ce5e356e024356d5210512bae46fb466f6968`. It permits bounded conventional-note edits while retaining the complete original ALS, note tree, note IDs and allocation metadata as immutable evidence. Different builds, populated expression, unknown fields, malformed counters, significant metadata text, clip automation, groove or playback offsets remain opaque/noneditable. SMF export still requires explicit `opaque_native_note_payload` loss approval because the derivative does not carry all that native state.

The [external import example](../examples/midi-workflow/external-import.json) is complete, independently authored material. It can replace the generation step without calling `midi_generate`. Query its returned material handle, make a bound edit, analyze it or export it directly.

`material_query` supports `summary`, `events`, `voices`, and `diff` with a `comparison`. A query returns the resolved selection's note IDs, material revision and selection hash. Pass that whole `selection` to `midi_transform`; do not reconstruct a stale selection. Pagination cursors bind the exact material/query/selection. Large selections may be omitted to honor the byte budget; narrow the query before editing.

Material time is reduced rational quarter notes: `{"n":1,"d":3}`. Stored note onsets use `space:"clip_qn"`; clip origins distinguish phrase from arrangement coordinates. Velocity and release velocity use the declared MIDI1 7-bit domain. Acoustic loudness and audible release remain unknown until audio exists.

### Symbolic relationships

Request `analyses:["voice_leading","role_overlap"]` to measure relationships among selected notes. Each clip is analyzed separately in its own `clip_qn` coordinates. Matching origin numbers do not establish a common clock. These analyses use declared voice and role identities; they do not assign a melody, identify a kick or judge whether parts leave enough room for one another.

`voice_leading` measures successive unambiguous single-note attacks within each declared voice. Simultaneous chords, overlapping voice context and unresolved tuning produce explicit abstentions. Signed intervals and up/down/same contours use only the known `tuning:12tet-a440` reference plus the stored cents offsets. Custom tuning labels do not establish a pitch table. The evidence retains exact note identities, gate spans and gaps; pitch-expression curves and sounding pitch are outside this static measurement.

`role_overlap` compares distinct declared roles within a clip. It reports overlapping gate-pair counts and their summed duration separately from the shared active duration counted only once. Coincident attacks are another measurement. Gates are half-open: touching endpoints do not overlap. Null or empty roles remain unassigned. Muted notes remain included as symbolic records under an explicit policy; neither gate overlap nor mute status establishes masking or rendered sound.

The relationship profile accepts at most 512 selected notes. Evidence is bounded, omissions are explicit, and a response exceeding 16 KiB refuses with a request to narrow the selection. No report is secretly published by this read-only tool. Default integrity/rhythm/pitch analyses remain independently available.

### Phrase and motif relationships

`material_structure` creates an immutable `pocket.material-structure/v1` graph from explicit material revisions, phrase spans, note membership and attributed relationships. The [complete structure example](../examples/midi-workflow/structure-create.json) works independently with literal external material. Equivalent material handles produce the same artifact. No generation step or native session is required.

Each node identifies a material key/revision, clip, half-open rational `clip_qn` span and explicit note IDs. Membership is selected by onset; a note's release may cross the span boundary without being truncated. Relations express `sequence`, `repeat`, `variation`, `withhold`, `return`, `call_response`, `motif_reference` or `derivation`. Optional identity claims and their uncertainty belong to the named actor. They do not establish inferred similarity, human listening or musical approval. Derivation verifies declared material lineage. Self-links and directed cycles fail; a later return uses a distinct occurrence node.

Query `summary`, `nodes`, `relations` or `members` using the returned `artifacts.structure`. Member rows carry exact note identities; pass them to `material_query` on the same material revision to obtain a fresh edit selection. A structure bound to an earlier revision cannot silently retarget an edited material. Create a new structure and, when appropriate, retain `parent_structure`.

The profile allows up to 64 materials, 1,024 nodes, 4,096 relations and 16,384 memberships, with at most 32 parent artifacts. Queries allow at most 256 rows and 65,536 bytes including the cursor. Cursors bind the graph and filters. This supports explicit long-form organization and retrieval; it does not execute an arrangement or infer motif relationships.

### Cross-clip sequence construction

`material_sequence(store_root, request_id, definition)` places whole ordinary-note clips into one new material clip. The strict definition contains a `label`, `materials:[{key,material}]`, attributed declared `clock`, tagged `origin`, `length_qn`, and `occurrences:[{occurrence_id,material_key,material_revision,clip_id,at_qn}]`. The required policies are `controller_policy:"reject_present"`, `expression_policy:"reject_present"` and `overlap_policy:"reject_same_channel_pitch"`. Length and placements accept reduced rationals or integer shorthand; source records remain canonical.

Every occurrence has an explicit destination clip-local placement. Source origins are retained in the construction proof but replaced by those placements, never presumed to share a clock. Entire source clips, including trailing rests, must fit. Notes receive fresh deterministic IDs and occurrence-scoped voices; authored role labels, pitch, dynamics, gate and mute remain exact. Whole immutable parent materials remain referenced from the new material; multi-parent construction has no invented single `parent_revision`. Return handles are `artifacts.material` and `artifacts.sequence` (`pocket.material-sequence/v1`).

This initial profile accepts 1–32 source materials, 1–256 occurrences, up to 32,768 inspected source notes and 8,192 constructed notes. It refuses raw/native source provenance, note source bindings, events, curves, tempo/meter maps, unknown clip fields, gates outside source clips and all same-channel/same-pitch overlaps. Previously constructed sequence parents are accepted with their full retained lineage. No cropping, controller chase, expression flattening, tempo conversion, native arrangement execution or implicit gap filling occurs. The compact receipt links the full source-to-destination identity proof. A later `material_query`, edit or export consumes the same ordinary material handle.

### Deterministic proposals and exact edits

The generation brief requires `role`, `pitch`, `cell_qn`, `cell`, and `length_qn`. Optional `enter_qn`, `exit_qn`, `gate_qn`, `velocities`, `variation_qn`, `channel`, and `intentions` remain explicit. `seeds` separates structure/timing/velocity seed values. The current provider repeats an explicit cell and varies later final attacks within the supplied bound; it is not a model or a general composition engine.

The supplied 32-qn example uses a test pitch and cell, eight qn of initial silence, quarter-qn gates and no attacks at/after qn 28. It returns two frozen alternatives and a no-addition baseline. Those settings are synthetic examples, not a musician-approved pitch, key or judgment.

Edits support `shift(delta_qn)`, `transpose(semitones)`, `repitch(pitch)`, `velocity(value)`, `release_velocity(value)`, `resize(duration_qn)` and `thin(every, offset=0)`. Unselected notes, raw event evidence and curves are preserved. New same-channel/same-pitch overlaps are refused. `locks.selected_fields` names exact fields; aliases are `duration` for `duration_qn` and `expression_shape` for `expression_refs`. To preserve rhythm, lock both `onset` and `duration_qn`. Unknown locks fail. The edit artifact records parent, child, semantic differences, invariant results and the exact-parent restoration reference. There is no automatic host Undo or descendant-overwriting inverse operation.

Expression-sensitive repitching requires the explicit supported `preserve_relative` policy. Resizing expressed notes remains unsupported. Per-note curves must reciprocally bind the same note and clip occurrence. An arrangement controller lane is not silently moved when its notes move.

Additional selected-note operations use strict, explicit records:

| Operation | Required fields and behavior |
|---|---|
| `delete` | `controller_timeline:"preserve_existing"`. Removes the selected notes; any selected-field lock or note-owned expression refuses. Original wire events remain retained as source-only evidence, and export omits the deleted note lifecycle. |
| `duplicate` | Nonzero `delta_qn`, `controller_timeline:"preserve_existing"`. Copies ordinary selected notes within their existing clip bounds, retaining pitch, attack/release, duration, voice, role and mute. Copies receive deterministic new IDs, `derived_from` and no reused source binding. Note-owned expression and opaque/native note bindings refuse. |
| `grid_quantize` | `grid_qn`, `phase_qn`, `strength`, `threshold_qn`, `ties:"earlier"` or `"later"`, `note_off:"follow_onset"`, `controller_timeline:"preserve_existing"`, `time_space:"clip_qn"`. Moves an onset by the declared fraction of its nearest-grid displacement only when the original distance is within the inclusive threshold. |
| `velocity_map` | `field:"velocity"` or `"release_velocity"`, `mapping:[{"from_value":64,"to_value":72}]`, `unmapped:"preserve"` or `"reject"`. Applies exact integer lookup in the selected MIDI 7-bit domain; no interpolation, clipping or note-on-zero conversion. |

Grid, phase, strength and threshold use reduced rationals or integer shorthand. Grid must be positive, strength lies from zero to one, and threshold lies from zero to half a grid interval. Quantization preserves duration; note-offs move with attacks. Notes with expression require explicit `expression_policy:"preserve_relative"`, unowned note-relative curves within the gate, and a compatible fixed-gate policy (`stretch_with_gate`, `preserve_fraction` or `crop`). Clock-dependent `preserve_ms` is unqualified. It does not quantize a controller timeline, infer groove, stretch expression or widen the native timing profile. Choosing zero strength or a threshold that leaves an onset alone remains a valid outcome.

The resolved selection stays fixed throughout an operation list. A new duplicate is not implicitly selected by later operations; query the child to edit it. Locks refer to preexisting selected IDs, which duplication leaves exact. Copies have separate derivation guarantees. Controller events remain at their original times, so a copied note need not encounter the same controller state or produce the same sound. All operations retain the complete parent and exact differences in the edit artifact; compact receipts do not replace that evidence.

An ordinary SMF copy checks the retained original's hash and the declared on/off lifecycle references. It does not reparse the source to certify externally supplied provenance, and it does not require Mido. Current canonical note values may legitimately differ from their source after an earlier edit. The separate import/export capabilities perform their own codec and fidelity checks. Overlap validation rejects new overlapping note-ID pairs and has a bounded comparison budget; it does not repair or certify an already-overlapping pair.

### Literal construction, splitting and merging

`midi_transform` also accepts three strict construction operations. All require `controller_timeline:"preserve_existing"`; they leave source bytes, ordered controller events and curves intact. New notes stay outside the call's fixed original selection. Obtain a fresh selection on the child to edit them.

- `add` requires `clip_id`, `time_space:"clip_qn"` and 1–4,096 `notes`. Every literal supplies `onset_qn`, `duration_qn`, complete `pitch`, `velocity`, `release_velocity`, `channel`, `mute`, `voice_id` and `role_ref`. Notes receive deterministic identities with empty derivation, expression and source binding. They must fit the existing clip. A revision-bound empty selection (`note_ids:[]`) is valid; no generated seed is needed.
- `split` requires 1–64 increasing `offsets_qn` strictly inside every selected gate, `time_space:"note_relative_qn"` and `articulation:"retrigger"`. Each segment gets a fresh derived identity and copies the original attack/release values. This adds attacks/releases; it does not create ties or establish acoustic continuity.
- `merge` requires `time_space:"clip_qn"` and `articulation:"remove_retriggers"`. The 2–4,096 surviving selected notes must be exactly contiguous in one clip and agree in pitch, channel, voice, role, mute and both dynamics fields. The merged note has a fresh identity and chronologically ordered derivation. No gap filling or choice between conflicting dynamics is implicit.

Split/merge reject selected-field locks, note-owned expression and opaque/native source bindings. Ordinary SMF replacements verify retained source hashes and declared lifecycles, retaining the old wire events as source-only evidence. Their new IDs remain subject to the conservative new-overlap rule, including overlaps that a replaced original previously had. No source reparse, native execution or receiver behavior is implied. Complete diffs and restoration references remain in the edit artifact; replies remain bounded.

### Supplied groove timing

The strict `groove` operation takes `cycle_qn`, `anchors:[{nominal_qn,offset_qn}]`, `phase_qn`, `strength`, `threshold_qn`, `ties`, `note_off:"follow_onset"`, `controller_timeline:"preserve_existing"` and `time_space:"clip_qn"`. Use 1–256 increasing nominal anchors inside a positive cycle, with offsets no larger than half a cycle. The displaced anchors must remain strictly ordered through the cycle seam. Strength is 0–1 and the inclusive threshold is 0–half a cycle; all timing is exact rational arithmetic.

Each note chooses the nearest periodic nominal anchor, with explicit earlier/later ties. If it is within the threshold, its onset moves by `strength × anchor offset`. Its existing deviation from the nominal anchor is preserved. This is a supplied timing template, not inferred swing or automatic grid correction. Duration and controller timelines remain fixed; no clip extension or clipping occurs. Negative pickups remain canonical but may be refused by a later export. Note-relative expression uses the same explicit, qualified fixed-gate preservation policy as grid timing. The immutable proof retains the normalized template hash, exact per-note offsets and omissions from the threshold; no audible groove judgment is claimed.

### Explicit revoicing and harmonic alternatives

The strict `revoice` operation supplies `destinations:[{note_id,pitch}]` covering every surviving selected original ID exactly once, `controller_timeline:"preserve_existing"`, `pitch_expression:"preserve_relative"`, and a `hypothesis` with `label`, `actor`, `actor_kind` (`human` or `agent`), `statement` and `uncertainty`. Each destination is a complete pitch record (`midi_note`, `cents_offset`, `tuning_ref`). This records a supplied harmonic interpretation and exact voicing choices; it does not infer harmony or convert tuning systems.

Only pitches change. Declared voices, roles, timing, dynamics, controllers, source evidence and note identities remain exact, subject to the existing locks and overlap guard. Note-owned expression requires outer `expression_policy:"preserve_relative"` and qualified fixed-gate, note-relative curves. Per-note pitch curves additionally require additive cents or semitones; absolute or multiplicative pitch refuses. A preserved relative curve does not establish a receiver's bend range or audible contour. Use separate child edits to retain competing harmonic interpretations and retain the unchanged parent as a valid choice.

### Constrained motif development

`midi_develop` composes the public sequence and edit providers. Its strict `definition` contains `label`, `seed:{material,material_revision,clip_id}`, destination `clock`, `origin`, `length_qn`, 2–64 `occurrences:[{occurrence_id,at_qn}]`, `locked_occurrence_id`, `endpoint_note_id`, 1–32 `eligible_occurrence_ids`, `variation` and `seeds:{structure,pitch,timing}`. The first occurrence must be earliest and locked. The endpoint must be an explicitly identified latest-onset seed note; every eligible occurrence must be strictly later than the locked occurrence.

`variation` supplies unique `pitch_offsets_semitones` and exact `timing_offsets_qn` domains (1–16 choices each), `max_changed_notes` (1–32), `pitch_min` and `pitch_max`. The complete seed must satisfy the pitch range. Seeded content-hash ranking freezes a nonzero feasible endpoint choice in up to the declared maximum eligible occurrences. Gates must remain inside their original occurrence; existing public overlap checks still apply. An infeasible selected realization fails without rerolling or relaxing constraints. The ordinary canonical sequence profile applies, with a 1,024-note output and 4,096-qn destination limit. An additional 10,000 material-note-copy evidence budget bounds the full composed proof before execution.

The receipt retains literal repetition A, developed B, the exact unchanged seed and a silent no-addition destination. B preserves every noneligible endpoint and all non-pitch/non-onset fields. The `pocket.motif-development/v1` artifact contains complete occurrence/source derivation, symbolic rhythm/accent/interval proofs, choices and reproducible public steps; callers supply their own `store_root`. Interval identity abstains for unknown tuning; perceptual similarity and musical usefulness remain unassessed. Failed composition retains completed subordinate artifacts and request journals but advertises no valid alternative set. Retrying a completed call verifies immutable evidence; failed or interrupted calls require inspection and a new request ID.

### Explicit multi-section development

`midi_arrangement_develop` takes a supplied `material_structure` graph and places complete ordinary-note clips in a declared destination. Its strict `definition` contains `label`, `structure`, `expected_structure_revision` (the exact graph handle SHA256), `clock`, `origin`, `length_qn`, `sections`, `locked_section_ids`, `variations`, `seeds:{structure,pitch,timing}` and `attribution`. Attribution uses the graph contract's `actor`, `actor_kind`, `statement`, `uncertainty` and evidence handles.

Supply 2–32 chronological `sections:[{section_id,node_id,at_qn}]`. Each graph node must cover its source clip's exact whole span and complete note membership. Full section spans, including rests, must fit without overlap; touching endpoints are allowed. Placements replace source origins without interpreting a shared clock. Destination length is positive and at most 65,536 quarter notes, with 1–1,024 output notes. Long duration does not imply a dense arrangement or performance readiness.

At least one section is locked. Each of 1–16 `variations` names a unique unlocked section strictly later than the earliest locked section, an explicit latest-onset `endpoint_note_id`, unique `pitch_offsets_semitones` and `timing_offsets_qn` domains (1–16 each), and `pitch_min`/`pitch_max`. The whole source section must satisfy its pitch bounds. Content-hash ranking using the supplied seeds selects one nonzero feasible pitch/time pair per varied endpoint; the gate must remain inside its original section. Other notes and non-pitch/non-onset fields remain exact. Infeasible selected overlaps fail without rerolling.

The `pocket.arrangement-development/v1` artifact retains the full unchanged graph and its material handles, literal sequence A, varied B, a silent destination of the same length, section identities, sparse changes and the exact public composition steps. Source and generated proofs have independent note and metadata bounds: source ancestry at most 8 MiB/100,000 metadata values; conservative generated reserves at most 48 MiB/750,000 values; and `S + N*(V+2) + sum(remaining_notes_after_each_256_note_delete) <= 10000`, where S includes retained ancestral source notes, N is output notes and V is varied endpoints. Shared artifact integrity limits remain enforced. Public receipts are bounded to 16 KiB and show at most eight unchanged material handles with explicit total/omitted counts; the full unchanged parent list remains in the artifact. Failed or interrupted composition retains subordinate evidence and advertises no valid alternative set; inspecting that evidence does not authorize automatic replay.

`midi_arrangement_query` accepts the development handle and `view:"summary"|"sections"|"changes"`, with `limit` 1–128 (default 32), `max_bytes` 4,096–65,536 (default 16,384) and a revision/view-bound `cursor`. It revalidates source and lock proofs before returning `items`. An oversized item returns `needs_input` with an unchanged cursor; increase the budget or inspect the immutable artifact. Queries do not execute edits. The [77-minute sparse recipe](../examples/midi-arrangement/README.md) uses externally authored synthetic material and explicit placements. It is file evidence, with no inferred motif, model-conditioned composition, native execution or listening claim.

### Declared sustain and release reservations

`midi_lifecycle` analyzes one clip without sending MIDI. Require `source_basis:"canonical_notes_retained_cc64"`, explicit `initial_state:{active_notes:"none",sustain:[{channel,value}]}` covering each involved channel exactly once, a positive `horizon_qn`, a nonnegative declared `release_tail_qn`, and `equal_time_order:"note_off_cc_note_on"` or `"cc_note_off_note_on"`. Times are exact untagged rationals or integers. The binary sustain model treats CC64 values 64–127 as down. The tail is a supplied reservation assumption, not a measured acoustic release.

Current canonical notes supply attacks/gates; retained original note endpoints remain evidence only. Qualified retained SMF controller coordinates and order are checked against source bytes. The interpretation is isolated to the selected clip/source track; it does not establish shared-channel ownership across a session. Source-free records declare their own clip-local event positions. Negative pickups, ambiguous equal-time controller ordering, competing channel curves, native opaque notes, sostenuto/hold2, reset/channel-mode and SysEx/system behavior refuse this initial profile.

Events exactly at the horizon are processed; gates and reservations are half-open. A note still gated or sustained at the horizon has an unresolved release, never an invented release at the boundary. A same-channel/same-pitch reattack during a gate, sustain hold or declared tail records receiver-pairing ambiguity without guessing FIFO/LIFO. Complete findings and controller realization remain in `pocket.midi-lifecycle/v1`; replies are bounded to 16 KiB with omissions stated. At most 10,000 notes and 20,000 retained events are interpreted. This is symbolic analysis. The separate expression planner can consume its proof; bend-range setup, stop/seek/loop/cancel/panic, receiver behavior and E5 acceptance remain unqualified.

### Declared expression planning and file encoding

`midi_expression_plan(material, lifecycle, receiver_assumption, encoding, store_root, request_id)` allocates MIDI1 member channels and produces exact pitch-bend, channel-pressure and CC74 bytes. `lifecycle` is either the explicit lifecycle request above (including `clip_id`) or its immutable report handle. The planner independently recomputes a supplied report. It requires one source note/sustain channel, complete resolved releases through the supplied horizon, 1–4,096 audible notes and at most 65,536 realized events. Original material and curves remain immutable.

The strict receiver assumption declares lower/upper zone, manager channel, contiguous ordered member channels, member bend range, attributed evidence and optional instrument-state handle. Its required policies are `manager_pitch_policy:"neutral_no_pitch_messages"`, `configuration_policy:"assume_preconfigured_no_setup_messages"` and `configuration_status:"declared_unverified"`. These are explicit assumptions; the tool configures no instrument and emits no RPN setup.

The strict encoding declares `source_channel_policy:"single_source_channel_to_zone"`, `sustain_route:"manager_cc64"`, `allocation:"lowest_available_zone_order"`, `exhaustion:"reject_no_stealing"`, `reservation:"through_symbolic_release_plus_declared_tail"`, `same_pitch_policy:"new_per_note_channel_realization"`, `tuning:"tuning:12tet-a440"`, neutral pitch/pressure/slide values, `curve_mode:"step_only"`, `rounding:"nearest_ties_even"`, exact pitch/control error ceilings and `reuse_order:"old_expression_then_declared_off_cc_then_reset_setup_on"`. Notes reserve members through symbolic sustain release plus the caller's tail. Channel exhaustion refuses. A reused member receives ordered release/reset/setup before its next attack. Positive and negative bend endpoints have their distinct MIDI1 quantization errors recorded.

Pitch curves are additive cents/semitones; pressure and slide are absolute normalized 0–1. All are unowned note-relative steps, starting at zero and bounded by the gate. Linear curves, unsupported tuning, opaque source state, unmodeled raw channel messages and unresolved releases refuse. The plan retains exact allocation, event order and per-point rounding proof. Its receipt contains bounded previews and a `pocket.midi-expression-plan/v1` handle.

`midi_export(..., expression=<configuration or plan handle>)` serializes that same recomputed realization. A configuration contains exactly `lifecycle`, `receiver_assumption` and `encoding`. This first file profile requires exactly one complete clip and cannot combine with `cc_step_bindings`. It preserves supported text, tempo, meter and key metadata and their relative order before realized channel events at equal time. Routing/opaque metadata, unavailable map projections, SMF2 and opaque native note payloads refuse. Muted-note omission still requires its named approval. Final reset events may extend the encoded end beyond the musical clip; the sidecar reports the exact extension. A larger analysis horizon alone adds no silence. Exact PPQ is required throughout.

Inline configuration and a separately published plan produce identical bytes and sidecar evidence. Rehashed or stale plans are recomputed and rejected if their contents differ. Omitting `expression` retains the earlier export contract. This file route establishes encoding only: receiver configuration, native preservation, sounding independence, measured release tails, live transport behavior and listening are unverified. See the [executable expression example](../examples/midi-expression/README.md).

### Local audio hypotheses, corrections and optional workers

`audio_hypotheses` takes `store_root`, `request_id`, `source`, `settings` and `attribution`. The strict source supplies `path`, `expected_sha256`, integer `start_frame` and `frames`, and `source_origin:"independently_acquired"` or `"user_recording"`. Pocket retains the complete original bytes and analyzes the exact selected crop: at most 20 seconds and two million frames, within a 256 MiB source with 1–8 channels. Settings supply `bpm_hint` (or null) and `beats_per_bar`. Attribution supplies actor, human/agent kind, statement and uncertainty. No upload, resampling, transcription model or native session is required.

The existing public Peek implementation supplies attack and competing pulse evidence, with explicit abstention. Original frame coordinates, decoded source metadata, detector/version/settings and full portable analysis are retained. Estimated pulse origins remain floating-point seconds estimates, never relabeled as exact frames. Automatic annotations are checked against the detector projection when read. No kick, instrument, note or chord identity is inferred.

`audio_hypothesis_correct` takes a parent handle, its exact `expected_revision`, 1–128 explicit `corrections` and attribution. Corrections name an ID, superseded annotation IDs, a typed attack/pulse/phrase-anchor/note hypothesis, evidence support and uncertainty. Support references must resolve to existing analysis JSON pointers or annotations. A supplied note/pitch is attributed to its author; it is not relabeled as detected. Original and competing interpretations remain queryable, including superseded rows. At most 32 correction revisions and 4,096 cumulative annotations are supported.

`audio_hypothesis_query` takes the hypothesis handle and optional `view` (`summary`, `annotations`, `history`), `limit` (1–128), `cursor` and `max_bytes` (4–64 KiB). Cursors bind the exact revision and view. A single row larger than the budget returns explicit `needs_input` with the same resumable cursor. The reader verifies original bytes, analysis, correction lineage and bounds. See the [audio example](../examples/audio-hypotheses/README.md).

`audio_hypothesis_submit` accepts the same analysis inputs and snapshots the source before launching an optional owned POSIX process. `job_status(store_root, job_id)` returns bounded current state and revision. `job_cancel(store_root, job_id, expected_revision)` requests cancellation with an exact revision; stale requests return conflict. Cancellation is cooperative at analysis and publication boundaries, not a decoder wall-clock deadline. Pending cancellation is distinct from acknowledged cancellation. No PID signaling or unrelated process control occurs.

The worker calls the same public primitive in a private staging store and publishes a verified result graph only after a serialized cancellation check. Execution ownership uses an inherited process lease. A released lease before terminal publication becomes `interrupted`, with no automatic retry. A held lease proves ownership, not progress. Completed submit-request replay returns its historical submission receipt; call `job_status` for current state. Cancelled/failed/interrupted jobs expose no committed result; staging remains diagnostic evidence. These workers require POSIX and installed local audio dependencies, and provide no native execution or performance readiness.

### Optional learned pulse evidence

`audio_model_inspect(store_root, request_id, declaration, qualification="none")` inspects an explicitly supplied local runtime and checkpoint. Call it with keyword arguments. The strict declaration has `adapter:"beat_this_cpu_v1"`, `executable:{path,sha256}`, `weights:{path,sha256}` and `expected_profile` (null or an exact runtime-profile SHA256). Paths are absolute. `qualification:"synthetic_cpu_v1"` runs seven bounded synthetic CPU cases and retains repeat hashes, raw outputs and the runtime profile. Missing dependencies return `unsupported` without a model artifact. This does not download, install or qualify musical accuracy.

`audio_pulse_hypotheses` accepts the same exact source and attribution as `audio_hypotheses`, plus `model` and `settings`. Model is exactly either `{kind:"inline",declaration,qualification:"synthetic_cpu_v1"}` or `{kind:"inspected",model:<qualified artifact handle>}`. Inline and inspected composition produce the same evidence; no inspection call history is required. Settings are exactly `{device:"cpu",dtype:"float32",threads:1,postprocessor:"minimal",downmix:"arithmetic_mean",resampler:"soxr_hq",seed:0}`. The qualified input profile is 2–20 seconds of mono/stereo PCM16 WAV at 8000, 44100 or 48000 Hz, within the existing 256 MiB source bound. The base Pocket environment needs no model packages. The separate declared environment needs BeatThis and its CPU dependencies; the local checkpoint is captured and loaded through the safe weights-only route.

The `pocket.audio-model-hypotheses/v1` artifact retains complete original bytes, exact crop frames, raw float32 arrays, two-run equality checks, preprocessing hashes, model/checkpoint/runtime identity, uncalibrated learned beat/downbeat hypotheses and excluded right-boundary events. Model-frame and source-frame coordinates are rational, including fractional peak positions. Downbeat snapping retains the original peak support. Synthetic tests have produced false boundary pulses; these are hypotheses, never authoritative meter, note onsets or musical decisions. The recorded file profile does not establish every operating-system or dynamically loaded dependency.

The existing `audio_hypothesis_query` and `audio_hypothesis_correct` also accept this model family. Reading/correcting retained evidence needs neither the runtime nor the original source path. Authored alternatives preserve learned attribution and use the same correction types and bounds; they cannot fabricate learned rows or rewrite raw model evidence.

An inspected qualified model reuses its retained seven-case qualification only after the current executable, checkpoint and runtime profile match; the requested crop still runs twice. Inline input performs qualification in the same call. Three earlier integration calls exceeded the unchanged 60-second subprocess limit and published no result. A later verification change pruned excluded directories while retaining the same included-file manifest; changed runner bytes required fresh qualification. That new seven-case qualification and actual direct/background/CLI/stdio-MCP inference passed with identical artifacts in the recorded environment. These bounded successes do not guarantee a deadline under other loads, musical accuracy or another runtime profile. Retained-artifact query/correction and mocked protocol tests remain separate evidence.

`audio_pulse_submit` accepts the direct learned-provider arguments and uses the same `job_status`/`job_cancel` interface. Its additive job/v2 record retains the exact source snapshot and model declaration. The owned model child inherits the execution lease, so a terminated parent does not make a still-running model appear unowned. Cancellation is cooperative at preparation, inference and publication boundaries; an in-flight child may finish before acknowledgement. Each owned model subprocess has a 60-second timeout, with no result published on failure. This is separate from the older deterministic worker's decoder timing limits. Neither worker launches native playback or retries an unknown result.

### Passages from long recordings

`audio_region_capture(store_root, request_id, source)` is independently callable with keyword arguments. `source` supplies the same explicit path, SHA256, start frame, frame count and origin fields described above. Its separate streaming profile accepts ordinary little-endian RIFF PCM16/24 or IEEE FLOAT32 WAV, 1–8 channels at 8–96 kHz, at most two hours and 4 GiB minus one byte. A selected passage is 1 frame through 20 seconds and at most 32 MiB. RF64, compressed WAV and ambiguous container layouts refuse. Reads are bounded to 1 MiB; a full-file hash and before/after file identity checks detect mutation or pathname replacement without loading the recording into memory.

The capture retains an independently written canonical WAV with exactly the selected interleaved PCM bytes, plus original-file SHA256, size, frame count, format, origin and an integer frame-offset mapping. No resampling or channel transformation occurs. Ancillary chunks are not copied. The original full recording is explicitly `external_identity_only`: its bytes are **not** retained or reconstructible from the passage. A successful capture established the full hash then; a later artifact-only query cannot recheck an external recording. Repeating the capture request rechecks the external source. Original paths and filesystem stamps do not enter the portable capture record.

FLOAT32 capture has the separate `riff_float32_region_v1` projection. Source
format code 3 requires 32 bits, a 16-byte format record or an 18-byte record with
zero extension, and exactly one four-byte `fact` count matching interleaved frames.
The canonical crop uses the 18-byte format record plus `fact` and `data` (58-byte
header). Selected sample bytes stay exact, including signed zero, subnormals and
finite values outside ±1. No clipping, normalization or integer conversion occurs.
Selected NaN/infinity refuses before publication and during retained validation;
this finite check makes no claim about unselected samples in the externally
retained original. FLOAT64, extensible WAV and RF64 remain unsupported. Existing
PCM16/24 crops and records retain their exact serialized behavior.

`audio_region_hypotheses(store_root, request_id, region, analysis, attribution)` composes the public capture and analysis functions. `region` is exactly `{kind:"inline",source:<source declaration>}` or `{kind:"captured",region:<capture handle>}`. `analysis` is `{kind:"peek",settings:<Peek settings>}` or `{kind:"learned_pulse",model:<explicit model input>,settings:<learned settings>}`. Each analyzer retains its own input limits; accepting PCM24/FLOAT32 capture does not qualify either format for the learned-pulse adapter. Inline and captured composition produce the same wrapper artifact.

`audio_region_query(store_root, hypotheses, view="summary", limit=32, cursor=null, max_bytes=16384)` reads the wrapper without reanalysis. `view:"annotations"` returns both unchanged local evidence and its original-source projection. Integer event/interval frames gain the exact crop offset; learned rational positions remain rational. A floating-point pulse origin remains a local estimate alongside a separate exact rational original offset in seconds. It is never converted into an exact global float. Pages use the existing 1–128 row and 4–64 KiB limits, with revision-bound cursors and explicit `needs_input` for an oversized row. A crop's evidence does not establish coverage of the gaps or the whole set.

`audio_region_correct` takes `store_root`, `request_id`, `parent`, its exact `expected_revision`, `batch` and correction `attribution`. The batch is exactly `{coordinate_space:"local_crop_frame",corrections:[...]}` or `{coordinate_space:"original_source_frame",corrections:[...]}`. Local corrections use the existing correction union. Original-coordinate attacks and half-open phrase/note/pulse intervals must lie inside the captured passage; the wrapper subtracts only the exact integer crop offset. An original-coordinate pulse uses `lattice_origin:{local_estimate_seconds,original_offset_seconds_q:{n,d}}`; the rational must be reduced and equal the capture offset divided by sample rate. Support pointers and superseded IDs always address underlying **local** evidence. Learned fractional rows remain unchanged prior evidence; corrections use the existing authored forms. No model, decoder or external source is needed. The same public correction function produces the local revision, with original request attribution preserved and the correction actor recorded separately. Bounds remain 128 entries per batch, 32 correction revisions and 4,096 cumulative annotations.

`audio_region_submit` accepts the same region/analysis/attribution inputs as the synchronous wrapper. Its additive job/v3 journal and inherited lease exist before hashing an inline source; this permits cancellation during a long capture. Inline submission has no source artifact yet. Captured inputs reuse their explicit handle. Status/cancellation validate retained records without opening external originals or runtimes. Capture checks cancellation between 1-MiB blocks, while Peek decoding remains cooperative at analysis/publication boundaries. Learned execution inherits the same lease and keeps its existing 60-second subprocess limit. Exact owned arguments, source and invocation identity are rechecked before publication under the shared commit lock. Cancellation, crash or changed identity cannot publish a usable partial result. The older v1/v2 contracts retain their prior behavior. See the [one-hour synthetic passage recipe](../examples/audio-regions/README.md).

### Retrieving attributed feedback

`audition_feedback_query` accepts keyword arguments `store_root`, an explicit `feedback` list of 1–128 distinct feedback handles, and optional `actor`, `actor_kind`, `decision`, `render_sha256`, `interval_frames`, `limit`, `cursor` and `max_bytes`. It verifies every supplied feedback/attachment/candidate/render graph, including records excluded by the filters. It never scans an entire store or infers taste. Only current `pocket.audition-feedback/v1` records are supported; legacy listener-feedback directories are a separate contract.

Filters are exact and combined. A half-open `interval_frames:[start,end]` filter requires the exact render SHA256 and matches interval overlap; touching endpoints do not overlap. Results preserve input order, actor kind, decision, statement, heard interval and artifact identities. Human listening and an agent's technical report remain different evidence kinds. Pagination uses 1–128 rows and 4–64 KiB with cursors bound to all input handles and filters. Retained artifact graphs can be queried after relocation without original project/render paths; this is retrieval of attributed records, not a new audition or approval.

### Interchange fidelity

Original SMF bytes remain separate from reconstructed notes and ordered wire events. Same-tick controller order, release velocity, note-on-zero and opaque meta/SysEx evidence are retained. Ambiguous overlapping lifecycles, unmatched note-offs and unterminated notes have explicit diagnostics. Ambiguous material is not strict-editable; where supported it permits an exact original-byte export. SMF type 2 asynchronous sequences and SMPTE timing are distinct restricted profiles, not ordinary simultaneous PPQ tracks.

Export chooses 9600 PPQ when exact; otherwise a compatible positive 15-bit PPQ, or refusal. An explicitly supplied PPQ is never changed. There is currently no approximate timing export: unrepresentable timing fails. Clip ending/trailing rests participate in exact timing. SMF0 merges tracks; the sidecar retains the chosen format and clip origins, while the rich material retains track and note identities.

Canonical curves, microtonal pitch, custom tuning, mute omission, opaque native payloads and unavailable tempo/meter map projections cannot disappear without named loss approval. Read the reported loss list before deliberately choosing `loss_policy:"approved"`. Approval produces a derivative; it does not establish fidelity in Live. Ableton documents 96-PPQ native MIDI export. The implementation's native probes confirmed 96 PPQ and measured lost fine timing, rest tails, note-on-zero release values, same-time controller events and metadata. Keep rich material and original bytes as masters. [Ableton MIDI files](https://help.ableton.com/hc/en-us/articles/209068169-Understanding-MIDI-files)

For explicit CC1/CC11 step encoding, `midi_export` accepts optional `cc_step_bindings` (1–16). Each binding is exactly either `{"curve": <artifact handle>, "clip_id": <existing clip ID>, "controller": 11, "same_tick_order": "before_existing"}` or `{"curve_id": <embedded material curve ID>, "controller": 1, "same_tick_order": "before_existing"}`. A standalone `curve_transform` result can go directly to export; no material rewrite is needed. Opaque target IDs never imply a controller number.

This profile requires unowned, absolute channel curves in `clip_qn`, with `step` interpolation, `unit:"midi1_7bit"`, domain 0–127, `quantized:true` and all 128 enum values. Point values must be exact integers; points must fit the selected clip and the chosen PPQ exactly. Existing raw control of the same channel/controller, duplicate destinations and ambiguous identities refuse. Bank, sustain, channel-mode messages, other controllers, interpolation and MPE are outside this encoding profile. Only explicit points are emitted; there is no implicit initialization or reset.

New steps precede existing equal-time events within their SMF track; SMF0 uses the merged track. Existing event order stays exact. SMF1 refuses a bound channel used by notes, raw channel events or another bound curve in a different clip, because cross-track playback order is not guaranteed. The sidecar states the ordering scope and retains full mappings, original material, external curve handles and clip origins. The compact receipt contains counts and the sidecar handle. Unbound rich curves still require named loss approval; omitted bindings preserve the prior export and original-byte passthrough behavior. File encoding does not certify a receiver or native round trip.

## Curves and independent sound work

`musical_time(operation, store_root, ...)` creates, queries or converts through an explicit `pocket.time-map/v1`. `operation:"create"` requires `request_id` and `definition`: `source_context`, a bounded `domain_qn`, rational step `tempo`, and `host_origin`. Optional definition fields are `render_origin`, `meter`, `bar_one_qn` and `cycles`. A declared inline clock context works without a DAW, instrument or audio file.

For `operation:"convert"`, supply the returned `time_map`, `positions` (at most 512) and `target_space`; omit `request_id`. Positions use `arrangement_qn`, `host_seconds` or `render_frame`; targets also include `bar_display`. Quarter-note and second values are reduced rationals. Frame positions are integers and require a hashed readable render, verified sample rate and explicit origin. Noninteger output frames require `quantization:{"policy":"nearest_half_away_from_zero","max_error_frames":{"n":1,"d":2}}` or a tighter tolerance; signed frame/second error is reported. Out-of-domain conversion fails.

For `operation:"query"`, supply `time_map`; optional `section="summary"` accepts `tempo`, `meter` or `cycles` with `offset=0`, `limit=100` (maximum 256). Tempo integrates exactly across declared steps and negative pickup positions; bar labels require explicit bar-one and partial-bar boundaries. A seven-eighth local cycle is an annotation and does not change host meter. Ramps, warp/source clocks, groove, looping occurrences and native clock verification are unsupported by this profile. Thread's existing timing interpretation remains separate.

`curve_transform` requires `store_root`, `request_id`, and `operations`. For creation pass `target`; for an edit pass `curve`. Optional arguments are `context`, `conflict_policy="reject_existing"` and `allow_discontinuity_change=False`.

Supported file operations are `create`, `shift`, `scale_time`, `scale_value`, `offset`, `smooth`, `simplify` and `splice`. Points contain rational `time`, finite `value` and integer `order`. Equal-time points require explicit step semantics. Quantized/enum targets require steps. Target kind, scope, unit, value domain, ownership and value mode are explicit; per-note targets require note identity and resize policy, and instance targets require a layout fingerprint. Units are not inferred from normalized values.

Curve artifacts retain `executable:false`. Eligible CC1/CC11 steps can be explicitly encoded by the export route above; there is no automatic sampling or native execution. `plan_override` records a proposed ownership override without changing existing automation. Smoothing steps requires explicit permission; range violations refuse instead of silently clamping. Approximation bounds are reported in the target unit. Polling is not gesture capture.

A separate supervised Live 12.4.5 stock Utility experiment saved and reopened exact ramp, step and pulse envelopes. Its parameter-specific override/re-enable sequence and one-point edit were observed, and the clean pulse copy was rendered. These private observations qualify that UI experiment only. The production candidate profile still excludes Utility automation; public native envelope writing, gesture capture and the Serum macro test remain unavailable.

`sound_plan` can be called alone with `store_root`, `request_id`, `brief`, and `recipe:"operator_sine_pluck"`. No MIDI or DAW is required. The recipe proposes oscillator A sine only, key-tracked pitch, no FM/filter/LFO/pitch modulation, amp attack 5 ms, decay 180 ms, sustain silence and release 60 ms, envelope looping off, four voices and no effects. It is a setup plan requiring native control/readback, not a saved fixture preset or an assertion about Operator's initialization state.

The technical acceptance run separately saved a versioned Operator `.adv` through the native UI and retained its exact bytes. The patch reopened inside the saved candidate. A later standalone load retained the intended displayed controls but introduced 26 small numeric serialization differences, alongside allocated identities and preset provenance. In the writer fixture, the loaded Operator's complete saved state remained exact through insertion, editing and cleanup except for one observed runtime ID. These are distinct checks; neither establishes bit-exact preset restoration, an automatic preset operation or compatibility with another installation.

For `recipe:"parameter_alternatives"`, supply `state`, `alternatives`, `expected_layout_sha256` and `expected_topology_token`; optional inputs are `locks`, `context`, `performance` and `max_alternatives=3` (maximum 8). Each alternative has a label, hypothesis and one or two exact parameter ID/value changes. Unknown targets, stale layouts, changed topology, ownership conflicts, locked fields, ranges and enum values are validated before artifact publication. `performance` must be valid material; `context` must be a context artifact. Plans preserve an unchanged baseline and remain non-executable.

`instrument_inspect` publishes a bounded installed inventory or a validated `scope:"supplied_observation"`. It requires `store_root`/`request_id`; optional arguments are `plugin_roots`, `product="all"`, `observation`, `limit=50`, `offset=0`. Supplied observations include exact identity/build/format/instance binding, topology token, descriptors and attribution. They remain incomplete, unverified observations. No `saved_instance`/`live_instance` extraction route is implemented here.

`instrument_parameters(store_root, state, limit=50, offset=0)` queries that artifact. `preset_catalog` supports `operation:"scan"` with approved `roots` and `request_id`, or `operation:"query"` with `catalog`; optional `product`, `query`, `limit` and `offset` bound results. Preset bytes are hashed and left opaque. Extension-based discovery is conditional; it does not certify loadability, assets, preview audio or suitability.

`serum_inspect`, `serum_presets` and `serum_plan` specialize those public functions. Serum 1/2 and their FX identities remain distinct; no private preset structure is edited. **Native Serum work is deferred at the user's request.** No license, install, live parameter write, preset load/save, complete-patch inspection, FX route or MPE receiver has been qualified.

## Native note observations

`build_native_midi_device(output_dir, store_root, request_id)` builds a separate read-only Max device in a new local directory. It does not start Live or install anything globally. Deliberately load `Native MIDI.amxd` on an audio-capable track after coordinating the native session; keep every adjacent package file together. Baste stays unchanged and read-only.

`build_native_midi_writer(output_dir, store_root, request_id)` builds the separate guarded writer package. Its immutable device handle identifies the compiled device and every adjacent source file. Building or loading it does not authorize an edit. The reader package remains read-only; write qualification and recovery are separate gates.

`native_midi_read(store_root, request_id, target=...)` reads an explicit `{"location":"arrangement","track_index":1,"clip_index":0}` target. Indices are positions in the current native topology, not durable IDs. The tool returns an immutable observation handle, bounded note rows, the session nonce and a native request reference. No candidate, generation call or Serum is required. Native notes expose their nine observed fields; per-note expression remains unobserved. Reads are sequential and recheck topology, without claiming atomicity or complete unsaved-state equivalence.

Optional `saved_binding` contains `saved_als`, `expected_sha256`, `track_id` and `clip_id`. It retains the exact saved bytes and verifies the native path and ordered primary arrangement mapping. It does not turn the saved file into proof of all current unsaved state. Optional `expected_session_nonce` rejects a different loaded device/session. `bridge_dir` defaults to the local Pocket native MIDI directory; `timeout_seconds` defaults to 15.

The default response has at most 64 notes; `limit` may be 1–256 and `byte_budget` 2,048–131,072 bytes. The native projection is capped at 2,000 notes. Follow the returned `next_page` with a new request ID and `observation` handle to page the same retained snapshot. Stored pages require no host. Fresh observations start at offset zero; reusing an old request ID returns historical evidence. Use a new ID for a fresh read.

`native_midi_status(store_root, request_id, native_request, bridge_dir=None)` inspects the retained local bridge journal without a host or redispatch. A completed journal describes that request, not current native state. A timeout can leave an unknown result; inspect it rather than repeating a mutation. Use the exact returned `recovery_arguments`, including the writer's separate `bridge_dir`. The reader device has no write route. Native read/build/status passed one real Live 12.4.5 fixture, including optional saved binding and actual CLI/MCP calls.

## Guarded native note writes and recovery

`native_midi_write` is a separate public provider, CLI command and MCP tool. Supply `store_root`, `request_id`, `workspace_id`, `expected_revision`, `saved_als`, `expected_sha256`, `expected_observation`, `writer_device`, `edit` and `supervision`. Optional `bridge_dir` defaults to the separate native MIDI writer directory; `timeout_seconds` defaults to 15. The writer needs no generation step or Serum. Externally supplied valid material can prepare its workspace.

The qualified fixture is deliberately narrow: the exact bundled writer, Live 12.4.5 build recorded by the profile, constant 120 BPM/4/4, a protected audio-only baseline with the known stock returns, and one owned unlooped primary MIDI clip containing Operator plus the writer. Transport must be stopped, recording and overdub off, and the target track unarmed. The observed automation-arm state is retained; it is not required to be off. Unknown source changes, enriched notes, groove, envelopes, ambiguous clip mapping or different packages fail. The native API cannot establish every unsaved property; explicit supervision and the exact saved primary clip are required. This does not qualify general MIDI editing, MPE, controllers, transport or automatic save/render.

Supported edits are:

- `{"kind":"insert_empty","notes":[...]}`: one to three ordinary notes in an empty clip. Each note has `pitch`, reduced rational `onset_qn`/`duration_qn`, integer `velocity` and `release_velocity`. Dyadic time denominators are bounded by `2**20`; notes must fit wholly inside the clip and cannot overlap. Attack values are 1–127; releases are 0–127.
- `{"kind":"set_velocity","note_id":2,"velocity":89,"insertion_terminal":{...}}`: change one note's attack velocity using the same session's verified insertion terminal and a fresh bound observation. Every other observed note field remains unchanged.

`supervision` contains `actor`, `actor_kind`, timezone-qualified `observed_at`, `exclusive_native_session:true` and `no_ui_edits_after_native_read:true`; an optional `note` must describe the actual operation. Native IDs are scoped to the observation/session, never durable clip or instrument identities. Save any intended intermediate native state, then obtain a new saved binding before the next edit.

The provider preserves a pending record before contacting Max, checks source/workspace bytes again before commit, and retains the exact request, preparation, native call and readback. A second native guard precedes the whitelisted mutation. Concurrent changes refuse; a lost outcome quarantines the workspace and host. Retrying the same request returns retained evidence and never dispatches again. That evidence is historical even if the project has since been saved or closed.

`candidate_native_reconcile(store_root, workspace_id, expected_revision, terminal, attribution, request_id)` accepts only a validated terminal matching the exact pending request and unchanged saved files. It updates file state and releases only that terminal's host lease. It never calls a native edit. `attribution` requires actor/kind/time/reason. Unknown outcomes cannot be reconciled as success. A stable process-owned lock permits recovery of an interrupted marked workspace; an active process, unmarked old lock, symlink or conflicting identity refuses.

If no terminal can be established, `candidate_native_abandon` requires the exact `pending`, workspace/revision, a `fresh_observation` of a different stopped saved project, `attribution`, and `request_id` (plus optional writer `bridge_dir`). The operator must first stop all old Live/Max instances of the candidate, unload its writer and close it without saving. Attribution must explicitly set `all_old_live_max_instances_stopped`, `old_writer_unloaded`, `old_candidate_closed_without_saving` and `no_native_dispatch_in_flight` to true. Connection loss alone is never closure proof. Abandonment permanently marks the candidate unsealable/nonresumable, retains the unknown outcome and all evidence, then releases only its matching lease. It is not rollback. Start a new copied candidate afterward.

One actual failed preparation exercised this abandonment path. A corrected package then passed three-note insertion, stale refusal with zero dispatch, a single velocity change, and exact note readback after saving/reopening. Separate source preservation and sealing checks remain necessary. Prepared-only cancellation exists internally but is not publicly exposed until its native acceptance gate passes.

## Supervised native candidate and comparison path

The additive v3 tools preserve legacy Stitch v2/Pipette behavior and Baste's read-only boundary. Stitch exposes the public candidate and audition functions through aliases to the same implementations; Pipette similarly exposes `promote_candidate` and `validate_candidate_promotion` for promotion v2. Existing MIDI/plugin guards remain in place. Their file validators and attributed reports are implemented; current native acceptance remains a separate gate.

A separate disposable E1 experiment exercised Live's extended-note read, insertion of three conventional notes, and one selected velocity change through a Max device. Readback, stale refusal and saved/reopened notes agreed. Independent comparison preserved protected musical source fields; eleven exact runtime-ID initializations and a byte-identical generated probe's changed timestamp required explicit experiment-only qualifications. That initial experiment alone did not qualify a production writer or expand candidate normalization. The later, separately tested public writer and its narrower profile are described above.

The narrow preservation profile is `live-12.4.5-225ce5e356-metadata/v1`, bound to the exact build above. A newer host or different serialized shape does not inherit that qualification. Its predicate report identifies each allowed metadata transition; musical controls, active sources and unknown changes remain protected.

1. `candidate_prepare(source_als, expected_source_sha256, store_root, request_id, ...)` copies an audio-only/plugin-free source into a new workspace and collects supported dependencies. `mode:"clone_only"` prepares the unchanged baseline. `mode:"with_material"` additionally needs a material handle, a context and `layer:{"track_name":"Pocket layer A","instrument_device":"Operator"}`. The current context is constant 120 BPM, 4/4 with rational `arrangement_start_qn`/`length_qn`. One unlooped conventional channel 1 clip is supported; native expression, events/curves, mute and microtuning are excluded.
2. `candidate_inspect(store_root, workspace_id, expected_revision)` rechecks state. After establishing exclusive safe use of Live, a musician or supervised operator imports material into this disposable copy, sets and verifies the stock sound, saves and reopens. The separately qualified note writer can perform its bounded note edits. Never displace an active/unsaved session.
3. `candidate_seal` requires workspace ID/revision, the saved path/hash, `native_report`, `request_id`, and an `instrument_state` for a layer. It verifies preserved source/dependencies and permitted changes. Native reports require actor/kind/time, host version, save/reopen/missing-media/device status and the saved hash. Recipe verification is required for the stock layer. Reports stay **attributed**; `provider_native_observation:false` remains explicit. An exact-build profile permits only independently reviewed metadata transitions observed in the stock fixture; unknown changes and source tempo/meter changes fail. `candidate_cancel` requires the same revision discipline.
4. `audition_plan` takes sealed `candidates` (one through eight), a sealed clone-only `baseline`, `span_qn`, a `question`, and optional pre/post-roll/tail/rate/channels. Defaults are 4 s tail, 48 kHz, stereo. Constant 120 BPM/4/4 and whole-frame mapping are the current bounds. Render execution is supervised.
5. `attach_candidate_render` requires exact candidate/render-plan handles, a completed float-WAV path/hash, matching `actual_settings`, and an attributed native export report. It checks decoded frames/rate/channels, finite samples, sample peaks and RMS; it classifies silence/overload and retains failed evidence without permitting promotion. It does not measure LUFS/true peak or listen.
6. `audition_feedback` records exact attachment, frame interval, actor/kind and note, with optional `keep`, `revise`, `reject`, or `no_addition`. `promote_candidate` requires exact candidate/attachment, an attributed keep decision and a new output directory. A human keep needs matching attributed human feedback. An agent keep remains an agent decision. `validate_candidate_promotion` rechecks lineage/package bytes and ordinary Thread after relocation; native relocated reopen still reads `not_verified`.

`promote_candidate` also accepts optional `related_artifacts` (at most 32 complete handles) to collect MIDI derivatives, fidelity sidecars, preset bytes or other comparison evidence into the sealed lineage. Omitting it preserves the earlier promotion-v2 schema and request identity. Extra artifacts do not grant a musical keep or expand native portability.

For one supervised velocity revision, `candidate_seal` accepts optional `final_material`. This must be a direct child of the prepared material with exactly one attack velocity changed. Every other note field, ordered identity, clip, source event, curve and map stays fixed. The seal retains the original preparation and a separate amendment artifact, then compares saved notes against the final material. An externally supplied valid child can replace `midi_transform`; the lineage and invariants are still checked. Omit the argument to retain the existing seal contract. This does not write the edit into Live.

Optional `runtime_identity_observation` supplies the full immutable native observation for the exact original workspace and owned track/clip. For the qualified stock fixture only, its seven observed protected-object runtime IDs can explain zero-to-ID metadata initialization. Both the retained observed source and the final saved source must still match the original protected project and dependencies under the existing exact-build rules. Unknown IDs, different paths, musical changes and altered dependencies refuse. The observation does not prove current unsaved state or replace save/reopen evidence. Omit the argument to preserve the earlier seal input and trial shape.

Sealing and `validate_candidate(candidate, store_root)` return concise receipts with exact candidate/saved-set handles and preservation counts. Full normalization, source, amendment and native evidence remain in the immutable `pocket.native-trial/v3` record. Validation still checks that complete graph. Previously retained journals and trials are never rewritten to shorten responses. Library consumers that need the complete validated record use the same validation core through `load_candidate_record`; it is not another MCP tool.

`candidate_inspect` returns the current workspace summary and history count. Request `history_limit` (1–20) and follow `history_cursor` to page retained history at the same expected revision. The default returns no history rows. Pages bind the exact workspace state and have a byte bound; changed state rejects old cursors. History remains complete on disk.

Inspect returned handles/revisions instead of inventing them. Original projects, recordings and presets remain unchanged. Mutable files are copied, never shared writable hard links. Keep machine-specific receipts, music and real listening feedback outside repository fixtures.

## Five separate evidence layers

1. **Artifact integrity:** validated structures, exact hashes, ordered events, source preservation and tests.
2. **Native save/reopen verification:** observed or explicitly attributed host behavior, with environment and limitations retained.
3. **Rendered audio and measurements:** the exact WAV and its technical checks.
4. **Actual human listening:** an attributed listener's report bound to the exact render and interval.
5. **An attributed musical decision:** keep, revise, reject or leave unchanged, with the decision-maker and context.

A render never establishes listening. Technical validation never selects a key, quantizes by default, calls a sound useful, or invents a musician's preference.

## Authored audio evidence and timing alternatives

`midi_timing_alternatives` composes retained audio-region evidence, `musical_time`,
`material_query` and `midi_transform`. Supply external material or an immutable
material handle, its exact selection, the exact region-hypotheses revision,
`alignment`, one or two `alternatives`, explicit `locks` and attribution.
No model, original audio path, candidate, DAW or generation step is required.

The selection contains 1–16 ordinary notes in one arrangement-origin clip. Each
alternative must match each selected note exactly once, in explicit edit order,
to an active authored attack or pulse correction. An attack supplies its original
integer frame. A pulse match requires a caller-declared rational point inside its
interval and a statement: the retained floating pulse origin is never promoted
to an exact grid. Automatic or superseded rows are retained but cannot be selected
by this first profile.

The strict alignment is `declared_unwarped_source_clock`, with original SHA256,
sample rate, rational `source_anchor_frame_q` and `host_anchor_seconds_q`, a
`time_map` handle and `clip_id`. The exact frame offset becomes host seconds,
then the public converter supplies arrangement quarter-notes; the clip origin
is subtracted explicitly. Warp, loop, latency and native synchronization are not
inferred. A conflicting material tempo-map reference refuses.

Each alternative declares rational strength 0–1 and maximum shift greater than
0 and at most 16 quarter-notes. Every target comes from the original selection.
The existing public editor applies one shift at a time, rejecting new overlaps
and note-owned expression. Intermediate overlap can refuse even when a simultaneous
move might fit. All other fields and controller timelines stay fixed; moved notes
can consequently encounter different controller values. A locked onset permits
only zero movement. Zero movement retains an explicit no-op proof.

`unchanged` and `no_addition` are the same original material handle: leave all
existing music alone. They do not mean a silent replacement. Completed manifests
retain exact matches, clocks, public edit proofs and alternatives. A later failure
retains completed steps as `pocket.midi-timing-progress/v1` with state `incomplete`,
without publishing a valid alternatives manifest. The same interrupted request
is not automatically rerun. Inspect normal request journals and retained evidence.

`midi_timing_query(store_root=..., manifest=..., view="summary"|"matches"|"steps")`
rechecks the complete graph and recomputes the public edits in a temporary isolated
store, without altering the caller's store or requiring its request journals.
Its cursor binds the manifest and view; `limit` is 1–32 and `max_bytes` is
2048–65536. A row larger than the requested budget refuses.

Input evidence is limited to 70,000 nodes / 4 MiB JSON / depth 40; material to
32 KiB / 2048 nodes; projected proof to 90,000 nodes; completed evidence to
100,000 nodes / 8 MiB JSON / depth 64. The manifest is at most 512 KiB, the
public receipt at most 16 KiB, and material-only replay copies at most 16 MiB.
These are ceilings, not a promise that every combination fits: broad source
ancestry reduces usable steps. A compact 16-note, two-alternative, 32-step
witness passed; a retained actual learned-model graph fits the input budget.

See [the executable timing recipe](../examples/midi-audio-timing/README.md) for
external notes, an explicit correction, A/B/unchanged alternatives, direct/composed
equality and source preservation. It is synthetic engineering evidence, not
listening or an acoustic improvement claim.

## Optional note hypotheses

`audio_note_model_inspect` and `audio_note_hypotheses` are separate public Python,
CLI and MCP providers. They require an explicitly supplied, already installed
local ONNX CPU environment and the supported Basic Pitch model's exact SHA256.
No installation, download, cloud request, Live session, MIDI generator or candidate
is part of this operation. Ordinary material and sound tools keep working without
this optional environment.

The strict model declaration uses `adapter="basic_pitch_onnx_cpu_v1"`, absolute
`executable` and `weights` paths with SHA256, and `expected_profile` (null for first
inspection). The declared virtual-environment launch path is preserved while the
resolved executable is hashed. The retained profile binds Python, listed runtime
distributions, standard-library files, runner/helper bytes, prefixes and launch
identity; unenumerated operating-system shared libraries are outside that profile.
Only the known ONNX weights are accepted. CPU execution is sequential with one
intra/inter-op thread, disabled spinning and no model cache or fallback backend.

Inspection with `qualification="none"` records a profile but cannot authorize
inference. `qualification="synthetic_onnx_cpu_v1"` runs two explicit two-second,
mono 22050-Hz cases: silence and a gated 440-Hz tone. It retains both complete raw
inference repeats. This built-in check establishes finite, repeatable execution
for those cases; it does not imply pitch accuracy, all-format qualification or
E8 musical acceptance. The measured broader production-path cases are separate
execution evidence.

`audio_note_hypotheses` takes `store_root`, `request_id`, the ordinary exact-frame
`source`, `model`, complete `settings` and attribution. `model` is either an
explicit inline declaration with qualification or an inspected model handle. The
source profile is 2–20 seconds of mono/stereo PCM16 or narrow IEEE FLOAT32 WAV at
22050/44100/48000 Hz. Float headroom is retained; selected nonfinite samples are
refused. Stereo uses an explicit float32 arithmetic mean, so opposite-phase
channels can cancel. There is no implicit normalization or clipping. Conversion
to 22050 Hz uses soxr HQ and retains the resulting bytes separately.

The complete settings are:

```json
{"device":"cpu","dtype":"float32","threads":1,"downmix":"arithmetic_mean","resampler":"soxr_hq","decoder":"basic_pitch_0_4_0_false_false_v1","onset_threshold":0.5,"frame_threshold":0.3,"min_note_frames":11,"energy_tol":11,"infer_onsets":false,"melodia_trick":false}
```

This is a fixed, explicitly different decoder interpretation from the vendor's
inferred-onset/energy-fill defaults. The narrow Apache-licensed decoder runs in
the base NumPy/SciPy environment. It recomputes the complete note ledger from
retained float32 window tensors, including traversal order, neighboring-bin
suppression, exclusions and empty results. Querying a relocated proof needs no
model runtime, external recording or request history. Base replay verifies exact
source decoding/downmix and retained resampling bytes; it does not rerun the
optional soxr resampler or ONNX model.

New analysis records separately retain the base decoder's NumPy/SciPy versions,
projection-module hash, vendor source hash and replay semantics. Exact earlier
records remain readable with `projection_provenance_status="legacy_not_retained"`;
they are not rewritten. Replay recomputes the complete ledger in the current base
environment, without a blanket claim of numerical equivalence across versions.

The proof distinguishes each 142-frame unwrapped window from the vendor's
172-frame timestamp correction. Vendor seconds, float representation, exact
float-derived source fractions and outward integer envelopes are retained.
These are timing estimates, not acoustic onset measurements. Pitch bins use
12-TET/A440 with `cents=0` as a declared reference, not measured tuning. Model
amplitude is not MIDI velocity; contour activations are not an expressive
performance, bend-range declaration or qualified receiver encoding. No instrument,
bass ownership, chord/key or kick identity is inferred.

Existing `audio_hypothesis_query` and `audio_hypothesis_correct` accept the new
`pocket.audio-note-hypotheses/v1` family. Corrections keep automatic rows, source
and raw evidence, append attributed alternatives and use the existing strict
annotation/support/supersession forms. Limits remain 128 corrections per batch,
32 revisions and 4096 total annotations. `audio_region_hypotheses` accepts
`analysis.kind="learned_notes"`; it composes the same provider with exact capture.
Region query/correction retain the exact original-frame offset alongside local
model evidence. Earlier Peek and learned-pulse serialized behavior is preserved.

`audio_note_submit` optionally runs the same primitive in the existing owned
POSIX worker engine (`pocket.analysis-job/v4`). Explicit inherited lease ownership,
revision-checked cancellation, private staging and semantic result verification
apply equally to direct and composed work. A cancelled, failed or interrupted job
has no committed successful result and is never automatically retried. Each owned
model subprocess has a 60-second bound. This is an offline tool, with no live
latency or performance-readiness claim.

See [the optional note recipe](../examples/audio-notes/README.md) for a synthetic
public-provider example. Musical acceptance still requires source-bound listening,
correction cost and attributed decisions; leave the existing music alone whenever
that is the useful choice.
