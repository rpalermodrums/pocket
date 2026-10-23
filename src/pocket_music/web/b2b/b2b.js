// SPDX-License-Identifier: AGPL-3.0-only
// The b2b host page. Every server string is inserted with textContent. The issuer token is read
// from the URL fragment once, removed from the address bar and kept only in memory, so a reload
// leaves a locked page that can still stop, hold and deny.
import {
  ackPayload, blockedLabel, changedSettings, eventLines, hostNowAfter, isLocked, noticeBanner, ownerLabel,
  padControl, rawMessage, remainingMs, requestId, seconds, shouldAck, tokenFromHash, waitingReason,
} from "/b2b-model.mjs";

const token = tokenFromHash(location.hash);
history.replaceState(null, "", location.pathname);

const $ = id => document.getElementById(id);
// One mutable holder for page state; every binding stays const.
const app = {csrf: null, uiEpoch: null, snapshot: null, publicView: null, receivedAt: 0, current: true,
             acked: new Set(), ackTries: new Map(), previews: new Map(), signatures: new Map(),
             panelBuilt: false, sliders: new Map(), presence: Promise.resolve()};
// A rejected ack (say, one that beat a "visible" report) is tried again, but not forever.
const ACK_TRIES = 3;

class HttpError extends Error {
  constructor(status, data) {
    super(data.error || `Unexpected response (${status})`);
    this.status = status;
    this.data = data;
  }
}

function el(tag, text, attributes = {}) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  return node;
}

function button(text, onClick, attributes = {}) {
  const node = el("button", text, {type: "button", ...attributes});
  node.addEventListener("click", onClick);
  return node;
}

const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const newRequestId = () => requestId(crypto.getRandomValues(new Uint8Array(12)));

async function api(path, {body, key = "issuer"} = {}) {
  const headers = {"X-Pocket-CSRF": key === "issuer" ? token || "" : app.csrf || ""};
  const options = {cache: "no-store", credentials: "same-origin", headers};
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    Object.assign(options, {method: "POST", body: JSON.stringify(body)});
  }
  const response = await fetch(path, options);
  const data = await response.json().catch(() => null);
  if (!response.ok || data === null || typeof data !== "object") {
    throw new HttpError(response.status, data || {error: `Unexpected response (${response.status})`});
  }
  return data;
}

function showError(error) {
  const box = $("error");
  const code = error && error.data ? error.data.code : null;
  box.textContent = code === "stale_page_state" ? waitingReason(code) : String(error.message || error);
  box.hidden = false;
}

function clearError() {
  $("error").hidden = true;
}

function view() {
  return app.snapshot || app.publicView;
}

function triple() {
  const current = view();
  return current ? {expected_session_epoch: current.session_epoch,
                    expected_control_revision: current.control_revision,
                    expected_state_sha256: current.state_sha256} : {};
}

// One page action: post it, explain a refusal in words, then refresh the view either way.
async function perform(path, body, key = "issuer") {
  clearError();
  try {
    const result = await api(path, {body, key});
    if (result.outcome && result.outcome.kind === "refused") {
      showError(new Error(`${waitingReason(result.outcome.code)}: ${result.outcome.detail}`));
    }
    return result;
  } catch (error) {
    showError(error);
    return null;
  } finally {
    await refresh();
  }
}

const pad = {control: null};  // the synthetic hand-back pad, once the panel is built

// Reductions need only the public CSRF token; fetch it again if the page doesn't have it yet.
async function reduce(path, fields) {
  if (pad.control) pad.control.noteReduction(path, fields);  // a Stop or Hold may cancel a press in flight
  if (!app.csrf) {
    try {
      app.csrf = (await api("/api/public", {key: "public"})).csrf_token;
    } catch (error) {
      showError(error);
    }
  }
  return perform(path, {request_id: newRequestId(), ...triple(), ...fields}, "public");
}
const increase = (path, fields) => perform(path, {request_id: newRequestId(), ...triple(), ...fields});

async function refresh() {
  try {
    if (isLocked(token) || !app.current) {
      renderPublic(await api("/api/public", {key: "public"}));
    } else {
      adopt(await api(`/api/state?ui_epoch=${encodeURIComponent(app.uiEpoch)}`));
    }
  } catch (error) {
    showError(error);
  }
}

function adopt(snapshot) {
  app.snapshot = snapshot;
  app.receivedAt = performance.now();
  if (snapshot.current === false && app.current) {
    // Another page attached: this one no longer receives notices, so it falls back to the locked view.
    app.current = false;
    publicLoop();
  }
  render(snapshot);
  acknowledgeVisible();
}

function hostNow() {
  const current = view();
  return current ? hostNowAfter(current.host_monotonic_ns, performance.now() - app.receivedAt) : null;
}

// The notice gate: acknowledge each fetched, pending notice once, and only while this page is visible.
function acknowledgeVisible() {
  const snapshot = app.snapshot;
  if (!snapshot || !app.current) return;
  for (const notice of snapshot.notices) {
    if (!shouldAck(document.visibilityState, notice, app.acked)) continue;
    app.acked.add(notice.notice_id);
    const tries = (app.ackTries.get(notice.notice_id) || 0) + 1;
    app.ackTries.set(notice.notice_id, tries);
    const retry = () => {
      if (tries < ACK_TRIES) app.acked.delete(notice.notice_id);  // the next render tries again
    };
    api("/api/notices/ack", {body: ackPayload(notice, app.uiEpoch)}).then(answer => {
      if (!answer.accepted && answer.outcome === "rejected") retry();
      return refresh();
    }, error => {
      retry();
      showError(error);
    });
  }
}

// Presence reports go one at a time, so an older "hidden" can never land after a newer "visible".
function sendPresence() {
  app.presence = app.presence.then(async () => {
    if (isLocked(token) || !app.uiEpoch || !app.current) return;
    try {
      const visible = document.visibilityState === "visible";
      await api("/api/presence", {body: {ui_epoch: app.uiEpoch, visible}});
    } catch (error) {
      showError(error);
    }
  });
  return app.presence;
}

function countdown(deadline) {
  return el("span", seconds(remainingMs(hostNow(), deadline)), {"data-deadline": deadline});
}

function renderClock() {
  const now = hostNow();
  for (const node of document.querySelectorAll("[data-deadline]")) {
    node.textContent = seconds(remainingMs(now, node.getAttribute("data-deadline")));
  }
}

// Sections with inputs re-render only when what they show changes, so typing is never lost.
function changed(name, value) {
  const signature = JSON.stringify(value);
  if (app.signatures.get(name) === signature) return false;
  app.signatures.set(name, signature);
  return true;
}

function render(snapshot) {
  const whose = app.current ? "this page receives notices"
    : "another page attached; reopen the terminal link here";
  const {session_epoch: epoch, control_revision: revision} = snapshot;
  $("session").textContent = `Session epoch ${epoch} · revision ${revision} · ${whose}`;
  $("locked").hidden = true;
  $("journal").hidden = !snapshot.journal_degraded;
  $("fault").hidden = !snapshot.host_fault;
  renderSettings(snapshot.settings, snapshot.settings_defaults);
  renderNotices(snapshot.notices, true);
  renderResources(snapshot);
  const label = new Map(snapshot.resources.map(resource => [resource.resource_id, resource.label]));
  const labels = ids => ids.map(id => label.get(id) || id);
  renderGrants(snapshot.grants.map(grant => ({grant_id: grant.grant_id, reason: grant.reason,
    labels: labels(grant.resources), deadline: grant.lease_expires_monotonic_ns})),
               snapshot.invitations.map(invitation => ({invitation_id: invitation.invitation_id,
    labels: labels(invitation.resources), deadline: invitation.expires_monotonic_ns})));
  renderPermission(snapshot);
  renderProposals(snapshot);
  renderEvents(snapshot.recent_events);
  renderPanel(snapshot.synthetic_panel);
}

function renderNotices(notices, keyed) {
  const pending = notices.filter(notice => notice.state === "pending");
  const ended = keyed ? notices.filter(notice => notice.state !== "pending").slice(-3) : [];
  const items = [...pending, ...ended].map(notice => {
    const item = el("li", null, notice.state === "pending" ? {} : {class: "ended"});
    item.append(el("strong", noticeBanner(notice)));
    if (notice.state === "pending") {
      const deadline = notice.deadline_monotonic_ns || null;
      if (deadline) item.append(countdown(deadline));
      item.append(button("Deny", () => reduce("/api/notices/deny",
        {notice_id: notice.notice_id, grant_id: notice.grant_id})));
    }
    return item;
  });
  $("notices").replaceChildren(...(items.length ? items : [el("li", "No agent claim is waiting.",
    {class: "ended"})]));
}

function groupOf(snapshot, resource) {
  return resource.conflict_group
    ? snapshot.resources.filter(other => other.conflict_group === resource.conflict_group)
      .map(other => other.resource_id)
    : [resource.resource_id];
}

function renderResources(snapshot) {
  const permission = snapshot.permissions[0] || null;
  const reasons = new Map(snapshot.grants.map(grant => [grant.grant_id, grant.reason]));
  const invited = new Set(snapshot.invitations.flatMap(invitation => invitation.resources));
  const rows = snapshot.resources.map(resource => {
    const owner = resource.owner;
    const proactive = Boolean(permission && permission.proactive_claims.allowed
      && permission.resources.includes(resource.resource_id));
    const row = el("tr");
    const name = el("td", resource.label);
    name.append(el("span", resource.resource_id, {class: "id"}));
    const ownerCell = el("td", null, {class: "owner"});
    if (owner.kind === "agent") {  // the lease counts down in place
      ownerCell.append("Agent, ", countdown(owner.expires_monotonic_ns),
                       ` left. Its reason: ${reasons.get(owner.grant_id) || "none given"}`);
    } else {
      ownerCell.append(ownerLabel(owner, {proactive, invited: invited.has(resource.resource_id)}));
    }
    if (resource.blocked_by && owner.kind === "unowned" && (proactive || invited.has(resource.resource_id))) {
      ownerCell.append(el("span", blockedLabel(resource.blocked_by), {class: "id"}));
    }
    const actions = el("td", null, {class: "actions"});
    const held = owner.hold === "explicit";
    const only = [resource.resource_id];
    if (!held) actions.append(button("Hold", () => reduce("/api/holds", {resources: only})));
    if (held) actions.append(button("Release", () => increase("/api/holds/release", {resources: only})));
    if (!held && !["agent", "unavailable", "pickup_pending"].includes(owner.kind)) {
      const group = groupOf(snapshot, resource);  // a conflict group is handed back whole; holds stay
      actions.append(button("Invite", () => increase("/api/invitations", {resources: group})));
    }
    if (owner.kind === "agent") {
      actions.append(button("Take it back", () => reduce("/api/grants/revoke", {grant_id: owner.grant_id})));
    }
    if (owner.kind === "pickup_pending") {
      actions.append(button("Accept", () => increase("/api/pickup/accept", {resource: only[0]})));
    }
    row.append(name, ownerCell, actions);
    return row;
  });
  $("resources").replaceChildren(...rows);
  $("resources-table").hidden = false;
}

function renderGrants(grants, invitations) {
  const held = grants.map(grant => {
    const item = el("li", `Agent holds ${grant.labels.join(", ")} for `);
    item.append(countdown(grant.deadline), `. Its reason: ${grant.reason} `,
                button("Take it back", () => reduce("/api/grants/revoke", {grant_id: grant.grant_id})));
    return item;
  });
  const offered = invitations.map(invitation => {
    const item = el("li", `Handed to the agent, not yet claimed: ${invitation.labels.join(", ")} · `);
    item.append(countdown(invitation.deadline), " ",
                button("Withdraw", () => reduce("/api/invitations/withdraw",
                                                {invitation_id: invitation.invitation_id})));
    return item;
  });
  $("grants").replaceChildren(...held, ...offered);
}

function previewCard(key, preview, onIssue, onCancel) {
  const card = el("div", null, {class: "card"});
  const lines = el("ol");
  for (const line of preview.lines) lines.append(el("li", line));
  card.append(el("p", "Exactly what you would allow:"), lines);
  if (onIssue) {
    const nameLabel = el("label", "Your name ");
    const name = el("input", null, {type: "text", maxlength: "80", autocomplete: "name", id: `${key}-name`});
    nameLabel.append(name);
    const statementLabel = el("label", "Your note (optional) ");
    const statement = el("input", null, {type: "text", maxlength: "500", id: `${key}-statement`});
    statementLabel.append(statement);
    const plain = "Your name and note are recorded as written; they don't sign anything.";
    card.append(nameLabel, statementLabel, el("p", plain, {class: "meta"}));
    card.append(button("Issue", () => {
      if (!name.value.trim()) return showError(new Error("Type your name before issuing."));
      return onIssue(name.value.trim(), statement.value);
    }));
  }
  if (onCancel) card.append(" ", button("Close", onCancel, {class: "quiet"}));
  return card;
}

function expectedPermission(snapshot) {
  const head = snapshot.permission_head;
  return head ? {expected_permission_id: head.permission_id, expected_permission_version: head.version,
                 expected_permission_sha256: head.state_sha256} : {};
}

async function review(key, body) {
  try {
    // Keep the exact change with its preview: confirming sends what was previewed, nothing newer.
    app.previews.set(key, {...await api("/api/policy/preview", {body}), change: body.amend?.change});
  } catch (error) {
    showError(error);
  }
  app.signatures.delete("permission");
  app.signatures.delete("proposals");
  render(app.snapshot);
}

function closePreview(key) {
  app.previews.delete(key);
  app.signatures.delete("permission");
  app.signatures.delete("proposals");
  render(app.snapshot);
}

async function issue(key, snapshot, name, statement) {
  const preview = app.previews.get(key);
  const result = await perform("/api/policy/issue", {
    request_id: newRequestId(), proposal_id: preview.proposal_id, proposal_sha256: preview.proposal_sha256,
    preview_sha256: preview.preview_sha256, attribution_name: name, attribution_statement: statement,
    ...expectedPermission(snapshot)});
  if (result && result.outcome && result.outcome.kind === "issued") closePreview(key);
}

async function amend(key, permission) {
  const preview = app.previews.get(key);
  const result = await perform("/api/policy/amend", {
    request_id: newRequestId(), permission_id: permission.permission_id,
    expected_permission_version: permission.version, expected_permission_sha256: permission.state_sha256,
    change: preview.change, preview_sha256: preview.preview_sha256});
  if (result && result.outcome && result.outcome.kind === "amended") closePreview(key);
}

function renderPermission(snapshot) {
  const active = snapshot.permissions[0] || null;
  const head = snapshot.permission_head;
  if (!changed("permission", [active && active.state_sha256, head && head.state_sha256,
                              [...app.previews.keys()]])) return;
  const box = $("permission");
  if (active) {
    const card = el("div", null, {class: "card"});
    const line = el("p", `Permission ${active.permission_id} · version ${active.version} · `);
    line.append(countdown(active.expires_monotonic_ns), " left");
    const claims = active.proactive_claims.allowed ? "Idle claims allowed." : "Invited claims only.";
    const moves = active.action_kinds.join(", ");
    const scope = `Controls: ${active.resources.join(", ")}. Moves: ${moves}. ${claims}`;
    card.append(line, el("p", scope, {class: "meta"}));
    const actions = el("div", null, {class: "row"});
    if (active.proactive_claims.allowed) {
      actions.append(button("Turn off proactive claims", () => review("off", {amend: {
        permission_id: active.permission_id, change: {proactive_allowed: false}}})));
    }
    const minutes = el("input", null, {type: "number", min: "1", max: "240", value: "5",
                                        "aria-label": "Minutes left after shortening"});
    const shorter = () => ({expires_in_ms: Math.max(1, Number(minutes.value) || 5) * 60000});
    actions.append(minutes, button("Shorten", () => review("shorten",
      {amend: {permission_id: active.permission_id, change: shorter()}})));
    actions.append(button("Revoke", () => reduce("/api/permissions/revoke",
      {permission_id: active.permission_id})));
    card.append(actions);
    for (const key of ["off", "shorten"]) {
      const preview = app.previews.get(key);
      if (!preview) continue;
      const confirm = previewCard(key, preview, null, () => closePreview(key));
      confirm.append(" ", button("Confirm change", () => amend(key, active)));
      card.append(confirm);
    }
    box.replaceChildren(card);
    return;
  }
  if (head) {
    const card = el("div", null, {class: "card"});
    const why = head.end_reason ? ` (${String(head.end_reason).replaceAll("_", " ")})` : "";
    card.append(el("p", `Permission ${head.permission_id} version ${head.version} is ${head.state}${why}. `
      + "Nothing carries over."));
    const preview = app.previews.get("reissue");
    if (preview) {
      const onIssue = (name, statement) => issue("reissue", snapshot, name, statement);
      card.append(previewCard("reissue", preview, onIssue, () => closePreview("reissue")));
    } else {
      card.append(button("Issue again", () => review("reissue", {reissue_of: head.permission_id})));
    }
    box.replaceChildren(card);
    return;
  }
  box.replaceChildren(el("p", "No standing permission. The agent can use controls only when you invite it.",
                         {class: "meta"}));
}

function renderProposals(snapshot) {
  const proposals = snapshot.proposals.filter(proposal => proposal.source !== "reissue");
  if (!changed("proposals", [proposals.map(p => p.sha256), [...app.previews.keys()]])) return;
  $("proposals").replaceChildren(...(proposals.length ? proposals.map(proposal => {
    const key = `proposal-${proposal.proposal_id}`;
    const item = el("li", null, {class: "card"});
    item.append(el("p", `Proposal ${proposal.proposal_id} from the agent: ${proposal.draft.statement}`));
    const preview = app.previews.get(key);
    if (preview) {
      item.append(previewCard(key, preview, (name, statement) => issue(key, snapshot, name, statement),
                              () => closePreview(key)));
    } else {
      const id = {proposal_id: proposal.proposal_id};
      item.append(button("Review", () => review(key, id)), " ",
                  button("Dismiss", () => increase("/api/proposals/dismiss", id), {class: "quiet"}));
    }
    return item;
  }) : [el("li", "No open proposals.", {class: "meta"})]));
}

// Session settings are the agent's choice at b2b_prepare; say so whenever one differs from the default.
function renderSettings(settings, defaults) {
  const changed = changedSettings(settings, defaults);
  $("settings").textContent = changed.length
    ? `The agent prepared this session with changed settings: ${changed.join(", ")}.` : "";
  $("settings").hidden = changed.length === 0;
}

function renderEvents(events) {
  $("events").replaceChildren(...eventLines(events).reverse().map(line => el("li", line)));
}

// The synthetic controller panel -----------------------------------------------------------------

function send(message, extra = {}) {
  return api("/api/synthetic/input", {body: {message, ...extra}}).catch(showError);
}

function shortName(address) {
  return address.replace("controller/synthetic/", "");
}

// A synthetic fader is not motorized: the slider shows where it physically sits, never the software value.
function slider(input, physical) {
  const label = el("label", `${shortName(input.address)} fader `);
  const output = el("output", physical ?? "–");
  const range = el("input", null, {type: "range", min: "0", max: "127", value: String(physical ?? 64)});
  const state = {sending: false, queued: null};
  const flush = async () => {
    if (state.sending || state.queued === null) return;
    const next = state.queued;
    state.queued = null;
    state.sending = true;
    await send(rawMessage(input.message, next));
    state.sending = false;
    flush();
  };
  const move = () => {
    output.textContent = range.value;
    state.queued = Number(range.value);
    flush();
  };
  range.addEventListener("input", move);
  range.addEventListener("pointerup", move);  // a touch sends its position even when it didn't change
  label.append(output, range);
  app.sliders.set(input.address, {range, output});
  return label;
}

function press(input) {
  return button(shortName(input.address), async () => {
    await send(rawMessage(input.message, 127));
    await send(rawMessage(input.message, 0));
  });
}

function buildPanel(panel) {
  const controls = $("panel-controls");
  for (const input of panel.inputs) {
    if (input.classification === "gesture") continue;
    if (input.encoding === "absolute_7bit") {
      controls.append(slider(input, panel.physical[panel.aliases[input.address]] ?? null));
    } else if (input.encoding === "relative_twos_complement") {
      const box = el("div", `${shortName(input.address)} `);
      box.append(button("−", () => send(rawMessage(input.message, -4))), " ",
                 button("+", () => send(rawMessage(input.message, 4))));
      controls.append(box);
    } else {
      controls.append(press(input));
    }
  }
  controls.append(button("Send unknown message", () => send([0xb3, 99, 1]), {class: "quiet"}));
  const gesture = panel.gestures[0];
  const padButton = $("handback");
  pad.control = padControl(gesture, send, () => performance.now());
  const down = () => {
    if (pad.control.press()) padButton.setAttribute("aria-pressed", "true");
  };
  const up = () => {
    if (pad.control.release()) padButton.setAttribute("aria-pressed", "false");
  };
  padButton.textContent = `${gesture.label} (hold at least ${gesture.long_press_ms / 1000} s)`;
  padButton.addEventListener("pointerdown", down);
  for (const name of ["pointerup", "pointercancel", "pointerleave"]) padButton.addEventListener(name, up);
  const faults = [["Disconnect controller", "disconnect"], ["Reconnect controller", "reconnect"],
                  ["Lose master route", "lose_route"], ["Restore master route", "restore_route"],
                  ["Restart writer", "restart_writer"]];
  $("panel-faults").replaceChildren(...faults.map(([text, action]) => button(text, () =>
    api("/api/synthetic/fault", {body: {action}}).then(refresh, showError), {class: "quiet"})));
  app.panelBuilt = true;
}

function renderPanel(panel) {
  $("panel-section").hidden = !panel;
  if (!panel) return;
  if (!app.panelBuilt) buildPanel(panel);
  const dependencies = Object.entries(panel.dependencies).map(([name, state]) => `${name} ${state}`)
    .join(", ");
  $("panel-state").textContent = `Adapter epoch ${panel.adapter_epoch} · ${dependencies}`;
  for (const [address, {range, output}] of app.sliders) {
    const resource = panel.aliases[address];
    const physical = panel.physical[resource];
    if (physical !== undefined && physical !== null && document.activeElement !== range) {
      range.value = String(physical);
    }
    const software = panel.software[resource];
    output.textContent = `${physical ?? "–"}${software === undefined ? "" : ` · software ${software}`}`;
  }
}

// The locked page: no issuer token, so only what the public CSRF allows -----------------------------

function renderPublic(publicView) {
  app.publicView = publicView;
  app.csrf = publicView.csrf_token || app.csrf;
  app.receivedAt = performance.now();
  const {session_epoch: epoch, control_revision: revision} = publicView;
  $("session").textContent = `Session epoch ${epoch} · revision ${revision}`;
  $("locked").hidden = false;
  $("journal").hidden = !publicView.journal_degraded;
  $("fault").hidden = !publicView.host_fault;
  renderNotices(publicView.notices.map(notice => ({...notice, state: "pending",
    deadline_monotonic_ns: hostNowAfter(publicView.host_monotonic_ns, notice.remaining_ms)})), false);
  const deadline = item => hostNowAfter(publicView.host_monotonic_ns, item.remaining_ms);
  renderGrants(publicView.agent_grants.map(grant => ({...grant, deadline: deadline(grant)})),
               (publicView.invitations || []).map(invitation => ({...invitation,
                                                                  deadline: deadline(invitation)})));
  $("resources-table").hidden = true;
  const permission = publicView.permission;
  $("permission").replaceChildren(permission
    ? button(`Revoke permission ${permission.permission_id}`, () => reduce("/api/permissions/revoke",
      {permission_id: permission.permission_id}))
    : el("p", "No standing permission.", {class: "meta"}));
  $("proposals").replaceChildren(el("li", "Reopen the page link from the host terminal to review proposals.",
                                    {class: "meta"}));
  $("events").replaceChildren(...publicView.holds.map(rid => el("li", `You hold ${rid}`)));
  $("panel-section").hidden = true;
}

// Startup ----------------------------------------------------------------------------------------------

async function waitLoop() {
  while (app.current) {
    try {
      const cursor = app.snapshot ? app.snapshot.events_cursor.sequence : 0;
      adopt(await api(`/api/wait?after_event=${cursor}&ui_epoch=${encodeURIComponent(app.uiEpoch)}`));
    } catch (error) {
      showError(error);
      await pause(1000);
    }
  }
}

async function publicLoop() {
  while (true) {
    await refresh();
    await pause(1000);
  }
}

async function start() {
  $("stop").addEventListener("click", () => reduce("/api/panic", {}));
  $("hold-all").addEventListener("click", () => reduce("/api/holds", {resources: "all"}));
  try {
    const publicView = await api("/api/public", {key: "public"});
    app.csrf = publicView.csrf_token;
    app.publicView = publicView;
  } catch (error) {
    showError(error);
  }
  if (isLocked(token)) {
    publicLoop();
    return;
  }
  try {
    app.uiEpoch = (await api("/api/attach", {body: {}})).ui_epoch;
  } catch (error) {
    showError(error);
    return;
  }
  await sendPresence();
  await refresh();
  document.addEventListener("visibilitychange", () => {
    sendPresence().then(acknowledgeVisible);  // the host must hear "visible" before an ack counts
  });
  setInterval(sendPresence, 1000);
  setInterval(renderClock, 250);
  waitLoop();
}

start();
