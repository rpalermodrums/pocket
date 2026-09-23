# What's supported

> **In brief.** Almost everything in the [MIDI and sound tools](midi.md) works
> on files alone, with no DAW. A small, carefully tested set of steps works
> inside Ableton Live, and a person supervises each one. Several larger
> abilities are still on the roadmap. This page says which is which.

## At a glance

**Works on its own, with no DAW or model:**

- Import, generate, query, edit and export MIDI material, with locks,
  lineage and an explicit report of anything lost in export.
- Phrase and motif structure, whole-clip sequences and multi-section
  development from phrases you supply.
- Exact musical time: tempo maps, pickups, partial bars and conversions.
- Control curves (CC1 and CC11), sustain and release analysis, and per-note
  expression planning and file encoding.
- Deterministic audio attack and pulse hypotheses on exact passages of long
  recordings, with attributed corrections.
- Sound plans, parameter alternatives and preset catalogs, as plans only.

**Optional, with a model you set up yourself:** learned pulse hypotheses
(BeatThis) and note hypotheses (Basic Pitch), each on a narrow, pinned CPU
profile.

**Inside Ableton Live 12, narrowly and supervised:** reading clip notes from the
open set, a guarded writer that inserts up to three notes or changes one
velocity in a specific test set, and a candidate loop in which a person saves,
reopens and renders in Live while Pocket checks the files, attaches renders,
records feedback and promotes a kept result.

**Not available yet:** writing native parameters, loading or saving presets,
native automation, recording gestures, automatic save or render, applying
candidates automatically, general transcription, source separation, native
Serum control and live performance.

Call `capabilities_list` (`pocket capabilities-list`, or the MCP tool) for the
current inventory on your own installation, with dependencies and pagination.
Capabilities that aren't available yet are listed with `public_tool: null`.
They're not callable stubs, and supplying a host profile can't enable them. See
the [MIDI guide](midi.md) for calls and limits, and the
[composable recipe](../examples/midi-workflow/README.md) to try it.

## Where things stand

The file-based capabilities below and the supervised evidence pipeline are
implemented. One narrow synthetic test set built from Live's stock devices
passed technical validation. Unchanged, A and B copies were saved and reopened,
source preservation and notes were checked, renders were attached, and an
agent's technical keep was promoted, moved and reopened. The relocated render
matched the original A's decoded samples exactly. None of this completes the
musical workflow, and no human listening or musical approval is claimed. General
native MIDI file fidelity failed concrete probes. Native Serum control is
deferred.

## Capability by capability

| Area | Implemented and tested | Native status / remaining gate |
|---|---|---|
| Shared contracts | Versioned handles and semantic artifact families, relative artifact paths, content hashes, strict validation, bounded discovery, request journals, public `request_status`, immutable output, transitive retry identity checks | Separate native host lease, process-owned workspace lock, exact terminal reconciliation and permanent supervised abandonment; never steals an active or unmarked lock |
| Material | Canonical rational time; external material; SMF original bytes/ordered evidence; saved Live clip projection; exact-build empty-metadata reader profile retaining whole ALS/note tree; revision-bound edits | Saved-file projection is not native readback. Unknown/populated metadata and different builds retain opaque source evidence and disable strict edits; native-payload export loss remains explicit |
| Native note reader | Separate device build, explicit arrangement reads, optional exact saved-file binding, stable artifact pagination, retained journal status; no candidate prerequisite | Actual Live 12.4.5 reader/CLI/MCP calls matched three known notes. Nine observed note fields only; expression unobserved, sequential reads, no writer route in the reader device |
| Native note writer | Separate public builder/writer, two guarded native checks, durable pending/commit journal, isolated workspace/source validation, exact package binding and terminal readback | Corrected package passed actual three-note insertion, zero-dispatch stale refusal, one velocity change and exact saved/reopened notes in the supervised stock fixture. General edits, controllers, expression, transport and save/render are excluded |
| Native recovery | Public validated-terminal reconciliation and permanent attributed abandonment; retained unknown outcomes, source stamps, leases and journals | Actual failed preparation was closed without saving, then abandoned using a different stopped project/session. Matching lease released; candidate permanently unsealable. Prepared cancellation remains implemented but native-unqualified and unexposed |
| SMF fidelity | Independent wire fixtures at 96/480/9600 PPQ; exact representable export; release, same-tick ordering, sustain/controller evidence, overlaps, malformed framing, rest tails and named degradation | General lossless native route experimentally unsupported: E2 found release, controller-order, tail/meta and fine-time losses plus global map insertion. Remaining E2 profiles unprobed. Mido is optional for SMF I/O |
| Generation and edits | Explicit cells, two alternatives and no-addition baseline; exact shift/pitch/velocity/release/gate/thinning edits; literal additions, ordinary-note split/merge, selected deletion/copies, supplied groove templates, threshold grid timing, exact velocity maps and attributed explicit revoicing; locks, parent restoration reference, expression identity checks | No automatic inference of key, groove, instrument, taste or acceptable sound; no implicit controller copying or clip extension |
| Symbolic relationships | Declared-voice intervals with polyphony/tuning abstentions; distinct-role gate-pair and shared-time measurements, coincident attacks, exact local spans and bounded evidence | Same-clip symbolic records only; no automatic voice/kick assignment, custom tuning interpretation, cross-clip clock inference or acoustic masking claim |
| Sequence construction | Public `material_sequence` translates explicitly placed whole ordinary-note clips, retains immutable parents/rests and complete identity proof, and namespaces occurrences/voices | File-only canonical profile; rejects raw/native bindings, controllers, expression, source maps and same-pitch overlaps; no native arrangement writer |
| Phrase and motif structure | Public `material_structure` create/query; exact material revisions, rational spans, note membership, attributed relationships, declared derivation, bounded retrieval, cycle rejection | File-only organization. No inferred similarity, automatic arrangement execution or musical approval |
| Motif and section development | Public `midi_develop` composes whole-clip repetitions; `midi_arrangement_develop` places explicit graph sections and bounded endpoint variations, with `midi_arrangement_query` for verified bounded section/change pages. Both retain unchanged/A/B/no-addition and locks | Canonical ordinary-note profile only; no inferred motif, perceptual similarity, model-conditioned composition or native arrangement execution |
| Authored audio timing alternatives | Public `midi_timing_alternatives` and `midi_timing_query` bind exact selected notes to active attributed attack/pulse corrections, declared source/host/arrangement clocks and the existing public editor; unchanged/no-addition retain original music | Artifact-only, 1–16 ordinary notes / 1–2 alternatives within proof budgets. No automatic matching, inferred pulse lattice, kick identity, masking improvement, warped clock or native execution. Sequential overlap refusals remain conservative |
| Sustain/lifecycle analysis | Public `midi_lifecycle` interprets declared canonical gates, initial CC64, explicit equal-time ordering and tail reservations; retains unresolved holds and same-pitch ambiguity | Symbolic selected-clip model only; acoustic tail and receiver behavior remain unknown |
| Expression encoding | Public `midi_expression_plan` allocates declared member channels through sustain/tails, refuses exhaustion, orders reset/reuse and records exact pitch/pressure/slide quantization; `midi_export(expression=...)` accepts inline configuration or the independently recomputed plan | File-verified single-clip step profile only; preconfigured receiver is an explicit unverified assumption. No native setup, recorded gesture, stop/seek/loop/cancel/panic or E5 receiver proof |
| Audio hypotheses and corrections | Exact original local bytes/frame crop; public Peek attack/competing-pulse evidence and abstention; optional explicit BeatThis CPU pulse/downbeat hypotheses with checkpoint/runtime/raw-array provenance; attributed immutable corrections and bounded queries for both families | Learned profile has seven synthetic qualification cases, exact rational mapping and retained boundary errors; no accuracy, transcription, stem separation, note/kick/instrument inference or human acceptance. Whole-source limit is 256 MiB; crops alone do not establish full-set coverage |
| Optional learned notes | Separate public known-weight ONNX CPU inspection/note hypotheses, full raw window repeats, licensed base decoder, explicit base/runtime provenance, exact source envelopes and normal attributed query/correction | Two-case built-in finite/repeat check plus separately recorded ten public synthetic path cases; no harmony, bass/instrument identity, measured tuning, native performance or E8 musical acceptance. PCM16/FLOAT32 mono/stereo at 22050/44100/48000 Hz, 2–20 second crop |
| Long-recording passages | Public streaming `audio_region_capture`, direct/inline `audio_region_hypotheses` composition, explicit local/original `audio_region_correct` and bounded `audio_region_query`; exact selected PCM, full original hash at capture and original-frame projections | Separate PCM16/24 and finite-selected FLOAT32 WAV profiles up to two hours / 4 GiB minus one byte; 20-second/32-MiB crop bound. Original is externally retained, not archived by Pocket. Independent synthetic 60-minute capture passed; no whole-set interpretation or musical acceptance |
| Owned audio workers | Optional POSIX submit/status/revision-checked cancel; same public deterministic, learned or mapped-region primitive, inherited execution lease including model child, verified staged graph and serialized publication; region job/v3 owns inline work before long-source hashing; note job/v4 uses the same execution and publication engine | Cooperative boundaries, including 1-MiB streaming capture checks; optional model child has a 60-second timeout, deterministic decoder has no wall-clock guarantee. No native execution; staging is not a committed result |
| Musical time | Public `musical_time` create/query/convert; exact step-tempo integration/inverse, declared clock origins, negative pickups, explicit partial bars, local cycles, identified-render frames and bounded rounding | Declared clocks only. No native timing certification, ramp, warp, groove or loop-occurrence interpretation; no DAW needed for a declared map |
| Curves | Canonical targets, rational points, transforms, ownership and approximation evidence; explicit CC1/CC11 step-to-SMF encoding from embedded or independent curve artifacts, exact timing and declared event order | No automatic sampling or gesture capture; the separate declared expression exporter handles its own narrow per-note step profile. Stock Utility UI experiment preserved step/ramp/pulse through reopen and one-point editing; no public native envelope writer or candidate-profile expansion |
| Instrument and sound planning | Bounded installed metadata inventory; attributed observation import; parameter identities, range/enum/unit/topology checks; opaque preset catalog; stock Operator recipe; sound-only alternatives | Operator `.adv` saved, hashed and loaded separately; displayed controls checked, with 26 small numeric serialization differences retained. Loaded DSP state stayed exact through the writer trial except one observed runtime ID. Native candidate recall verified; no automatic preset or parameter operation |
| Serum specialization | Separate Serum 1, Serum 2 and FX identities; opaque preset indexes; guarded offline parameter plans | Native work deferred. E3/E6/E7 unperformed; no licensed installation, patch compatibility, asset portability, macro receiver or FX route certified |
| Candidate lifecycle | Copied disposable workspaces, source/dependency rechecks, revision conflicts, cancel, supervised import packet, permitted-change validation, immutable v3 seals | Narrow stock fixture's baseline and two 15-note alternatives sealed after native save/reopen and independent exact-note/source checks. Exact-build metadata normalization reviewed. Reports remain attributed; no `candidate_apply` writer |
| Render evidence | Baseline plus bounded comparison plans; exact candidate/render binding; float-WAV frame/rate/channel checks, finite signal, sample peak and RMS; failed evidence retained | Three stock-fixture 28-second renders and one 8-second writer render attached. Separate 8-second Utility pulse experiment decoded; samples equal writer context from 2–8 seconds. All 48-kHz stereo. LUFS/true peak unavailable; rendering never establishes listening |
| Feedback and promotion | Exact render intervals; human/agent attribution; explicit keep; new collected package; optional related MIDI/sidecar/preset evidence; promotion v2 identity checks and Thread file validation | Agent technical keep exercised. Same-machine stock project moved, natively reopened and rendered with sample-exact A output. This separate attributed observation does not change generic provider `native_relocated_reopen:not_verified` or establish human approval |
| Feedback retrieval | Public `audition_feedback_query` validates explicit current feedback handles and exact render/actor/interval filters, with bounded pages and artifact-only relocation | No global store search, preference inference, legacy listener-directory support or invented human verdict |
| CLI/MCP/composition | Both call public providers; real-interface parity tests; independent use; external material substitutes for generation; malformed inputs reject | Requires optional `agent` extra for MCP; no workflow engine required |
| Legacy behavior | Existing Stitch v2/Pipette contracts and MIDI/plugin guards retained; additive public aliases; Baste remains read-only | New file fixtures do not recertify old or new native host behavior |

**Native-verified** here is limited to the supervised stock fixture, separate reader, bounded ordinary-note writer and attributed abandonment in their recorded environment. The catalog's `requires_native_setup` and `native_verified:false` do not certify a current session from a supplied report. **Experimentally unsupported** applies to the measured general-SMF fidelity failures, while unperformed and deferred probes remain distinct.

## Roadmap milestones

Native and musical work is planned in **phases**, and each phase is closed by
one or more **experiments**, labeled E1 to E9. `capabilities_list` uses the same
labels to explain why a capability isn't available yet.

| Label | What it has to establish |
|---|---|
| Phase 0 | Native foundations: preserving a Live project, and reading and writing notes in Live |
| Phase 1 | A stock-instrument layer, built only from Live's own devices |
| Phase 2 | Instrument control, starting with Serum |
| Phase 3 | Automation, per-note expression, time and routing inside Live |
| Phase 4 | Audio-derived material and long-form development |
| Phase 5 | Prepared live performance |
| E1 | A source project survives save and reopen unchanged, and notes can be read and written programmatically |
| E2 | MIDI files survive Live's own import and export without loss |
| E3 | Static instrument parameters can be identified, written and read back |
| E4 | Automation curves can be read, written and owned natively |
| E5 | Per-note expression reaches a real instrument correctly, including sustain, bend range, stop, seek and loop |
| E6 | Saved patches and their assets survive reopen and relocation |
| E7 | Dynamic effects and routing stay correct as the device topology changes |
| E8 | Audio-derived material is musically acceptable on a musician's real recordings |
| E9 | A prepared live performance handles scheduling, panic, takeover and recovery under real load |

A gate is **closed** until its experiment passes. In the table below, *gate*
means one of these milestones. It doesn't mean a note's gate time.

| Phase / experiment | Current disposition | Smallest remaining action |
|---|---|---|
| Phase 0, E1 preservation | Narrow stock fixture passed saved/reopened source checks and independently reviewed normalization | Expand only through new version/fixture evidence; unknown semantic changes must still fail |
| Phase 0, E1 programmatic note access | Public read/build/status and guarded writer implemented. The release package passed three-note insertion, velocity-only edit, zero-dispatch stale refusal and saved/reopened readback. Actual failed preparation exercised permanent supervised abandonment | Current profile is exact-build ordinary notes only; cleanup/source sealing is separately checked. Prepared-only cancellation still needs native qualification |
| Phase 0, E2 fidelity | Offline portion implemented; Core/Fine native probes saved/reopened/exported; full-fidelity route failed | Retain declared native losses and restricted production profile; remaining PPQ/overlap/expression/SysEx cases need separate probes |
| Phase 1, stock layer | Technical loop exercised on one synthetic stock fixture, including seal, render attachment, technical promotion and relocated reopen/render | Listening to candidate renders on real musical passages is still required. Source-derived crops are not candidate renders, and a technical keep is not a musical decision |
| Phase 2, E3 static Serum control | Deferred | When requested, use the musician's licensed installation and pin build/format; qualify actual descriptor identity, native values and readback before exposing writes |
| Phase 2, E6 patch/assets | Deferred with native Serum | Test unique saved copies and actual assets through reopen and relocation; label same-environment coverage until verified |
| Phase 2/3, E7 dynamic FX / Serum FX | Deferred; offline topology invalidation tested | Test duplicate FX identities, topology changes and a separately identified audio-input FX route |
| Phase 3, E4 automation | Explicit CC1/CC11 file export implemented. Supervised stock Utility step/ramp/pulse and single-point revision saved/reopened; ownership active→overridden→active observed. Clean pulse rendered; no listening. Later disposable scalar-probe startup remained unresolved, with no parameter read or setter dispatched | Serum macro test deferred. Recorded gesture, native parameter-domain mapping and public envelope/candidate authorization remain unqualified; the stock UI experiment does not close E4. One disposable copy showed an unexplained change to its file metadata time only; that caveat stays attached to the evidence |
| Phase 3, E5 expression | Canonical identities, symbolic sustain/tails and declared member-channel step planning/SMF encoding implemented and independently file-verified; receiver route remains unqualified | Verify two-note independence, sustain/tails, reset/reuse, stop/seek/loop/cancel, bend ranges, capture clock and native round trip |
| Phase 3, time/routing | Exact declared step-tempo/seconds/frame conversion, partial bars/local cycles and retained imported maps implemented; native profile restricted to constant 120 BPM/4/4 | Qualify native tempo/meter changes, looping, groove and routing/sidechain changes; no implicit profile expansion |
| Phase 4, E8 audio-derived material | Deterministic attack/pulse and optional explicit BeatThis learned-pulse hypotheses, exact source/model provenance, attributed corrections and optional owned local workers implemented; narrow optional Basic Pitch ONNX note hypotheses now implemented with retained window tensors and artifact-only correction; separation absent | Evaluate on a musician's own isolated bass, drums and dense mixes, measuring source-frame alignment, uncertainty and correction cost |
| Phase 4, long-form development | Explicit phrase/motif graph, constrained whole-clip endpoint development and supplied multi-section construction implemented; bounded section/change queries, source/lock proofs and attribution retained | Model-conditioned development, inferred motif discovery, native arrangement execution and musical usefulness remain unqualified |
| Phase 5, E9 prepared performance | Unsupported/not implemented | Establish scheduling, panic, takeover and recovery, then run the specified 30-minute soak under actual load before performance claims |

The feature set remains incomplete while required gates remain open. Source-preservation failure blocks native delivery regardless of whether a different import route appears to work.

Remaining file implementation includes source separation, broader transcription profiles and model-conditioned development beyond explicit corrected-audio timing. The optional note adapter has a separate known-weight ONNX CPU profile and a two-case built-in finite/repeatability check; musical accuracy and E8 acceptance remain open. Long-source passage capture and mapped analysis are implemented within their separate narrow PCM16/24 and IEEE FLOAT32 profiles. The optional learned-pulse adapter passes its seven-case qualification, and direct, worker, CLI and stdio-MCP calls produce equivalent artifacts. That qualifies only the recorded local profile and synthetic source. It is not a general timing or accuracy guarantee. Explicit supplied multi-section development is implemented within the documented ordinary-note and proof budgets. Deterministic audio hypotheses/corrections, owned local workers, declared member-channel allocation and exact step encoding are implemented; native receiver qualification remains open. Explicit supplied harmonic revoicing is implemented; automatic harmonic interpretation and voice leading remain unavailable. These are implementation gaps, not capabilities waiting only for Serum.

## Five worked scenarios

These five scenarios guided the design of the MIDI and sound tools. They're hypothetical, so their proposed outputs and listening judgments are not implementation evidence. The table shows how far today's tools reach toward each one.

| Example | Reusable capability available now | What remains unavailable / acceptance status |
|---|---|---|
| Evolving percussion over audio | Explicit cells, variations, exact edits, no-addition baseline, stock sound plan, exact-frame attack/pulse hypotheses with attributed corrections, supervised candidate/render/feedback tools; one synthetic stock technical loop completed | Three actual musical passages and human judgment pending; Serum variant deferred; macro curve is a plan only |
| Bass leaving room for the kick | User-supplied anchors/pitches, uncertain optional note hypotheses with explicit correction, external material import, declared-role gate relationships, bounded shifts/grid edits and explicit corrected-audio timing alternatives with pitch/velocity locks, independent sound plans | No kick detector or source interpretation is asserted. Symbolic gates do not establish acoustic overlap. Serum state application, 2×2 native comparison, sidechain change and contextual listening pending/deferred |
| Later-section patch variation | Material revision/lineage, exact-note locks, whole-clip sequence/development and explicitly placed graph sections with exact locks, descriptor-bound static alternatives and opaque preset catalog | Native arrangement execution, rich-expression occurrence construction, dirty-state checkpoint/restore, native patch save and existing-automation preservation route unqualified; Serum deferred |
| Independent expressive movement | Canonical curves, independently callable lifecycle/member-channel plan and exact SMF step realization; executable two-note pitch/pressure example, source/quantization/reset proof; separate CC1/CC11 route | Receiver setup, audible independence, native persistence, performed gestures and transport recovery remain unqualified; E4/E5 and actual listening required |
| Compare sound and performance combinations | Independent material/sound identities, bounded candidate lists, baseline comparison plan, exact attachments, feedback and explicit promotion | Six applied Serum combinations, blind playback, repeated stochastic render assessment and relocated native reopen unqualified; Serum deferred; no listener decision invented |

## Evidence and reproduction

The five layers remain separate: **artifact integrity; native save/reopen verification; rendered audio and measurements; actual human listening; an attributed musical decision.** Automated tests establish only the file/validation behavior they exercise. Synthetic native reports in tests exercise report validation and must never be presented as completed Live sessions.

Run the independent focused checks from the repository with the project's test environment:

```sh
python -m pytest -q tests/test_midi_qa.py tests/test_midi_interfaces.py
python -m pytest -q tests/test_instrument_qa.py tests/test_candidate_interfaces.py
python -m pytest -q tests/test_native_candidates.py tests/test_auditions.py tests/test_native_normalization.py tests/test_native_normalization_qa.py
python -m pytest -q tests/test_time_maps.py tests/test_time_qa.py
python -m pytest -q tests/test_material_structure.py tests/test_material_structure_qa.py tests/test_structure_interfaces.py
python -m pytest -q tests/test_midi_relationships.py tests/test_midi_relationships_qa.py tests/test_midi_edit_extensions.py tests/test_midi_edit_extensions_qa.py tests/test_midi_extended_interfaces.py
python -m pytest -q tests/test_midi_note_construction.py tests/test_midi_construction_qa.py tests/test_midi_construction_interfaces.py tests/test_midi_groove.py tests/test_midi_groove_qa.py
python -m pytest -q tests/test_material_sequence.py tests/test_material_sequence_qa.py tests/test_material_sequence_interfaces.py tests/test_midi_revoice.py tests/test_midi_revoice_qa.py
python -m pytest -q tests/test_midi_develop.py tests/test_midi_develop_qa.py tests/test_midi_lifecycle.py tests/test_midi_lifecycle_qa.py tests/test_midi_lifecycle_interfaces.py
python -m pytest -q tests/test_arrangement_develop.py tests/test_arrangement_develop_qa.py tests/test_arrangement_interfaces.py
python -m pytest -q tests/test_midi_timing_alternatives.py tests/test_midi_timing_alternatives_qa.py tests/test_midi_timing_interfaces.py
python -m pytest -q tests/test_midi_expression.py tests/test_midi_expression_qa.py tests/test_midi_expression_interfaces.py tests/test_midi_expression_export.py tests/test_midi_expression_export_qa.py
python -m pytest -q tests/test_audio_hypotheses.py tests/test_audio_hypotheses_qa.py tests/test_audio_hypotheses_interfaces.py tests/test_audio_hypothesis_jobs.py tests/test_audio_hypothesis_jobs_qa.py tests/test_audio_hypothesis_jobs_interfaces.py tests/test_new_wave_discovery.py
python -m pytest -q tests/test_audio_regions.py tests/test_audio_regions_qa.py tests/test_audio_region_analysis.py tests/test_audio_region_analysis_qa.py tests/test_audio_region_corrections.py tests/test_audio_region_corrections_qa.py tests/test_audio_region_jobs.py tests/test_audio_region_jobs_qa.py tests/test_region_feedback_interfaces.py
python -m pytest -q tests/test_feedback_query.py tests/test_feedback_query_qa.py tests/test_audio_models.py tests/test_audio_models_qa.py tests/test_audio_model_corrections.py tests/test_audio_model_corrections_qa.py tests/test_audio_pulse_jobs_qa.py tests/test_audio_pulse_interfaces.py
```

The first suite uses independently written external material, raw SMF fixtures and a separate wire decoder, then exercises actual CLI and stdio MCP. Independent instrument/curve and candidate interface checks complement hand-written candidate, attachment and promotion failure-mode tests. Interface tests skip MCP only when its optional dependency is absent. Follow the [example recipe](../examples/midi-workflow/README.md) for a disposable, host-independent run.

Keep actual environment inventories, source paths, projects, presets, renders and real listening notes in the ignored `private/` folder, never in repository fixtures. A future native run must record its exact environment, fixture, procedure, observed result, evidence identities and resulting capability decision. Rejected alternatives and a decision to leave the source alone remain valid outcomes.

The optional note profile uses complete retained ONNX window repeats, a licensed base-only decoder, explicit float timing and outward source envelopes. It does not establish a bass instrument, key, tuning, kick identity or native note performance. `audio_note_model_inspect`, `audio_note_hypotheses` and `audio_note_submit` share normal CLI/MCP providers; existing query/correction and mapped-region tools accept the new family. No optional runtime is required for retained queries. See [the exact profile](midi.md#optional-note-hypotheses).
