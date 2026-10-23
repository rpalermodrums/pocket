# b2b: share the controls with an agent

> **In brief.** You and an agent each own named controls. You hand some back with a
> pad or a button, or let the agent use idle ones under a permission you issue.
> Before the agent's first move, a notice shows on your page, and your touch always
> wins: the controls you move are yours again before its next step.
>
> **You need** macOS or Linux and Python. This version is experimental and runs only
> against a built-in synthetic two-deck mixer. It opens no Serato, controller, MIDI
> or audio device.

Playing back to back means two people sharing one set of decks. b2b is the same
idea with an agent: a small local host decides, control by control, who may change
what right now. It never decides what should happen musically, and it never puts
anything back after you take a control.

## Who owns what

Every control has exactly one owner at a time. The host page shows it in words:

| Owner | What it means |
|---|---|
| Free, invite only | Nobody owns it. The agent may claim it only after you hand it back. |
| Free, agent may claim it when idle | A permission you issued lets the agent claim it once it has been still long enough. |
| Free, handed to the agent | You invited the agent to it, and it hasn't claimed it yet. Withdraw takes the invitation back. |
| You (holding) | You held it on the page, or reserved it at the start. It stays yours until you release it. |
| You (moving) | You touched it. It becomes free again two seconds after your last movement. |
| Agent, *time* left. Its reason: *reason* | The agent holds a short lease on it. The reason is the agent's own words. |
| Fader 98, software 40 | After the agent let go, the fader and the software disagree. See [pickup](#when-a-fader-doesnt-match-pickup). |
| Unavailable | The controller or the master route is gone. Nothing can claim it. |

A pickup or unavailable row ends in "(you hold it)" when you held that control, so
it stays yours once it settles.

Four ways to play fall out of these owners rather than being separate modes:
invite only (everything free, nothing claimable until you hand it back),
collaborate (a permission lets the agent use idle controls), you hold (you keep
some controls on the page) and agent holds selected controls (a short lease it
claimed under an invitation or your permission).

The controls are named resources such as `synthetic/right/gain`. A physical control
and the software parameter it moves share one owner, so `controller/synthetic/channel-2/trim`
and `synthetic/right/gain` can never be held by two people. Some controls yield
together: a jog wheel yields the whole deck motion group, and the effect unit's
route and depth are claimed as one. `b2b_observe` lists every resource, its aliases
and its group.

## Start the host

Run the host yourself, in your own terminal, with a new folder for its private files:

```sh
python -m pocket_music.b2b_host --runtime-dir private/b2b/run-1
```

It prints the page address to the terminal, not to a file:

```text
Open the b2b page: http://127.0.0.1:51234/#k=…
Keep it visible while the agent works. Ctrl-C stops this host.
```

The part after `#k=` is the right to issue permissions and acknowledge notices, so
keep that address to yourself. The page reads it once, removes it from the address
bar and keeps it only in memory. Keep the page open and visible beside your decks:
a claim is refused when no visible page could show its notice.

Without a terminal, for example under a script, the host refuses to start unless
you pass `--allow-piped-stdout`. Then the address is printed in the JSON status
line, and whoever reads that output holds the right it carries. This is a speed
bump, not a guarantee; if the agent starts the host itself, it can read the address
too, and the boundary is gone.

If you reload the page, it comes back locked. **Stop agent control**, **Hold every
control**, **Deny**, **Take it back**, **Withdraw** and **Revoke** keep working, because
they can only take authority away. Everything else needs the address from the terminal again:
acknowledging notices, inviting, issuing, **Release**, **Accept**, **Shorten**,
**Turn off proactive claims** and the synthetic controller. No new move can start
until you reopen it. Any local program that can reach the port can trigger those
reductions too.

Ctrl-C stops the host. Every agent grant ends first, and the decks keep playing.
The runtime folder keeps `connection.json` and `journal.ndjson`; keep runtime
folders out of Git, because the journal is a private record of the session.

## Prepare a session

The agent binds a session to your host with `b2b_prepare`. From the command line:

```sh
pocket b2b prepare --spec prepare.json --output private/b2b-session
```

```json
{"runtime_dir": "private/b2b/run-1",
 "human_reserved": ["synthetic/left/gain", "synthetic/left/filter",
                    "synthetic/left/transport", "synthetic/left/position"],
 "proposed_policy": {
   "resources": ["synthetic/right/gain", "synthetic/right/filter"],
   "action_kinds": ["set", "ramp"],
   "limits": [{"resource": "synthetic/right/gain", "minimum": 40, "maximum": 110}],
   "proactive_claims": {"allowed": true, "idle_interval_ms": 4000, "cooldown_ms": 5000},
   "max_lease_ms": 3000, "duration_ms": 1800000,
   "statement": "Shape the right deck while you mix."}}
```

Preparing grants nothing. `human_reserved` controls start as yours (holding), and
the agent can never undo that. A `proposed_policy` becomes a proposal on your page,
waiting for you to read it. If the agent changed any session setting, such as a
timeout, the page says which, next to its default. Each host holds one session; a
second, different prepare is refused. The new session folder holds a sealed `b2b.json`, with no
token or nonce in it. Every other provider takes that `session_dir`.

## Hand the right deck back

Long-press **Hand back** on the synthetic controller panel (half a second to five
seconds), or press **Invite** on a row of the page. That creates one invitation for
those controls, which the agent can use once. A short press, a bounce or a press
you never release creates nothing. Changed your mind? **Withdraw** on the page, or
**Hold** on one of those controls, ends the invitation before the agent uses it.
If the agent claims it just as you press Withdraw, that claim ends too; after that,
use **Take it back**.

**Invite** never drops a hold. Linked controls, like an FX unit's depth and route,
are handed back together, so release any hold in that pair first. The hand-back
pad is different: a long press hands back its controls, held or not. While the pad
is down, a Hold on one of its controls, or Stop, cancels that press, even if the
Hold or Stop reaches the host first; so does a lost or returning controller or route,
a writer restart, sleep, or a host fault. A second press before a release counts
from the second press. The page sends how long it saw the pad held, so a slow
request can only make a press shorter, never longer.

The agent then:

1. calls `b2b_observe` and reads `session_epoch`, `control_revision` and
   `state_sha256`, plus `monotonic_ns` from the host clock;
2. calls `b2b_claim` with those three values, the resources, the invitation as
   its basis, a reason, a lease of up to ten seconds and a deadline;
3. calls `b2b_act` with the same three values from the claim's receipt.

The claim is all or none. A notice appears on your page straight away, such as
"Agent taking Right deck gain for 3.0 s. Its reason: shape the level", and the visible page
acknowledges it. Until then, no step goes out. If the page can't show it within
one and a half seconds, the grant ends and nothing is sent. **Deny** on the notice
ends it too.

Now move the channel-2 trim. The gain is yours again at once. If the agent was in
the middle of a move, it stops before its next step and is told how far it got.
Nothing is restored or resumed; to try again, it claims again later.

## Let the agent use idle controls

A standing permission lets the agent claim controls without an invitation, once
each has been idle for the interval you chose. The agent can only propose one: an
agent call to `b2b_authorize` always comes back refused, with
`authority_issuer_required` and the proposal attached. You issue it on the page.

Press **Review** on the proposal. The page shows the exact lines you would allow.
For the proposal above they begin:

- May claim these controls once each has been idle for 4.0 s: Right deck filter (0–127), Right deck gain (40–110).
- Moves allowed on those claims: set, ramp. Each one lasts at most 3.0 s.
- Controls you invite aren't bound by this permission: an invited claim follows each control's own range and the host's lease limit.
- Every claim is announced on this page before its first move. If this page can't show it in time, nothing is sent.

A permission governs idle claims only. When the agent proposes a change to the
permission you already issued, the preview begins by saying that it replaces it, and
the lines that follow are the whole new permission.

Type your name and press **Issue**. Your name and note are recorded as written; they
don't sign anything. The page issues exactly the lines you read, or refuses if the
proposal changed underneath you.

While a permission is active, every claim under it still needs a visible page and
its own notice, and still loses to your touch. A claim refused because you were using
the control, one you deny and one you take back all start the cooldown on those
controls, so a busy control can't fill your page with notices.
**Turn off proactive claims** and **Shorten** narrow the permission; **Revoke** and
**Stop agent control** end it.

A permission ends, and never comes back by itself, when the controller or the master
route drops or returns, the writer restarts, the computer sleeps or the host restarts.
Press **Issue again** to review the same lines and issue a new one.

## Moves and outcomes

One act is one bounded move on one control the grant holds:

| Move | For | What it does |
|---|---|---|
| `set` | faders and knobs | One step to a value. |
| `ramp` | faders and knobs | Steps every 50 ms to a target, over 50 ms to 4 s, never more than 16 per step. |
| `nudge` | jog wheels | One relative step of up to 16. |
| `set_state` | play buttons | Toggles only when a fresh readback shows the other state. |

Every step is rechecked just before it goes out: the owner, the lease, the deadlines,
the notice, the permission, the adapter's freshness and the journal. A step that
would be late is cancelled rather than sent in a burst.

The receipt's `outcome` says what happened:

| Outcome | Receipt status | Meaning |
|---|---|---|
| `verified` | `ok` | Every step went out and the control read back the final value, within 2 for a fader. |
| `dispatched_unverified` | `ok` | Every step went out; no readback confirmed the final value. |
| `partially_dispatched` | `partial` | Some steps went out, then it stopped. `last_step` says how far. |
| `cancelled_before_dispatch` | `cancelled` | Nothing went out. `reason` says why. |
| `outcome_unknown` | `outcome_unknown` | A step may have gone out. Never retried, and the grant ends. |
| `refused` | `conflict` or `needs_input` | Nothing was planned. `code` says why. |

"Verified" means only that the control reported the value back. Nothing here
listens to the music, and nothing judges whether a move sounded right. A step that
already went out keeps its own outcome even if your touch or Stop ends the grant
while its acknowledgement is still on the way.

## When a fader doesn't match: pickup

The synthetic faders are not motorized. When the agent lets go of a fader it moved,
the fader may still sit somewhere else. The page then shows both values, such as
"Fader 98, software 40", and the control waits for you. Touch the fader (the jump
from software to fader position is recorded) or press **Accept** on the page. Until
then nobody can claim it. **Hold** keeps it waiting for pickup, so the mismatch stays
visible, and keeps it yours once you settle it. This version has explicit pickup only.

## When b2b says no

Refusals are recorded receipts with a `code`, and each comes with a next step:

| Code | What to do |
|---|---|
| `authority_issuer_required` | Ask the person to review the proposal on the page. |
| `notice_destination_unavailable` | Ask the person to open the page link from the host terminal and keep it visible. |
| `human_active` | Wait until `retry_not_before_monotonic_ns`, then observe and claim with a new request ID. |
| `gesture_in_progress` | The person is pressing a pad. Observe until gestures read idle, then claim with a new request ID. |
| `resource_held_by_human` | Don't claim it. It stays held until the person releases it. |
| `classification_uncertain`, `claim_cooldown` | Retry after `retry_not_before_monotonic_ns`. |
| `pickup_unsettled` | The person settles it on the page; nothing the agent sends can. |
| `permission_not_active`, `permission_scope`, `proactive_not_permitted` | Observe and use the current permission, or ask for an invitation. |
| `baseline_unknown`, `state_unknown`, `evidence_stale` | Observe first. A ramp on a control with no readback needs a `set` first. |
| `adapter_unavailable`, `resource_unavailable` | Observe, and wait for the controller or master route to return; then claim again in the new epoch. |
| `journal_unavailable` | Observe, and wait for the person to start a new host. |

Mistakes in the request itself are errors, not receipts: a wrong field or type is
`invalid_arguments`, a name that isn't declared is `unsupported_profile` (the hint
names `b2b_observe`), and a stale observation is `stale_revision`. None of them use
up the request ID. See [contracts and errors](contracts.md#opt-in-machine-errors).

## When something goes wrong

- **The notice wasn't shown.** The grant ends at its deadline and nothing is sent.
  A notice the page fetched but didn't acknowledge reads `notice_deadline_missed`;
  one it never showed, or showed while hidden, reads `notice_delivery_unknown`.
- **A response was lost.** Call `b2b_request_status` with the same `session_dir`
  and `request_id`. Sending the same request again with the same ID returns the
  retained receipt without moving anything; changed inputs under the same ID are
  refused. Never retry under a new ID to find out what happened. See
  [recovery](contracts.md#recovery).
- **The epoch changed.** A controller reconnect, a restored route, a writer restart
  or sleep and wake starts a new adapter epoch. Every grant, invitation and the
  permission end, and every observation goes stale. The host checks for a sleep at
  every entry, so the first call after waking already sees it. See
  [time has more than one clock](concepts.md#time-has-more-than-one-clock).
- **The journal is full or failed.** Agent control stops and the page says so.
  Touching your controls, Hold and Stop keep working; releasing a hold, inviting,
  accepting a pickup or issuing waits for a new host. Restart the host with a new
  runtime folder.
- **The host hit an internal fault.** Agent control stops for the rest of that host,
  as when the journal fails: the page shows a banner and the terminal prints the
  fault. Touching your controls, Hold, Stop, Deny and Take it back keep working;
  releasing a hold, inviting, accepting a pickup or issuing waits for a new host.
  `b2b_prepare`, `b2b_authorize`, `b2b_claim` and `b2b_act` fail with `io_error`;
  `b2b_observe`, `b2b_events`, `b2b_request_status`, `b2b_release` and `b2b_revoke`
  still answer.
- **The host stopped.** Calls to the old session fail with `io_error`.
  `b2b_request_status` still answers from the old journal: `complete` with its
  receipt, `stopped_before_dispatch` when nothing was admitted, or `outcome_unknown`
  when a step was admitted. A new host starts with no grants and no permission.
- **What the journal survives.** It is written before each step goes out and synced
  before the host answers each change, so it keeps outcomes through a host crash. It
  makes no claim about a power loss or an operating system crash. If the journal can't
  be read in full, `b2b_request_status` says `outcome_unknown` rather than claiming
  nothing was sent.

## Providers

Python, the CLI and MCP all call the same nine providers. CLI actions take `--spec`
JSON; `pocket b2b-observe --spec …` and the other flat commands work too.

| Provider | CLI action | Purpose |
|---|---|---|
| `b2b_prepare` | `b2b prepare` | Bind a new session to a running host; record an optional proposal |
| `b2b_authorize` | `b2b authorize` | Propose a standing permission (always refused as issuer) |
| `b2b_observe` | `b2b observe` | Owners, grants, notices, permissions and evidence, each with its age |
| `b2b_claim` | `b2b claim` | Claim idle controls under an invitation or the permission |
| `b2b_act` | `b2b act` | One bounded move as short, rechecked steps |
| `b2b_release` | `b2b release` | Give back some or all of a grant |
| `b2b_revoke` | `b2b revoke` | Shorten a lease, withdraw a proposal or drop all own authority |
| `b2b_events` | `b2b events` | Sequenced events after a cursor |
| `b2b_request_status` | `b2b request-status` | Inspect a retained request, even after the host stops |

Mutations return [receipts](concepts.md#stores-artifacts-and-handles). All but
`b2b_prepare` take a `request_id`, reused only to retry the same request, as everywhere
in [Pocket](concepts.md#nothing-is-overwritten); an identical `b2b_prepare` retry
returns the same session. Using b2b from an assistant works as in
[Use Pocket with an agent](agents.md).

## What this version establishes

- **Synthetic only.** The only adapter is `synthetic-two-deck/v1`, an in-memory
  mixer. Its jump-on-touch faders, immediate acknowledgements and toggling play
  buttons are this model's choices, not measured behavior of any device. Shuttle
  stays the separate way to play Serato with an agent.
- **Ordering, not timing.** The tests prove ordering and cancellation boundaries on
  a test clock: a touch stops the next step, an expiry wins over a due step, a late
  acknowledgement never produces a step. They measure no latency and run no soak, so
  no timing target is stated here as a result.
- **Gated next steps.** `capabilities_list(domain="collaboration")` lists, across its
  pages, what is not available yet: Serato control, Listen evidence, an instrument extension, a
  monitoring exemption, soft takeover and a programmatic issuer. Each is
  [explicitly closed](concepts.md#profiles-limits-and-refusals) until it is qualified.
- **A local boundary.** The page and the agent socket protect a local boundary only.
  The trusted page and the agent channel are separate code paths inside one process,
  not a sandbox, and a name typed on the page is [attributed](concepts.md#four-kinds-of-evidence),
  never verified.
