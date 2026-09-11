# Initial evaluation

This is a working first implementation of the two maps and Transition Lab. It has been exercised on generated fixtures and local production files. Musical understanding and native export automation are not claimed.

## What the checks establish

| Area | Evidence |
|---|---|
| Source identity | Stable hashes survive renaming and distinguish different recordings with the same filename. Invalid or changing sources fail explicitly. |
| Track Map | Generated clicks test original-source coordinates, pulse rate, half/double alternatives, local acceleration and abstention. Silence, noise, anti-phase stereo and a known pitch-class fixture exercise failure cases. |
| Set Map | Generated Live-shaped fixtures test warp inversion, negative pickups, natural timing through BPM ramps, target-linked automation, duplicate IDs, missing dependencies and unsupported cases. |
| Transition Lab | Generated trials test exact zero-gain samples, explicit gain, frame boundaries, stale hashes, immutable output destinations and narrowly scoped feedback. Native preparation tests reject unsupported edits and prove the declared XML-only change. |
| Native render attachment | A declared completed float WAV must match the candidate, dependencies, settings and requested duration within two frames. Actual decoding and file identity are checked; Live operation attribution remains supplied by the caller. |
| Interfaces | CLI results use the same library records. A real local MCP stdio exchange initializes the server, lists tools, calls a provider and checks error propagation. |

No recording, source-specific report or personal listening note is part of the repository's fixtures. Tests generate their media and XML in temporary directories. The synthetic XML fixtures test supported document semantics; they are not full native Ableton project templates.

## Local production checks

Set Map inspected three saved production revisions containing 26, 26 and 21 audio clips. The source set files remained unchanged. Across 365 sampled forward/inverse mapping checks, the maximum round-trip discrepancy was approximately 1.82e-12 beats. No missing runtime media or Max references were reported in those checks. This verifies internal coordinate consistency and supported source resolution, not native DSP accuracy or musical bar position.

Track Map ran on three existing 30-second music passages. Two crop-wide estimates were near 120 BPM. The third favored roughly 246 BPM with a 123 BPM half-count alternative; some local windows also switched counting rate. The report exposes those ambiguities and keeps all musical bar orientations unresolved. These passages are development examples, not a held-out benchmark or proof that the previous disputed alignment is solved.

Transition Lab also extracts exact comparison windows from existing production renders. The zero-gain sample comparison is checked against the parent audio. These new excerpts do not inherit approval from a different trial or imply that anyone has auditioned them.

## Limits to test with our ears

Track Map's attack detector and tonal summaries can miss soft attacks, confuse percussion with pitch, favor subdivisions, or mistake similar texture for a repeated phrase. It does not transcribe notes or infer a definitive key. A tempo hint can express a counting preference; it cannot create missing evidence.

Set Map deliberately returns unknown for unsupported source-time semantics. It does not execute plugin DSP, rack selection, sidechains or groove timing. Its complete saved-state report is best written to a local file rather than repeatedly pasted into an agent's context.

The native adapter prepares a bounded media-only shift with host controls fixed. It does not automatically control Live, evaluate the sound, or handle arbitrary MIDI/tempo/edit operations. A completed-render attachment verifies an audio artifact and declared lineage; it cannot independently prove which project Live rendered.

The next useful trial is an unfamiliar transition: compare explicit hypotheses, record which representation helped, and preserve a listener's correction without broadening its scope. That evidence should determine whether better beat providers, selective transcription, MuseTok or broader native tooling comes next.
