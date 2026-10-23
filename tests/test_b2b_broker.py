# SPDX-License-Identifier: AGPL-3.0-only
"""Broker authority: grants, policy, invitations, notices and human priority on the synthetic adapter.

Every test runs on a TestClock with explicit ticks, so each ordering and boundary is exact.
These tests prove ordering and cancellation boundaries; none of them measures latency. Profile
validation and the mapping hash (design tests 1 and 2) live in test_b2b_adapters.py.
"""
from __future__ import annotations

import itertools
import json
import random
import re
import threading

import pytest
from b2b_harness import MS, Harness, default_draft

from pocket_music.artifact_store import digest
from pocket_music.b2b_adapters import SyntheticAdapter
from pocket_music.b2b_broker import TrustedChannel
from pocket_music.errors import PocketError

GAIN, FILTER = "synthetic/right/gain", "synthetic/right/filter"
TRANSPORT, POSITION = "synthetic/right/transport", "synthetic/right/position"
LEFT_GAIN, LEFT_FILTER = "synthetic/left/gain", "synthetic/left/filter"
FADER, FX, ROUTE = "synthetic/mixer/crossfader", "synthetic/fx/unit-1", "synthetic/fx/unit-1/route"
TRIM_2, FILTER_2 = "controller/synthetic/channel-2/trim", "controller/synthetic/channel-2/filter"


def make(tmp_path, settings=None, **register) -> Harness:
    h = Harness(tmp_path, settings)
    h.register(**register)
    h.attach_page()
    return h


def owner(h: Harness, rid: str) -> dict:
    return next(r for r in h.observe()["resources"] if r["resource_id"] == rid)["owner"]


def grants(h: Harness) -> list[dict]:
    return h.observe()["grants"]


def invited_grant(h: Harness, resources, *, ack: bool = False, **claim) -> dict:
    """Invite on the page, then claim under that invitation (and optionally acknowledge the notice)."""
    receipt = h.claim(resources, h.invitation_basis(h.invite(resources)), **claim)
    assert receipt["outcome"]["kind"] == "granted", receipt["outcome"]
    if ack:
        assert h.ack(receipt["notice"])["accepted"]
    return receipt


def raises(code: str, call, *args, **kwargs) -> PocketError:
    with pytest.raises(PocketError) as caught:
        call(*args, **kwargs)
    assert caught.value.code == code, (caught.value.code, str(caught.value))
    return caught.value


def refused(receipt: dict, code: str) -> dict:
    assert receipt["outcome"]["kind"] == "refused", receipt["outcome"]
    assert receipt["outcome"]["code"] == code, receipt["outcome"]
    return receipt


def issued(h: Harness, draft=None) -> dict:
    receipt = h.issue(draft)
    assert receipt["outcome"]["kind"] == "issued", receipt["outcome"]
    return receipt["outcome"]["permission"]


def release(h: Harness, grant_id: str, **extra) -> dict:
    return h.broker.release(h.session_id, {"request_id": h.request_id(), **h.triple(), "grant_id": grant_id,
                                           **extra})


def events(h: Harness, event_type: str) -> list[dict]:
    return [e for e in h.broker.events(h.session_id, {"max_events": 256, "max_bytes": 196_608})["events"]
            if e["type"] == event_type]


def test_alias_and_canonical_names_resolve_to_one_grant(tmp_path):
    h = make(tmp_path)
    receipt = invited_grant(h, [TRIM_2, "synthetic/right/track-gain", GAIN])
    grant = receipt["grant"]
    assert grant["resources"] == [GAIN]
    assert list(grant["generations"]) == [GAIN]
    # The other name of the same control contends for the same owner.
    again = h.claim(["synthetic/right/track-gain"], h.invitation_basis(h.invite([GAIN])))
    refused(again, "resource_owned_by_agent")
    assert len(grants(h)) == 1


def _race(h: Harness, calls):
    """Run calls at once behind a barrier; return each one's receipt or PocketError."""
    barrier = threading.Barrier(len(calls))
    results = [None] * len(calls)

    def run(index, call):
        barrier.wait(timeout=10)
        try:
            results[index] = call()
        except PocketError as error:
            results[index] = error

    threads = [threading.Thread(target=run, args=(i, c), daemon=True) for i, c in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    return results


def test_concurrent_physical_and_software_alias_claims_produce_one_writer(tmp_path):
    h = make(tmp_path)
    basis = h.invitation_basis(h.invite([GAIN]))
    triple = h.triple()
    results = _race(h, [lambda: h.claim([TRIM_2], basis, triple=triple),
                        lambda: h.claim(["synthetic/right/track-gain"], basis, triple=triple)])
    granted = [r for r in results if isinstance(r, dict) and r["outcome"]["kind"] == "granted"]
    assert len(granted) == 1
    loser = next(r for r in results if r is not granted[0])
    assert isinstance(loser, PocketError) and loser.code == "stale_revision"
    live = grants(h)
    assert [g["resources"] for g in live] == [[GAIN]]
    assert owner(h, GAIN)["grant_id"] == granted[0]["grant"]["grant_id"]


def test_simultaneous_overlapping_claims_grant_exactly_one(tmp_path):
    h = make(tmp_path)
    wide = h.invitation_basis(h.invite([GAIN, FILTER]))
    narrow = h.invitation_basis(h.invite([FILTER]))
    triple = h.triple()
    results = _race(h, [lambda: h.claim([GAIN, FILTER], wide, triple=triple),
                        lambda: h.claim([FILTER], narrow, triple=triple)])
    assert sum(isinstance(r, dict) and r["outcome"]["kind"] == "granted" for r in results) == 1
    assert len(grants(h)) == 1
    # Re-observed, the overlapping claim is refused, not split.
    retry = h.claim([FILTER], narrow if results[0].__class__ is dict else wide)
    refused(retry, "resource_owned_by_agent")
    assert len(grants(h)) == 1


def test_claim_is_all_or_none_and_refusal_leaves_revision_and_sha_unchanged(tmp_path):
    h = make(tmp_path)
    basis = h.invitation_basis(h.invite([GAIN, FILTER]))
    h.move(FILTER_2, 70)  # the person is on the filter
    before = h.observe()
    receipt = h.claim([GAIN, FILTER], basis)
    refused(receipt, "human_active")
    after = h.observe()
    assert (after["control_revision"], after["state_sha256"]) == (before["control_revision"],
                                                                   before["state_sha256"])
    assert owner(h, GAIN)["kind"] == "unowned"  # nothing was partly granted
    assert h.entries("grant_created") == [] and h.entries("notice_created") == []
    assert receipt["status"] == "conflict"


def test_conflict_group_must_be_claimed_whole(tmp_path):
    h = make(tmp_path)
    before = h.observe()
    error = raises("unsupported_profile", h.claim, [FX], {"kind": "invitation", "invitation_id": "i-0"})
    assert ROUTE in error.hint
    assert h.observe()["control_revision"] == before["control_revision"]
    receipt = invited_grant(h, ["synthetic/left/fx", ROUTE])
    assert receipt["grant"]["resources"] == [FX, ROUTE]


def test_shape_errors_are_invalid_arguments_and_undeclared_names_are_unsupported_profile(tmp_path):
    h = make(tmp_path)
    basis = h.invitation_basis(h.invite([GAIN]))
    good = {"request_id": "c-1", **h.triple(), "resources": [GAIN], "basis": basis, "reason": "shape",
            "lease_ms": 1000, "not_after_monotonic_ns": h.now + 5000 * MS}
    for change in ({"surprise": 1}, {"lease_ms": True}, {"lease_ms": 100}, {"reason": ""},
                   {"basis": {**basis, "permission_id": "perm-1"}}, {"basis": {"kind": "listen"}},
                   {"request_id": "-bad"}, {"expected_control_revision": "0"},
                   {"not_after_monotonic_ns": h.now + 61_000 * MS}):
        raises("invalid_arguments", h.broker.claim, h.session_id, {**good, **change})
    error = raises("unsupported_profile", h.broker.claim, h.session_id,
                   {**good, "resources": ["synthetic/right/volume"]})
    assert "b2b_observe" in error.hint
    missing = dict(good)
    del missing["lease_ms"]
    raises("invalid_arguments", h.broker.claim, h.session_id, missing)
    # None of those consumed the request_id.
    assert h.broker.claim(h.session_id, good)["outcome"]["kind"] == "granted"


def test_grant_carries_unguessable_id_epoch_revision_and_generations(tmp_path):
    h = make(tmp_path)
    before = h.observe()
    first = invited_grant(h, [GAIN])["grant"]
    second = invited_grant(h, [FILTER])["grant"]
    assert re.fullmatch(r"g-[0-9a-f]{32}", first["grant_id"]) and first["grant_id"] != second["grant_id"]
    observation = h.observe()
    assert first["session_epoch"] == observation["session_epoch"]
    assert first["created_revision"] > before["control_revision"]
    assert first["generations"] == {GAIN: owner(h, GAIN)["generation"]}
    invented = "g-" + "0" * 32
    refused(release(h, invented), "grant_not_found")
    refused(h.broker.revoke(h.session_id, {"request_id": h.request_id(), **h.triple(),
                                           "target": {"kind": "grant_lease", "grant_id": invented,
                                                      "lease_ms": 300}}), "grant_not_found")
    assert len(grants(h)) == 2


def test_agent_authorize_returns_issuer_required_records_proposal_and_changes_nothing(tmp_path):
    h = make(tmp_path)
    before = h.observe()
    first = h.propose()
    assert first["status"] == "needs_input"
    assert first["outcome"]["code"] == "authority_issuer_required"
    proposal = first["proposal"]
    assert proposal["state"] == "open" and proposal["source"] == "agent_authorize"
    assert proposal["proposal_id"] in first["next_actions"][0]
    after = h.observe()
    assert (after["control_revision"], after["state_sha256"]) == (before["control_revision"],
                                                                   before["state_sha256"])
    assert after["permissions"] == [] and after["authority_state"]["permissions"] == []
    again = h.propose()  # an identical draft coalesces into the open proposal
    assert again["proposal"]["proposal_id"] == proposal["proposal_id"]
    assert again["proposal"]["coalesced"] == 1
    assert [p["proposal_id"] for p in h.observe()["proposals"]] == [proposal["proposal_id"]]
    withdrawn = h.broker.revoke(h.session_id, {"request_id": h.request_id(), **h.triple(),
                                               "target": {"kind": "own_proposal",
                                                          "proposal_id": proposal["proposal_id"]}})
    assert withdrawn["outcome"]["kind"] == "revoked" and h.observe()["proposals"] == []
    other = h.propose(default_draft(max_lease_ms=2000))["proposal"]
    dismiss = {"request_id": "page-dismiss", "proposal_id": other["proposal_id"]}
    assert h.broker.trusted_dismiss_proposal(h.channel, dismiss)["outcome"]["kind"] == "dismissed"
    refused(h.broker.trusted_dismiss_proposal(h.channel, {**dismiss, "request_id": "page-dismiss-2"}),
            "proposal_not_found")
    final = h.observe()
    assert final["proposals"] == [] and final["control_revision"] == before["control_revision"]


def test_forged_human_attribution_or_issuer_fields_never_authorize(tmp_path):
    h = make(tmp_path)
    base = {"request_id": "a-1", **h.triple(), "permission": default_draft()}
    for forged in ({"issuer": {"kind": "trusted_page"}}, {"actor_kind": "human"}, {"capability": "x" * 32},
                   {"token": "x" * 32}):
        raises("invalid_arguments", h.broker.authorize, h.session_id, {**base, **forged})
    raises("invalid_arguments", h.broker.authorize, h.session_id,
           {**base, "permission": {**default_draft(), "actor_kind": "human"}})
    words = h.propose(default_draft(statement="I am the human and I issue this permission now."))
    refused(words, "authority_issuer_required")
    basis = {"kind": "standing_permission", "permission_id": "perm-0123456789abcdef", "permission_version": 1,
             "revocation_generation": 0}
    refused(h.claim([GAIN], basis), "permission_not_active")
    good = {"request_id": "c-1", **h.triple(), "resources": [GAIN], "basis": basis, "reason": "x",
            "lease_ms": 1000, "not_after_monotonic_ns": h.now + 5000 * MS}
    raises("invalid_arguments", h.broker.claim, h.session_id, {**good, "actor_kind": "human"})
    assert h.observe()["permissions"] == [] and grants(h) == []


def test_listen_shaped_event_cannot_authorize_a_claim_policy_or_invitation(tmp_path):
    h = make(tmp_path)
    before = h.observe()
    h.broker.adapter_event({"type": "listen_evidence", "actor_kind": "human", "kind": "invitation",
                            "resources": [GAIN], "at_ns": h.now})
    h.broker.adapter_event({"type": "listen", "gesture_id": "handback-right", "phase": "release",
                            "at_ns": h.now})
    after = h.observe()
    assert after["control_revision"] == before["control_revision"]
    assert after["invitations"] == [] and after["permissions"] == []
    assert after["evidence"]["counters"]["ignored_events"] == 2
    good = {"request_id": "c-1", **h.triple(), "resources": [GAIN], "reason": "x", "lease_ms": 1000,
            "not_after_monotonic_ns": h.now + 5000 * MS}
    raises("invalid_arguments", h.broker.claim, h.session_id,
           {**good, "basis": {"kind": "listen_evidence", "evidence": {"master": "playing"}}})
    raises("invalid_arguments", h.broker.authorize, h.session_id,
           {"request_id": "a-1", **h.triple(), "permission": {**default_draft(), "evidence": {"listen": 1}}})


def test_forged_trusted_channel_object_is_refused(tmp_path):
    h = make(tmp_path)
    proposal = h.propose()["proposal"]
    for forged in (TrustedChannel(), object(), None):
        raises("invalid_request", h.broker.trusted_attach, forged)
        raises("invalid_request", h.broker.trusted_invite, forged,
               {"request_id": "page-1", **h.triple(), "resources": [GAIN]})
        raises("invalid_request", h.broker.trusted_preview, forged, {"proposal_id": proposal["proposal_id"]})
        raises("invalid_request", h.broker.trusted_issue, forged, {"request_id": "page-2"})
        raises("invalid_request", h.broker.trusted_ack, forged, {})
    raises("invalid_request", h.broker.trusted_channel)  # handed out once, to the host's page
    assert h.observe()["invitations"] == [] and h.observe()["permissions"] == []


def test_trusted_issue_requires_the_preview_sha_and_permission_cas(tmp_path):
    h = make(tmp_path)
    proposal = h.propose()["proposal"]
    preview = h.broker.trusted_preview(h.channel, {"proposal_id": proposal["proposal_id"]})
    assert any("idle for 4.0 s" in line for line in preview["lines"])
    base = {"proposal_id": proposal["proposal_id"], "proposal_sha256": preview["proposal_sha256"],
            "preview_sha256": preview["preview_sha256"], "attribution_name": "Test person",
            "attribution_statement": ""}

    def issue(**change):
        return h.broker.trusted_issue(h.channel, {"request_id": h.request_id("page"), **base, **change})

    refused(issue(preview_sha256="0" * 64), "confirmation_mismatch")
    refused(issue(proposal_sha256="0" * 64), "proposal_changed")
    refused(issue(expected_permission_id="perm-0123456789abcdef", expected_permission_version=1,
                  expected_permission_sha256="0" * 64), "stale_page_state")
    assert h.observe()["permissions"] == []
    before = h.observe()["control_revision"]
    receipt = issue()
    assert receipt["status"] == "ok"
    permission = receipt["outcome"]["permission"]
    assert permission["issuer_ref"]["kind"] == "trusted_page"
    assert permission["attribution"] == {"actor": "Test person", "actor_kind": "human", "statement": "",
                                         "verified": False}
    assert permission["preview_sha256"] == preview["preview_sha256"]
    assert h.observe()["control_revision"] == before + 1
    refused(issue(), "proposal_changed")  # the proposal was issued; a second issue needs a new proposal
    head = (permission["permission_id"], permission["version"], permission["state_sha256"])
    second = h.issue(default_draft(max_lease_ms=2000), expected=head)
    refused(second, "permission_active")


def _issue_payload(h: Harness, request_id: str, draft=None) -> dict:
    proposal = h.propose(draft)["proposal"]
    preview = h.broker.trusted_preview(h.channel, {"proposal_id": proposal["proposal_id"]})
    return {"request_id": request_id, "proposal_id": proposal["proposal_id"],
            "proposal_sha256": preview["proposal_sha256"], "preview_sha256": preview["preview_sha256"],
            "attribution_name": "Test person", "attribution_statement": "Issued in a test"}


def test_replayed_issue_receipt_cannot_resurrect_a_revoked_permission(tmp_path):
    h = make(tmp_path)
    payload = _issue_payload(h, "page-issue-1")
    first = h.broker.trusted_issue(h.channel, payload)
    permission = first["outcome"]["permission"]
    revoked = h.broker.reduce_revoke_permission({"request_id": "page-revoke-1",
                                                 "permission_id": permission["permission_id"]})
    assert revoked["outcome"]["kind"] == "revoked"
    replay = h.broker.trusted_issue(h.channel, payload)
    assert replay["replayed"] is True and replay["outcome"] == first["outcome"]
    assert h.observe()["permissions"] == []
    refused(h.claim([GAIN], h.permission_basis(permission)), "permission_not_active")
    raises("idempotency_conflict", h.broker.trusted_issue, h.channel,
           {**payload, "attribution_name": "Other"})
    head = (permission["permission_id"], permission["version"],
            h.page_state()["permission_history"][0]["state_sha256"])
    again = h.broker.trusted_issue(h.channel, {**payload, "request_id": "page-issue-2",
                                               "expected_permission_id": head[0],
                                               "expected_permission_version": head[1],
                                               "expected_permission_sha256": head[2]})
    refused(again, "proposal_changed")
    assert len([e for e in h.entries("permission") if "permission" in e["data"]]) == 1


def _amend(h: Harness, permission: dict, change: dict, request_id: str) -> dict:
    preview = h.broker.trusted_preview(h.channel, {"amend": {"permission_id": permission["permission_id"],
                                                             "change": change}})
    return {"request_id": request_id, "permission_id": permission["permission_id"],
            "expected_permission_version": permission["version"],
            "expected_permission_sha256": permission["state_sha256"], "change": change,
            "preview_sha256": preview["preview_sha256"]}


def test_two_policy_versions_race_and_exactly_one_amendment_wins(tmp_path):
    h = make(tmp_path)
    permission = issued(h)
    off = _amend(h, permission, {"proactive_allowed": False}, "page-amend-a")
    shorter = _amend(h, permission, {"expires_in_ms": 600_000}, "page-amend-b")
    results = _race(h, [lambda: h.broker.trusted_amend(h.channel, off),
                        lambda: h.broker.trusted_amend(h.channel, shorter)])
    kinds = sorted(r["outcome"].get("code", r["outcome"]["kind"]) for r in results)
    assert kinds == ["amended", "stale_page_state"]
    current = h.observe()["permissions"]
    assert len(current) == 1 and current[0]["version"] == 2
    assert current[0]["permission_id"] == permission["permission_id"]
    history = h.page_state()["permission_history"]
    assert [p["version"] for p in history] == [2]


def test_revoked_permission_cannot_be_amended(tmp_path):
    h = make(tmp_path)
    permission = issued(h)
    amend_of = {"permission_id": permission["permission_id"], "change": {"proactive_allowed": False}}
    preview_before = h.broker.trusted_preview(h.channel, {"amend": amend_of})
    h.broker.reduce_revoke_permission({"request_id": "page-r", "permission_id": permission["permission_id"]})
    raises("invalid_request", h.broker.trusted_preview, h.channel,
           {"amend": {"permission_id": permission["permission_id"], "change": {"proactive_allowed": False}}})
    amend = h.broker.trusted_amend(h.channel, {
        "request_id": "page-a", "permission_id": permission["permission_id"],
        "expected_permission_version": permission["version"],
        "expected_permission_sha256": permission["state_sha256"], "change": {"proactive_allowed": False},
        "preview_sha256": preview_before["preview_sha256"]})
    refused(amend, "permission_not_active")
    assert h.observe()["permissions"] == []
    # Issue again is a new proposal the person must issue; nothing reactivates the revoked version.
    reissue = h.broker.trusted_preview(h.channel, {"reissue_of": permission["permission_id"]})
    original = h.broker.trusted_preview(h.channel, {"proposal_id": h.propose()["proposal"]["proposal_id"]})
    assert reissue["lines"] == original["lines"] and reissue["proposal_id"] != permission["proposal_id"]
    assert h.page_state()["permission_history"][0]["state"] == "revoked"


def test_revocation_invalidates_claims_notice_waits_and_undispatched_steps_in_one_transaction(tmp_path):
    # The undispatched-steps half needs b2b_act; the action tests cover it with a running ramp.
    h = make(tmp_path)
    permission = issued(h)
    claim = h.claim([GAIN], h.permission_basis(permission))
    assert claim["outcome"]["kind"] == "granted"
    notice = claim["notice"]
    revision = h.observe()["control_revision"]
    changes = len(h.entries("authority_changed"))
    h.broker.reduce_revoke_permission({"request_id": "page-r", "permission_id": permission["permission_id"]})
    after = h.observe()
    assert after["control_revision"] == revision + 1 and len(h.entries("authority_changed")) == changes + 1
    assert after["grants"] == [] and after["permissions"] == []
    assert owner(h, GAIN)["kind"] == "unowned"
    ended = events(h, "grant_ended")[-1]
    assert ended["reason"] == "permission_ended" and ended["grant_id"] == claim["grant"]["grant_id"]
    late = h.ack(notice)
    assert late == {"accepted": False, "outcome": "cancelled", "notice_id": notice["notice_id"]}
    refused(h.claim([GAIN], h.permission_basis(permission)), "permission_not_active")
    head = h.page_state()["permission_history"][-1]
    fresh = h.issue(expected=(head["permission_id"], head["version"], head["state_sha256"]))
    assert fresh["outcome"]["permission"]["revocation_generation"] == permission["revocation_generation"] + 1


def test_agent_can_release_narrow_and_shorten_but_never_expand(tmp_path):
    h = make(tmp_path)
    invitation = h.invite([GAIN, FILTER])
    grant = h.claim([GAIN, FILTER], h.invitation_basis(invitation))["grant"]
    gid = grant["grant_id"]

    def revoke(target):
        return h.broker.revoke(h.session_id, {"request_id": h.request_id(), **h.triple(), "target": target})

    def release(resources=None):
        request = {"request_id": h.request_id(), **h.triple(), "grant_id": gid}
        if resources is not None:
            request["resources"] = resources
        return h.broker.release(h.session_id, request)

    refused(revoke({"kind": "grant_lease", "grant_id": gid, "lease_ms": 5000}), "expansion_not_permitted")
    shorter = revoke({"kind": "grant_lease", "grant_id": gid, "lease_ms": 1000})
    assert shorter["outcome"]["kind"] == "revoked"
    assert shorter["grant"]["lease_expires_monotonic_ns"] == h.now + 1000 * MS
    # A shorter lease keeps the grant, and its pinned generation, as they were.
    assert owner(h, GAIN)["generation"] == grant["generations"][GAIN]
    refused(release([FADER]), "resource_not_granted")
    narrowed = release([FILTER])
    assert narrowed["outcome"] == {"kind": "narrowed", "grant_id": gid, "remaining": [GAIN]}
    assert owner(h, FILTER)["kind"] == "unowned"
    assert release()["outcome"] == {"kind": "released", "grant_id": gid, "resources": [GAIN]}
    refused(release(), "grant_not_found")
    refused(h.claim([GAIN], h.invitation_basis(invitation)), "invitation_ended")  # single use


def test_agent_cannot_revoke_policy_or_remove_a_human_or_prepared_hold(tmp_path):
    h = make(tmp_path, human_reserved=[LEFT_GAIN])
    permission = issued(h)
    h.broker.reduce_hold({"request_id": "page-hold", "resources": [LEFT_FILTER]})
    receipt = h.broker.revoke(h.session_id, {"request_id": h.request_id(), **h.triple(),
                                             "target": {"kind": "permission",
                                                        "permission_id": permission["permission_id"]}})
    refused(receipt, "authority_issuer_required")
    assert receipt["status"] == "needs_input" and "{" not in receipt["next_actions"][0]
    assert h.observe()["permissions"][0]["state"] == "active"
    refused(h.claim([LEFT_GAIN], h.permission_basis(permission)), "permission_scope")
    everything = h.broker.revoke(h.session_id, {"request_id": h.request_id(), **h.triple(),
                                                "target": {"kind": "all_own_authority"}})
    assert everything["outcome"]["kind"] == "revoked"
    h.tick(60_000, step_ms=100)
    for rid, gesture in ((LEFT_GAIN, "prepared-hold"), (LEFT_FILTER, "page-hold-")):
        held = owner(h, rid)
        assert (held["kind"], held["hold"]) == ("human", "explicit")
        assert held["gesture_id"].startswith(gesture)
    assert h.observe()["authority_state"]["holds"] == [LEFT_FILTER, LEFT_GAIN]
    assert not any(hasattr(h.broker, name) for name in ("release_hold", "accept_pickup", "issue", "ack"))


def test_long_press_release_creates_one_invitation_from_duplicate_messages(tmp_path):
    h = make(tmp_path)
    h.press()
    h.tick(40)
    h.press()
    h.tick(40)
    h.press()
    h.tick(520)
    h.release()
    h.tick(40)
    h.release()
    outstanding = h.observe()["invitations"]
    assert len(outstanding) == 1 and outstanding[0]["source"] == "gesture"
    assert outstanding[0]["resources"] == sorted([GAIN, FILTER, TRANSPORT, POSITION])
    assert len(events(h, "invitation_created")) == 1
    h.tick(100)
    h.long_press()  # a second long press while it is outstanding returns the same invitation
    again = h.observe()["invitations"]
    assert [i["invitation_id"] for i in again] == [outstanding[0]["invitation_id"]]
    assert again[0]["coalesced_presses"] == 1
    assert events(h, "gesture_released")[-1]["outcome"] == "coalesced"


def test_short_press_bounce_and_lost_release_create_no_invitation(tmp_path):
    h = make(tmp_path)
    h.long_press(held_ms=200)
    assert events(h, "gesture_released")[-1]["outcome"] == "short_press_ignored"
    h.tick(100)
    h.press()
    h.tick(10)
    h.release()  # a contact bounce inside the 30 ms debounce is ignored
    assert h.observe()["gestures"]["handback-right"] == "pressed"
    h.tick(5100)  # held past max_hold_ms: the release was lost
    assert h.observe()["gestures"]["handback-right"] == "idle"
    h.press()
    h.tick(5100)
    h.release()  # arrives after the reset, so it is ignored
    assert len(events(h, "release_lost")) == 2
    assert h.observe()["invitations"] == [] and events(h, "invitation_created") == []


def test_generic_note_without_binding_is_unknown_input_not_an_invitation(tmp_path):
    h = make(tmp_path, {"activity_release_ms": 1000, "uncertainty_ms": 5000})
    h.adapter.inject_message([0x97, 65, 127])
    h.tick(600)
    h.adapter.inject_message([0x97, 65, 0])
    assert h.observe()["invitations"] == [] and events(h, "invitation_created") == []
    assert events(h, "unknown_input")[0]["classification"] == "unknown"
    h.tick(1100)
    receipt = refused(h.claim([GAIN], h.invitation_basis(h.invite([GAIN]))), "classification_uncertain")
    assert receipt["retry_not_before_monotonic_ns"] > h.now


def test_invitation_is_bound_to_adapter_epoch_and_expires(tmp_path):
    h = make(tmp_path)
    h.long_press()
    first = h.observe()["invitations"][0]
    h.adapter.restart_writer()
    assert h.observe()["invitations"] == []
    basis = {"kind": "invitation", "invitation_id": first["invitation_id"]}
    refused(h.claim([GAIN], basis), "invitation_ended")
    h.tick(100)
    h.long_press()
    second = h.observe()["invitations"][0]
    h.tick(30_000, step_ms=100)
    assert h.observe()["invitations"] == []
    refused(h.claim([GAIN], {"kind": "invitation", "invitation_id": second["invitation_id"]}),
            "invitation_ended")
    h.press()
    h.tick(300)
    h.adapter.restart_writer()  # a press before an epoch change and a release after it never fire
    h.tick(300)
    h.release()
    assert h.observe()["invitations"] == []


def test_invitation_makes_a_settled_resource_claimable_at_settle_ms(tmp_path):
    # Ordering only: claimable exactly settle_ms after the last touch. Not a latency measurement.
    h = make(tmp_path)
    h.move(TRIM_2, 90)
    touched = h.now
    basis = h.invitation_basis(h.invite([GAIN]))
    assert owner(h, GAIN)["kind"] == "unowned"  # the invite lets go of the person's control
    first = refused(h.claim([GAIN], basis), "human_active")
    assert first["retry_not_before_monotonic_ns"] == touched + 100 * MS
    h.tick_ns(100 * MS - 1)
    refused(h.claim([GAIN], basis), "human_active")
    h.tick_ns(1)
    assert h.claim([GAIN], basis)["outcome"]["kind"] == "granted"


def test_proactive_claim_requires_active_permission_and_idle_interval_to_the_nanosecond(tmp_path):
    h = make(tmp_path, {"activity_release_ms": 1000})
    fake = {"kind": "standing_permission", "permission_id": "perm-0123456789abcdef", "permission_version": 1,
            "revocation_generation": 0}
    refused(h.claim([GAIN], fake), "permission_not_active")
    invited_only = issued(h, default_draft(proactive_claims={"allowed": False, "idle_interval_ms": 4000,
                                                             "cooldown_ms": 0}))
    refused(h.claim([GAIN], h.permission_basis(invited_only)), "proactive_not_permitted")
    h.broker.reduce_revoke_permission({"request_id": "page-r",
                                       "permission_id": invited_only["permission_id"]})
    head = h.page_state()["permission_history"][-1]
    permission = h.issue(default_draft(proactive_claims={"allowed": True, "idle_interval_ms": 4000,
                                                         "cooldown_ms": 0}),
                         expected=(head["permission_id"], head["version"], head["state_sha256"]))
    permission = permission["outcome"]["permission"]
    basis = h.permission_basis(permission)
    h.move(TRIM_2, 90)
    touched = h.now
    h.tick(3990)
    h.tick_ns(10 * MS - 1)
    early = refused(h.claim([GAIN], basis), "human_active")
    assert early["retry_not_before_monotonic_ns"] == touched + 4000 * MS
    h.tick_ns(1)
    assert h.claim([GAIN], basis)["outcome"]["kind"] == "granted"


def test_proactive_claim_refused_during_gesture_unsettled_pickup_or_uncertain_classification(tmp_path):
    h = make(tmp_path, {"activity_release_ms": 1000, "uncertainty_ms": 5000})
    permission = issued(h, default_draft(proactive_claims={"allowed": True, "idle_interval_ms": 500,
                                                           "cooldown_ms": 0}))
    basis = h.permission_basis(permission)
    h.press()
    refused(h.claim([GAIN], basis), "gesture_in_progress")
    h.tick(100)
    h.release()
    h.adapter.inject_message([0xB3, 99, 1])
    unknown_at = h.now
    h.tick(1100)
    uncertain = refused(h.claim([GAIN], basis), "classification_uncertain")
    assert uncertain["retry_not_before_monotonic_ns"] == unknown_at + 5000 * MS
    h.tick(4000)
    h.move(FILTER_2, 64)
    h.tick(1100)
    grant = invited_grant(h, [FILTER])["grant"]
    h.broker._record_commanded(FILTER, grant["grant_id"], 40)  # what admission records after a submitted set
    release(h, grant["grant_id"])
    assert owner(h, FILTER)["kind"] == "pickup_pending"
    pickup = refused(h.claim([FILTER], basis), "pickup_unsettled")
    assert pickup["status"] == "needs_input"
    assert h.claim([GAIN], basis)["outcome"]["kind"] == "granted"


def test_refused_or_denied_proactive_claim_starts_a_cooldown_and_refusals_coalesce(tmp_path):
    h = make(tmp_path, {"activity_release_ms": 1000})
    permission = issued(h)  # idle 4000 ms, cooldown 5000 ms
    basis = h.permission_basis(permission)
    h.move(TRIM_2, 90)
    refused_at = h.now
    refused(h.claim([GAIN], basis), "human_active")
    h.tick(100)
    refused(h.claim([GAIN], basis), "human_active")
    coalesced = events(h, "claim_refused")
    assert len(coalesced) == 1 and coalesced[0]["count"] == 2 and coalesced[0]["code"] == "human_active"
    h.tick(3900)
    cooling = refused(h.claim([GAIN], basis), "claim_cooldown")
    assert cooling["retry_not_before_monotonic_ns"] == refused_at + 5000 * MS  # refusals never extend it
    h.tick(1000)
    claim = h.claim([GAIN], basis)
    assert claim["outcome"]["kind"] == "granted"
    denied = h.broker.reduce_deny({"request_id": "page-deny", "notice_id": claim["notice"]["notice_id"],
                                   "grant_id": claim["grant"]["grant_id"]})
    assert denied["outcome"]["kind"] == "denied"
    after_deny = refused(h.claim([GAIN], basis), "claim_cooldown")
    assert after_deny["retry_not_before_monotonic_ns"] == h.now + 5000 * MS
    # Narrowing the permission keeps its cooldowns: an amendment never lets the agent retry sooner.
    current = h.observe()["permissions"][0]
    amended = h.broker.trusted_amend(h.channel, _amend(h, current, {"expires_in_ms": 600_000}, "page-amend"))
    assert amended["outcome"]["kind"] == "amended"
    refused(h.claim([GAIN], h.permission_basis(amended["outcome"]["permission"])), "claim_cooldown")


def test_one_notice_per_claim_and_a_noisy_encoder_creates_no_notices_or_revisions(tmp_path):
    h = make(tmp_path)
    invited_grant(h, [GAIN], ack=True, lease_ms=10_000)
    assert len(events(h, "notice_created")) == 1
    revision = h.observe()["control_revision"]
    for _ in range(200):
        h.jog(1, 1)
        h.tick(10)
    for _ in range(5):
        refused(h.claim([FILTER], {"kind": "invitation", "invitation_id": "i-0000000000000000"}),
                "invitation_not_found")
    assert len(events(h, "notice_created")) == 1
    assert h.observe()["control_revision"] == revision
    noise = [e for e in events(h, "human_input") if e["resource"] == "synthetic/left/position"]
    assert sum(e["count"] for e in noise) == 200 and len(noise) <= 9
    assert len(h.entries("human_input")) <= 10


def test_physical_input_revokes_only_matching_resources_and_an_unrelated_grant_continues(tmp_path):
    h = make(tmp_path)
    right = invited_grant(h, [GAIN, FILTER], ack=True)["grant"]
    fader = invited_grant(h, [FADER], ack=True)["grant"]
    revision = h.observe()["control_revision"]
    h.move(TRIM_2, 70)
    live = {g["grant_id"]: g for g in grants(h)}
    assert live[right["grant_id"]]["resources"] == [FILTER]
    assert live[fader["grant_id"]]["resources"] == [FADER]
    gain = owner(h, GAIN)
    assert gain["kind"] == "human" and gain["hold"] == "activity" and gain["cause"] == "human_input"
    assert gain["generation"] > right["generations"][GAIN]
    assert h.observe()["control_revision"] == revision + 1
    ended = events(h, "grant_ended")[-1]
    assert (ended["resources"], ended["remaining"], ended["reason"]) == ([GAIN], [FILTER], "human_input")


def test_jog_yields_the_coupled_deck_motion_group(tmp_path):
    h = make(tmp_path)
    grant = invited_grant(h, [TRANSPORT, POSITION], ack=True)["grant"]
    h.jog(2, -3)
    assert grants(h) == []
    for rid in (TRANSPORT, POSITION):
        assert owner(h, rid)["kind"] == "human"
    ended = events(h, "grant_ended")[-1]
    assert ended["grant_id"] == grant["grant_id"] and sorted(ended["resources"]) == [POSITION, TRANSPORT]


def test_unknown_midi_yields_the_whole_adapter_scope_and_opens_an_uncertainty_window(tmp_path):
    h = make(tmp_path, {"activity_release_ms": 1000, "uncertainty_ms": 5000})
    invited_grant(h, [GAIN], ack=True)
    invited_grant(h, [FADER], ack=True)
    h.adapter.inject_message([0xB3, 99, 1])
    assert grants(h) == []
    assert {e["reason"] for e in events(h, "grant_ended")} == {"unknown_input"}
    for rid in (GAIN, FADER, LEFT_GAIN, FX, ROUTE, "synthetic/left/transport"):
        assert owner(h, rid)["kind"] == "human", rid
    h.tick(1100)
    refused(h.claim([GAIN], h.invitation_basis(h.invite([GAIN]))), "classification_uncertain")
    assert h.observe()["evidence"]["uncertainty_until_monotonic_ns"] > h.now


def test_unqualified_headphone_control_yields_the_whole_scope(tmp_path):
    h = make(tmp_path)
    invited_grant(h, [GAIN], ack=True)
    h.move("controller/synthetic/headphone-level", 64)
    assert grants(h) == []
    assert events(h, "unknown_input")[-1]["classification"] == "unqualified"
    assert all(r["owner"]["kind"] == "human" for r in h.observe()["resources"])


def test_relative_encoder_without_touch_sensing_yields_on_its_first_delta(tmp_path):
    h = make(tmp_path)
    invited_grant(h, [TRANSPORT, POSITION], ack=True)
    h.jog(2, 1)  # one tick of the encoder, with no touch sensor in front of it
    assert grants(h) == []
    assert len(events(h, "grant_ended")) == 1


def test_feedback_echo_and_heartbeat_are_not_human_movement(tmp_path):
    h = make(tmp_path)
    grant = invited_grant(h, [GAIN], ack=True)["grant"]
    revision = h.observe()["control_revision"]
    h.adapter.inject_message(h.raw(TRIM_2, 99), origin="feedback")
    h.tick(500)  # heartbeats arrive while the adapter is pumped
    assert owner(h, GAIN)["grant_id"] == grant["grant_id"]
    observation = h.observe()
    assert observation["control_revision"] == revision
    assert observation["evidence"]["counters"]["feedback"] == 1
    assert observation["evidence"]["resources"][GAIN]["last_human_input_monotonic_ns"] is None
    assert events(h, "human_input") == []


def test_explicit_hold_survives_inactivity_and_activity_ownership_becomes_unowned(tmp_path):
    h = make(tmp_path, human_reserved=["synthetic/left/transport"])
    h.broker.reduce_hold({"request_id": "page-hold", "resources": [LEFT_GAIN]})
    h.move("controller/synthetic/channel-1/filter", 30)
    assert owner(h, LEFT_FILTER)["hold"] == "activity"
    h.tick(2000)
    assert owner(h, LEFT_FILTER)["kind"] == "unowned"
    h.tick(120_000, step_ms=1000)
    assert owner(h, LEFT_GAIN)["hold"] == "explicit"
    assert owner(h, "synthetic/left/transport")["hold"] == "explicit"
    released = h.broker.trusted_release_hold(h.channel, {"request_id": "page-rel", **h.triple(),
                                                         "resources": [LEFT_GAIN]})
    assert released["outcome"]["resources"] == [LEFT_GAIN]
    assert owner(h, LEFT_GAIN)["kind"] == "unowned"
    refused = h.invite(["synthetic/left/transport"])  # an invitation never drops a hold; Release does
    assert (refused["outcome"]["kind"], refused["outcome"]["code"]) == ("refused", "resource_held_by_human")
    assert owner(h, "synthetic/left/transport")["hold"] == "explicit"


def test_takeover_restores_nothing_and_never_resumes(tmp_path):
    h = make(tmp_path)
    grant = invited_grant(h, [GAIN], ack=True, lease_ms=10_000)["grant"]
    ramp = {"kind": "ramp", "resource": GAIN, "target": 40, "duration_ms": 1000}
    run = h.act_begin(grant["grant_id"], ramp)
    h.tick(200)
    sent = len(h.adapter.sent())
    assert 0 < sent < 20  # the ramp is part way through
    h.move(TRIM_2, 90)
    result = h.broker.act_result(run)
    assert result["outcome"]["kind"] == "partially_dispatched" and result["outcome"]["last_step"] == sent
    h.tick(12_000)  # past the activity release and past the lease the grant had
    assert owner(h, GAIN)["kind"] == "unowned" and grants(h) == []
    assert len(h.adapter.sent()) == sent and h.adapter.value(GAIN) == 90  # nothing restored, nothing resumed
    assert h.broker.act_result(run) == result
    assert len(events(h, "grant_created")) == 1


def _pickup(h: Harness, rid: str, address: str, commanded: int, *, fader=None) -> dict:
    if fader is not None:
        h.move(address, fader)
        h.tick(2100)
    grant = invited_grant(h, [rid])["grant"]
    h.broker._record_commanded(rid, grant["grant_id"], commanded)  # what admission records after a submit
    release(h, grant["grant_id"])
    return owner(h, rid)


def test_released_absolute_control_with_a_mismatch_enters_pickup_pending(tmp_path):
    h = make(tmp_path)
    revision = h.observe()["control_revision"]
    pending = _pickup(h, FILTER, FILTER_2, 40, fader=64)
    assert (pending["kind"], pending["target_value"], pending["physical_value"]) == ("pickup_pending", 40, 64)
    state = h.observe()["authority_state"]
    assert state["pickup"] == [{"resource": FILTER, "target_value": 40}]
    assert h.observe()["control_revision"] > revision
    assert events(h, "pickup_pending")[-1]["resource"] == FILTER
    unknown = _pickup(h, LEFT_GAIN, "controller/synthetic/channel-1/trim", 50)  # fader never seen this epoch
    assert unknown["kind"] == "pickup_pending" and unknown["physical_value"] is None
    matching = _pickup(h, GAIN, TRIM_2, 101, fader=100)  # numeric readback 100, fader 100
    assert matching["kind"] == "unowned"
    untouched = invited_grant(h, [FADER])["grant"]  # a control the grant never moved needs no pickup
    h.broker.release(h.session_id, {"request_id": h.request_id(), **h.triple(),
                                    "grant_id": untouched["grant_id"]})
    assert owner(h, FADER)["kind"] == "unowned"


def test_pickup_settles_by_touch_with_a_recorded_jump_or_by_page_acceptance(tmp_path):
    h = make(tmp_path)
    _pickup(h, FILTER, FILTER_2, 40, fader=64)
    h.move(FILTER_2, 50)
    assert owner(h, FILTER)["kind"] == "human"
    settled = events(h, "pickup_settled")[-1]
    assert (settled["how"], settled["software_before"], settled["software_after"]) == ("touched", 64, 50)
    _pickup(h, LEFT_FILTER, "controller/synthetic/channel-1/filter", 20, fader=90)
    accepted = h.broker.trusted_accept_pickup(h.channel, {"request_id": "page-pick", **h.triple(),
                                                          "resource": LEFT_FILTER})
    assert accepted["outcome"]["kind"] == "pickup_accepted"
    assert owner(h, LEFT_FILTER)["kind"] == "unowned"
    assert events(h, "pickup_settled")[-1]["how"] == "accepted_on_page"
    again = h.broker.trusted_accept_pickup(h.channel, {"request_id": "page-pick-2", **h.triple(),
                                                       "resource": LEFT_FILTER})
    refused(again, "stale_page_state")
    assert h.observe()["authority_state"]["pickup"] == []


def test_mutations_use_exact_cas_over_the_authority_state_sha(tmp_path):
    h = make(tmp_path)
    basis = h.invitation_basis(h.invite([GAIN]))
    observation = h.observe()
    assert digest(observation["authority_state"]) == observation["state_sha256"]
    stale = h.triple(observation)
    h.invite([FILTER])  # the revision moves on
    error = raises("stale_revision", h.claim, [GAIN], basis, triple=stale)
    assert "b2b_observe" in error.hint
    current = h.triple()
    raises("stale_revision", h.claim, [GAIN], basis, triple={**current, "expected_state_sha256": "0" * 64})
    raises("stale_revision", h.claim, [GAIN], basis, triple={**current, "expected_session_epoch": "h0.9"})
    receipt = h.claim([GAIN], basis, triple=current)
    assert receipt["outcome"]["kind"] == "granted" and receipt["revision_check"] == "exact"
    after = h.observe()
    assert digest(after["authority_state"]) == after["state_sha256"] == receipt["state_sha256"]


def test_revision_advances_iff_the_authority_sha_changes(tmp_path):
    for seed in range(25):  # 25 seeded sequences of 40 random events
        (tmp_path / f"seed-{seed}").mkdir()
        h = make(tmp_path / f"seed-{seed}", {"activity_release_ms": 300, "notice_ack_timeout_ms": 400})
        rng = random.Random(seed)
        previous = h.observe()
        for _ in range(40):
            step = rng.randrange(12)
            try:
                if step == 0:
                    address = rng.choice([TRIM_2, FILTER_2, "controller/synthetic/crossfader"])
                    h.move(address, rng.randrange(128))
                elif step == 1:
                    h.jog(rng.choice([1, 2]), rng.choice([-2, 1]))
                elif step == 2:
                    h.invite(rng.choice([[GAIN], [FILTER], [GAIN, FILTER], [FADER]]))
                elif step == 3 and h.observe()["invitations"]:
                    invitation = rng.choice(h.observe()["invitations"])
                    h.claim(invitation["resources"][:1] if FX not in invitation["resources"]
                            else invitation["resources"], {"kind": "invitation",
                                                           "invitation_id": invitation["invitation_id"]})
                elif step == 4 and h.observe()["notices"]:
                    h.ack(rng.choice(h.observe()["notices"]))
                elif step == 5 and grants(h):
                    grant = rng.choice(grants(h))
                    h.broker.release(h.session_id, {"request_id": h.request_id(), **h.triple(),
                                                    "grant_id": grant["grant_id"]})
                elif step == 6:
                    h.broker.reduce_hold({"request_id": h.request_id("page"),
                                          "resources": [rng.choice([GAIN, LEFT_GAIN, FADER])]})
                elif step == 7 and h.observe()["authority_state"]["holds"]:
                    holds = h.observe()["authority_state"]["holds"]
                    h.broker.trusted_release_hold(h.channel, {"request_id": h.request_id("page"),
                                                              **h.triple(), "resources": holds})
                elif step == 8:
                    h.long_press(held_ms=rng.choice([200, 600]))
                elif step == 9 and rng.random() < 0.2:
                    h.broker.panic({"request_id": h.request_id("page")})
                elif step == 10 and rng.random() < 0.1:
                    h.adapter.restart_writer()
                else:
                    h.tick(rng.randrange(10, 500, 10))
            except PocketError as error:
                assert error.code in ("stale_revision", "unsupported_profile")
            current = h.observe()
            assert digest(current["authority_state"]) == current["state_sha256"]
            if current["state_sha256"] != previous["state_sha256"]:
                assert current["control_revision"] > previous["control_revision"]
            else:  # a state can change and change back between two observations; the chain below checks it
                assert current["control_revision"] >= previous["control_revision"]
            previous = current
        chain = [e["data"] for e in h.entries("authority_changed")]
        assert [c["revision"] for c in chain] == list(range(1, len(chain) + 1))
        assert all(a["state_sha256"] != b["state_sha256"] for a, b in itertools.pairwise(chain))
        assert chain[-1]["state_sha256"] == previous["state_sha256"]
        assert chain[-1]["revision"] == previous["control_revision"]


def test_human_mixing_on_free_resources_does_not_stale_the_agent(tmp_path):
    h = make(tmp_path)
    basis = h.invitation_basis(h.invite([GAIN]))
    triple = h.triple()
    for value in (10, 40, 90):
        h.move("controller/synthetic/channel-1/trim", value)
        h.move("controller/synthetic/crossfader", value)
        h.jog(1, 2)
        h.tick(20)
    h.tick(200)
    receipt = h.claim([GAIN], basis, triple=triple)
    assert receipt["outcome"]["kind"] == "granted"
    revision = h.observe()["control_revision"]
    h.ack(receipt["notice"])  # a page acknowledgement does not stale the agent either
    assert h.observe()["control_revision"] == revision


def test_reductions_are_admitted_at_a_stale_revision_within_the_epoch(tmp_path):
    h = make(tmp_path)
    first = invited_grant(h, [GAIN])
    second = invited_grant(h, [FILTER])
    third = invited_grant(h, [FADER])
    fourth = invited_grant(h, [TRANSPORT, POSITION])
    fifth = invited_grant(h, [LEFT_FILTER])
    stale = h.triple()
    h.invite([LEFT_GAIN])  # the revision moves on
    released = h.broker.release(h.session_id, {"request_id": h.request_id(), **stale,
                                               "grant_id": first["grant"]["grant_id"]})
    assert released["outcome"]["kind"] == "released"
    assert (released["revision_check"], released["expected_revision_current"]) == ("not_required", False)
    denied = h.broker.reduce_deny({"request_id": "page-deny", **stale,
                                   "notice_id": second["notice"]["notice_id"],
                                   "grant_id": second["grant"]["grant_id"]})
    assert denied["outcome"]["kind"] == "denied" and denied["expected_revision_current"] is False
    held = h.broker.reduce_hold({"request_id": "page-hold", **stale, "resources": [FADER]})
    assert held["outcome"]["kind"] == "held"
    assert (owner(h, FADER)["kind"], owner(h, FADER)["hold"]) == ("human", "explicit")
    ends = {e["grant_id"]: e["reason"] for e in events(h, "grant_ended")}
    assert ends[third["grant"]["grant_id"]] == "held_by_human"  # a page hold ends the agent's grant on it
    taken = h.broker.reduce_revoke_grant({"request_id": "page-take", **stale,
                                          "grant_id": fourth["grant"]["grant_id"]})
    assert taken["outcome"]["kind"] == "revoked"
    assert {e["grant_id"]: e["reason"] for e in events(h, "grant_ended")}[fourth["grant"]["grant_id"]] == (
        "revoked_by_human")
    assert owner(h, TRANSPORT)["kind"] == owner(h, POSITION)["kind"] == "unowned"
    everything = h.broker.revoke(h.session_id, {"request_id": h.request_id(), **stale,
                                                "target": {"kind": "all_own_authority"}})
    assert everything["outcome"]["grants"] == [fifth["grant"]["grant_id"]]
    assert h.broker.panic({"request_id": "page-panic", **stale})["outcome"]["kind"] == "panic"
    old_epoch = {**stale, "expected_session_epoch": f"{h.broker.host_epoch}.0"}
    raises("stale_revision", h.broker.release, h.session_id, {"request_id": h.request_id(), **old_epoch,
                                                             "grant_id": first["grant"]["grant_id"]})


def test_epoch_change_invalidates_grants_invitations_permissions_and_observation_triples(tmp_path):
    h = make(tmp_path)
    permission = issued(h)
    grant = h.claim([GAIN], h.permission_basis(permission))["grant"]
    h.invite([FADER])
    before = h.observe()
    h.adapter.restart_writer()
    after = h.observe()
    assert after["session_epoch"] != before["session_epoch"]
    assert after["grants"] == [] and after["invitations"] == [] and after["permissions"] == []
    assert events(h, "grant_ended")[-1]["reason"] == "epoch_changed"
    history = h.page_state()["permission_history"][0]
    assert history["state"] == "invalidated"
    raises("stale_revision", h.claim, [FILTER], {"kind": "invitation", "invitation_id": "i-0000000000000000"},
           triple=h.triple(before))
    assert grant["grant_id"] not in {g["grant_id"] for g in after["grants"]}
    reissue = h.broker.trusted_preview(h.channel, {"reissue_of": permission["permission_id"]})
    original = h.broker.trusted_preview(h.channel, {"proposal_id": h.propose()["proposal"]["proposal_id"]})
    assert reissue["lines"] == original["lines"]  # identical lines; a new proposal id, so a new preview SHA


def test_controller_disconnect_marks_unavailable_and_reconnect_is_unowned_in_a_new_epoch(tmp_path):
    h = make(tmp_path, human_reserved=[LEFT_GAIN])
    permission = issued(h)
    invited_grant(h, [FADER], ack=True)
    h.adapter.disconnect_controller()
    observation = h.observe()
    assert observation["grants"] == [] and observation["permissions"] == []
    assert {u["reason"] for u in observation["authority_state"]["unavailable"]} == {"controller_disconnected"}
    assert len(observation["authority_state"]["unavailable"]) == len(observation["resources"])
    refused(h.claim([GAIN], h.permission_basis(permission)), "permission_not_active")
    epoch = observation["session_epoch"]
    h.adapter.reconnect_controller()
    restored = h.observe()
    assert restored["session_epoch"] != epoch and restored["authority_state"]["unavailable"] == []
    assert owner(h, LEFT_GAIN)["hold"] == "explicit"  # the person's hold survives; nothing else resumes
    assert owner(h, FADER)["kind"] == "unowned" and restored["grants"] == []
    h.adapter.disconnect_controller()
    h.adapter.reconnect_controller(mapping_sha256="0" * 64)
    changed = h.observe()["authority_state"]["unavailable"]
    assert {u["reason"] for u in changed} == {"mapping_changed"}
    h.adapter.restart_writer()
    assert {u["reason"] for u in h.observe()["authority_state"]["unavailable"]} == {"mapping_changed"}


def test_master_route_loss_marks_dependent_resources_unavailable(tmp_path):
    h = make(tmp_path)
    invited_grant(h, [GAIN], ack=True)
    h.adapter.lose_route("master")
    observation = h.observe()
    assert observation["grants"] == []
    assert {u["reason"] for u in observation["authority_state"]["unavailable"]} == {"route_lost:master"}
    refused(h.claim([GAIN], {"kind": "invitation", "invitation_id": "i-0000000000000000"}),
            "invitation_not_found")
    h.move(TRIM_2, 30)  # the controller still reports touches; they never grant
    assert owner(h, GAIN)["kind"] == "unavailable"
    h.adapter.restore_route("master")
    assert owner(h, GAIN)["kind"] == "unowned" and h.observe()["authority_state"]["unavailable"] == []
    assert events(h, "resource_available")


def test_suspend_is_detected_on_the_next_broker_entry_and_starts_a_new_epoch(tmp_path):
    h = make(tmp_path)
    grant = invited_grant(h, [GAIN], ack=True, lease_ms=10_000)["grant"]
    basis = h.invitation_basis(h.invite([FILTER]))
    triple = h.triple()
    h.clock.suspend(60_000 * MS)  # the machine sleeps; no tick runs
    raises("stale_revision", h.claim, [FILTER], basis, triple=triple)
    observation = h.observe()
    assert observation["session_epoch"] != triple["expected_session_epoch"]
    assert observation["grants"] == [] and observation["invitations"] == []
    ended = [e for e in events(h, "grant_ended") if e["grant_id"] == grant["grant_id"]]
    assert ended[-1]["reason"] == "clock_discontinuity"
    assert events(h, "epoch_changed")[-1]["reason"] == "clock_discontinuity"
    assert h.adapter.epoch() == observation["authority_state"]["adapter_epoch"]


def test_writer_restart_starts_a_new_epoch_without_resuming(tmp_path):
    h = make(tmp_path)
    invited_grant(h, [GAIN], ack=True, lease_ms=10_000)
    sent = h.adapter.sent_count
    epoch = h.observe()["session_epoch"]
    h.adapter.restart_writer()
    h.tick(500)
    observation = h.observe()
    assert observation["session_epoch"] != epoch and observation["grants"] == []
    assert owner(h, GAIN)["kind"] == "unowned"
    assert h.adapter.sent_count == sent
    assert events(h, "epoch_changed")[-1]["reason"] == "writer_restarted"


def test_panic_revokes_all_agent_authority_and_sends_nothing_to_the_adapter(tmp_path):
    h = make(tmp_path, human_reserved=[LEFT_GAIN])
    permission = issued(h)
    invited_grant(h, [FADER], ack=True)
    h.claim([GAIN], h.permission_basis(permission))
    h.invite([TRANSPORT, POSITION])
    sent = h.adapter.sent_count
    receipt = h.broker.panic({"request_id": "page-stop"})
    assert receipt["status"] == "ok" and len(receipt["outcome"]["grants"]) == 2
    observation = h.observe()
    assert observation["grants"] == observation["invitations"] == observation["permissions"] == []
    assert h.page_state()["permission_history"][0]["state"] == "revoked"
    assert {e["reason"] for e in events(h, "grant_ended")} == {"panic"}
    assert h.adapter.sent_count == sent
    assert h.adapter.value("synthetic/left/transport") == "playing" == h.adapter.value(TRANSPORT)
    assert owner(h, LEFT_GAIN)["hold"] == "explicit"
    refused(h.claim([GAIN], h.permission_basis(permission)), "permission_not_active")


class _ReentrantAdapter(SyntheticAdapter):
    """Calls the broker from inside a broker section, which the adapter contract forbids."""

    misbehave = False

    def readback(self, resource):
        if self.misbehave:
            self._sink.adapter_event({"type": "heartbeat", "at_ns": 0})
        return super().readback(resource)


def test_sink_called_from_inside_a_readback_is_a_contract_violation_not_a_deadlock(tmp_path):
    # The guard is the same at every broker entry; the action tests cover a re-entry from submit.
    h = Harness(tmp_path)
    adapter = _ReentrantAdapter()
    from pocket_music.b2b_broker import Broker

    broker = Broker(adapter, h.clock, h.journal, host_epoch="h00000000000000c3")
    h.broker, h.adapter = broker, adapter
    h.register()
    adapter.misbehave = True
    outcome = []

    thread = threading.Thread(target=lambda: outcome.append(broker.observe(h.session_id)), daemon=True)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive() and len(outcome) == 1  # refused inside, never a deadlock
    observed = outcome[0]  # the readback that re-entered is dropped, and the observation says so
    assert broker._sink_violations >= 1 and observed["evidence"]["counters"]["sink_violations"] >= 1
    assert any("from inside a broker section" in w for w in observed["warnings"])
    adapter.misbehave = False
    assert broker.observe(h.session_id)["session_id"] == h.session_id  # the lock was released


def test_a_full_request_table_never_disables_stop_hold_or_deny(tmp_path, monkeypatch):
    import pocket_music.b2b_broker as broker_module

    monkeypatch.setattr(broker_module, "MAX_REQUESTS", 5)
    h = make(tmp_path)
    grant = invited_grant(h, [GAIN], ack=True)["grant"]
    for n in range(8):  # any local process can send refused reductions with the served CSRF token
        refused(h.broker.reduce_deny({"request_id": f"page-junk-{n}", "notice_id": "n-0000000000000000",
                                      "grant_id": "g-" + "0" * 32}), "grant_not_found")
    assert len(h.broker._requests["public"]) == 5  # bounded by forgetting the oldest finished ones
    invited = h.invite([FILTER])  # the issuer page keeps its own table, so it isn't crowded out
    assert invited["outcome"]["kind"] == "invited"
    held = h.broker.reduce_hold({"request_id": "page-hold", "resources": [LEFT_GAIN]})
    assert held["outcome"]["kind"] == "held"
    stopped = h.broker.panic({"request_id": "page-stop"})
    assert stopped["outcome"]["kind"] == "panic" and stopped["outcome"]["grants"] == [grant["grant_id"]]
    assert grants(h) == []


def test_an_invalid_amendment_change_records_nothing(tmp_path):
    h = make(tmp_path)
    permission = issued(h)
    request = {"request_id": "page-amend", "permission_id": permission["permission_id"],
               "expected_permission_version": permission["version"],
               "expected_permission_sha256": permission["state_sha256"], "preview_sha256": "0" * 64}
    for change in ({"proactive_allowed": True}, {}, {"proactive_allowed": False, "expires_in_ms": 2000},
                   {"expires_in_ms": 10}):
        raises("invalid_arguments", h.broker.trusted_amend, h.channel, {**request, "change": change})
    assert "page-amend" not in h.broker._requests["page"]  # the request_id is still unused
    preview = h.broker.trusted_preview(h.channel, {"amend": {"permission_id": permission["permission_id"],
                                                             "change": {"proactive_allowed": False}}})
    amended = h.broker.trusted_amend(h.channel, {**request, "change": {"proactive_allowed": False},
                                                 "preview_sha256": preview["preview_sha256"]})
    assert amended["outcome"]["kind"] == "amended"


def test_an_agent_proposed_amendment_previews_as_a_whole_replacement(tmp_path):
    h = make(tmp_path)
    permission = issued(h, default_draft(resources=[GAIN], limits=[]))
    wide = default_draft(resources=[GAIN, FILTER, TRANSPORT], action_kinds=["set", "ramp", "set_state"],
                         limits=[])
    proposal = h.broker.authorize(h.session_id, {
        "request_id": h.request_id(), **h.triple(), "permission": wide,
        "expected_permission_id": permission["permission_id"],
        "expected_permission_version": permission["version"],
        "expected_permission_sha256": permission["state_sha256"]})["proposal"]
    lines = h.broker.trusted_preview(h.channel, {"proposal_id": proposal["proposal_id"]})["lines"]
    assert lines[0] == (f"Replaces permission {permission['permission_id']} version 1, which ends when you "
                        "press Issue. These lines are the whole new permission, not a change to the old one.")


def test_an_invite_only_permission_preview_claims_no_limits_it_does_not_enforce(tmp_path):
    h = make(tmp_path)
    draft = default_draft(proactive_claims={"allowed": False, "idle_interval_ms": 4000, "cooldown_ms": 5000})
    proposal = h.propose(draft)["proposal"]
    lines = h.broker.trusted_preview(h.channel, {"proposal_id": proposal["proposal_id"]})["lines"]
    assert lines[0].startswith("Allows no idle claims on Right deck filter, Right deck gain.")
    assert "not this permission" in lines[0]
    assert not any(line.startswith(("Moves allowed", "May claim", "If a claim is refused")) for line in lines)
    proactive = h.broker.trusted_preview(h.channel, {"proposal_id": h.propose()["proposal"]["proposal_id"]})
    assert "Controls you invite aren't bound by this permission" in proactive["lines"][2]


def test_a_hold_on_a_control_waiting_for_pickup_survives_the_touch_that_settles_it(tmp_path):
    h = make(tmp_path)
    permission = issued(h)
    assert _pickup(h, FILTER, FILTER_2, 40, fader=100)["kind"] == "pickup_pending"  # the agent made it so
    held = h.broker.reduce_hold({"request_id": "page-hold", "resources": "all"})
    assert FILTER in held["outcome"]["resources"] and held["outcome"]["waiting_for_pickup"] == [FILTER]
    assert owner(h, FILTER)["kind"] == "pickup_pending" and owner(h, FILTER)["hold"] == "explicit"
    h.move(FILTER_2, 40)  # the person settles the pickup by touch
    h.tick(5000)
    assert owner(h, FILTER)["kind"] == "human" and owner(h, FILTER)["hold"] == "explicit"
    refused(h.claim([FILTER], h.permission_basis(permission)), "resource_held_by_human")
    (tmp_path / "accept").mkdir()
    accepted = make(tmp_path / "accept")
    _pickup(accepted, FILTER, FILTER_2, 40, fader=100)
    accepted.broker.reduce_hold({"request_id": "page-hold", "resources": [FILTER]})
    accepted.broker.trusted_accept_pickup(accepted.channel, {"request_id": "page-accept",
                                                             **accepted.triple(), "resource": FILTER})
    assert owner(accepted, FILTER)["kind"] == "human" and owner(accepted, FILTER)["hold"] == "explicit"


def test_take_it_back_starts_the_cooldown_as_deny_does(tmp_path):
    h = make(tmp_path)
    permission = issued(h)
    h.tick(4000)
    grant = h.claim([GAIN], h.permission_basis(permission))["grant"]
    h.broker.reduce_revoke_grant({"request_id": "page-take", "grant_id": grant["grant_id"]})
    refused(h.claim([GAIN], h.permission_basis(permission)), "claim_cooldown")


def test_an_invitation_can_be_withdrawn_and_a_hold_ends_it(tmp_path):
    h = make(tmp_path)
    first = h.invite([GAIN])["outcome"]["invitation"]
    withdrawn = h.broker.reduce_withdraw_invitation({"request_id": "page-w",
                                                     "invitation_id": first["invitation_id"]})
    assert withdrawn["outcome"] == {"kind": "revoked", "invitation_id": first["invitation_id"]}
    basis = {"kind": "invitation", "invitation_id": first["invitation_id"]}
    refused(h.claim([GAIN], basis), "invitation_ended")
    refused(h.broker.reduce_withdraw_invitation({"request_id": "page-w2",
                                                 "invitation_id": first["invitation_id"]}),
            "invitation_not_found")
    second = h.invite([FILTER])["outcome"]["invitation"]
    held = h.broker.reduce_hold({"request_id": "page-hold", "resources": [FILTER]})
    assert held["outcome"]["invitations_withdrawn"] == [second["invitation_id"]]
    h.broker.trusted_release_hold(h.channel, {"request_id": "page-release", **h.triple(),
                                              "resources": [FILTER]})
    refused(h.claim([FILTER], {"kind": "invitation", "invitation_id": second["invitation_id"]}),
            "invitation_ended")


def test_a_panic_without_a_request_id_gets_one_nobody_can_post_first(tmp_path):
    h = make(tmp_path)
    ids = {h.broker.panic({})["request_id"] for _ in range(3)}
    assert len(ids) == 3 and all(re.fullmatch(r"page-panic-[0-9a-f]{24}", rid) for rid in ids)


def test_an_ack_from_a_page_that_reported_hidden_does_not_count(tmp_path):
    h = make(tmp_path)
    notice = invited_grant(h, [GAIN])["notice"]
    h.page_state()
    h.broker.trusted_presence(h.channel, h.ui_epoch, False)
    assert h.ack(notice, fetch=False)["outcome"] == "rejected"
    h.broker.trusted_presence(h.channel, h.ui_epoch, True)
    assert h.ack(notice, fetch=False)["accepted"] is True


@pytest.mark.parametrize("bound", ["grants", "invitations", "proposals", "claim_resources",
                                   "permission_history"])
def test_the_section_bounds_hold(tmp_path, monkeypatch, bound):
    import pocket_music.b2b_broker as broker_module

    h = make(tmp_path)
    if bound == "grants":
        monkeypatch.setattr(broker_module, "MAX_GRANTS", 2)
        invited_grant(h, [GAIN])
        invited_grant(h, [FILTER])
        refused(h.claim([TRANSPORT], h.invitation_basis(h.invite([TRANSPORT]))), "grant_limit_reached")
    elif bound == "invitations":
        monkeypatch.setattr(broker_module, "MAX_INVITATIONS", 2)
        h.invite([GAIN])
        h.invite([FILTER])
        refused(h.invite([TRANSPORT]), "invitation_limit_reached")
    elif bound == "proposals":
        monkeypatch.setattr(broker_module, "MAX_PROPOSALS", 2)
        for statement in ("one", "two"):
            assert h.propose(default_draft(statement=statement))["proposal"] is not None
        third = h.propose(default_draft(statement="three"))
        assert third["proposal"] is None and third["warnings"] == ["proposal_limit_reached"]
    elif bound == "claim_resources":
        monkeypatch.setattr(broker_module, "MAX_CLAIM_RESOURCES", 2)  # the synthetic profile has 11 controls
        raises("unsupported_profile", h.claim, [GAIN, FILTER, TRANSPORT],
               {"kind": "invitation", "invitation_id": "i-0000000000000000"})
    else:
        monkeypatch.setattr(broker_module, "MAX_PERMISSION_HISTORY", 3)
        for n in range(5):
            head = h.page_state()["permission_head"]
            expected = None if head is None else (head["permission_id"], head["version"],
                                                  head["state_sha256"])
            receipt = h.issue(default_draft(statement=f"version {n}"), expected=expected)
            permission = receipt["outcome"]["permission"]
            h.broker.reduce_revoke_permission({"request_id": f"page-r{n}",
                                               "permission_id": permission["permission_id"]})
        assert len(h.broker._permissions) == 3


def test_a_fault_inside_a_request_ends_agent_authority_and_keeps_the_sha_honest(tmp_path, monkeypatch):
    h = make(tmp_path)
    permission = issued(h)
    invited_grant(h, [FILTER])
    h.invite([TRANSPORT])
    before = h.observe()

    def fault(*_args):
        raise RuntimeError("a host bug")

    monkeypatch.setattr(h.broker, "_evaluate_claim", fault)
    request = {"request_id": "claim-fault", **h.triple(before), "resources": [GAIN],
               "basis": {"kind": "invitation", "invitation_id": "i-0000000000000000"}, "reason": "x",
               "lease_ms": 3000, "not_after_monotonic_ns": h.now + 5000 * MS}
    with pytest.raises(RuntimeError):
        h.broker.claim(h.session_id, request)
    after = h.observe()
    assert after["grants"] == [] and digest(after["authority_state"]) == after["state_sha256"]
    # As when the journal fails: the permission and the invitations end with the grants.
    assert after["authority_state"]["permissions"] == [] and after["invitations"] == []
    assert after["control_revision"] > before["control_revision"]
    replay = h.broker.claim(h.session_id, request)  # retained, never run again
    assert replay["replayed"] is True and replay["outcome"]["kind"] == "outcome_unknown"
    assert h.broker.request_status(h.session_id, "claim-fault")["state"] == "complete"
    monkeypatch.undo()
    h.tick(4000)
    refused(h.claim([GAIN], h.permission_basis(permission)), "permission_not_active")


def test_a_full_agent_table_refuses_reductions_rather_than_forget_a_request(tmp_path, monkeypatch):
    import pocket_music.b2b_broker as broker_module

    h = make(tmp_path)
    grant = invited_grant(h, [GAIN])["grant"]
    monkeypatch.setattr(broker_module, "MAX_REQUESTS", len(h.broker._requests["agent"]))
    raises("invalid_request", release, h, grant["grant_id"])
    assert h.broker.request_status(h.session_id, grant["request_id"])["state"] == "complete"
    assert h.broker.panic({})["outcome"]["kind"] == "panic"  # the person can always stop


def test_a_hand_back_after_shutdown_creates_no_invitation(tmp_path):
    h = make(tmp_path)
    h.broker.reduce_hold({"request_id": "page-hold", "resources": [GAIN]})
    h.broker.shutdown()
    h.long_press()
    assert h.observe()["invitations"] == [] and owner(h, GAIN)["hold"] == "explicit"
    released = events(h, "gesture_released")[-1]
    assert released["outcome"] == "agent_control_stopped" and released["invitation_id"] is None


def test_input_on_other_controls_never_settles_a_pickup(tmp_path):
    h = make(tmp_path)
    _pickup(h, FILTER, FILTER_2, 40, fader=100)
    h.move("controller/synthetic/headphone-level", 64)  # unqualified: everything yields, nothing is settled
    h.tick(2100)
    assert owner(h, FILTER)["kind"] == "pickup_pending" and events(h, "pickup_settled") == []
    refused(h.claim([FILTER], h.invitation_basis(h.invite([FILTER]))), "pickup_unsettled")
    h.move("controller/synthetic/fx-1/depth", 100)
    h.tick(2100)
    grant = invited_grant(h, [FX, ROUTE], ack=True)["grant"]
    moved = h.act(grant["grant_id"], {"kind": "set", "resource": FX, "value": 30})
    assert moved["outcome"]["kind"] == "verified"
    release(h, grant["grant_id"])
    assert owner(h, FX)["kind"] == "pickup_pending"  # the fader stayed at 100
    h.move("controller/synthetic/fx-1/route", 127)  # the route button shares FX depth's conflict group
    h.tick(2100)
    assert owner(h, FX)["kind"] == "pickup_pending" and events(h, "pickup_settled") == []
    h.broker.reduce_hold({"request_id": "page-hold", "resources": [FILTER]})
    h.move("controller/synthetic/headphone-level", 30)
    h.broker.trusted_release_hold(h.channel, {"request_id": "page-release", **h.triple(),
                                              "resources": [FILTER]})
    assert owner(h, FILTER)["kind"] == "pickup_pending" and owner(h, FILTER)["hold"] is None


def test_a_hand_back_or_release_on_a_held_pickup_drops_the_hold_and_keeps_the_pickup(tmp_path):
    h = make(tmp_path)
    _pickup(h, FILTER, FILTER_2, 40, fader=100)
    h.broker.reduce_hold({"request_id": "page-hold", "resources": [FILTER]})
    refused(h.invite([FILTER]), "resource_held_by_human")  # a page invitation never drops a hold
    h.long_press()  # the pad's scope is the right deck, this filter included
    assert owner(h, FILTER)["kind"] == "pickup_pending" and owner(h, FILTER)["hold"] is None
    h.broker.reduce_hold({"request_id": "page-hold-2", "resources": [FILTER]})
    released = h.broker.trusted_release_hold(h.channel, {"request_id": "page-release", **h.triple(),
                                                         "resources": [FILTER]})
    assert released["outcome"]["resources"] == [FILTER]
    assert owner(h, FILTER)["kind"] == "pickup_pending" and owner(h, FILTER)["hold"] is None


def test_an_invite_never_drops_a_hold_on_another_control_in_its_group(tmp_path):
    h = make(tmp_path)
    h.broker.reduce_hold({"request_id": "page-hold", "resources": [FX]})
    receipt = refused(h.invite([FX, ROUTE]), "resource_held_by_human")
    assert FX in receipt["outcome"]["detail"]
    assert owner(h, FX)["hold"] == "explicit" and h.observe()["invitations"] == []


def test_stop_voids_a_hand_back_pad_held_through_it(tmp_path):
    h = make(tmp_path)
    h.broker.reduce_hold({"request_id": "page-hold", "resources": [GAIN]})
    h.press()
    h.broker.panic({})
    h.tick(600)
    h.release()
    assert h.observe()["invitations"] == [] and owner(h, GAIN)["hold"] == "explicit"


def test_withdraw_after_the_agent_claimed_the_invitation_ends_that_grant(tmp_path):
    h = make(tmp_path)
    invitation = h.invite([GAIN])["outcome"]["invitation"]
    grant = h.claim([GAIN], {"kind": "invitation", "invitation_id": invitation["invitation_id"]})["grant"]
    withdrawn = h.broker.reduce_withdraw_invitation({"request_id": "page-w",
                                                     "invitation_id": invitation["invitation_id"]})
    assert withdrawn["outcome"] == {"kind": "revoked", "invitation_id": invitation["invitation_id"],
                                    "grant_id": grant["grant_id"]}
    assert grants(h) == [] and owner(h, GAIN)["kind"] == "unowned"


def test_events_are_sized_as_the_host_sends_them(tmp_path):
    h = make(tmp_path)
    reason = "\U0001f3b5" * 200  # 800 bytes of UTF-8, 2400 escaped
    for _ in range(40):
        grant = invited_grant(h, [GAIN], reason=reason)["grant"]
        release(h, grant["grant_id"])
    page = h.broker.events(h.session_id, {"max_events": 256, "max_bytes": 65_536})
    wire = sum(len(json.dumps(e, ensure_ascii=True, separators=(",", ":"))) for e in page["events"])
    assert page["truncated_by_bytes"] is True and wire <= 65_536
    raises("invalid_arguments", h.broker.events, h.session_id, {"max_bytes": 262_144})
