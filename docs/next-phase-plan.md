# Next phase: evidence-bound practice edits

## Outcome

Given a recorded performance, select an explicit musical interpretation, make a
small reversible passage edit, compare it with the unchanged baseline, and retain
the musician's exact feedback. The workflow must remain usable without a DAW or
model installation. This phase advances Pocket's general music toolkit; responsive
ensemble playing and a living jazz practice partner remain post-v1 work.

The foundation now works end to end on generated fixtures and the user-selected
Fee-Fi-Fo-Fum recording. [Reference findings](reference-foundation-check.md) show
two useful design pressures: analysis can abstain or disagree, and valid original
audio can carry signal warnings. Neither should disappear when an agent edits.

## Baseline we will preserve

- Existing public providers and the seven new context/practice tools are callable
  through Python, CLI and MCP. The registry remains the shared implementation path.
- Immutable artifacts, source hashes, explicit clocks and request journals remain
  the foundation. Existing schema meanings and receipt statuses stay intact.
- `practice_render` remains the original-rate exact-sample profile. A later DSP
  profile must not silently broaden its evidence claim.
- `practice_compare` v1 retains its exact-context restriction. Cross-revision
  comparison gets a new provider/record with explicit lineage and correspondence.
- Native candidate preparation, export and promotion retain their qualified
  profiles. No Live, Serum, Splice or model expansion is implied by this phase.
- The 49-operation proposed facade is a design reference, not the current callable
  surface. Do not build a second implementation just to match those names.

## Ordered delivery plan

Each row is a reviewable implementation change, with its own tests and docs.
Names below are proposed additions until that change is implemented and verified.

| ID | Deliverable | Depends on | Status | Exit evidence |
|---|---|---|---|---|
| P1 | Typed musical interpretation selection and context binding | Foundation | Ready | Wrong source, stale annotation, ambiguity and abstention fixtures; direct/CLI/MCP parity |
| P2 | Explicit context edits with immutable change receipts | P1 | Queued | A source-window slip and an occurrence placement shift remain distinguishable; linked selections and locks hold |
| P3 | Comparison across context revisions | P2 | Queued | Untouched baseline, explicit correspondence and source/output mappings verified across a child edit |
| P4 | Shared bounded feedback retrieval | P3 | Queued | Conflicting human/agent reports remain separate; interval/identity filters and pagination work |
| P5 | One explicit audio derivative profile | P3; musical experiment selected | Decision at implementation boundary | Original retained, processing declared, independent numerical oracle and listening comparison |
| P6 | Contract publication and agent acceptance | P1–P4; P5 if included | Queued | One generated contract source, accurate discovery, packaged examples and recovery walkthrough |

P1 → P2 → P3 is the next critical path. P4 and P6's documentation work can follow
P3 independently of the listening-driven P5 choice. The native linked-handover
adapter follows this standalone path as a separate qualified milestone.

## P1 — Bind interpretations to actual evidence

**Problem:** the reference exercise preserved competing analysis, but linked a
selected attack to a context through text. That is readable but not enforceable.
The opening had no pulse candidate; an agent must not convert a fallback clock
into an inferred tempo or an onset into beat one.

**Public operations, proposed:**

```text
interpretation_create(store_root, request_id, context, source_clock_id,
                      claim, attribution, evidence?)
interpretation_query(store_root, interpretation)
context_bind_interpretation(store_root, request_id, context, interpretation,
                            binding, attribution)
```

Use closed tagged claims, initially `onset`, `pulse`, `bar_one` and `phrase_start`.
Evidence identifies an exact `audio-region-hypotheses` revision and annotation ID;
authored-only claims explicitly say so and carry uncertainty. Preserve original
source coordinates and the selected source clock. A new selection record does not
modify the analysis, promote confidence to truth, or apply a tempo grid.

Separate three concepts in the schema: detector claim, selected interpretation,
and declared coordinate clock. A detector onset can support an authored bar-one
interpretation, but cannot certify it. A selection from a nonempty candidate list
must identify the candidate. An abstention can support “unresolved,” not a guessed
tempo. Binding creates an immutable child context; it does not rewrite the parent.

**Implementation locations:** new `interpretation_types.py` and
`interpretations.py`; reuse `audio_region_query`, retained hypothesis validation,
`musical_context`, `artifact_store` and `capabilities`. Context v1 stays readable.
If a new context field is required, introduce an explicit v2 schema with a
lossless v1 reader/migration; avoid ambiguous “sometimes present” v1 semantics.

**Tests:** exact nonzero crop offset; fractional model frame coordinates retained;
wrong recording with the same filename; wrong crop of the same recording;
annotation from another revision; corrected/superseded candidate retained;
detector abstention; conflicting selections; authored bar one with unresolved
meter; no automatic tempo-map edit; retries and transport parity.

**Done means:** replace the reference recipe's textual selection link with this
public operation while retaining all competing evidence and original audio.

## P2 — Make small edits through an explicit provider

**Public operation, proposed:**

```text
context_edit(store_root, request_id, context, operations, locks, attribution)
```

Start with three closed operations: `anchor_rebind`, `occurrence_slip_source` and
`occurrence_shift_timeline`. Source slip changes source frame bounds while
preserving duration and timeline placement. Timeline shift changes placement
while preserving the source window and length. Require explicit occurrence IDs;
linked edits carry the exact list to change, not an implicit “move everything.”

Return child context plus a versioned edit receipt containing the exact parent,
operations, changed paths, preserved-field proof and source/clock mappings.
Untargeted occurrences, anchors, materials and source identities remain equal.
Changes that require another render profile may be represented as intent but
must be clearly reported as unrealizable by `practice_render`.

**Implementation:** new context edit types/provider; reuse context validation,
the shared journal and pure data changes. Do not invoke a host or rewrite media.
Keep branchable immutable contexts; defer a shared mutable head/CAS session until
an actual interactive client needs it.

**Tests:** one-beat timeline shift with a nonzero internal cue; 50 ms source slip;
wrong occurrence; range overflow; linked edit versus isolated edit; fractional
source frame refusal; negative pickup coordinates where the map permits them;
protected-field drift; request-ID conflict; original parent revalidation.

## P3 — Compare an edit with its actual baseline

**Public operation, proposed:**

```text
practice_compare_revisions(store_root, request_id, baseline, variants,
                           edit_receipts, correspondence, question,
                           duration_policy)
```

Create a new comparison family rather than weakening `practice_compare` v1.
Each variant must identify its derivation from the exact baseline context through
bounded verified edit receipts. Matching a context label, song title or source
hash alone is insufficient. `correspondence` explicitly pairs the compared
occurrences and output intervals; equal-duration and unequal-duration cases have
declared policies. Require an unchanged baseline render.

**Implementation:** extract shared comparison/readback logic from
`practice_audio.py` only where this second profile needs it. Retain native
audition as an adapter; do not funnel standalone comparisons through native
candidate preparation. Reuse `audio_evidence` and preserve signal flags.

**Tests:** sibling/descendant edits, unrelated parent rejection, forged edit
receipt, same-title/different-recording rejection, valid boundary slip, declared
duration difference, silent/overloaded variant, relocation, unchanged baseline
hash and exact human feedback attachment. Test profile limitations at discovery.

## P4 — Retrieve feedback without inventing consensus

Factor the bounded selection/pagination mechanics from `feedback_query.py` into
shared code. Keep native attachment validation and standalone render validation
in separate adapters. Add a standalone `practice_feedback_query` accepting
explicit feedback handles, exact render identity, optional interval/actor/decision
filters and bounded result/cursor sizes. Preserve `audition_feedback_query` calls
and output contracts.

Tests must retain contradictory reports, original intervals and actor kinds,
reject stale cursors and substituted audio, and verify that overlap retrieval
does not extend a keep decision to another passage or infer a preference.

## P5 — Choose and implement one audio derivative

Choose the first edit from the listening experiment: an explicit join envelope,
count-in or rate-preserving gain adjustment. These address different questions;
implement one and leave the others discoverable as unavailable. Time stretch and
pitch shift require separate algorithm/profile qualification and are not a default
response to timing uncertainty.

**Recommended first candidate to discuss:** short, explicitly bounded join
envelopes. The current repeated excerpts make it possible to assess whether a
raw seam is actually the problem. This recommendation is a hypothesis pending
listening, not a claim that the existing joins sound bad.

The derivative must retain source/render identity, exact operation interval,
curve/amount, deterministic processing version and output mapping. Preserve the
untouched baseline. A gain change to address overload must be explicit and create
a new derivative; never normalize acquisition or remove the original warning.

Use independent sample oracles for gain/envelopes, and loop-boundary fixtures
with attacks close to the seam. The resulting signal can pass technical checks
without passing musical listening. A “keep” requires an actual attributed report.

## P6 — Publish what is actually callable

Generate transport input definitions, capability inventory and executable
examples from the implemented provider registry/types. Add stable machine error
codes through a versioned envelope/adapter while preserving existing `PocketError`
behavior and receipt statuses. Reconcile the planned facade before freezing new
external HTTP names; do not implement HTTP merely to make a document true.

Document runtime preflight for optional acquisition. The reference's old yt-dlp
failure should lead to actionable diagnostics and explicit tool selection, not
automatic changes to a user's global installation. Current upstream requirements
belong in setup documentation with tested versions and an isolated-runtime recipe.

The browser review exposed another packaging boundary: Chrome rejected DOUBLE
WAV. Specify browser-supported review exports separately from evidence renders.
Check decoded equality where representable, otherwise declare quantization and
verify its bound. Never silently clip, normalize or replace the original artifact.

Acceptance includes packaged Python, real CLI/MCP calls, stale/retry recovery,
proof-bound failures, no-DAW dependency checks, accurately unavailable profiles,
and a concise Pocket skill that composes public operations. Do not copy mutation
logic into the skill or install optional models to pass a core test.

## Decisions and stopping rules

Engineering can start P1–P4 with generated fixtures and the retained reference.
The only musical decision needed before P5 is the exact cue/join question and
which edit to compare. The first listening session should identify actual phrase
boundaries; the current eight-second windows are deliberately technical probes.

Do not require a full genre taxonomy, universal score model, new database,
distributed scheduler or package-wide rename. Preserve Trane's broader direction
through clear types and replaceable adapters. Live following, comping, trading,
instrument response latency and musical memory need separate post-v1 evaluation.

## Ready-to-use implementation brief

Implement **P1 only as the first reviewable delivery**, followed by dependency-ready
P2/P3 work when authorized for that implementation session. Work in the current
Pocket checkout, read AGENTS and the Pocket skill, preserve unrelated changes and
follow the owner's Git instructions. Start by mapping the existing hypothesis and
context contracts; reuse their validation and journals. Add strict typed evidence
references, authored-only/unresolved cases, source-bound selection and immutable
context binding. Preserve v1 readers and prove the exact nonzero-frame and
abstention cases through Python, CLI and MCP. Update the reference recipe using
only public calls. Finish with scoped tests, compatibility notes, honest remaining
musical acceptance and the next dependency-ready task.
