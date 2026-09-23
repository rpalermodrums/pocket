// Pure practice-review page rules: drafts never retarget, playback never reports.
import assert from "node:assert/strict";
import test from "node:test";

import {
  draftHasContent, draftStale, formatSeconds, intervalError, listeningState, newClientRequestId, newDraft,
  reportLine, reportPayload, saveProblem, secondsToFrame, shortcutAllowed, switchBlocked,
} from "../src/pocket_music/web/practice-review/review-model.mjs";

test("frame conversion is explicit and displayed", () => {
  assert.equal(secondsToFrame(1.0000625, 8000), 8001);
  assert.equal(secondsToFrame(0, 44100), 0);
  assert.equal(secondsToFrame(Number.NaN, 8000), null);
  assert.equal(secondsToFrame(1, 0), null);
  assert.equal(formatSeconds(61.5), "1:01.500");
  assert.equal(formatSeconds(59.9996), "1:00.000");
  assert.equal(formatSeconds(2645983 / 44100), "1:00.000");
  assert.equal(formatSeconds(119.9999), "2:00.000");
  assert.equal(formatSeconds(0.0004), "0:00.000");
});

test("the header counts people and agents separately", () => {
  assert.equal(listeningState(0, 0), "Not yet reviewed by a person. Playing audio never records a report.");
  assert.equal(listeningState(0, 1),
    "Not yet reviewed by a person. 1 agent report (technical, not listening). Playing audio never records a report.");
  assert.match(listeningState(2, 0), /^2 human listening reports in this session\./);
});

test("intervals must be nonempty, ordered and inside the item", () => {
  assert.equal(intervalError(0, 10, 10), null);
  assert.match(intervalError(null, 10, 10), /both/);
  assert.match(intervalError(5, 5, 10), /after/);
  assert.match(intervalError(6, 5, 10), /after/);
  assert.match(intervalError(-1, 5, 10), /inside/);
  assert.match(intervalError(0, 11, 10), /inside/);
  assert.match(intervalError(0.5, 5, 10), /both/);
});

test("a draft with content never moves to another item or preview", () => {
  const draft = newDraft("variant-1", "a".repeat(64));
  assert.equal(draftHasContent(draft), false);
  assert.equal(switchBlocked(draft, "baseline"), false);
  draft.note = "Late snare";
  assert.equal(switchBlocked(draft, "baseline"), true);
  assert.equal(switchBlocked(draft, "variant-1"), false);
  assert.equal(draftStale(draft, "a".repeat(64)), false);
  assert.equal(draftStale(draft, "b".repeat(64)), true);
  const ticked = newDraft("baseline", "c".repeat(64));
  ticked.listened = true;
  assert.equal(switchBlocked(ticked, "variant-1"), true);
});

test("saving requires a preview, interval, author, note and explicit listening", () => {
  const draft = newDraft("variant-1", null);
  assert.match(saveProblem(draft, 100), /preview/);
  draft.previewSha = "a".repeat(64);
  assert.match(saveProblem(draft, 100), /both/);
  Object.assign(draft, {start: 10, end: 20});
  assert.match(saveProblem(draft, 100), /name/);
  draft.actor = "  ";
  assert.match(saveProblem(draft, 100), /name/);
  draft.actor = "Ryan";
  assert.match(saveProblem(draft, 100), /heard/);
  draft.note = "The join dips";
  assert.match(saveProblem(draft, 100), /listened/);
  draft.listened = true;
  assert.equal(saveProblem(draft, 100), null);
  draft.clientRequestId = newClientRequestId(new Uint8Array(16).fill(171));
  assert.deepEqual(reportPayload(draft, 7), {
    expected_revision: 7, item_id: "variant-1", preview_sha256: "a".repeat(64), interval_frames: [10, 20],
    actor: "Ryan", note: "The join dips", decision: null, listened: true, client_request_id: "ab".repeat(16)});
});

test("shortcuts never fire while typing or operating controls", () => {
  for (const tag of ["INPUT", "textarea", "SELECT", "BUTTON", "AUDIO", "SUMMARY", "A"]) {
    assert.equal(shortcutAllowed(tag, false), false);
  }
  assert.equal(shortcutAllowed("BODY", false), true);
  assert.equal(shortcutAllowed("DIV", true), false);
});

test("report lines distinguish agents, v1 records and declared previews", () => {
  const labels = {"variant-1": "Variant 1"};
  const human = reportLine({actor: "Ryan", actor_kind: "human", item_id: "variant-1", interval_frames: [1, 2],
    reviewed_audio: {preview_sha256: "f".repeat(64)}, decision: "keep", evidence_kind: "attributed_human_listening"},
  labels);
  assert.equal(human.kind, "Human listening report");
  assert.equal(human.heard, `declared preview ${"f".repeat(12)}`);
  assert.equal(human.audioLabel, "Heard");
  const agent = reportLine({actor: "Agent", actor_kind: "agent", item_id: null, interval_frames: [1, 2],
    decision: null, evidence_kind: "agent_report"}, labels);
  assert.equal(agent.kind, "Agent report");
  assert.equal(agent.heard, "playback identity not recorded");
  assert.equal(agent.audioLabel, "Audio referenced");
  assert.equal(agent.item, "Unknown item");
  assert.equal(agent.decision, "no decision");
});
