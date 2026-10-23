# SPDX-License-Identifier: AGPL-3.0-only
"""Acts at the broker: one bounded move as short steps, each rechecked before it goes out.

Covers planning, the admission checklist, cancellation between steps, ActionOutcome and the
receipt statuses, and the notice gate: what a page must do before an agent's first move under a
grant. Everything runs on a TestClock with explicit ticks, so these prove ordering and
cancellation boundaries; none of them measures latency.
"""
from __future__ import annotations

import itertools
import threading
from dataclasses import replace

import pytest
from b2b_harness import MS, Harness, default_draft

from pocket_music.artifact_store import receipt as make_receipt
from pocket_music.b2b_adapters import AdapterContractError, SyntheticAdapter
from pocket_music.b2b_broker import Broker
from pocket_music.b2b_journal import RESERVE_BYTES, read_entries
from pocket_music.errors import PocketError

GAIN, FILTER = "synthetic/right/gain", "synthetic/right/filter"
TRANSPORT, POSITION = "synthetic/right/transport", "synthetic/right/position"
LEFT_TRANSPORT = "synthetic/left/transport"
FX, ROUTE = "synthetic/fx/unit-1", "synthetic/fx/unit-1/route"
TRIM_2 = "controller/synthetic/channel-2/trim"
UNCERTAIN = "The control was dispatched; no readback confirmed the final value."
RAMP = {"kind": "ramp", "resource": GAIN, "target": 40, "duration_ms": 500}  # 100 -> 40 in 10 steps


def _harness(tmp_path, *, poll=True, visible=True, **kwargs) -> Harness:
    tmp_path.mkdir(exist_ok=True)
    h = Harness(tmp_path, **kwargs)
    h.register()
    h.attach_page(visible=visible, poll=poll)
    return h


def _claim(h: Harness, resources=(GAIN,), **claim) -> dict:
    receipt = h.claim(list(resources), h.invitation_basis(h.invite(list(resources))), **claim)
    assert receipt["outcome"]["kind"] == "granted", receipt["outcome"]
    return receipt


def _granted(h: Harness, resources=(GAIN,), **claim) -> str:
    """Claim under a page invitation and acknowledge the notice; return the grant_id."""
    receipt = _claim(h, resources, **claim)
    assert h.ack(receipt["notice"])["accepted"]
    return receipt["grant"]["grant_id"]


def _proactive(h: Harness, draft=None) -> tuple[str, dict]:
    """Issue a permission, wait out its idle interval, claim under it and acknowledge."""
    permission = h.issue(draft)["outcome"]["permission"]
    h.tick(permission["proactive_claims"]["idle_interval_ms"])
    receipt = h.claim(permission["resources"], h.permission_basis(permission))
    assert receipt["outcome"]["kind"] == "granted", receipt["outcome"]
    assert h.ack(receipt["notice"])["accepted"]
    return receipt["grant"]["grant_id"], permission


def _ended(h: Harness) -> dict:
    return [e for e in h.broker.events(h.session_id)["events"] if e["type"] == "grant_ended"][-1]


def _journal(root) -> list[dict]:
    trusted, _ = read_entries(root / "runtime" / "journal.ndjson")
    return [entry for _, entry in trusted]


def _sent(h: Harness, resource=GAIN) -> list[dict]:
    return [step.message for step, _ in h.adapter.sent() if step.resource == resource]


def _until(h: Harness, run, limit_ms=5000) -> dict:
    for _ in range(limit_ms // 10):
        result = h.broker.act_result(run)
        if result is not None:
            return result
        h.tick(10)
    raise AssertionError("the act did not finish")


def _after_steps(h: Harness, run, count: int) -> None:
    """Tick until ``count`` steps of this run were submitted (at most one per 50 ms)."""
    for _ in range(count * 10):
        if len(h.adapter.sent()) >= count:
            return
        h.tick(10)
    raise AssertionError(f"only {len(h.adapter.sent())} steps were sent")


# Planning, outcomes and evidence ---------------------------------------------------------------

def test_set_with_numeric_readback_is_verified_by_control_readback_only(tmp_path):
    h = _harness(tmp_path / "fresh")
    result = h.act(_granted(h), {"kind": "set", "resource": GAIN, "value": 80})
    assert result["status"] == "ok" and result["outcome"]["kind"] == "verified"
    assert h.adapter.value(GAIN) == 80 and result["steps"]["dispatched_steps"] == 1
    (ref,) = result["outcome"]["evidence_refs"]
    entry = next(e for e in h.entries("readback") if f"journal:{e['seq']}" == ref)
    assert entry["data"]["value"] == 80 and entry["data"]["scope"] == "control_readback"
    assert result["coverage"]["audio"] == "not_observed"
    assert result["coverage"]["musical_listening"] == "not_performed"
    assert "not that anything sounded right" in result["next_actions"][0]
    mismatch = _harness(tmp_path / "mismatch")
    mismatch.adapter.readback_mode = "mismatch"
    other = mismatch.act(_granted(mismatch), {"kind": "set", "resource": GAIN, "value": 80})
    assert other["outcome"]["kind"] == "dispatched_unverified" and other["uncertainty"] == [UNCERTAIN]
    assert other["warnings"][0].startswith("readback_mismatch")


def test_commanded_only_control_is_dispatched_unverified_with_an_uncertainty_entry(tmp_path):
    h = _harness(tmp_path)
    result = h.act(_granted(h, (FILTER,)), {"kind": "set", "resource": FILTER, "value": 20})
    assert result["status"] == "ok"
    assert result["outcome"] == {"kind": "dispatched_unverified", "request_id": result["request_id"]}
    assert result["uncertainty"] == [UNCERTAIN] and h.adapter.value(FILTER) == 20


def test_ramp_becomes_short_steps_on_the_test_clock_not_a_blocking_fade(tmp_path):
    h = _harness(tmp_path)
    gid = _granted(h, lease_ms=5000, not_after_ms=8000)
    t0 = h.now
    run = h.act_begin(gid, {"kind": "ramp", "resource": GAIN, "target": 60, "duration_ms": 2000},
                      not_after_ms=6000)
    assert h.broker.act_result(run) is None and len(h.adapter.sent()) == 1  # returned at once
    h.tick(49)
    assert len(h.adapter.sent()) == 1
    h.tick(1)
    assert len(h.adapter.sent()) == 2
    result = _until(h, run)
    assert result["outcome"]["kind"] == "verified" and result["steps"]["planned"] == 40
    assert [m["value"] for m in _sent(h)] == list(range(99, 59, -1))
    deadlines = [a.deadline_monotonic_ns for _, a in h.adapter.sent()]
    assert deadlines == [t0 + (i + 1) * 50 * MS for i in range(40)]
    # The step count grows so no step moves further than max_delta (16).
    h.act(gid, {"kind": "set", "resource": GAIN, "value": 0})
    before = len(h.adapter.sent())
    fast = h.act(gid, {"kind": "ramp", "resource": GAIN, "target": 127, "duration_ms": 50})
    values = [0] + [m["value"] for m in _sent(h)[before:]]
    assert fast["steps"]["planned"] == 8 and values[-1] == 127
    assert max(abs(b - a) for a, b in itertools.pairwise(values)) <= 16


def test_ramp_requires_a_known_baseline(tmp_path):
    h = _harness(tmp_path / "commanded")
    gid = _granted(h, (FILTER,))
    refused = h.act(gid, {"kind": "ramp", "resource": FILTER, "target": 90, "duration_ms": 200})
    assert refused["status"] == "conflict" and refused["outcome"]["code"] == "baseline_unknown"
    assert h.adapter.sent() == ()
    first = h.act(gid, {"kind": "set", "resource": FILTER, "value": 30})
    assert first["outcome"]["kind"] == "dispatched_unverified"
    ramp = h.act(gid, {"kind": "ramp", "resource": FILTER, "target": 90, "duration_ms": 200})
    assert ramp["outcome"]["kind"] == "dispatched_unverified" and h.adapter.value(FILTER) == 90
    h.move("controller/synthetic/channel-2/filter", 64)  # a touch clears the baseline
    h.tick(2200)
    again = _granted(h, (FILTER,))
    refused = h.act(again, {"kind": "ramp", "resource": FILTER, "target": 10, "duration_ms": 200})
    assert refused["outcome"]["code"] == "baseline_unknown"
    stale = _harness(tmp_path / "stale")
    stale.adapter.readback_mode = "stale"
    old = stale.act(_granted(stale), RAMP)
    assert old["outcome"]["code"] == "evidence_stale" and stale.adapter.sent() == ()


def test_set_state_with_unknown_state_is_refused_and_never_sent(tmp_path):
    h = _harness(tmp_path)
    gid = _granted(h, (TRANSPORT,))
    h.adapter.readback_mode = "stale"
    refused = h.act(gid, {"kind": "set_state", "resource": TRANSPORT, "state": "paused"})
    assert refused["outcome"]["code"] == "state_unknown" and h.adapter.sent() == ()
    assert h.adapter.value(TRANSPORT) == "playing"
    h.adapter.readback_mode = "fresh"
    same = h.act(gid, {"kind": "set_state", "resource": TRANSPORT, "state": "playing"})
    assert same["outcome"]["kind"] == "verified" and same["steps"]["dispatched_steps"] == 0
    assert same["outcome"]["evidence_refs"] and h.adapter.sent() == ()
    paused = h.act(gid, {"kind": "set_state", "resource": TRANSPORT, "state": "paused"})
    assert paused["outcome"]["kind"] == "verified" and _sent(h, TRANSPORT) == [{"kind": "toggle"}]
    assert h.adapter.value(TRANSPORT) == "paused" and h.adapter.value(LEFT_TRANSPORT) == "playing"


def test_admission_carries_the_grant_epoch_revision_generation_and_step_deadline(tmp_path):
    h = _harness(tmp_path)
    gid = _granted(h)
    t0 = h.now
    h.act(gid, {"kind": "set", "resource": GAIN, "value": 70})
    observation = h.observe()
    ((step, admission),) = h.adapter.sent()
    grant = observation["grants"][0]
    assert (admission.grant_id, admission.session_epoch) == (gid, observation["session_epoch"])
    assert admission.adapter_epoch == observation["evidence"]["adapter"]["adapter_epoch"]
    assert admission.generation == grant["generations"][GAIN]
    assert admission.control_revision == observation["control_revision"]
    assert admission.deadline_monotonic_ns == t0 + 50 * MS
    assert (step.index, step.count, step.resource) == (0, 1, GAIN)


def _condition_released(h, gid):
    h.broker.release(h.session_id, {"request_id": h.request_id(), **h.triple(), "grant_id": gid})


def _condition_generation(h, gid):
    owner = h.broker._owners[GAIN]  # a generation change with its recorded cause, the grant untouched
    h.broker._owners[GAIN] = replace(owner, generation=owner.generation + 1, cause="binding_changed")


def _condition_epoch(h, gid):
    h.adapter.restart_writer()


def _condition_permission(h, gid):
    permission = h.observe()["permissions"][0]
    h.broker.reduce_revoke_permission({"request_id": h.request_id("page"),
                                       "permission_id": permission["permission_id"]})


def _condition_journal_full(h, gid):
    h.journal.max_bytes = h.journal.bytes + RESERVE_BYTES  # the next append overflows


@pytest.mark.parametrize("case, condition, act_options, claim_options, expected", [
    ("released", _condition_released, {}, {}, ("partially_dispatched", "released", 3)),
    ("generation_changed", _condition_generation, {}, {}, ("partially_dispatched", "binding_changed", 3)),
    ("lease_expired", None, {}, {"lease_ms": 300}, ("partially_dispatched", "lease_expired", 6)),
    ("deadline_passed", None, {"not_after_ms": 310}, {}, ("partially_dispatched", "deadline_passed", 7)),
    ("epoch_changed", _condition_epoch, {}, {}, ("partially_dispatched", "epoch_changed", 3)),
    ("evidence_stale", lambda h, gid: setattr(h.adapter, "freeze_heartbeat", True),
     {"max_evidence_age_ms": 50}, {}, ("partially_dispatched", "evidence_stale", 4)),
    ("permission_revoked", _condition_permission, {}, None, ("partially_dispatched", "permission_ended", 3)),
    ("human_input", lambda h, gid: h.move(TRIM_2, 90), {}, {}, ("partially_dispatched", "human_input", 3)),
    ("dependency_unavailable", lambda h, gid: h.adapter.lose_route("master"), {}, {},
     ("partially_dispatched", "resource_unavailable", 3)),
    ("journal_full", _condition_journal_full, {}, {}, ("outcome_unknown", None, 3)),
])
def test_step_admission_rechecks_each_condition(tmp_path, case, condition, act_options, claim_options,
                                                 expected):
    h = _harness(tmp_path)
    if claim_options is None:
        gid, _ = _proactive(h, default_draft(resources=[GAIN]))
    else:
        gid = _granted(h, **claim_options)
    run = h.act_begin(gid, RAMP, **act_options)
    _after_steps(h, run, 3)
    if condition is not None:
        condition(h, gid)
    result = _until(h, run)
    kind, reason, last_step = expected
    assert result["outcome"]["kind"] == kind, result["outcome"]
    assert result["steps"]["submitted"] == last_step and len(h.adapter.sent()) == last_step
    if kind == "partially_dispatched":
        assert result["outcome"] == {"kind": kind, "last_step": last_step, "cancel_reason": reason,
                                     "observation_required": True}
    h.tick(600)
    assert len(h.adapter.sent()) == last_step  # nothing more, ever


def _boundary_lease(h, offset_ns):
    receipt = _claim(h, lease_ms=1000)
    run = h.act_begin(receipt["grant"]["grant_id"], {"kind": "set", "resource": GAIN, "value": 70})
    h.page_state()
    h.clock.advance(receipt["grant"]["lease_expires_monotonic_ns"] - offset_ns - h.now)
    h.adapter.pump(h.now)  # a fresh heartbeat, as the host scheduler would deliver it
    h.ack(receipt["notice"], fetch=False)
    return run


def _boundary_deadline(h, offset_ns):
    receipt = _claim(h)
    run = h.act_begin(receipt["grant"]["grant_id"], {"kind": "set", "resource": GAIN, "value": 70},
                      not_after_ms=1000)
    h.page_state()
    h.clock.advance(1000 * MS - offset_ns)
    h.adapter.pump(h.now)
    h.ack(receipt["notice"], fetch=False)
    return run


def _boundary_step(h, offset_ns):
    run = h.act_begin(_granted(h), RAMP)  # step 2 is due at +50 ms and late from +100 ms
    h.tick_ns(100 * MS - offset_ns)
    return run


@pytest.mark.parametrize("boundary", [_boundary_lease, _boundary_deadline, _boundary_step])
@pytest.mark.parametrize("offset_ns, admitted", [(0, False), (1, True)])
def test_no_step_is_admitted_at_or_after_its_boundary(tmp_path, boundary, offset_ns, admitted):
    h = _harness(tmp_path)
    run = boundary(h, offset_ns)
    expected = (2 if boundary is _boundary_step else 1) if admitted else (
        1 if boundary is _boundary_step else 0)
    assert len(h.adapter.sent()) == expected
    if not admitted:
        result = _until(h, run)
        reason = {_boundary_lease: "lease_expired", _boundary_deadline: "deadline_passed",
                  _boundary_step: "step_late"}[boundary]
        outcome = result["outcome"]
        assert outcome.get("reason", outcome.get("cancel_reason")) == reason


def _revoke_by(h: Harness, how: str, gid: str) -> None:
    if how == "human_input":
        h.move(TRIM_2, 90)
    elif how == "release":
        _condition_released(h, gid)
    elif how == "panic":
        h.broker.panic({"request_id": h.request_id("page")})
    elif how == "permission_revoke":
        _condition_permission(h, gid)
    else:
        h.move("controller/synthetic/fx-1/route", 127)  # a route press rebinds the whole fx unit


_REASONS = {"human_input": "human_input", "release": "released", "panic": "panic",
            "permission_revoke": "permission_ended", "fx_route_press": "binding_changed"}


@pytest.mark.parametrize("how", sorted(_REASONS))
def test_revocation_during_every_dispatch_step(tmp_path, how):
    for k in range(1, 11):
        h = _harness(tmp_path / f"{how}-{k}")
        if how == "permission_revoke":
            gid, _ = _proactive(h, default_draft(resources=[GAIN]))
            action = RAMP
        elif how == "fx_route_press":
            gid = _granted(h, (FX, ROUTE))
            action = {"kind": "ramp", "resource": FX, "target": 100, "duration_ms": 500}  # 0 -> 100, 10 steps
        else:
            gid = _granted(h)
            action = RAMP
        run = h.act_begin(gid, action)
        _after_steps(h, run, k)
        _revoke_by(h, how, gid)
        result = _until(h, run)
        h.tick(600)
        assert len(h.adapter.sent()) == k, (how, k)
        if k < 10:
            assert result["outcome"] == {"kind": "partially_dispatched", "last_step": k,
                                         "cancel_reason": _REASONS[how], "observation_required": True}
        else:
            assert result["outcome"]["kind"] == "verified"  # it finished before the revocation


def test_human_input_between_steps_stops_the_next_step(tmp_path):
    h = _harness(tmp_path)
    run = h.act_begin(_granted(h), RAMP)
    _after_steps(h, run, 4)
    h.tick(20)
    h.move(TRIM_2, 90)
    assert h.broker.act_result(run)["outcome"]["cancel_reason"] == "human_input"  # before any next tick
    h.tick(100)
    assert len(h.adapter.sent()) == 4 and h.adapter.value(GAIN) == 90  # the person's value stays
    owner = next(r for r in h.observe()["resources"] if r["resource_id"] == GAIN)["owner"]
    assert owner["kind"] == "human" and owner["hold"] == "activity"


class _SpyAdapter(SyntheticAdapter):
    """Checks the broker lock during submit and races a physical move against the first one."""

    broker = None

    def __init__(self):
        super().__init__()
        self.held, self.started, self.thread = [], threading.Event(), None

    def submit(self, step, admission):
        self.held.append(self.broker._lock_owner == threading.get_ident() and self.broker._lock.locked())
        if self.thread is None:
            raw = [0xB1, 19, 90]  # channel-2 trim from another thread, while this step is in submit
            self.thread = threading.Thread(target=lambda: (self.started.set(), self.inject_message(raw)))
            self.thread.start()
            assert self.started.wait(5)
        return super().submit(step, admission)


def test_check_and_submit_are_one_critical_section(tmp_path):
    h = Harness(tmp_path, broker=False)
    adapter = _SpyAdapter()
    broker = Broker(adapter, h.clock, h.journal, host_epoch="h00000000000000c4")
    adapter.broker = broker
    h.adapter, h.broker, h.sink = adapter, broker, None
    h.register()
    h.attach_page()
    run = h.act_begin(_granted(h), RAMP)
    adapter.thread.join(10)
    assert not adapter.thread.is_alive()
    result = _until(h, run)
    assert adapter.held == [True] and len(adapter.sent()) == 1
    assert result["outcome"]["cancel_reason"] == "human_input" and result["outcome"]["last_step"] == 1
    kinds = [e["kind"] for e in h.entries() if e["kind"] in ("step_submitted", "human_input")]
    assert kinds == ["step_submitted", "human_input"]  # the move waited until submit had returned


def test_late_scheduler_tick_cancels_rather_than_bursting_steps(tmp_path):
    h = _harness(tmp_path)
    run = h.act_begin(_granted(h), RAMP)
    _after_steps(h, run, 2)
    h.tick_ns(300 * MS)  # the scheduler stalls past several step windows
    result = h.broker.act_result(run)
    assert result["outcome"]["cancel_reason"] == "step_late" and len(h.adapter.sent()) == 2
    h.tick(600)
    assert len(h.adapter.sent()) == 2


def test_blocked_adapter_dispatches_nothing(tmp_path):
    h = _harness(tmp_path)
    gid = _granted(h)
    h.adapter.block_submit = True
    result = h.act(gid, {"kind": "set", "resource": GAIN, "value": 70})
    assert result["status"] == "cancelled"
    assert result["outcome"] == {"kind": "cancelled_before_dispatch", "reason": "adapter_blocked"}
    assert h.adapter.sent() == () and h.adapter.value(GAIN) == 100
    h.adapter.block_submit = False
    run = h.act_begin(gid, RAMP)
    _after_steps(h, run, 2)
    h.adapter.block_submit = True
    assert _until(h, run)["outcome"]["cancel_reason"] == "adapter_blocked"
    assert len(h.adapter.sent()) == 2


@pytest.mark.parametrize("mode", ["never", "failed"])
def test_missing_or_failed_adapter_ack_is_outcome_unknown_without_retry(tmp_path, mode):
    h = _harness(tmp_path)
    gid = _granted(h)
    h.adapter.ack_mode = mode
    run = h.act_begin(gid, {"kind": "set", "resource": GAIN, "value": 70})
    if mode == "never":
        h.tick(240)
        assert h.broker.act_result(run) is None  # draining, inside ack_timeout_ms
        h.tick(10)
    result = h.broker.act_result(run)
    assert result["status"] == "outcome_unknown"
    assert result["outcome"] == {"kind": "outcome_unknown", "request_id": result["request_id"],
                                 "automatic_retry": False}
    assert "b2b_request_status" in result["next_actions"][0]
    assert h.observe()["grants"] == [] and _ended(h)["reason"] == "outcome_unknown"
    h.tick(1000)
    assert len(h.adapter.sent()) == 1  # never resent
    deferred = _harness(tmp_path / "deferred")
    deferred.adapter.ack_mode = "deferred"  # a late but present ack, then readback, still verifies
    on_time = deferred.act(_granted(deferred), {"kind": "set", "resource": GAIN, "value": 70})
    assert on_time["outcome"]["kind"] == "verified" and on_time["steps"]["acknowledged"] == 1


def test_partial_outcome_names_the_last_step_and_requires_observation(tmp_path):
    h = _harness(tmp_path)
    gid = _granted(h)
    run = h.act_begin(gid, RAMP)
    _after_steps(h, run, 4)
    _condition_released(h, gid)
    result = h.broker.act_result(run)
    assert result["status"] == "partial"
    assert result["outcome"] == {"kind": "partially_dispatched", "last_step": 4, "cancel_reason": "released",
                                 "observation_required": True}
    assert result["steps"]["cancelled_from"] == 5 and result["steps"]["planned"] == 10
    assert "b2b_observe before anything else" in result["next_actions"][0]
    cancelled = [e for e in h.entries("step_cancelled") if e["data"]["request_id"] == result["request_id"]]
    assert [e["data"]["from_step"] for e in cancelled] == [5]


def test_one_act_is_one_bounded_move_and_a_concurrent_act_on_the_grant_is_refused(tmp_path):
    h = _harness(tmp_path)
    gid = _granted(h)
    run = h.act_begin(gid, RAMP)
    second = h.act(gid, {"kind": "set", "resource": GAIN, "value": 50})
    assert second["status"] == "conflict" and second["outcome"]["code"] == "action_in_progress"
    assert _until(h, run)["outcome"]["kind"] == "verified" and len(h.adapter.sent()) == 10
    with pytest.raises(PocketError) as caught:  # one move per act: no list of moves is accepted
        h.act_begin(gid, {"kind": "set", "resource": GAIN, "value": 50, "moves": []})
    assert caught.value.code == "invalid_arguments"
    # A waiter wakes on the tick that completes a verified set, though no authority changed.
    h.adapter.ack_mode = "deferred"
    run = h.act_begin(gid, {"kind": "set", "resource": GAIN, "value": 60})
    before = h.observe()
    got, woke = [], threading.Event()

    def wait():
        for _ in range(4):
            result = h.broker.act_wait(run, 30)
            if result is not None:
                got.append(result)
                woke.set()
                return

    waiter = threading.Thread(target=wait)
    waiter.start()
    h.tick(20)
    assert woke.wait(5) and got[0]["outcome"]["kind"] == "verified"
    waiter.join(5)
    after = h.observe()
    assert (after["control_revision"], after["state_sha256"]) == (before["control_revision"],
                                                                   before["state_sha256"])


def test_claim_ack_act_succeeds_with_the_claim_receipts_triple(tmp_path):
    h = _harness(tmp_path)
    receipt = _claim(h)
    cursor = h.broker.last_event_sequence
    woke = threading.Event()
    waiter = threading.Thread(target=lambda: h.broker.wait_for_event(cursor, 30) > cursor and woke.set())
    waiter.start()
    assert h.ack(receipt["notice"])["accepted"]
    assert woke.wait(5)  # the page long-poll woke on the ack
    waiter.join(5)
    triple = {"expected_session_epoch": receipt["session_epoch"],
              "expected_control_revision": receipt["control_revision"],
              "expected_state_sha256": receipt["state_sha256"]}
    observed = h.observe()
    assert (observed["control_revision"], observed["state_sha256"]) == (receipt["control_revision"],
                                                                         receipt["state_sha256"])
    result = h.act(receipt["grant"]["grant_id"], {"kind": "set", "resource": GAIN, "value": 75},
                   triple=triple)
    assert result["outcome"]["kind"] == "verified" and result["revision_check"] == "exact"


def test_ack_before_the_deadline_admits_the_first_step(tmp_path):
    h = _harness(tmp_path)
    receipt = _claim(h)
    run = h.act_begin(receipt["grant"]["grant_id"], {"kind": "set", "resource": GAIN, "value": 70})
    h.page_state()
    h.tick(1000)
    assert h.adapter.sent() == () and h.broker.act_result(run) is None  # waiting for the page
    h.clock.advance(receipt["notice"]["deadline_monotonic_ns"] - 1 - h.now)
    assert h.ack(receipt["notice"], fetch=False)["accepted"]
    assert len(h.adapter.sent()) == 1  # admitted in the ack's own critical section, before any tick
    assert h.broker.act_result(run)["outcome"]["kind"] == "verified"


# The notice gate --------------------------------------------------------------------------------

def test_never_fetched_notice_is_delivery_unknown_and_cancelled_before_dispatch(tmp_path):
    h = _harness(tmp_path / "page")
    receipt = _claim(h)
    deadline = receipt["notice"]["deadline_monotonic_ns"]
    assert deadline == receipt["notice"]["created_monotonic_ns"] + 1500 * MS
    run = h.act_begin(receipt["grant"]["grant_id"], {"kind": "set", "resource": GAIN, "value": 70})
    h.tick(1500)
    notices = h.page_state()["notices"]
    assert [n["state"] for n in notices] == ["delivery_unknown"]
    assert _ended(h)["reason"] == "notice_delivery_unknown"
    assert h.observe()["grants"] == [] and h.adapter.sent() == ()
    assert [e["data"]["state"] for e in h.entries("notice_failed")] == ["delivery_unknown"]
    result = h.broker.act_result(run)
    assert result["status"] == "cancelled"
    assert result["outcome"] == {"kind": "cancelled_before_dispatch", "reason": "notice_delivery_unknown"}
    assert result["notice"]["state"] == "delivery_unknown"


def test_fetched_but_unacknowledged_notice_is_deadline_missed(tmp_path):
    h = _harness(tmp_path / "page")
    receipt = _claim(h)
    run = h.act_begin(receipt["grant"]["grant_id"], RAMP)
    h.page_state()  # the visible page fetched it but never acknowledged it
    h.tick(1500)
    assert _ended(h)["reason"] == "notice_deadline_missed"
    assert h.adapter.sent() == ()
    assert h.broker.act_result(run)["outcome"]["reason"] == "notice_deadline_missed"
    hidden = _harness(tmp_path / "hidden")
    _claim(hidden)
    hidden.page_state()
    hidden.broker.trusted_presence(hidden.channel, hidden.ui_epoch, False)  # fetched, then hidden
    hidden.tick(1500)
    assert _ended(hidden)["reason"] == "notice_delivery_unknown"


def _late_ack(tmp_path, *, tick_first: bool) -> tuple[dict, dict, dict]:
    h = _harness(tmp_path)
    receipt = _claim(h)
    run = h.act_begin(receipt["grant"]["grant_id"], {"kind": "set", "resource": GAIN, "value": 70})
    h.page_state()
    deadline = receipt["notice"]["deadline_monotonic_ns"]
    if tick_first:
        h.tick_ns(deadline - h.now)
    else:
        h.clock.advance(deadline - h.now)  # the ack reaches the lock at the deadline, before any tick
    ack = h.ack(receipt["notice"], fetch=False)
    h.tick(100)
    assert h.adapter.sent() == ()
    return ack, h.observe(), h.broker.act_result(run)


def test_late_ack_is_journaled_and_never_produces_a_step(tmp_path):
    raced, raced_view, raced_act = _late_ack(tmp_path / "raced", tick_first=False)
    ticked, ticked_view, ticked_act = _late_ack(tmp_path / "ticked", tick_first=True)
    # Whether the scheduler or the ack reaches the lock first, the deadline wins the same way.
    assert raced["accepted"] is False and raced["outcome"] == ticked["outcome"] == "deadline_missed"
    assert raced_view["grants"] == ticked_view["grants"] == []
    assert raced_act["outcome"] == ticked_act["outcome"] == {"kind": "cancelled_before_dispatch",
                                                             "reason": "notice_deadline_missed"}
    kinds = [e["kind"] for e in _journal(tmp_path / "raced")]
    assert "ack_late" in kinds and "ack_accepted" not in kinds and "step_admitted" not in kinds


def test_mismatched_or_prior_epoch_ack_is_rejected_and_does_not_count(tmp_path):
    h = _harness(tmp_path / "page")
    receipt = _claim(h, lease_ms=5000, not_after_ms=8000)
    notice = receipt["notice"]
    run = h.act_begin(receipt["grant"]["grant_id"], {"kind": "set", "resource": GAIN, "value": 70},
                      not_after_ms=8000)
    h.page_state()
    for wrong in ({"grant_id": "g-" + "0" * 32},
                  {"action_deadline_monotonic_ns": str(notice["action_deadline_monotonic_ns"] + 1)},
                  {"session_epoch": f"{h.broker.host_epoch}.0"}):
        assert h.ack(notice, fetch=False, **wrong) == {"accepted": False, "outcome": "rejected",
                                                      "notice_id": notice["notice_id"]}
    old_ui = h.ui_epoch
    h.attach_page()  # a newer page attaches; the old page's epoch stops counting
    assert h.ack(notice, fetch=False, ui_epoch=old_ui)["outcome"] == "rejected"
    assert h.ack(notice, fetch=False)["outcome"] == "rejected"  # this page has not fetched it yet
    pending = next(n for n in h.page_state()["notices"] if n["notice_id"] == notice["notice_id"])
    assert pending["state"] == "pending" and pending["rejected_acks"] == 5
    assert h.adapter.sent() == () and h.broker.act_result(run) is None  # none of them counted
    assert h.ack(notice, fetch=False)["accepted"] is True
    assert h.ack(notice, fetch=False) == {"accepted": True, "outcome": "acknowledged",
                                          "notice_id": notice["notice_id"]}  # a repeat is a no-op
    assert len([e for e in _journal(tmp_path / "page") if e["kind"] == "ack_accepted"]) == 1
    assert h.broker.act_result(run)["outcome"]["kind"] == "verified" and len(h.adapter.sent()) == 1


def test_admitted_step_keeps_its_own_outcome_when_the_page_denies_or_detaches_after_ack(tmp_path):
    h = _harness(tmp_path / "deny")
    receipt = _claim(h)
    gid, notice = receipt["grant"]["grant_id"], receipt["notice"]
    assert h.ack(notice)["accepted"]
    run = h.act_begin(gid, RAMP)
    _after_steps(h, run, 3)
    h.broker.reduce_deny({"request_id": h.request_id("page"), "notice_id": notice["notice_id"],
                          "grant_id": gid})
    denied = _until(h, run)
    assert denied["outcome"] == {"kind": "partially_dispatched", "last_step": 3,
                                 "cancel_reason": "notice_denied", "observation_required": True}
    assert denied["notice"]["state"] == "denied"  # the notice record is its own field
    detached = _harness(tmp_path / "detach")
    gid = _granted(detached)
    run = detached.act_begin(gid, RAMP)
    _after_steps(detached, run, 3)
    detached.broker.trusted_presence(detached.channel, detached.ui_epoch, False)
    detached.broker.trusted_poll(detached.channel, detached.ui_epoch, False)
    result = _until(detached, run)
    assert result["outcome"]["kind"] == "verified" and len(detached.adapter.sent()) == 10
    assert result["notice"]["state"] == "acknowledged"


def test_notice_failure_sends_nothing_and_leaves_transport_playing(tmp_path):
    h = _harness(tmp_path)
    receipt = _claim(h, (GAIN, TRANSPORT))
    gain = h.act_begin(receipt["grant"]["grant_id"], RAMP)
    h.tick(1600)
    assert h.broker.act_result(gain)["status"] == "cancelled"
    assert h.adapter.sent() == () and h.adapter.value(GAIN) == 100
    assert h.adapter.value(TRANSPORT) == h.adapter.value(LEFT_TRANSPORT) == "playing"


def test_agent_channel_has_no_notice_acknowledgement(tmp_path):
    agent_methods = {"register", "observe", "authorize", "claim", "act_begin", "act_result", "act_wait",
                     "cancel_run", "release", "revoke", "events", "request_status"}
    public = {name for name in dir(Broker) if not name.startswith("_")}
    assert not any("ack" in name for name in public & agent_methods)
    assert {name for name in public if "ack" in name} == {"trusted_ack"}
    h = _harness(tmp_path / "page")
    receipt = _claim(h)
    for forged in (None, object()):
        with pytest.raises(PocketError) as caught:
            h.broker.trusted_ack(forged, {"notice_id": receipt["notice"]["notice_id"]})
        assert caught.value.code == "invalid_request"
    assert h.page_state()["notices"][0]["state"] == "pending"


def test_claim_is_refused_without_an_attached_visible_page(tmp_path):
    h = Harness(tmp_path)
    h.register()

    def claim():
        return h.claim([GAIN], {"kind": "invitation", "invitation_id": "i-0000000000000000"})

    receipt = claim()
    assert receipt["status"] == "needs_input"
    assert receipt["outcome"]["code"] == "notice_destination_unavailable"
    h.attach_page(visible=False)
    assert claim()["outcome"]["code"] == "notice_destination_unavailable"
    h.broker.trusted_presence(h.channel, h.ui_epoch, True)
    h.broker.trusted_poll(h.channel, h.ui_epoch, False)  # no long-poll outstanding now
    h.tick(3001)  # not seen within trusted_page_timeout_ms
    assert claim()["outcome"]["code"] == "notice_destination_unavailable"
    h.broker.trusted_presence(h.channel, h.ui_epoch, True)
    assert claim()["outcome"]["code"] == "invitation_not_found"  # the page check passed
    assert h.observe()["notices"] == []


# Receipts and the adapter contract ---------------------------------------------------------------

_CLOSED_STATUSES = {"ok", "needs_input", "unsupported", "conflict", "cancelled", "partial", "failed",
                    "outcome_unknown"}


def test_receipt_statuses_follow_the_outcome_table(tmp_path):
    h = _harness(tmp_path)
    statuses = {}
    granted = _claim(h)
    statuses["granted"] = granted["status"]
    gid = granted["grant"]["grant_id"]
    run = h.act_begin(gid, RAMP)
    h.ack(granted["notice"])
    _after_steps(h, run, 2)
    statuses["narrowed_or_released"] = h.broker.release(h.session_id, {
        "request_id": h.request_id(), **h.triple(), "grant_id": gid})["status"]
    statuses["partially_dispatched"] = h.broker.act_result(run)["status"]
    pickup = h.claim([GAIN], h.invitation_basis(h.invite([GAIN])))  # the moved gain awaits pickup
    statuses["pickup_unsettled"] = pickup["status"]
    assert pickup["outcome"]["code"] == "pickup_unsettled"
    h.broker.trusted_accept_pickup(h.channel, {"request_id": h.request_id("page"), **h.triple(),
                                               "resource": GAIN})
    statuses["authority_issuer_required"] = h.propose()["status"]
    statuses["refused_other"] = h.claim([GAIN], {"kind": "invitation",
                                                 "invitation_id": "i-0000000000000000"})["status"]
    gid = _granted(h)
    statuses["verified"] = h.act(gid, {"kind": "set", "resource": GAIN, "value": 60})["status"]
    gid = _granted(h, (FILTER,))
    unverified = h.act(gid, {"kind": "set", "resource": FILTER, "value": 60})
    statuses["dispatched_unverified"] = unverified["status"]
    assert unverified["uncertainty"] == [UNCERTAIN]
    statuses["revoked"] = h.broker.revoke(h.session_id, {"request_id": h.request_id(), **h.triple(),
                                                         "target": {"kind": "all_own_authority"}})["status"]
    left = "synthetic/left/gain"  # the right gain now waits for pickup again
    gid = _granted(h, (left,))
    h.adapter.block_submit = True
    blocked = h.act(gid, {"kind": "set", "resource": left, "value": 60})
    statuses["cancelled_before_dispatch"] = blocked["status"]
    h.adapter.block_submit, h.adapter.ack_mode = False, "failed"
    statuses["outcome_unknown"] = h.act(gid, {"kind": "set", "resource": left, "value": 60})["status"]
    assert statuses == {"granted": "ok", "narrowed_or_released": "ok", "partially_dispatched": "partial",
                        "pickup_unsettled": "needs_input",
                        "authority_issuer_required": "needs_input", "refused_other": "conflict",
                        "verified": "ok", "dispatched_unverified": "ok", "revoked": "ok",
                        "cancelled_before_dispatch": "cancelled", "outcome_unknown": "outcome_unknown"}
    assert set(statuses.values()) <= _CLOSED_STATUSES - {"unsupported", "failed"}
    for code in ("pickup_unsettled", "notice_destination_unavailable", "authority_issuer_required"):
        made = h.broker._receipt("claim", "r-table", h.broker._refused(code, "table"), h.now, "exact", None)
        assert made["status"] == "needs_input"
    with pytest.raises(PocketError):
        make_receipt("r-x", "rejected")  # receipt() itself keeps the status set closed


class _ReentrantSubmit(SyntheticAdapter):
    """Calls the broker from inside submit, which the adapter contract forbids."""

    swallow = False

    def submit(self, step, admission):
        submission = super().submit(step, admission)
        try:
            self._sink.adapter_event({"type": "heartbeat", "at_ns": 0})
        except AdapterContractError:
            if not self.swallow:
                raise
        return submission


@pytest.mark.parametrize("swallow", [False, True])
def test_sink_called_from_inside_submit_makes_that_step_outcome_unknown(tmp_path, swallow):
    h = Harness(tmp_path, broker=False)
    adapter = _ReentrantSubmit()
    adapter.swallow = swallow
    broker = Broker(adapter, h.clock, h.journal, host_epoch="h00000000000000c5")
    h.adapter, h.broker, h.sink = adapter, broker, None
    h.register()
    h.attach_page()
    gid = _granted(h)
    result = h.act(gid, RAMP)
    assert result["outcome"]["kind"] == "outcome_unknown" and broker._sink_violations == 1
    assert len(adapter.sent()) == 1 and h.observe()["grants"] == []
    h.tick(600)
    assert len(adapter.sent()) == 1  # never retried
