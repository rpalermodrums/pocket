# SPDX-License-Identifier: AGPL-3.0-only
"""b2b adapter contract, resource profiles and the built-in synthetic two-deck mixer.

An adapter declares its controls as a profile: canonical resources, the aliases that name them,
the physical messages that move them, gestures, and the groups that must yield or be claimed
together. The alias table is inside the profile, so it is part of the mapping hash, and every
name resolves to its canonical resource before anything reads or changes an owner.

Contract: ``submit`` is bounded, never blocks and never calls the sink. ``pump`` and the
injection methods collect what to deliver under the adapter's own lock, release it, and only
then call the sink. Lock order is always the broker lock, then the adapter lock; the adapter
never takes the broker lock while it holds its own.
"""
from __future__ import annotations

import json
import re
import threading
from collections import deque
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from .artifact_store import canonical_bytes, digest
from .errors import PocketError

PROFILE_SCHEMA = "pocket.b2b-adapter-profile/v1"
CONTROL_KINDS = ("absolute", "relative", "toggle", "state_setting")
UNITS = {"absolute": "midi_7bit", "relative": "relative_ticks", "toggle": "state", "state_setting": "state"}
READBACKS = {"absolute": ("numeric", "commanded_only", "none"), "relative": ("commanded_only", "none"),
             "toggle": ("state", "none"), "state_setting": ("state", "none")}
PICKUPS = ("explicit_pickup_required", "not_applicable")
AUDIBILITY = ("audible", "uncertain", "inaudible")
ENCODINGS = {"absolute_7bit": ("cc", "absolute"), "relative_twos_complement": ("cc", "relative"),
             "note_press": ("note", "toggle"), "note_toggle_route": ("note", "state_setting")}
CLASSIFICATIONS = ("control", "gesture", "unqualified")
DEPENDENCY_STATES = ("available", "unavailable")
ACK_MODES = ("inline", "deferred", "never", "failed")
READBACK_MODES = ("fresh", "stale", "mismatch")
_PROFILE_FIELDS = frozenset({"schema", "profile", "native", "resources", "aliases", "physical_inputs",
                             "gestures", "yield_groups", "conflict_groups", "dependencies",
                             "step_interval_ms"})
_DESCRIPTOR_FIELDS = frozenset({"resource_id", "label", "control_kind", "unit", "range", "states",
                                "max_delta", "readback", "motorized", "pickup", "step", "audibility",
                                "routes", "depends_on", "shared_bus", "conflict_group", "yield_group",
                                "binding", "agent_writable"})
_INPUT_FIELDS = frozenset({"address", "message", "encoding", "classification"})
_GESTURE_FIELDS = frozenset({"gesture_id", "label", "message", "press_value", "release_value", "debounce_ms",
                             "long_press_ms", "max_hold_ms", "scope"})
_NAME = re.compile(r"[a-z0-9][a-z0-9._/:-]{0,127}")
_PROFILE_HINT = "b2b_observe lists each declared resource and its aliases."
_MAX_RESOURCES, _MAX_SCOPE = 64, 16


class AdapterBlocked(Exception):
    """The adapter can't accept a step now; nothing was submitted."""


class AdapterRefused(Exception):
    """The step names another epoch, a passed deadline or an undeclared move; nothing was submitted."""


class AdapterContractError(Exception):
    """A caller broke the adapter contract, for example a sink entered from inside a broker section."""


@dataclass(frozen=True, slots=True)
class Readback:
    value: int | str
    scope: str  # "control_readback": the control reported it; it says nothing about audio
    observed_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class Step:
    request_id: str
    index: int  # 0-based
    count: int
    resource: str  # canonical id
    message: dict  # {kind: set, value} | {kind: nudge, delta} | {kind: set_state, state} | {kind: toggle}


@dataclass(frozen=True, slots=True)
class Admission:
    grant_id: str
    session_epoch: str
    adapter_epoch: int
    control_revision: int
    generation: int
    deadline_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class Submission:
    submission_id: str
    submitted_monotonic_ns: int
    ack: str  # ok | failed | pending (a pending ack arrives later through pump)
    readback: Readback | None


class AdapterSink(Protocol):
    def adapter_event(self, event: dict) -> None: ...


class Adapter(Protocol):
    """What the broker needs from an adapter. Lock order is the broker lock, then the adapter lock."""

    profile: dict  # a pocket.b2b-adapter-profile/v1 document; the broker validates it at host start

    def attach(self, sink: AdapterSink, clock) -> None: ...
    def epoch(self) -> int: ...
    def health(self) -> dict: ...  # {dependencies: {name: available|unavailable}, heartbeat_monotonic_ns}
    def readback(self, resource: str) -> Readback | None: ...  # in memory only; no device I/O
    def submit(self, step: Step, admission: Admission) -> Submission: ...  # bounded; never calls the sink
    def pump(self, now_ns: int) -> None: ...  # deliver deferred acks, readback and heartbeat through the sink
    def renew_epoch(self, reason: str) -> int: ...  # the broker starts an epoch; never calls the sink
    def close(self) -> None: ...


def _refuse(message: str, hint: str = _PROFILE_HINT) -> PocketError:
    return PocketError(message, code="unsupported_profile", hint=hint)


def mapping_sha256(profile: dict) -> str:
    """The mapping identity: the whole profile, alias table included."""
    return digest(profile)


def message_key(message) -> tuple[str, int, int]:
    if (not isinstance(message, dict) or set(message) != {"type", "channel", "number"}
            or message["type"] not in ("cc", "note")
            or type(message["channel"]) is not int or not 1 <= message["channel"] <= 16
            or type(message["number"]) is not int or not 0 <= message["number"] <= 127):
        raise _refuse("A profile message needs type cc or note, channel 1-16 and number 0-127")
    return message["type"], message["channel"], message["number"]


def parse_raw(raw) -> tuple[tuple[str, int, int], int] | None:
    """Return ((type, channel, number), value) for a three-byte cc or note message, else None."""
    if (not isinstance(raw, (list, tuple)) or len(raw) != 3 or any(type(b) is not int for b in raw)
            or not 0x80 <= raw[0] <= 0xFF or not all(0 <= b <= 127 for b in raw[1:])):
        return None
    status, number, value = raw
    channel = (status & 0x0F) + 1
    if status >> 4 == 0xB:
        return ("cc", channel, number), value
    if status >> 4 == 0x9:
        return ("note", channel, number), value
    if status >> 4 == 0x8:
        return ("note", channel, number), 0
    return None


def raw_message(message: dict, value: int) -> list[int]:
    """Build the three bytes a controller sends for a profile message (a note value 0 is a release)."""
    kind, channel, number = message_key(message)
    return [(0xB0 if kind == "cc" else 0x90) | (channel - 1), number, value]


def _ms(value, name, minimum, maximum) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _refuse(f"{name} must be a whole number of milliseconds from {minimum} to {maximum}")
    return value


def _names(value, what, *, minimum=1, maximum=_MAX_SCOPE) -> tuple[str, ...]:
    if (not isinstance(value, list) or not minimum <= len(value) <= maximum
            or any(not isinstance(v, str) or not v for v in value) or len(set(value)) != len(value)):
        raise _refuse(f"{what} must list {minimum} to {maximum} distinct names")
    return tuple(value)


def _frozen(value):
    """A read-only view of JSON data, so a validated profile can't drift from its hash."""
    if isinstance(value, dict):
        return MappingProxyType({k: _frozen(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_frozen(v) for v in value)
    return value


@dataclass(frozen=True, slots=True)
class Profile:
    """A validated adapter profile: alias resolution, group closure and message lookup."""

    canonical_json: bytes  # the validated document's canonical JSON; mapping_sha256 hashes it
    name: str
    sha256: str
    dependencies: tuple
    step_interval_ms: int
    order: tuple  # canonical ids in declared order
    resources: MappingProxyType  # canonical id -> descriptor (read-only)
    aliases: MappingProxyType  # alias -> canonical id
    yield_groups: MappingProxyType  # group id -> members
    conflict_groups: MappingProxyType
    yield_group_of: MappingProxyType  # canonical id -> group id
    conflict_group_of: MappingProxyType
    inputs: MappingProxyType  # (type, channel, number) -> physical input
    gestures: MappingProxyType  # (type, channel, number) -> gesture binding
    gesture_by_id: MappingProxyType
    gesture_scope: MappingProxyType  # gesture id -> canonical scope

    @property
    def document(self) -> dict:
        """A fresh copy of the validated profile document."""
        return json.loads(self.canonical_json)

    def canonical(self, name) -> str | None:
        if not isinstance(name, str):
            return None
        return name if name in self.resources else self.aliases.get(name)

    def resolve(self, names) -> tuple[str, ...]:
        """Resolve aliases, then dedupe: a control's physical and software names are one resource."""
        if not isinstance(names, (list, tuple)) or any(not isinstance(n, str) for n in names):
            raise PocketError("Resources are a list of resource names", code="invalid_arguments",
                              hint="Pass resource names as a JSON list of strings; b2b_observe lists them.")
        resolved = []
        for name in names:
            canonical = self.canonical(name)
            if canonical is None:
                raise _refuse(f"{name!r} is not a declared resource or alias")
            if canonical not in resolved:
                resolved.append(canonical)
        return tuple(resolved)

    def conflict_missing(self, resources) -> tuple[str, ...]:
        """Members of the named resources' conflict groups that the request left out."""
        missing = []
        for rid in resources:
            group = self.conflict_group_of.get(rid)
            if group is not None:
                missing += [m for m in self.conflict_groups[group] if m not in resources and m not in missing]
        return tuple(missing)

    def conflict_closure(self, resources) -> tuple[str, ...]:
        return tuple(dict.fromkeys([*resources, *self.conflict_missing(resources)]))

    def yield_closure(self, resources) -> tuple[str, ...]:
        """What a physical touch takes back: the yield group, and the whole conflict group for a binding."""
        expanded = list(resources)
        for rid in resources:
            group = self.yield_group_of.get(rid)
            if group is not None:
                expanded += self.yield_groups[group]
            if self.resources[rid]["binding"]:
                expanded += self.conflict_groups[self.conflict_group_of[rid]]
        return tuple(dict.fromkeys(expanded))

    def controller_resources(self) -> tuple[str, ...]:
        """Every resource a physical input can reach, with its groups: unknown input yields all of them."""
        reached = [self.canonical(i["address"]) for i in self.inputs.values()
                   if i["classification"] == "control"]
        closed = set(self.yield_closure(self.conflict_closure(tuple(dict.fromkeys(reached)))))
        return tuple(r for r in self.order if r in closed)

    def descriptor(self, rid: str):
        return self.resources[rid]

    def label(self, rid: str) -> str:
        return self.resources[rid]["label"]

    def aliases_of(self, rid: str) -> tuple[str, ...]:
        return tuple(sorted(a for a, t in self.aliases.items() if t == rid))

    def describe(self) -> list[dict]:
        """The public view of every resource, in declared order, as fresh JSON values."""
        return [{**thaw(self.resources[rid]), "aliases": list(self.aliases_of(rid))} for rid in self.order]


def thaw(value):
    """Plain JSON values from a validated profile's read-only maps, for journal entries and events."""
    if isinstance(value, MappingProxyType):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw(v) for v in value]
    return value


def _check_descriptor(d, dependencies, step_interval_ms) -> None:
    if not isinstance(d, dict) or set(d) != _DESCRIPTOR_FIELDS:
        raise _refuse("Each resource descriptor declares exactly the descriptor fields")
    rid = d["resource_id"]
    if not isinstance(rid, str) or not _NAME.fullmatch(rid):
        raise _refuse("Each resource has a lowercase canonical id of at most 128 characters")
    kind = d["control_kind"]
    if kind not in CONTROL_KINDS:
        raise _refuse(f"{rid} declares an unknown control kind")
    if not isinstance(d["label"], str) or not 1 <= len(d["label"]) <= 80:
        raise _refuse(f"{rid} needs a label of 1 to 80 characters")
    if d["unit"] != UNITS[kind]:
        raise _refuse(f"{rid} is {kind}, so its unit is {UNITS[kind]}")
    if d["readback"] not in READBACKS[kind]:
        raise _refuse(f"{rid} declares a readback a {kind} control can't have")
    if d["audibility"] not in AUDIBILITY:
        raise _refuse(f"{rid} declares an unknown audibility")
    if d["motorized"] is not False:
        raise _refuse(f"{rid} is motorized; this version supports nonmotorized controls only")
    if d["pickup"] not in PICKUPS:
        raise _refuse(f"{rid} declares an unqualified pickup policy",
                      "Soft takeover needs native proof; capabilities_list lists b2b_soft_takeover as "
                      "unavailable. Use explicit_pickup_required.")
    if d["pickup"] != ("explicit_pickup_required" if kind == "absolute" else "not_applicable"):
        raise _refuse(f"{rid}: a nonmotorized absolute control needs explicit pickup; other kinds have none")
    if kind == "absolute":
        bounds = d["range"]
        if (not isinstance(bounds, dict) or set(bounds) != {"minimum", "maximum"}
                or any(type(v) is not int for v in bounds.values())
                or not 0 <= bounds["minimum"] < bounds["maximum"] <= 127):
            raise _refuse(f"{rid} needs an integer range inside 0-127")
    elif d["range"] is not None:
        raise _refuse(f"{rid} is {kind}, so it has no range")
    if kind in ("absolute", "relative"):
        span = d["range"]["maximum"] - d["range"]["minimum"] if kind == "absolute" else 63
        if type(d["max_delta"]) is not int or not 1 <= d["max_delta"] <= span:
            raise _refuse(f"{rid} needs a positive max_delta no larger than its range")
    elif d["max_delta"] is not None:
        raise _refuse(f"{rid} is {kind}, so it has no max_delta")
    if kind in ("toggle", "state_setting"):
        states = _names(d["states"], f"{rid} states", minimum=2, maximum=2 if kind == "toggle" else 16)
        if any(len(s) > 32 for s in states):
            raise _refuse(f"{rid} names a state longer than 32 characters")
    elif d["states"] is not None:
        raise _refuse(f"{rid} is {kind}, so it has no states")
    step = d["step"]
    if (not isinstance(step, dict) or set(step) != {"interval_ms", "cancellation_boundary", "drain_ms"}
            or step["cancellation_boundary"] != "between_steps"):
        raise _refuse(f"{rid} needs a bounded step that can be cancelled between steps")
    if _ms(step["interval_ms"], f"{rid} step interval_ms", 1, 1000) != step_interval_ms:
        raise _refuse(f"{rid} steps every {step['interval_ms']} ms; its profile steps every "
                      f"{step_interval_ms} ms")
    _ms(step["drain_ms"], f"{rid} step drain_ms", 0, 1000)
    _names(d["routes"], f"{rid} routes", maximum=8)
    if not set(_names(d["depends_on"], f"{rid} depends_on", maximum=8)) <= set(dependencies):
        raise _refuse(f"{rid} depends on an undeclared dependency")
    if d["shared_bus"] is not None:
        _names(d["shared_bus"], f"{rid} shared_bus views", minimum=2, maximum=8)
    for field in ("conflict_group", "yield_group"):
        if d[field] is not None and not isinstance(d[field], str):
            raise _refuse(f"{rid} names its {field} with a string or null")
    if type(d["binding"]) is not bool or type(d["agent_writable"]) is not bool:
        raise _refuse(f"{rid} declares binding and agent_writable as booleans")
    if d["binding"] and (d["conflict_group"] is None or kind != "state_setting"):
        raise _refuse(f"{rid}: only a state-setting control in a conflict group can be a binding")


def _group_table(groups, resources, field) -> tuple[dict, dict]:
    if not isinstance(groups, dict):
        raise _refuse(f"{field}s map a group id to its members")
    members_of, owner = {}, {}
    for group, members in groups.items():
        if not isinstance(group, str) or not _NAME.fullmatch(group):
            raise _refuse(f"{field} ids are lowercase names")
        members = _names(members, f"{field} {group}", maximum=_MAX_SCOPE)
        for member in members:
            if member not in resources:
                raise _refuse(f"{field} {group!r} names undeclared resource {member!r}")
            if member in owner:
                raise _refuse(f"{member} is in two {field}s")
            owner[member] = group
        members_of[group] = members
    for rid, d in resources.items():
        if d[field] != owner.get(rid):
            raise _refuse(f"{rid} declares {field} {d[field]!r} but the profile groups it differently")
    return members_of, owner


def validate_profile(document: dict) -> Profile:
    """Validate a pocket.b2b-adapter-profile/v1 document; refuse anything this version can't honor."""
    if not isinstance(document, dict) or set(document) != _PROFILE_FIELDS:
        raise _refuse("An adapter profile declares exactly the pocket.b2b-adapter-profile/v1 fields")
    try:
        document = json.loads(canonical_bytes(document))
    except PocketError as error:
        raise _refuse("An adapter profile is finite JSON data") from error
    if document["schema"] != PROFILE_SCHEMA:
        raise _refuse("Unsupported adapter profile schema")
    if not isinstance(document["profile"], str) or not _NAME.fullmatch(document["profile"]):
        raise _refuse("A profile names itself with a lowercase id")
    if document["native"] is not False:
        raise _refuse("This version runs only the synthetic adapter; a native profile is a separate gate",
                      "capabilities_list(domain='collaboration') lists b2b_serato_adapter as unavailable.")
    dependencies = _names(document["dependencies"], "dependencies")
    step_interval_ms = _ms(document["step_interval_ms"], "step_interval_ms", 1, 1000)
    descriptors = document["resources"]
    if not isinstance(descriptors, list) or not 1 <= len(descriptors) <= _MAX_RESOURCES:
        raise _refuse(f"A profile declares 1 to {_MAX_RESOURCES} resources")
    resources = {}
    for d in descriptors:
        _check_descriptor(d, dependencies, step_interval_ms)
        if d["resource_id"] in resources:
            raise _refuse(f"{d['resource_id']} is declared twice")
        resources[d["resource_id"]] = d
    aliases = document["aliases"]
    if not isinstance(aliases, dict):
        raise _refuse("aliases maps each alias to one canonical resource")
    for alias, target in aliases.items():
        if not _NAME.fullmatch(alias):
            raise _refuse(f"Alias {alias!r} is not a lowercase name")
        if alias in resources:
            raise _refuse(f"Alias {alias!r} repeats a canonical resource id")
        if target not in resources:
            raise _refuse(f"Alias {alias!r} names an undeclared resource")
    yield_groups, yield_group_of = _group_table(document["yield_groups"], resources, "yield_group")
    conflict_groups, conflict_group_of = _group_table(document["conflict_groups"], resources,
                                                      "conflict_group")

    def canonical(name):
        return name if name in resources else aliases.get(name)

    raw_inputs, raw_gestures = document["physical_inputs"], document["gestures"]
    if not isinstance(raw_inputs, list) or not isinstance(raw_gestures, list):
        raise _refuse("physical_inputs and gestures are lists")
    inputs, addresses = {}, set()
    for item in raw_inputs:
        if not isinstance(item, dict) or set(item) != _INPUT_FIELDS:
            raise _refuse("Each physical input declares address, message, encoding and classification")
        address = item["address"]
        if not isinstance(address, str) or not _NAME.fullmatch(address) or address in addresses:
            raise _refuse("Each physical input has one unique lowercase address")
        key = message_key(item["message"])
        if key in inputs:
            raise _refuse(f"Message {key} is mapped twice")
        if item["classification"] not in CLASSIFICATIONS:
            raise _refuse(f"{address} declares an unqualified classification",
                          "Exempting a monitoring control needs native proof; capabilities_list lists "
                          "b2b_monitor_exemption as unavailable. Classify it unqualified.")
        if item["encoding"] not in ENCODINGS:
            raise _refuse(f"{address} declares an unknown encoding")
        message_type, kind = ENCODINGS[item["encoding"]]
        if key[0] != message_type:
            raise _refuse(f"{address} uses {item['encoding']}, which needs a {message_type} message")
        target = canonical(address)
        if item["classification"] == "control":
            if target is None:
                raise _refuse(f"Control input {address} names no declared resource")
            if resources[target]["control_kind"] != kind:
                raise _refuse(f"{address} uses {item['encoding']}, which can't move a "
                              f"{resources[target]['control_kind']} control")
        elif target is not None:
            raise _refuse(f"{address} is {item['classification']}, so it can't name a resource")
        addresses.add(address)
        inputs[key] = item
    gestures, gesture_by_id, gesture_scope = {}, {}, {}
    for g in raw_gestures:
        if not isinstance(g, dict) or set(g) != _GESTURE_FIELDS:
            raise _refuse("Each gesture declares exactly the gesture fields")
        gesture_id = g["gesture_id"]
        if not isinstance(gesture_id, str) or not _NAME.fullmatch(gesture_id) or gesture_id in gesture_by_id:
            raise _refuse("Each gesture has one unique lowercase id")
        if not isinstance(g["label"], str) or not 1 <= len(g["label"]) <= 80:
            raise _refuse(f"Gesture {gesture_id} needs a label of 1 to 80 characters")
        key = message_key(g["message"])
        if key in gestures or inputs.get(key, {}).get("classification") != "gesture":
            raise _refuse(f"Gesture {gesture_id} needs its own message, declared as a gesture input "
                          "and never as a control")
        debounce = _ms(g["debounce_ms"], f"{gesture_id} debounce_ms", 1, 250)
        max_hold = _ms(g["max_hold_ms"], f"{gesture_id} max_hold_ms", 1, 60000)
        long_press = g["long_press_ms"]
        if type(long_press) is not int or not max(300, debounce + 1) <= long_press < max_hold:
            raise _refuse("A long press lasts at least 300 ms, beyond the debounce and below max_hold_ms")
        values = (g["press_value"], g["release_value"])
        if any(type(v) is not int or not 0 <= v <= 127 for v in values) or values[0] == values[1]:
            raise _refuse(f"Gesture {gesture_id} needs distinct press and release values 0-127")
        scope = []
        for name in _names(g["scope"], f"Gesture {gesture_id} scope"):
            if canonical(name) is None:
                raise _refuse(f"Gesture {gesture_id} scope names undeclared {name!r}")
            scope.append(canonical(name))
        scope = tuple(dict.fromkeys(scope))
        if any(m not in scope for rid in scope if rid in conflict_group_of
               for m in conflict_groups[conflict_group_of[rid]]):
            raise _refuse(f"Gesture {gesture_id} must cover whole conflict groups")
        gestures[key] = g
        gesture_by_id[gesture_id] = g
        gesture_scope[gesture_id] = scope
    unbound = [i["address"] for k, i in inputs.items()
               if i["classification"] == "gesture" and k not in gestures]
    if unbound:
        raise _refuse(f"Gesture inputs {unbound} have no gesture binding")
    return Profile(canonical_json=canonical_bytes(document), name=document["profile"],
                   sha256=mapping_sha256(document),
                   dependencies=dependencies, step_interval_ms=step_interval_ms, order=tuple(resources),
                   resources=_frozen(resources), aliases=MappingProxyType(dict(aliases)),
                   yield_groups=MappingProxyType(yield_groups),
                   conflict_groups=MappingProxyType(conflict_groups),
                   yield_group_of=MappingProxyType(yield_group_of),
                   conflict_group_of=MappingProxyType(conflict_group_of),
                   inputs=_frozen_keys(inputs), gestures=_frozen_keys(gestures),
                   gesture_by_id=_frozen(gesture_by_id), gesture_scope=MappingProxyType(gesture_scope))


def _frozen_keys(table: dict) -> MappingProxyType:
    return MappingProxyType({key: _frozen(value) for key, value in table.items()})


# The synthetic-two-deck/v1 profile --------------------------------------------------------------

def _descriptor(rid, label, kind, *, readback, audibility="audible", states=None, conflict=None,
                yield_group=None, shared_bus=None, binding=False, writable=True):
    absolute = kind == "absolute"
    return {"resource_id": rid, "label": label, "control_kind": kind, "unit": UNITS[kind],
            "range": {"minimum": 0, "maximum": 127} if absolute else None,
            "states": states, "max_delta": 16 if kind in ("absolute", "relative") else None,
            "readback": readback, "motorized": False,
            "pickup": "explicit_pickup_required" if absolute else "not_applicable",
            "step": {"interval_ms": 50, "cancellation_boundary": "between_steps", "drain_ms": 0},
            "audibility": audibility, "routes": ["master"], "depends_on": ["controller", "route:master"],
            "shared_bus": shared_bus, "conflict_group": conflict, "yield_group": yield_group,
            "binding": binding, "agent_writable": writable}


def _input(address, kind, channel, number, encoding, classification="control"):
    return {"address": address, "message": {"type": kind, "channel": channel, "number": number},
            "encoding": encoding, "classification": classification}


def synthetic_profile() -> dict:
    """A fresh copy of the synthetic-two-deck/v1 profile document."""
    resources, aliases, inputs = [], {}, []
    for deck, channel in (("left", 1), ("right", 2)):
        title = deck.capitalize() + " deck"
        motion = f"synthetic/{deck}/deck-motion"
        resources += [
            _descriptor(f"synthetic/{deck}/gain", f"{title} gain", "absolute", readback="numeric"),
            _descriptor(f"synthetic/{deck}/filter", f"{title} filter", "absolute", readback="commanded_only"),
            _descriptor(f"synthetic/{deck}/transport", f"{title} transport", "toggle", readback="state",
                        states=["paused", "playing"], yield_group=motion),
            _descriptor(f"synthetic/{deck}/position", f"{title} position", "relative", readback="none",
                        yield_group=motion),
        ]
        aliases.update({f"controller/synthetic/channel-{channel}/trim": f"synthetic/{deck}/gain",
                        f"synthetic/{deck}/track-gain": f"synthetic/{deck}/gain",
                        f"controller/synthetic/channel-{channel}/filter": f"synthetic/{deck}/filter",
                        f"controller/synthetic/play-{channel}": f"synthetic/{deck}/transport",
                        f"controller/synthetic/jog-{channel}": f"synthetic/{deck}/position",
                        f"synthetic/{deck}/crossfader-view": "synthetic/mixer/crossfader",
                        f"synthetic/{deck}/fx": "synthetic/fx/unit-1"})
        inputs += [
            _input(f"controller/synthetic/channel-{channel}/trim", "cc", channel, 19, "absolute_7bit"),
            _input(f"controller/synthetic/channel-{channel}/filter", "cc", channel, 23, "absolute_7bit"),
            _input(f"controller/synthetic/play-{channel}", "note", channel, 11, "note_press"),
            _input(f"controller/synthetic/jog-{channel}", "cc", channel, 33, "relative_twos_complement"),
        ]
    resources += [
        _descriptor("synthetic/mixer/crossfader", "Crossfader", "absolute", readback="numeric",
                    shared_bus=["left", "right"]),
        _descriptor("synthetic/fx/unit-1", "FX unit 1 depth", "absolute", readback="numeric",
                    conflict="synthetic/fx/unit-1"),
        _descriptor("synthetic/fx/unit-1/route", "FX unit 1 route", "state_setting", readback="state",
                    audibility="uncertain", states=["left", "right"], conflict="synthetic/fx/unit-1",
                    binding=True, writable=False),
    ]
    aliases.update({"controller/synthetic/crossfader": "synthetic/mixer/crossfader",
                    "controller/synthetic/fx-1/depth": "synthetic/fx/unit-1",
                    "controller/synthetic/fx-1/route": "synthetic/fx/unit-1/route"})
    inputs += [
        _input("controller/synthetic/crossfader", "cc", 7, 31, "absolute_7bit"),
        _input("controller/synthetic/fx-1/depth", "cc", 5, 2, "absolute_7bit"),
        _input("controller/synthetic/fx-1/route", "note", 5, 71, "note_toggle_route"),
        # Not qualified as monitoring-only, so it conservatively yields every control.
        _input("controller/synthetic/headphone-level", "cc", 7, 12, "absolute_7bit", "unqualified"),
        _input("controller/synthetic/handback-pad", "note", 8, 64, "note_press", "gesture"),
    ]
    gestures = [{"gesture_id": "handback-right", "label": "Hand back the right deck",
                 "message": {"type": "note", "channel": 8, "number": 64}, "press_value": 127,
                 "release_value": 0, "debounce_ms": 30, "long_press_ms": 500, "max_hold_ms": 5000,
                 "scope": ["synthetic/right/gain", "synthetic/right/filter", "synthetic/right/transport",
                           "synthetic/right/position"]}]
    return {"schema": PROFILE_SCHEMA, "profile": "synthetic-two-deck/v1", "native": False,
            "resources": resources, "aliases": aliases, "physical_inputs": inputs, "gestures": gestures,
            "yield_groups": {"synthetic/left/deck-motion": ["synthetic/left/transport",
                                                            "synthetic/left/position"],
                             "synthetic/right/deck-motion": ["synthetic/right/transport",
                                                             "synthetic/right/position"]},
            "conflict_groups": {"synthetic/fx/unit-1": ["synthetic/fx/unit-1", "synthetic/fx/unit-1/route"]},
            "dependencies": ["controller", "route:master"], "step_interval_ms": 50}


SYNTHETIC_PROFILE = synthetic_profile()
SYNTHETIC_DEFAULTS = MappingProxyType({"synthetic/left/gain": 100, "synthetic/right/gain": 100,
                                       "synthetic/left/filter": 64, "synthetic/right/filter": 64,
                                       "synthetic/mixer/crossfader": 64, "synthetic/fx/unit-1": 0})
_STALE_READBACK_NS = 10_000_000_000


class SyntheticAdapter:
    """A deterministic in-memory two-deck mixer for tests and the local demo.

    It has no thread and does no I/O: no subprocess, socket, MIDI or audio access. Physical
    messages arrive only through ``inject_message`` and the fault methods, and reach the
    broker exactly as a native adapter would deliver them. Nothing here qualifies native
    behaviour: jump-on-touch, inline acks and toggle transport are this model's choices.

    The broker calls ``submit``, ``readback``, ``health``, ``epoch`` and ``renew_epoch``, which
    take only the adapter lock. ``pump`` and the injection and fault methods call the broker,
    so nothing may call them while holding the broker lock.
    """

    def __init__(self, profile: dict | None = None):
        self._profile = validate_profile(SYNTHETIC_PROFILE if profile is None else profile)
        self.mapping_sha256 = self._profile.sha256
        self._lock = threading.Lock()
        # Held while events go to the sink, so they arrive in the order this adapter made them.
        # It is never taken under the broker lock.
        self._delivery = threading.RLock()
        self._sink = None
        self._clock = None
        self._closed = False
        self._epoch = 1
        resources = self._profile.resources
        self._software = {r: SYNTHETIC_DEFAULTS.get(r, d["range"]["minimum"])
                          for r, d in resources.items() if d["control_kind"] == "absolute"}
        self._physical = dict.fromkeys(self._software)
        self._states = {r: ("playing" if "playing" in d["states"] else d["states"][0])
                        for r, d in resources.items() if d["control_kind"] in ("toggle", "state_setting")}
        self._position = {r: 0 for r, d in resources.items() if d["control_kind"] == "relative"}
        self._dependencies = dict.fromkeys(self._profile.dependencies, "available")
        self._deferred = []
        self._sent = deque(maxlen=65536)
        self._sent_count = 0
        self._dropped_messages = 0
        self._serial = 0
        self._heartbeat_ns = 0
        self._heartbeat_event_ns = None
        # Fault knobs for tests and the demo page.
        self.ack_mode = "inline"          # inline | deferred | never | failed
        self.deferred_ack_ms = 20
        self.block_submit = False
        self.freeze_heartbeat = False
        self.readback_mode = "fresh"      # fresh | stale | mismatch
        self.heartbeat_interval_ms = 100

    # Contract surface -------------------------------------------------------------------------
    @property
    def profile(self) -> dict:
        """A fresh copy of this adapter's profile document."""
        return self._profile.document

    def attach(self, sink: AdapterSink, clock) -> None:
        with self._lock:
            if self._sink is not None and self._sink is not sink:
                raise AdapterContractError("This adapter already reports to a broker")
            self._sink, self._clock = sink, clock
            self._heartbeat_ns = clock.now_ns()

    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    def health(self) -> dict:
        with self._lock:
            return {"dependencies": dict(self._dependencies), "heartbeat_monotonic_ns": self._heartbeat_ns}

    def readback(self, resource: str) -> Readback | None:
        with self._lock:
            return self._readback(resource, self._now())

    def submit(self, step: Step, admission: Admission) -> Submission:
        with self._lock:
            now = self._now()
            if self._closed:
                raise AdapterBlocked("The synthetic writer is closed")
            if admission.adapter_epoch != self._epoch:
                raise AdapterRefused("The step names a different adapter epoch")
            if now >= admission.deadline_monotonic_ns:
                raise AdapterRefused("The step reached the adapter at or after its deadline")
            d = self._profile.resources.get(step.resource)
            if d is None or not d["agent_writable"] or not self._valid_message(d, step.message):
                raise AdapterRefused("The step is not a declared move for this resource")
            if self.block_submit:
                raise AdapterBlocked("The synthetic writer is blocked")
            if any(self._dependencies[name] != "available" for name in d["depends_on"]):
                raise AdapterBlocked("A dependency of this resource is unavailable")
            mode = self._mode(self.ack_mode, ACK_MODES)
            self._apply(step.resource, step.message)
            self._serial += 1
            submission_id = f"s-{self._epoch}-{self._serial}"
            self._sent.append((step, admission))
            self._sent_count += 1
            if mode == "inline":
                return Submission(submission_id, now, "ok", self._readback(step.resource, now))
            if mode == "failed":
                return Submission(submission_id, now, "failed", None)
            if mode == "deferred":
                due = now + self.deferred_ack_ms * 1_000_000
                self._deferred.append((due, self._serial, submission_id, step.resource))
            return Submission(submission_id, now, "pending", None)

    def pump(self, now_ns: int) -> None:
        with self._delivery:
            with self._lock:
                due = sorted(item for item in self._deferred if item[0] <= now_ns)
                self._deferred = [item for item in self._deferred if item[0] > now_ns]
                events = []
                for at, _, submission_id, resource in due:
                    events.append({"type": "ack", "submission_id": submission_id, "status": "ok",
                                   "at_ns": at})
                    readback = self._readback(resource, now_ns)
                    if readback is not None:
                        events.append(self._readback_event(resource, readback, now_ns))
                if not self.freeze_heartbeat:
                    self._heartbeat_ns = max(self._heartbeat_ns, now_ns)
                    interval = self.heartbeat_interval_ms * 1_000_000
                    if self._heartbeat_event_ns is None or now_ns - self._heartbeat_event_ns >= interval:
                        self._heartbeat_event_ns = now_ns
                        events.append({"type": "heartbeat", "at_ns": now_ns})
            self._deliver(events)

    def renew_epoch(self, reason: str) -> int:
        """Start a new adapter epoch at the broker's request, for example after a clock discontinuity.

        Pending acks and known physical positions belong to the old epoch and are dropped. It
        never calls the sink: the broker already knows.
        """
        with self._lock:
            return self._new_epoch()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._deferred = []

    # Injection (tests and the trusted demo page only) ---------------------------------------
    def inject_message(self, raw, origin: str = "controller") -> tuple[dict, ...]:
        """Deliver one controller message, or feedback echo, as a native adapter would; return the events."""
        if origin not in ("controller", "feedback"):
            raise ValueError("origin is controller or feedback")
        with self._delivery:
            with self._lock:
                now = self._now()
                if self._dependencies.get("controller") != "available":
                    self._dropped_messages += 1
                    return ()
                events = self._classify(raw, origin, now)
            self._deliver(events)
        return tuple(events)

    def disconnect_controller(self) -> None:
        self._set_dependency("controller", "unavailable", None)

    def reconnect_controller(self, mapping_sha256: str | None = None) -> None:
        """Reconnect in a new epoch; a different mapping hash models a controller that changed its mapping."""
        self._set_dependency("controller", "available", ("controller_reconnected", mapping_sha256))

    def lose_route(self, route: str = "master") -> None:
        self._set_dependency(self._route_name(route), "unavailable", None)

    def restore_route(self, route: str = "master") -> None:
        self._set_dependency(self._route_name(route), "available", ("route_restored", None))

    def restart_writer(self) -> None:
        with self._delivery:
            with self._lock:
                event = self._epoch_event(self._new_epoch(), "writer_restarted", None)
            self._deliver([event])

    def sent(self) -> tuple:
        """Every submitted (step, admission), oldest first (the last 65,536)."""
        with self._lock:
            return tuple(self._sent)

    @property
    def sent_count(self) -> int:
        with self._lock:
            return self._sent_count

    def value(self, resource: str):
        """The synthetic mixer's software value, state or accumulated position."""
        with self._lock:
            for table in (self._software, self._states, self._position):
                if resource in table:
                    return table[resource]
            raise KeyError(resource)

    def physical_value(self, resource: str) -> int | None:
        """Where the synthetic fader physically sits, or None before its first message this epoch."""
        with self._lock:
            return self._physical[resource]

    def panel_state(self) -> dict:
        """What the demo page's synthetic controller panel shows."""
        with self._lock:
            return {"adapter_epoch": self._epoch, "dependencies": dict(self._dependencies),
                    "software": dict(self._software), "physical": dict(self._physical),
                    "states": dict(self._states), "positions": dict(self._position),
                    "dropped_messages": self._dropped_messages}

    # Internals --------------------------------------------------------------------------------
    def _now(self) -> int:
        if self._clock is None:
            raise AdapterContractError("Attach the adapter to a broker before using it")
        return self._clock.now_ns()

    @staticmethod
    def _mode(value, allowed):
        if value not in allowed:
            raise ValueError(f"A synthetic fault knob is one of {allowed}")
        return value

    def _readback(self, resource, now):
        d = self._profile.resources[resource]
        if d["readback"] not in ("numeric", "state"):
            return None
        mode = self._mode(self.readback_mode, READBACK_MODES)
        if d["control_kind"] == "absolute":
            value = self._software[resource]
            if mode == "mismatch":
                value = value + 10 if value + 10 <= d["range"]["maximum"] else value - 10
        else:
            value = self._states[resource]
            if mode == "mismatch":
                value = next(s for s in d["states"] if s != value)
        observed = now - _STALE_READBACK_NS if mode == "stale" else now
        return Readback(value, "control_readback", observed)

    @staticmethod
    def _readback_event(resource, readback, now):
        return {"type": "readback", "resource": resource, "value": readback.value, "scope": readback.scope,
                "observed_monotonic_ns": readback.observed_monotonic_ns, "at_ns": now}

    @staticmethod
    def _valid_message(d, message) -> bool:
        if not isinstance(message, dict):
            return False
        kind = d["control_kind"]
        expected = {"absolute": ("set", "value"), "relative": ("nudge", "delta"),
                    "toggle": ("toggle", None), "state_setting": ("set_state", "state")}[kind]
        fields = {"kind", expected[1]} if expected[1] else {"kind"}
        if message.get("kind") != expected[0] or set(message) != fields:
            return False
        if kind == "absolute":
            value = message["value"]
            return type(value) is int and d["range"]["minimum"] <= value <= d["range"]["maximum"]
        if kind == "relative":
            return type(message["delta"]) is int and abs(message["delta"]) <= d["max_delta"]
        if kind == "state_setting":
            return message["state"] in d["states"]
        return True

    def _apply(self, resource, message):
        if message["kind"] == "set":
            self._software[resource] = message["value"]  # software only; the fader stays where it is
        elif message["kind"] == "nudge":
            self._position[resource] += message["delta"]
        elif message["kind"] == "toggle":
            states = self._profile.resources[resource]["states"]
            self._states[resource] = next(s for s in states if s != self._states[resource])
        else:
            self._states[resource] = message["state"]

    def _new_epoch(self):
        self._epoch += 1
        self._deferred = []
        self._physical = dict.fromkeys(self._physical)
        return self._epoch

    def _epoch_event(self, epoch, reason, mapping):
        return {"type": "epoch", "adapter_epoch": epoch, "mapping_sha256": mapping or self._profile.sha256,
                "reason": reason, "dependencies": dict(self._dependencies), "at_ns": self._now()}

    def _route_name(self, route):
        name = f"route:{route}"
        if name not in self._dependencies:
            raise ValueError(f"The synthetic mixer has no {route} route")
        return name

    def _set_dependency(self, name, state, restore):
        with self._delivery:
            with self._lock:
                if self._dependencies[name] == state:
                    return
                self._dependencies[name] = state
                if restore is None:
                    event = {"type": "dependency", "name": name, "state": state, "at_ns": self._now()}
                else:
                    event = self._epoch_event(self._new_epoch(), restore[0], restore[1])
            self._deliver([event])

    def _deliver(self, events):
        # Called with the delivery lock held and the adapter lock released.
        sink = self._sink
        if sink is None:
            raise AdapterContractError("Attach the adapter to a broker before delivering events")
        for event in events:
            sink.adapter_event(event)

    def _classify(self, raw, origin, now):
        shown = list(raw) if (isinstance(raw, (list, tuple)) and len(raw) <= 16
                              and all(type(b) is int and 0 <= b <= 255 for b in raw)) else None
        if origin == "feedback":
            return [{"type": "feedback", "raw": shown, "at_ns": now}]
        parsed = parse_raw(raw)
        item = None if parsed is None else self._profile.inputs.get(parsed[0])
        if item is None:
            return [{"type": "unknown_input", "raw": shown, "classification": "unknown", "at_ns": now}]
        key, value = parsed
        if item["classification"] == "unqualified":
            return [{"type": "unknown_input", "raw": shown, "classification": "unqualified",
                     "address": item["address"], "at_ns": now}]
        if item["classification"] == "gesture":
            gesture = self._profile.gestures[key]
            phase = ("press" if value == gesture["press_value"] else
                     "release" if value == gesture["release_value"] else None)
            if phase is None:
                return [{"type": "unknown_input", "raw": shown, "classification": "unknown", "at_ns": now}]
            return [{"type": "gesture", "gesture_id": gesture["gesture_id"], "phase": phase, "at_ns": now}]
        resource = self._profile.canonical(item["address"])
        encoding = item["encoding"]
        events = []
        if encoding == "absolute_7bit":
            before = self._software[resource]
            self._software[resource] = self._physical[resource] = value  # jump semantics
            after = decoded = value
        elif encoding == "relative_twos_complement":
            decoded = value - 128 if value >= 64 else value
            before = self._position[resource]
            self._position[resource] = after = before + decoded
        else:
            decoded = value
            before = after = self._states[resource]
            if value > 0:
                states = self._profile.resources[resource]["states"]
                after = states[(states.index(before) + 1) % len(states)]
                self._states[resource] = after
                if encoding == "note_toggle_route":
                    group = self._profile.conflict_group_of[resource]
                    # A binding change arrives first, so the grant it ends records binding_changed.
                    events.append({"type": "binding_changed", "group": group,
                                   "resources": list(self._profile.conflict_groups[group]), "value": after,
                                   "at_ns": now})
        return [*events, {"type": "physical_input", "resources": [resource], "address": item["address"],
                          "encoding": encoding, "value": decoded,
                          "physical_value": decoded if encoding == "absolute_7bit" else None,
                          "software_before": before, "software_after": after, "at_ns": now}]
