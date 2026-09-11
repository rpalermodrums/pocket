# Field-test implementation milestones

This pass implements the five needs identified by the private real-set field test, plus minimal feedback retrieval. The stopping point is a reproducible native opening trial, bounded source-window queries, and rhythm reports that expose the observed phase/count failures without authorizing automatic edits.

## Work and integration order

1. **Dependable native trials:** typed agent inputs, collected native candidates with explicit dependency lineage, and distinct artifact/signal/readiness outcomes. Preserve silent failures, allow intentional silence explicitly, and verify a relocated production trial in Live.
2. **Timestamp entry point:** identity-bound saved-map handles, compact clip discovery and seconds-based region queries, relevant control coverage, and explicit conversion to complete source frames. Reject stale evidence and genuine out-of-range or unsupported mappings.
3. **Rhythm uncertainty:** competing acoustic phases, overlapping-crop stability, local phase/count continuity, and clear abstention. Add feedback retrieval bound to exact outputs, intervals and scopes. Musical bar position remains unresolved without applicable evidence.

The review player, broader MIDI/tempo editing, native Save As lineage, transcription/stems and provider/model evaluation remain subsequent milestones.

## Parallel ownership

| Workstream | Owns | Does not edit |
|---|---|---|
| Native trials | `transition_lab.py`, native input types, native tests, Transition Lab docs | CLI/MCP adapters, package metadata, set or rhythm providers |
| Bounded queries | Set Map/query/frame helpers, related tests, Set Map docs | CLI/MCP adapters, native/rhythm providers, package metadata |
| Rhythm continuity | Track Map/rhythm helpers, related tests, Track Map docs | Native/set providers, adapters, package metadata |
| Integrator | CLI/MCP adapters, end-to-end interface tests, feedback lookup, package metadata, README/skill/evaluation/release docs | Other owned files while their workstream is active |

Each workstream runs in its own branch/worktree. Contributors make focused local commits and report their hashes and checks. Only the integrator merges into and pushes `main`, after reviewing staged content and running relevant tests. Completed tested slices may ship independently; integration follows the order above. No force-pushes, broad staging, destructive cleanup or concurrent edits on a shared branch. Private media, full maps, local paths and listening notes stay outside Git.

## Acceptance gates

- Actual stdio discovery and schema-conforming calls exercise every public tool; nested lists/settings have usable types. Invalid calls create no partial outputs.
- A newly prepared collected trial survives relocation, opens in Live with no missing active files and produces the declared non-silent native excerpt. Signal validity, declared export lineage, native observations and listening judgment remain separate.
- A silent expected-music export cannot be described as ready to compare. Preserve its identity and evidence; deliberate silence requires explicit intent.
- A normal 32-second transition query returns under 25 KB of JSON using a small handle request, including relevant clips, source frames, mapping limitations and controls. Heavy raw state is opt-in. Stale project or source files invalidate the handle.
- Near-zero native rounding is resolved under a documented sample-scale policy; genuine negative/out-of-file intervals remain explicit. Natural-time and layered/MIDI regions are covered.
- Perc One's crop-dependent half-pulse selection and Oceans' documented local count event are exposed or trigger explicit uncertainty. Synthetic public regressions reproduce their mechanics; private source checks include crop-stable controls and previously unused passages. No scalar tempo/score authorizes a media move.
- Feedback retrieval preserves exact hash/interval/scope applicability and conflicting listener claims.
- Relevant tests, lint and integrated interface checks pass before each implementation push. Native observations and private evaluation receipts remain separate from public generated fixtures.

## Completion record

All three planned milestones are implemented and integrated. [Evaluation](evaluation.md) records the production acceptance; [0.2 release notes](releases/0.2.0.md) describe the contracts and migration path.

| Milestone | Integration evidence |
|---|---|
| Dependable native trials | Typed tools, collected v2 preparation/validation, independent artifact/signal/operator labels and scoped feedback retrieval. Pushed boundaries include `63fcb0d` and `ccfc5e5`. One relocated production candidate opened without missing files and completed its exact native excerpt. |
| Timestamp entry point | Provider work `0e4b497`, CLI/MCP integration `7bca10c`. Three real region responses are under 25 KB on the actual connection; five source intervals pass exact-frame analysis without endpoint changes. |
| Rhythm uncertainty | Provider work `ad6ec98`, followed by the sub-frame tonal-tail fix `dcd4c92`, midpoint-scope correction `e31e5be` and exact-frame interface integration `5eca9f8`. Competing phases and local uncertainty remain evidence for review, never a clip-move instruction. |

Contributors used separate worktrees and focused local commits. One integrator reviewed and merged exact commits into `main`; no force pushes or concurrent branch edits were used. Public generated tests and private source evidence remain separate. The native workspace/export state and original media were preserved.

This cycle stops here. The next step is a bounded listening comparison on an unfamiliar transition, using the saved correction scopes to judge whether the richer evidence improves the musical choice. A compact Track Map view and phase checks across the full crop are candidates for the next usability/analysis cycle. Review-player UI, broad MIDI/tempo editing, native Save As lineage, transcription/stems and provider/model evaluation remain deferred.

A successful technical diagnostic is not a musical audition.
