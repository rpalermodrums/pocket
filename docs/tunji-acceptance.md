# Tunji motif → stock instrument → preserved-set handover

This is an acceptance scenario for the user's requested musical result. It is not a report that the work has been completed. The reference is the **opening bass motif from the original 1962 studio recording of “Tunji”**; the destination is the user's preserved Ableton set. Keep the existing harmony, retain a close version of the motif, explore accompaniment and development, and deliver something the user can hear and continue editing.

Start by presenting the actual preserved-set bounce when available. Do not hold that useful listening result behind optional transcription or a new native feature. A source-file excerpt, an offline mix and a native set render must be labeled separately.

## Bind the music before editing

Record the exact locally supplied recording/version and file hash, the chosen opening phrase's source-frame interval, and the destination set revision and passage. Do not substitute another recording, invent a timestamp or pitch sequence, or infer the bass part from a file or track name. Record who selected the phrase and destination. The preserved set's audio is context; it is not automatically a bass stem or MIDI ground truth.

Use `audio_region_capture` to retain exact supported source samples; inspect the capture receipt and retained capture record for their coordinates. If helpful, `audio_region_hypotheses` can compose the same public analysis primitives with explicit original-frame mapping; `audio_region_query` then inspects that mapped-analysis handle. Optional `audio_note_model_inspect` and `audio_note_hypotheses` provide uncertain note hypotheses; `audio_hypothesis_query`, `audio_hypothesis_correct` and `audio_region_correct` preserve corrections and attribution without rerunning a model. Silence, missed notes, competing pitches and uncertain boundaries must remain visible. A supplied transcription or directly authored ordinary-note material can replace model output entirely.

Record the chosen note sequence, timing interpretation, tuning assumption and any deliberate simplification as attributed choices. A model's pitch bin is not proof of instrument identity or performed tuning; its amplitude is not confidence or MIDI velocity. Raw contour evidence is not qualified MPE expression. Keep the recording and raw hypotheses intact when correcting them.

## Make the alternatives visible

| Choice | Musical question | What stays explicit |
|---|---|---|
| Unchanged / no addition | Does the destination already work best alone? | Original set and comparison interval; no new layer |
| Close motif | Can the selected opening bass idea remain recognizable in this context? | Exact source phrase, chosen notes, rhythm, rests and explicitly attributed differences |
| Accompaniment | Can the idea support the existing harmony without replacing its bass role? | Register, density, gate, dynamics, placement and which original notes remain locked |
| Development | Can repetition or a bounded variation carry the idea into the next section? | Preserved motif identity, exact changed notes/sections and rejected alternatives |

Import supplied material through `material_import`; inspect it with `material_query`. Use `midi_transform` for declared edits and locks, including explicit per-note revoicing rather than an inferred harmonic rewrite. `material_structure` records attributed motif/phrase relationships; `material_sequence`, `midi_develop` and `midi_arrangement_develop` construct supported ordinary-note occurrences and sections. `midi_arrangement_query` keeps long developments reviewable.

Use `musical_time` for declared clock conversion. If timing is based on audio evidence, `midi_timing_alternatives` requires explicit matches between selected note IDs and active authored attack/pulse hypotheses; inspect its proofs with `midi_timing_query`. It does not automatically decide which attack matters or make the music fit. Keep pickups, rests, ambiguity and the option to leave the phrase alone.

Each choice should identify its parent material and exact edits. A changed rhythm, register or ending must be visible before audition. Do not silently flatten expressive source data to fit a native import route.

## Keep the sound editable and separate

Choose a stock instrument, initially Operator, and retain its exact saved native state separately from the MIDI material. Explain the proposed sound's role in plain language and make level/register choices reviewable. A different sound must not silently alter the notes, and a note edit must not imply a preset change. Serum and Splice remain outside this scenario unless the user explicitly reopens that work.

The immediate technical route is a new disposable stock project with a supported audio context and an editable MIDI clip. `candidate_prepare` creates supported isolated workspaces; `candidate_inspect` checks their revisions. Native instrument loading, save/reopen and rendering are supervised steps, not implemented by a recipe or an unavailable tool name.

The separately qualified `build_native_midi_writer` → `native_midi_read` → `native_midi_write` route supports only its exact current package/build and guarded stock profile: stopped Live 12.4.5, constant 120 BPM / 4/4, one empty primary arrangement clip, one insertion of **1–3 nonoverlapping ordinary notes** with supported exact dyadic timing, and a narrowly qualified subsequent velocity change. It does not append an arbitrary phrase, repeat batches into a nonempty clip, write expression/controllers, start transport, save or render. A longer motif requires a separately qualified route; selecting a smaller cell must be an explicit musical choice, not a workaround disguised as fidelity.

General native SMF fidelity failed the existing E2 experiments. `midi_export` remains useful for an identified MIDI derivative and fidelity sidecar, but successful file export does not prove native import preserved timing, releases, controllers, tails or tempo. Do not weaken those checks. The preserved variable-tempo/Max-containing set is outside the narrow stock writer/candidate profile; a direct full-set insertion needs its own source-preservation experiment and native readback. A standalone stock demonstration is not yet that handover.

## Hear the handover and the role change

Prepare a comparison spanning context before the motif, its entrance, the handover and enough time after it to judge the new role. Name the intended change: for example, bass statement becoming accompaniment, or a motif yielding to an existing part. This is a proposed musical intention, not something inferred from symbolic role labels.

Let the listener compare unchanged, close, accompaniment and development choices under declared gain and timing conditions. Identify any time stretch, gain adjustment or listening derivative; keep the unprocessed render master. Check that the chosen role does not obscure the existing harmony through actual listening, not a claim derived from note overlap or a track called “kick.” Revisions may be small, and “no addition” is a valid final choice.

For supported sealed candidates, `candidate_seal`, `audition_plan` and `attach_candidate_render` bind exact save/reopen and completed render evidence. `audition_feedback` records an actual actor, render, interval and verdict; `audition_feedback_query` retrieves that evidence. Rendering is never an audition, and an agent technical keep is never the user's musical approval. `promote_candidate` and `validate_candidate_promotion` apply only when their supported preservation and decision gates have passed; unsupported set contexts must not be given a success-shaped promotion claim.

A prepared arrangement or manually rehearsed transition is distinct from live control. No host-quantized launch, controller takeover, panic/recovery or performance readiness is established here. Phase 5 / E9 still requires qualified native operations and the declared-load 30-minute soak. Do not put model inference or structural editing on an audio-thread path.

## Delivery and acceptance evidence

| Deliverable | Required identity and qualification |
|---|---|
| WAV | Completed native render master, exact source/candidate revision, interval, sample rate/channel layout, export settings and measured integrity; source crop or offline mix labeled as such |
| MP3 | Listening derivative linked to the WAV hash, with encoder/settings retained; not a lossless or sample-exact measurement master |
| ALS | New editable project with stock instrument and MIDI, collected or explicitly inventoried dependencies, actual save/reopen result and protected-source comparison |
| MIDI | Exact derivative plus fidelity sidecar and canonical material lineage; native interchange limits remain explicit |
| Choices and feedback | Unchanged/close/accompaniment/development identities, audible comparison labels, actual listener/interval/verdict, rejected options and outstanding uncertainty |

Keep these five evidence layers separate:

1. **Artifact integrity:** hashes, source frames, material/edit lineage, locks, dependency files and complete proofs.
2. **Actual native save/reopen:** what project was saved and reopened, observed note/instrument state, missing media and preservation differences.
3. **Rendered audio and measurements:** completed files, decoding, duration, finite samples and measured levels. These checks do not establish musical quality.
4. **Actual human listening:** who listened to which exact render and interval; unknown if not reported.
5. **Attributed musical decision:** what was kept, revised, rejected or left alone, and whose judgment that was.

Acceptance requires a playable, correctly labeled result and editable material—not just passing tests, a proposed plan or a rendered technical fixture. Until the relevant native and listening gates pass, label the result as a partial demonstration and identify the specific remaining step.

See [public calls and limits](midi.md), the [capability matrix](midi-capabilities.md), the [note-hypothesis example](../examples/audio-notes/README.md), the [timing example](../examples/midi-audio-timing/README.md) and the [MIDI workflow](../examples/midi-workflow/README.md). These are composable public tools with shared Python/CLI/MCP implementations; the scenario does not add a private editor, hidden state or a required workflow runner.
