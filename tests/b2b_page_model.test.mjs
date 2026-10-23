// SPDX-License-Identifier: AGPL-3.0-only
// Pure b2b page rules: owners in words, BigInt countdowns, and when a notice may be acknowledged.
import assert from "node:assert/strict";
import test from "node:test";

import {
  ackPayload, changedSettings, eventLine, eventLines, hostNowAfter, isLocked, noticeBanner, ownerLabel,
  rawMessage, remainingMs, requestId, seconds, shouldAck, tokenFromHash, waitingReason,
} from "../src/pocket_music/web/b2b/b2b-model.mjs";

test("owners read as words", () => {
  assert.equal(ownerLabel({kind: "unowned"}), "Free, invite only");
  assert.equal(ownerLabel({kind: "unowned"}, {proactive: true}), "Free, agent may claim it when idle");
  assert.equal(ownerLabel({kind: "unowned"}, {proactive: true, invited: true}), "Free, handed to the agent");
  assert.equal(eventLine({type: "gesture_released", outcome: "short_press_ignored"}),
               "Hand-back pad released too soon: nothing handed back");
  assert.equal(eventLine({type: "gesture_released", outcome: "agent_control_stopped"}),
               "Hand-back pad released: agent control is stopped, nothing handed back");
  assert.equal(ownerLabel({kind: "human", hold: "explicit"}), "You (holding)");
  assert.equal(ownerLabel({kind: "human", hold: "activity"}), "You (moving)");
  assert.equal(ownerLabel({kind: "agent", expires_monotonic_ns: "1002400000000"},
                          {reason: "shape the level", hostNow: "1000000000000"}),
               "Agent, 2.4 s left. Its reason: shape the level");
  assert.equal(ownerLabel({kind: "pickup_pending", physical_value: 98, target_value: 40}),
               "Fader 98, software 40");
  assert.equal(ownerLabel({kind: "pickup_pending", physical_value: null, target_value: 40}),
               "Fader position unknown, software 40");
  assert.equal(ownerLabel({kind: "pickup_pending", physical_value: 9, target_value: 40, hold: "explicit"}),
               "Fader 9, software 40 (you hold it)");
  assert.equal(ownerLabel({kind: "unavailable", reason: "controller_disconnected"}),
               "Unavailable: controller disconnected");
  assert.equal(ownerLabel({kind: "unavailable", reason: "route_lost:master", hold: "explicit"}),
               "Unavailable: master route lost (you hold it)");
  assert.equal(ownerLabel(null), "Unknown");
});

test("countdowns use BigInt host time beyond 2^53 nanoseconds", () => {
  const now = "9007199254740993000";  // past 2^53, where Number would round
  const deadline = (BigInt(now) + 1500000001n).toString();
  assert.equal(remainingMs(now, deadline), 1500);
  assert.equal(remainingMs(deadline, now), 0);
  assert.equal(remainingMs("12.5", deadline), null);
  assert.equal(remainingMs(now, undefined), null);
  assert.equal(hostNowAfter(now, 250.9), (BigInt(now) + 250000000n).toString());
  assert.equal(hostNowAfter(now, -5), now);
});

test("a notice is acknowledged only while visible, only while pending and only once", () => {
  const notice = {notice_id: "n-1", grant_id: "g-1", session_epoch: "h1.1", state: "pending",
                  action_deadline_monotonic_ns: "9007199254740993001"};
  const acked = new Set();
  assert.equal(shouldAck("hidden", notice, acked), false);
  assert.equal(shouldAck("visible", {...notice, state: "acknowledged"}, acked), false);
  assert.equal(shouldAck("visible", notice, acked), true);
  acked.add("n-1");
  assert.equal(shouldAck("visible", notice, acked), false);
  // The deadline string is echoed exactly; a Number would have rounded it.
  assert.deepEqual(ackPayload(notice, "ui-1"), {notice_id: "n-1", grant_id: "g-1", session_epoch: "h1.1",
    ui_epoch: "ui-1", action_deadline_monotonic_ns: "9007199254740993001"});
});

test("hostile server text stays plain text", () => {
  const text = "<img src=x onerror=alert(1)> & \"quotes\"";
  assert.equal(noticeBanner({state: "pending", text}), text);
  assert.equal(noticeBanner({state: "denied", text}), `You denied: ${text}`);
  assert.equal(noticeBanner({state: "delivery_unknown", text}),
               "The agent's claim was cancelled: this page didn't show it in time. Nothing was sent.");
  assert.equal(ownerLabel({kind: "agent", expires_monotonic_ns: "1"}, {reason: text, hostNow: "1"}),
               `Agent, 0.0 s left. Its reason: ${text}`);
  assert.equal(eventLine({type: "notice_created", text}), `Notice: ${text}`);
});

test("refusal codes become waiting reasons, and the locked state needs no token", () => {
  assert.equal(waitingReason("human_active"), "You (moving)");
  assert.equal(waitingReason("pickup_unsettled"), "Waiting for you to settle the fader");
  assert.equal(waitingReason("stale_page_state"), "This page was out of date; it refreshed. Press again.");
  assert.equal(waitingReason("some_new_code"), "some new code");
  assert.equal(waitingReason(undefined), "");
  assert.equal(isLocked(null), true);
  assert.equal(isLocked("short"), true);
  assert.equal(isLocked("x".repeat(43)), false);
  assert.equal(tokenFromHash("#k=" + "A".repeat(43)), "A".repeat(43));
  assert.equal(tokenFromHash("#k=<script>"), null);
  assert.equal(tokenFromHash(""), null);
});

test("panel messages and request ids are exact", () => {
  assert.deepEqual(rawMessage({type: "cc", channel: 2, number: 19}, 90), [0xb1, 19, 90]);
  assert.deepEqual(rawMessage({type: "note", channel: 8, number: 64}, 127), [0x97, 64, 127]);
  // A relative jog sends two's complement.
  assert.deepEqual(rawMessage({type: "cc", channel: 1, number: 33}, -4), [0xb0, 33, 124]);
  assert.equal(requestId(new Uint8Array([0, 15, 255])), "page-000fff");
});

test("long countdowns read in minutes, and a move's steps read as one line", () => {
  assert.equal(seconds(2400), "2.4 s");
  assert.equal(seconds(1732400), "28 min 52 s");
  assert.equal(seconds(3900000), "1 h 5 min");
  assert.equal(seconds(null), "–");
  const step = (n, request) => ({type: "step_admitted", request_id: request,
                                 resource: "synthetic/right/gain", step: n, count: 3});
  const done = {type: "action_completed", outcome: "verified", resource: "synthetic/right/gain"};
  assert.deepEqual(eventLines([{type: "page_attached"}, step(1, "a"), step(2, "a"), step(3, "a"), done,
                               step(1, "b")]),
                   ["This page attached", "Agent move on synthetic/right/gain: step 3 of 3 admitted",
                    "Agent move verified on synthetic/right/gain",
                    "Agent move on synthetic/right/gain: step 1 of 3 admitted"]);
});

test("settings the agent changed are listed next to their defaults", () => {
  const defaults = {uncertainty_ms: 2000, activity_release_ms: 2000};
  assert.deepEqual(changedSettings({uncertainty_ms: 0, activity_release_ms: 2000}, defaults),
                   ["uncertainty_ms 0 (default 2000)"]);
  assert.deepEqual(changedSettings(defaults, defaults), []);
  assert.deepEqual(changedSettings(undefined, defaults), []);
});
