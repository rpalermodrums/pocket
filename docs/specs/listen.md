# Listen: turn live audio into current, inspectable evidence

Status: proposed product and technical specification. Listen is not an existing
public provider or a qualified native capture adapter. This document separates
the first implementation from later musical analysis.

Listen receives live audio and reports what it can measure, what an analyzer
hypothesizes, and when that evidence becomes stale. It can watch a master,
inspect a headphone cue, or capture a short passage for a slower analysis. It
emits signals to interested tools. Those signals do not grant control or decide
whether a mix sounds good.

The first useful result is reliable awareness of the sound actually passing
through declared routes. That supports [Shuttle](../shuttle.md) and
[b2b](b2b.md) without making the conversation itself an audio clock.

## Product boundaries

The current Shuttle host reads stereo Serato Virtual Audio, computes signal
measurements in 100 ms blocks, and optionally writes a master capture for at most
20 minutes. It has no isolated headphone-cue capture or long-lived musical
stream contract. Its metering proves neither physical headphone output nor
musical phrase alignment.

Pocket already has bounded local analysis. `audio_hypotheses` retains exact
source frames and competing Peek evidence. `audio_region_capture`,
`audio_region_hypotheses`, and their job providers handle declared file regions.
`audio_pulse_hypotheses` and `audio_note_hypotheses` provide optional, separately
qualified model analyses. These providers need exact local sources. They are not
continuous live listeners.

Listen owns stream capture, clock evidence, bounded buffering, route health,
and event delivery. It delegates retained-window analysis to those existing
providers. General transcription, source separation, arbitrary audio editing,
set rendering, and a musical performance scheduler remain outside the MVP.

## The performing experience

A compact status view shows each intended route with its actual device, channel
map, signal state, last received frame, and age. The master and cue appear as
separate routes. A route can be healthy and quiet. A route can carry loud signal
but still have an unverified semantic label.

Useful initial notices include these cases:

- "Master capture stopped receiving frames. Existing music may still be playing."
- "Cue route is connected, but its test has not established isolated cue audio."
- "This analysis describes audio from 3.8 seconds ago. It cannot schedule the next beat."
- "The capture has a missing interval. The recording is incomplete."

The view avoids musical verdicts such as "the decks are stopped" from silence,
"beat one found" from an onset, or "the vocal finished" from a pause in speech.
It exposes uncertain evidence so a performer or a policy can make a decision.

Listen supports three uses with the same stream contract. A prepared set can
verify expected routes and inspect rehearsed joins. A loose set can analyze a
candidate's cue while the master continues. An improvised set can capture a
recent passage and refresh a shortlist. The stream never forces a running order.

## Core workflows

### Verify master and cue before performance

1. The operator selects a named adapter, stable device identity, and explicit channel maps.
2. Listen reports the actual negotiated sample rate, layout, and capture clock.
3. An opt-in routing test presents a known master-only passage and a separate cue-only passage.
4. Listen records which channels contain the test, their timing, and any leakage.
5. The operator confirms physical headphone output separately if that matters.
6. A sealed route calibration binds these observations to the current device and topology epoch.

A device's channel names are advertised metadata, not a routing proof. A quiet
cue channel can mean no cue is selected, cue routing is wrong, or the passage is
quiet. Listen preserves those possibilities. It does not change the controller's
headphone mixer, enable a cue, or start a deck to resolve them without an
explicit test action through the control adapter.

If a device exposes only master audio, cue remains `unavailable`. Listen must
not relabel master audio as cue. The test does not qualify a different sample
rate, a different channel map, or a reconnected device instance automatically.

### Inspect a candidate while the master continues

1. A qualified performer routes the inactive candidate to isolated cue.
2. Listen captures a bounded window with its stream-frame interval and calibration ID.
3. A worker seals the exact PCM bytes as a new local recording artifact.
4. Existing public audio providers analyze that recording outside the capture callback.
5. The result retains capture frames, any declared deck-to-source mapping, uncertainty, and current age.

Captured cue audio describes what was routed at that time. It is not
automatically the candidate's unprocessed original recording. Effects, speed,
reverse playback, loops, and routing changes can alter the relation. A control
adapter must supply an independently checked mapping before source-relative
cues are proposed.

### Detect a stream failure without stopping the music

Capture discontinuity, nonfinite input, device loss, or stale frames produce
health events. Control consumers can cancel new automated moves according to
their own declared policy. Listen does not pause decks, mute the master, or
restart an application. The sound already playing can continue while monitoring
recovers.

After recovery, the stream has a new epoch. Consumers refresh routing, current
transport evidence, and musical context. They do not replay timed moves whose
deadlines passed during the outage.

### Retain a performance for review

Recording is an explicit subscription with a new destination, format, maximum
duration, byte budget, and stop policy. Its state is separate from capture
health. A live meter does not prove a file is being written.

Listen reports `recording_started`, `recording_degraded`, `recording_stopped`, and
`recording_sealed` separately. A sealed recording has verified headers, duration,
hash, channel layout, and known gap intervals. Its receipt distinguishes a
complete capture from a playable file containing missing audio. Disk exhaustion
cannot leave a green "recording" indicator.

## The smallest MVP

The first version supports one local capture device, up to four explicitly
mapped channels, master and cue route groups, a declared device sample rate,
bounded ring buffers, signal health, sequenced events, and explicit recording.
The initial native profile targets macOS with the connected FLX4 and Serato
Virtual Audio routes that actually pass the routing test. Four useful channels
are a qualification goal, not an assumption about every device or driver.

Signal measurements include sample peak, RMS, nonfinite count, clipped sample
count under a declared threshold, and near-silence under an explicit level and
duration. True peak and loudness require separately declared algorithms and
latency. They must not be mislabeled sample-peak or short-block RMS values.

The MVP offers one bounded recent-window capture that existing deterministic
Peek analysis can consume. Learned pulse and note workers are opt-in additions
after the basic capture and event path passes the soak tests. A model cannot be
a required dependency for route health.

Listen is not in the audible monitoring path. The first adapter copies host
input without modifying, mixing, resampling, or feeding it back to an output.
The user's sound therefore does not depend on an agent poll succeeding.

## Architecture

```text
capture adapter -> preallocated PCM ring -> measurement worker -> event broker
                          |                       |                 |
                          |                       |             subscriptions
                          |                       |             and bounded polls
                          |                       |
                          +-> optional recorder <-+
                          |
                          +-> bounded window snapshot -> analysis worker
                                                        -> evidence event
```

The callback stores raw block metadata and a bounded copy of PCM in a native
preallocated ring. It performs no model inference, file writing, device
discovery, network request, JSON encoding, blocking lock, process launch, or
unbounded allocation. A Python daemon can coordinate workers, but Python
execution is not placed in a native real-time callback.

Workers own measurement, serialization, optional disk writes, and model work.
Slow consumers cannot stall capture. Each consumer has a declared buffer and
overflow policy. Recording gets its own queue and reports any lost interval.
Model work never competes with recording through an unbounded shared queue.

The runtime and each worker have independent health and epoch records. A model
worker crash leaves stream measurement available. A recorder failure does not
imply capture failure. An unhealthy capture invalidates timing-dependent
analysis even when the last worker result looked plausible.

## Clock and identity model

Every source has a stable configured identity and a current instance epoch.
The adapter records the device identifier, driver and application versions,
sample format, channel map, sample rate, and configuration hash. Device names
are display fields. Identically named devices remain distinct.

Each block contains these coordinates:

| Coordinate | Meaning |
|---|---|
| `stream_frame_start` and `frames` | Exact end-exclusive interval in the current uninterrupted stream epoch. |
| `device_sample_time` | The device's sample counter when the driver exposes it. Otherwise explicitly unavailable. |
| `device_host_time_ns` | Driver-provided host-clock timestamp and its documented scope, when available. |
| `received_monotonic_ns` | When the capture runtime received the block. This is not the time sound reached the headphones. |
| `published_monotonic_ns` | When the event broker made the evidence available. |
| `wall_time_utc` | Optional display and audit timestamp, never a scheduling clock. |

Process monotonic time, device sample time, Serato display time, original source
frames, and arrangement beats are distinct domains. The runtime assigns clock
IDs and calibration IDs rather than comparing raw numbers across domains.

A calibration declares the mapping, valid interval, measured delay, uncertainty,
drift estimate, and evidence method. A callback receipt time alone cannot prove
device latency. A multi-device route needs separate clocks and a measured
relationship. Without that relationship, cross-route phase is unknown.

Discontinuities start a new stream epoch. Causes include device restart,
sample-rate change, sample-counter reset, capture-source replacement, or an
unexplained backward clock jump. Missing frames retain their interval and cause
rather than disappearing from the timeline. If the driver cannot identify the
exact missing interval, the event records a lower bound and timing uncertainty.

Looping or reversing a deck does not reverse Listen's stream counter. Source
position is a separate piecewise mapping supplied by a qualified control
adapter. A source-frame mapping is valid only for its source identity, speed,
direction, loop state, clock calibration, and bounded observation interval.

## Tagged records and evidence

The following is a proposed schema shape, not an existing API response:

```json
{
	"schema": "pocket.listen-event/v1",
	"session_id": "listen-session-a",
	"runtime_epoch": "runtime-4",
	"stream_epoch": "stream-9",
	"sequence": 182,
	"route_id": "master",
	"route_calibration_id": "calibration-3",
	"interval": {"clock_id": "capture-9", "start_frame": 480000, "frames": 4800},
	"received_monotonic_ns": 500100000000,
	"published_monotonic_ns": 500116000000,
	"valid_until_monotonic_ns": 500400000000,
	"evidence": {
		"kind": "measurement",
		"type": "signal_level",
		"profile": "rms-peak-100ms/v1",
		"rms_dbfs": [-18.2, -18.8],
		"sample_peak_dbfs": [-4.1, -4.6],
		"finite": true
	},
	"coverage": {"physical_headphones": "not_observed", "human_listening": "not_performed"}
}
```

`evidence.kind` is a tagged union:

- `measurement` carries the algorithm, units, channel scope, interval, and complete numeric result.
- `hypothesis` carries an analyzer version, alternatives, confidence definition, uncertainty, source interval, and computation interval.
- `health` carries a state transition, cause, affected routes, and whether missing frames are known exactly.
- `calibration` carries a route or clock proof with its valid topology and interval.
- `recording` carries a recorder state and its independently verified file evidence.

Authored musical anchors and listening reports reference these events or sealed
recordings through existing context and feedback records. Listen does not
relabel them as signal measurements. Actor names are attribution, not identity
verification.

Hypothesis confidence is not a universal probability. A deterministic threshold
has its threshold and method, not an invented confidence score. Invalid or
nonfinite numeric values cannot enter the normal event schema. They produce
explicit health evidence.

## Delivery, backpressure, and deadlines

Each runtime emits a monotonic event sequence. Subscribers declare route IDs,
event kinds, a maximum batch size, a retention cursor, and an evidence-age
limit. Polling returns the next bounded page, the current cursor, and explicit
missing sequence ranges. Events are ordered by publication sequence. Source
intervals allow delayed analysis to refer to older audio without appearing new.

Consumers deduplicate by runtime epoch and sequence. Delivery is at least once
within the retained range, not exactly once. An expired cursor returns
`cursor_expired` with the earliest retained sequence. It never reports an empty
successful page that hides missed events.

The in-memory event history has a fixed count and byte budget. Normal meter
events can coalesce per route. Health transitions use a separate reserved queue.
If that queue also overflows, a broker-health sequence gap invalidates normal
subscription health until the consumer refreshes a snapshot. No finite queue is
described as lossless under an indefinitely stalled consumer.

Local runtime consumers can subscribe through a bounded socket stream. MCP and
CLI expose bounded polls with the same event records. They do not hold an MCP
request open forever or promise that agent turn latency is suitable for a beat
deadline. A proposed poll limit is 256 events or 256 KiB and a maximum one-second
long poll. Qualification can tighten those bounds.

Every actionable consumer checks both source age and publication age, the route
calibration, the stream epoch, and the event deadline. A freshly published result
about a passage heard six seconds ago remains six-second-old evidence. A late
analysis result can support review while being ineligible for control.

Listen events are evidence only. A `near_silence` event cannot pause a deck, an
attack cannot launch a sample, and a pulse estimate cannot grant ownership.
Shuttle or b2b must separately validate authority, timing, and current host state
before any native action.

An event's attribution is not an authorization credential. Neither
`actor_kind: "human"`, a listener report, nor a model result can issue, amend, or
renew b2b standing policy. The trusted host or configured issuer capability
owns that policy change and checks its session epoch, version, and revocation
generation. Listen exposes no authority-changing provider. An event subscriber
cannot promote a new route, a monitoring CC label, or a historical calibration
to control permission merely by naming it in an event.

## Analysis windows and musical limits

A recent-window request pins a declared route, epoch, and frame interval. The
ring either retains every requested frame or refuses with `window_unavailable`.
It never expands, shifts, pads, or silently stitches a missing interval. A
separate explicit derivative can insert silence for export if requested, with
its transformation and gap mapping recorded.

The worker seals exact PCM bytes with rate, layout, interval, route, and clock
evidence before passing a file to existing audio providers. The resulting
analysis preserves its original stream coordinates. A filename or current deck
title does not establish the source's original recording identity.

Workers have bounded duration, concurrency, memory, and cancellation checks at
declared analysis boundaries. Stale queued analyses expire before starting.
An overloaded analyzer skips work and reports it rather than falling farther
behind the performance. Model installation, downloads, source decoding, and
network discovery stay outside the live measurement loop.

Listen keeps these musical distinctions explicit:

| Evidence | What it can support | What it does not establish |
|---|---|---|
| Detected attack | A sound began near this frame. | Musical pulse, beat one, or a new phrase. |
| Pulse hypothesis | Candidate periodic timing with alternatives. | A correct bar grid or a safe handoff point. |
| Tempo relationship | Candidate speed or half-time relationship. | Aligned kicks, compatible groove, or matched phrase numbers. |
| Near-silence | Samples stayed below the declared threshold. | Stopped transport, a dropout, or an intended ending. |
| Vocal activity hypothesis | A model found voice-like material. | A sentence has resolved or the last word can be cut. |
| Spectral overlap | Measured energy overlaps in declared bands. | Hats clash, harmony is wrong, or a transition is bad. |
| Harmonic hypothesis | Candidate pitch evidence from this mixture. | An aesthetic verdict on dissonance or reliable isolated track keys. |

A vocal or phrase ending remains an authored anchor or attributed listening
decision unless a separately qualified analyzer supplies an explicitly uncertain
hypothesis. That hypothesis still does not authorize a cutoff. A rehearsed
handoff can use approved source-relative anchors, but different tempo, effects,
routing, or track versions require new evidence.

## Proposed public providers

These names are proposals. Implementation must add discovery entries only after
the corresponding callable exists and its limits are documented.

| Python and MCP provider | CLI action | Purpose |
|---|---|---|
| `listen_prepare` | `listen prepare` | Validate device descriptors, routes, and bounded processing settings. No capture or output-device change. |
| `listen_start` | `listen start` | Start the explicitly configured capture session and its owned local runtime. |
| `listen_observe` | `listen observe` | Read current capture, route, clock, worker, and recorder health. |
| `listen_calibrate` | `listen calibrate` | Retain an explicit routing or clock test with attributed native evidence. No deck control. |
| `listen_events` | `listen events` | Read bounded events after a cursor, including gaps and expiry. |
| `listen_capture_window` | `listen capture-window` | Seal an exact retained PCM interval and mapping. |
| `listen_analyze` | `listen analyze` | Submit a bounded sealed window to existing public analysis providers. |
| `listen_record_start` | `listen record-start` | Start a recorder with an explicit new destination and budgets. |
| `listen_record_stop` | `listen record-stop` | Stop and finalize only the named recorder. |
| `listen_stop` | `listen stop` | Stop this capture runtime and finalize its owned subscribers. |
| `listen_request_status` | `listen request-status` | Inspect a retained mutation or worker request without repeating it. |

All interfaces call the same library functions. Mutations use an exact runtime
epoch, current configuration revision and SHA, a unique request ID, and declared
resources. The permission to record is independent of permission to capture
temporarily for metering. Observation never starts a recorder as a side effect.

A proposed `listen_events` call has this shape:

```json
{
	"session_dir": "/private/listen-session",
	"expected_runtime_epoch": "runtime-4",
	"after": {"runtime_epoch": "runtime-4", "sequence": 181},
	"routes": ["master", "cue"],
	"kinds": ["measurement", "health"],
	"max_events": 32,
	"max_bytes": 65536,
	"wait_ms": 200,
	"max_evidence_age_ms": 600
}
```

Examples use synthetic identifiers. An age filter cannot hide a health failure.
The response reports filtered stale counts, current health, and sequence gaps.
Subscriptions deliver data through local authenticated connections. Browser
origins, arbitrary filesystem traversal, and unconfigured network destinations
are refused.

## Privacy, retention, and recovery

The default session retains signal metadata and a bounded in-memory PCM window.
The setup states the window's duration and memory cost. It writes no continuous
audio file. Capturing a passage, recording a set, or sending audio to a model
each requires explicit opt-in scope and a declared retention policy.

Local models are optional. No audio is uploaded by default. A future remote
model needs separate destination, material, and retention authorization. Public
support reports omit recording titles, absolute paths, device serial numbers,
and audio unless the operator selects them for export.

The host binds capture, retained-window, recording, and model permissions to the
session's configured local capability. Their consent attribution alone cannot
create those permissions. An agent may request an already allowed recording,
reduce retention, or stop its subscription. It cannot extend the allowed
material, duration, destination, or retention by asserting a human actor field.
These permissions do not include b2b policy issuance or native control.

Metadata journals and recording manifests use append-only events with bounded
disk budgets. On restart, a new runtime epoch exposes prior requests as
historical. Unfinished recordings have a recoverable partial-file state and a
verified recovery receipt. A recovered header does not prove that every sample
was captured. The runtime never silently reconnects to a similarly named device
or resumes a recording into an old file.

Stopping capture invalidates subscriptions and flushes or cancels workers within
their declared limits. It does not change another application's transport.
Deletion of retained recordings is an explicit operation outside live capture.

## Acceptance and rollout

These are proposed targets, not measurements or hard real-time guarantees. Each
profile publishes its hardware, driver, rates, callback sizes, load, sample
counts, tail latencies, and observed failures.

- A 60-minute native capture has no callback overruns attributable to Listen. Callback time at the 99.9th percentile stays below 10 percent of the device period, with maximum time also reported.
- Signal health reaches local subscribers within 200 ms at the 99th percentile. A missing-frame watchdog reports stale capture within 600 ms. Device buffering and event age remain visible.
- A qualified same-clock calibration has a proposed error budget of 2 ms. Cross-device timing has no claim until independently measured drift and uncertainty fit the consuming action's budget.
- Analysis overload cannot extend callback time or grow queues without bound. A one-minute blocked consumer causes explicit gaps, not capture failure or silent event loss.
- Stop, disconnect, recorder failure, and worker cancellation each produce a terminal state within their declared bounded wait. A blocked adapter reports unavailable rather than reusing old success.
- Recording beyond 20 minutes remains supported within the requested disk and duration budgets. Successful stop requires a stable, verified file and an exact gap report.

Generated tests use synthetic ramps, impulses, silence, nonfinite blocks,
known source counters, and explicit counter discontinuities. They cover clock
reset, drift, suspend, channel reorder, duplicate device names, route changes,
callback overrun, ring overwrite, worker crash, disk full, corrupt finalization,
event cursor expiry, replay, and lost start or stop responses.

Native tests prove the declared channel map with master-only and cue-only
passages, then recheck after reconnect and sample-rate changes. Physical
headphone confirmation is a separate operator observation. A rhythmic test
proves timestamp error only against its declared reference. It does not turn a
successful pulse estimate into native phrase qualification.

Release order is capture and health, route qualification, bounded recording,
recent-window analysis, then optional model subscriptions. Shuttle can adopt
Listen's evidence contract after both adapters pass regression tests. Keep the
old profile's limits visible during that transition. The shared
[live performance specification](live-performance.md) owns cross-module rollout
and the complete recorded-performance workflow.
