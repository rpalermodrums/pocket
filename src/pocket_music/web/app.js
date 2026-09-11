"use strict";
const ui = {snapshot: null, busy: false, nextOffset: null, searchTimer: null, searchToken: 0};
const $ = (id) => document.getElementById(id);
const node = (tag, text, className) => {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = String(text);
  if (className) element.className = className;
  return element;
};
const button = (text, action, className = "secondary") => {
  const element = node("button", text, className);
  element.type = "button";
  element.addEventListener("click", action);
  return element;
};
const tell = (text) => { $("message").textContent = text; $("message").hidden = !text; };
const time = (seconds) => Number.isFinite(seconds) ? `${Math.round(seconds / 60)} min` : "Duration unknown";
const artist = (track) => (track.artists || []).join(", ");
const laneName = (lane) => ({hold: "Hold", lift: "Lift", left_turn: "Left turn", explore: "Explore"}[lane] || lane);

async function request(path, data) {
  const response = await fetch(path, data === undefined ? {cache: "no-store"} : {
    method: "POST", headers: {"Content-Type": "application/json", "X-Pocket-CSRF": ui.snapshot.csrf_token},
    body: JSON.stringify({expected_workspace_revision: ui.snapshot.revision, ...data})
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "The workspace could not complete this action.");
  return payload;
}

async function act(action, success) {
  if (ui.busy) return;
  ui.busy = true;
  document.querySelectorAll("button").forEach((element) => { element.disabled = true; });
  tell("Working…");
  try { await action(); tell(success || ""); }
  catch (error) { tell(`${error.message} Your previous work is preserved. Refresh to reload current state.`); }
  finally {
    ui.busy = false;
    document.querySelectorAll("button").forEach((element) => { element.disabled = element.dataset.unavailable === "true"; });
  }
}

function tab(name) {
  $("workshop").hidden = name !== "workshop";
  $("deck").hidden = name !== "deck";
  document.body.classList.toggle("lowlight", name === "deck");
  ["workshop", "deck"].forEach((key) => {
    $("tab-" + key).classList.toggle("selected", key === name);
    $("tab-" + key).setAttribute("aria-pressed", String(key === name));
  });
}

async function refresh() {
  ui.snapshot = await request("/api/state");
  render();
  await Promise.all([loadTracks(), loadOptions()]);
}

function render() {
  const state = ui.snapshot;
  $("empty-bag").hidden = Boolean(state.bag);
  $("bag-name").textContent = state.bag ? `${state.bag.title} · ${state.bag.summary.track_count} records` : "Bring a few records. Find a direction.";
  if (state.bag) {
    const maximum = Math.min(100, Math.max(1, state.bag.summary.track_count - state.bag.summary.unavailable));
    $("records").max = maximum;
    if (Number($("records").value) > maximum) $("records").value = maximum;
  }
  $("route-form").hidden = !state.bag;
  $("replan").hidden = !state.plan;
  $("prepare").textContent = state.session ? "New session" : "Prepare session";
  $("current-title").textContent = state.current_track ? state.current_track.title : "Choose your starting record";
  $("current-artist").textContent = state.current_track ? artist(state.current_track) : "Use “Start here” in your record bag.";
  document.querySelectorAll("[data-track-choice]").forEach((element) => { element.textContent = state.session ? "Choose record" : "Start here"; });
  const direction = state.session?.intent?.direction || "hold";
  document.querySelectorAll("[data-intent]").forEach((element) => {
    element.classList.toggle("selected", element.dataset.intent === direction);
    element.setAttribute("aria-pressed", String(element.dataset.intent === direction));
  });
  $("history").replaceChildren(...(state.session?.history || []).slice(-15).map((entry) => node("li", `${entry.action}${entry.track_id ? " · " + entry.track_id : ""}`)));
  $("session-info").textContent = state.session ? `Session revision ${state.session.revision} · ${state.session.played_ids.length} chosen · ${state.session.skipped_ids.length} skipped. Shared directory: ${state.session.session_dir}` : "No session yet.";
  renderRoutes();
}

async function loadTracks(offset = 0) {
  const token = ++ui.searchToken;
  const result = await request(`/api/tracks?q=${encodeURIComponent($("search").value)}&offset=${offset}`);
  if (token !== ui.searchToken) return;
  if (offset === 0) $("track-list").replaceChildren();
  result.tracks.forEach((track) => {
    const row = node("div", null, "track-row");
    const info = node("div");
    info.append(node("p", track.title, "track-title"), node("p", artist(track), "track-artist"));
    const profile = track.profile || {};
    const details = [profile.bpm ? `${profile.bpm} BPM` : "Tempo unknown", ...(profile.tags || []).slice(0, 2)];
    if (track.available === false) details.push("Unavailable");
    info.append(node("p", details.join(" · "), "track-details"));
    const start = button(ui.snapshot.session ? "Choose record" : "Start here", () => ui.snapshot.session ? updateSession("choose", {track_id: track.track_id}) : startSession(track.track_id), "quiet");
    start.dataset.trackChoice = "true";
    start.dataset.unavailable = String(track.available === false);
    start.disabled = track.available === false;
    row.append(info, start);
    $("track-list").append(row);
  });
  $("track-count").textContent = result.total_matches ? `${result.total_matches} matching records` : (ui.snapshot.bag ? "No matching records." : "");
  ui.nextOffset = result.next_offset;
  $("more-tracks").hidden = result.next_offset === null;
}

function renderRoutes() {
  if (!ui.snapshot.plan) {
    $("routes").replaceChildren(node("p", "Your first routes will appear here.", "empty-route"));
    return;
  }
  const plan = ui.snapshot.plan;
  $("routes").replaceChildren(...plan.routes.map((route, index) => {
    const article = node("article", null, "route");
    article.append(node("h2", `Route ${index + 1}`), node("p", `${time(route.duration.estimated_performance_seconds)} estimated · ${route.tracks.length} records`, "small muted"));
    const list = node("ol");
    route.tracks.forEach((track) => {
      const item = node("li");
      item.append(node("strong", track.title), node("span", artist(track)));
      list.append(item);
    });
    const actions = node("div", null, "route-actions");
    actions.append(button("Keep this direction", () => feedback(route, "prefer")), button("Try a different route", () => feedback(route, "avoid"), "quiet"));
    const details = node("details");
    details.append(node("summary", "Transition ideas & pair feedback"));
    const lookup = Object.fromEntries(route.tracks.map((track) => [track.track_id, track.title]));
    route.transitions.forEach((transition) => {
      const row = node("div", null, "transition");
      row.append(node("strong", `${lookup[transition.from_track_id]} → ${lookup[transition.to_track_id]}`));
      row.append(node("p", transition.proposal?.treatment || "Review the source sections before mixing."));
      row.append(node("p", (transition.unknowns || []).slice(0, 2).join(" "), "muted"));
      row.append(button("Prefer this pair", () => feedback(route, "prefer", transition), "quiet"), button("Avoid this pair", () => feedback(route, "avoid", transition), "quiet"));
      details.append(row);
    });
    article.append(list, actions, details);
    return article;
  }));
}

function brief() {
  return {title: "Workspace exploration", setting: $("setting").value, target_minutes: Number($("minutes").value), track_count: Number($("records").value)};
}
async function routes(useFeedback = false) {
  await act(async () => {
    ui.snapshot = await request("/api/routes", {brief: useFeedback ? undefined : brief(), seed: Number($("seed").value), use_feedback: useFeedback});
    render();
  }, "Routes saved. These are proposed directions, ready for your review.");
}
async function feedback(route, disposition, transition) {
  await act(async () => {
    ui.snapshot = await request("/api/feedback", {route_id: route.route_id, disposition, expected_plan_sha256: ui.snapshot.plan_handle.sha256,
      from_track_id: transition?.from_track_id, to_track_id: transition?.to_track_id});
    render();
  }, transition ? "Pair feedback saved for this bag and brief." : "Route feedback saved. Try again to apply it.");
}
async function startSession(trackId) {
  await act(async () => {
    ui.snapshot = await request("/api/session", {track_id: trackId, intent: {direction: "hold"}});
    render(); tab("deck"); await loadOptions();
  }, "Session prepared. Choosing a record here does not start playback.");
}
async function updateSession(action, extra) {
  if (!ui.snapshot.session) return tell("Prepare a session or choose a starting record first.");
  await act(async () => {
    const session = ui.snapshot.session;
    ui.snapshot = await request("/api/session/action", {action, expected_revision: session.revision, expected_sha256: session.sha256, ...extra});
    render(); await loadOptions();
  }, action === "choose" ? "Selection saved. Playback remains under your control." : "Session updated.");
}
async function loadOptions() {
  if (!ui.snapshot.session) { $("options").replaceChildren(node("p", "Prepare a session to see your next options.", "muted")); return; }
  const result = await request("/api/options");
  if (!result.options.length) { $("options").replaceChildren(node("p", "No unplayed options remain for this intent. Adjust your intent or start a new session.", "muted")); return; }
  $("options").replaceChildren(...result.options.map((option) => {
    const item = node("article", null, "option");
    item.append(node("span", laneName(option.lane), "lane"), node("h3", option.title), node("p", artist(option), "muted"));
    item.append(node("p", option.proposed_transition?.treatment || "Review an overlap before committing."));
    const actions = node("div", null, "option-actions");
    actions.append(button("Choose next", () => updateSession("choose", {track_id: option.track_id}), "primary"), button("Skip for now", () => updateSession("skip", {track_id: option.track_id}), "quiet"));
    const details = node("details");
    details.append(node("summary", "Why this record / what’s unknown"));
    [...(option.reasons || []), ...(option.unknowns || [])].forEach((text) => details.append(node("p", text)));
    item.append(actions, details);
    return item;
  }));
}

$("tab-workshop").addEventListener("click", () => tab("workshop"));
$("tab-deck").addEventListener("click", () => tab("deck"));
$("refresh").addEventListener("click", () => act(refresh, "Current workspace and session reloaded."));
$("route-form").addEventListener("submit", (event) => { event.preventDefault(); routes(); });
$("replan").addEventListener("click", () => { $("seed").value = Number($("seed").value) + 1; routes(true); });
$("prepare").addEventListener("click", () => startSession(undefined));
$("search").addEventListener("input", () => { clearTimeout(ui.searchTimer); ui.searchTimer = setTimeout(() => loadTracks().catch((error) => tell(error.message)), 180); });
$("more-tracks").addEventListener("click", () => loadTracks(ui.nextOffset).catch((error) => tell(error.message)));
document.querySelectorAll("[data-intent]").forEach((element) => element.addEventListener("click", () => updateSession("intent", {intent: {...ui.snapshot.session?.intent, direction: element.dataset.intent}})));
$("attach-form").addEventListener("submit", (event) => {
  event.preventDefault();
  act(async () => { ui.snapshot = await request("/api/session/attach", {session_dir: $("session-path").value}); render(); await loadOptions(); }, "Shared session loaded.");
});
$("import-file").addEventListener("change", () => act(async () => {
  const file = $("import-file").files[0];
  if (!file) return;
  if (file.size > 2_000_000) throw new Error("JSON import must be under 2 MB.");
  const data = JSON.parse(await file.text());
  ui.snapshot = await request("/api/import", Array.isArray(data) ? {tracks: data, title: "Imported records"} : {tracks: data.tracks, title: data.title || "Imported records"});
  $("import-file").value = ""; $("search").value = "";
  render(); await Promise.all([loadTracks(), loadOptions()]);
}, "Record bag imported. Previous bags, routes and sessions are preserved."));
$("demo").addEventListener("click", () => act(async () => {
  const titles = ["Soft entrance", "Off the grid", "Blue staircase", "Slow current", "Bright corners", "Open room", "Loose thread", "After the rain"];
  const tracks = titles.map((title, index) => ({track_id: `practice-${index + 1}`, title, artists: ["Generated practice record"], duration_seconds: 300 + index * 15,
    catalog_source: "user_list", profile: {provenance: "user", energy: .2 + index * .08, bpm: 112 + index * 2, tags: index % 2 ? ["percussive", "loose"] : ["warm", "spacious"]}}));
  ui.snapshot = await request("/api/import", {title: "Generated practice bag", tracks});
  render(); await Promise.all([loadTracks(), loadOptions()]);
}, "Generated practice bag loaded. These are fictional records, with no audio attached."));
act(refresh);
