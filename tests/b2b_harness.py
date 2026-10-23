# SPDX-License-Identifier: AGPL-3.0-only
"""Shared b2b test fixtures: a test clock, the synthetic adapter and a journal under tmp_path.

Not collected (no test_ prefix). Nothing sleeps and nothing starts a thread: ``tick`` advances
the TestClock, pumps the adapter and ticks the broker when the harness has one. Physical input
goes through the adapter's own injection methods, exactly as the demo page sends it.
"""
from __future__ import annotations

import itertools

from pocket_music.b2b_adapters import SyntheticAdapter, raw_message, validate_profile
from pocket_music.b2b_broker import Broker
from pocket_music.b2b_clock import TestClock
from pocket_music.b2b_journal import Journal, read_entries

HOST_EPOCH = "h00000000000000b2"
MS = 1_000_000
START_NS = 1_000_000_000_000


def default_draft(**changes) -> dict:
    """A proposed policy for the right deck's gain and filter, idle claims allowed."""
    draft = {"resources": ["synthetic/right/gain", "synthetic/right/filter"], "action_kinds": ["set", "ramp"],
             "limits": [{"resource": "synthetic/right/gain", "minimum": 40, "maximum": 110}],
             "proactive_claims": {"allowed": True, "idle_interval_ms": 4000, "cooldown_ms": 5000},
             "max_lease_ms": 3000, "duration_ms": 1_800_000,
             "statement": "Shape the right deck while you mix."}
    draft.update(changes)
    return draft


class RecordingSink:
    """Stands in for the broker when a test drives the adapter alone."""

    def __init__(self):
        self.events = []

    def adapter_event(self, event: dict) -> None:
        self.events.append(event)

    def of(self, event_type: str) -> list[dict]:
        return [e for e in self.events if e["type"] == event_type]

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]


class Harness:
    """One synthetic host's parts, wired as the host wires them.

    ``broker=False`` attaches a RecordingSink instead of a broker, for adapter and journal tests.
    With a broker, ``observe``, ``triple``, ``claim`` and ``act_begin`` call the agent channel as
    the host's RPC dispatcher would.
    """

    def __init__(self, tmp_path, settings=None, *, profile=None, broker: bool = True,
                 journal_max_bytes: int = 4 * 1024 * 1024, writer=None, fsync=None):
        self.clock = TestClock(START_NS)
        self.adapter = SyntheticAdapter(profile)
        self.profile = validate_profile(self.adapter.profile)
        self.settings = settings
        self.runtime_dir = tmp_path / "runtime"
        self.runtime_dir.mkdir(mode=0o700)
        self.journal_path = self.runtime_dir / "journal.ndjson"
        self.journal = Journal(self.journal_path, clock=self.clock, host_epoch=HOST_EPOCH,
                               max_bytes=journal_max_bytes, writer=writer, fsync=fsync)
        self._ids = itertools.count(1)
        self._channel = None
        self.ui_epoch = None
        self.session_id = None
        self.sink = None
        self.broker = None
        if broker:
            self.broker = Broker(self.adapter, self.clock, self.journal, host_epoch=HOST_EPOCH)
        else:
            self.sink = RecordingSink()
            self.adapter.attach(self.sink, self.clock)

    @property
    def now(self) -> int:
        return self.clock.now_ns()

    def tick(self, ms: int, step_ms: int = 10) -> None:
        """Advance ``ms`` in steps: clock, then adapter pump, then broker tick, as the host scheduler does."""
        steps = [step_ms] * (ms // step_ms) + ([ms % step_ms] if ms % step_ms else [])
        for step in steps:
            self.tick_ns(step * MS)

    def tick_ns(self, ns: int) -> None:
        self.clock.advance(ns)
        self.adapter.pump(self.clock.now_ns())
        if self.broker is not None:
            self.broker.tick()

    def raw(self, address: str, value: int) -> list[int]:
        """The bytes the synthetic controller sends for a physical input address."""
        for item in self.profile.inputs.values():
            if item["address"] == address:
                return raw_message(dict(item["message"]), value)
        raise KeyError(address)

    def move(self, address: str, value: int) -> tuple[dict, ...]:
        return self.adapter.inject_message(self.raw(address, value))

    def jog(self, deck: int, delta: int) -> tuple[dict, ...]:
        return self.move(f"controller/synthetic/jog-{deck}", delta & 0x7F)

    def press(self, gesture_id: str = "handback-right") -> tuple[dict, ...]:
        gesture = self.profile.gesture_by_id[gesture_id]
        return self.adapter.inject_message(raw_message(dict(gesture["message"]), gesture["press_value"]))

    def release(self, gesture_id: str = "handback-right") -> tuple[dict, ...]:
        gesture = self.profile.gesture_by_id[gesture_id]
        return self.adapter.inject_message(raw_message(dict(gesture["message"]), gesture["release_value"]))

    def long_press(self, gesture_id: str = "handback-right", held_ms: int = 600) -> None:
        self.press(gesture_id)
        self.tick(held_ms)
        self.release(gesture_id)

    def entries(self, kind: str | None = None) -> list[dict]:
        self.journal.sync()
        entries, _ = read_entries(self.journal_path)
        return [entry for _, entry in entries if kind is None or entry["kind"] == kind]

    def register(self, *, settings=None, human_reserved=None, proposed_policy=None) -> dict:
        """b2b_prepare's host side, as the provider sends it; sets ``session_id``."""
        request = {"output_dir_resolved": str(self.runtime_dir.parent / "session")}
        settings = self.settings if settings is None else settings
        if settings is not None:
            request["settings"] = settings
        if human_reserved is not None:
            request["human_reserved"] = list(human_reserved)
        if proposed_policy is not None:
            request["proposed_policy"] = proposed_policy
        result = self.broker.register(request)
        self.session_id = result["session_id"]
        return result

    @property
    def channel(self):
        """The trusted channel, taken once from the broker as the host's page takes it."""
        if self._channel is None:
            self._channel = self.broker.trusted_channel()
        return self._channel

    def attach_page(self, *, visible: bool = True, poll: bool = True) -> str:
        """Attach the trusted page; ``poll`` keeps one long-poll outstanding, as the real page does."""
        self.ui_epoch = self.broker.trusted_attach(self.channel)
        self.broker.trusted_presence(self.channel, self.ui_epoch, visible)
        if poll:
            self.broker.trusted_poll(self.channel, self.ui_epoch, True)
        return self.ui_epoch

    def page_state(self) -> dict:
        return self.broker.trusted_state(self.channel, self.ui_epoch)

    def ack(self, notice: dict, *, fetch: bool = True, **overrides) -> dict:
        """Acknowledge a notice as the visible page does: fetch it, then post the exact deadline string."""
        if fetch:
            self.page_state()
        payload = {"notice_id": notice["notice_id"], "grant_id": notice["grant_id"],
                   "session_epoch": notice["session_epoch"], "ui_epoch": self.ui_epoch,
                   "action_deadline_monotonic_ns": str(notice["action_deadline_monotonic_ns"])}
        payload.update(overrides)
        return self.broker.trusted_ack(self.channel, payload)

    def invite(self, resources, *, request_id=None) -> dict:
        """The page's Invite button: an exact-CAS invitation for these resources."""
        return self.broker.trusted_invite(self.channel, {"request_id": request_id or self.request_id("page"),
                                                         **self.triple(), "resources": list(resources)})

    def invitation_basis(self, receipt: dict) -> dict:
        return {"kind": "invitation", "invitation_id": receipt["outcome"]["invitation"]["invitation_id"]}

    def propose(self, draft: dict | None = None) -> dict:
        """The agent's b2b_authorize: it only records a proposal."""
        return self.broker.authorize(self.session_id, {
            "request_id": self.request_id(), **self.triple(), "permission": draft or default_draft()})

    def issue(self, draft: dict | None = None, *, name: str = "Test person",
              statement: str = "Issued in a test", expected=None) -> dict:
        """Propose, preview and issue on the page, exactly as the person would."""
        proposal = self.propose(draft)["proposal"]
        preview = self.broker.trusted_preview(self.channel, {"proposal_id": proposal["proposal_id"]})
        payload = {"request_id": self.request_id("page"), "proposal_id": proposal["proposal_id"],
                   "proposal_sha256": preview["proposal_sha256"], "preview_sha256": preview["preview_sha256"],
                   "attribution_name": name, "attribution_statement": statement}
        if expected is not None:
            payload.update(expected_permission_id=expected[0], expected_permission_version=expected[1],
                           expected_permission_sha256=expected[2])
        return self.broker.trusted_issue(self.channel, payload)

    @staticmethod
    def permission_basis(permission: dict) -> dict:
        return {"kind": "standing_permission", "permission_id": permission["permission_id"],
                "permission_version": permission["version"],
                "revocation_generation": permission["revocation_generation"]}

    def request_id(self, prefix: str = "r") -> str:
        return f"{prefix}-{next(self._ids)}"

    def observe(self) -> dict:
        return self.broker.observe(self.session_id)

    def triple(self, observation: dict | None = None) -> dict:
        o = self.observe() if observation is None else observation
        return {"expected_session_epoch": o["session_epoch"],
                "expected_control_revision": o["control_revision"],
                "expected_state_sha256": o["state_sha256"]}

    def claim(self, resources, basis: dict, *, request_id=None, reason="shape the level", lease_ms=3000,
              not_after_ms=5000, triple=None) -> dict:
        return self.broker.claim(self.session_id, {
            "request_id": request_id or self.request_id(), **(triple or self.triple()),
            "resources": list(resources), "basis": basis, "reason": reason, "lease_ms": lease_ms,
            "not_after_monotonic_ns": self.now + not_after_ms * MS})

    def act_begin(self, grant_id: str, action: dict, *, request_id=None, not_after_ms=5000, triple=None,
                  max_evidence_age_ms=500):
        return self.broker.act_begin(self.session_id, {
            "request_id": request_id or self.request_id(), **(triple or self.triple()), "grant_id": grant_id,
            "action": action, "not_after_monotonic_ns": self.now + not_after_ms * MS,
            "max_evidence_age_ms": max_evidence_age_ms})

    def act(self, grant_id: str, action: dict, *, limit_ms: int = 10_000, **begin) -> dict:
        """act_begin, then tick 10 ms at a time until the act is terminal (bounded by ``limit_ms``)."""
        run = self.act_begin(grant_id, action, **begin)
        for _ in range(limit_ms // 10):
            result = self.broker.act_result(run)
            if result is not None:
                return result
            self.tick(10)
        raise AssertionError(f"The act was still running after {limit_ms} ms of test clock")
