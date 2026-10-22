# b2b: share a performance without fighting the controls

Status: proposed product and technical specification. No provider, controller
gesture, Live writer, or native acceptance described here is implemented by this
document. Existing capabilities remain as reported by `capabilities_list`.

b2b lets a person and an agent perform together. The person uses a controller
while the agent prepares a record, shapes a software layer, or takes an agreed
part of the mixer. Either performer can hand over a part of the performance
without handing over everything.

The default experience is a shared crate and an open musical direction. A
running order is optional. A person can invite the agent back with a configured
gesture, or permit the agent to claim idle controls during the session. The
agent announces that claim before its first audible action. Physical movement
always takes priority over queued agent movement.

## Product boundaries

[Shuttle](../shuttle.md) executes short moves in an observed Serato session.
Its first profile uses one global owner and yields to any detected FLX4 input.
That is useful for solo agent performance, but prevents useful simultaneous
work and repeatedly interrupts collaboration over unrelated adjustments.

b2b adds ownership, invitations, and delivery of takeover notices. It uses
Shuttle's qualified controls rather than silently adding new ones. It does not
move physical knobs. A DDJ-FLX4 has no assumed motorized fader or knob feedback.

Playing a software instrument alongside Serato requires a separate, qualified
writer and audio route. Current `instrument_parameter_set` and
`performance_launch` capabilities are unavailable. The narrow
`native_midi_write` fixture operation is not a live instrument performer.
[Baste](../baste.md) remains a read-only observer. b2b must not acquire a write
capability by extending Baste's reader facade.

The first product uses software controls in the existing Serato setup while the
human uses the FLX4. A separate instrument or sampler is an extension, not a
prerequisite for collaborative mixing. Its unavailable writer and routing must
be qualified before that extension appears in discovery.

## The performing experience

At session setup, the performers choose a crate, a controller profile, and a
small set of agent permissions. The human can keep transport and jog wheels
while allowing the agent to prepare the other deck, contribute a loop, or shape
one declared effect. The screen always shows who owns each part and why an
action is waiting.

The human can use these modes without starting a new session:

| Mode | Behavior |
|---|---|
| Invite only | The agent waits for an explicit handback gesture or session instruction. |
| Collaborate | A standing session permission permits the agent to claim named idle controls and announce the claim. |
| Human holds | The agent can observe and propose. It sends no control to the held resources. |
| Agent holds selected resources | The agent operates only the granted resources. Human movement revokes the affected grant. |

Modes apply to named resources. There is no global autonomous mode that bypasses
resource limits. A standing permission is an attributed human instruction, not
an inference from a quiet controller. Silence, an idle fader, or an agent's
musical preference does not create permission.

The same ownership rules support a fully prepared set, a loose route with
options, and an improvised set with only a crate. Improvisation is the first
product workflow. A prepared route never gains stronger control authority than
an improvised move.

The smallest useful display contains a resource owner, a brief action reason,
and a hold or reclaim control. For example, "Agent taking right-deck filter for
one short move" appears before the filter moves. Notices are visible outside
the chat so a delayed conversation cannot hide a takeover. The MVP sends no
spoken notice into the master audio.

## Workflows

### The person mixes while the agent prepares the next record

1. The human keeps the left deck and grants selected right-deck software controls.
2. The agent observes the musical context and both decks' actual state.
3. The agent proposes a prepared record or a bounded loop from the crate.
4. b2b grants only the required right-deck resources. The person continues using the FLX4.
5. A local notice appears before the first audible launch.
6. The agent checks fresh state and master evidence after the move.

The agent can claim the next software move without interrupting unrelated human
controls. Dispatch and observed state remain separate. The master stream
establishes resulting signal, not the transition's musical quality.

An optional instrument extension follows the same workflow on a dedicated bus.
The agent does not use a file-only MIDI plan as proof that an instrument played.
Its adapter needs its own readback, dispatch, and route qualification.

### The person hands a deck back

1. The human triggers a configured invitation for the right deck.
2. b2b records the gesture, target scope, controller identity, and session epoch.
3. The agent refreshes deck identity, position, controls, and audio evidence.
4. b2b grants the idle right-deck resources if the invitation is still valid.
5. A notice appears. The agent makes one bounded move and observes again.

A release gesture invites control. It does not issue play, replace a record, or
certify a phrase boundary. Repeated button messages produce one invitation.

### The agent takes an allowed opportunity

The agent can request a claim under the standing permission when the named
resources have been idle for the configured interval. b2b refuses a claim during
a gesture, an unsettled pickup, or an uncertain controller classification. A
claim includes its reason, deadline, and intended resources. The human receives
the notice before the first audible action and can interrupt at any time.

The agent does not ask the same permission again for every claim within this
scope. A wider scope, a revoked grant, or an unqualified adapter needs a new
human instruction. Expired or restarted sessions do not inherit authority.

### The human moves an agent-owned control

Physical input revokes the matching resources before the next agent step.
Coupled resources also yield when the profile declares them inseparable. A jog
gesture can reserve its deck's transport, position, loop, and tempo controls.
A verified headphone-volume control need not interrupt an unrelated deck.
An unknown input conservatively yields the whole adapter scope.

b2b leaves the resulting controls where the performers put them. It does not
restore a pre-takeover snapshot or silently resume the interrupted move.

## The smallest MVP

The MVP has one human, one agent, one FLX4, and the existing Serato software
control profile. It offers explicit handback, scoped proactive claims,
human priority, pickup, local notices, and a reconstructable control journal.

The first optional instrument adapter is deliberately small. It supports one
preloaded stock instrument or sampler, bounded note or loop launch,
stop-new-notes, a bounded level move, and an explicit tail policy. It cannot load arbitrary plugins,
rewrite a project, browse presets, or control arbitrary Live parameters.
Building the adapter does not qualify its native behavior.

For that extension, the audio route must permit both applications to contribute
to a measured master without double monitoring or feedback. Exclusive-device behavior,
latency, and headphone cue routing require native tests. b2b refuses an
unsupported route rather than changing the user's output device during a set.

The first implementation sequence is:

1. Prove arbitration and cancellation with deterministic synthetic adapters.
2. Qualify per-resource control on the existing Serato profile.
3. Run the complete collaborative Serato workflow with [Listen](listen.md) evidence.
4. Qualify a separate instrument writer and combined audio route as an extension.

No model or network call runs in the control loop. Prepared clips, profiles,
cached selection data, and explicit musical anchors are sufficient for the MVP.

## Resource and authority model

A resource is an adapter-qualified control address, not an arbitrary string
passed through to a host. Example resources are `serato/right/filter`,
`serato/right/transport`, and `instrument/layer-1/level`.

Each adapter declares:

- Its resource descriptors, units, ranges, readback capability, and control dependencies.
- Which physical events affect each resource and which events remain unknown.
- Whether a control is absolute, relative, toggled, or state-setting.
- Its qualified step duration, cancellation boundary, and outcome verification.
- Its resource conflict groups, including application focus and library navigation.
- Which actions are audible, including actions whose audibility is uncertain.

A load uses a deck's identity, transport, and application-focus resources as one
conflict group. An effect route change reserves the route and its parameters
together. A background loading task cannot steal foreground keyboard focus from
the human without that resource being allowed and idle.

The broker grants a requested resource set atomically. It grants the whole set
or none. The adapter receives an unforgeable local grant reference, a session
epoch, a control revision, and a resource generation. It never receives broad
authority from a Listen event or a Whisker suggestion.

### Shared controls have one resource identity

Addresses are aliases for a canonical control element in a particular adapter
epoch. For a profile that binds channel 2 to the right deck, its physical trim,
Serato trim, and dedicated MIDI command refer to the same resource. The example names
`controller/flx4/channel-2/trim` and `serato/right/gain` cannot obtain independent
grants. The alias table is part of the qualified mapping hash, and observations
resolve aliases before arbitration.

Shared controls have shared scope. A crossfader, master level, or effect routed
to both decks has a canonical shared-bus identity. An effect-unit route and its
controls reserve the same unit, even when two deck-specific aliases name it.
Changing a bus route advances the generations of every affected alias and
invalidates grants based on its former binding. Unknown or conflicting aliases
make that scope exclusive until the adapter qualifies the relationship.

| Proposed canonical resource | Possible aliases after qualification |
|---|---|
| `serato/right/gain` | The mapped physical channel trim and software track-gain command. |
| `serato/mixer/crossfader` | Both deck-specific crossfader views and the physical crossfader. |
| `serato/fx/unit-1` | Unit-1 route, enable, and parameters shown beside either assigned deck. |
| `audio/monitor/headphone-level` | A proved physical or software headphone-level control on the same monitoring path. |
| `audio/master/level` | A proved physical or software level control for the shared master. |

These are proposed identities, not additional current Shuttle controls. A
hardware-only or unobservable control remains outside software write authority.

The resource descriptor distinguishes a control's write conflicts from the
audio routes it can affect. Two independent channel levels can be changed
concurrently even though both reach the master. Two interfaces to one channel
level cannot. Output routing and monitoring-path controls are explicit shared
resources when their effects cross the proposed ownership boundary.

### A policy issuer grants authority

An attribution records who claims to have said something. It does not
authenticate a person or authorize a policy change. Setting
`actor_kind: "human"`, attaching a listening report, or receiving a Listen event
cannot create, expand, renew, or reinstate a standing permission.

A trusted local host issues policy changes from its human control UI, a
configured physical authorization gesture, or a separately configured issuer
capability. Agent-facing tools receive only the narrower claim and action
capabilities. The host injects the issuer capability on its trusted channel.
It does not accept a JSON actor field as a substitute. A programmatic issuer
uses a configured local credential outside specs and receipts. The UI must show
the exact scope, duration, and proactive behavior before issuing that policy.

`b2b_authorize` remains a common public provider across Python, CLI, and MCP,
but its callable permission depends on that issuer capability. Calling it from
an ordinary agent connection returns `authority_issuer_required` and changes
nothing. A trusted client can submit a proposed policy for human review without
granting it. This is a local capability boundary, not a claim of protection
against an administrator who can replace the host or steal its credentials.

Each permission has an issuer reference, session epoch, version, state SHA,
expiry, and revocation generation. Issuance and amendment compare the expected
epoch, version, and SHA atomically. Agent claims reference the exact permission
version. An agent can release resources, shorten a lease, or narrow its own
active grant. That restriction does not amend the standing policy. The agent
cannot expand limits, extend expiry, remove a human hold, amend an issuer, or
undo revocation. Only the issuer can issue or amend the standing policy.

Revocation takes effect in the same broker transaction that advances permission
and affected resource generations. All dependent pending claims, notice waits,
and undispatched steps become invalid. A replay of an earlier authorization
receipt remains historical and cannot resurrect the permission. Session restart
invalidates issuer session bindings and prior grants as well as control leases.

### Tagged records

The following records are schema sketches. Concrete field limits and names are
part of implementation review, not an existing wire contract.

```text
ResourceOwner = (
	{"kind": "unowned", "generation": int}
	| {"kind": "human", "gesture_id": str, "generation": int}
	| {"kind": "agent", "grant_id": str, "generation": int,
	   "expires_monotonic_ns": int}
	| {"kind": "pickup_pending", "target_value": float,
	   "physical_value": float | None, "generation": int}
	| {"kind": "unavailable", "reason": str, "generation": int}
)

ClaimBasis = (
	{"kind": "invitation", "invitation_id": str}
	| {"kind": "standing_permission", "permission_id": str,
	   "permission_version": int, "revocation_generation": int}
)

ActionOutcome = (
	{"kind": "refused", "code": str}
	| {"kind": "cancelled_before_dispatch", "reason": str}
	| {"kind": "partially_dispatched", "last_step": int,
	   "cancel_reason": str, "observation_required": True}
	| {"kind": "dispatched_unverified", "request_id": str}
	| {"kind": "verified", "evidence_refs": list[str]}
	| {"kind": "outcome_unknown", "request_id": str,
	   "automatic_retry": False}
)
```

The types above use dictionary notation to illustrate tagged unions. They are
language-neutral schema sketches. Implement them with strict validated
records. Unknown fields, invalid enum values, nonfinite values, and undeclared
resources fail at the boundary.

A `StandingPermission` includes an attributed human instruction, allowed
resources and action kinds, value and rate limits, expiry, maximum lease time,
notice destination, allowed proactive claims, and a revocation control. It
binds to the session and adapter configuration hashes and carries its verified
issuer reference separately from attribution. Possessing a configured issuer
capability authorizes only its declared local scope. An asserted actor name
never establishes human identity.

### Ownership transitions

| Current state | Event | Result |
|---|---|---|
| Unowned | Valid explicit invitation or allowed idle claim | Atomic short lease, pending notice |
| Pending notice | Notice delivery acknowledged and evidence fresh | Agent can dispatch within the action deadline |
| Pending notice | Human input, expiry, or failed delivery | Grant revoked, no audible dispatch |
| Agent | Human movement on a held resource | Affected group yields and its generations advance |
| Agent | Release, expiry, or explicit hold | Pending work cancels, controls remain as observed |
| Human | Gesture ends and inactivity timer passes | Human hold remains, or resource becomes claimable under its declared policy |
| Any | Controller, host, or monitored audio route disconnects | Dependent resources become unavailable |
| Unavailable | Reconnect and fresh qualification | Unowned in a new adapter epoch, no automatic resume |

An inactivity timer cannot release an explicit human hold. A noisy encoder
cannot trigger unlimited notice spam. Notices coalesce by claim, and denied
claims have a cooldown. Qualification must distinguish movement from a heartbeat
or device feedback. Uncertain classification stays conservative.

### A notice can expire without a musical action

The trusted local performance UI acknowledges that it displayed a notice bound
to the grant ID and action deadline. This proves delivery to that UI, not human
attention. A chat message, agent-generated receipt, or unrelated display event
does not satisfy the notice gate. The UI channel belongs to the configured
session and rejects prior epochs.

If the acknowledgement is missing, late, or unknown at the deadline, the broker
cancels undispatched steps and reports `notice_delivery_unknown` or
`notice_deadline_missed`. It does not perform a late audible action. If no step
was admitted, the receipt states `cancelled_before_dispatch`. That does not mean
the decks stopped playing. If a native step was already admitted, its actual
dispatch outcome stays separate from the notice error and requires observation.
Current human playback remains unchanged by the notice failure.

## Pickup and gestures

A nonmotorized fader can remain at a physical value different from the software
value. After the human moves it, b2b stops writing immediately. The adapter can
use soft takeover only when its native mapping supports that behavior and
native tests prove it. Otherwise the display warns of the mismatch and requires
an explicit pickup policy before agent control resumes. b2b cannot guarantee
that a physical move already delivered to Serato causes no value jump.

Relative encoders need no absolute pickup, but still require a current baseline
for absolute agent moves. Unknown numeric readback cannot become a precise
current value merely because the agent sent a value earlier.

Handback uses a user-configured spare gesture with press, release, debounce, and
long-press semantics declared by the profile. No existing controller function
is repurposed silently. Configuration must test collisions, duplicate messages,
release loss, and accidental activation. A generic MIDI note is not an invitation
without that binding and the matching controller epoch.

Excluding a monitoring control from a yield requires native evidence, not a
guessed CC number. The qualified profile records the exact endpoint, port,
channel, message type, controller number, value encoding, mode, firmware and app
versions, and active mapping hash. A controlled test moves that physical control
through its range and confirms the affected headphone or monitor path, unchanged
deck and master controls, and absence of another message with a wider effect.
The test covers modifier modes, shared encoders, and simultaneous human input.
It records emitted bytes separately from physical-output confirmation.

Until that test passes, the control stays unknown and conservatively yields the
adapter scope. A changed mapping, endpoint, mode, or software version invalidates
the exemption. Normalizing a noisy stream or recognizing a familiar message
shape cannot establish that an input is harmless.

## Dispatch, cancellation, and recovery

One local broker serializes authority changes and the start of each control
step. A networked agent never sends host MIDI directly. Each step validates the
grant, resource generation, deadline, adapter epoch, evidence age, and current
human activity immediately before dispatch. The check and dispatch admission
are one critical operation. An action plan cannot acquire resources later
without another atomic grant.

Revocation cancels all undispatched steps. A native message already sent may
arrive after revocation. Receipts retain that uncertainty rather than promise
rollback. Adapters bound step size and drain time, and the acceptance test
measures the last post-intervention output. Long envelopes become short
cancellable steps, never a blocking sleep followed by a whole fade.

The broker uses a monotonic clock for leases and deadlines. Wall time is display
metadata. Epoch changes invalidate all outstanding grants, schedules, and
observation tokens. Suspend, reconnect, app restart, source replacement, or
uncertain clock recovery requires fresh musical context. "Resume" never means
replaying old control commands.

An append-only journal retains intent, authority, dispatch admission, adapter
acknowledgements, cancellation, and observations. A response loss is queried by
its original request ID. Repeating identical arguments returns the retained
outcome without repeating the move. A changed request under the same ID fails.
After a crash between dispatch and journal completion, the action is
`outcome_unknown`. The agent observes before considering a new action.

Durable history proves what the broker retained, not fresh host state. Recovery
rebuilds current record identities, source positions, routing, ownership, and
controller activity before any new claim. If musical position cannot be
reconstructed, the agent keeps observing or asks for an explicit restart rather
than improvising a false continuation.

The MVP panic control revokes agent authority and cancels pending moves.
It leaves human transport running. An instrument extension also stops new
instrument notes. A qualified adapter may issue cleanup for
notes it owns under a separately declared policy. It must not send a global
all-notes-off, reset an effect, or mute the master on the assumption that the
agent owns the whole performance.

## Proposed public providers

All interfaces call the same public library functions. The daemon owns native
I/O. Providers validate bounded requests and return receipts.

| Python and MCP provider | CLI action | Purpose |
|---|---|---|
| `b2b_prepare` | `b2b prepare` | Bind existing adapter sessions, crate, and proposed resource policy. No claim or playback. |
| `b2b_authorize` | `b2b authorize` | Issue or amend a permission through a trusted issuer capability. Agent attribution alone is refused. |
| `b2b_observe` | `b2b observe` | Read fresh authority and adapter evidence, with separate ages. |
| `b2b_claim` | `b2b claim` | Atomically request named resources under an invitation or permission. |
| `b2b_act` | `b2b act` | Execute one bounded move under a current grant and action deadline. |
| `b2b_release` | `b2b release` | Revoke agent resources and cancel pending steps. |
| `b2b_revoke` | `b2b revoke` | Withdraw authority within the caller's revoke scope. Agent callers can only narrow or release their own authority. |
| `b2b_events` | `b2b events` | Read bounded sequenced notices and outcomes after a cursor. |
| `b2b_request_status` | `b2b request-status` | Inspect retained outcomes without redispatch. |

Mutations require `request_id`, `expected_session_epoch`,
`expected_control_revision`, and the exact state SHA. Control revisions advance
on authority, resource binding, or relevant gesture changes. Audio meter ticks
do not invalidate an unrelated ownership transaction.

Policy mutations additionally require the expected permission version and SHA
plus the host-verified issuer or revocation capability. Authorization secrets
are never tool JSON fields or receipt contents. Claims and actions need the
current permission version and revocation generation. The broker checks those
values again at each dispatch admission, not just when the claim was created.

For example, a proposed `b2b_claim` input is:

```json
{
	"session_dir": "/private/performance-session",
	"request_id": "claim-layer-17",
	"expected_session_epoch": "epoch-c",
	"expected_control_revision": 41,
	"expected_state_sha256": "RETURNED_STATE_SHA256",
	"resources": ["serato/right/gain"],
	"basis": {
		"kind": "standing_permission",
		"permission_id": "permission-2",
		"permission_version": 3,
		"revocation_generation": 0
	},
	"reason": "Shape the right-deck level while the human mixes the left deck",
	"lease_ms": 2000,
	"not_after_monotonic_ns": 981000000000
}
```

Values and paths are synthetic. A successful claim returns a grant bound to the
requested resources, their generations, the permission, and the notice. It does
not imply an action happened. `b2b_act` must consume the grant before its expiry
and satisfy any fresh-state preconditions. CLI commands use `--spec` JSON, and
MCP exposes the same nested schemas, enums, and error dispositions.

## Existing modules and dependencies

| Module | Relationship |
|---|---|
| Shuttle | Qualified Serato observation and control adapter. Reuse identity, request replay, and guarded dispatch. |
| Whisker and Weave | Cached options and optional set direction. A shortlist never grants control. |
| Record bags | Exact prepared source identity and availability. |
| Listen | Master and cue evidence, route health, and current signal age. Events never convey authority. |
| Musical context and interpretations | Distinct authored downbeats, phrases, and uncertain timing evidence. |
| Practice and Stitch | Prepare and compare exact transitions before or beside a set. Listening approvals remain scoped. |
| Baste and Thread | Fresh read-only Live context and saved project inspection. Neither is the instrument writer. |
| Native MIDI and instruments | Reuse descriptors and identity rules. Add a separately qualified performance adapter without broadening fixture claims. |

Preparation, timed handoffs, and recovery remain compositions of these modules.
Per-step EQ, effects, timed moves, and clock recovery belong in Shuttle's
execution adapter after qualification. b2b supplies authority to those steps.
It does not add a second conductor or arrangement engine. The shared
[live performance specification](live-performance.md) describes the rollout.

A rehearsed handoff remains immutable evidence bound to exact source versions,
the track pair, cue, tempo, phase, envelopes, effects, and routing. Changing one
of those inputs creates a new candidate. A kept rehearsal is neither a blanket
approval of the full set nor approval of a later release master. b2b preserves
these distinctions when it reuses prepared moves during improvisation.

## Acceptance and rollout

All numbers below are proposed acceptance targets, not measured performance.
Measure them on each declared native profile, with sample sizes and tail
latencies. Failing a target keeps that profile experimental.

- On the qualified profile, human input stops admission of matching agent steps within 20 ms at the 99th percentile. The last already-admitted native message arrives within 50 ms. Report both values separately.
- No step begins after its grant expiry, a relevant generation change, or its deadline. A test clock proves each boundary without wall-clock sleeps.
- Every first audible action has a delivered local notice from the same grant. Suppressed, failed, or stale notice delivery blocks that action.
- A 60-minute session has no unbounded queue, journal growth in memory, or live model dependency. Recording duration has no hidden 20-minute cutoff.
- An explicit invitation can make a settled resource claimable within 100 ms locally. This excludes agent deliberation and native musical scheduling.

Fault tests cover simultaneous claims, revocation during every dispatch step,
stale epochs, lost responses, duplicate gestures, unknown MIDI, touchless
encoders, physical and software value mismatch, app-focus changes, controller
disconnect, audio-route loss, suspended machines, blocked adapters, notice
failure, writer restart, and a full journal disk. No case silently repeats an
audible command or restores old controls.

Authority tests attempt to forge a human actor field, submit a listener event as
authorization, replay an old issuer receipt, expand an agent grant, amend a
revoked permission, and race two policy versions. Every attempt fails without
changing authority. Alias tests claim the physical and software names for one
control concurrently, change a shared effect route under a pending move, and
inject an unqualified monitoring CC. The first two cannot create simultaneous
writers, and the third yields conservatively. Notice tests lose or delay the
delivery acknowledgement on both sides of the action deadline and retain the
correct dispatch outcome without stopping existing playback.

Native acceptance includes a human mixing on one deck while the agent controls
the other, an unrelated headphone adjustment, a deliberate jog interruption,
explicit handback, and proactive reclaim under a standing permission. An
instrument extension adds a software layer through the measured master to its
own acceptance. Beat, phrase, harmonic, and vocal
quality require attributed listening to the actual captured output.

The first release excludes arbitrary controller support, unrestricted LiveAPI
writes, remote multi-user jamming, automatic track replacement, autonomous
vocal-ending decisions, and phase-accurate scheduling inferred from screenshots.
Keep unsupported capabilities unavailable in discovery. Publish measured native
results separately from synthetic tests and from musical feedback.

Control journals are local and private by default. Retained audio follows
Listen's separate opt-in policy. Exporting a support bundle requires a redacted
preview and explicit selection of any recordings or personal paths.
