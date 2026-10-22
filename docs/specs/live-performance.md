# Live performance modules

Status: proposed architecture. The fixes described in [Shuttle](../shuttle.md)
and [Whisker](../whisker.md) are implemented separately. The b2b and listen
providers below do not exist yet and must not appear as available capabilities.

Pocket needs to help a musician keep playing while an agent prepares, listens,
and contributes. The session must keep its musical context when a screen read
fails, a person touches a control, or the next record takes longer to prepare.

This design adds two modules:

- [b2b](b2b.md) decides who may change each control and makes handovers visible.
- [listen](listen.md) reads live audio and emits observations with their time,
  route, uncertainty, and coverage.

Shuttle remains the execution adapter. Whisker remains the record-selection
module. A separate conductor module would duplicate their responsibilities.
Add scheduling to Shuttle only after its timing and cancellation contracts are
qualified. Baste stays a read-only Ableton observer.

## What the performance process exposed

| Observed problem | Product requirement | Module responsibility |
|---|---|---|
| Matching tempo did not establish the correct beat, bar, or phrase | Keep separate pulse, bar orientation, phrase anchors, and musical approval | Peek proposes evidence; musical context and interpretations retain authored anchors; Stitch renders comparisons |
| Vocals ended mid-sentence | Protect a declared vocal interval, including a pickup or tail | Whisker suggests phrase completion; musical context retains explicit intervals; renderers preserve them |
| Two drum parts competed during a blend | Decide which record owns the kick and high percussion at each stage | Whisker proposes roles; Shuttle executes qualified gain, EQ, and FX actions |
| A very short fade lost continuity; a long fade lost momentum | Choose an envelope for the musical pair and audition the intersection | Context and rendering artifacts retain the candidate; listening feedback remains scoped to it |
| A plan needed slow UI navigation at the last moment | Prepare the free deck before a declared admission deadline | Shuttle prepares and verifies; the agent chooses a later boundary when preparation is late |
| Clock reading blocked preparation or ended a healthy performance | Separate observation health from the ongoing transport | Shuttle rejects unsafe new moves; listen reports degradation without stopping music |
| A person changed monitoring volume and all automation yielded | Distinguish control ownership from listening controls where the adapter proves the distinction | b2b uses qualified resource mappings; Shuttle keeps conservative yielding until then |
| A recording command or moving meter was mistaken for a saved take | Track capture frames, gaps, finalization, and the actual saved artifact separately | listen reports stream health; recording consumers produce explicit save receipts |
| The master and headphones did not carry the same signal | Verify each route independently | listen qualifies master and cue capture separately; physical headphone output remains a separate check |
| Restarting lost the exact successful handoff | Retain the source, timing, processing, and attributed approval as one revision | Existing artifact, timeline, rendering, and practice-review contracts |

The table describes needs, not a claim that a signal analyzer can decide whether
a transition is good. Human listening remains separate from technical checks.

## Keep one architecture for three ways to play

| Mode | Prepared before play | Decided during play |
|---|---|---|
| Intricate planned performance | Exact sources, phrase anchors, envelopes, FX, cue checks, launch windows, alternatives, and recording policy | Whether current evidence still admits each move; recovery at a new boundary |
| Loosely planned set | A record bag, possible routes, a few rehearsed pairs, source analysis, and texture candidates | Record order, time on each record, transition choice, peaks, and valleys |
| Improvisation from a record bag | Source identities, known cues and loops, optional cached analysis, adapters, and monitoring routes | All musical choices, including deliberate exceptions to regular phrasing |

The third mode is the primary product goal. Preparation supplies material and
familiarity. It must not require a fixed running order or turn a ranking into a
command. The same transition can be rehearsed in advance and chosen much later.

## Keep the evidence and authority separate

An audio stream can show a level change or support a pulse hypothesis. It does
not grant control of a deck. A controller movement can revoke authority without
proving that playback stopped. A software command can be acknowledged without
proving the expected sound reached the master or headphones.

Each live decision joins four separately versioned records:

1. The exact musical material and its approved or proposed interpretation.
2. A current adapter observation, including any unresolved fields.
3. A b2b grant for the resources the action changes.
4. A Shuttle request and its observed result, with uncertainty after dispatch.

Listen observations can inform a decision. Shuttle must recheck the grant and
freshness at dispatch, and before each remaining step of a multi-step action.
One observed event must never authorize an unbounded sequence of moves.

## Extend existing modules before adding more names

### Peek supplies hypotheses with coordinates

Keep tempo candidates, band-phase disagreement, crop sensitivity, and unresolved
bar orientation visible. A stable beat grid is not a bar-one certificate. New
analysis must retain competing interpretations rather than overwrite the one a
musician approved. Expensive analysis belongs before the performance or in a
bounded worker away from audio delivery.

### Whisker keeps simultaneous constraints visible

Transition suggestions must retain tempo-reset guidance when both records have
dense vocals. A short foreground exchange must not imply cutting off a vocal.
Suggest protecting the full phrase and assigning percussion ownership. These
remain suggestions. They do not assert that a specific cue or EQ value works.

### Context, Thread, and Stitch retain the actual transition candidate

An accepted candidate needs exact sources, source intervals, tempo mapping,
authored beat and phrase anchors, gain and processing envelopes, and route
assumptions. A listener's approval references the exact rendered or captured
candidate and interval. Changing an incoming cue, EQ envelope, FX timing, or
source version creates a new candidate. It does not inherit approval silently.

Musical context and interpretation records own authored timing claims. Thread
inspects saved Ableton projects. Stitch creates supported new project candidates
and renders. These roles remain distinct from live scheduling and control.

Use existing artifact and practice-review records for this lineage. Introduce
new public fields only after a schema review and generated-media round-trip
tests. This design does not add a second approval store.

### Shuttle owns prepared execution

The current public adapter supports short observed moves. The following are
future additions to that adapter, not capabilities established by this PR:

- Qualify absolute channel fader, EQ, filter, FX enable and depth controls, with
  readback or an explicit commanded-only result for each parameter. Loading a
  record or human intervention invalidates remembered values where necessary.
- Describe a handoff as bounded steps with declared affected resources,
  cancellation points, a latest start, and source and session identities.
- Use a resident scheduler for a previously admitted sequence. Do not put a
  chat response, OCR call, network request, or audio decode on its deadline.
- Preserve the original launch origin. An unknown clock must not silently
  become a new source position. New launch, seek, reverse, and loop decisions
  need the observations their qualified adapter contract requires.
- When preparation is late, reject the old launch. Keep the existing audible
  deck unchanged. Choose a deliberate future boundary only when the current
  musical context and source reserve support it.
- An optional end-of-source guard may enable a separately qualified loop only
  under explicit standing policy. It cannot guess a loop, retry an uncertain
  toggle, restore old controls after intervention, or survive a source change.

A long fade is not one unstoppable operation. Each pending step must remain
cancellable. A request with an uncertain dispatch result requires status and
fresh observation, not a new request ID for a blind retry.

## Route preparation and recording

Before an audible collaboration, establish the actual device, sample rate,
channel count, session epoch, and each monitoring route. Test master and cue
separately. A silent cue bus while no cue is selected is not a dropout. A moving
software meter does not prove the physical headphone controls are set correctly.

Recording is a consumer of the stream, with a separate lifecycle. Start means
the recorder accepted a request. Recording means frames are being retained.
Finalized means the file is closed and its saved frame count is verified. A
duration limit, write failure, overrun, or missing frames makes the coverage
partial or failed. Preserve that evidence and the partial file.

Mastering and delivery use the existing saved-project, render, and review
workflow. Preserve the original take. A native DAW export needs file and format
verification. Loudness and clipping measurements do not establish a listener's
approval of a new master. Full native export automation remains a separate
adapter qualification task; do not make it a prerequisite for listen or b2b.

## Recovery must keep the musical context honest

| Failure | Permitted response | Evidence needed before new musical changes |
|---|---|---|
| One unreadable source-clock frame | Publish unknown; continue independent audio and controller monitoring | A later fresh observation, or another already qualified timing basis |
| Repeated stale observer output | Mark the observer degraded and reject actions that depend on it | Fresh bounded observation and unchanged source and transport context |
| Human touches an owned control | Cancel the affected pending movement and publish the new owner | A new explicit handback or an admitted proactive claim under standing policy |
| Controller disconnects while nobody owns controls | Invalidate the adapter epoch and all grants | Reconnect and requalify the actual endpoints and routes |
| The agent or UI stalls | Leave the ongoing audio path intact; expire stale grants and pending launches | Current musical context, fresh control and audio evidence, and a new admitted action |
| Audio capture drops frames | Mark the gap; invalidate analyses spanning it | Contiguous samples and a new analysis window |
| Native dispatch succeeds but the reply is lost | Report the request as dispatched or uncertain; never redispatch by inference | Retained request status and fresh state |
| The native application restarts | Create a new epoch; do not replay old moves | New app, source, controller, route, and recorder observations |

No software component can prevent independent hardware input from changing the
sound. Cancellation limits describe what Pocket stops sending after it observes
an intervention. They are not a guarantee of silence-free playback or exact
musical recovery on every machine.

## Build and qualify in this order

1. Ship the existing-module fixes with generated-media tests and honest
   capability discovery. Keep native behavior claims limited to prior evidence.
2. Build listen's qualified stereo capture, clocked meters, health events,
   bounded subscriptions, and explicit capture finalization. Validate dropout,
   format mismatch, backpressure, timestamp, and device-restart failures offline.
3. Qualify master and cue routing on actual hardware, including a full-duration
   recording test and a deliberate disconnect. Store private acceptance locally.
4. Build b2b's resource grants, handback, visible proactive claims, revocation,
   request outcomes, and adapter pickup behavior against a deterministic fake.
5. Test simultaneous human and agent operation on real decks with muted output,
   then with a recorded musical rehearsal. Verify notification delivery and
   affected-resource cancellation, including races during a ramp.
6. Add qualified mixer and FX actions and scheduled handoff execution to Shuttle.
   Validate each adapter operation independently before combining it in music.
7. Evaluate all three performance modes. Require fresh listening feedback on
   musical results as well as technical evidence for timing and recovery.

The performance targets and acceptance gates in the b2b and listen specs are
proposals. They become claims only after measurements on a named native profile.
