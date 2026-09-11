# Evaluation

Current cycle: [Baste/Pipette acceptance](baste-pipette-acceptance.md) records 0.4
automated, installed-package and native checks with their remaining gates. The
0.2 results below are historical evidence for the earlier saved-project workflow.

Pocket 0.2 implements the five needs from the private real-set field test plus bounded feedback retrieval. The evidence below distinguishes generated regressions, production-file checks, native operator observations and musical judgments. No new musical audition or autonomous native export capability is claimed.

## Version 0.2 field-test acceptance

| Gate | Observed result |
|---|---|
| Typed agent contracts | Actual MCP stdio discovery and calls cover every public tool, including nested trial variants, handles, export settings and native observations. Invalid calls fail without creating partial trials. |
| Collected native project | One real opening-transition candidate collected 26 audio files plus a Max device and its adjacent patch. After physical relocation, all 27 saved absolute hints were stale; validation passed and Live reported no missing or external files. |
| Native signal/readiness | The relocated candidate produced a completed 32-second, 48 kHz stereo float WAV: 1,536,000 frames, about −5.08 dBFS sample peak and −19.03 dBFS RMS, no non-finite samples or sample overload. Attachment became ready only after usable signal and the attributed loading/export observations. Exact zero-gain comparison samples match their parent renders. |
| Timestamp queries | Three production 32-second regions returned complete serialized MCP results of 15,500, 8,168 and 17,802 bytes. Requests were under 450 bytes and region calls took 0.049–0.061 seconds on the development machine. Regions include the relevant clips and control tracks; dense event lists disclose truncation. This is observed performance, not a universal latency guarantee. |
| Exact frame handoff | Five valid audio intervals across warped, natural and layered production regions went from Thread to Peek through MCP without changing either frame endpoint. Generated tests also cover tiny file-boundary overshoot, true out-of-range requests and stale handles. |
| Rhythm failure visibility | Thirteen paired calls over seven local sources expose the known crop-sensitive half-pulse alternatives and a localized count/phase uncertainty despite a steady crop-wide rate. Stable acoustic development controls remain distinguished; overlapping-crop agreement is explicitly scoped to the tested midpoints, not the edges; a tight event-only crop and natural timing can abstain. No corrected integer count or musical downbeat is asserted. |
| Scoped feedback | Sealed queries filter exact output identity, frame overlap and scope, retaining original text/ranges and contradictory claims. Stale outputs and altered notes are rejected. |

The native workspace and its prior export settings were restored. Source project/audio hashes remained unchanged. A range-label variable-shadowing bug was found before the first candidate was rendered; its regression preceded a freshly prepared final candidate. Private evidence retains both the unrendered failed draft and the corrected trial. A single-source-frame trailing tonal window found during the end-to-end handoff now returns explicit insufficient evidence instead of evaluating an empty analysis slice.

The rhythm additions cost more analysis and output. In the 13-call paired development corpus, median runtime rose from 0.50 to 1.85 seconds and median JSON size from about 111 to 171 KB; the slowest new call took 2.41 seconds. These are full Peek reports, not the bounded Thread queries covered by the 25 KB gate. Save full analyses outside agent context and retain only the evidence needed for the current hypothesis. The previously unused passages are useful checks, but the corpus is not an independent benchmark with human downbeat annotations.

The implementation followed isolated branches with one integrator, ordered merges and tested milestone pushes. The integrated local suite passes 163 tests and three subtests with runtime warnings treated as errors; lint passes. Generated fixtures also run in CI on Python 3.11, 3.12 and 3.14. The owner's delivery receipt records final CI, wheel checks and private source identities.

## Earlier baseline checks

## What the checks establish

| Area | Evidence |
|---|---|
| Source identity | Stable hashes survive renaming and distinguish different recordings with the same filename. Invalid or changing sources fail explicitly. |
| Peek | Generated clicks test original-source coordinates, pulse rate, half/double alternatives, local acceleration and abstention. Silence, noise, anti-phase stereo and a known pitch-class fixture exercise failure cases. |
| Thread | Generated Live-shaped fixtures test warp inversion, negative pickups, natural timing through BPM ramps, target-linked automation, duplicate IDs, missing dependencies and unsupported cases. |
| Stitch | Generated trials test exact zero-gain samples, explicit gain, frame boundaries, stale hashes, immutable output destinations and narrowly scoped feedback. Native preparation tests reject unsupported edits and prove the declared XML-only change. |
| Native render attachment | A declared completed float WAV must match the candidate, dependencies, settings and requested duration within two frames. Decoding, signal disposition and file identity are checked; Live operation attribution remains supplied by the caller. |
| Interfaces | CLI results use the same library records. Actual local MCP stdio exchanges cover tool discovery, the complete supported workflow and error propagation. |

No recording, source-specific report or personal listening note is part of the repository's fixtures. Tests generate their media and XML in temporary directories. The synthetic XML fixtures test supported document semantics; they are not full native Ableton project templates.

## Local production checks

Thread inspected three saved production revisions containing 26, 26 and 21 audio clips. The source set files remained unchanged. Across 365 sampled forward/inverse mapping checks, the maximum round-trip discrepancy was approximately 1.82e-12 beats. No missing runtime media or Max references were reported in those checks. This verifies internal coordinate consistency and supported source resolution, not native DSP accuracy or musical bar position.

Peek ran on three existing 30-second music passages. Two crop-wide estimates were near 120 BPM. The third favored roughly 246 BPM with a 123 BPM half-count alternative; some local windows also switched counting rate. The report exposes those ambiguities and keeps all musical bar orientations unresolved. These passages are development examples, not a held-out benchmark or proof that the previous disputed alignment is solved.

Stitch also extracts exact comparison windows from existing production renders. The zero-gain sample comparison is checked against the parent audio. These new excerpts do not inherit approval from a different trial or imply that anyone has auditioned them.

## Limits to test with our ears

Peek's attack detector and tonal summaries can miss soft attacks, confuse percussion with pitch, favor subdivisions, or mistake similar texture for a repeated phrase. It does not transcribe notes or infer a definitive key. A tempo hint can express a counting preference; it cannot create missing evidence.

Thread deliberately returns unknown for unsupported source-time semantics. It does not execute plugin DSP, rack selection, sidechains or groove timing. Its complete saved-state report is best written to a local file rather than repeatedly pasted into an agent's context.

The native adapter prepares a bounded media-only shift with host controls fixed. It does not automatically control Live, evaluate the sound, or handle arbitrary MIDI/tempo/edit operations. A completed-render attachment verifies an audio artifact and declared lineage; it cannot independently prove which project Live rendered. Collecting the supported Max files does not discover dependencies hidden inside a patch. Creating a new trial from an already relocated source whose absolute references are stale requires deliberate relinking first; validation and attachment of the collected trial itself work after relocation.

The next useful trial is an unfamiliar transition: compare explicit hypotheses, record which representation helped, and preserve a listener's correction without broadening its scope. That evidence should determine whether better beat providers, selective transcription, MuseTok or broader native tooling comes next.
