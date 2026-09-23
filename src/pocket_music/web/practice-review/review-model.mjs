// Pure review-state helpers shared by the page and Node tests. No DOM, network or playback.

export const DECISIONS = [
  ["", "No decision (note only)"],
  ["keep", "Keep"],
  ["revise", "Revise"],
  ["reject", "Reject"],
  ["no_addition", "No addition"],
];

// Converting the player's position to a frame is an explicit, displayed step:
// the page always shows the integer frames that would be saved.
export function secondsToFrame(seconds, rate) {
  if (!Number.isFinite(seconds) || !Number.isInteger(rate) || rate <= 0) return null;
  return Math.round(seconds * rate);
}

export function frameToSeconds(frame, rate) {
  return frame / rate;
}

export function formatSeconds(seconds) {
  if (!Number.isFinite(seconds)) return "–";
  const minutes = Math.floor(seconds / 60);
  const rest = seconds - minutes * 60;
  return `${minutes}:${rest.toFixed(3).padStart(6, "0")}`;
}

export function intervalError(start, end, frames) {
  if (!Number.isInteger(start) || !Number.isInteger(end)) return "Choose both a start and an end.";
  if (start < 0 || end > frames) return "The interval must stay inside this item.";
  if (end <= start) return "The end must come after the start.";
  return null;
}

export function newDraft(itemId, previewSha) {
  return {itemId, previewSha, start: null, end: null, actor: "", note: "", decision: "", listened: false,
          clientRequestId: null};
}

export function draftHasContent(draft) {
  return Boolean(draft && (draft.note.trim() || draft.listened || draft.decision));
}

// A draft is bound to one item and the exact preview heard. Switching away or a
// changed preview must never silently retarget it.
export function switchBlocked(draft, targetItemId) {
  return draftHasContent(draft) && draft.itemId !== targetItemId;
}

export function draftStale(draft, currentPreviewSha) {
  return Boolean(draft && draft.previewSha && currentPreviewSha !== draft.previewSha);
}

export function saveProblem(draft, frames) {
  if (!draft || !draft.previewSha) return "Prepare and listen to this item's preview first.";
  const interval = intervalError(draft.start, draft.end, frames);
  if (interval) return interval;
  if (!draft.actor.trim()) return "Add your name so the report is attributed.";
  if (!draft.note.trim()) return "Write what you heard.";
  if (!draft.listened) return "Confirm that you listened to this interval.";
  return null;
}

export function reportPayload(draft, revision) {
  return {
    expected_revision: revision,
    item_id: draft.itemId,
    preview_sha256: draft.previewSha,
    interval_frames: [draft.start, draft.end],
    actor: draft.actor.trim(),
    note: draft.note,
    decision: draft.decision || null,
    listened: draft.listened === true,
    client_request_id: draft.clientRequestId,
  };
}

export function newClientRequestId(randomBytes) {
  return Array.from(randomBytes, byte => byte.toString(16).padStart(2, "0")).join("");
}

// Keyboard shortcuts never fire while someone types or operates a control.
export function shortcutAllowed(tagName, isContentEditable) {
  const tag = String(tagName || "").toUpperCase();
  return !isContentEditable && !["INPUT", "TEXTAREA", "SELECT", "BUTTON", "AUDIO", "SUMMARY", "A"].includes(tag);
}

export function reportLine(row, labels) {
  const who = `${row.actor} (${row.actor_kind === "human" ? "person" : "agent"})`;
  const item = labels[row.item_id] || "Unknown item";
  const [start, end] = row.interval_frames;
  const heard = row.reviewed_audio
    ? `declared preview ${row.reviewed_audio.preview_sha256.slice(0, 12)}`
    : "playback identity not recorded";
  return {who, item, frames: `${start}–${end}`, heard, decision: row.decision || "no decision",
          kind: row.evidence_kind === "attributed_human_listening" ? "Human listening report" : "Agent report"};
}
