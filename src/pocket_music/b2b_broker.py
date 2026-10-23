# SPDX-License-Identifier: AGPL-3.0-only
"""The b2b authority broker: who owns each control, and when an agent may move one.

One broker per host serializes every authority change, and the admission of every control
step, under one lock. A person's touch always wins: physical input takes the touched
controls and their coupled groups back before the agent's next step, and nothing is
restored or resumed afterwards.

Agents never issue policy. ``authorize`` from the agent channel only records a proposal;
only the trusted page, which holds the one ``TrustedChannel`` object, issues a standing
permission. Attribution records what a person typed. It never authenticates anything.

Every agent mutation cites the observation triple (session_epoch, control_revision,
state_sha256). The revision advances exactly when the SHA of the authority-only state
changes, in ``_commit``. Human mixing on free controls, notice acknowledgements and
proposals change no authority state, so they never stale the agent.

Time comes only from the injected clock. The broker never sleeps and starts no timer: due
work runs lazily in ``_advance`` at every entry, in a fixed order. An act is one bounded move
planned as short steps; ``_admit_step`` rechecks every condition before each one and is the
only code that calls ``adapter.submit``.
"""
from __future__ import annotations

import itertools
import json
import re
import secrets
import threading
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from types import MappingProxyType

from .artifact_store import canonical_bytes, digest, receipt
from .b2b_adapters import (
    AdapterBlocked,
    AdapterContractError,
    AdapterRefused,
    Admission,
    Readback,
    Step,
    Submission,
    validate_profile,
)
from .b2b_clock import suspend_gap_ns
from .b2b_journal import JournalUnavailable, read_line
from .errors import PocketError

AUTHORITY_SCHEMA = "pocket.b2b-authority-state/v1"
OBSERVATION_SCHEMA = "pocket.b2b-observation/v1"
EVENTS_SCHEMA = "pocket.b2b-events/v1"
PERMISSION_SCHEMA = "pocket.b2b-permission/v1"
PROPOSAL_SCHEMA = "pocket.b2b-proposal/v1"
MS = 1_000_000

# name: (default, minimum, maximum), all in milliseconds. b2b_prepare may override each one.
SETTINGS = MappingProxyType({
    "notice_ack_timeout_ms": (1500, 100, 10_000),
    "max_lease_ms": (10_000, 250, 60_000),
    "activity_release_ms": (2000, 100, 60_000),
    "settle_ms": (100, 0, 10_000),
    "invitation_ttl_ms": (30_000, 1000, 600_000),
    "trusted_page_timeout_ms": (3000, 1000, 60_000),
    "max_action_ms": (4000, 50, 4000),
    "max_evidence_age_ms": (500, 50, 5000),
    "ack_timeout_ms": (250, 10, 5000),
    "uncertainty_ms": (2000, 0, 60_000),
    "suspend_threshold_ms": (1000, 100, 60_000),
})

MAX_GRANTS, MAX_INVITATIONS, MAX_PROPOSALS, MAX_PERMISSION_HISTORY = 8, 8, 16, 32
MAX_NOTICES, MAX_CLAIM_RESOURCES, MAX_REQUESTS, RECEIPT_CACHE = 256, 16, 16_384, 1024
MAX_EVENTS, MAX_EVENT_BYTES, MAX_RECEIPT_BYTES = 4096, 4 * 1024 * 1024, 64 * 1024
MAX_EVENTS_PAGE_BYTES = 192 * 1024  # below the 256 KiB response line, with room for the envelope
MAX_GRANT_HISTORY, MAX_ENDED_RECORDS, MAX_RUN_HISTORY = 256, 64, 256
COALESCE_NS = 250 * MS  # human-input and refusal events coalesce within this window
MAX_AHEAD_NS = 60_000 * MS  # a claim or act deadline is at most 60 s ahead
PICKUP_TOLERANCE = 2
VERIFY_TOLERANCE = 2  # a numeric readback within 2 of the final value verifies; a state must match
ACTION_KINDS = ("set", "ramp", "nudge", "set_state")

_CLAIM_CODES = ("resource_held_by_human", "resource_owned_by_agent", "resource_unavailable",
                "pickup_unsettled", "human_active", "gesture_in_progress", "classification_uncertain",
                "claim_cooldown",
                "invitation_not_found", "invitation_ended", "invitation_scope", "permission_not_active",
                "permission_scope", "proactive_not_permitted", "lease_exceeds_limit", "deadline_passed",
                "notice_destination_unavailable", "grant_limit_reached", "journal_unavailable")
_ACT_CODES = ("grant_not_found", "grant_not_active", "resource_not_granted", "action_in_progress",
              "action_not_permitted", "outside_limits", "baseline_unknown", "state_unknown", "evidence_stale",
              "adapter_unavailable", "journal_unavailable", "deadline_passed")
_REDUCTION_CODES = ("grant_not_found", "expansion_not_permitted", "authority_issuer_required",
                    "proposal_not_found", "resource_not_granted")
_PAGE_CODES = ("proposal_changed", "confirmation_mismatch", "permission_active", "permission_not_active",
               "stale_page_state", "invitation_limit_reached")
REFUSAL_CODES = tuple(dict.fromkeys(_CLAIM_CODES + _ACT_CODES + _REDUCTION_CODES + _PAGE_CODES))
NEEDS_INPUT_CODES = frozenset({"authority_issuer_required", "pickup_unsettled",
                               "notice_destination_unavailable"})
CANCEL_REASONS = ("human_input", "unknown_input", "binding_changed", "released", "revoked_by_agent",
                  "revoked_by_human", "held_by_human", "permission_ended", "lease_expired", "deadline_passed",
                  "step_late", "notice_deadline_missed", "notice_delivery_unknown", "notice_denied",
                  "epoch_changed", "clock_discontinuity", "resource_unavailable", "evidence_stale",
                  "adapter_blocked", "adapter_refused", "journal_unavailable", "panic", "host_stopping",
                  "host_wait_exceeded", "outcome_unknown")
EVENT_TYPES = ("session_registered", "page_attached", "page_detached",
               "grant_created", "grant_ended", "step_admitted", "step_cancelled", "action_completed",
               "claim_refused",
               "notice_created", "notice_acknowledged", "notice_failed", "notice_denied",
               "human_input", "unknown_input", "gesture_pressed", "gesture_released", "release_lost",
               "pickup_pending", "pickup_settled",
               "invitation_created", "invitation_ended", "proposal_recorded", "permission_issued",
               "permission_amended", "permission_ended",
               "resource_unavailable", "resource_available", "epoch_changed", "panic", "journal_degraded")
# Human takeovers of a standing-permission grant start a claim cooldown on the taken controls.
_TAKEOVER_REASONS = frozenset({"human_input", "unknown_input", "binding_changed"})
_COOLDOWN_CODES = frozenset({"human_active", "gesture_in_progress", "pickup_unsettled",
                             "classification_uncertain"})
_OK_KINDS = frozenset({"prepared", "granted", "released", "narrowed", "revoked", "verified",
                       "dispatched_unverified", "invited", "held", "hold_released", "pickup_accepted",
                       "issued", "amended", "dismissed", "denied", "panic"})

COVERAGE = MappingProxyType({
    "adapter": "synthetic_no_native_device", "audio": "not_observed", "listen": "not_integrated",
    "musical_listening": "not_performed", "latency": "not_measured",
    "notice": "delivery_to_page_not_attention",
    "playback": "not_changed_by_b2b_refusal_or_cancel", "durability": "process_crash_only"})
PROVENANCE = MappingProxyType({"provider": "pocket", "contract": "b2b-v1", "canonicalizer": "json-v1"})

_RETRY_LATER = "Wait until retry_not_before_monotonic_ns, then observe and claim with a new request_id."
_OBSERVE_AGAIN = "Call b2b_observe; claim again if a resource is claimable."
_POLICY_CURRENT = ("Call b2b_observe and use the current permission_id, version and revocation_generation, "
                   "or ask for an invitation.")
_NOTICE_FAILED = "Call b2b_observe; claim again later with a new request_id."
_HOST_HEALTH = "Call b2b_observe; wait for the person to restart the host."
_COME_BACK = ("Call b2b_observe; wait until the controller or master route returns, then claim again in the "
              "new epoch.")
_INVITE_AGAIN = "Call b2b_observe for outstanding invitations, or ask the person to hand the controls back."
NEXT_ACTIONS = MappingProxyType({
    "authority_issuer_required": ("Ask the person to review proposal {proposal_id} on the b2b host page; "
                                  "after they issue it, b2b_observe shows its permission_id, version and "
                                  "revocation_generation."),
    "permission_not_active": _POLICY_CURRENT, "permission_scope": _POLICY_CURRENT,
    "proactive_not_permitted": _POLICY_CURRENT,
    "human_active": _RETRY_LATER,
    "gesture_in_progress": ("The person is pressing a pad; observe until gestures read idle, then claim "
                            "with a new request_id."),
    "resource_held_by_human": "Don't claim it; it stays held until the person releases it.",
    "classification_uncertain": "Retry after retry_not_before_monotonic_ns with a new request_id.",
    "claim_cooldown": "Retry after retry_not_before_monotonic_ns with a new request_id.",
    "pickup_unsettled": "The person settles this on the page; nothing you send can.",
    "notice_destination_unavailable": ("Ask the person to open the b2b page link from the host terminal and "
                                       "keep it visible."),
    "notice_delivery_unknown": _NOTICE_FAILED, "notice_deadline_missed": _NOTICE_FAILED,
    "notice_denied": _NOTICE_FAILED,
    "grant_not_found": _OBSERVE_AGAIN, "grant_not_active": _OBSERVE_AGAIN, "lease_expired": _OBSERVE_AGAIN,
    "deadline_passed": _OBSERVE_AGAIN, "epoch_changed": _OBSERVE_AGAIN,
    "action_not_permitted": "Stay within the limits b2b_observe shows.",
    "outside_limits": "Stay within the limits b2b_observe shows.",
    "baseline_unknown": "Call b2b_observe; set the control before ramping it.",
    "state_unknown": "Call b2b_observe; never toggle without a known state.",
    "evidence_stale": "Call b2b_observe; never toggle without a known state.",
    "expansion_not_permitted": "Ask the person for a wider permission or an invitation.",
    "journal_unavailable": _HOST_HEALTH, "adapter_unavailable": _COME_BACK,
    "resource_unavailable": _COME_BACK,
    "partially_dispatched": "Call b2b_observe before anything else; the last steps may still land.",
    "outcome_unknown": "Call b2b_request_status, then b2b_observe; never resend under a new request_id.",
    "resource_owned_by_agent": "Call b2b_observe; one of your grants already holds it.",
    "invitation_not_found": _INVITE_AGAIN, "invitation_ended": _INVITE_AGAIN,
    "invitation_scope": _INVITE_AGAIN,
    "lease_exceeds_limit": "Ask for a lease no longer than the permission's max_lease_ms.",
    "grant_limit_reached": "Release a grant you no longer need, then claim with a new request_id.",
    "resource_not_granted": "Name only resources your grant holds; b2b_observe lists them.",
    "action_in_progress": "Wait for the running act to finish, then act again with a new request_id.",
    "proposal_not_found": "Call b2b_observe for your open proposals.",
    "granted": ("Call b2b_act with this grant_id; its first step waits until the page acknowledges the "
                "notice."),
    "verified": ("Call b2b_observe before your next move; verified means the control read the value back, "
                 "not that anything sounded right."),
    "dispatched_unverified": "Call b2b_observe before your next move; no readback confirmed the final value.",
    "cancelled_before_dispatch": ("Nothing was sent. Call b2b_observe; claim again if a resource is "
                                  "claimable."),
    "step_late": "Nothing more was sent. Call b2b_observe, then act again with a new request_id.",
    "adapter_blocked": _HOST_HEALTH, "adapter_refused": _HOST_HEALTH,
    "released": "Call b2b_observe before claiming again.",
    "narrowed": "Call b2b_observe before claiming again.",
    "revoked": "Call b2b_observe before claiming again.",
})

REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_STALE_HINT = "Call b2b_observe and pass its session_epoch, control_revision and state_sha256."
_UNKNOWN_FIELD_HINT = ("Remove {field!r}. b2b requests take only their declared fields; no agent input "
                       "carries an issuer, token, capability, actor or actor_kind.")
_STATUS_HINT = ("Call b2b_request_status with this session_dir and request_id; never retry under a new "
                "request_id.")
_SESSION_HINT = ("Pass the session_dir that b2b_prepare created for this host; each b2b host holds one "
                 "session.")
_ONE_SESSION_HINT = ("Each b2b host holds one session; start a new host with a new runtime dir for a new "
                     "session.")
_CHANNEL_HINT = "Only the b2b host page can make this call; reopen the page link from the host terminal."
_REVOKE_POLICY_NEXT = ("Ask the person to press Revoke or Stop agent control on the b2b host page; you can "
                       "drop your own grants with all_own_authority.")


def _bad(message: str, hint: str | None = None) -> PocketError:
    return PocketError(message, code="invalid_arguments", hint=hint)


_PROFILE_HINT = "b2b_observe lists each declared resource and its aliases."


def _profile_error(message: str, hint: str = _PROFILE_HINT) -> PocketError:
    return PocketError(message, code="unsupported_profile", hint=hint)


def _fields(value, required, optional=(), where="request") -> dict:
    if not isinstance(value, dict):
        raise _bad(f"{where} must be a JSON object",
                   f"Pass {where} as a JSON object with its declared fields.")
    unknown = sorted(set(value) - set(required) - set(optional))
    if unknown:
        raise _bad(f"Unknown field {unknown[0]!r} in {where}", _UNKNOWN_FIELD_HINT.format(field=unknown[0]))
    missing = [name for name in required if name not in value]
    if missing:
        raise _bad(f"{where} is missing {missing[0]!r}", f"Add {missing[0]!r} to {where}.")
    return value


def _int(value, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _bad(f"{name} must be an integer from {minimum} to {maximum}",
                   f"Pass {name} as a whole number from {minimum} to {maximum}.")
    return value


def _text(value, name: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise _bad(f"{name} must be text of {minimum} to {maximum} characters",
                   f"Pass {name} as a string of {minimum} to {maximum} characters.")
    return value


def _bool(value, name: str) -> bool:
    if type(value) is not bool:
        raise _bad(f"{name} must be true or false", f"Pass {name} as a JSON boolean.")
    return value


def _request_id(value) -> str:
    if not isinstance(value, str) or not REQUEST_ID.fullmatch(value):
        raise _bad("request_id must be 1-96 letters, digits, '.', '_' or '-' starting with a letter or digit",
                   "Pass a new request_id such as claim-0001 for each new request; reuse it only to retry.")
    return value


def _sha(value, name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise _bad(f"{name} must be a lowercase SHA-256 hex digest", _STALE_HINT)
    return value


def _ident(value, name: str, prefix: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix) or not 2 <= len(value) <= 64:
        raise _bad(f"{name} must be a {prefix}... identifier", f"Pass the {name} that b2b_observe reports.")
    return value


def _triple(request: dict, *, required: bool = True) -> tuple | None:
    """The observation triple a mutation cites: (session_epoch, control_revision, state_sha256)."""
    names = ("expected_session_epoch", "expected_control_revision", "expected_state_sha256")
    if not required and not any(request.get(name) is not None for name in names):
        return None
    epoch = request.get("expected_session_epoch")
    if not isinstance(epoch, str) or not 1 <= len(epoch) <= 64:
        raise _bad("expected_session_epoch must be the session_epoch string b2b_observe reports", _STALE_HINT)
    return (epoch, _int(request.get("expected_control_revision"), "expected_control_revision", 0, 2**63 - 1),
            _sha(request.get("expected_state_sha256"), "expected_state_sha256"))


def normalize_settings(value) -> dict:
    """Fill b2b_prepare settings with defaults; refuse unknown or out-of-range fields."""
    value = {} if value is None else value
    _fields(value, (), tuple(SETTINGS), "settings")
    return {name: _int(value.get(name, default), f"settings.{name}", low, high)
            for name, (default, low, high) in SETTINGS.items()}


def _seconds(ms: int) -> str:
    return f"{ms / 1000:.1f} s"


def _duration_text(ms: int) -> str:
    if ms % 3_600_000 == 0:
        hours = ms // 3_600_000
        return f"{hours} hour" + ("" if hours == 1 else "s")
    if ms % 60_000 == 0:
        minutes = ms // 60_000
        return f"{minutes} minute" + ("" if minutes == 1 else "s")
    return _seconds(ms)


def render_preview(draft: dict, profile) -> list[str]:
    """The lines a person reads before issuing a permission. Time-independent, so its SHA is stable.

    A permission governs idle claims only: an invited claim is bounded by each control's own
    range and the host's lease limit, so the lines never suggest the permission limits those.
    """
    limits = {item["resource"]: item for item in draft.get("limits", [])}
    named = []
    for rid in draft["resources"]:
        d = profile.resources[rid]
        if rid in limits:
            named.append(f"{d['label']} ({limits[rid]['minimum']}–{limits[rid]['maximum']})")
        elif d["control_kind"] == "absolute":
            named.append(f"{d['label']} ({d['range']['minimum']}–{d['range']['maximum']})")
        else:
            named.append(d["label"])
    proactive = draft["proactive_claims"]
    labels = ", ".join(profile.resources[rid]["label"] for rid in draft["resources"])
    if proactive["allowed"]:
        idle = _seconds(proactive["idle_interval_ms"])
        lines = [f"May claim these controls once each has been idle for {idle}: {', '.join(named)}.",
                 (f"Moves allowed on those claims: {', '.join(draft['action_kinds'])}. Each one lasts at "
                  f"most {_seconds(draft['max_lease_ms'])}."),
                 ("Controls you invite aren't bound by this permission: an invited claim follows each "
                  "control's own range and the host's lease limit.")]
    else:
        lines = [(f"Allows no idle claims on {labels}. The agent uses them only when you invite it, and an "
                  "invited claim follows each control's own range and the host's lease limit, not this "
                  "permission.")]
    lines.append("Every claim is announced on this page before its first move. If this page can't show it in "
                 "time, nothing is sent.")
    if proactive["allowed"]:
        lines.append(f"If a claim is refused because you were using that control, or you deny it or take it "
                     f"back, the agent waits {_seconds(proactive['cooldown_ms'])} before trying that control "
                     "again." if proactive["cooldown_ms"] else
                     "If a claim is refused because you were using that control, or you deny it or take it "
                     "back, the agent may try that control again at once.")
    lines += [(f"Lasts {_duration_text(draft['duration_ms'])} from when you press Issue. Stop agent control "
               "or Revoke ends it at once."),
              ("Ends if the controller or the master route drops or returns, the writer restarts, the "
               "computer sleeps or the host restarts."),
              "If a fader and the software disagree, the agent waits until you settle it.",
              f"The agent's note: {draft['statement']}"]
    return lines


def page_json(value):
    """A copy for the page: every *_ns integer becomes a decimal string (BigInt-safe in the browser)."""
    if isinstance(value, dict):
        return {k: (str(v) if k.endswith("_ns") and type(v) is int else page_json(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [page_json(v) for v in value]
    return value


def _plain(value):
    """A deep JSON copy, so callers can never mutate retained state."""
    return json.loads(canonical_bytes(value))


_OWNER_FIELDS = {"unowned": (), "human": ("hold", "gesture_id", "since_monotonic_ns"),
                 "agent": ("grant_id", "expires_monotonic_ns"),
                 "pickup_pending": ("target_value", "physical_value", "hold"),
                 "unavailable": ("reason", "hold", "gesture_id")}


@dataclass(frozen=True, slots=True)
class ResourceOwner:
    """unowned | human(activity|explicit) | agent | pickup_pending | unavailable.

    Every owner change increments ``generation``; ``cause`` records why. An unavailable owner
    keeps ``hold="explicit"`` when the person held the control, so the hold survives a restore.
    """

    kind: str
    generation: int
    cause: str | None = None
    hold: str | None = None
    gesture_id: str | None = None
    since_monotonic_ns: int | None = None
    grant_id: str | None = None
    expires_monotonic_ns: int | None = None
    target_value: int | None = None
    physical_value: int | None = None
    reason: str | None = None

    def to_json(self) -> dict:
        return {"kind": self.kind, "generation": self.generation, "cause": self.cause,
                **{name: getattr(self, name) for name in _OWNER_FIELDS[self.kind]}}

    @property
    def explicit(self) -> bool:
        return self.kind in ("human", "unavailable", "pickup_pending") and self.hold == "explicit"


@dataclass(frozen=True, slots=True)
class ClaimBasis:
    kind: str  # invitation | standing_permission
    invitation_id: str | None = None
    permission_id: str | None = None
    permission_version: int | None = None
    revocation_generation: int | None = None

    def to_json(self) -> dict:
        if self.kind == "invitation":
            return {"kind": self.kind, "invitation_id": self.invitation_id}
        return {"kind": self.kind, "permission_id": self.permission_id,
                "permission_version": self.permission_version,
                "revocation_generation": self.revocation_generation}


@dataclass(frozen=True, slots=True)
class Notice:
    notice_id: str
    grant_id: str
    session_epoch: str
    resources: tuple  # labels
    reason: str
    text: str
    created_monotonic_ns: int
    deadline_monotonic_ns: int
    action_deadline_monotonic_ns: int
    state: str = "pending"  # pending|acknowledged|deadline_missed|delivery_unknown|denied|cancelled
    fetched_by: tuple = ()  # ui_epoch strings and/or "public"
    acknowledged_monotonic_ns: int | None = None
    ack_ui_epoch: str | None = None
    rejected_acks: int = 0
    late_acks: int = 0

    def to_json(self) -> dict:
        return {"notice_id": self.notice_id, "grant_id": self.grant_id, "session_epoch": self.session_epoch,
                "resources": list(self.resources), "reason": self.reason, "text": self.text,
                "created_monotonic_ns": self.created_monotonic_ns,
                "deadline_monotonic_ns": self.deadline_monotonic_ns,
                "action_deadline_monotonic_ns": self.action_deadline_monotonic_ns, "state": self.state,
                "fetched_by": list(self.fetched_by),
                "acknowledged_monotonic_ns": self.acknowledged_monotonic_ns,
                "ack_ui_epoch": self.ack_ui_epoch, "rejected_acks": self.rejected_acks,
                "late_acks": self.late_acks}


@dataclass(frozen=True, slots=True)
class Grant:
    """An atomic, all-or-none grant. ``grant_id`` is 128 random bits only the broker mints."""

    grant_id: str
    request_id: str
    session_epoch: str
    adapter_epoch: int
    claimed: tuple  # sorted canonical ids at claim time
    resources: tuple  # what the grant still holds
    generations: tuple  # ((resource, generation pinned at grant), ...)
    basis: ClaimBasis
    reason: str
    created_revision: int
    created_monotonic_ns: int
    lease_expires_monotonic_ns: int
    action_deadline_monotonic_ns: int
    notice_id: str
    state: str = "active"  # active | ended
    end_reason: str | None = None
    lost: tuple = ()  # ((resource, cancel reason), ...) in the order the grant lost them

    def generation(self, resource: str) -> int | None:
        return dict(self.generations).get(resource)

    def lost_reason(self, resource: str) -> str | None:
        """Why this grant no longer holds ``resource`` (None while it still does)."""
        if resource in self.resources:
            return None
        return dict(self.lost).get(resource, self.end_reason)

    @property
    def proactive(self) -> bool:
        return self.basis.kind == "standing_permission"

    def authority_json(self) -> dict:
        return {"grant_id": self.grant_id, "resources": list(self.resources), "basis": self.basis.to_json(),
                "lease_expires_monotonic_ns": self.lease_expires_monotonic_ns,
                "action_deadline_monotonic_ns": self.action_deadline_monotonic_ns}

    def to_json(self) -> dict:
        return {**self.authority_json(), "request_id": self.request_id, "session_epoch": self.session_epoch,
                "adapter_epoch": self.adapter_epoch, "claimed": list(self.claimed),
                "generations": dict(self.generations), "reason": self.reason,
                "created_revision": self.created_revision, "created_monotonic_ns": self.created_monotonic_ns,
                "notice_id": self.notice_id, "state": self.state, "end_reason": self.end_reason,
                "lost": [{"resource": r, "reason": why} for r, why in self.lost]}


@dataclass(frozen=True, slots=True)
class Invitation:
    invitation_id: str
    source: str  # gesture | trusted_page
    gesture_id: str | None
    scope: tuple
    adapter_epoch: int
    session_epoch: str
    created_monotonic_ns: int
    expires_monotonic_ns: int
    state: str = "outstanding"  # outstanding | consumed | expired | cancelled
    coalesced_presses: int = 0
    end_reason: str | None = None

    def to_json(self) -> dict:
        return {"invitation_id": self.invitation_id, "source": self.source, "gesture_id": self.gesture_id,
                "resources": list(self.scope), "adapter_epoch": self.adapter_epoch,
                "session_epoch": self.session_epoch, "created_monotonic_ns": self.created_monotonic_ns,
                "expires_monotonic_ns": self.expires_monotonic_ns, "state": self.state,
                "coalesced_presses": self.coalesced_presses, "end_reason": self.end_reason}


@dataclass(frozen=True, slots=True)
class Proposal:
    proposal_id: str
    source: str  # prepare | agent_authorize | reissue
    draft_json: bytes  # canonical JSON of the normalized PermissionDraft
    amend_of: tuple | None  # (permission_id, version, state_sha256)
    sha256: str
    created_monotonic_ns: int
    state: str = "open"  # open | issued | dismissed | withdrawn
    coalesced: int = 0

    @property
    def draft(self) -> dict:
        return json.loads(self.draft_json)

    def to_json(self) -> dict:
        amend = None if self.amend_of is None else dict(zip(("permission_id", "version", "state_sha256"),
                                                            self.amend_of, strict=True))
        return {"schema": PROPOSAL_SCHEMA, "proposal_id": self.proposal_id, "source": self.source,
                "draft": self.draft, "amend_of": amend, "sha256": self.sha256,
                "created_monotonic_ns": self.created_monotonic_ns, "state": self.state,
                "coalesced": self.coalesced}


def _proposal_sha(source: str, draft: dict, amend_of: tuple | None) -> str:
    return digest({"source": source, "draft": draft,
                   "amend_of": None if amend_of is None else list(amend_of)})


@dataclass(frozen=True, slots=True)
class StandingPermission:
    """A person-issued standing permission. Its issuer is the trusted page, never an agent field."""

    permission_id: str
    version: int
    session_id: str
    session_epoch: str
    mapping_sha256: str
    settings_sha256: str
    issuer_host_epoch: str
    issuer_ui_epoch: str
    actor: str  # what the person typed; never authenticates
    statement: str
    proposal_id: str | None
    proposal_sha256: str
    preview_sha256: str
    draft_json: bytes
    issued_monotonic_ns: int
    expires_monotonic_ns: int
    expires_wall_time_utc: str
    revocation_generation: int
    state: str = "active"  # active | revoked | expired | invalidated | superseded
    end_reason: str | None = None

    @property
    def draft(self) -> dict:
        return json.loads(self.draft_json)

    @property
    def resources(self) -> tuple:
        return tuple(self.draft["resources"])

    @property
    def proactive(self) -> dict:
        return self.draft["proactive_claims"]

    def limit(self, resource: str) -> tuple[int, int] | None:
        for item in self.draft.get("limits", []):
            if item["resource"] == resource:
                return item["minimum"], item["maximum"]
        return None

    def body(self) -> dict:
        draft = self.draft
        return {"schema": PERMISSION_SCHEMA, "permission_id": self.permission_id, "version": self.version,
                "session_id": self.session_id, "session_epoch": self.session_epoch,
                "mapping_sha256": self.mapping_sha256, "settings_sha256": self.settings_sha256,
                "issuer_ref": {"kind": "trusted_page", "host_epoch": self.issuer_host_epoch,
                               "ui_epoch": self.issuer_ui_epoch},
                "attribution": {"actor": self.actor, "actor_kind": "human", "statement": self.statement,
                                "verified": False},
                "proposal_id": self.proposal_id, "proposal_sha256": self.proposal_sha256,
                "preview_sha256": self.preview_sha256, "resources": draft["resources"],
                "action_kinds": draft["action_kinds"], "limits": draft.get("limits", []),
                "proactive_claims": draft["proactive_claims"], "max_lease_ms": draft["max_lease_ms"],
                "agent_statement": draft["statement"], "issued_monotonic_ns": self.issued_monotonic_ns,
                "expires_monotonic_ns": self.expires_monotonic_ns,
                "expires_wall_time_utc": self.expires_wall_time_utc, "notice_destination": "trusted_page",
                "revocation_generation": self.revocation_generation, "state": self.state,
                "end_reason": self.end_reason}

    @property
    def state_sha256(self) -> str:
        return digest(self.body())

    def to_json(self) -> dict:
        return {**self.body(), "state_sha256": self.state_sha256}

    def authority_json(self) -> dict:
        return {"permission_id": self.permission_id, "version": self.version,
                "state_sha256": self.state_sha256, "revocation_generation": self.revocation_generation,
                "expires_monotonic_ns": self.expires_monotonic_ns}


@dataclass(frozen=True, slots=True)
class _Request:
    operation: str
    input_sha256: str
    state: str  # running | complete
    receipt_sha256: str | None = None
    journal_offset: int | None = None


@dataclass(frozen=True, slots=True)
class _Reread:
    """A retained receipt evicted from memory: read its journal line after the lock is released."""

    offset: int
    receipt_sha256: str


@dataclass(frozen=True, slots=True, eq=False)
class ActionRun:
    """The handle ``act_begin`` returns; ``act_result`` reads the act's outcome through it.

    An identical retry of an act that already finished carries its retained receipt in
    ``ready`` and runs nothing.
    """

    request_id: str
    ready: dict | None = None


@dataclass(frozen=True, slots=True)
class _Run:
    """One act: its planned adapter messages and how far dispatch got. Replaced on every change."""

    request_id: str
    grant_id: str
    session_epoch: str
    resource: str
    action: dict
    messages: tuple  # one adapter message per planned step
    final: int | str | None  # what a readback must show to verify (None: nothing can verify it)
    readback_kind: str  # the descriptor's readback: numeric | state | commanded_only | none
    not_after_monotonic_ns: int
    max_evidence_age_ms: int
    created_monotonic_ns: int
    state: str = "awaiting_notice"  # awaiting_notice | running | draining | terminal
    t0: int | None = None  # step i is due at t0 + i * step interval, and late one interval after that
    submitted: int = 0
    acknowledged: int = 0
    pending: tuple = ()  # ((submission_id, ack_due_ns), ...)
    last_ack: str | None = None
    last_submit_ns: int | None = None
    drain_until_ns: int | None = None
    evidence_refs: tuple = ()
    mismatch: int | str | None = None
    verified: bool = False
    uncertain_step: int | None = None  # a step whose submit raised: it may or may not have gone out
    receipt: dict | None = None

    @property
    def active(self) -> bool:
        return self.state != "terminal"


def _round_div(numerator: int, denominator: int) -> int:
    """numerator / denominator rounded half away from zero, in exact integers."""
    quotient, remainder = divmod(abs(numerator), denominator)
    rounded = quotient + (1 if 2 * remainder >= denominator else 0)
    return rounded if numerator >= 0 else -rounded


def _valid_readback(readback, descriptor=None) -> bool:
    """A control readback the broker can compare and journal: an int or a state name, a time, its scope.

    With the control's descriptor, the value must also be the kind it declares: an int for a numeric
    readback, one of its states for a state readback.
    """
    if not (isinstance(readback, Readback) and type(readback.observed_monotonic_ns) is int
            and readback.scope == "control_readback"
            and (type(readback.value) is int or isinstance(readback.value, str))):
        return False
    if descriptor is None:
        return True
    if descriptor["readback"] == "numeric":
        return type(readback.value) is int
    return descriptor["readback"] == "state" and readback.value in (descriptor.get("states") or ())


def _valid_submission(submission) -> bool:
    """What submit must return: a Submission whose fields the broker can read without raising."""
    return (isinstance(submission, Submission) and isinstance(submission.submission_id, str)
            and submission.ack in ("ok", "failed", "pending")
            and type(submission.submitted_monotonic_ns) is int
            and (submission.readback is None or _valid_readback(submission.readback)))


class TrustedChannel:
    """The trusted page's identity inside the host process.

    The broker creates exactly one and hands it out once. Methods that can increase authority
    check ``channel is self._trusted``. This separates code paths inside one process; it is not
    a sandbox, and the agent RPC dispatcher never holds a reference to it.
    """

    __slots__ = ()


class Broker:
    """The one local authority broker for a host. Every public method takes the broker lock."""

    def __init__(self, adapter, clock, journal, *, host_epoch: str):
        if not isinstance(host_epoch, str) or not 1 <= len(host_epoch) <= 32:
            raise ValueError("host_epoch is a short string")
        self._profile = validate_profile(adapter.profile)
        self._adapter = adapter
        self._clock = clock
        self._journal = journal
        self.host_epoch = host_epoch
        self._lock = threading.Lock()
        self.changed = threading.Condition(self._lock)
        self._lock_owner = None
        self._trusted = TrustedChannel()
        self._trusted_taken = False
        self._settings = MappingProxyType(normalize_settings({}))
        self._session = None
        self._adapter_epoch = adapter.epoch()
        self._mapping_ok = True
        self._revision = 0
        self._sha = None
        self._owners = {rid: ResourceOwner("unowned", 0) for rid in self._profile.order}
        self._evidence = {rid: {"physical_value": None, "last_input_ns": None, "commanded_value": None,
                                "commanded_grant": None, "readback": None} for rid in self._profile.order}
        self._grants = OrderedDict()  # every grant, ended ones kept as bounded history
        self._live_grants = {}  # ids of active grants, in creation order
        self._notices = OrderedDict()
        self._invitations = OrderedDict()
        self._proposals = OrderedDict()
        self._permissions = []  # every version, oldest first
        self._revocations = 0  # the session revocation counter
        self._cooldowns = {}  # (permission_id, resource) -> until_ns
        self._gestures = {gid: self._idle_gesture() for gid in self._profile.gesture_by_id}
        self._page = {"ui_epoch": None, "visible": False, "last_seen_ns": None, "polls": 0, "reported": False}
        self._last_unknown_ns = None
        # Request tables per channel. "public" holds the reduce-only page routes, which any local process can
        # reach with the served CSRF token, so flooding them can never crowd out the issuer page's requests.
        self._requests = {"agent": {}, "page": {}, "public": {}}
        self._receipts = OrderedDict()  # (channel, request_id) -> receipt, most recent last
        self._events = deque()
        self._event_bytes = 0
        self._event_seq = 0
        self._windows = {}  # coalescing windows: key -> [event, until_ns, journal_count]
        self._seq = itertools.count(1)
        self._proposal_seq = itertools.count(1)
        self._journal_degraded = False
        self._secrets = ()  # host secrets no input may carry: they would otherwise reach the journal
        self._stopped = False  # set by shutdown: nothing more is granted, invited, issued or dispatched
        self._sink_violations = 0  # the action half reads it: a step in flight then has an unknown outcome
        self._counters = {"feedback": 0, "ignored_events": 0, "gesture_bounces": 0, "gesture_duplicates": 0,
                          "ignored_releases": 0, "lost_journal_entries": 0}
        self._heartbeat_ns = None
        self._runs = OrderedDict()  # request_id -> _Run, finished ones kept as bounded history
        self._live_runs = {}  # request_ids of runs not yet terminal, in start order
        self._notify = False
        self._entry_now = clock.now_ns()
        self._last_pair = (self._entry_now, clock.uptime_ns())
        adapter.attach(self, clock)
        self._bindings = {}
        for rid in self._profile.order:
            if self._profile.resources[rid]["binding"]:
                readback = adapter.readback(rid)
                self._bindings[rid] = (readback.value if readback is not None
                                       else self._profile.resources[rid]["states"][0])
        self._sha = digest(self._authority_state())

    @property
    def session_epoch(self) -> str:
        return f"{self.host_epoch}.{self._adapter_epoch}"

    @property
    def profile(self):
        return self._profile

    @property
    def settings(self) -> dict:
        return dict(self._settings)

    @property
    def last_event_sequence(self) -> int:
        """The newest event sequence; a long-poll waits on ``changed`` until it passes a cursor."""
        return self._event_seq

    def trusted_channel(self) -> TrustedChannel:
        with self._lock:
            if self._trusted_taken:
                raise PocketError("The trusted channel was already handed to the host page",
                                  code="invalid_request", hint=_CHANNEL_HINT)
            self._trusted_taken = True
            return self._trusted

    @contextmanager
    def _entry(self):
        with self._lock:
            self._lock_owner = threading.get_ident()
            self._notify = False
            try:
                now = self._clock.now_ns()
                self._entry_now = now
                self._advance(now)
                yield now
            finally:
                if self._notify:
                    self.changed.notify_all()
                self._lock_owner = None

    def _advance(self, now: int) -> None:
        """Run everything due by ``now`` in a fixed order, before the caller's own work.

        A fault here (a timer, an epoch renewal) fails closed before it propagates, so the same due
        item can't wedge every later entry, Stop included.
        """
        try:
            self._advance_due(now)
        except Exception as fault:
            self._after_fault(fault, self._fail_closed)
            raise

    def _advance_due(self, now: int) -> None:
        fired = False
        uptime = self._clock.uptime_ns()
        gap = suspend_gap_ns(self._last_pair, (now, uptime))
        self._last_pair = (now, uptime)
        if gap > self._settings["suspend_threshold_ms"] * MS:
            # A suspend: deadline time moved on without uptime. Every lease lapsed; every token is stale.
            self._new_epoch(now, self._adapter.renew_epoch("clock_discontinuity"), "clock_discontinuity")
            fired = True
        if self._journal.state != "ok" and not self._journal_degraded:
            self._degrade(now)
            fired = True
        fired = self._run_timers(now) or fired
        if fired:
            self._commit("timers")

    def _authority_state(self) -> dict:
        owners = self._owners
        return {"schema": AUTHORITY_SCHEMA, "session_epoch": self.session_epoch,
                "adapter_epoch": self._adapter_epoch, "mapping_sha256": self._profile.sha256,
                "bindings": dict(sorted(self._bindings.items())),
                "grants": [g.authority_json()
                           for g in sorted(self._active_grants(), key=lambda g: g.grant_id)],
                "holds": sorted(rid for rid, o in owners.items() if o.explicit),
                "pickup": [{"resource": rid, "target_value": o.target_value}
                           for rid, o in sorted(owners.items()) if o.kind == "pickup_pending"],
                "unavailable": [{"resource": rid, "reason": o.reason}
                                for rid, o in sorted(owners.items()) if o.kind == "unavailable"],
                "permissions": [p.authority_json() for p in self._permissions if p.state == "active"],
                "invitations": [{"invitation_id": i.invitation_id, "resources": list(i.scope),
                                 "expires_monotonic_ns": i.expires_monotonic_ns}
                                for i in sorted(self._outstanding_invitations(),
                                                key=lambda i: i.invitation_id)]}

    def _commit(self, cause: str) -> None:
        """The one place the revision advances: exactly when the authority-state SHA changes."""
        if self._journal.state != "ok" and not self._journal_degraded:
            self._degrade(self._entry_now)
        sha = digest(self._authority_state())
        if sha == self._sha:
            return
        self._sha = sha
        self._revision += 1
        self._notify = True
        self._log("authority_changed", {"revision": self._revision, "state_sha256": sha, "cause": cause})
        if self._journal.state != "ok" and not self._journal_degraded:
            self._degrade(self._entry_now)
            self._commit(cause)

    def _log(self, kind: str, data: dict):
        """Append under the lock; a full or failed journal degrades at the next commit instead of raising."""
        try:
            return self._journal.append(kind, data, session_epoch=self.session_epoch,
                                        monotonic_ns=self._entry_now)
        except JournalUnavailable:
            self._counters["lost_journal_entries"] += 1
            return None

    def _emit(self, event_type: str, **fields) -> dict:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"Unknown b2b event type {event_type}")
        self._event_seq += 1
        event = {"sequence": self._event_seq, "monotonic_ns": self._entry_now,
                 "session_epoch": self.session_epoch, "control_revision": self._revision, "type": event_type,
                 **fields}
        size = len(json.dumps(event, ensure_ascii=True, allow_nan=False, separators=(",", ":")))  # wire bytes
        self._events.append([event, size])
        self._event_bytes += size
        while len(self._events) > MAX_EVENTS or self._event_bytes > MAX_EVENT_BYTES:
            _, dropped = self._events.popleft()
            self._event_bytes -= dropped
        self._notify = True
        return event

    def _coalesced(self, key, event_type: str, now: int, journal_kind: str | None, journal_data: dict,
                   window_ns: int = COALESCE_NS, **fields) -> None:
        """Emit the first event of a window at once; count later ones on it (bounded noise)."""
        window = self._windows.get(key)
        if window is not None and now < window[1]:
            window[0]["count"] += 1
            window[2] += 1
            return
        previous = 0 if window is None else window[2]
        event = self._emit(event_type, count=1, **fields)
        self._windows[key] = [event, now + window_ns, 1]
        if journal_kind is not None:
            self._log(journal_kind, {**journal_data, "count": 1, "previous_window_count": previous})
        if len(self._windows) > 4096:
            for stale in [k for k, w in self._windows.items() if w[1] <= now]:
                del self._windows[stale]

    def _run_timers(self, now: int) -> bool:
        due = []
        for i, p in enumerate(self._permissions):
            if p.state == "active" and p.expires_monotonic_ns <= now:
                due.append((p.expires_monotonic_ns, 0, i, "permission", (p.permission_id, p.version)))
        for i, inv in enumerate(self._invitations.values()):
            if inv.state == "outstanding" and inv.expires_monotonic_ns <= now:
                due.append((inv.expires_monotonic_ns, 1, i, "invitation", inv.invitation_id))
        live = self._active_grants()
        for i, g in enumerate(live):
            if g.lease_expires_monotonic_ns <= now:
                due.append((g.lease_expires_monotonic_ns, 2, i, "lease", g.grant_id))
        # A pending notice always belongs to an active grant: ending a grant cancels its pending notice.
        for i, g in enumerate(live):
            n = self._notices.get(g.notice_id)
            if n is not None and n.state == "pending" and n.deadline_monotonic_ns <= now:
                due.append((n.deadline_monotonic_ns, 3, i, "notice", n.notice_id))
        for i, (gid, gs) in enumerate(self._gestures.items()):
            if gs["state"] == "pressed":
                reset = gs["t0"] + self._profile.gesture_by_id[gid]["max_hold_ms"] * MS + 1
                if reset <= now:
                    due.append((reset, 4, i, "gesture", gid))
        release_ns = self._settings["activity_release_ms"] * MS
        for i, (rid, o) in enumerate(self._owners.items()):
            if o.kind == "human" and o.hold == "activity" and o.since_monotonic_ns + release_ns <= now:
                due.append((o.since_monotonic_ns + release_ns, 5, i, "activity", rid))
        due += self._action_timers(now)  # category 7: the action half's adapter ack timeouts
        for i, (key, until) in enumerate(self._cooldowns.items()):
            if until <= now:
                due.append((until, 8, i, "cooldown", key))
        page = self._page
        if page["ui_epoch"] is not None and not page["reported"] and page["polls"] == 0:
            gone = page["last_seen_ns"] + self._settings["trusted_page_timeout_ms"] * MS
            if gone <= now:
                due.append((gone, 9, 0, "page", page["ui_epoch"]))
        for at, _, _, kind, key in sorted(due, key=lambda item: item[:3]):
            self._fire(kind, key, at, now)
        return bool(due)

    def _fire(self, kind: str, key, at: int, now: int) -> None:
        if kind == "permission":
            p = self._active_permission()
            if p is not None and (p.permission_id, p.version) == key:
                self._end_permission(p, "expired", "expired")
        elif kind == "invitation":
            inv = self._invitations.get(key)
            if inv is not None and inv.state == "outstanding":
                self._end_invitation(inv, "expired", "expired")
        elif kind == "lease":
            g = self._grants.get(key)
            if g is not None and g.state == "active":
                self._end_grant(g, "lease_expired")
        elif kind == "notice":
            n = self._notices.get(key)
            if n is not None and n.state == "pending":
                self._fail_notice(n, at)
        elif kind == "gesture":
            gs = self._gestures[key]
            if gs["state"] == "pressed":
                self._gestures[key] = {**self._idle_gesture(), "last_transition_ns": at}
                self._emit("release_lost", gesture_id=key)
                self._log("gesture", {"gesture_id": key, "outcome": "release_lost"})
        elif kind == "activity":
            o = self._owners[key]
            if o.kind == "human" and o.hold == "activity":
                self._set_owner(key, "unowned", "activity_released")
        elif kind == "cooldown":
            self._cooldowns.pop(key, None)
        elif kind == "page":
            if self._page["ui_epoch"] == key and not self._page["reported"]:
                self._page["reported"] = True
                self._emit("page_detached", ui_epoch=key, reason="not_seen")
        else:
            self._fire_action(kind, key, at, now)

    def _set_owner(self, rid: str, kind: str, cause: str, **fields) -> ResourceOwner:
        owner = ResourceOwner(kind, self._owners[rid].generation + 1, cause, **fields)
        self._owners[rid] = owner
        return owner

    def _active_grants(self) -> list[Grant]:
        return [self._grants[grant_id] for grant_id in self._live_grants]

    def _outstanding_invitations(self) -> list[Invitation]:
        return [i for i in self._invitations.values() if i.state == "outstanding"]

    def _active_permission(self) -> StandingPermission | None:
        return next((p for p in reversed(self._permissions) if p.state == "active"), None)

    def _store_grant(self, grant: Grant) -> None:
        self._grants[grant.grant_id] = grant
        if grant.state == "active":
            self._live_grants[grant.grant_id] = None  # an existing key keeps its place
        else:
            self._live_grants.pop(grant.grant_id, None)
        if len(self._grants) > MAX_GRANT_HISTORY:
            for gid in [gid for gid, g in self._grants.items() if g.state != "active"][:16]:
                del self._grants[gid]

    def _needs_pickup(self, rid: str, grant: Grant) -> tuple[int | None, int | None] | None:
        """(target, physical) when a released absolute control may no longer match its fader.

        Only a control this grant actually moved can disagree: the agent changed the software
        value, not the fader. One the grant never moved goes straight back to unowned.
        """
        d = self._profile.resources[rid]
        evidence = self._evidence[rid]
        if d["control_kind"] != "absolute" or d["motorized"] or evidence["commanded_grant"] != grant.grant_id:
            return None
        readback = self._read_back(rid) if d["readback"] == "numeric" else None
        software = readback.value if readback is not None else evidence["commanded_value"]
        physical = evidence["physical_value"]
        if software is None or physical is None or abs(software - physical) > PICKUP_TOLERANCE:
            return software, physical
        return None

    def _lose(self, grant: Grant, resources, reason: str, *, release_owners: bool, takeover: bool = False
              ) -> Grant:
        """Narrow a grant, or end it when nothing is left. Undispatched steps on those controls cancel."""
        lost = tuple(r for r in grant.resources if r in resources)
        if not lost:
            return grant
        remaining = tuple(r for r in grant.resources if r not in lost)
        if release_owners:
            for rid in lost:
                owner = self._owners[rid]
                if owner.kind == "agent" and owner.grant_id == grant.grant_id:
                    pickup = self._needs_pickup(rid, grant)
                    if pickup is None:
                        self._set_owner(rid, "unowned", reason)
                    else:
                        self._set_owner(rid, "pickup_pending", reason, target_value=pickup[0],
                                        physical_value=pickup[1])
                        self._emit("pickup_pending", resource=rid, target_value=pickup[0],
                                   physical_value=pickup[1])
                        self._log("pickup", {"resource": rid, "state": "pending", "target_value": pickup[0],
                                             "physical_value": pickup[1]})
        updated = replace(grant, resources=remaining, lost=grant.lost + tuple((r, reason) for r in lost),
                          **({} if remaining else {"state": "ended", "end_reason": reason}))
        self._store_grant(updated)
        if not remaining:
            notice = self._notices.get(grant.notice_id)
            if notice is not None and notice.state == "pending":
                self._notices[notice.notice_id] = replace(notice, state="cancelled")
        if takeover and grant.proactive and reason in _TAKEOVER_REASONS:
            self._start_cooldown(grant.basis.permission_id, lost, self._entry_now)
        self._emit("grant_ended", grant_id=grant.grant_id, resources=list(lost), remaining=list(remaining),
                   reason=reason)
        self._log("grant_ended", {"grant_id": grant.grant_id, "resources": list(lost),
                                  "remaining": list(remaining), "reason": reason})
        self._on_grant_lost(updated, lost, reason, self._entry_now)
        return updated

    def _end_grant(self, grant: Grant, reason: str) -> Grant:
        return self._lose(grant, grant.resources, reason, release_owners=True)

    def _end_all_grants(self, reason: str) -> None:
        for grant in self._active_grants():
            self._end_grant(grant, reason)

    def _take_back(self, resources, cause: str, now: int, settles=()) -> None:
        """Human priority: the touched controls become the person's before the agent's next step.

        Only a touch on the control itself settles its pickup (``settles``). Input anywhere else leaves
        the pickup waiting, so a fader that disagrees with the software never becomes claimable unseen.
        """
        by_grant = {}
        for rid in resources:
            owner = self._owners[rid]
            if owner.kind == "agent":
                by_grant.setdefault(owner.grant_id, []).append(rid)
        for gid, rids in by_grant.items():
            self._lose(self._grants[gid], rids, cause, release_owners=False, takeover=True)
        for rid in resources:
            owner = self._owners[rid]
            if owner.kind == "pickup_pending" and rid not in settles:
                continue
            if owner.kind == "pickup_pending" and owner.explicit:
                # The touch settles the pickup; the person's hold on it stays.
                self._set_owner(rid, "human", cause, hold="explicit", gesture_id=owner.gesture_id,
                                since_monotonic_ns=now)
                continue
            if owner.kind == "unavailable" or owner.explicit:
                continue
            if owner.kind == "human":
                self._owners[rid] = replace(owner, since_monotonic_ns=now)  # still the person's; no new owner
            else:
                self._set_owner(rid, "human", cause, hold="activity", gesture_id=f"input-{next(self._seq)}",
                                since_monotonic_ns=now)

    def _start_cooldown(self, permission_id, resources, now: int) -> None:
        permission = self._active_permission()
        if permission is None or permission.permission_id != permission_id:
            return
        cooldown = permission.proactive["cooldown_ms"] * MS
        if cooldown <= 0:
            return
        for rid in resources:
            # A refusal during a cooldown never extends it.
            if self._cooldowns.get((permission_id, rid), 0) <= now:
                self._cooldowns[(permission_id, rid)] = now + cooldown

    def _end_permission(self, permission: StandingPermission, state: str, end_reason: str,
                        cancel_reason: str = "permission_ended") -> None:
        ended = replace(permission, state=state, end_reason=end_reason)
        self._permissions[self._permissions.index(permission)] = ended
        for grant in self._active_grants():
            if grant.proactive and grant.basis.permission_id == permission.permission_id:
                self._end_grant(grant, cancel_reason)
        if state != "superseded":
            # A narrowing amendment keeps the same permission_id, so its cooldowns stay in force.
            for key in [k for k in self._cooldowns if k[0] == permission.permission_id]:
                del self._cooldowns[key]
        self._emit("permission_ended", permission_id=permission.permission_id, version=permission.version,
                   state=state, reason=end_reason)
        self._log("permission", {"permission_id": permission.permission_id, "version": permission.version,
                                 "state": state, "end_reason": end_reason,
                                 "state_sha256": ended.state_sha256})

    def _store_permission(self, permission: StandingPermission) -> None:
        self._permissions.append(permission)
        while len(self._permissions) > MAX_PERMISSION_HISTORY:
            oldest = next((p for p in self._permissions if p.state != "active"), None)
            if oldest is None:
                break
            self._permissions.remove(oldest)

    def _end_invitation(self, invitation: Invitation, state: str, reason: str, grant_id: str | None = None
                        ) -> None:
        """Consumed, expired or cancelled. Ended records are trimmed, so a long session stays bounded."""
        self._invitations[invitation.invitation_id] = replace(invitation, state=state, end_reason=reason)
        self._emit("invitation_ended", invitation_id=invitation.invitation_id, state=state, reason=reason)
        self._log("invitation", {"invitation_id": invitation.invitation_id, "state": state, "reason": reason,
                                 **({} if grant_id is None else {"grant_id": grant_id})})
        ended = [i for i, inv in self._invitations.items() if inv.state != "outstanding"]
        for iid in ended[:max(0, len(ended) - MAX_ENDED_RECORDS)]:
            del self._invitations[iid]

    def _make_invitation(self, source: str, gesture_id: str | None, scope: tuple, now: int) -> Invitation:
        # Handing controls back is the person's choice, so a human owner (a hold included) lets go of them.
        for rid in scope:
            owner = self._owners[rid]
            if owner.kind == "human":
                self._set_owner(rid, "unowned", "invited")
            elif owner.kind == "pickup_pending" and owner.explicit:
                self._owners[rid] = replace(owner, hold=None, gesture_id=None)  # still waits for pickup
        invitation = Invitation(f"i-{secrets.token_hex(8)}", source, gesture_id, tuple(sorted(scope)),
                                self._adapter_epoch, self.session_epoch, now,
                                now + self._settings["invitation_ttl_ms"] * MS)
        self._invitations[invitation.invitation_id] = invitation
        self._emit("invitation_created", invitation=invitation.to_json())
        self._log("invitation", {"invitation": invitation.to_json()})
        return invitation

    def _clear_authority(self, cancel_reason: str, permission_state: str, *, invitations: bool = True,
                         gestures: bool = True) -> None:
        """End every grant, outstanding invitation, gesture and cooldown, and the active permission."""
        self._end_all_grants(cancel_reason)
        if invitations:
            for invitation in self._outstanding_invitations():
                self._end_invitation(invitation, "cancelled", cancel_reason)
        if gestures:
            self._gestures = {gid: self._idle_gesture() for gid in self._gestures}
        self._cooldowns.clear()
        permission = self._active_permission()
        if permission is not None:
            self._end_permission(permission, permission_state, cancel_reason)
        self._revocations += 1

    def _dependency_reason(self, name: str) -> str:
        return "controller_disconnected" if name == "controller" else f"route_lost:{name.split(':', 1)[-1]}"

    def _mark_unavailable(self, resources, reason: str) -> None:
        for rid in resources:
            owner = self._owners[rid]
            if owner.kind == "unavailable" and owner.reason == reason:
                continue
            explicit = owner.explicit
            self._set_owner(rid, "unavailable", reason, reason=reason, hold="explicit" if explicit else None,
                            gesture_id=owner.gesture_id if explicit else None)

    def _new_epoch(self, now: int, adapter_epoch: int, reason: str, dependencies: dict | None = None) -> None:
        """A new adapter epoch: nothing carries over except a person's explicit holds. No resume."""
        cancel = "clock_discontinuity" if reason == "clock_discontinuity" else "epoch_changed"
        self._clear_authority(cancel, "invalidated")
        old = self.session_epoch
        self._adapter_epoch = adapter_epoch
        if dependencies is None:
            health = self._health()
            dependencies = {} if health is None else health[0]  # unreadable health: nothing is available
        restored = []
        for rid in self._profile.order:
            owner = self._owners[rid]
            down = [n for n in self._profile.resources[rid]["depends_on"]
                    if dependencies.get(n) != "available"]
            if not self._mapping_ok:
                self._mark_unavailable([rid], "mapping_changed")
            elif down:
                self._mark_unavailable([rid], self._dependency_reason(down[0]))
            elif owner.explicit:
                if owner.kind == "unavailable":
                    restored.append(rid)
                self._set_owner(rid, "human", reason, hold="explicit", gesture_id=owner.gesture_id,
                                since_monotonic_ns=now)
            else:
                if owner.kind == "unavailable":
                    restored.append(rid)
                self._set_owner(rid, "unowned", reason)
            evidence = self._evidence[rid]
            evidence.update(physical_value=None, commanded_value=None, commanded_grant=None)
        self._emit("epoch_changed", previous_session_epoch=old, adapter_epoch=adapter_epoch, reason=reason)
        self._log("epoch_changed", {"previous_session_epoch": old, "adapter_epoch": adapter_epoch,
                                    "reason": reason})
        if restored:
            self._emit("resource_available", resources=restored)

    def _degrade(self, now: int) -> None:
        """The journal is full or failed: agent authority ends; human priority keeps working in memory."""
        self._journal_degraded = True
        self._end_all_grants("journal_unavailable")
        for invitation in self._outstanding_invitations():
            self._end_invitation(invitation, "cancelled", "journal_unavailable")
        permission = self._active_permission()
        if permission is not None:
            self._end_permission(permission, "invalidated", "journal_unavailable")
        self._revocations += 1
        self._emit("journal_degraded", state=self._journal.state)

    def _page_ready(self, at: int) -> bool:
        """A token-holding page attached and visible, seen within the page timeout or long-polling."""
        page = self._page
        if page["ui_epoch"] is None or not page["visible"] or page["last_seen_ns"] is None:
            return False
        timeout = self._settings["trusted_page_timeout_ms"] * MS
        return page["polls"] > 0 or at - page["last_seen_ns"] <= timeout

    def _fail_notice(self, notice: Notice, deadline: int) -> None:
        """No valid ack by the deadline: end the grant before anything was sent. Never a late move."""
        state = "deadline_missed" if notice.fetched_by and self._page_ready(deadline) else "delivery_unknown"
        self._notices[notice.notice_id] = replace(notice, state=state)
        self._emit("notice_failed", notice_id=notice.notice_id, grant_id=notice.grant_id, state=state)
        self._log("notice_failed", {"notice_id": notice.notice_id, "grant_id": notice.grant_id,
                                    "state": state})
        grant = self._grants.get(notice.grant_id)
        if grant is not None and grant.state == "active":
            self._end_grant(grant, f"notice_{state}")

    def _mark_fetched(self, fetcher: str) -> None:
        for notice in list(self._notices.values()):
            if notice.state == "pending" and fetcher not in notice.fetched_by:
                self._notices[notice.notice_id] = replace(notice, fetched_by=notice.fetched_by + (fetcher,))
                self._log("notice_fetched", {"notice_id": notice.notice_id, "fetcher": fetcher})

    def _store_notice(self, notice: Notice) -> None:
        self._notices[notice.notice_id] = notice
        ended = [nid for nid, n in self._notices.items() if n.state != "pending"]
        for nid in ended[:max(0, len(self._notices) - MAX_NOTICES)]:
            del self._notices[nid]

    def forbid_inputs(self, *secrets: str) -> None:
        """Refuse, before anything is recorded, any request that carries one of these host secrets.

        The agent can read the nonce and fetch the public CSRF token, so it could echo either
        back as a request_id or a reason; the journal must never hold them.
        """
        if any(not isinstance(value, str) or len(value) < 16 for value in secrets):
            raise ValueError("Forbid only secrets of at least 16 characters")
        with self._lock:
            self._secrets = (*self._secrets, *(value.encode() for value in secrets))

    def _check_secrets(self, *values) -> None:
        if self._secrets:
            data = canonical_bytes(list(values))
            if any(secret in data for secret in self._secrets):
                raise _bad("This request carries a b2b host secret",
                           "Never pass the agent nonce or a page token as a b2b value; use your own "
                           "request_id and text.")

    def _lookup(self, channel: str, operation: str, session_id, request_id: str, inputs: dict):
        """Step 6: an identical retry gets the retained outcome and runs nothing."""
        self._check_secrets(request_id, inputs)
        sha = digest({"operation": operation, "session_id": session_id, "inputs": inputs})
        entry = self._requests[channel].get(request_id)
        if entry is None:
            return None, sha
        if entry.operation != operation or entry.input_sha256 != sha:
            raise PocketError("This request_id was already used with different inputs",
                              code="idempotency_conflict",
                              hint="Use a new request_id for changed inputs, or call b2b_request_status with "
                                   "this request_id to see the earlier request.")
        if entry.state == "running":
            raise PocketError("The earlier request with this request_id is still running",
                              code="request_not_complete", hint=_STATUS_HINT)
        cached = self._receipts.get((channel, request_id))
        if cached is not None:
            self._receipts.move_to_end((channel, request_id))
            return {**_plain(cached), "replayed": True}, sha
        if entry.journal_offset is None:
            raise PocketError("The retained receipt for this request_id can't be read",
                              code="request_not_complete", hint=_STATUS_HINT)
        return _Reread(entry.journal_offset, entry.receipt_sha256), sha

    def _reread(self, hit: _Reread) -> dict:
        """Read an evicted receipt's journal line outside the lock and verify it; never re-execute."""
        entry = read_line(self._journal.path, hit.offset)
        retained = None if entry is None else entry["data"].get("receipt")
        if (entry is None or entry["kind"] != "request_completed" or not isinstance(retained, dict)
                or digest(retained) != hit.receipt_sha256):
            raise PocketError("The retained receipt for this request_id can't be read",
                              code="request_not_complete", hint=_STATUS_HINT)
        return {**retained, "replayed": True}

    def _check_triple(self, triple, mode: str) -> tuple[str, bool | None]:
        """Steps 7 and 8. ``exact`` for increases; ``epoch`` for agent reductions; ``record`` never blocks."""
        if triple is None:
            return "not_required", None
        current = triple[1] == self._revision and triple[2] == self._sha
        if mode == "record":
            return "not_required", current and triple[0] == self.session_epoch
        if triple[0] != self.session_epoch:
            raise PocketError("expected_session_epoch is not the current session epoch",
                              code="stale_revision", hint=_STALE_HINT)
        if mode == "epoch":
            return "not_required", current
        if not current:
            raise PocketError("expected_control_revision or expected_state_sha256 is not current",
                              code="stale_revision", hint=_STALE_HINT)
        return "exact", None

    def _make_room(self, channel: str, *, reduction: bool) -> None:
        """Keep a request table bounded. On the public table, a reduction (Stop above all) is never
        refused for lack of room: it forgets the oldest finished request, whose replay then can't be
        guaranteed. A full agent or page table refuses instead."""
        table = self._requests[channel]
        if len(table) < MAX_REQUESTS:
            return
        # Only the public table forgets: its requests are reductions any local process can send, while an
        # agent or page request_id must keep its replay and its status for the host's lifetime.
        finished = (rid for rid, entry in table.items() if entry.state == "complete")
        oldest = next(finished, None) if reduction and channel == "public" else None
        if oldest is None:
            raise PocketError("This b2b host has kept its maximum number of requests",
                              code="invalid_request",
                              hint="This b2b host has kept its maximum number of requests; ask the person "
                                   "to stop it and start a new host. Stop agent control still works.")
        del table[oldest]
        self._receipts.pop((channel, oldest), None)
        self._counters["forgotten_requests"] = self._counters.get("forgotten_requests", 0) + 1

    def _begin_request(self, channel: str, request_id: str, operation: str, sha: str, *,
                       reduction: bool = False) -> None:
        self._make_room(channel, reduction=reduction)
        table = self._requests[channel]
        table[request_id] = _Request(operation, sha, "running")
        self._log("request_started", {"request_id": request_id, "channel": channel, "operation": operation,
                                      "input_sha256": sha})

    def _complete_request(self, channel: str, request_id: str, result: dict) -> None:
        sha = digest(result)
        ref = self._log("request_completed", {"request_id": request_id, "channel": channel,
                                              "receipt_sha256": sha, "receipt": result})
        entry = self._requests[channel].get(request_id)
        if entry is not None:
            self._requests[channel][request_id] = replace(entry, state="complete", receipt_sha256=sha,
                                                          journal_offset=None if ref is None else ref.offset)
        self._retain_receipt(channel, request_id, result)

    def _retain_receipt(self, channel: str, request_id: str, result: dict) -> None:
        self._receipts[(channel, request_id)] = result
        self._receipts.move_to_end((channel, request_id))
        while len(self._receipts) > RECEIPT_CACHE:
            self._receipts.popitem(last=False)

    def _receipt(self, operation: str, request_id: str, result: dict, now: int, revision_check: str,
                 expected_current: bool | None) -> dict:
        outcome = result["outcome"]
        kind = outcome["kind"]
        if kind == "refused":
            status = "needs_input" if outcome["code"] in NEEDS_INPUT_CODES else "conflict"
        else:
            status = {"cancelled_before_dispatch": "cancelled", "partially_dispatched": "partial",
                      "outcome_unknown": "outcome_unknown"}.get(kind, "ok")
            if status == "ok" and kind not in _OK_KINDS:
                raise ValueError(f"No receipt status for outcome {kind}")
        key = outcome.get("code") or outcome.get("reason") or kind
        hint = NEXT_ACTIONS.get(key) or NEXT_ACTIONS.get(kind)
        if hint is not None and "{proposal_id}" in hint:
            hint = hint.format(proposal_id="(see b2b_observe proposals)")
        next_actions = list(result.get("next_actions") or ([hint] if hint else []))
        extra = {"provenance": dict(PROVENANCE), "operation": operation, "replayed": False,
                 "outcome": outcome, "session_epoch": self.session_epoch, "control_revision": self._revision,
                 "state_sha256": self._sha, "revision_check": revision_check, "monotonic_ns": now,
                 **result.get("fields", {})}
        if expected_current is not None:
            extra["expected_revision_current"] = expected_current
        made = receipt(request_id, status, change_summary={"operation": operation, "outcome": kind},
                       coverage=dict(COVERAGE), warnings=list(result.get("warnings", [])),
                       uncertainty=list(result.get("uncertainty", [])), next_actions=next_actions, **extra)
        if len(canonical_bytes(made)) > MAX_RECEIPT_BYTES:
            raise ValueError("A b2b receipt exceeded its 64 KiB bound")
        return made

    def _mutate(self, channel: str, operation: str, session_id, request: dict, inputs: dict, triple,
                mode: str, evaluate, *, increase: bool, precheck=None):
        """The request handling order (steps 5-10) for one mutation, already shape-checked."""
        request_id = request["request_id"]
        with self._entry() as now:
            if channel == "agent":
                self._check_session(session_id)
            hit, sha = self._lookup(channel, operation, session_id, request_id, inputs)
            if hit is None:
                revision_check, current = self._check_triple(triple, mode)
                if precheck is not None:
                    precheck(now)
                if increase:
                    self._check_running()
                if increase and self._journal_degraded:
                    return self._unjournaled_refusal(channel, operation, request_id, sha, now, revision_check,
                                                     current)
                self._begin_request(channel, request_id, operation, sha, reduction=not increase)
                degraded = self._journal_degraded
                result = self._evaluate(channel, operation, request_id, now, evaluate)
                self._commit(operation)
                if (increase and not degraded and self._journal_degraded
                        and result["outcome"]["kind"] in ("granted", "invited", "issued", "amended")):
                    # The journal failed while recording this; the degrade transaction already ended it.
                    result = self._refused("journal_unavailable",
                                           "The journal failed while recording this request")
                made = self._receipt(operation, request_id, result, now, revision_check, current)
                self._complete_request(channel, request_id, made)
                return made
            if isinstance(hit, dict):
                return hit
        return self._reread(hit)

    def _evaluate(self, channel: str, operation: str, request_id: str, now: int, evaluate) -> dict:
        """Run one request's domain evaluation. Nothing raised here may leave the request running.

        Input is validated before the request is recorded, so anything raised here is a host fault
        that may come after a step went out: agent authority ends, the request is retained as
        outcome_unknown, and the fault still propagates so the host reports it.
        """
        try:
            return evaluate(now)
        except Exception as fault:
            def record():
                self._fail_closed()
                entry = self._requests[channel].get(request_id)
                if entry is not None and entry.state == "complete":
                    return  # the request's own receipt was recorded before the fault
                lost = "The b2b host failed while handling this request; agent authority ended."
                result = {"outcome": {"kind": "outcome_unknown", "request_id": request_id,
                                      "automatic_retry": False}, "warnings": [lost]}
                made = self._receipt(operation, request_id, result, now, "not_required", None)
                self._complete_request(channel, request_id, made)

            self._after_fault(fault, record)
            raise

    @staticmethod
    def _after_fault(fault: Exception, record) -> None:
        """Run the fail-closed record; if it fails too, say so on the original fault, never silently."""
        try:
            record()
        except Exception as also:  # noqa: BLE001 - the original fault propagates, carrying this one
            fault.add_note(f"Failing closed after this fault also raised {type(also).__name__}: {also}")

    def _fail_closed(self) -> None:
        """After a host fault, all agent authority ends, as when the journal degrades, and the state commits.

        Runs go first, so none reads as merely cancelled or partial. A run whose own record fails is
        forced terminal, and the grants, invitations and permission end whatever happened before.
        """
        try:
            for run in self._active_runs():
                try:
                    self._unknown(run, self._entry_now, "The b2b host failed while this act was running")
                except Exception:  # noqa: BLE001 - the fault that brought us here is the one reported
                    self._force_unknown(run)
        finally:
            self._clear_authority("outcome_unknown", "invalidated")
            self._commit("host_fault")

    def _force_unknown(self, run: _Run) -> None:
        """Recording this run failed while failing closed: terminal, with the smallest honest receipt."""
        self._live_runs.pop(run.request_id, None)  # first, so ending its grant can't touch it again
        entry = self._requests["agent"].get(run.request_id)
        if entry is not None and entry.state == "complete":
            return  # its receipt was recorded before the fault
        outcome = {"kind": "outcome_unknown", "request_id": run.request_id, "automatic_retry": False}
        lost = ["The b2b host failed while this act was running; its full record could not be kept."]
        try:
            made = self._receipt("act", run.request_id, {"outcome": outcome, "warnings": lost},
                                 self._entry_now, "exact", None)
        except Exception:  # noqa: BLE001 - a plain record still says what is known
            made = {"request_id": run.request_id, "status": "outcome_unknown", "outcome": outcome,
                    "warnings": lost}
        self._runs[run.request_id] = replace(run, state="terminal", receipt=made)
        self._complete_request("agent", run.request_id, made)

    def _check_running(self) -> None:
        if self._stopped:
            raise PocketError("This b2b host is stopping", code="io_error",
                              hint="Start a new host and prepare a new session; nothing carries over.")

    def _unjournaled_refusal(self, channel, operation, request_id, sha, now, revision_check, current) -> dict:
        """Step 9: an increase while the journal is unavailable, retained in memory only."""
        result = {"outcome": {"kind": "refused", "code": "journal_unavailable",
                              "detail": f"The journal is {self._journal.state}"}}
        made = self._receipt(operation, request_id, result, now, revision_check, current)
        self._make_room(channel, reduction=False)
        self._requests[channel][request_id] = _Request(operation, sha, "complete", digest(made))
        self._retain_receipt(channel, request_id, made)
        return made

    def _refused(self, code: str, detail: str, **fields) -> dict:
        if code not in REFUSAL_CODES:
            raise ValueError(f"Unknown b2b refusal code {code}")
        return {"outcome": {"kind": "refused", "code": code, "detail": detail}, "fields": fields}

    def _resolve(self, names, what: str = "resources", *, whole_groups: bool = True, allow_empty: bool = False
                 ) -> tuple[str, ...]:
        resolved = self._profile.resolve(names)
        if not (0 if allow_empty else 1) <= len(resolved) <= MAX_CLAIM_RESOURCES:
            raise _profile_error(f"{what} must name 1 to {MAX_CLAIM_RESOURCES} declared resources")
        missing = self._profile.conflict_missing(resolved) if whole_groups else ()
        if missing:
            shown = ", ".join(missing[:4]) + (", ..." if len(missing) > 4 else "")
            raise _profile_error(f"{what} name part of a conflict group",
                                 f"A conflict group is claimed whole; also name {shown}.")
        return tuple(sorted(resolved))

    def _draft(self, value, settings) -> dict:
        _fields(value, ("resources", "action_kinds", "proactive_claims", "max_lease_ms", "duration_ms",
                        "statement"), ("limits",), "permission")
        resources = self._resolve(value["resources"], "permission resources")
        kinds = value["action_kinds"]
        if (not isinstance(kinds, list) or not 1 <= len(kinds) <= len(ACTION_KINDS)
                or any(k not in ACTION_KINDS for k in kinds) or len(set(kinds)) != len(kinds)):
            raise _bad("action_kinds lists distinct kinds from set, ramp, nudge and set_state",
                       "Pass action_kinds as a list such as [\"set\", \"ramp\"].")
        limits = value.get("limits", [])
        if not isinstance(limits, list) or len(limits) > MAX_CLAIM_RESOURCES:
            raise _bad("limits is a list of at most 16 {resource, minimum, maximum} objects")
        normalized = {}
        for item in limits:
            _fields(item, ("resource", "minimum", "maximum"), where="limit")
            rid = self._profile.resolve([item["resource"]])[0]
            low = _int(item["minimum"], "limit.minimum", 0, 127)
            high = _int(item["maximum"], "limit.maximum", 0, 127)
            d = self._profile.resources[rid]
            if rid not in resources or d["control_kind"] != "absolute":
                raise _profile_error(f"A limit names {rid}, which is not an absolute control in this "
                                     "permission")
            if not d["range"]["minimum"] <= low <= high <= d["range"]["maximum"] or rid in normalized:
                raise _profile_error(f"The limit for {rid} must be one range inside its declared range")
            normalized[rid] = {"resource": rid, "minimum": low, "maximum": high}
        proactive = _fields(value["proactive_claims"], ("allowed", "idle_interval_ms", "cooldown_ms"),
                            where="proactive_claims")
        claims = {"allowed": _bool(proactive["allowed"], "proactive_claims.allowed"),
                  "idle_interval_ms": _int(proactive["idle_interval_ms"], "proactive_claims.idle_interval_ms",
                                           500, 600_000),
                  "cooldown_ms": _int(proactive["cooldown_ms"], "proactive_claims.cooldown_ms", 0, 600_000)}
        draft = {"resources": list(resources), "action_kinds": [k for k in ACTION_KINDS if k in kinds],
                 "proactive_claims": claims,
                 "max_lease_ms": _int(value["max_lease_ms"], "max_lease_ms", 250, settings["max_lease_ms"]),
                 "duration_ms": _int(value["duration_ms"], "duration_ms", 1000, 14_400_000),
                 "statement": _text(value["statement"], "statement", 1, 500)}
        if normalized:
            draft["limits"] = [normalized[rid] for rid in sorted(normalized)]
        return draft

    def _check_session(self, session_id) -> None:
        if self._session is None or session_id != self._session["session_id"]:
            raise PocketError("This b2b host holds no session with that session_id", code="invalid_request",
                              hint=_SESSION_HINT)

    def _check_channel(self, channel) -> None:
        if channel is not self._trusted:
            raise PocketError("Only the trusted b2b page can make this call", code="invalid_request",
                              hint=_CHANNEL_HINT)

    def register(self, request: dict) -> dict:
        """b2b_prepare's host side: one session per host; idempotent for identical inputs."""
        _fields(request, ("output_dir_resolved",), ("settings", "human_reserved", "proposed_policy"),
                "register")
        output = _text(request["output_dir_resolved"], "output_dir_resolved", 1, 4096)
        settings = normalize_settings(request.get("settings"))
        reserved = self._resolve(request.get("human_reserved") or [], "human_reserved", whole_groups=False,
                                 allow_empty=True)
        policy = request.get("proposed_policy")
        draft = None if policy is None else self._draft(policy, settings)
        sha = digest({"output_dir_resolved": output, "settings": settings, "human_reserved": list(reserved),
                      "proposed_policy": draft})
        self._check_secrets(output, draft)
        with self._entry() as now:
            self._check_running()
            if self._session is not None:
                if self._session["output_dir_resolved"] == output and self._session["input_sha256"] == sha:
                    return {**_plain(self._session["result"]), "replayed": True}
                raise PocketError("This b2b host already holds a session", code="invalid_request",
                                  hint=_ONE_SESSION_HINT)
            self._settings = MappingProxyType(settings)
            session_id = secrets.token_hex(16)
            for rid in reserved:
                self._take_reserved(rid, "prepared-hold")
            proposal = None if draft is None else self._record_proposal("prepare", draft, None, now)
            result = {"session_id": session_id, "host_epoch": self.host_epoch,
                      "session_epoch": self.session_epoch, "adapter_profile": self._profile.name,
                      "mapping_sha256": self._profile.sha256,
                      "settings": settings, "settings_sha256": digest(settings),
                      "human_reserved": list(reserved),
                      "proposal_id": None if proposal is None else proposal.proposal_id}
            self._session = {"session_id": session_id, "output_dir_resolved": output, "input_sha256": sha,
                             "result": result}
            self._emit("session_registered", session_id=session_id, human_reserved=list(reserved))
            self._log("session_registered", {**result, "output_dir_sha256": digest(output)})
            self._commit("register")
            self._session["result"] = {**result, "control_revision": self._revision,
                                       "state_sha256": self._sha, "monotonic_ns": now}
            return {**_plain(self._session["result"]), "replayed": False}

    def _take_reserved(self, rid: str, gesture_id: str) -> None:
        """A person's explicit hold. The agent channel can create this reduction but never undo it.

        A control waiting for pickup stays that way, so the fader mismatch stays visible, but the
        hold is remembered: settling the pickup leaves the control the person's, not free.
        """
        owner = self._owners[rid]
        if owner.kind == "agent":
            grant = self._grants[owner.grant_id]
            if grant.state == "active":
                self._end_grant(grant, "held_by_human")
            owner = self._owners[rid]
        if owner.kind in ("unavailable", "pickup_pending"):
            if not owner.explicit:
                self._owners[rid] = replace(owner, hold="explicit", gesture_id=gesture_id)
        elif not owner.explicit:
            self._set_owner(rid, "human", "held_by_human", hold="explicit", gesture_id=gesture_id,
                            since_monotonic_ns=self._entry_now)

    def _record_proposal(self, source: str, draft: dict, amend_of: tuple | None, now: int) -> Proposal | None:
        """Record a proposal for the person to review, coalescing an identical open one. Grants nothing."""
        sha = _proposal_sha(source, draft, amend_of)
        for proposal in self._proposals.values():
            if proposal.state == "open" and proposal.sha256 == sha:
                updated = replace(proposal, coalesced=proposal.coalesced + 1)
                self._proposals[proposal.proposal_id] = updated
                return updated
        if sum(1 for p in self._proposals.values() if p.state == "open") >= MAX_PROPOSALS:
            return None
        proposal = Proposal(f"p-{next(self._proposal_seq)}", source, canonical_bytes(draft), amend_of, sha,
                            now)
        self._proposals[proposal.proposal_id] = proposal
        closed = [pid for pid, p in self._proposals.items() if p.state != "open"]
        for pid in closed[:max(0, len(closed) - MAX_ENDED_RECORDS)]:
            del self._proposals[pid]
        self._emit("proposal_recorded", proposal_id=proposal.proposal_id, source=source)
        self._log("proposal", {"proposal": proposal.to_json()})
        return proposal

    def authorize(self, session_id: str, request: dict) -> dict:
        """Agents only propose: always refused authority_issuer_required; the person reviews the proposal."""
        _fields(request, ("request_id", "expected_session_epoch", "expected_control_revision",
                          "expected_state_sha256", "permission"),
                ("expected_permission_id", "expected_permission_version", "expected_permission_sha256"),
                "b2b_authorize")
        _request_id(request["request_id"])
        triple = _triple(request)
        draft = self._draft(request["permission"], self._settings)
        amend = [request.get(name) for name in ("expected_permission_id", "expected_permission_version",
                                                "expected_permission_sha256")]
        if any(v is not None for v in amend):
            if any(v is None for v in amend):
                raise _bad("Name all three expected_permission fields to propose an amendment, or none")
            amend = (_ident(amend[0], "expected_permission_id", "perm-"),
                     _int(amend[1], "expected_permission_version", 1, 2**31),
                     _sha(amend[2], "expected_permission_sha256"))
        else:
            amend = None
        inputs = {"triple": list(triple), "permission": draft,
                  "amend_of": None if amend is None else list(amend)}

        def evaluate(now):
            proposal = self._record_proposal("agent_authorize", draft, amend, now)
            warnings = [] if proposal is not None else ["proposal_limit_reached"]
            pid = "(none recorded)" if proposal is None else proposal.proposal_id
            return {"outcome": {"kind": "refused", "code": "authority_issuer_required",
                                "detail": "Only the person issues a standing permission, on the host page."},
                    "fields": {"proposal": None if proposal is None else proposal.to_json()},
                    "warnings": warnings,
                    "next_actions": [NEXT_ACTIONS["authority_issuer_required"].format(proposal_id=pid)]}

        return self._mutate("agent", "authorize", session_id, request, inputs, triple, "exact", evaluate,
                            increase=True)

    def claim(self, session_id: str, request: dict) -> dict:
        """Atomically claim idle named resources under an invitation or the active standing permission."""
        _fields(request, ("request_id", "expected_session_epoch", "expected_control_revision",
                          "expected_state_sha256", "resources", "basis", "reason", "lease_ms",
                          "not_after_monotonic_ns"), where="b2b_claim")
        _request_id(request["request_id"])
        triple = _triple(request)
        basis = self._basis(request["basis"])
        reason = _text(request["reason"], "reason", 1, 200)
        lease_ms = _int(request["lease_ms"], "lease_ms", 250, self._settings["max_lease_ms"])
        not_after = _int(request["not_after_monotonic_ns"], "not_after_monotonic_ns", 0, 2**63 - 1)
        resources = self._resolve(request["resources"])
        inputs = {"triple": list(triple), "resources": list(resources), "basis": basis.to_json(),
                  "reason": reason, "lease_ms": lease_ms, "not_after_monotonic_ns": not_after}

        def precheck(now):
            if not_after > now + MAX_AHEAD_NS:
                raise _bad("not_after_monotonic_ns is more than 60 s ahead of the host clock",
                           "Read monotonic_ns from b2b_observe and add at most 60000000000.")

        def evaluate(now):
            return self._evaluate_claim(request["request_id"], resources, basis, reason, lease_ms, not_after,
                                        now)

        return self._mutate("agent", "claim", session_id, request, inputs, triple, "exact", evaluate,
                            increase=True, precheck=precheck)

    @staticmethod
    def _basis(value) -> ClaimBasis:
        if not isinstance(value, dict) or value.get("kind") not in ("invitation", "standing_permission"):
            raise _bad("basis.kind must be invitation or standing_permission",
                       "Pass basis as {kind: invitation, invitation_id} or {kind: standing_permission, "
                       "permission_id, permission_version, revocation_generation}.")
        if value["kind"] == "invitation":
            _fields(value, ("kind", "invitation_id"), where="basis")
            return ClaimBasis("invitation",
                              invitation_id=_ident(value["invitation_id"], "invitation_id", "i-"))
        _fields(value, ("kind", "permission_id", "permission_version", "revocation_generation"),
                where="basis")
        return ClaimBasis(
            "standing_permission", permission_id=_ident(value["permission_id"], "permission_id", "perm-"),
            permission_version=_int(value["permission_version"], "permission_version", 1, 2**31),
            revocation_generation=_int(value["revocation_generation"], "revocation_generation", 0, 2**63 - 1))

    def _resource_blocker(self, resources, basis_kind: str, permission, now: int):
        """Claim checks 5-9 for these resources: (code, detail, retry_not_before) or None."""
        owners = {rid: self._owners[rid] for rid in resources}
        release_ns = self._settings["activity_release_ms"] * MS
        interval = (self._settings["settle_ms"] if basis_kind == "invitation"
                    else permission.proactive["idle_interval_ms"]) * MS
        nearby = self._profile.yield_closure(self._profile.conflict_closure(tuple(resources)))
        touched = [self._evidence[rid]["last_input_ns"] for rid in nearby
                   if self._evidence[rid]["last_input_ns"] is not None]
        idle_at = max(touched) + interval if touched else None
        for code, test in (("resource_unavailable", lambda o: o.kind == "unavailable"),
                           ("resource_owned_by_agent", lambda o: o.kind == "agent"),
                           ("resource_held_by_human", lambda o: o.explicit),
                           ("human_active", lambda o: o.kind == "human"),
                           ("pickup_unsettled", lambda o: o.kind == "pickup_pending")):
            hits = [rid for rid, o in owners.items() if test(o)]
            if hits:
                retry = None
                if code == "human_active":
                    retry = max(owners[rid].since_monotonic_ns + release_ns for rid in hits)
                    retry = retry if idle_at is None else max(retry, idle_at)
                return code, f"{code}: {', '.join(hits)}", retry
        if idle_at is not None and now < idle_at:
            return "human_active", "The person moved this control or its group too recently", idle_at
        if basis_kind == "standing_permission":
            pressed = [gid for gid, gs in self._gestures.items() if gs["state"] == "pressed"]
            if pressed:
                return "gesture_in_progress", f"Gesture {pressed[0]} is pressed", None
        uncertainty = self._settings["uncertainty_ms"] * MS
        if self._last_unknown_ns is not None and now - self._last_unknown_ns < uncertainty:
            return ("classification_uncertain", "Unknown or unqualified input arrived recently",
                    self._last_unknown_ns + uncertainty)
        if basis_kind == "standing_permission":
            until = max((self._cooldowns.get((permission.permission_id, rid), 0) for rid in resources),
                        default=0)
            if until > now:
                return "claim_cooldown", "A claim on this control was refused or denied recently", until
        return None

    def _evaluate_claim(self, request_id, resources, basis: ClaimBasis, reason, lease_ms, not_after, now
                        ) -> dict:
        if not_after <= now:
            return self._refused("deadline_passed", "not_after_monotonic_ns is not after the host clock")
        if not self._page_ready(now):
            return self._refused("notice_destination_unavailable",
                                 "No attached, visible b2b page can show this claim's notice")
        if len(self._active_grants()) >= MAX_GRANTS:
            return self._refused("grant_limit_reached", f"At most {MAX_GRANTS} grants are active at once")
        permission = None
        if basis.kind == "invitation":
            invitation = self._invitations.get(basis.invitation_id)
            if invitation is None:
                return self._refused("invitation_not_found", "No invitation has that invitation_id")
            if (invitation.state != "outstanding" or invitation.expires_monotonic_ns <= now
                    or invitation.adapter_epoch != self._adapter_epoch):
                return self._refused("invitation_ended", f"The invitation is {invitation.state}")
            outside = [rid for rid in resources if rid not in invitation.scope]
            if outside:
                return self._refused("invitation_scope", f"Outside the invitation: {', '.join(outside)}")
        else:
            permission = self._active_permission()
            if permission is None or permission.permission_id != basis.permission_id:
                return self._refused("permission_not_active", "No active permission has that permission_id")
            if permission.version != basis.permission_version:
                return self._refused("permission_not_active",
                                     f"permission_version is not current (current: {permission.version})")
            if (basis.revocation_generation != permission.revocation_generation
                    or basis.revocation_generation != self._revocations):
                return self._refused("permission_not_active", "revocation_generation is not current")
            if not permission.proactive["allowed"]:
                return self._refused("proactive_not_permitted", "This permission allows only invited claims")
            outside = [rid for rid in resources if rid not in permission.resources]
            if outside:
                return self._refused("permission_scope", f"Outside the permission: {', '.join(outside)}")
            if lease_ms > permission.draft["max_lease_ms"]:
                limit = permission.draft["max_lease_ms"]
                return self._refused("lease_exceeds_limit", f"The permission allows leases up to {limit} ms")
        blocker = self._resource_blocker(resources, basis.kind, permission, now)
        if blocker is not None:
            code, detail, retry = blocker
            if permission is not None and code in _COOLDOWN_CODES:
                self._start_cooldown(permission.permission_id, resources, now)
            # Refusals coalesce within the permission's cooldown, so a noisy control can't flood events.
            window = max(COALESCE_NS, 0 if permission is None else permission.proactive["cooldown_ms"] * MS)
            self._coalesced(("claim_refused", code, resources), "claim_refused", now, None, {}, window,
                            code=code, resources=list(resources), retry_not_before_monotonic_ns=retry)
            return self._refused(code, detail, retry_not_before_monotonic_ns=retry)
        grant, notice = self._grant(request_id, resources, basis, reason, lease_ms, not_after, now)
        if basis.kind == "invitation":
            invitation = self._invitations[basis.invitation_id]
            self._end_invitation(invitation, "consumed", "claimed", grant.grant_id)
        return {"outcome": {"kind": "granted", "grant": grant.to_json()},
                "fields": {"grant": grant.to_json(), "notice": notice.to_json()}}

    def _grant(self, request_id, resources, basis, reason, lease_ms, not_after, now) -> tuple[Grant, Notice]:
        """All at once: pin generations, create the notice, journal both. Nothing moves before the ack."""
        grant_id = f"g-{secrets.token_hex(16)}"
        lease_expires = now + lease_ms * MS
        generations = []
        for rid in resources:
            owner = self._set_owner(rid, "agent", "claim", grant_id=grant_id,
                                    expires_monotonic_ns=lease_expires)
            generations.append((rid, owner.generation))
        labels = tuple(self._profile.label(rid) for rid in resources)
        notice = Notice(f"n-{secrets.token_hex(8)}", grant_id, self.session_epoch, labels, reason,
                        f"Agent taking {', '.join(labels)}: {reason} (lease {_seconds(lease_ms)})", now,
                        min(now + self._settings["notice_ack_timeout_ms"] * MS, not_after), not_after)
        grant = Grant(grant_id, request_id, self.session_epoch, self._adapter_epoch, tuple(resources),
                      tuple(resources), tuple(generations), basis, reason, self._revision, now, lease_expires,
                      not_after, notice.notice_id)
        self._store_grant(grant)
        self._store_notice(notice)
        self._emit("grant_created", grant=grant.authority_json(), notice_id=notice.notice_id)
        self._emit("notice_created", notice_id=notice.notice_id, grant_id=grant_id, text=notice.text,
                   deadline_monotonic_ns=notice.deadline_monotonic_ns)
        self._log("grant_created", {"grant": grant.to_json()})
        self._log("notice_created", {"notice": notice.to_json()})
        return grant, notice

    def release(self, session_id: str, request: dict) -> dict:
        """Give back some or all of an own grant; controls stay where they are (pickup rule)."""
        _fields(request, ("request_id", "expected_session_epoch", "expected_control_revision",
                          "expected_state_sha256", "grant_id"), ("resources",), "b2b_release")
        _request_id(request["request_id"])
        triple = _triple(request)
        grant_id = _ident(request["grant_id"], "grant_id", "g-")
        names = request.get("resources")
        resources = None if names is None else self._resolve(names)
        inputs = {"triple": list(triple), "grant_id": grant_id,
                  "resources": None if resources is None else list(resources)}

        def evaluate(now):
            grant = self._grants.get(grant_id)
            if grant is None or grant.state != "active":
                return self._refused("grant_not_found", "No active grant has that grant_id")
            chosen = grant.resources if resources is None else resources
            outside = [rid for rid in chosen if rid not in grant.resources]
            if outside:
                return self._refused("resource_not_granted", f"Not held by this grant: {', '.join(outside)}")
            updated = self._lose(grant, chosen, "released", release_owners=True)
            if updated.state == "ended":
                outcome = {"kind": "released", "grant_id": grant_id, "resources": list(chosen)}
            else:
                outcome = {"kind": "narrowed", "grant_id": grant_id, "remaining": list(updated.resources)}
            return {"outcome": outcome, "fields": {"released": list(chosen), "grant": updated.to_json()}}

        return self._mutate("agent", "release", session_id, request, inputs, triple, "epoch", evaluate,
                            increase=False)

    def revoke(self, session_id: str, request: dict) -> dict:
        """Shorten an own lease, withdraw an own proposal or drop all own authority. Never expands."""
        _fields(request, ("request_id", "expected_session_epoch", "expected_control_revision",
                          "expected_state_sha256", "target"), where="b2b_revoke")
        _request_id(request["request_id"])
        triple = _triple(request)
        target = request["target"]
        kind = target.get("kind") if isinstance(target, dict) else None
        if kind == "grant_lease":
            _fields(target, ("kind", "grant_id", "lease_ms"), where="target")
            target = {"kind": kind, "grant_id": _ident(target["grant_id"], "grant_id", "g-"),
                      "lease_ms": _int(target["lease_ms"], "target.lease_ms", 1,
                                       self._settings["max_lease_ms"])}
        elif kind == "own_proposal":
            _fields(target, ("kind", "proposal_id"), where="target")
            target = {"kind": kind, "proposal_id": _ident(target["proposal_id"], "proposal_id", "p-")}
        elif kind == "all_own_authority":
            _fields(target, ("kind",), where="target")
            target = {"kind": kind}
        elif kind == "permission":
            _fields(target, ("kind", "permission_id"), where="target")
            target = {"kind": kind,
                      "permission_id": _ident(target["permission_id"], "permission_id", "perm-")}
        else:
            raise _bad("target.kind must be grant_lease, own_proposal, all_own_authority or permission")
        inputs = {"triple": list(triple), "target": target}

        def evaluate(now):
            if kind == "permission":
                return {**self._refused("authority_issuer_required",
                                        "Only the person revokes a standing permission, on the host page"),
                        "next_actions": [_REVOKE_POLICY_NEXT]}
            if kind == "own_proposal":
                proposal = self._proposals.get(target["proposal_id"])
                if proposal is None or proposal.state != "open" or proposal.source == "reissue":
                    return self._refused("proposal_not_found", "No open agent proposal has that proposal_id")
                self._proposals[proposal.proposal_id] = replace(proposal, state="withdrawn")
                self._log("proposal", {"proposal_id": proposal.proposal_id, "state": "withdrawn"})
                return {"outcome": {"kind": "revoked", "target": target}}
            if kind == "all_own_authority":
                ended = [g.grant_id for g in self._active_grants()]
                self._end_all_grants("revoked_by_agent")
                return {"outcome": {"kind": "revoked", "target": target, "grants": ended}}
            grant = self._grants.get(target["grant_id"])
            if grant is None or grant.state != "active":
                return self._refused("grant_not_found", "No active grant has that grant_id")
            expires = now + target["lease_ms"] * MS
            if expires >= grant.lease_expires_monotonic_ns:
                return self._refused("expansion_not_permitted", "A lease can only be shortened")
            updated = replace(grant, lease_expires_monotonic_ns=expires)
            self._store_grant(updated)
            for rid in updated.resources:
                # Same owner, shorter lease: the generation stays, so a running act keeps its pinned grant.
                self._owners[rid] = replace(self._owners[rid], expires_monotonic_ns=expires)
            return {"outcome": {"kind": "revoked", "target": target, "lease_expires_monotonic_ns": expires},
                    "fields": {"grant": updated.to_json()}}

        return self._mutate("agent", "revoke", session_id, request, inputs, triple, "epoch", evaluate,
                            increase=False)

    def observe(self, session_id: str) -> dict:
        """Owners, grants, notices, permissions and adapter evidence, each with its own age."""
        with self._entry() as now:
            self._check_session(session_id)
            return self._observation(now, include_all_notices=False)

    def _observation(self, now: int, *, include_all_notices: bool) -> dict:
        state = self._authority_state()
        permission = self._active_permission()
        invitations = self._outstanding_invitations()
        health = self._health()  # None when the adapter's health can't be read as declared
        resources = []
        for rid in self._profile.order:
            d = self._profile.resources[rid]
            blocked = {}
            for kind in ("invitation", "standing_permission"):
                covered = (any(rid in i.scope for i in invitations) if kind == "invitation"
                           else permission is not None and permission.proactive["allowed"]
                           and rid in permission.resources)
                closure = self._profile.conflict_closure((rid,))
                blocker = (self._resource_blocker(closure, kind, permission, now)
                           if kind == "invitation" or permission is not None else None)
                blocked[kind] = (covered, blocker)
            first = next((b for _, b in blocked.values() if b is not None), None)
            resources.append({
                "resource_id": rid, "label": d["label"], "aliases": list(self._profile.aliases_of(rid)),
                "control_kind": d["control_kind"], "range": None if d["range"] is None else dict(d["range"]),
                "states": None if d["states"] is None else list(d["states"]), "audibility": d["audibility"],
                "agent_writable": d["agent_writable"], "conflict_group": d["conflict_group"],
                "yield_group": d["yield_group"], "owner": self._owners[rid].to_json(),
                "generation": self._owners[rid].generation,
                "claimable": {"invitation": blocked["invitation"][0] and blocked["invitation"][1] is None,
                              "permission": blocked["standing_permission"][0]
                              and blocked["standing_permission"][1] is None},
                "blocked_by": None if first is None else first[0],
                "retry_not_before_monotonic_ns": None if first is None else first[2]})
        evidence = {}
        for rid in self._profile.order:
            e = self._evidence[rid]
            readback = self._read_back(rid) if self._profile.resources[rid]["readback"] in (
                "numeric", "state") else None
            evidence[rid] = {"readback": None if readback is None else {
                                 "scope": readback.scope, "value": readback.value,
                                 "observed_monotonic_ns": readback.observed_monotonic_ns},
                             "physical_value": e["physical_value"],
                             "last_human_input_monotonic_ns": e["last_input_ns"],
                             "commanded_value": e["commanded_value"]}
        page = self._page
        uncertainty_until = (None if self._last_unknown_ns is None
                             else self._last_unknown_ns + self._settings["uncertainty_ms"] * MS)
        notices = [n.to_json() for n in self._notices.values() if include_all_notices or n.state == "pending"]
        dependencies, heartbeat = ({}, None) if health is None else health
        observation = {
            "schema": OBSERVATION_SCHEMA,
            "session_id": None if self._session is None else self._session["session_id"],
            "host_epoch": self.host_epoch, "session_epoch": self.session_epoch,
            "control_revision": self._revision, "state_sha256": self._sha, "monotonic_ns": now,
            "wall_time_utc": self._clock.wall_utc(), "authority_state": state, "resources": resources,
            "gestures": {gid: gs["state"] for gid, gs in self._gestures.items()},
            "notices": notices,
            "grants": [g.to_json() for g in self._active_grants()],
            "invitations": [i.to_json() for i in invitations],
            "permissions": [] if permission is None else [permission.to_json()],
            "proposals": [p.to_json() for p in self._proposals.values() if p.state == "open"],
            "cooldowns": [{"permission_id": pid, "resource": rid, "until_monotonic_ns": until}
                          for (pid, rid), until in sorted(self._cooldowns.items())],
            "evidence": {"resources": evidence,
                         "adapter": {"profile": self._profile.name, "mapping_sha256": self._profile.sha256,
                                     "adapter_epoch": self._adapter_epoch,
                                     "dependencies": dict(dependencies),
                                     "heartbeat_monotonic_ns": heartbeat, "native": False},
                         "trusted_page": {"attached": page["ui_epoch"] is not None,
                                          "visible": page["visible"], "ready": self._page_ready(now),
                                          "ui_epoch": page["ui_epoch"],
                                          "last_seen_monotonic_ns": page["last_seen_ns"]},
                         "journal": {k: v for k, v in self._journal.status().items() if k != "last_seq"},
                         "uncertainty_until_monotonic_ns": uncertainty_until,
                         "counters": {**self._counters, "sink_violations": self._sink_violations}},
            "ages_ms": {"adapter_heartbeat": None if heartbeat is None else (now - heartbeat) // MS,
                        "trusted_page": None if page["last_seen_ns"] is None
                        else (now - page["last_seen_ns"]) // MS},
            "events_cursor": {"host_epoch": self.host_epoch, "sequence": self._event_seq},
            "coverage": dict(COVERAGE),
            "warnings": ([] if self._page_ready(now) else
                         ["No b2b page is attached and visible; a claim would be refused."])
            + ([] if health is not None else ["The adapter's health could not be read; acts are refused."])
            + ([] if not self._sink_violations else
               [(f"The adapter called the broker from inside a broker section {self._sink_violations} "
                 "time(s); its readings may be missing.")])}
        return _plain(observation)

    def events(self, session_id: str, request: dict | None = None) -> dict:
        """Bounded sequenced events after a cursor. An expired cursor says so; it never hides a gap."""
        request = {} if request is None else request
        _fields(request, (), ("after", "max_events", "max_bytes"), "b2b_events")
        after = request.get("after")
        if after is not None:
            _fields(after, ("host_epoch", "sequence"), where="after")
            _text(after["host_epoch"], "after.host_epoch", 1, 32)
            _int(after["sequence"], "after.sequence", 0, 2**63 - 1)
        max_events = _int(request.get("max_events", 64), "max_events", 1, 256)
        max_bytes = _int(request.get("max_bytes", 65536), "max_bytes", 1024, MAX_EVENTS_PAGE_BYTES)
        with self._entry():
            self._check_session(session_id)
            earliest = self._events[0][0]["sequence"] if self._events else self._event_seq + 1
            result = {"schema": EVENTS_SCHEMA, "session_id": session_id, "host_epoch": self.host_epoch,
                      "events": [], "earliest_retained_sequence": earliest, "cursor_expired": False,
                      "cursor_expired_reason": None, "truncated_by_bytes": False}
            reason = None
            if after is not None and after["host_epoch"] != self.host_epoch:
                reason = "host_epoch_changed"
            elif after is not None and after["sequence"] < earliest - 1:
                reason = "before_retained"
            if reason is not None:
                result.update(cursor_expired=True, cursor_expired_reason=reason,
                              next_cursor={"host_epoch": self.host_epoch, "sequence": earliest - 1})
                return result
            start = earliest - 1 if after is None else after["sequence"]
            chosen, used = [], 0
            for event, size in self._events:
                if event["sequence"] <= start:
                    continue
                if len(chosen) >= max_events:
                    break
                if chosen and used + size > max_bytes:
                    result["truncated_by_bytes"] = True
                    break
                chosen.append(event)
                used += size
            result["events"] = _plain(chosen)
            last = chosen[-1]["sequence"] if chosen else max(start, earliest - 1)
            result["next_cursor"] = {"host_epoch": self.host_epoch, "sequence": last}
            return result

    def request_status(self, session_id: str, request_id: str) -> dict:
        """The host's view of one agent request: retained outcome only, never a redispatch."""
        _request_id(request_id)
        with self._entry():
            self._check_session(session_id)
            entry = self._requests["agent"].get(request_id)
            cached = self._receipts.get(("agent", request_id))
            if entry is None:
                state = "not_found"
            else:
                state = entry.state
            retained = None if cached is None else {**_plain(cached), "replayed": True}
            offset = None if entry is None else entry.journal_offset
            sha = None if entry is None else entry.receipt_sha256
        if state == "complete" and retained is None and offset is not None:
            try:
                retained = self._reread(_Reread(offset, sha))
            except PocketError:
                retained = None
        return {"request_id": request_id, "source": "host", "host_running": True, "state": state,
                "receipt": retained, "receipt_unavailable": state == "complete" and retained is None,
                "redispatched": False, "automatic_retry": False}

    def trusted_attach(self, channel) -> str:
        """A new page epoch. An earlier page's epoch stops counting for presence and acks."""
        self._check_channel(channel)
        with self._entry() as now:
            ui_epoch = f"ui-{secrets.token_hex(8)}"
            self._page = {"ui_epoch": ui_epoch, "visible": False, "last_seen_ns": now, "polls": 0,
                          "reported": False}
            self._emit("page_attached", ui_epoch=ui_epoch)
            return ui_epoch

    def _seen(self, ui_epoch, now: int) -> bool:
        page = self._page
        if page["ui_epoch"] is None or ui_epoch != page["ui_epoch"]:
            return False
        page["last_seen_ns"] = now
        if page["reported"]:
            page["reported"] = False
            self._emit("page_attached", ui_epoch=ui_epoch)
        return True

    def trusted_presence(self, channel, ui_epoch, visible) -> dict:
        self._check_channel(channel)
        _bool(visible, "visible")
        with self._entry() as now:
            current = self._seen(ui_epoch, now)
            if current:
                self._page["visible"] = visible
            return {"current": current, "visible": visible if current else False}

    def trusted_poll(self, channel, ui_epoch, active) -> dict:
        """Count a long-poll in progress; while one is outstanding the page counts as seen."""
        self._check_channel(channel)
        _bool(active, "active")
        with self._entry() as now:
            current = self._seen(ui_epoch, now)
            if current:
                self._page["polls"] = max(0, self._page["polls"] + (1 if active else -1))
            return {"current": current, "polls": self._page["polls"] if current else 0}

    def trusted_state(self, channel, ui_epoch) -> dict:
        """The page's full snapshot. For the current page it counts as presence and as a notice fetch."""
        self._check_channel(channel)
        with self._entry() as now:
            current = self._seen(ui_epoch, now)
            if current:
                self._mark_fetched(ui_epoch)
            observation = self._observation(now, include_all_notices=True)
            permissions = {}
            for p in self._permissions:
                permissions[p.permission_id] = p
            observation.update(
                ui_epoch=ui_epoch, current=current, host_monotonic_ns=now,
                proposals=[p.to_json() for p in self._proposals.values() if p.state == "open"],
                permission_history=[p.to_json() for p in permissions.values()],
                # Issue cites this record exactly (expected_permission_*), or null when none was issued.
                permission_head=self._permissions[-1].to_json() if self._permissions else None,
                recent_events=_plain([event for event, _ in list(self._events)[-20:]]),
                # Chosen by the agent at b2b_prepare; the page shows the person any that differ.
                settings=dict(self._settings),
                settings_defaults={name: default for name, (default, _, _) in SETTINGS.items()},
                journal_degraded=self._journal_degraded)
            return page_json(observation)

    def trusted_public(self) -> dict:
        """What a page without the issuer token may see (after a reload): enough to stop, hold and deny."""
        with self._entry() as now:
            self._mark_fetched("public")
            permission = self._active_permission()
            labels = self._profile.label
            return page_json(_plain({
                "host_monotonic_ns": now, "session_epoch": self.session_epoch,
                "control_revision": self._revision, "state_sha256": self._sha,
                "page_attached": self._page_ready(now),
                "agent_grants": [{"grant_id": g.grant_id, "labels": [labels(r) for r in g.resources],
                                  "reason": g.reason,
                                  "remaining_ms": max(0, (g.lease_expires_monotonic_ns - now) // MS)}
                                 for g in self._active_grants()],
                "notices": [{"notice_id": n.notice_id, "grant_id": n.grant_id, "text": n.text,
                             "action_deadline_monotonic_ns": n.action_deadline_monotonic_ns,
                             "remaining_ms": max(0, (n.deadline_monotonic_ns - now) // MS)}
                            for n in self._notices.values() if n.state == "pending"],
                "invitations": [{"invitation_id": i.invitation_id, "labels": [labels(r) for r in i.scope],
                                 "remaining_ms": max(0, (i.expires_monotonic_ns - now) // MS)}
                                for i in self._outstanding_invitations()],
                "permission": None if permission is None else {
                    "permission_id": permission.permission_id, "version": permission.version,
                    "state": permission.state,
                    "remaining_ms": max(0, (permission.expires_monotonic_ns - now) // MS)},
                "holds": sorted(rid for rid, o in self._owners.items() if o.explicit),
                "journal_degraded": self._journal_degraded}))

    def trusted_ack(self, channel, payload) -> dict:
        """The notice gate. Only a fetched, pending notice, before its deadline, from the current page."""
        self._check_channel(channel)
        names = ("notice_id", "grant_id", "session_epoch", "ui_epoch", "action_deadline_monotonic_ns")
        _fields(payload, names, where="ack")
        for name in names:
            _text(payload[name], name, 1, 64)
        self._check_secrets(payload)  # an unknown notice_id is journaled as sent
        with self._entry() as now:
            notice = self._notices.get(payload["notice_id"])
            if notice is None:
                self._log("ack_rejected", {"notice_id": payload["notice_id"], "why": "unknown_notice"})
                return {"accepted": False, "outcome": "unknown_notice", "notice_id": payload["notice_id"]}
            deadline = str(notice.action_deadline_monotonic_ns)
            matches = (payload["grant_id"] == notice.grant_id
                       and payload["session_epoch"] == self.session_epoch == notice.session_epoch
                       and payload["ui_epoch"] == self._page["ui_epoch"]
                       and payload["action_deadline_monotonic_ns"] == deadline)
            if notice.state == "acknowledged" and matches and payload["ui_epoch"] == notice.ack_ui_epoch:
                return {"accepted": True, "outcome": "acknowledged", "notice_id": notice.notice_id}
            if notice.state != "pending":
                # _advance has already classified a late ack from state recorded before the deadline.
                self._notices[notice.notice_id] = replace(notice, late_acks=notice.late_acks + 1)
                self._log("ack_late", {"notice_id": notice.notice_id, "state": notice.state})
                return {"accepted": False, "outcome": notice.state, "notice_id": notice.notice_id}
            if not matches or payload["ui_epoch"] not in notice.fetched_by or not self._page["visible"]:
                self._notices[notice.notice_id] = replace(notice, rejected_acks=notice.rejected_acks + 1)
                self._log("ack_rejected", {"notice_id": notice.notice_id, "why": "mismatch"})
                return {"accepted": False, "outcome": "rejected", "notice_id": notice.notice_id}
            self._seen(payload["ui_epoch"], now)
            acknowledged = replace(notice, state="acknowledged", acknowledged_monotonic_ns=now,
                                   ack_ui_epoch=payload["ui_epoch"])
            self._notices[notice.notice_id] = acknowledged
            self._emit("notice_acknowledged", notice_id=notice.notice_id, grant_id=notice.grant_id)
            self._log("ack_accepted", {"notice_id": notice.notice_id, "grant_id": notice.grant_id,
                                       "ui_epoch": payload["ui_epoch"]})
            self._on_notice_acknowledged(acknowledged, now)
            self._commit("ack")
            return {"accepted": True, "outcome": "acknowledged", "notice_id": notice.notice_id}

    def _page_inputs(self, payload, required, optional=()) -> tuple[dict, tuple | None]:
        _fields(payload, ("request_id", *required),
                (*optional, "expected_session_epoch", "expected_control_revision", "expected_state_sha256"),
                "page request")
        _request_id(payload["request_id"])
        return payload, _triple(payload, required=False)

    def _page_exact(self, payload) -> tuple:
        """A page increase cites revision and SHA exactly (the epoch is inside the SHA)."""
        epoch = payload.get("expected_session_epoch", self.session_epoch)
        return (epoch,
                _int(payload.get("expected_control_revision"), "expected_control_revision", 0, 2**63 - 1),
                _sha(payload.get("expected_state_sha256"), "expected_state_sha256"))

    def trusted_invite(self, channel, payload) -> dict:
        """The person hands named controls to the agent: one single-use invitation, exact CAS."""
        self._check_channel(channel)
        self._page_inputs(payload, ("resources",))
        triple = self._page_exact(payload)
        resources = self._resolve(payload["resources"])
        inputs = {"triple": list(triple), "resources": list(resources)}

        def evaluate(now):
            held = [rid for rid in resources if self._owners[rid].explicit]
            if held:  # a conflict group is invited whole, so one row's Invite could reach another's hold
                return self._refused("resource_held_by_human",
                                     f"Release your hold on {', '.join(held)} first; an invitation never "
                                     "drops a hold")
            if len(self._outstanding_invitations()) >= MAX_INVITATIONS:
                return self._refused("invitation_limit_reached",
                                     f"At most {MAX_INVITATIONS} invitations are outstanding at once")
            invitation = self._make_invitation("trusted_page", None, resources, now)
            return {"outcome": {"kind": "invited", "invitation": invitation.to_json()}}

        return self._mutate("page", "invite", None, payload, inputs, triple, "exact", evaluate, increase=True)

    def trusted_release_hold(self, channel, payload) -> dict:
        self._check_channel(channel)
        self._page_inputs(payload, ("resources",))
        triple = self._page_exact(payload)
        resources = self._resolve(payload["resources"], whole_groups=False)
        inputs = {"triple": list(triple), "resources": list(resources)}

        def evaluate(now):
            released, not_held = [], []
            for rid in resources:
                owner = self._owners[rid]
                if owner.kind == "human" and owner.explicit:
                    self._set_owner(rid, "unowned", "hold_released")
                    released.append(rid)
                elif owner.kind in ("unavailable", "pickup_pending") and owner.explicit:
                    self._owners[rid] = replace(owner, hold=None, gesture_id=None)
                    released.append(rid)
                else:
                    not_held.append(rid)
            return {"outcome": {"kind": "hold_released", "resources": released, "not_held": not_held}}

        return self._mutate("page", "release_hold", None, payload, inputs, triple, "exact", evaluate,
                            increase=True)

    def trusted_accept_pickup(self, channel, payload) -> dict:
        self._check_channel(channel)
        self._page_inputs(payload, ("resource",))
        triple = self._page_exact(payload)
        rid = self._resolve([payload["resource"]] if isinstance(payload["resource"], str) else None,
                            whole_groups=False)[0]
        inputs = {"triple": list(triple), "resource": rid}

        def evaluate(now):
            owner = self._owners[rid]
            if owner.kind != "pickup_pending":
                return self._refused("stale_page_state", f"{rid} is not waiting for pickup")
            if owner.explicit:  # settled, and still the person's
                self._set_owner(rid, "human", "pickup_accepted", hold="explicit", gesture_id=owner.gesture_id,
                                since_monotonic_ns=now)
            else:
                self._set_owner(rid, "unowned", "pickup_accepted")
            self._emit("pickup_settled", resource=rid, how="accepted_on_page")
            self._log("pickup", {"resource": rid, "state": "accepted"})
            return {"outcome": {"kind": "pickup_accepted", "resource": rid}}

        return self._mutate("page", "accept_pickup", None, payload, inputs, triple, "exact", evaluate,
                            increase=True)

    @staticmethod
    def _amend_change(change) -> dict:
        """A narrowing change, checked before anything is recorded: one field, narrowing only."""
        if not isinstance(change, dict) or len(change) != 1:
            raise _bad("change is {proactive_allowed: false} or {expires_in_ms: N}")
        if "proactive_allowed" in change:
            if change["proactive_allowed"] is not False:
                raise _bad("An amendment can only turn proactive claims off",
                           "Pass change {proactive_allowed: false}, or issue a new permission.")
            return {"proactive_allowed": False}
        if "expires_in_ms" in change:
            return {"expires_in_ms": _int(change["expires_in_ms"], "change.expires_in_ms", 1000, 14_400_000)}
        raise _bad("change is {proactive_allowed: false} or {expires_in_ms: N}")

    def _amend_draft(self, permission: StandingPermission, change) -> dict:
        change = self._amend_change(change)
        draft = permission.draft
        if "proactive_allowed" in change:
            draft["proactive_claims"] = {**draft["proactive_claims"], "allowed": False}
        else:
            draft["duration_ms"] = change["expires_in_ms"]
        return draft

    def _amend_preview(self, permission: StandingPermission, change) -> dict:
        draft = self._amend_draft(permission, change)
        what = ("turn proactive claims off" if "proactive_allowed" in change
                else f"end {_duration_text(draft['duration_ms'])} after you confirm")
        ends = ("Ends when it was already going to end." if "proactive_allowed" in change
                else f"Ends {_duration_text(draft['duration_ms'])} after you confirm.")
        lines = [f"Change permission {permission.permission_id} version {permission.version}: {what}.",
                 *(f"{ends} Stop agent control or Revoke ends it at once." if line.startswith("Lasts ")
                   else line for line in render_preview(draft, self._profile))]
        amend_of = [permission.permission_id, permission.version, permission.state_sha256]
        proposal_sha = digest({"amend_of": amend_of, "change": change})
        return {"proposal_id": None, "proposal_sha256": proposal_sha, "lines": lines,
                "preview_sha256": digest({"proposal_id": None, "proposal_sha256": proposal_sha,
                                          "lines": lines}),
                "draft": draft}

    def _proposal_preview(self, proposal: Proposal) -> dict:
        lines = render_preview(proposal.draft, self._profile)
        if proposal.amend_of is not None:
            # An agent-proposed amendment issues as a whole replacement, so the person reads it as one.
            permission_id, version, _ = proposal.amend_of
            lines = [(f"Replaces permission {permission_id} version {version}, which ends when you press "
                      "Issue. These lines are the whole new permission, not a change to the old one."),
                     *lines]
        return {"proposal_id": proposal.proposal_id, "proposal_sha256": proposal.sha256, "lines": lines,
                "preview_sha256": digest({"proposal_id": proposal.proposal_id,
                                          "proposal_sha256": proposal.sha256, "lines": lines})}

    def _permission_head(self, permission_id: str) -> StandingPermission | None:
        return next((p for p in reversed(self._permissions) if p.permission_id == permission_id), None)

    def trusted_preview(self, channel, payload) -> dict:
        """Exact scope, duration and proactive behavior, server-rendered, before anything is issued."""
        self._check_channel(channel)
        if not isinstance(payload, dict) or len(payload) != 1:
            raise _bad("preview takes {proposal_id}, {amend: {permission_id, change}} or {reissue_of}")
        with self._entry() as now:
            if "proposal_id" in payload:
                proposal = self._proposals.get(payload["proposal_id"])
                if proposal is None or proposal.state != "open":
                    raise PocketError("No open proposal has that proposal_id", code="invalid_request",
                                      hint="Refresh the page; that proposal was issued, dismissed or "
                                           "withdrawn.")
                return self._proposal_preview(proposal)
            if "amend" in payload:
                amend = _fields(payload["amend"], ("permission_id", "change"), where="amend")
                permission = self._permission_head(amend["permission_id"])
                if permission is None or permission.state != "active":
                    raise PocketError("No active permission has that permission_id", code="invalid_request",
                                      hint="Refresh the page; only an active permission can be amended.")
                preview = self._amend_preview(permission, amend["change"])
                return {k: v for k, v in preview.items() if k != "draft"}
            if "reissue_of" in payload:
                old = (self._permission_head(payload["reissue_of"]) if isinstance(payload["reissue_of"], str)
                       else None)
                if old is None or old.state == "active":
                    raise PocketError("Only an ended permission can be issued again", code="invalid_request",
                                      hint="Refresh the page; Issue again appears once a permission has "
                                           "ended.")
                proposal = self._record_proposal("reissue", old.draft, None, now)
                if proposal is None:
                    raise PocketError("Too many open proposals", code="invalid_request",
                                      hint="Dismiss an open proposal on the page, then press Issue again.")
                return self._proposal_preview(proposal)
            raise _bad("preview takes {proposal_id}, {amend: {permission_id, change}} or {reissue_of}")

    def _expected_permission(self, payload) -> tuple | None:
        values = [payload.get(name) for name in ("expected_permission_id", "expected_permission_version",
                                                 "expected_permission_sha256")]
        if all(v is None for v in values):
            return None
        if any(v is None for v in values):
            raise _bad("Name all three expected_permission fields, or none")
        return (_ident(values[0], "expected_permission_id", "perm-"),
                _int(values[1], "expected_permission_version", 1, 2**31),
                _sha(values[2], "expected_permission_sha256"))

    def trusted_issue(self, channel, payload) -> dict:
        """Issue exactly the previewed permission. The issuer is this channel; attribution only records."""
        self._check_channel(channel)
        self._page_inputs(payload, ("proposal_id", "proposal_sha256", "preview_sha256", "attribution_name",
                                    "attribution_statement"),
                          ("expected_permission_id", "expected_permission_version",
                           "expected_permission_sha256"))
        proposal_id = _ident(payload["proposal_id"], "proposal_id", "p-")
        proposal_sha = _sha(payload["proposal_sha256"], "proposal_sha256")
        preview_sha = _sha(payload["preview_sha256"], "preview_sha256")
        name = _text(payload["attribution_name"], "attribution_name", 1, 80)
        statement = _text(payload["attribution_statement"], "attribution_statement", 0, 500)
        expected = self._expected_permission(payload)
        inputs = {"proposal_id": proposal_id, "proposal_sha256": proposal_sha, "preview_sha256": preview_sha,
                  "attribution_name": name, "attribution_statement": statement,
                  "expected_permission": None if expected is None else list(expected)}

        def evaluate(now):
            if self._session is None:
                return self._refused("permission_not_active", "No session is prepared on this host yet")
            proposal = self._proposals.get(proposal_id)
            if proposal is None or proposal.state != "open" or proposal.sha256 != proposal_sha:
                return self._refused("proposal_changed", "The proposal changed, or was issued or dismissed")
            if self._proposal_preview(proposal)["preview_sha256"] != preview_sha:
                return self._refused("confirmation_mismatch", "The confirmed preview is not this proposal's")
            head = self._permissions[-1] if self._permissions else None
            head_id = None if head is None else (head.permission_id, head.version, head.state_sha256)
            if expected != head_id:
                return self._refused("stale_page_state", "The page's permission state is not current")
            active = self._active_permission()
            if proposal.amend_of is None and active is not None:
                return self._refused("permission_active",
                                     "A permission is already active; amend or revoke it")
            if proposal.amend_of is not None and (active is None or proposal.amend_of != (
                    active.permission_id, active.version, active.state_sha256)):
                return self._refused("stale_page_state", "The permission this proposal amends is not current")
            permission = self._issue(proposal.draft, proposal, preview_sha, name, statement, active, now)
            self._proposals[proposal_id] = replace(proposal, state="issued")
            return {"outcome": {"kind": "issued", "permission": permission.to_json()},
                    "fields": {"permission": permission.to_json()}}

        return self._mutate("page", "issue", None, payload, inputs, None, "record", evaluate, increase=True)

    def _issue(self, draft, proposal, preview_sha, name, statement, previous, now, expires=None
               ) -> StandingPermission:
        expires = now + draft["duration_ms"] * MS if expires is None else expires
        wall = datetime.fromisoformat(self._clock.wall_utc()) + timedelta(milliseconds=(expires - now) // MS)
        permission = StandingPermission(
            permission_id=previous.permission_id if previous is not None else f"perm-{secrets.token_hex(8)}",
            version=1 if previous is None else previous.version + 1,
            session_id=self._session["session_id"], session_epoch=self.session_epoch,
            mapping_sha256=self._profile.sha256, settings_sha256=digest(dict(self._settings)),
            issuer_host_epoch=self.host_epoch, issuer_ui_epoch=self._page["ui_epoch"] or "none",
            actor=name, statement=statement,
            proposal_id=None if proposal is None else proposal.proposal_id,
            proposal_sha256=proposal.sha256 if proposal is not None else digest(draft),
            preview_sha256=preview_sha, draft_json=canonical_bytes(draft), issued_monotonic_ns=now,
            expires_monotonic_ns=expires, expires_wall_time_utc=wall.isoformat(),
            revocation_generation=self._revocations)
        if previous is not None:
            self._end_permission(previous, "superseded", "amended")
        self._store_permission(permission)
        self._emit("permission_amended" if previous is not None else "permission_issued",
                   permission_id=permission.permission_id, version=permission.version)
        self._log("permission", {"permission": permission.to_json()})
        return permission

    def trusted_amend(self, channel, payload) -> dict:
        """Narrow the active permission only: turn proactive claims off or end it sooner. Exact CAS."""
        self._check_channel(channel)
        self._page_inputs(payload, ("permission_id", "expected_permission_version",
                                    "expected_permission_sha256", "change", "preview_sha256"))
        permission_id = _ident(payload["permission_id"], "permission_id", "perm-")
        version = _int(payload["expected_permission_version"], "expected_permission_version", 1, 2**31)
        sha = _sha(payload["expected_permission_sha256"], "expected_permission_sha256")
        preview_sha = _sha(payload["preview_sha256"], "preview_sha256")
        change = self._amend_change(payload["change"])
        inputs = {"permission_id": permission_id, "version": version, "sha": sha, "change": change,
                  "preview_sha256": preview_sha}

        def evaluate(now):
            head = self._permission_head(permission_id)
            if head is None or head.state != "active":
                return self._refused("permission_not_active", "That permission is not active")
            if head.version != version or head.state_sha256 != sha:
                return self._refused("stale_page_state", "The permission changed since the page read it")
            preview = self._amend_preview(head, change)
            if preview["preview_sha256"] != preview_sha:
                return self._refused("confirmation_mismatch", "The confirmed preview is not this amendment's")
            draft = preview["draft"]
            expires = head.expires_monotonic_ns
            if "expires_in_ms" in change:
                expires = now + change["expires_in_ms"] * MS
                if expires >= head.expires_monotonic_ns:
                    return self._refused("expansion_not_permitted",
                                         "An amendment can only end a permission sooner")
            permission = self._issue(draft, None, preview_sha, head.actor, head.statement, head, now, expires)
            return {"outcome": {"kind": "amended", "permission": permission.to_json()},
                    "fields": {"permission": permission.to_json()}}

        return self._mutate("page", "amend", None, payload, inputs, None, "record", evaluate, increase=True)

    def trusted_dismiss_proposal(self, channel, payload) -> dict:
        self._check_channel(channel)
        self._page_inputs(payload, ("proposal_id",))
        proposal_id = _ident(payload["proposal_id"], "proposal_id", "p-")

        def evaluate(now):
            proposal = self._proposals.get(proposal_id)
            if proposal is None or proposal.state != "open":
                return self._refused("proposal_not_found", "No open proposal has that proposal_id")
            self._proposals[proposal_id] = replace(proposal, state="dismissed")
            self._log("proposal", {"proposal_id": proposal_id, "state": "dismissed"})
            return {"outcome": {"kind": "dismissed", "proposal_id": proposal_id}}

        return self._mutate("page", "dismiss_proposal", None, payload, {"proposal_id": proposal_id}, None,
                            "record", evaluate, increase=False)

    def reduce_hold(self, payload) -> dict:
        self._page_inputs(payload, ("resources",))
        triple = _triple(payload, required=False)
        names = payload["resources"]
        resources = (tuple(self._profile.order) if names == "all"
                     else self._resolve(names, whole_groups=False))
        inputs = {"triple": None if triple is None else list(triple), "resources": list(resources)}

        def evaluate(now):
            for rid in resources:
                self._take_reserved(rid, f"page-hold-{next(self._seq)}")
            # Holding a control takes back any outstanding invitation that covers it.
            withdrawn = [i.invitation_id for i in self._outstanding_invitations()
                         if set(i.scope) & set(resources)]
            for invitation_id in withdrawn:
                self._end_invitation(self._invitations[invitation_id], "cancelled", "held_by_human")
            return {"outcome": {"kind": "held", "resources": list(resources),
                                "waiting_for_pickup": [r for r in resources
                                                       if self._owners[r].kind == "pickup_pending"],
                                "invitations_withdrawn": withdrawn}}

        return self._mutate("public", "hold", None, payload, inputs, triple, "record", evaluate,
                            increase=False)

    def reduce_deny(self, payload) -> dict:
        self._page_inputs(payload, ("notice_id", "grant_id"))
        triple = _triple(payload, required=False)
        notice_id = _ident(payload["notice_id"], "notice_id", "n-")
        grant_id = _ident(payload["grant_id"], "grant_id", "g-")
        inputs = {"triple": None if triple is None else list(triple), "notice_id": notice_id,
                  "grant_id": grant_id}

        def evaluate(now):
            grant = self._grants.get(grant_id)
            notice = self._notices.get(notice_id)
            if grant is None or grant.state != "active" or notice is None or notice.grant_id != grant_id:
                return self._refused("grant_not_found", "That grant already ended")
            self._notices[notice_id] = replace(notice, state="denied")
            self._emit("notice_denied", notice_id=notice_id, grant_id=grant_id)
            self._log("notice_failed", {"notice_id": notice_id, "grant_id": grant_id, "state": "denied"})
            if grant.proactive:
                self._start_cooldown(grant.basis.permission_id, grant.resources, now)
            self._end_grant(grant, "notice_denied")
            return {"outcome": {"kind": "denied", "grant_id": grant_id, "notice_id": notice_id}}

        return self._mutate("public", "deny", None, payload, inputs, triple, "record", evaluate,
                            increase=False)

    def reduce_revoke_grant(self, payload) -> dict:
        self._page_inputs(payload, ("grant_id",))
        triple = _triple(payload, required=False)
        grant_id = _ident(payload["grant_id"], "grant_id", "g-")
        inputs = {"triple": None if triple is None else list(triple), "grant_id": grant_id}

        def evaluate(now):
            grant = self._grants.get(grant_id)
            if grant is None or grant.state != "active":
                return self._refused("grant_not_found", "That grant already ended")
            if grant.proactive:  # taking a claim back starts its cooldown, as Deny does
                self._start_cooldown(grant.basis.permission_id, grant.resources, now)
            self._end_grant(grant, "revoked_by_human")
            return {"outcome": {"kind": "revoked", "grant_id": grant_id}}

        return self._mutate("public", "revoke_grant", None, payload, inputs, triple, "record", evaluate,
                            increase=False)

    def reduce_withdraw_invitation(self, payload) -> dict:
        """Take back an outstanding invitation before the agent uses it."""
        self._page_inputs(payload, ("invitation_id",))
        triple = _triple(payload, required=False)
        invitation_id = _ident(payload["invitation_id"], "invitation_id", "i-")
        inputs = {"triple": None if triple is None else list(triple), "invitation_id": invitation_id}

        def evaluate(now):
            invitation = self._invitations.get(invitation_id)
            if invitation is not None and invitation.state == "consumed":
                # The agent claimed it first: withdrawing still means no, so that grant ends too.
                claimed = [g for g in self._active_grants() if g.basis.invitation_id == invitation_id]
                for grant in claimed:
                    self._end_grant(grant, "revoked_by_human")
                if claimed:
                    return {"outcome": {"kind": "revoked", "invitation_id": invitation_id,
                                        "grant_id": claimed[0].grant_id}}
            if invitation is None or invitation.state != "outstanding":
                return self._refused("invitation_not_found", "That invitation already ended")
            self._end_invitation(invitation, "cancelled", "withdrawn_by_human")
            return {"outcome": {"kind": "revoked", "invitation_id": invitation_id}}

        return self._mutate("public", "withdraw_invitation", None, payload, inputs, triple, "record",
                            evaluate, increase=False)

    def reduce_revoke_permission(self, payload) -> dict:
        self._page_inputs(payload, ("permission_id",))
        triple = _triple(payload, required=False)
        permission_id = _ident(payload["permission_id"], "permission_id", "perm-")
        inputs = {"triple": None if triple is None else list(triple), "permission_id": permission_id}

        def evaluate(now):
            permission = self._active_permission()
            if permission is None or permission.permission_id != permission_id:
                return self._refused("permission_not_active", "That permission is not active")
            self._end_permission(permission, "revoked", "revoked")
            self._revocations += 1
            return {"outcome": {"kind": "revoked", "permission_id": permission_id}}

        return self._mutate("public", "revoke_permission", None, payload, inputs, triple, "record", evaluate,
                            increase=False)

    def panic(self, payload) -> dict:
        """Stop agent control: end every grant and pending move, revoke the permission. Sends nothing."""
        payload = dict(payload or {})
        if "request_id" not in payload:
            payload["request_id"] = f"page-panic-{secrets.token_hex(12)}"  # never one a client could reuse
        self._page_inputs(payload, ())
        triple = _triple(payload, required=False)
        inputs = {"triple": None if triple is None else list(triple)}

        def evaluate(now):
            ended = [g.grant_id for g in self._active_grants()]
            permission = self._active_permission()
            self._end_all_grants("panic")
            for invitation in self._outstanding_invitations():
                self._end_invitation(invitation, "cancelled", "panic")
            # A pad held down through Stop hands nothing back when it is lifted.
            self._gestures = {gid: self._idle_gesture() for gid in self._gestures}
            if permission is not None:
                self._end_permission(permission, "revoked", "panic", "panic")
            self._revocations += 1
            permission_id = None if permission is None else permission.permission_id
            self._emit("panic", grants=ended, permission_id=permission_id)
            self._log("panic", {"grants": ended, "expected": None if triple is None else list(triple)})
            return {"outcome": {"kind": "panic", "grants": ended, "permission_id": permission_id}}

        return self._mutate("public", "panic", None, payload, inputs, triple, "record", evaluate,
                            increase=False)

    def adapter_event(self, event: dict) -> None:
        """Every adapter event enters here. A call from inside a broker section breaks the contract."""
        if self._lock_owner == threading.get_ident():
            # The lock is not reentrant: refuse instead of deadlocking. The action half reads the counter
            # and records the step that was in flight as outcome_unknown.
            self._sink_violations += 1
            raise AdapterContractError("The adapter called the broker from inside a broker section")
        with self._entry() as now:
            try:
                self._on_event(event, now)
            except Exception as fault:  # a host fault mid-event may leave authority half-changed
                self._after_fault(fault, self._fail_closed)
                raise
            self._commit(f"adapter:{event.get('type') if isinstance(event, dict) else 'invalid'}")

    def _on_event(self, event, now: int) -> None:
        kind = event.get("type") if isinstance(event, dict) else None
        if kind == "physical_input":
            self._physical_input(event, now)
        elif kind == "unknown_input":
            self._unknown_input(event, now)
        elif kind == "binding_changed":
            self._binding_changed(event, now)
        elif kind == "gesture":
            self._gesture(event, now)
        elif kind == "feedback":
            self._counters["feedback"] += 1  # echo of what was sent; never human movement
        elif kind == "heartbeat":
            self._heartbeat_ns = now
        elif kind == "readback":
            self._on_adapter_readback(event, now)
        elif kind == "ack":
            self._on_adapter_ack(event, now)
        elif kind == "dependency":
            self._dependency_event(event, now)
        elif kind == "epoch":
            self._epoch_event(event, now)
        else:
            # Not an adapter event (a Listen-shaped dict, say): counted, never authority or human input.
            self._counters["ignored_events"] += 1

    def _physical_input(self, event, now: int) -> None:
        touched = event.get("resources")
        if (not isinstance(touched, list) or not touched
                or any(not isinstance(r, str) or r not in self._owners for r in touched)):
            # Malformed: conservatively yield everything.
            self._unknown_input({"classification": "unknown", "raw": None}, now)
            return
        touched = tuple(dict.fromkeys(touched))
        closure = self._profile.yield_closure(touched)
        settled = [rid for rid in touched if self._owners[rid].kind == "pickup_pending"]
        self._take_back(closure, "human_input", now, settles=settled)
        for rid in closure:
            self._evidence[rid]["last_input_ns"] = now
        for rid in touched:
            evidence = self._evidence[rid]
            physical = event.get("physical_value")
            if type(physical) is int and self._profile.resources[rid]["control_kind"] == "absolute":
                evidence["physical_value"] = physical
            # A touch clears the ramp baseline: after it the agent must set before it ramps.
            evidence["commanded_value"] = evidence["commanded_grant"] = None
        for rid in settled:
            jump = {"software_before": event.get("software_before"),
                    "software_after": event.get("software_after")}
            self._emit("pickup_settled", resource=rid, how="touched", **jump)
            self._log("pickup", {"resource": rid, "state": "settled_by_touch", **jump})
        primary = touched[0]
        self._coalesced(("human_input", primary), "human_input", now, "human_input",
                        {"resources": list(closure), "address": event.get("address")},
                        resource=primary, resources=list(closure), address=event.get("address"))

    def _unknown_input(self, event, now: int) -> None:
        """Unknown or unqualified input yields the adapter's whole scope and opens an uncertainty window."""
        classification = event.get("classification") if event.get("classification") in (
            "unknown", "unqualified") else "unknown"
        scope = self._profile.controller_resources()
        self._take_back(scope, "unknown_input", now)
        for grant in self._active_grants():
            self._lose(grant, grant.resources, "unknown_input", release_owners=True, takeover=True)
        self._last_unknown_ns = now
        self._coalesced(("unknown_input", classification), "unknown_input", now, "unknown_input",
                        {"classification": classification, "address": event.get("address")},
                        classification=classification, address=event.get("address"))

    def _binding_changed(self, event, now: int) -> None:
        group = event.get("group")
        members = self._profile.conflict_groups.get(group)
        if members is None:
            self._unknown_input({"classification": "unknown"}, now)
            return
        self._take_back(tuple(members), "binding_changed", now)
        for rid in members:
            self._evidence[rid]["last_input_ns"] = now
            if rid in self._bindings and event.get("value") in self._profile.resources[rid]["states"]:
                self._bindings[rid] = event["value"]

    def _idle_gesture(self) -> dict:
        return {"state": "idle", "t0": None, "last_transition_ns": None}

    def _gesture(self, event, now: int) -> None:
        """Press/release timing is the broker's. Bounces, duplicates and lost releases never invite."""
        gid, phase = event.get("gesture_id"), event.get("phase")
        binding = self._profile.gesture_by_id.get(gid)
        if binding is None or phase not in ("press", "release"):
            self._unknown_input({"classification": "unknown"}, now)
            return
        state = self._gestures[gid]
        last = state["last_transition_ns"]
        if last is not None and now - last < binding["debounce_ms"] * MS:
            self._counters["gesture_bounces"] += 1
            return
        if phase == "press":
            if state["state"] == "pressed":
                self._counters["gesture_duplicates"] += 1
                return
            self._gestures[gid] = {"state": "pressed", "t0": now, "last_transition_ns": now}
            self._emit("gesture_pressed", gesture_id=gid)
            self._log("gesture", {"gesture_id": gid, "phase": "press"})
            return
        if state["state"] != "pressed":
            self._counters["ignored_releases"] += 1
            return
        held = now - state["t0"]
        self._gestures[gid] = {"state": "idle", "t0": None, "last_transition_ns": now}
        if held < binding["long_press_ms"] * MS:
            outcome, invitation = "short_press_ignored", None
        else:
            outcome, invitation = self._invite_by_gesture(gid, now)
        self._emit("gesture_released", gesture_id=gid, outcome=outcome, held_ms=held // MS,
                   invitation_id=None if invitation is None else invitation.invitation_id)
        self._log("gesture", {"gesture_id": gid, "phase": "release", "outcome": outcome,
                              "invitation_id": None if invitation is None else invitation.invitation_id})

    def _invite_by_gesture(self, gid: str, now: int):
        if self._stopped or self._journal_degraded:
            return "agent_control_stopped", None  # nothing is handed back, and holds stay
        for invitation in self._outstanding_invitations():
            if invitation.gesture_id == gid and invitation.adapter_epoch == self._adapter_epoch:
                updated = replace(invitation, coalesced_presses=invitation.coalesced_presses + 1)
                self._invitations[invitation.invitation_id] = updated
                return "coalesced", updated
        if len(self._outstanding_invitations()) >= MAX_INVITATIONS:
            return "invitation_limit_reached", None
        scope = self._profile.gesture_scope[gid]
        return "invitation_created", self._make_invitation("gesture", gid, scope, now)

    def _dependency_event(self, event, now: int) -> None:
        name, state = event.get("name"), event.get("state")
        if name not in self._profile.dependencies or state != "unavailable":
            # Restores arrive as epoch events, so an "available" here is contract noise.
            self._counters["ignored_events"] += 1
            return
        reason = self._dependency_reason(name)
        affected = [rid for rid in self._profile.order if name in self._profile.resources[rid]["depends_on"]]
        self._clear_authority("resource_unavailable", "invalidated")
        self._mark_unavailable(affected, reason)
        self._emit("resource_unavailable", resources=affected, reason=reason)
        self._log("dependency", {"name": name, "state": state, "resources": affected})

    def _epoch_event(self, event, now: int) -> None:
        epoch = event.get("adapter_epoch")
        if type(epoch) is not int or epoch <= self._adapter_epoch:
            self._counters["ignored_events"] += 1
            return
        if event.get("mapping_sha256") != self._profile.sha256:
            self._mapping_ok = False  # until a new host starts
        dependencies = event.get("dependencies") if isinstance(event.get("dependencies"), dict) else None
        reason = event.get("reason") if isinstance(event.get("reason"), str) else "restored"
        self._new_epoch(now, epoch, reason, dependencies)

    def tick(self) -> None:
        """The scheduler's entry (and tests'): due expiries first, then due steps."""
        with self._entry() as now:
            if self._active_runs():
                self._run_due(now)
                self._commit("tick")

    def shutdown(self, reason: str = "host_stopping") -> None:
        """End agent authority and pending moves; the person's controls and holds stay as they are."""
        with self._entry():
            self._stopped = True
            self._end_all_grants("host_stopping")
            for run in self._active_runs():  # a run still draining after its grant ended
                self._stop_run(run, "host_stopping", self._entry_now)
            for invitation in self._outstanding_invitations():
                self._end_invitation(invitation, "cancelled", "host_stopping")
            permission = self._active_permission()
            if permission is not None:
                self._end_permission(permission, "invalidated", reason)
            self._log("host_stopping", {"reason": reason})
            self._commit("host_stopping")


    def _action(self, value) -> dict:
        """Shape (invalid_arguments) and profile (unsupported_profile) checks for one act's move."""
        _fields(value, ("kind", "resource"), ("value", "target", "duration_ms", "delta", "state"), "action")
        kind = value["kind"]
        if kind not in ACTION_KINDS:
            raise _bad("action.kind must be set, ramp, nudge or set_state",
                       "Pass action as {kind: set, resource, value}, {kind: ramp, resource, target, "
                       "duration_ms}, {kind: nudge, resource, delta} or {kind: set_state, resource, state}.")
        needed = {"set": ("value",), "ramp": ("target", "duration_ms"), "nudge": ("delta",),
                  "set_state": ("state",)}[kind]
        _fields(value, ("kind", "resource", *needed), where=f"a {kind} action")
        rid = self._profile.resolve([value["resource"]])[0]
        d = self._profile.resources[rid]
        fits = {"set": ("absolute",), "ramp": ("absolute",), "nudge": ("relative",),
                "set_state": ("toggle", "state_setting")}[kind]
        if d["control_kind"] not in fits:
            raise _profile_error(f"A {kind} action does not fit {rid}, a {d['control_kind']} control",
                                 "set and ramp move absolute controls, nudge moves relative ones and "
                                 "set_state sets toggles; b2b_observe lists each resource's control_kind.")
        if not d["agent_writable"]:
            raise _profile_error(f"{rid} is never agent-writable",
                                 "b2b_observe marks the controls an agent may move with agent_writable.")
        action = {"kind": kind, "resource": rid}
        if kind in ("set", "ramp"):
            name = "value" if kind == "set" else "target"
            number = value[name]
            if type(number) is not int:
                raise _bad(f"action.{name} must be an integer", f"Pass action.{name} as a whole number.")
            low, high = d["range"]["minimum"], d["range"]["maximum"]
            if not low <= number <= high:
                raise _profile_error(f"action.{name} must be inside {rid}'s range {low}-{high}")
            action[name] = number
            if kind == "ramp":
                duration = value["duration_ms"]
                longest = self._settings["max_action_ms"]
                if type(duration) is not int:
                    raise _bad("action.duration_ms must be an integer",
                               f"Pass action.duration_ms as whole milliseconds from 50 to {longest}.")
                if not 50 <= duration <= longest:
                    raise _profile_error(f"A ramp lasts 50 to {longest} ms",
                                         f"Pass action.duration_ms from 50 to {longest}; split longer moves.")
                action["duration_ms"] = duration
        elif kind == "nudge":
            delta = value["delta"]
            if type(delta) is not int:
                raise _bad("action.delta must be an integer", "Pass action.delta as a signed whole number.")
            if abs(delta) > d["max_delta"]:
                raise _profile_error(f"A nudge on {rid} moves at most {d['max_delta']} ticks")
            action["delta"] = delta
        else:
            state = value["state"]
            if not isinstance(state, str) or state not in d["states"]:
                raise _profile_error(f"action.state must be one of {', '.join(d['states'])}")
            action["state"] = state
        return action

    def act_begin(self, session_id: str, request: dict) -> ActionRun:
        """Plan one bounded move as short steps; never blocks. No step goes out before the notice ack."""
        _fields(request, ("request_id", "expected_session_epoch", "expected_control_revision",
                          "expected_state_sha256", "grant_id", "action", "not_after_monotonic_ns"),
                ("max_evidence_age_ms",), "b2b_act")
        request_id = _request_id(request["request_id"])
        triple = _triple(request)
        grant_id = _ident(request["grant_id"], "grant_id", "g-")
        action = self._action(request["action"])
        not_after = _int(request["not_after_monotonic_ns"], "not_after_monotonic_ns", 0, 2**63 - 1)
        oldest = self._settings["max_evidence_age_ms"]
        max_age = _int(request.get("max_evidence_age_ms", oldest), "max_evidence_age_ms", 50, oldest)
        inputs = {"triple": list(triple), "grant_id": grant_id, "action": action,
                  "not_after_monotonic_ns": not_after, "max_evidence_age_ms": max_age}
        with self._entry() as now:
            self._check_session(session_id)
            hit, sha = self._lookup("agent", "act", session_id, request_id, inputs)
            if hit is None:
                self._check_triple(triple, "exact")
                if not_after > now + MAX_AHEAD_NS:
                    raise _bad("not_after_monotonic_ns is more than 60 s ahead of the host clock",
                               "Read monotonic_ns from b2b_observe and add at most 60000000000.")
                self._check_running()
                if self._journal_degraded:
                    refusal = self._unjournaled_refusal("agent", "act", request_id, sha, now, "exact", None)
                    return ActionRun(request_id, refusal)
                self._begin_request("agent", request_id, "act", sha)

                def plan_and_start(now):
                    planned = self._plan(request_id, grant_id, action, not_after, max_age, now)
                    if isinstance(planned, _Run):
                        self._start_run(planned, now)
                    else:
                        self._complete_request("agent", request_id,
                                               self._receipt("act", request_id, planned, now, "exact", None))

                self._evaluate("agent", "act", request_id, now, plan_and_start)
                self._commit("act")
                return ActionRun(request_id)
            if isinstance(hit, dict):
                return ActionRun(request_id, hit)
        return ActionRun(request_id, self._reread(hit))

    def act_result(self, run: ActionRun) -> dict | None:
        """The act's receipt once it is terminal, else None. Never waits."""
        if run.ready is not None:
            return run.ready
        with self._lock:
            result, hit = self._run_result(run.request_id)
        return result if hit is None else self._reread(hit)

    def act_wait(self, run: ActionRun, timeout_s: float) -> dict | None:
        """Wait once on ``changed`` (the lock is released while waiting); the host bounds the total wait."""
        if run.ready is not None:
            return run.ready
        with self._lock:
            result, hit = self._run_result(run.request_id)
            if result is None and hit is None:
                self.changed.wait(timeout_s)
                result, hit = self._run_result(run.request_id)
        return result if hit is None else self._reread(hit)

    def cancel_run(self, run: ActionRun, reason: str = "host_wait_exceeded") -> None:
        """Stop an act before its next step, for example when the host stops waiting for it."""
        if reason not in CANCEL_REASONS:
            raise ValueError(f"Unknown b2b cancel reason {reason}")
        with self._entry() as now:
            state = self._runs.get(run.request_id)
            if state is not None and state.active:
                self._stop_run(state, reason, now)
            self._commit("cancel_run")

    def wait_for_event(self, after_sequence: int, timeout_s: float) -> int:
        """The page long-poll: wait once on ``changed`` until the event sequence passes a cursor."""
        with self._lock:
            if self._event_seq <= after_sequence:
                self.changed.wait(timeout_s)
            return self._event_seq

    def _run_result(self, request_id: str) -> tuple[dict | None, _Reread | None]:
        run = self._runs.get(request_id)
        if run is not None:
            return (None if run.active else _plain(run.receipt)), None
        cached = self._receipts.get(("agent", request_id))
        if cached is not None:
            return _plain(cached), None
        entry = self._requests["agent"].get(request_id)
        if entry is not None and entry.state == "complete" and entry.journal_offset is not None:
            return None, _Reread(entry.journal_offset, entry.receipt_sha256)
        return None, None

    def _plan(self, request_id, grant_id, action, not_after, max_age, now) -> _Run | dict:
        """The act's preconditions and its steps, or a recorded refusal. Nothing is sent here."""
        grant = self._grants.get(grant_id)
        if grant is None:
            return self._refused("grant_not_found", "No grant has that grant_id")
        if grant.state != "active":
            return self._refused("grant_not_active", f"The grant ended: {grant.end_reason}")
        rid = action["resource"]
        if rid not in grant.resources:
            return self._refused("resource_not_granted", f"{rid} is not held by this grant")
        if any(r.grant_id == grant_id for r in self._active_runs()):
            return self._refused("action_in_progress", "Another act is still running on this grant")
        if not_after <= now or now >= grant.action_deadline_monotonic_ns:
            return self._refused("deadline_passed", "The act's or the grant's deadline is not after the host "
                                                    "clock")
        permission = None
        if grant.proactive:
            permission = self._active_permission()
            if permission is None or action["kind"] not in permission.draft["action_kinds"]:
                return self._refused("action_not_permitted",
                                     f"The permission does not allow {action['kind']} moves")
        d = self._profile.resources[rid]
        health = self._health()
        if health is None:
            return self._refused("adapter_unavailable", "The adapter's health could not be read")
        dependencies, heartbeat = health
        try:
            readback = self._adapter.readback(rid) if d["readback"] in ("numeric", "state") else None
        except Exception as error:  # noqa: BLE001 - a broken adapter must never leave this request running
            return self._refused("adapter_unavailable",
                                 f"The adapter could not be read: {type(error).__name__}")
        if readback is not None and not _valid_readback(readback, d):
            return self._refused("adapter_unavailable", "The adapter returned a malformed readback")
        down = [n for n in d["depends_on"] if dependencies.get(n) != "available"]
        if down:
            return self._refused("adapter_unavailable", f"{down[0]} is unavailable")
        if heartbeat is None or now - heartbeat > max_age * MS:
            return self._refused("evidence_stale", "The adapter heartbeat is older than max_evidence_age_ms")
        fresh = (readback if readback is not None and now - readback.observed_monotonic_ns <= max_age * MS
                 else None)
        kind, values = action["kind"], ()
        if kind == "set":
            values = (action["value"],)
            messages, final = ({"kind": "set", "value": action["value"]},), action["value"]
        elif kind == "ramp":
            if d["readback"] == "numeric":
                if readback is None:
                    return self._refused("baseline_unknown", f"{rid} has reported no value to ramp from")
                if fresh is None:
                    return self._refused("evidence_stale",
                                         f"{rid}'s readback is older than max_evidence_age_ms")
                start = fresh.value
            else:
                start = self._evidence[rid]["commanded_value"]
                if start is None:
                    return self._refused("baseline_unknown",
                                         f"No value was commanded on {rid} in this epoch since the last "
                                         "touch; set it before ramping")
            target = action["target"]
            interval = self._profile.step_interval_ms
            count = max(-(-action["duration_ms"] // interval), -(-abs(target - start) // d["max_delta"]))
            values = tuple(start + _round_div((target - start) * (i + 1), count) for i in range(count))
            messages, final = tuple({"kind": "set", "value": v} for v in values), target
        elif kind == "nudge":
            messages, final = ({"kind": "nudge", "delta": action["delta"]},), None
        else:
            final = action["state"]
            if d["control_kind"] == "toggle":
                if fresh is None:
                    return self._refused("state_unknown",
                                         f"{rid} has no fresh state readback; nothing was sent")
                messages = () if fresh.value == final else ({"kind": "toggle"},)
            else:
                messages = ({"kind": "set_state", "state": final},)
        limit = None if permission is None else permission.limit(rid)
        if limit is not None and any(not limit[0] <= v <= limit[1] for v in values):
            low, high = limit
            return self._refused("outside_limits", f"The permission keeps {rid} inside {low}-{high}; "
                                                   "nothing is clamped")
        run = _Run(request_id, grant_id, self.session_epoch, rid, action, messages, final, d["readback"],
                   not_after, max_age, now)
        if not messages:
            # Already in the asked-for state: verified by that readback, nothing sent.
            return replace(run, state="draining", verified=True, drain_until_ns=now,
                           evidence_refs=self._readback_ref(run, fresh))
        return run

    def _start_run(self, run: _Run, now: int) -> None:
        self._runs[run.request_id] = run
        self._live_runs[run.request_id] = None
        self._notify = True
        if run.state == "draining":
            self._conclude(run, now)
            return
        grant = self._grants[run.grant_id]
        notice = self._notices.get(grant.notice_id)
        if notice is not None and notice.state == "acknowledged":
            self._runs[run.request_id] = replace(run, state="running", t0=now)
            self._advance_run(run.request_id, now)

    def _trim_runs(self, keep: str) -> None:
        """Bound finished-run history; the run that just finished stays, its caller still reads it."""
        ended = [rid for rid, r in self._runs.items() if not r.active and rid != keep]
        for rid in ended[:max(0, len(ended) + 1 - MAX_RUN_HISTORY)]:
            del self._runs[rid]

    def _advance_run(self, request_id: str, now: int) -> None:
        """Admit the run's due step, if any. Step windows never overlap, so a late tick cannot burst."""
        run = self._runs.get(request_id)
        interval = self._profile.step_interval_ms * MS
        while run is not None and run.state == "running" and run.submitted < len(run.messages):
            if now < run.t0 + run.submitted * interval:
                return
            if not self._admit_step(run, now):
                return
            run = self._runs.get(request_id)

    def _admit_step(self, run: _Run, now: int) -> bool:
        """The admission checklist, then submit, as one critical section. The only caller of submit."""
        interval = self._profile.step_interval_ms * MS
        index = run.submitted
        latest = run.t0 + index * interval + interval
        rid = run.resource
        grant = self._grants.get(run.grant_id)
        owner = self._owners[rid]
        reason = detail = None
        if self._stopped:
            reason = "host_stopping"
        elif (grant is None or run.session_epoch != self.session_epoch
                or grant.adapter_epoch != self._adapter_epoch):
            reason = "epoch_changed"
        elif grant.state != "active":
            reason = grant.lost_reason(rid) or "released"
        else:
            notice = self._notices.get(grant.notice_id)
            if notice is None or notice.state != "acknowledged":
                if notice is not None and notice.state == "pending" and now < notice.deadline_monotonic_ns:
                    return False  # the run waits for the page
                reason = {"deadline_missed": "notice_deadline_missed", "denied": "notice_denied"}.get(
                    None if notice is None else notice.state, "notice_delivery_unknown")
        if reason is None:
            if now >= grant.lease_expires_monotonic_ns:
                reason = "lease_expired"
            elif now >= min(grant.action_deadline_monotonic_ns, run.not_after_monotonic_ns):
                reason = "deadline_passed"
            elif now >= latest:
                reason = "step_late"  # never caught up by bursting
        if reason is None and (owner.kind != "agent" or owner.grant_id != grant.grant_id
                               or owner.generation != grant.generation(rid)):
            cause = owner.cause if owner.cause in CANCEL_REASONS else "human_input"
            reason = grant.lost_reason(rid) or cause
        if reason is None and grant.proactive:
            p = self._active_permission()
            if (p is None or p.permission_id != grant.basis.permission_id
                    or p.version != grant.basis.permission_version
                    or p.revocation_generation != grant.basis.revocation_generation
                    or p.expires_monotonic_ns <= now):
                reason = "permission_ended"
        health = None
        if reason is None:
            health = self._health()
            if health is None:
                reason, detail = "evidence_stale", "The adapter's health could not be read"
            elif health[1] is None or now - health[1] > run.max_evidence_age_ms * MS:
                reason = "evidence_stale"
        if reason is None:
            if any(gs["state"] == "pressed" for gs in self._gestures.values()):
                reason = "human_input"
            elif self._last_unknown_ns is not None and self._last_unknown_ns >= grant.created_monotonic_ns:
                reason = "unknown_input"
        if reason is None:
            dependencies = health[0]
            if any(dependencies.get(n) != "available" for n in self._profile.resources[rid]["depends_on"]):
                reason = "resource_unavailable"
        if reason is not None:
            self._stop_run(run, reason, now, detail=detail)
            return False
        message = run.messages[index]
        deadline = min(latest, grant.lease_expires_monotonic_ns, grant.action_deadline_monotonic_ns,
                       run.not_after_monotonic_ns)
        if self._log("step_admitted", {"request_id": run.request_id, "step": index + 1,
                                       "count": len(run.messages), "resource": rid, "message": message,
                                       "generation": owner.generation, "grant_id": grant.grant_id,
                                       "deadline_monotonic_ns": deadline}) is None:
            self._stop_run(run, "journal_unavailable", now)
            return False
        self._emit("step_admitted", request_id=run.request_id, grant_id=grant.grant_id, resource=rid,
                   step=index + 1, count=len(run.messages))
        step = Step(run.request_id, index, len(run.messages), rid, dict(message))
        admission = Admission(grant.grant_id, self.session_epoch, self._adapter_epoch, self._revision,
                              owner.generation, deadline)
        violations = self._sink_violations
        try:
            submission = self._adapter.submit(step, admission)
            if not _valid_submission(submission):
                # The step may already have gone out; never treat this as "not sent" or send it again.
                raise AdapterContractError(f"submit returned {type(submission).__name__}, not a Submission")
        except AdapterBlocked as error:
            self._stop_run(run, "adapter_blocked", now, detail=str(error))
            return False
        except AdapterRefused as error:
            late = self._clock.now_ns() >= deadline  # it reached the adapter after its window closed
            self._stop_run(run, "step_late" if late else "adapter_refused", now, detail=str(error))
            return False
        except Exception as error:  # noqa: BLE001 - a contract break mid-submit: this step may have gone out
            self._runs[run.request_id] = run = replace(run, uncertain_step=index + 1)
            self._unknown(run, now, f"The adapter raised {type(error).__name__} during submit of step "
                                    f"{index + 1}; it may or may not have been sent")
            return False
        ack_due = now + self._settings["ack_timeout_ms"] * MS
        run = replace(run, submitted=index + 1, last_submit_ns=now, last_ack=submission.ack,
                      acknowledged=run.acknowledged + (submission.ack == "ok"),
                      pending=run.pending + (((submission.submission_id, ack_due),)
                                             if submission.ack == "pending" else ()))
        self._runs[run.request_id] = run
        logged = self._log("step_submitted", {"request_id": run.request_id, "step": index + 1,
                                              "submission_id": submission.submission_id,
                                              "ack": submission.ack})
        if message["kind"] == "set":
            self._record_commanded(rid, grant.grant_id, message["value"])
        if submission.readback is not None:
            run = self._note_readback(run, submission.readback)
        if self._sink_violations != violations:
            self._unknown(run, now, "The adapter called the broker during submit")
        elif logged is None:
            self._unknown(run, now, "The journal failed after this step was submitted")
        elif submission.ack == "failed":
            self._unknown(run, now, f"The adapter reported a failed ack for step {index + 1}")
        elif run.submitted == len(run.messages):
            run = replace(run, state="draining", drain_until_ns=ack_due)
            self._runs[run.request_id] = run
            self._conclude(run, now)
        return True

    def _read_back(self, rid: str) -> Readback | None:
        """The adapter's readback for evidence, or None when it can't be read as declared."""
        try:
            readback = self._adapter.readback(rid)
        except Exception:  # noqa: BLE001 - an adapter fault reads as no readback, never as a value
            return None
        return readback if _valid_readback(readback, self._profile.resources[rid]) else None

    def _health(self) -> tuple[dict, int | None] | None:
        """The adapter's dependencies and heartbeat, or None when health can't be read as declared."""
        try:
            health = self._adapter.health()
        except Exception:  # noqa: BLE001 - an adapter fault reads as missing evidence, never as health
            return None
        dependencies = health.get("dependencies") if isinstance(health, dict) else None
        if not isinstance(dependencies, dict):
            return None
        heartbeat = health.get("heartbeat_monotonic_ns")
        return dependencies, heartbeat if type(heartbeat) is int else None

    def _readback_ref(self, run: _Run, readback: Readback) -> tuple:
        ref = self._log("readback", {"request_id": run.request_id, "resource": run.resource,
                                     "value": readback.value, "scope": readback.scope,
                                     "observed_monotonic_ns": readback.observed_monotonic_ns})
        return () if ref is None else (f"journal:{ref.seq}",)

    def _note_readback(self, run: _Run, readback: Readback) -> _Run:
        """A control readback observed after the last submission may verify the final value."""
        if (run.final is None or run.submitted < len(run.messages) or run.last_submit_ns is None
                or readback.observed_monotonic_ns < run.last_submit_ns):
            return run
        if run.readback_kind == "state":
            matches = readback.value == run.final
        else:
            matches = type(readback.value) is int and abs(readback.value - run.final) <= VERIFY_TOLERANCE
        run = replace(run, evidence_refs=run.evidence_refs + self._readback_ref(run, readback),
                      verified=run.verified or matches, mismatch=None if matches else readback.value)
        self._runs[run.request_id] = run
        return run

    def _conclude(self, run: _Run, now: int, *, force: bool = False) -> None:
        """Every step went out: finish once acks and readback settle, or at the drain deadline."""
        run = self._runs[run.request_id]
        if run.state != "draining":
            return
        expired = force or now >= run.drain_until_ns
        if run.pending:
            if expired:
                self._unknown(run, now, f"No adapter ack for {len(run.pending)} submitted step(s) within "
                                        "ack_timeout_ms")
            return
        awaiting_readback = run.readback_kind in ("numeric", "state") and run.final is not None
        if awaiting_readback and not run.verified and not expired:
            return
        if self._journal.state != "ok":
            # Steps went out but their record (or their evidence) may be incomplete: never claim an outcome.
            self._unknown(run, now, "The journal became unavailable after a step was submitted")
            return
        if run.verified:
            self._finish(run, {"kind": "verified", "evidence_refs": list(run.evidence_refs)}, now)
            return
        warnings = ([] if run.mismatch is None else
                    [f"readback_mismatch: {run.resource} read back {run.mismatch!r}, not {run.final!r}"])
        self._finish(run, {"kind": "dispatched_unverified", "request_id": run.request_id}, now,
                     warnings=warnings,
                     uncertainty=["The control was dispatched; no readback confirmed the final value."])

    def _stop_run(self, run: _Run, reason: str, now: int, *, detail: str | None = None) -> None:
        """Stop before the next step. What already went out keeps its own outcome; nothing is undone."""
        run = self._runs[run.request_id]
        if not run.active:
            return
        if run.submitted and (reason == "journal_unavailable" or self._journal.state != "ok"):
            self._unknown(run, now, "The journal became unavailable after a step was submitted")
            return
        if run.state == "draining":
            # Every step already went out, so its acks and readback still decide the outcome. Only the
            # host giving up on the run concludes it now.
            if reason in ("host_stopping", "host_wait_exceeded"):
                self._conclude(run, now, force=True)
            return
        self._log("step_cancelled", {"request_id": run.request_id, "from_step": run.submitted + 1,
                                     "count": len(run.messages), "reason": reason, "detail": detail})
        self._emit("step_cancelled", request_id=run.request_id, grant_id=run.grant_id, resource=run.resource,
                   from_step=run.submitted + 1, reason=reason)
        if run.submitted == 0:
            outcome = {"kind": "cancelled_before_dispatch", "reason": reason}
        else:
            outcome = {"kind": "partially_dispatched", "last_step": run.submitted, "cancel_reason": reason,
                       "observation_required": True}
        self._finish(run, outcome, now, warnings=[] if detail is None else [f"{reason}: {detail}"])

    def _unknown(self, run: _Run, now: int, detail: str) -> None:
        """Dispatch may have happened: never retried, and the grant ends."""
        self._finish(run, {"kind": "outcome_unknown", "request_id": run.request_id, "automatic_retry": False},
                     now, warnings=[detail])
        grant = self._grants.get(run.grant_id)
        if grant is not None and grant.state == "active":
            self._end_grant(grant, "outcome_unknown")

    def _finish(self, run: _Run, outcome: dict, now: int, *, warnings=(), uncertainty=()) -> None:
        run = self._runs[run.request_id]
        grant = self._grants.get(run.grant_id)
        notice = None if grant is None else self._notices.get(grant.notice_id)
        steps = {"planned": len(run.messages), "submitted": run.submitted, "acknowledged": run.acknowledged,
                 "dispatched_steps": run.submitted, "pending_acks": len(run.pending),
                 "cancelled_from": run.submitted + 1 if run.submitted < len(run.messages) else None,
                 "uncertain_step": run.uncertain_step, "last_ack": run.last_ack}
        result = {"outcome": outcome, "warnings": list(warnings), "uncertainty": list(uncertainty),
                  "fields": {"grant_id": run.grant_id, "action": run.action, "steps": steps,
                             "notice": None if notice is None else notice.to_json()}}
        made = self._receipt("act", run.request_id, result, now, "exact", None)
        self._runs[run.request_id] = replace(run, state="terminal", receipt=made)
        self._live_runs.pop(run.request_id, None)
        self._complete_request("agent", run.request_id, made)
        self._emit("action_completed", request_id=run.request_id, grant_id=run.grant_id,
                   resource=run.resource, outcome=outcome["kind"], dispatched_steps=run.submitted,
                   planned_steps=len(run.messages))
        self._notify = True
        self._trim_runs(keep=run.request_id)

    def _active_runs(self) -> list[_Run]:
        return [self._runs[request_id] for request_id in self._live_runs]

    def _on_grant_lost(self, grant: Grant, resources, reason: str, now: int) -> None:
        """A grant lost these resources (narrowed or ended): its runs on them stop before the next step."""
        for run in self._active_runs():
            if run.grant_id == grant.grant_id and run.resource in resources:
                self._stop_run(run, reason, now)

    def _on_notice_acknowledged(self, notice: Notice, now: int) -> None:
        """A valid ack: the grant's waiting run starts, and its first step goes in this same section."""
        for run in self._active_runs():
            if run.grant_id == notice.grant_id and run.state == "awaiting_notice":
                self._runs[run.request_id] = replace(run, state="running",
                                                     t0=max(run.created_monotonic_ns, now))
                self._notify = True
                self._advance_guarded(run.request_id, now)

    def _run_due(self, now: int) -> None:
        """From ``tick``, after every expiry due by ``now``: admit due steps."""
        for run in self._active_runs():
            if run.state != "running":
                continue
            self._advance_guarded(run.request_id, now)

    def _advance_guarded(self, request_id: str, now: int) -> None:
        """Admit due steps. A fault here may follow a submit: fail closed, then let the host report it."""
        try:
            self._advance_run(request_id, now)
        except Exception as fault:
            self._after_fault(fault, self._fail_closed)
            raise

    def _on_adapter_ack(self, event: dict, now: int) -> None:
        sid, status = event.get("submission_id"), event.get("status")
        for run in self._active_runs():
            if any(pending == sid for pending, _ in run.pending):
                ok = status == "ok"
                run = replace(run, pending=tuple(p for p in run.pending if p[0] != sid),
                              acknowledged=run.acknowledged + ok, last_ack="ok" if ok else "failed")
                self._runs[run.request_id] = run
                self._log("step_ack", {"request_id": run.request_id, "submission_id": sid, "status": status})
                if ok:
                    self._conclude(run, now)
                else:
                    self._unknown(run, now, f"The adapter reported a failed ack for {sid}")
                return
        self._counters["late_adapter_acks"] = self._counters.get("late_adapter_acks", 0) + 1

    def _on_adapter_readback(self, event: dict, now: int) -> None:
        rid = event.get("resource")
        readback = Readback(event.get("value"), event.get("scope"), event.get("observed_monotonic_ns"))
        if rid not in self._evidence or not _valid_readback(readback, self._profile.resources[rid]):
            self._counters["ignored_events"] += 1  # malformed or not this control's kind: never a value
            return
        self._evidence[rid]["readback"] = {"value": readback.value, "scope": readback.scope,
                                           "observed_monotonic_ns": readback.observed_monotonic_ns}
        for run in self._active_runs():
            if run.resource == rid and run.state == "draining":
                self._conclude(self._note_readback(run, readback), now)

    def _record_commanded(self, rid: str, grant_id: str, value) -> None:
        """A step on ``rid`` was submitted: the ramp baseline for a commanded-only control.

        The pickup rule compares this value with the fader when the grant ends.
        """
        self._evidence[rid].update(commanded_value=value, commanded_grant=grant_id)

    def _action_timers(self, now: int) -> list:
        """Due (due_ns, 7, seq, kind, key) items: ack timeouts, drain deadlines and act deadlines."""
        due = []
        for i, run in enumerate(self._active_runs()):
            for sid, at in run.pending:
                if at <= now:
                    due.append((at, 7, i, "ack_timeout", (run.request_id, sid)))
            if run.state == "draining" and run.drain_until_ns <= now:
                due.append((run.drain_until_ns, 7, i, "drain", run.request_id))
            if run.state in ("awaiting_notice", "running"):
                grant = self._grants.get(run.grant_id)
                limit = run.not_after_monotonic_ns if grant is None else min(
                    grant.action_deadline_monotonic_ns, run.not_after_monotonic_ns)
                if limit <= now:
                    due.append((limit, 7, i, "act_deadline", run.request_id))
        return due

    def _fire_action(self, kind: str, key, at: int, now: int) -> None:
        request_id = key[0] if kind == "ack_timeout" else key
        run = self._runs.get(request_id)
        if run is None or not run.active:
            return
        if kind == "ack_timeout":
            if any(sid == key[1] for sid, _ in run.pending):
                self._unknown(run, now, f"No adapter ack for {key[1]} within ack_timeout_ms")
        elif kind == "drain":
            self._conclude(run, now)
        elif kind == "act_deadline" and run.state in ("awaiting_notice", "running"):
            self._stop_run(run, "deadline_passed", now)
