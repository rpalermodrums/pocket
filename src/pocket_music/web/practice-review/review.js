// Practice review page. All text from the server is inserted with textContent.
// Media events only move the playhead; a report exists only after an explicit save.
import {
  DECISIONS, draftHasContent, draftStale, formatSeconds, frameToSeconds, intervalError, newClientRequestId,
  newDraft, reportLine, reportPayload, saveProblem, secondsToFrame, shortcutAllowed, switchBlocked,
} from "/review-model.mjs";

const $ = id => document.getElementById(id);
let state = null;
let selectedId = null;
let draft = null;
let actorName = "";
let pendingSwitch = null;
let saving = false;
let stopAt = null;
const positions = {};

async function api(path, body) {
  const options = {cache: "no-store", credentials: "same-origin"};
  if (body !== undefined) {
    Object.assign(options, {method: "POST", body: JSON.stringify(body),
      headers: {"Content-Type": "application/json", "X-Pocket-CSRF": state ? state.csrf_token : ""}});
  }
  const response = await fetch(path, options);
  let data;
  try {
    data = await response.json();
  } catch {
    data = {error: `Unexpected response (${response.status})`};
  }
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function showError(message) {
  $("error").textContent = message;
  $("error").hidden = false;
  $("error").focus();
}

function clearError() {
  $("error").hidden = true;
  $("error").textContent = "";
}

function announce(message) {
  $("status").textContent = message;
}

function item(id = selectedId) {
  return state && state.items.find(candidate => candidate.item_id === id);
}

function previewSha(id = selectedId) {
  const current = item(id);
  return current && current.preview ? current.preview.sha256 : null;
}

function labels() {
  return Object.fromEntries(state.items.map(candidate => [candidate.item_id, candidate.label]));
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function renderItems() {
  const container = $("items");
  container.replaceChildren();
  for (const candidate of state.items) {
    const button = element("button", undefined, "item");
    button.type = "button";
    button.setAttribute("role", "radio");
    button.setAttribute("aria-checked", String(candidate.item_id === selectedId));
    button.tabIndex = candidate.item_id === selectedId ? 0 : -1;
    button.dataset.item = candidate.item_id;
    const seconds = frameToSeconds(candidate.frames, candidate.sample_rate);
    button.append(element("strong", candidate.label),
      element("span", candidate.processing.label),
      element("span", `${formatSeconds(seconds)} · ${candidate.sample_rate} Hz · ${candidate.channels === 1 ? "mono" : "stereo"}`),
      element("span", candidate.signal.usable_for_expectation ? "Signal checks passed"
        : `Signal warning: ${candidate.signal.disposition.replaceAll("_", " ")}`,
        candidate.signal.usable_for_expectation ? undefined : "warning"),
      element("span", candidate.preview ? `Preview ${candidate.preview.short_id}` : "No preview yet"),
      element("span", `Audio ${candidate.short_id}`));
    button.addEventListener("click", () => selectItem(candidate.item_id));
    button.addEventListener("keydown", event => {
      const order = state.items.map(entry => entry.item_id);
      const index = order.indexOf(candidate.item_id);
      const step = {ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1}[event.key];
      if (step) {
        event.preventDefault();
        const target = order[(index + step + order.length) % order.length];
        selectItem(target, true);
      }
    });
    container.append(button);
  }
}

function renderPlayer() {
  const current = item();
  $("item-summary").textContent = `${current.label}: ${current.processing.label}. `
    + `${current.frames} frames at ${current.sample_rate} Hz.`;
  $("provenance").open = false;
  $("provenance-list").replaceChildren();
  $("preview-missing").hidden = Boolean(current.preview);
  $("preview-ready").hidden = !current.preview;
  const audio = $("audio");
  if (current.preview) {
    $("preview-label").textContent = `Browser preview ${current.preview.short_id}: ${current.preview.label}.`;
    const source = `/api/review/audio/${current.item_id}?v=${current.preview.short_id}`;
    if (audio.getAttribute("src") !== source) {
      audio.pause();
      audio.setAttribute("src", source);
      audio.load();
    }
    const occurrences = $("occurrences");
    occurrences.replaceChildren(...current.occurrences.map(entry => element("li",
      `${entry.occurrence_id}: ${formatSeconds(frameToSeconds(entry.output_span_frames[0], current.sample_rate))}–`
      + `${formatSeconds(frameToSeconds(entry.output_span_frames[1], current.sample_rate))}`)));
  } else {
    audio.pause();
    audio.removeAttribute("src");
  }
}

function renderSelection() {
  const current = item();
  if (!current || !draft) return;
  for (const [field, value] of [["start", draft.start], ["end", draft.end]]) {
    const input = $(field);
    if (document.activeElement !== input) {
      input.value = value === null ? "" : frameToSeconds(value, current.sample_rate).toFixed(3);
    }
    input.max = frameToSeconds(current.frames, current.sample_rate).toFixed(3);
  }
  const problem = draft.start === null && draft.end === null ? "Mark where your listening started and ended."
    : intervalError(draft.start, draft.end, current.frames);
  $("interval").textContent = problem || `Frames ${draft.start}–${draft.end} of ${current.frames} `
    + `(${formatSeconds(frameToSeconds(draft.start, current.sample_rate))}–`
    + `${formatSeconds(frameToSeconds(draft.end, current.sample_rate))}). These exact frames will be saved.`;
}

function renderForm() {
  const current = item();
  const stale = draftStale(draft, previewSha());
  const problem = stale ? "The preview changed after this draft began. Cancel the draft and listen again."
    : saveProblem(draft, current.frames);
  $("draft-target").textContent = current.preview
    ? `This report is about ${current.label}, preview ${current.preview.short_id} of audio ${current.short_id}.`
    : `${current.label} needs a preview before you can report on it.`;
  $("save-problem").textContent = problem || "Ready to save. Saving creates a permanent, attributed report.";
  $("save").disabled = saving || Boolean(problem);
  for (const id of ["actor", "note", "decision", "listened"]) $(id).disabled = !current.preview;
  if (document.activeElement !== $("actor")) $("actor").value = draft.actor;
  if (document.activeElement !== $("note")) $("note").value = draft.note;
  $("decision").value = draft.decision;
  $("listened").checked = draft.listened;
}

function render() {
  $("question").textContent = state.question;
  $("comparison-meta").textContent = `${state.comparison.family} · comparison ${state.comparison.short_id} · `
    + (state.comparison.signal_ready ? "signal checks passed" : "signal warnings retained");
  $("listening-state").textContent = state.report_count
    ? `${state.report_count} saved report${state.report_count === 1 ? "" : "s"} in this session. Playing audio never records a report.`
    : "Not yet reviewed. Playing audio never records a report.";
  $("alignment").textContent = state.alignment.synchronized_switching
    ? `Switching keeps the playhead position: ${state.alignment.basis}.`
    : `Switching does not align playback: ${state.alignment.basis}.`;
  renderItems();
  renderPlayer();
  renderSelection();
  renderForm();
}

function resetDraft() {
  draft = newDraft(selectedId, previewSha());
  draft.actor = actorName;
}

function selectItem(id, focus = false) {
  if (id === selectedId) return;
  if (switchBlocked(draft, id)) {
    pendingSwitch = id;
    $("switch-guard-text").textContent = `You have an unsaved report for ${item(draft.itemId).label}. `
      + "Save it, or discard it before switching. A draft never moves to another item.";
    $("switch-guard").hidden = false;
    $("keep-draft").focus();
    return;
  }
  const audio = $("audio");
  const previous = selectedId;
  if (previous) positions[previous] = audio.currentTime || 0;
  audio.pause();
  selectedId = id;
  resetDraft();
  render();
  const target = state.alignment.synchronized_switching && previous ? positions[previous] : positions[id];
  if (item().preview && target) {
    const cue = () => { audio.currentTime = target; };
    if (audio.readyState >= 1) cue(); else audio.addEventListener("loadedmetadata", cue, {once: true});
  }
  announce(`${item().label} selected.`);
  if (focus) document.querySelector(`[data-item="${id}"]`).focus();
}

function mark(field) {
  const current = item();
  if (!current.preview) return;
  draft[field] = secondsToFrame($("audio").currentTime, current.sample_rate);
  draft.clientRequestId = null;
  renderSelection();
  renderForm();
  announce(`${field === "start" ? "Start" : "End"} set to frame ${draft[field]}.`);
}

function playSelection() {
  const current = item();
  if (!current.preview || intervalError(draft.start, draft.end, current.frames)) {
    announce("Mark a valid interval first.");
    return;
  }
  const audio = $("audio");
  audio.currentTime = frameToSeconds(draft.start, current.sample_rate);
  stopAt = frameToSeconds(draft.end, current.sample_rate);
  audio.play().catch(error => showError(`Playback did not start: ${error.message}`));
}

async function makePreview() {
  clearError();
  const button = $("make-preview");
  button.disabled = true;
  announce("Preparing a declared browser preview…");
  try {
    const result = await api("/api/review/previews", {expected_revision: state.revision, item_id: selectedId});
    state = result.state;
    if (draft && !draft.previewSha && draft.itemId === selectedId) draft.previewSha = previewSha();
    render();
    announce(`Preview ready.${result.warnings.length ? " Signal warnings: " + result.warnings.join("; ") : ""}`);
    $("audio").focus();
  } catch (error) {
    showError(`Preview not created: ${error.message}`);
  } finally {
    button.disabled = false;
  }
}

async function save(event) {
  event.preventDefault();
  clearError();
  const current = item();
  const problem = draftStale(draft, previewSha()) ? "The preview changed after this draft began."
    : saveProblem(draft, current.frames);
  if (problem) {
    $("save-problem").textContent = problem;
    $("save-problem").focus();
    return;
  }
  if (saving) return;
  saving = true;
  renderForm();
  if (!draft.clientRequestId) draft.clientRequestId = newClientRequestId(crypto.getRandomValues(new Uint8Array(16)));
  try {
    const result = await api("/api/review/reports", reportPayload(draft, state.revision));
    state = result.state;
    actorName = draft.actor;
    showReceipt(result.report);
    resetDraft();
    render();
    $("receipt").focus();
    await loadReports();
  } catch (error) {
    showError(`Report not saved: ${error.message}`);
  } finally {
    saving = false;
    if (state) renderForm();
  }
}

function showReceipt(row) {
  const line = reportLine(row, labels());
  const receipt = $("receipt");
  receipt.replaceChildren(element("h3", "Report saved"),
    element("p", `${line.kind} by ${line.who} about ${line.item}, frames ${line.frames}, ${line.heard}.`),
    element("p", `Decision: ${line.decision}. Report ${row.feedback.sha256.slice(0, 12)} is retained and verified.`));
  receipt.hidden = false;
}

async function loadReports(cursor = null) {
  const list = $("reports");
  try {
    const page = await api(`/api/review/reports${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`);
    if (!cursor) list.replaceChildren();
    for (const row of page.items) {
      const line = reportLine(row, labels());
      const entry = element("li");
      entry.append(element("span", line.kind, "badge"), element("p", `${line.who} · ${line.item} · frames ${line.frames}`),
        element("p", `Heard: ${line.heard} · decision: ${line.decision}`, "meta"), element("p", row.note, "note"));
      list.append(entry);
    }
    if (page.status === "needs_input") {
      list.append(element("li", "A report is larger than this page can show at once; read it with practice_feedback_query."));
    }
    const more = $("more-reports");
    more.hidden = !page.next_cursor;
    more.onclick = () => loadReports(page.next_cursor);
    if (!page.total) list.replaceChildren(element("li", "No reports saved yet."));
  } catch (error) {
    showError(`Reports could not be verified: ${error.message}`);
  }
}

async function loadProvenance() {
  if (!$("provenance").open) return;
  try {
    const detail = await api(`/api/review/items/${selectedId}`);
    const rows = [["Render", detail.render.sha256], ["Audio", detail.audio_sha256], ["Context", detail.context_sha256],
      ["Processing", detail.processing.profile], ["Signal", `${detail.signal.disposition}, peak ${detail.signal.sample_peak.toFixed(4)}, `
        + `RMS ${detail.signal.rms.toFixed(4)}, ${detail.signal.sample_overload_count} overload samples`]];
    if (detail.joins) rows.push(["Joins", detail.joins.map(join => `${join.fade_out_frames}/${join.fade_in_frames} frames at ${join.boundary_frame}`).join("; ")]);
    for (const mapping of detail.mappings) {
      rows.push([`Occurrence ${mapping.occurrence_id}`, `source frames ${mapping.source_span_frames.join("–")} → output `
        + `${mapping.output_span_frames.join("–")}`]);
    }
    if (detail.preview) {
      rows.push(["Preview", detail.preview.sha256], ["Conversion", `${detail.preview.conversion.algorithm}, `
        + `${detail.preview.conversion.rounding}, dither ${detail.preview.conversion.dither}`],
      ["Quantization", `${detail.preview.quantization.samples - detail.preview.quantization.exact_samples} of `
        + `${detail.preview.quantization.samples} samples rounded, max ${detail.preview.quantization.max_abs_error_lsb} LSB`]);
    }
    $("provenance-list").replaceChildren(...rows.flatMap(([term, value]) => [element("dt", term), element("dd", value)]));
  } catch (error) {
    showError(`Provenance could not be verified: ${error.message}`);
  }
}

function updateDraft(field, value) {
  draft[field] = value;
  draft.clientRequestId = null;
  if (field === "actor") actorName = value;
  renderForm();
}

function wire() {
  $("decision").replaceChildren(...DECISIONS.map(([value, label]) => {
    const option = element("option", label);
    option.value = value;
    return option;
  }));
  $("make-preview").addEventListener("click", makePreview);
  $("mark-start").addEventListener("click", () => mark("start"));
  $("mark-end").addEventListener("click", () => mark("end"));
  $("play-selection").addEventListener("click", playSelection);
  for (const field of ["start", "end"]) {
    $(field).addEventListener("input", event => {
      const value = event.target.value === "" ? null : secondsToFrame(Number(event.target.value), item().sample_rate);
      updateDraft(field, value);
      renderSelection();
    });
  }
  $("actor").addEventListener("input", event => updateDraft("actor", event.target.value));
  $("note").addEventListener("input", event => updateDraft("note", event.target.value));
  $("decision").addEventListener("change", event => updateDraft("decision", event.target.value));
  $("listened").addEventListener("change", event => updateDraft("listened", event.target.checked));
  $("report").addEventListener("submit", save);
  $("cancel").addEventListener("click", () => {
    const had = draftHasContent(draft);
    resetDraft();
    $("switch-guard").hidden = true;
    render();
    announce(had ? "Draft discarded. No report was saved." : "Nothing to discard.");
  });
  $("discard-draft").addEventListener("click", () => {
    resetDraft();
    $("switch-guard").hidden = true;
    const target = pendingSwitch;
    pendingSwitch = null;
    announce("Draft discarded. No report was saved.");
    selectItem(target, true);
  });
  $("keep-draft").addEventListener("click", () => {
    pendingSwitch = null;
    $("switch-guard").hidden = true;
    $("note").focus();
  });
  $("provenance").addEventListener("toggle", loadProvenance);
  const audio = $("audio");
  audio.addEventListener("timeupdate", () => {
    if (stopAt !== null && audio.currentTime >= stopAt) {
      audio.pause();
      stopAt = null;
    }
  });
  audio.addEventListener("pause", () => { positions[selectedId] = audio.currentTime; });
  audio.addEventListener("error", () => showError("The browser could not play this preview."));
  document.addEventListener("keydown", event => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (!shortcutAllowed(event.target.tagName, event.target.isContentEditable)) return;
    if (!item() || !item().preview) return;
    if (event.key === " ") {
      event.preventDefault();
      if (audio.paused) audio.play().catch(error => showError(`Playback did not start: ${error.message}`));
      else audio.pause();
    } else if (event.key === "[") {
      mark("start");
    } else if (event.key === "]") {
      mark("end");
    } else if (event.key === "p" || event.key === "P") {
      playSelection();
    }
  });
}

async function start() {
  wire();
  try {
    state = await api("/api/review");
    selectedId = state.items[0].item_id;
    resetDraft();
    render();
    await loadReports();
  } catch (error) {
    $("question").textContent = "This review could not be verified.";
    showError(error.message);
  }
}

start();
