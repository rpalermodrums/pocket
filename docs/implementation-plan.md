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

Each milestone will update the evaluation/release documentation with implemented behavior, checks, remaining limits and its commit boundary. A successful technical diagnostic is not a musical audition.
