# Foundation implementation status

This is a record of implemented boundaries and remaining work, separate from the
broader proposed v1 architecture. The north star is a general agentic music toolkit,
including a responsive jazz practice partner after v1. Reliable tools for exact
musical questions come first; neither a DAW nor any genre defines the core.

## Current crosswalk

| Area | Decision | Implemented now | Remaining boundary |
|---|---|---|---|
| Artifact identity, immutable publication, request journals | Keep | Existing store, hashes, graph integrity bounds and replay rules reused. | Structured error codes can be introduced through a versioned compatibility plan. |
| Rational coordinates | Extract | `music_types.Rational` and `coordinates` provide neutral shared types/validation. Old `material_types.Rational` is the same object; `time_maps` keeps its helper import names. | General tuning/pitch and score/form types should follow concrete uses. |
| Time conversion | Adapt | Musical context scopes clocks and converts through existing exact step-tempo maps. Explicit affine occurrences bridge original source frames and timeline quarter notes. | Thread's legacy float linear-BPM and warp profiles remain separate. A qualified bridge must declare numerical tolerance and supported mappings. |
| Musical workspace | Add | Immutable `pocket.musical-context/v1`: retained sources, symbolic materials, clocks, occurrences, attributed internal anchors and parent lineage. | A revision-checked shared head/session adapter; typed references to chosen model hypotheses; score/form context. |
| Audio realization | Compose | Existing exact region capture → explicit occurrence sequence → original-rate DOUBLE WAV → independent source-sample revalidation. | Time stretch, pitch, layering, envelopes and explicit count-in providers. |
| Evidence and feedback | Extract | Shared `audio_evidence` signal measurement and report validation used by native auditions and standalone practice. | More native candidate lifecycle extraction only when a second implementation needs it. Native XML/save/export/promotion stay native. |
| API surfaces | Keep and extend | Seven registered public providers with Python/CLI/MCP parity, strict nested transport types, bounded query/discovery and request journals. | The proposed 49-operation facade still needs an explicit migration/versioning decision before publication. No HTTP server added. |
| Selection workspace | Preserve | Existing `pocket workspace`, `pocket.workspace/v1`, Weave and Whisker behavior. | Connect selection resources through an explicit adapter if a workflow needs them. |

## Compatibility decisions

1. New record families are additive. Existing serialized schemas, source clocks,
   receipt statuses, tool names and aliases retain their meanings.
2. A proposed occurrence map is musical intent. Successful coordinate conversion
   does not certify an audio edit, native execution, beat-one correctness or listening.
3. Native audition measurements now call shared code while their qualified
   120 BPM/4/4 export requirements and saved-artifact formats stay in the adapter.
4. `context_create(parent=...)` makes a branchable immutable revision. It does not
   pretend to offer concurrency protection for an interactive mutable workspace.
5. Existing transient/runtime Live identities are not promoted into core identities.
6. Technical fixtures use generated audio. The repository contains no personal
   recordings, exported sets or real listener feedback from this work.

## Acceptance supplied by the implementation

- Independent coordinate fixtures: a nonzero capture offset, an internal anchor,
  two occurrences of the same passage, exact forward/inverse conversion and
  refusal of ambiguous occurrence, wrong clock, fractional source frame or stale hash.
- End-to-end standalone path: capture → context → passage/repetition render →
  comparison → attributed agent report → readback after relocation and removal of
  the original external source.
- Adversarial evidence: changed source invalidates replay; forged mapping and
  substituted audio fail even when new artifact hashes are internally consistent.
- Render-profile boundary: every tempo step is checked, including opposite tempo
  changes that have the correct total duration but still require internal warping.
- Real CLI and MCP exchanges: typed discovery, mutation, query, conflict and
  ambiguity rejection agree with direct Python providers.
- Regression checks cover existing time maps and native auditions after extraction.

These checks establish technical behavior. The generated fixture is not the
required acoustic musical acceptance example, native qualification or human listening.

### Generated-fixture checkpoint — 2026-09-23

| Check | Result |
|---|---|
| Full repository regression run | 3,244 tests and 21 subtests passed. |
| Final context/practice and transport checks | 26 tests passed, including the additional retained-record and 150 BPM cases. |
| Lint for changed Python modules, tests and example | Passed. |
| Documented standalone demo and generated CLI spec | Executed successfully in a temporary directory. |
| Isolated wheel build | Passed; all seven providers and the unchanged Rational import alias verified from the wheel. |
| Documentation links and diff whitespace | Passed. |

The user subsequently selected Fee-Fi-Fo-Fum. The [reference recording check](reference-foundation-check.md)
passed exact-sample, occurrence, replay, interface and relocation checks on three
passages. Two comparisons were signal-ready; an overloaded passage remained
flagged. Human listening and native musical acceptance remain pending.

The [next-phase implementation plan](next-phase-plan.md) now turns the sequence
below into six scoped deliveries with proposed contracts, dependencies and tests.

## Next implementation sequence

The [next-phase plan](next-phase-plan.md) is the current delivery authority:

1. **P1: interpretation binding.** Source-bound typed choices preserve detector
   abstention, competing hypotheses and authored corrections.
2. **P2: explicit context edits.** Distinguish source-window slips from timeline
   placement shifts; retain parents, locks and exact linked selections.
3. **P3: comparisons across revisions.** Add explicit baseline lineage and
   occurrence correspondence without weakening v1's exact-context comparison.
4. **P4: feedback retrieval.** Reuse bounded selection and pagination while
   preserving contradictory reports, actor identity and listening intervals.
5. **P5: one audio derivative.** Human listening chooses a join envelope, count-in,
   gain or other narrowly justified profile. This decision does not block P1–P4.
6. **P6: contract publication and acceptance.** Generate actual tool contracts
   from the implementation, verify packaged examples and recovery behavior.

The linked native handover adapter is a subsequent qualified milestone. Translate
Thread's source/warp mappings explicitly, preserve the original set, and qualify
save/reopen/export separately. Acoustic musical acceptance also remains pending;
neither technical passage probes nor native receipts substitute for listening.

No full package move, database replacement, distributed service, model expansion
or real-time engine is needed to complete these steps. Live following, improvising
roles and responsive accompaniment are later systems with different timing and
musical acceptance requirements.
