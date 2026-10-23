// SPDX-License-Identifier: AGPL-3.0-only
// Pure b2b page rules shared by the page and Node tests. No DOM, network or clock reads.
//
// Every host monotonic value arrives as a decimal string: those nanosecond counts pass 2^53
// after a few days of uptime, so all arithmetic on them uses BigInt and deadlines are echoed
// back exactly as received.

const NS_PER_MS = 1000000n;
const DECIMAL = /^\d{1,20}$/;

export function isLocked(token) {
  return typeof token !== "string" || token.length < 16;
}

// The issuer token travels only in the URL fragment "#k=<token>".
export function tokenFromHash(hash) {
  const match = /^#k=([A-Za-z0-9_-]{16,128})$/.exec(hash || "");
  return match ? match[1] : null;
}

export function remainingMs(hostNowString, deadlineString) {
  if (!DECIMAL.test(hostNowString || "") || !DECIMAL.test(deadlineString || "")) return null;
  const left = BigInt(deadlineString) - BigInt(hostNowString);
  return left <= 0n ? 0 : Number(left / NS_PER_MS);
}

// The host clock as of now: the snapshot's host time plus whole local milliseconds since it arrived.
export function hostNowAfter(hostNowString, elapsedMs) {
  if (!DECIMAL.test(hostNowString || "")) return null;
  const elapsed = Number.isFinite(elapsedMs) && elapsedMs > 0 ? BigInt(Math.floor(elapsedMs)) : 0n;
  return (BigInt(hostNowString) + elapsed * NS_PER_MS).toString();
}

export function seconds(ms) {
  if (ms === null || ms === undefined) return "–";
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  const minutes = Math.floor(ms / 60000);
  return minutes < 60 ? `${minutes} min ${Math.floor((ms % 60000) / 1000)} s`
    : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

const UNAVAILABLE = {
  controller_disconnected: "controller disconnected",
  "route_lost:master": "master route lost",
  mapping_changed: "controller mapping changed; start a new host",
  epoch_restarting: "restarting",
};

// The owner of one control, in words. ``context`` carries what the owner record can't:
// {reason (the grant's), hostNow (string), proactive (an active permission may claim it when idle),
//  invited (an outstanding invitation covers it)}.
export function ownerLabel(owner, context = {}) {
  if (!owner || typeof owner.kind !== "string") return "Unknown";
  if (owner.kind === "unowned") {
    if (context.invited) return "Free, handed to the agent";
    return context.proactive ? "Free, agent may claim it when idle" : "Free, invite only";
  }
  if (owner.kind === "human") return owner.hold === "explicit" ? "You (holding)" : "You (moving)";
  if (owner.kind === "agent") {
    const left = remainingMs(context.hostNow, owner.expires_monotonic_ns);
    return `Agent, ${seconds(left)} left. Its reason: ${context.reason || "none given"}`;
  }
  if (owner.kind === "pickup_pending") {
    const fader = owner.physical_value === null || owner.physical_value === undefined
      ? "Fader position unknown" : `Fader ${owner.physical_value}`;
    const software = owner.target_value === null || owner.target_value === undefined
      ? "software unknown" : `software ${owner.target_value}`;
    return `${fader}, ${software}${owner.hold === "explicit" ? " (you hold it)" : ""}`;
  }
  if (owner.kind === "unavailable") {
    const why = UNAVAILABLE[owner.reason] || String(owner.reason || "unknown").replaceAll("_", " ");
    return `Unavailable: ${why}${owner.hold === "explicit" ? " (you hold it)" : ""}`;
  }
  return "Unknown";
}

const NOTICE_ENDED = {
  acknowledged: "Shown",
  deadline_missed: "The agent's claim was cancelled: this page didn't confirm it in time",
  delivery_unknown: "The agent's claim was cancelled: this page didn't show it in time",
  denied: "You denied",
  cancelled: "Ended",
};

export function noticeBanner(notice) {
  if (!notice || typeof notice.text !== "string") return "";
  if (notice.state === "pending") return notice.text;
  const prefix = NOTICE_ENDED[notice.state] || "Ended";
  return notice.state === "deadline_missed" || notice.state === "delivery_unknown"
    ? `${prefix}. Nothing was sent.` : `${prefix}: ${notice.text}`;
}

// Acknowledge only a pending notice this page fetched, only while the page is visible, and once.
export function shouldAck(visibilityState, notice, ackedIds) {
  return visibilityState === "visible" && Boolean(notice) && notice.state === "pending"
    && typeof notice.notice_id === "string" && !ackedIds.has(notice.notice_id);
}

export function ackPayload(notice, uiEpoch) {
  return {notice_id: notice.notice_id, grant_id: notice.grant_id, session_epoch: notice.session_epoch,
          ui_epoch: uiEpoch, action_deadline_monotonic_ns: String(notice.action_deadline_monotonic_ns)};
}

const WAITING = {
  human_active: "You (moving)",
  gesture_in_progress: "You (moving)",
  resource_held_by_human: "You (holding)",
  classification_uncertain: "Agent waiting after an unrecognised message",
  claim_cooldown: "Agent waiting after a refused or denied claim",
  pickup_unsettled: "Waiting for you to settle the fader",
  notice_destination_unavailable: "Agent waiting for this page",
  notice_delivery_unknown: "The agent's claim was cancelled: this page didn't show it in time",
  notice_deadline_missed: "The agent's claim was cancelled: this page didn't confirm it in time",
  notice_denied: "You denied the agent's claim",
  journal_unavailable: "Journal unavailable. Agent control stopped. Touching your controls, Hold and Stop "
    + "still work.",
  resource_unavailable: "Unavailable",
  authority_issuer_required: "Proposal from the agent · Review",
  stale_page_state: "This page was out of date; it refreshed. Press again.",
};

export function waitingReason(code) {
  if (typeof code !== "string" || !code) return "";
  return WAITING[code] || code.replaceAll("_", " ");
}

// Why a free control can't be claimed: a person-owned reason on a free row is a linked control's.
const LINKED = {resource_held_by_human: "Waiting: a linked control is held",
                human_active: "Waiting: a linked control is moving"};

export function blockedLabel(code) {
  return LINKED[code] || waitingReason(code);
}

export function requestId(bytes) {
  return `page-${Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("")}`;
}

// The three bytes a profile message sends: cc 0xB0 or note 0x90, ORed with channel - 1.
export function rawMessage(message, value) {
  const status = (message.type === "cc" ? 0xb0 : 0x90) | (message.channel - 1);
  return [status, message.number, value & 0x7f];
}

const words = value => String(value ?? "").replaceAll("_", " ");

const EVENT_TEXT = {
  page_attached: () => "This page attached",
  page_detached: () => "The page stopped answering",
  session_registered: () => "An agent session started",
  grant_created: e => `Agent granted ${(e.grant?.resources || []).join(", ")}`,
  grant_ended: e => `Agent grant ended (${String(e.reason || "").replaceAll("_", " ")})`,
  action_completed: e => `Agent move ${String(e.outcome || "").replaceAll("_", " ")} on ${e.resource}`,
  step_cancelled: e => `Agent move stopped before step ${e.from_step} (${words(e.reason)})`,
  claim_refused: e => `Agent claim refused: ${waitingReason(e.code)}`,
  notice_created: e => `Notice: ${e.text}`,
  notice_acknowledged: () => "Notice shown on this page",
  notice_failed: e => waitingReason(`notice_${e.state}`),
  notice_denied: () => "You denied the agent's claim",
  human_input: e => `You moved ${e.resource}`,
  gesture_pressed: () => "Hand-back pad pressed",
  gesture_released: e => ({invitation_created: "Hand-back pad released: controls handed to the agent",
                           coalesced: "Hand-back pad released: the earlier hand-back still stands",
                           short_press_ignored: "Hand-back pad released too soon: nothing handed back",
                           invitation_limit_reached: "Hand-back pad released: too many open invitations",
                           agent_control_stopped: "Hand-back pad released: agent control is stopped, nothing "
                             + "handed back",
                           voided: "Hand-back pad released: Hold or Stop cancelled this press, nothing "
                             + "handed back"}
                          [e.outcome] || "Hand-back pad released"),
  release_lost: () => "Hand-back pad held too long: nothing handed back",
  unknown_input: e => `Unrecognised controller message (${e.classification}); everything yields`,
  invitation_created: () => "Controls handed to the agent",
  invitation_ended: e => `Invitation ${e.state}`,
  proposal_recorded: e => `Proposal ${e.proposal_id} from the agent`,
  permission_issued: e => `Permission ${e.permission_id} issued`,
  permission_amended: e => `Permission ${e.permission_id} changed to version ${e.version}`,
  permission_ended: e => `Permission ${e.permission_id} ${e.state}`,
  pickup_pending: e => `${e.resource} waits for pickup`,
  pickup_settled: e => `${e.resource} picked up`,
  epoch_changed: e => `New adapter epoch (${String(e.reason).replaceAll("_", " ")})`,
  resource_unavailable: e => `Unavailable: ${String(e.reason)}`,
  resource_available: () => "Controls available again",
  panic: () => "Stop agent control: every agent grant ended",
  journal_degraded: () => waitingReason("journal_unavailable"),
};

export function eventLine(event) {
  const text = EVENT_TEXT[event?.type];
  return text ? text(event) : String(event?.type || "event").replaceAll("_", " ");
}

// A step is announced as it is admitted, just before it goes to the adapter, so this says "admitted".
function stepLine(event) {
  return `Agent move on ${event.resource}: step ${event.step} of ${event.count} admitted`;
}

// Session settings the agent chose at b2b_prepare that differ from the defaults, as "name value (default d)".
export function changedSettings(settings, defaults) {
  return Object.entries(settings || {}).filter(([name, value]) => (defaults || {})[name] !== value)
    .map(([name, value]) => `${name} ${value} (default ${defaults[name]})`);
}

// The person's event list, oldest first: consecutive steps of one move read as a single line,
// so a long ramp can't push everything else out of the last few lines.
export function eventLines(events) {
  return events.reduce((lines, event) => {
    const last = lines[lines.length - 1];
    if (event.type === "step_admitted" && last && last.request_id === event.request_id) {
      return [...lines.slice(0, -1), {...last, text: stepLine(event)}];
    }
    const text = event.type === "step_admitted" ? stepLine(event) : eventLine(event);
    return [...lines, {request_id: event.type === "step_admitted" ? event.request_id : null, text}];
  }, []).map(line => line.text);
}
