# SPDX-License-Identifier: AGPL-3.0-only
"""b2b adapter profiles and the synthetic two-deck mixer. No device, thread or broker is used."""
from __future__ import annotations

import pytest
from b2b_harness import MS, Harness, RecordingSink

from pocket_music.artifact_store import digest
from pocket_music.b2b_adapters import (
    SYNTHETIC_PROFILE,
    AdapterBlocked,
    AdapterContractError,
    AdapterRefused,
    Admission,
    Step,
    SyntheticAdapter,
    mapping_sha256,
    parse_raw,
    synthetic_profile,
    validate_profile,
)
from pocket_music.b2b_clock import TestClock
from pocket_music.errors import PocketError, valid_hint


@pytest.fixture
def rig(tmp_path):
    return Harness(tmp_path, broker=False)


def admission(adapter, *, deadline_ns=None, epoch=None):
    deadline = adapter._clock.now_ns() + 50 * MS if deadline_ns is None else deadline_ns
    return Admission("g-test", "h.1", adapter.epoch() if epoch is None else epoch, 0, 1, deadline)


def step(resource, message, index=0, count=1):
    return Step("r-1", index, count, resource, message)


def _input(profile, address):
    return next(i for i in profile["physical_inputs"] if i["address"] == address)


def _resource(profile, rid):
    return next(r for r in profile["resources"] if r["resource_id"] == rid)


# Each mutation breaks one rule; the hint names the gate where a rule needs native proof.
def _unknown_top_field(p):
    p["focus"] = "serato"


def _unknown_descriptor_field(p):
    _resource(p, "synthetic/left/gain")["soft_takeover"] = True


def _alias_to_unknown(p):
    p["aliases"]["controller/synthetic/channel-9/trim"] = "synthetic/left/volume"


def _alias_equals_canonical(p):
    p["aliases"]["synthetic/left/gain"] = "synthetic/right/gain"


def _message_twice(p):
    _input(p, "controller/synthetic/channel-2/filter")["message"] = {"type": "cc", "channel": 2, "number": 19}


def _gesture_on_a_control_message(p):
    p["gestures"][0]["message"] = {"type": "cc", "channel": 2, "number": 19}


def _unbound_gesture_input(p):
    p["gestures"].clear()


def _two_conflict_groups(p):
    p["conflict_groups"]["synthetic/fx/other"] = ["synthetic/fx/unit-1"]


def _two_yield_groups(p):
    p["yield_groups"]["synthetic/right/extra"] = ["synthetic/right/position"]


def _group_names_undeclared(p):
    p["yield_groups"]["synthetic/left/deck-motion"].append("synthetic/left/pitch")


def _motorized(p):
    _resource(p, "synthetic/mixer/crossfader")["motorized"] = True


def _soft_takeover(p):
    _resource(p, "synthetic/left/gain")["pickup"] = "soft_takeover"


def _monitor_exemption(p):
    _input(p, "controller/synthetic/headphone-level")["classification"] = "monitoring_exempt"


def _short_long_press(p):
    p["gestures"][0]["long_press_ms"] = 299


def _zero_debounce(p):
    p["gestures"][0]["debounce_ms"] = 0


def _unbounded_hold(p):
    p["gestures"][0]["max_hold_ms"] = 10**9


def _zero_step(p):
    _resource(p, "synthetic/left/gain")["step"]["interval_ms"] = 0


def _native(p):
    p["native"] = True


def _partial_conflict_scope(p):
    p["gestures"][0]["scope"].append("synthetic/fx/unit-1")


def _range_on_a_toggle(p):
    _resource(p, "synthetic/left/transport")["range"] = {"minimum": 0, "maximum": 1}


def _one_view_shared_bus(p):
    _resource(p, "synthetic/mixer/crossfader")["shared_bus"] = ["left"]


def _binding_outside_a_group(p):
    _resource(p, "synthetic/left/transport")["binding"] = True


def _encoding_for_another_kind(p):
    _input(p, "controller/synthetic/jog-1")["encoding"] = "absolute_7bit"


def _unqualified_input_names_a_resource(p):
    _input(p, "controller/synthetic/headphone-level")["address"] = "synthetic/left/track-gain"


@pytest.mark.parametrize("mutate, gate", [
    (_unknown_top_field, None), (_unknown_descriptor_field, None), (_alias_to_unknown, None),
    (_alias_equals_canonical, None), (_message_twice, None), (_gesture_on_a_control_message, None),
    (_unbound_gesture_input, None), (_two_conflict_groups, None), (_two_yield_groups, None),
    (_group_names_undeclared, None), (_motorized, None), (_soft_takeover, "b2b_soft_takeover"),
    (_monitor_exemption, "b2b_monitor_exemption"), (_short_long_press, None), (_zero_debounce, None),
    (_unbounded_hold, None), (_zero_step, None), (_native, "b2b_serato_adapter"),
    (_partial_conflict_scope, None),
    (_range_on_a_toggle, None), (_one_view_shared_bus, None), (_binding_outside_a_group, None),
    (_encoding_for_another_kind, None), (_unqualified_input_names_a_resource, None),
])
def test_profile_validation_refuses_conflicting_aliases_duplicate_messages_and_unqualified_features(
        mutate, gate):
    profile = synthetic_profile()
    validate_profile(profile)
    mutate(profile)
    with pytest.raises(PocketError) as raised:
        validate_profile(profile)
    assert raised.value.code == "unsupported_profile"
    assert valid_hint(raised.value.hint)
    if gate is not None:
        assert gate in raised.value.hint


def test_mapping_hash_includes_the_alias_table():
    base = synthetic_profile()
    assert validate_profile(base).sha256 == mapping_sha256(base) == digest(base)
    retargeted = synthetic_profile()
    retargeted["aliases"]["synthetic/right/fx"] = "synthetic/mixer/crossfader"
    added = synthetic_profile()
    added["aliases"]["controller/synthetic/channel-2/volume"] = "synthetic/right/gain"
    hashes = {validate_profile(p).sha256 for p in (base, retargeted, added)}
    assert len(hashes) == 3
    # Key order is not identity: the canonical JSON sorts keys.
    reordered = dict(reversed(list(synthetic_profile().items())))
    assert validate_profile(reordered).sha256 == mapping_sha256(base)


def test_validated_profile_is_a_private_copy_that_callers_cannot_drift():
    document = synthetic_profile()
    profile = validate_profile(document)
    document["aliases"]["synthetic/left/track-gain"] = "synthetic/right/gain"
    returned = profile.document
    returned["aliases"].clear()
    assert profile.canonical("synthetic/left/track-gain") == "synthetic/left/gain"
    assert profile.sha256 == mapping_sha256(profile.document) == mapping_sha256(SYNTHETIC_PROFILE)
    with pytest.raises(TypeError):
        profile.resources["synthetic/left/gain"]["motorized"] = True


def test_physical_and_software_aliases_resolve_to_one_canonical_resource():
    profile = validate_profile(SYNTHETIC_PROFILE)
    names = ["controller/synthetic/channel-2/trim", "synthetic/right/track-gain", "synthetic/right/gain"]
    assert profile.resolve(names) == ("synthetic/right/gain",)
    views = ["synthetic/left/crossfader-view", "synthetic/right/crossfader-view",
             "controller/synthetic/crossfader"]
    assert profile.resolve(views) == ("synthetic/mixer/crossfader",)
    assert profile.resolve(["synthetic/left/fx", "synthetic/right/fx"]) == ("synthetic/fx/unit-1",)
    assert profile.aliases_of("synthetic/right/gain") == ("controller/synthetic/channel-2/trim",
                                                          "synthetic/right/track-gain")
    crossfader = next(r for r in profile.describe() if r["resource_id"] == "synthetic/mixer/crossfader")
    assert crossfader["shared_bus"] == ["left", "right"]
    assert "synthetic/left/crossfader-view" in crossfader["aliases"]


def test_undeclared_names_are_unsupported_profile_and_bad_shapes_are_invalid_arguments():
    profile = validate_profile(SYNTHETIC_PROFILE)
    with pytest.raises(PocketError) as undeclared:
        profile.resolve(["serato/deck-2/gain"])
    assert undeclared.value.code == "unsupported_profile" and "b2b_observe" in undeclared.value.hint
    for shape in ("synthetic/left/gain", [1], None):
        with pytest.raises(PocketError) as bad:
            profile.resolve(shape)
        assert bad.value.code == "invalid_arguments"


def test_yield_and_conflict_closures_cover_coupled_groups():
    profile = validate_profile(SYNTHETIC_PROFILE)
    assert set(profile.yield_closure(("synthetic/right/position",))) == {
        "synthetic/right/position", "synthetic/right/transport"}
    assert set(profile.yield_closure(("synthetic/fx/unit-1/route",))) == {
        "synthetic/fx/unit-1/route", "synthetic/fx/unit-1"}
    # Depth alone is not a binding, so touching it yields only itself.
    assert profile.yield_closure(("synthetic/fx/unit-1",)) == ("synthetic/fx/unit-1",)
    assert profile.conflict_missing(("synthetic/fx/unit-1",)) == ("synthetic/fx/unit-1/route",)
    assert profile.conflict_missing(("synthetic/fx/unit-1", "synthetic/fx/unit-1/route")) == ()
    assert profile.controller_resources() == profile.order
    assert profile.gesture_scope["handback-right"] == tuple(
        f"synthetic/right/{name}" for name in ("gain", "filter", "transport", "position"))


def test_synthetic_profile_is_nonmotorized_and_every_writable_control_needs_a_notice():
    profile = validate_profile(SYNTHETIC_PROFILE)
    assert profile.name == "synthetic-two-deck/v1" and profile.document["native"] is False
    for rid in profile.order:
        d = profile.descriptor(rid)
        assert d["motorized"] is False
        assert d["audibility"] in ("audible", "uncertain")
        assert d["step"] == {"interval_ms": 50, "cancellation_boundary": "between_steps", "drain_ms": 0}
        absolute = d["control_kind"] == "absolute"
        assert d["pickup"] == ("explicit_pickup_required" if absolute else "not_applicable")
    route = profile.descriptor("synthetic/fx/unit-1/route")
    assert (route["binding"], route["agent_writable"], route["audibility"]) == (True, False, "uncertain")
    assert not any(rid.startswith("serato/") for rid in [*profile.order, *profile.aliases])


def test_submit_never_calls_the_sink_and_acks_inline_with_control_readback(rig):
    a = rig.adapter
    submission = a.submit(step("synthetic/right/gain", {"kind": "set", "value": 40}), admission(a))
    assert rig.sink.events == []
    assert submission.ack == "ok" and submission.submission_id == "s-1-1"
    assert submission.readback.value == 40 and submission.readback.scope == "control_readback"
    assert submission.readback.observed_monotonic_ns == rig.now
    # Software only: the synthetic fader keeps its own (unknown) position, so a pickup can be detected.
    assert a.value("synthetic/right/gain") == 40 and a.physical_value("synthetic/right/gain") is None
    assert len(a.sent()) == a.sent_count == 1 and a.sent()[0][0].resource == "synthetic/right/gain"
    filtered = a.submit(step("synthetic/right/filter", {"kind": "set", "value": 30}), admission(a))
    assert filtered.ack == "ok" and filtered.readback is None  # commanded only


@pytest.mark.parametrize("case", ["old_epoch", "deadline_reached", "wrong_kind", "out_of_range", "big_nudge",
                                  "not_writable", "undeclared"])
def test_submit_refuses_another_epoch_a_reached_deadline_and_undeclared_moves(rig, case):
    a = rig.adapter
    moves = {
        "old_epoch": (step("synthetic/right/gain", {"kind": "set", "value": 1}), admission(a, epoch=0)),
        "deadline_reached": (step("synthetic/right/gain", {"kind": "set", "value": 1}),
                             admission(a, deadline_ns=rig.now)),
        "wrong_kind": (step("synthetic/right/gain", {"kind": "nudge", "delta": 1}), admission(a)),
        "out_of_range": (step("synthetic/right/gain", {"kind": "set", "value": 128}), admission(a)),
        "big_nudge": (step("synthetic/right/position", {"kind": "nudge", "delta": 17}), admission(a)),
        "not_writable": (step("synthetic/fx/unit-1/route", {"kind": "set_state", "state": "right"}),
                         admission(a)),
        "undeclared": (step("synthetic/right/volume", {"kind": "set", "value": 1}), admission(a)),
    }
    with pytest.raises(AdapterRefused):
        a.submit(*moves[case])
    assert a.sent() == () and a.value("synthetic/right/gain") == 100 and rig.sink.events == []


@pytest.mark.parametrize("fault", ["block_submit", "lose_route", "disconnect", "closed"])
def test_blocked_or_dependency_lost_writer_accepts_nothing(rig, fault):
    a = rig.adapter
    if fault == "block_submit":
        a.block_submit = True
    elif fault == "lose_route":
        a.lose_route("master")
    elif fault == "disconnect":
        a.disconnect_controller()
    else:
        a.close()
    with pytest.raises(AdapterBlocked):
        a.submit(step("synthetic/right/gain", {"kind": "set", "value": 1}), admission(a))
    assert a.sent() == () and a.value("synthetic/right/gain") == 100


def test_deferred_ack_arrives_through_pump_at_its_due_time_and_not_before(rig):
    a = rig.adapter
    a.ack_mode, a.deferred_ack_ms = "deferred", 20
    submitted_at = rig.now
    submission = a.submit(step("synthetic/right/gain", {"kind": "set", "value": 70}), admission(a))
    assert submission.ack == "pending" and submission.readback is None and rig.sink.events == []
    rig.tick_ns(20 * MS - 1)
    assert rig.sink.of("ack") == []
    rig.tick_ns(1)
    (ack,) = rig.sink.of("ack")
    assert ack == {"type": "ack", "submission_id": submission.submission_id, "status": "ok",
                   "at_ns": submitted_at + 20 * MS}
    (readback,) = rig.sink.of("readback")
    assert readback["resource"] == "synthetic/right/gain" and readback["value"] == 70
    assert rig.sink.types().index("ack") < rig.sink.types().index("readback")
    rig.tick(100)
    assert len(rig.sink.of("ack")) == 1


@pytest.mark.parametrize("mode, ack", [("never", "pending"), ("failed", "failed")])
def test_never_and_failed_acks_are_reported_and_never_retried(rig, mode, ack):
    a = rig.adapter
    a.ack_mode = mode
    submission = a.submit(step("synthetic/right/gain", {"kind": "set", "value": 70}), admission(a))
    assert submission.ack == ack and submission.readback is None
    rig.tick(1000)
    assert rig.sink.of("ack") == [] and a.sent_count == 1


def test_readback_modes_are_stale_or_mismatched_and_motion_has_none(rig):
    a = rig.adapter
    assert a.readback("synthetic/right/gain").value == 100
    assert a.readback("synthetic/right/transport").value == "playing"
    assert a.readback("synthetic/right/filter") is None and a.readback("synthetic/right/position") is None
    a.readback_mode = "stale"
    assert a.readback("synthetic/right/gain").observed_monotonic_ns < rig.now - 5_000 * MS
    a.readback_mode = "mismatch"
    assert a.readback("synthetic/right/gain").value == 110
    assert a.readback("synthetic/right/transport").value == "paused"
    a.readback_mode = "sometimes"
    with pytest.raises(ValueError):
        a.readback("synthetic/right/gain")


def test_classification_of_controls_gestures_unqualified_unknown_and_feedback(rig):
    trim = rig.move("controller/synthetic/channel-2/trim", 40)
    assert [e["type"] for e in trim] == ["physical_input"]
    assert trim[0]["resources"] == ["synthetic/right/gain"]
    assert trim[0]["address"] == "controller/synthetic/channel-2/trim"
    assert [e["phase"] for e in (*rig.press(), *rig.release())] == ["press", "release"]
    assert rig.sink.of("gesture")[0]["gesture_id"] == "handback-right"
    (headphones,) = rig.move("controller/synthetic/headphone-level", 90)
    assert headphones["type"] == "unknown_input" and headphones["classification"] == "unqualified"
    unmapped_note = rig.adapter.inject_message([0x96, 64, 127])  # a note with no binding
    pitch_bend = rig.adapter.inject_message([0xE0, 0, 64])
    garbage = rig.adapter.inject_message("not midi")
    for (event,) in (unmapped_note, pitch_bend, garbage):
        assert event["type"] == "unknown_input" and event["classification"] == "unknown"
    assert garbage[0]["raw"] is None
    wrong_velocity = rig.adapter.inject_message([0x97, 64, 64])  # the pad, but neither press nor release
    assert wrong_velocity[0]["type"] == "unknown_input"
    echo = rig.adapter.inject_message(rig.raw("controller/synthetic/channel-2/trim", 99), origin="feedback")
    assert echo == ({"type": "feedback", "raw": [0xB1, 19, 99], "at_ns": rig.now},)
    assert rig.adapter.value("synthetic/right/gain") == 40  # feedback moves nothing
    with pytest.raises(ValueError):
        rig.adapter.inject_message([0xB1, 19, 1], origin="listen")


def test_absolute_touch_jumps_software_and_records_the_physical_value(rig):
    a = rig.adapter
    a.submit(step("synthetic/right/gain", {"kind": "set", "value": 40}), admission(a))
    (touch,) = rig.move("controller/synthetic/channel-2/trim", 98)
    assert (touch["software_before"], touch["software_after"], touch["value"], touch["physical_value"]) == (
        40, 98, 98, 98)
    assert a.value("synthetic/right/gain") == 98 and a.physical_value("synthetic/right/gain") == 98


def test_jog_decodes_twos_complement_deltas(rig):
    (forward,) = rig.jog(2, 3)
    (back,) = rig.jog(2, -5)
    assert (forward["value"], forward["software_after"]) == (3, 3)
    assert (back["value"], back["software_before"], back["software_after"]) == (-5, 3, -2)
    assert forward["resources"] == ["synthetic/right/position"] and forward["physical_value"] is None


def test_route_press_reports_binding_changed_before_the_physical_input(rig):
    press = rig.move("controller/synthetic/fx-1/route", 127)
    assert [e["type"] for e in press] == ["binding_changed", "physical_input"]
    assert press[0]["group"] == "synthetic/fx/unit-1" and press[0]["value"] == "right"
    assert press[0]["resources"] == ["synthetic/fx/unit-1", "synthetic/fx/unit-1/route"]
    release = rig.move("controller/synthetic/fx-1/route", 0)
    assert [e["type"] for e in release] == ["physical_input"]
    assert rig.adapter.value("synthetic/fx/unit-1/route") == "right"
    rig.move("controller/synthetic/fx-1/route", 127)
    assert rig.adapter.value("synthetic/fx/unit-1/route") == "left"


def test_transport_starts_playing_and_only_a_play_press_or_toggle_step_changes_it(rig):
    a = rig.adapter
    assert a.value("synthetic/left/transport") == a.value("synthetic/right/transport") == "playing"
    rig.move("controller/synthetic/play-1", 127)
    rig.move("controller/synthetic/play-1", 0)
    assert a.value("synthetic/left/transport") == "paused"
    a.submit(step("synthetic/right/transport", {"kind": "toggle"}), admission(a))
    assert a.value("synthetic/right/transport") == "paused"
    with pytest.raises(AdapterRefused):
        a.submit(step("synthetic/right/transport", {"kind": "set_state", "state": "playing"}), admission(a))


@pytest.mark.parametrize("restore", ["reconnect_controller", "restore_route"])
def test_reconnect_and_route_restore_start_a_new_epoch_and_forget_physical_positions(rig, restore):
    a = rig.adapter
    rig.move("controller/synthetic/channel-2/trim", 90)
    a.ack_mode = "deferred"
    a.submit(step("synthetic/left/gain", {"kind": "set", "value": 20}), admission(a))
    if restore == "reconnect_controller":
        a.disconnect_controller()
        # A disconnected controller sends nothing.
        assert rig.move("controller/synthetic/channel-2/trim", 10) == ()
        expected = ("controller", "controller_reconnected")
    else:
        a.lose_route("master")
        expected = ("route:master", "route_restored")
    lost = rig.sink.of("dependency")[-1]
    assert (lost["name"], lost["state"]) == (expected[0], "unavailable")
    getattr(a, restore)()
    epoch = rig.sink.of("epoch")[-1]
    assert (epoch["adapter_epoch"], epoch["reason"]) == (2, expected[1])
    assert epoch["mapping_sha256"] == a.mapping_sha256
    assert epoch["dependencies"][expected[0]] == "available"
    assert a.epoch() == 2 and a.physical_value("synthetic/right/gain") is None
    rig.tick(100)
    assert rig.sink.of("ack") == []  # an old epoch's pending ack never arrives
    getattr(a, restore)()
    assert a.epoch() == 2  # restoring what is available changes nothing


def test_reconnect_with_a_changed_mapping_reports_that_mapping(rig):
    rig.adapter.disconnect_controller()
    rig.adapter.reconnect_controller(mapping_sha256="0" * 64)
    assert rig.sink.of("epoch")[-1]["mapping_sha256"] == "0" * 64


def test_writer_restart_reports_an_epoch_and_renew_epoch_never_calls_the_sink(rig):
    a = rig.adapter
    a.restart_writer()
    assert rig.sink.of("epoch")[-1]["reason"] == "writer_restarted" and a.epoch() == 2
    before = list(rig.sink.events)
    assert a.renew_epoch("clock_discontinuity") == 3 and rig.sink.events == before
    with pytest.raises(AdapterRefused):
        a.submit(step("synthetic/right/gain", {"kind": "set", "value": 1}), admission(a, epoch=2))


def test_heartbeat_events_are_throttled_and_a_frozen_heartbeat_stops_aging(rig):
    rig.tick(1000)
    assert 9 <= len(rig.sink.of("heartbeat")) <= 11
    assert rig.adapter.health()["heartbeat_monotonic_ns"] == rig.now
    rig.adapter.freeze_heartbeat = True
    frozen_at = rig.now
    rig.tick(1000)
    health = rig.adapter.health()
    assert health["heartbeat_monotonic_ns"] == frozen_at
    assert health["dependencies"] == {"controller": "available", "route:master": "available"}


def test_events_reach_the_sink_with_the_adapter_lock_released():
    clock = TestClock()
    adapter = SyntheticAdapter()

    class CheckingSink(RecordingSink):
        def adapter_event(self, event):
            assert not adapter._lock.locked()
            adapter.health()  # would deadlock if the adapter still held its lock
            super().adapter_event(event)

    sink = CheckingSink()
    adapter.attach(sink, clock)
    adapter.inject_message([0xB1, 19, 5])
    adapter.ack_mode = "deferred"
    adapter.submit(step("synthetic/right/gain", {"kind": "set", "value": 9}),
                   Admission("g", "h.1", 1, 0, 1, clock.now_ns() + 50 * MS))
    clock.advance(30 * MS)
    adapter.pump(clock.now_ns())
    adapter.restart_writer()
    assert sink.types() == ["physical_input", "ack", "readback", "heartbeat", "epoch"]


def test_adapter_used_before_attach_is_a_contract_error():
    adapter = SyntheticAdapter()
    with pytest.raises(AdapterContractError):
        adapter.inject_message([0xB1, 19, 5])
    with pytest.raises(AdapterContractError):
        adapter.readback("synthetic/right/gain")
    adapter.attach(RecordingSink(), TestClock())
    with pytest.raises(AdapterContractError):
        adapter.attach(RecordingSink(), TestClock())


def test_parse_raw_accepts_only_three_byte_cc_and_note_messages():
    assert parse_raw([0xB1, 19, 40]) == (("cc", 2, 19), 40)
    assert parse_raw([0x97, 64, 127]) == (("note", 8, 64), 127)
    assert parse_raw([0x87, 64, 99]) == (("note", 8, 64), 0)
    for raw in ([0xE0, 0, 64], [0xB1, 19], [0x31, 19, 40], [0xB1, 128, 0], [0xB1, 19, True], (0xF0, 1, 2)):
        assert parse_raw(raw) is None
