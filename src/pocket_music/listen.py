# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import math
import os
import re
import secrets
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from . import listen_host, listen_native
from .artifact_store import canonical_bytes, digest, put_record, receipt, request_status, run_request
from .errors import PocketError
from .listen_native import ADAPTER_PROFILES, COREAUDIO_PROFILE, SYNTHETIC_PROFILE
from .listen_types import ListenAdapter, ListenConfig, ListenCursor, ListenEventKind, ListenOnExpired

SEMANTIC_STATUS = {"master": "declared_unverified", "unlabeled": "no_semantic_claim",
                   "cue": "not_established"}
RUNTIME_SCHEMA = "pocket.listen-runtime/v1"
RETENTION = "in_memory_bounded_window_no_file"
DESCRIPTOR_CAPACITY = 4096
DESCRIPTOR_BYTES = 64
FACTS_CAPACITY = 1024
WORKER_KEYS = ("state", "error", "lag_frames", "overruns", "unmeasured_frames", "unmeasured_after_failure_frames",
               "unaccounted_frames", "facts_dropped")
WORKER_TERMINAL_KEYS = (*WORKER_KEYS, "unmeasured_at_stop_frames", "unmeasured_at_stop_is_upper_bound")
SAMPLE_RATES = listen_native.SAMPLE_RATES


class Setting(NamedTuple):
    low: int
    high: int
    default: int


SETTINGS = {
    "buffering": {"ring_seconds": Setting(1, 60, 10)},
    "health": {"stale_after_ms": Setting(100, 5000, 500)},
    "delivery": {"meter_coalesce_windows": Setting(1, 50, 5),
                 "normal_retention_events": Setting(256, 65536, 8192),
                 "normal_retention_bytes": Setting(65536, 33554432, 4194304),
                 "health_retention_events": Setting(64, 8192, 1024),
                 "health_retention_bytes": Setting(32768, 4194304, 524288)},
    "limits": {"max_runtime_seconds": Setting(10, 43200, 14400)},
}

EVENT_MAX_BYTES = 8192
MAX_EVENTS = 256
MIN_PAGE_BYTES, MAX_PAGE_BYTES = 2 * EVENT_MAX_BYTES, 262144
MAX_WAIT_MS = 1000
MAX_EVIDENCE_AGE_MS = 3600000
SCAN_RUNTIMES = 64

_ROUTE_ID = re.compile(r"[a-z][a-z0-9-]{0,31}")
_EPOCH = re.compile(r"rt-[0-9a-f]{16}")
_ROLE_LIMITS = {"master": 1, "cue": 1, "unlabeled": 2}

_PAGE_HINT = ("A page holds at most 256 events and 256 KiB (at least 16 KiB), and a wait lasts at most "
              "1000 ms. Page onward with next_after.")
_CURSOR_HINT = "Pass after exactly as listen_start or listen_observe returned it: {runtime_epoch, sequence}."
_KINDS_HINT = 'Health events can\'t be filtered out; use kinds ["health"] or ["measurement", "health"].'
_NAME_HINT = ("Select the input with device.uid; listen_devices lists each uid. Display names can repeat, "
              "so Listen never selects by name.")
_OVERLAP_HINT = ("Give each channel to one audio route; one channel can't carry two labels. To see levels "
                 "on channels you'd call cue, declare them as an unlabeled route.")
_ROLE_WORD_HINT = ("Use a route_id with a word that starts with 'cue' or 'master' (such as cue-left, cue1 or "
                   "master2) only for a route with that role; name other routes another way, such as "
                   "deck-b.")
_NAMESPACE_HINT = ("Synthetic devices use uids starting with 'synthetic:'; the CoreAudio adapter never "
                   "accepts them.")
_DIRECTORY_HINT = "Choose a new output_dir; Listen never reuses or overwrites a directory."
CURSOR_AHEAD_HINT = "Use the cursor listen_start or listen_observe returned; Listen never clamps a cursor."
_WRITABLE_HINT = "Choose a new output_dir in a writable location."
_OWNERSHIP_HINT = ("Choose an output_dir on a volume that enforces ownership; on one that ignores it, every "
                   "account counts as the owner of every file.")
_POSIX_HINT = "Listen's runtime runs on macOS or Linux; the synthetic adapter needs no other setup there."
_MACOS_HINT = "The CoreAudio adapter runs on macOS 15 or later; use the synthetic adapter on this system."
_SHA_HINT = ("Pass config_sha256 from this session's listen_prepare receipt unchanged; to change settings, "
             "prepare a new session directory.")
_STALE_EPOCH_HINT = ("Call listen_observe for the current runtime_epoch; listen_stop never stops a different "
                     "runtime.")
_EPOCH_HINT = ("Pass runtime_epoch exactly as listen_start or listen_observe returned it, such as "
               "rt-0123456789abcdef.")
_JOURNAL_KEYS = listen_host.JOURNAL_CHECK_KEYS
_BUILD_KEYS = ("build_id", "build_json_sha256", "helper_sha256")


def coverage(audio_source, **extra):
    return {"audio_source": audio_source, "physical_headphones": "not_observed",
            "headphone_cue": "not_observed", "human_listening": "not_performed",
            "route_semantics": "not_verified", "control_issued": False,
            "device_settings_changed": False, "audio_written_to_disk": False,
            "native_qualification": "not_established", **extra}


def resolve_routes(routes, device_input_channels):
    resolved, reasons = [], []
    for route in routes:
        exposed = all(c < device_input_channels for c in route["channels"])
        row = {"route_id": route["route_id"], "role": route["role"], "channels": list(route["channels"]),
               "channels_exposed": exposed, "semantic_status": SEMANTIC_STATUS[route["role"]],
               "route_calibration_id": None}
        if route["role"] == "cue":
            row.update(availability="unavailable", metering="withheld_until_routing_test",
                       isolated_cue="unavailable", reason="routing_test_not_available")
        elif exposed:
            row.update(availability="available", metering="metered", reason=None)
        else:
            row.update(availability="unavailable", metering="not_metered", reason="channels_not_exposed")
            if route["role"] == "master":
                reasons.append("master_channels_not_exposed")
        resolved.append(row)
    if not reasons and not any(r["metering"] == "metered" for r in resolved):
        reasons.append("no_metered_channels_exposed")
    return tuple(resolved), tuple(reasons)


def selected_channels(resolved_routes):
    return tuple(c for r in resolved_routes if r["metering"] == "metered" for c in r["channels"])


def stale_after_fits_block(stale_after_ms, block_frames, sample_rate):
    if type(block_frames) is not int or block_frames < 1 or not sample_rate:
        return True
    return stale_after_ms * sample_rate > 2 * block_frames * 1000


class _Notice(NamedTuple):
    severity: str
    text: str
    next_step: str | None


class _NoticeText(NamedTuple):
    text: str
    next_step: str


_NOTICES = {
    "capture_stale": _Notice("fault", ("{routes} capture stopped receiving frames {age_s} s ago. Music "
                                       "that was already playing may still be playing; this is a "
                                       "monitoring fault, not a sign the decks stopped."),
                             ("Check the source app and device. If frames return, Listen reports "
                              "capture_resumed.")),
    "capture_no_frames": _Notice("fault", ("No frames have arrived from {routes} since capture started "
                                           "{age_s} s ago. This is a monitoring fault, not a sign the "
                                           "decks stopped."),
                                 ("Check the source app and device. If frames arrive, Listen reports "
                                  "capture_resumed.")),
    "frames_missing_exact": _Notice("warning", ("Capture is missing {total} frames in {gaps} exact gaps; "
                                                "the latest is {frames} frames at stream frame "
                                                "{start_frame} on {stream_epoch}. Levels never cover those "
                                                "intervals."),
                                    "Treat measurements across the gap as incomplete."),
    "measurement_facts_dropped": _Notice("warning", ("Level measurement's fact queue was full, so "
                                                     "measurement lost track of {count} capture facts, and "
                                                     "{frames} frames it never measured can't be told "
                                                     "apart as missing or unmeasured. Health events still "
                                                     "report every gap."), None),
    "new_stream_epoch": _Notice("info", ("Capture is on stream epoch {stream_epoch} ({cause}). Frame "
                                         "positions from earlier streams don't carry over."), None),
    "capture_ended": _Notice("fault", "Capture ended ({cause}). Listen doesn't reconnect on its own.",
                             ("Stop this runtime with listen_stop and its runtime_epoch, then start a new "
                              "one with listen_start and a new request_id.")),
    "device_lost": _Notice("fault", ("The input device went away, so capture ended. Listen doesn't "
                                     "reconnect on its own."),
                           ("Stop this runtime with listen_stop and its runtime_epoch, reconnect the "
                            "device, then start again with a new request_id.")),
    "format_changed": _Notice("fault", ("The input's sample rate or format changed ({cause}), so capture "
                                        "ended instead of converting it."),
                              ("Stop this runtime with listen_stop and its runtime_epoch, then prepare a "
                               "new session for the new format and start it.")),
    "channel_map_changed": _Notice("fault", ("The input's channel layout changed, so capture ended instead "
                                             "of relabeling channels."),
                                   ("Stop this runtime with listen_stop and its runtime_epoch, check the "
                                    "inputs with listen_devices, then prepare a new session.")),
    "microphone_permission_needed": _Notice("fault", ("macOS hasn't granted microphone access to the app "
                                                      "that runs Pocket, so Listen didn't start capture."),
                                            ("If System Settings > Privacy & Security > Microphone lists "
                                             "that app, turn it on there. If it isn't listed, run Pocket "
                                             "from an app that already has microphone access; Listen "
                                             "never asks for access itself. Then start again with a new "
                                             "request_id.")),
    "nonfinite_input": _Notice("warning", ("{routes} received samples that aren't finite numbers (NaN or "
                                           "infinity). Their levels stay empty until finite input returns."),
                               "Check the source app's output."),
    "measurement_overrun": _Notice("warning", ("Listen fell behind and didn't measure {frames} frames. "
                                               "Capture kept running; those frames have no levels."), None),
    "measurement_lagging": _Notice("warning", ("Level measurement is {seconds} s behind capture. If "
                                               "capture overwrites audio or block records it hasn't "
                                               "reached, Listen skips ahead to half that buffer behind and "
                                               "reports the skipped frames as one measurement_overrun."),
                                   "Use fewer routes, larger measurement.window_frames or more coalescing."),
    "measurement_worker_failed": _Notice("fault", ("Level measurement stopped after an internal error, so "
                                                   "routes report no current level or near-silence; "
                                                   "{frames} frames since then have no levels. Capture "
                                                   "health is still reported."),
                                         "Stop this runtime and start a new one to measure levels."),
    "journal_degraded": _Notice("warning", ("Listen's runtime journal is {state}, so not every health "
                                            "event is kept on disk. Live events are unaffected."), None),
    "device_changes_unwatched": _Notice("warning", ("The macOS helper couldn't watch these input "
                                                    "properties: {properties}. Changes to them may be "
                                                    "reported late, and a processor overload may show as a "
                                                    "plain device gap."), None),
    "adapter_stats_unavailable": _Notice("warning", ("The input adapter's statistics can't be read "
                                                     "({count} failed reads), so the macOS helper's "
                                                     "heartbeat isn't being checked: "
                                                     "capture.helper_unresponsive is unknown. Stale "
                                                     "capture is still reported from the blocks that "
                                                     "arrive."), None),
    "runtime_expiring": _Notice("warning", "This runtime stops at its configured limit in {seconds} s.",
                                "Start a new runtime after it stops if you need to keep watching."),
    "near_silence": _Notice("info", ("{routes} samples stayed below {level_dbfs} dBFS for at least "
                                     "{seconds} s. That's a level measurement, not a stopped deck or an "
                                     "intended ending."), None),
    "master_label_untested": _Notice("info", ("Master's label comes from your setup. No routing test has "
                                              "confirmed it."), None),
    "cue_not_established": _Notice("info", ("Cue isn't monitored. This version has no routing test, so "
                                            "Listen treats no channel as cue audio."),
                                   ("To see whether those channels carry signal, declare them as an "
                                    "unlabeled route in a new session.")),
    "display_name_shared": _Notice("info", ("More than one input is named {name}. Listen selects inputs by "
                                            "uid only."), None),
    "devices_unreadable": _Notice("warning", ("The macOS helper couldn't read the input stream "
                                              "configuration of {count} devices, so they aren't listed; "
                                              "any of them may be an input."),
                                  ("Call listen_devices again. If an input you expect stays missing, "
                                   "reconnect it, then list again.")),
    "synthetic_audio": _Notice("info", ("This session uses scripted synthetic audio. Nothing here "
                                        "describes a real device."), None),
    "runtime_not_running": _Notice("info", "No Listen runtime is running for this session.",
                                   "Call listen_start with a new request_id."),
    "runtime_epoch_changed": _Notice("info", "The running runtime is {runtime_epoch}, not the one you named.",
                                     "Use the cursor from this observation."),
}
_NOTICE_ORDER = {code: index for index, code in enumerate(_NOTICES)}
_SEVERITY_ORDER = {"fault": 0, "warning": 1, "info": 2}


def notice(code, **values):
    item = _NOTICES[code]
    return {"code": code, "severity": item.severity, "text": item.text.format(**values),
            "next_step": item.next_step}


_MICROPHONE = {
    "denied": _NoticeText(("macOS denied microphone access to the app that runs Pocket, so Listen didn't "
                           "start capture."),
                          ("Turn microphone access on for that app in System Settings > Privacy & "
                           "Security > Microphone, then start again with a new request_id.")),
    "restricted": _NoticeText(("Microphone access is restricted for the app that runs Pocket, for example "
                               "by a profile that manages this Mac, so Listen didn't start capture."),
                              ("System Settings can't lift a restriction; whoever manages this Mac can. "
                               "Until then Listen can't capture from that app.")),
    "not_determined": _NoticeText(("The app that runs Pocket has never asked macOS for microphone access, "
                                   "and Listen never asks, so Listen didn't start capture. macOS lists an "
                                   "app under Privacy & Security > Microphone only after it has asked, so "
                                   "there is nothing to turn on yet."),
                                  ("This version has no way to request access. Run Pocket from an app "
                                   "that macOS already lists with microphone access on, then start again "
                                   "with a new request_id.")),
}
_AUTHORIZATIONS_THE_OPERATOR_CAN_CHANGE = ("denied", "unknown")


def permission_notice(authorization):
    item = notice("microphone_permission_needed")
    if authorization not in _MICROPHONE:
        return item
    return {**item, **_MICROPHONE[authorization]._asdict()}


def _channel_list(channels):
    names = [str(c) for c in channels]
    return ("channel " if len(names) == 1 else "channels ") + " and ".join(names)


def cue_notice(channels=(), exposed=True):
    item = notice("cue_not_established")
    if exposed:
        return item
    return {**item, "next_step": (f"This input has no {_channel_list(channels)} (counting from 0), so an "
                                  "unlabeled route there would show nothing. listen_devices lists each "
                                  "input's channel count.")}


def order_notices(notices, limit=8):
    unique = {}
    for item in notices:
        unique.setdefault(item["code"], item)
    ranked = sorted(unique.values(),
                    key=lambda n: (_SEVERITY_ORDER[n["severity"]], _NOTICE_ORDER[n["code"]]))
    urgent = [n for n in ranked if n["severity"] != "info"]
    return urgent + [n for n in ranked if n["severity"] == "info"][:max(0, limit - len(urgent))]


def _bounded_int(value, low, high, hint, name):
    if type(value) is not int or not low <= value <= high:
        raise PocketError(f"{name} must be an integer from {low} to {high}", hint=hint)
    return value


def validate_poll_args(after, route_ids, *, routes=None, kinds=None, max_events=64, max_bytes=65536,
                       wait_ms=0, max_evidence_age_ms=None, on_expired="refuse"):
    if (not isinstance(after, dict) or set(after) != {"runtime_epoch", "sequence"}
            or not isinstance(after["runtime_epoch"], str) or type(after["sequence"]) is not int
            or after["sequence"] < 0):
        raise PocketError("after must be a cursor with runtime_epoch and a sequence of at least 0",
                          hint=_CURSOR_HINT)
    _bounded_int(max_events, 1, MAX_EVENTS, _PAGE_HINT, "max_events")
    _bounded_int(max_bytes, MIN_PAGE_BYTES, MAX_PAGE_BYTES, _PAGE_HINT, "max_bytes")
    _bounded_int(wait_ms, 0, MAX_WAIT_MS, _PAGE_HINT, "wait_ms")
    if max_evidence_age_ms is not None:
        _bounded_int(max_evidence_age_ms, 1, MAX_EVIDENCE_AGE_MS,
                     "max_evidence_age_ms is 1 to 3600000 ms, or null for no age filter.",
                     "max_evidence_age_ms")
    if on_expired not in ("refuse", "resume_from_earliest"):
        raise PocketError("on_expired must be refuse or resume_from_earliest",
                          hint="Use on_expired 'refuse' or 'resume_from_earliest'.")
    if kinds is not None:
        if (not isinstance(kinds, list) or not kinds
                or not all(isinstance(k, str) and k in ("measurement", "health") for k in kinds)
                or len(set(kinds)) != len(kinds) or "health" not in kinds):
            raise PocketError("kinds must include health", hint=_KINDS_HINT)
        kinds = tuple(kinds)
    if routes is not None:
        if (not isinstance(routes, list) or not routes or not all(isinstance(r, str) for r in routes)
                or not set(routes) <= set(route_ids)):
            raise PocketError("routes must name configured route_ids", hint="listen_observe lists route_ids.")
        routes = tuple(routes)
    return {"after": dict(after), "routes": routes, "kinds": kinds, "max_events": max_events,
            "max_bytes": max_bytes, "wait_ms": wait_ms, "max_evidence_age_ms": max_evidence_age_ms,
            "on_expired": on_expired}


EVENTS_RECEIPT_STATUS = {"page": "ok", "cursor_expired": "conflict", "snapshot_required": "conflict",
                         "runtime_epoch_mismatch": "conflict", "runtime_not_running": "needs_input",
                         "runtime_unreachable": "outcome_unknown"}


def events_receipt_status(page):
    status = EVENTS_RECEIPT_STATUS[page["events_status"]]
    return "partial" if status == "ok" and page.get("missing") else status


def _refuse(field, rule):
    raise PocketError(f"Listen config field {field} is invalid", hint=f"{field} {rule}"[:400])


def _keys(value, name, required, optional=()):
    if not isinstance(value, dict):
        _refuse(name, "must be an object.")
    missing = [key for key in required if key not in value]
    unknown = sorted(map(str, set(value) - set(required) - set(optional)))
    if missing or unknown:
        allowed = ", ".join([*required, *(f"{key} (optional)" for key in optional)]) or "no fields"
        problem = f"is missing {', '.join(missing)}" if missing else f"has unknown {', '.join(unknown)}"
        _refuse(name, f"{problem}; it takes {allowed}.")
    return value


def _int(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        _refuse(name, f"must be an integer from {low} to {high}.")
    return value


def _number(value, low, high, name, unit):
    # The range comes first: Python compares an int with a float exactly, while isfinite would convert an
    # int too large for a float and raise OverflowError instead of refusing it.
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high
            or not math.isfinite(value)):
        _refuse(name, f"must be a finite number from {low:g} to {high:g} {unit}.")
    return float(value)


def _adapter(value):
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str) \
            or value["kind"] not in ("synthetic", "coreaudio"):
        _refuse("adapter.kind", "must be synthetic or coreaudio.")
    if value["kind"] == "synthetic":
        _keys(value, "adapter", ("kind", "script"))
        from .listen_adapters import validate_synthetic_script
        return {"kind": "synthetic", "script": validate_synthetic_script(value["script"])}
    _keys(value, "adapter", ("kind", "build_id"))
    if not isinstance(value["build_id"], str) or not listen_native.BUILD_ID.fullmatch(value["build_id"]):
        _refuse("adapter.build_id", "must be the build_id listen_build_helper returned, such as "
                "20261023T101500Z-0123abcd.")
    return {"kind": "coreaudio", "build_id": value["build_id"]}


def _device(value, adapter):
    if isinstance(value, dict) and "name" in value:
        raise PocketError("Listen selects an input by uid, never by name", hint=_NAME_HINT)
    _keys(value, "device", ("uid", "expected_sample_rate"))
    uid, rate = value["uid"], value["expected_sample_rate"]
    if not isinstance(uid, str) or not 1 <= len(uid) <= 256 or not uid.isprintable():
        _refuse("device.uid", "must be 1 to 256 printable characters, exactly as listen_devices lists it.")
    if uid.startswith("synthetic:") != (adapter["kind"] == "synthetic"):
        raise PocketError("device.uid is in the wrong namespace for this adapter", hint=_NAMESPACE_HINT)
    if adapter["kind"] == "synthetic" and uid not in {d["uid"] for d in adapter["script"]["devices"]}:
        _refuse("device.uid", "must equal one uid in adapter.script.devices.")
    if type(rate) is not int or rate not in SAMPLE_RATES:
        _refuse("device.expected_sample_rate",
                "must be one of 44100, 48000, 88200 or 96000; Listen never resamples.")
    return {"uid": uid, "expected_sample_rate": rate}


def _routes(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 4:
        _refuse("routes", "must list 1 to 4 audio routes, each with route_id, role and channels.")
    rows = []
    for index, route in enumerate(value):
        name = f"routes[{index}]"
        _keys(route, name, ("route_id", "role", "channels"))
        route_id, role, channels = route["route_id"], route["role"], route["channels"]
        if not isinstance(route_id, str) or not _ROUTE_ID.fullmatch(route_id):
            _refuse(f"{name}.route_id", "must be a lowercase letter, then up to 31 of a-z, 0-9 and -.")
        if not isinstance(role, str) or role not in _ROLE_LIMITS:
            _refuse(f"{name}.role", "must be master, cue or unlabeled.")
        named = {r for r in ("cue", "master") if any(w.startswith(r) for w in route_id.split("-"))}
        if named - {role}:
            raise PocketError(f"Route {route_id} names a role it doesn't have", hint=_ROLE_WORD_HINT)
        if (not isinstance(channels, list) or not 1 <= len(channels) <= 2
                or not all(type(c) is int and 0 <= c <= 63 for c in channels)
                or len(set(channels)) != len(channels)):
            _refuse(f"{name}.channels", "must list 1 or 2 distinct 0-based input channels from 0 to 63; "
                                        "channel 0 is the device's first input.")
        rows.append({"route_id": route_id, "role": role, "channels": list(channels)})
    ids = [r["route_id"] for r in rows]
    if len(set(ids)) != len(ids):
        _refuse("routes", "must give each audio route its own route_id.")
    if any(sum(r["role"] == role for r in rows) > limit for role, limit in _ROLE_LIMITS.items()):
        _refuse("routes", "allow at most one master, one cue and two unlabeled routes.")
    if all(r["role"] == "cue" for r in rows):
        _refuse("routes", "must include a master or unlabeled route; cue isn't monitored without a "
                          "routing test.")
    declared = [c for r in rows for c in r["channels"]]
    if len(set(declared)) != len(declared):
        raise PocketError("Two audio routes share a channel", hint=_OVERLAP_HINT)
    if len(declared) > 4:
        _refuse("routes", "declare at most 4 channels in total.")
    return rows


_REQUIRED_MEASUREMENT = {
    "clip_threshold_dbfs": ("measurement.clip_threshold_dbfs is required: a number from -20 to 0 dBFS. "
                            "Listen has no default threshold."),
    "near_silence": ("measurement.near_silence is required: {level_dbfs from -120 to -20, min_duration_ms "
                     "from 100 to 600000}. Listen has no default near-silence level or duration."),
}
_REQUIRED_NEAR_SILENCE = {
    "level_dbfs": ("measurement.near_silence.level_dbfs is required: a number from -120 to -20 dBFS. "
                   "Listen has no default near-silence level."),
    "min_duration_ms": ("measurement.near_silence.min_duration_ms is required: an integer from 100 to "
                        "600000. Listen has no default near-silence duration."),
}


def _measurement(value, rate, ring_frames):
    if not isinstance(value, dict):
        _refuse("measurement", "must be an object with clip_threshold_dbfs and near_silence.")
    for key, hint in _REQUIRED_MEASUREMENT.items():
        if key not in value:
            raise PocketError(f"measurement.{key} is required", hint=hint)
    _keys(value, "measurement", ("clip_threshold_dbfs", "near_silence"), ("window_frames",))
    near = value["near_silence"]
    for key, hint in _REQUIRED_NEAR_SILENCE.items():
        if isinstance(near, dict) and key not in near:
            raise PocketError(f"measurement.near_silence.{key} is required", hint=hint)
    _keys(near, "measurement.near_silence", ("level_dbfs", "min_duration_ms"))
    window = _int(value.get("window_frames", rate // 10), 32, rate, "measurement.window_frames")
    if window > ring_frames // 4:
        _refuse("measurement.window_frames", f"must be at most a quarter of the ring: {ring_frames // 4} "
                                             "frames at this buffering.ring_seconds and sample rate.")
    level = _number(near["level_dbfs"], -120.0, -20.0, "measurement.near_silence.level_dbfs", "dBFS")
    duration = _int(near["min_duration_ms"], 100, 600000, "measurement.near_silence.min_duration_ms")
    return {"clip_threshold_dbfs": _number(value["clip_threshold_dbfs"], -20.0, 0.0,
                                           "measurement.clip_threshold_dbfs", "dBFS"),
            "near_silence": {"level_dbfs": level, "min_duration_ms": duration},
            "window_frames": window}


def _section(config, section):
    bounds = SETTINGS[section]
    value = _keys(config.get(section, {}), section, (), tuple(bounds))
    return {key: _int(value.get(key, setting.default), setting.low, setting.high, f"{section}.{key}")
            for key, setting in bounds.items()}


def _verify_build(build_id):
    if sys.platform != "darwin":
        raise PocketError("The CoreAudio adapter runs on macOS only", code="unsupported_profile",
                          hint=_MACOS_HINT)
    build = listen_native.verify_build(build_id)
    return {key: build[key] for key in _BUILD_KEYS}


def _validate_config(config):
    if not isinstance(config, dict):
        _refuse("config", "must be an object with adapter, device, routes and measurement.")
    _keys(config, "config", ("adapter", "device", "routes", "measurement"), tuple(SETTINGS))
    adapter = _adapter(config["adapter"])
    device = _device(config["device"], adapter)
    routes = _routes(config["routes"])
    sections = {section: _section(config, section) for section in SETTINGS}
    rate = device["expected_sample_rate"]
    measurement = _measurement(config["measurement"], rate, sections["buffering"]["ring_seconds"] * rate)
    if measurement["window_frames"] * sections["delivery"]["meter_coalesce_windows"] < rate // 100:
        _refuse("delivery.meter_coalesce_windows",
                f"times measurement.window_frames must cover at least 10 ms of audio ({rate // 100} frames "
                f"at {rate} Hz), so each route publishes at most 100 level events a second.")
    stale = sections["health"]["stale_after_ms"]
    if adapter["kind"] == "synthetic":
        block = adapter["script"]["block_frames"]
        opened = next(d["sample_rate"] for d in adapter["script"]["devices"] if d["uid"] == device["uid"])
        if not stale_after_fits_block(stale, block, opened):
            period = block * 1000 / opened
            _refuse("health.stale_after_ms",
                    f"must be more than twice the block period: {block}-frame blocks at {opened} Hz arrive "
                    f"{period:.1f} ms apart, so use more than {2 * period:.0f} ms, or a smaller "
                    "adapter.script.block_frames.")
    if measurement["window_frames"] * sections["delivery"]["meter_coalesce_windows"] * 1000 > stale * rate:
        _refuse("delivery.meter_coalesce_windows",
                f"times measurement.window_frames must cover at most health.stale_after_ms of audio "
                f"({stale * rate // 1000} frames at {rate} Hz), so a level stays current until the next is "
                "due and none spans more audio than that; use fewer or shorter windows, or a longer "
                "stale_after_ms.")
    build = _verify_build(adapter["build_id"]) if adapter["kind"] == "coreaudio" else None
    resolved = {"adapter": adapter, "device": device, "routes": routes, "measurement": measurement,
                **sections}
    canonical_bytes(resolved)
    return resolved, build


def _utc():
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _require_posix():
    if os.name != "posix":
        raise PocketError("Listen's runtime needs a POSIX system", code="unsupported_profile",
                          hint=_POSIX_HINT)


def _session(session_dir):
    root, _, record = listen_host.load_session(session_dir)
    if record["config"]["adapter"]["kind"] == "coreaudio" and sys.platform != "darwin":
        raise PocketError("This session uses the CoreAudio adapter, which runs on macOS only",
                          code="unsupported_profile", hint=_MACOS_HINT)
    return root, record


def _audio_source(session, *, read=True):
    if session["config"]["adapter"]["kind"] == "synthetic":
        return "synthetic_script"
    return "device_input" if read else "none"


def _no_audio_read(source):
    return "none" if source == "device_input" else source


def _record_source(record, source):
    own = (record.get("coverage") or {}).get("audio_source")
    return own if own in ("synthetic_script", "device_input", "none") else _no_audio_read(source)


def _session_routes(routes):
    resolved, _ = resolve_routes(routes, 64)
    keys = ("route_id", "role", "channels", "metering", "semantic_status", "route_calibration_id")
    return [{**{k: r[k] for k in keys}, **({"isolated_cue": "unavailable"} if r["role"] == "cue" else {})}
            for r in resolved]


def _session_notices(config):
    roles = {r["role"] for r in config["routes"]}
    return ([notice("master_label_untested")] if "master" in roles else []) + \
           ([notice("cue_not_established")] if "cue" in roles else []) + \
           ([notice("synthetic_audio")] if config["adapter"]["kind"] == "synthetic" else [])


def _memory(config, metered):
    rate, channels = config["device"]["expected_sample_rate"], len(metered)
    native = config["adapter"]["kind"] == "coreaudio"
    sizes = {"pcm_ring_bytes": config["buffering"]["ring_seconds"] * rate * channels * 4,
             "helper_ring_bytes": listen_native.helper_ring_frames(rate) * channels * 4 if native else None,
             "adapter_queue_max_bytes": (listen_native.ADAPTER_QUEUE_FRAMES * listen_native.MAX_BLOCK_FRAMES
                                         * channels * 4) if native else None,
             "adapter_decoder_max_bytes": listen_native.DECODER_BUFFER_BYTES if native else None,
             "descriptor_ring_bytes": DESCRIPTOR_CAPACITY * DESCRIPTOR_BYTES,
             "measurement_scratch_bytes": config["measurement"]["window_frames"] * channels * 4,
             "broker_max_bytes": config["delivery"]["normal_retention_bytes"]
             + config["delivery"]["health_retention_bytes"]}
    return {**sizes, "total_declared_bytes": sum(v for v in sizes.values() if v is not None),
            "not_in_total": {"journal_queue_lines": listen_host.JOURNAL_QUEUE_LINES,
                             "journal_line_max_bytes": listen_host.JOURNAL_LINE_BYTES,
                             "facts_queue_items": FACTS_CAPACITY, "python_object_overhead": "not_counted"}}


def _disk():
    sizes = {"journal_budget_bytes": listen_host.JOURNAL_BUDGET_BYTES,
             "host_stderr_cap_bytes": listen_host.STDERR_CAP_BYTES}
    sealed = (listen_host.LAUNCH_NAME, listen_host.STARTUP_FAILURE_NAME)
    records = [*sealed, *(f"{name}.sha256" for name in sealed), listen_host.CONNECTION_NAME,
               listen_host.TERMINAL_POINTER_NAME]
    return {**sizes, "scope": "per_runtime",
            "per_runtime_total_declared_bytes": sum(sizes.values()),
            "not_in_total": {"runtime_directory_records": records,
                             "session_records": ["terminal_record", "start_and_stop_request_journals"]},
            "runtimes_max": listen_host.MAX_ORDINAL}


def _new_directory(output_dir):
    if not isinstance(output_dir, str) or not output_dir:
        raise PocketError("output_dir must name a new local directory", hint=_DIRECTORY_HINT)
    named = Path(output_dir).expanduser()
    try:
        named.parent.mkdir(parents=True, exist_ok=True)
    except FileExistsError as error:
        raise PocketError(f"Can't create {named}: {named.parent} isn't a directory", code="io_error",
                          hint=_WRITABLE_HINT) from error
    except OSError as error:
        raise PocketError(f"Can't create {named}: {error}", code="io_error", hint=_WRITABLE_HINT) from error
    try:
        if listen_native.volume_ignores_ownership(str(named.parent)):
            raise PocketError(f"{named.parent} is on a volume that ignores ownership", code="io_error",
                              hint=_OWNERSHIP_HINT)
        named.mkdir(mode=0o700, exist_ok=False)
        descriptor = os.open(named, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileExistsError as error:
        raise PocketError(f"{named} already exists", code="io_error", hint=_DIRECTORY_HINT) from error
    except OSError as error:
        raise PocketError(f"Can't create {named}: {error}", code="io_error", hint=_WRITABLE_HINT) from error
    try:
        created = os.fstat(descriptor)
        os.fchmod(descriptor, 0o700)
        listen_native.clear_acl(descriptor)
        resolved = named.resolve(strict=True)
        found = os.stat(resolved)
    except OSError as error:
        raise PocketError(f"Can't create {named}: {error}", code="io_error", hint=_WRITABLE_HINT) from error
    finally:
        os.close(descriptor)
    if (created.st_uid != os.getuid() or (found.st_dev, found.st_ino) != (created.st_dev, created.st_ino)):
        raise PocketError(f"{named} changed while Listen created it", code="io_error", hint=_DIRECTORY_HINT)
    return resolved


def _terminal_summary(handle, record, *, include_handle=True):
    capture = record.get("capture") or {}
    refusal = capture.get("refusal")
    summary = {"runtime_epoch": record.get("runtime_epoch"), "ordinal": record.get("ordinal"),
               "terminal": "sealed", "state": record.get("state"), "cause": record.get("cause"),
               "stop_request_id": record.get("stop_request_id"),
               "capture": {"final_state": capture.get("final_state"),
                           "ended_cause": capture.get("ended_cause"),
                           "refusal_reasons": refusal.get("reasons") if isinstance(refusal, dict) else None,
                           "missing": capture.get("missing"), "after_end": capture.get("after_end"),
                           "stale_episodes": capture.get("stale_episodes")},
               "routes": record.get("routes"), "worker": record.get("worker"),
               "broker": record.get("broker"), "journal": record.get("journal"), "stop": record.get("stop")}
    return {**summary, "terminal_handle": handle} if include_handle else summary


def _runtime_record(root, row):
    ordinal, epoch, path = row
    try:
        terminal = listen_host.read_terminal(root, path)
    except PocketError:
        return {"runtime_epoch": epoch, "ordinal": ordinal, "terminal": "integrity_mismatch"}
    if terminal is not None:
        return _terminal_summary(*terminal)
    journal = listen_host.verify_journal(path / listen_host.JOURNAL_NAME)
    return {"runtime_epoch": epoch, "ordinal": ordinal, "terminal": "missing",
            "journal": {k: journal[k] for k in _JOURNAL_KEYS}}


def _newest_record(root):
    newest = listen_host.newest_runtime(root)
    return None if newest is None else _runtime_record(root, newest)


def _current(root):
    if os.name != "posix":
        return {"state": "unknown", "runtime_epoch": None}
    current = listen_host.current_runtime(root)
    running = current["state"] in ("running", "unreachable")
    return {"state": current["state"], "runtime_epoch": current["runtime_epoch"] if running else None}


def _replayed(result, root):
    return {**result, "replayed": True, "current_runtime": _current(root)}


def _unreachable_reason(error):
    if error.reason == "lease_held_no_connection":
        return "lease_held_no_connection"
    return "lease_held_socket_unreachable"


def listen_build_helper() -> dict:
    """Compile the optional macOS CoreAudio input helper into a new build folder of Listen's own and seal it.

    Runs only the Swift compiler, never the helper: no device is opened and no capture starts. The
    receipt's build_id names the build from then on; build_dir only says where it is.
    """
    built = listen_native.build_helper()
    adapter = canonical_bytes({"kind": "coreaudio", "build_id": built["build_id"]}).decode()
    return receipt(build={**built["build"], "build_id": built["build_id"], "build_dir": built["build_dir"],
                          "build_json_sha256": built["build_json_sha256"],
                          "build_status": "built_not_started"},
                   compiler_messages=built["compiler_messages"],
                   coverage=coverage("none", device_opened=False, capture_started=False,
                                     helper_executed=False, native_acceptance="not_performed"),
                   next_actions=[f"listen_devices with {adapter} lists inputs by uid; it opens no stream"])


def listen_devices(adapter: ListenAdapter) -> dict:
    """List inputs by stable uid, with shared display names flagged. Opens no stream.

    The synthetic adapter lists its script's devices and runs nothing. The CoreAudio adapter runs
    the sealed helper's read-only list-devices mode once, from a fresh copy it removes afterwards.
    """
    resolved = _adapter(adapter)
    if resolved["kind"] == "synthetic":
        from .listen_adapters import synthetic_inventory
        rows = list(synthetic_inventory(resolved["script"]))
        return receipt(devices=rows, devices_omitted=len(resolved["script"]["devices"]) - len(rows),
                       devices_unreadable=0, adapter_profile=SYNTHETIC_PROFILE,
                       microphone_authorization="not_applicable", macos_version="not_applicable", build=None,
                       notices=order_notices(_device_notices(rows, "not_applicable")
                                             + [notice("synthetic_audio")]),
                       coverage=coverage("synthetic_script", device_opened=False, stream_opened=False,
                                         helper_executed=False, helper_mode=None),
                       next_actions=["listen_prepare with device.uid set to one of these uids"])
    if sys.platform != "darwin":
        raise PocketError("The CoreAudio adapter runs on macOS only", code="unsupported_profile",
                          hint=_MACOS_HINT)
    probe = listen_native.probe_devices(resolved["build_id"])
    build = {key: probe["build"][key] for key in _BUILD_KEYS}
    probed = coverage("none", device_opened=False, stream_opened=False, helper_executed=True,
                      helper_mode="list-devices")
    if probe["status"] != "ok":
        return receipt(status="failed", probe=probe["probe"], build=build, adapter_profile=COREAUDIO_PROFILE,
                       coverage=probed,
                       next_actions=[("The helper's list-devices probe failed; probe.stderr_tail says "
                                      "why. Build the helper again with listen_build_helper if it keeps "
                                      "failing.")])
    rows = probe["devices"]
    authorization, unreadable = probe["microphone_authorization"], probe["devices_unreadable"]
    return receipt(devices=rows, devices_omitted=probe["devices_omitted"], devices_unreadable=unreadable,
                   adapter_profile=COREAUDIO_PROFILE,
                   microphone_authorization=authorization, macos_version=probe["macos_version"], build=build,
                   notices=order_notices(_device_notices(rows, authorization, unreadable)), coverage=probed,
                   next_actions=["listen_prepare with device.uid set to one of these uids"])


def _device_notices(rows, authorization, unreadable=None):
    shared = [row["name"] for row in rows if row.get("display_name_shared")]
    permission = authorization not in ("authorized", "not_applicable")
    return ([notice("display_name_shared", name=shared[0])] if shared else []) + \
           ([notice("devices_unreadable", count=unreadable)] if unreadable else []) + \
           ([permission_notice(authorization)] if permission else [])


def listen_prepare(output_dir: str, config: ListenConfig) -> dict:
    """Seal one input by uid, its declared audio routes and bounded settings into a new session.

    Validation fills in every default and seals it in the session record. The receipt echoes the
    resolved settings sections under session.settings; the record also holds the adapter's. Runs no
    subprocess,
    opens no device and starts no capture; for CoreAudio it only reads the sealed helper build.
    """
    _require_posix()
    resolved, build = _validate_config(config)
    root = _new_directory(output_dir)
    routes = _session_routes(resolved["routes"])
    metered = [c for r in routes if r["metering"] == "metered" for c in r["channels"]]
    memory, disk = _memory(resolved, metered), _disk()
    profile = ADAPTER_PROFILES[resolved["adapter"]["kind"]]
    sha = digest(resolved)
    record = {"schema": listen_host.SESSION_SCHEMA, "session_id": secrets.token_hex(16),
              "created_wall_utc": _utc(), "config": resolved, "config_sha256": sha,
              "adapter_profile": profile, "build": build, "metered_channels": metered, "routes": routes,
              "memory": memory, "disk": disk, "retention": RETENTION}
    lease = root / listen_host.LEASE_NAME
    os.close(os.open(lease, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600))
    handle = put_record(record, root)
    with (root / listen_host.SESSION_POINTER).open("xb") as stream:
        stream.write(canonical_bytes({"schema": listen_host.SESSION_POINTER_SCHEMA, "session": handle}))
    settings = {key: resolved[key] for key in ("device", "measurement", *SETTINGS)}
    return receipt(artifacts={"session": handle},
                   session={"session_dir": str(root), "session_id": record["session_id"],
                            "config_sha256": sha, "adapter_profile": profile, "build": build,
                            "routes": routes, "metered_channels": metered, "settings": settings,
                            "memory": memory, "disk": disk, "retention": RETENTION},
                   coverage=coverage("synthetic_script" if build is None else "none", capture="not_started",
                                     device_opened=False),
                   notices=order_notices(_session_notices(resolved)),
                   next_actions=[(f"listen_start with session_dir, a new request_id and "
                                 f"expected_config_sha256 {sha}")])


def _check_build(session):
    if session["config"]["adapter"]["kind"] != "coreaudio":
        return
    build = _verify_build(session["config"]["adapter"]["build_id"])
    sealed = session["build"]
    if (build["helper_sha256"], build["build_json_sha256"]) != (sealed.get("helper_sha256"),
                                                               sealed.get("build_json_sha256")):
        raise PocketError("The helper build changed since listen_prepare", code="evidence_mismatch",
                          hint="Build the helper again with listen_build_helper, then prepare a new session.")


def listen_start(session_dir: str, request_id: str, expected_config_sha256: str) -> dict:
    """Start one owned local capture runtime for a prepared session; copies input only.

    It never changes a device, route or deck. A repeated request_id replays the recorded receipt,
    including a conflict or an unknown outcome, so a new attempt needs a new request_id.
    """
    _require_posix()
    root, session = _session(session_dir)
    if not isinstance(expected_config_sha256, str) or expected_config_sha256 != session["config_sha256"]:
        raise PocketError("expected_config_sha256 doesn't match this session", code="stale_revision",
                          hint=_SHA_HINT)
    if not _completed(root, request_id):
        _check_build(session)
    executed = False

    def work():
        nonlocal executed
        executed = True
        return _launch(root, session, request_id)

    result = run_request(root, request_id, "listen_start",
                         {"expected_config_sha256": expected_config_sha256}, work)
    return result if executed else _replayed(result, root)


def _launch(root, session, request_id):
    fd = listen_host.acquire_lease(root)
    if fd is None:
        return receipt(request_id, status="conflict", start_outcome="already_running",
                       runtime_conflict="already_running",
                       running_runtime_epoch=_current(root)["runtime_epoch"],
                       replayed=False, coverage=coverage(_audio_source(session, read=False), capture="already_running"),
                       next_actions=["Call listen_observe to watch the running runtime",
                                     "listen_stop with its runtime_epoch and a new request_id stops it"])
    try:
        runtime_dir, launch = listen_host.prepare_launch(root, request_id=request_id, launch="provider")
        process = listen_host.spawn_host(root, runtime_dir, fd)
    finally:
        os.close(fd)        # without LOCK_UN: the daemon's inherited description keeps the lease
    outcome = listen_host.wait_for_runtime(root, runtime_dir, process)
    runtime = {"schema": RUNTIME_SCHEMA, "runtime_epoch": launch["runtime_epoch"],
               "ordinal": launch["ordinal"], "pid": None,
               "runtime_dir": str(Path(runtime_dir).relative_to(root)),
               "started_monotonic_ns": None, "scheduled_stop_monotonic_ns": None}
    common = {"start_outcome": outcome["outcome"], "replayed": False,
              "previous_runtime": launch["previous_runtime"]}
    build = {"capturing": _running_receipt, "started_no_audio_yet": _running_receipt,
             "capture_ended_during_start": _running_receipt, "capture_refused": _refused_receipt,
             "stopped_during_start": _stopped_receipt, "host_failed": _failed_receipt,
             "start_unconfirmed": _unconfirmed_receipt}[outcome["outcome"]]
    return build(root, session, request_id, runtime_dir, runtime, outcome, common)


def _observe_once(runtime_dir):
    try:
        return listen_host.call(runtime_dir, "observe", timeout_s=5.0)["observation"]
    except (listen_host.RuntimeUnreachable, PocketError, KeyError, TypeError):
        return None


def _device_changed(root, previous, device):
    rows = {epoch: path for _, epoch, path in listen_host.runtime_dirs(root)}
    if previous is None or not isinstance(device, dict) or previous.get("runtime_epoch") not in rows:
        return "unknown"
    try:
        terminal = listen_host.read_terminal(root, rows[previous["runtime_epoch"]])
    except PocketError:
        return "unknown"
    before = (terminal[1].get("capture") or {}).get("device") if terminal is not None else None
    if not isinstance(before, dict) or before.get("uid") is None:
        return "unknown"
    return (before.get("uid"), before.get("device_instance")) != (device.get("uid"),
                                                                device.get("device_instance"))


_CAPTURING_ACTIONS = ["listen_events with after set to this cursor reads every event from the start",
                      "listen_observe shows current levels and health; this receipt is a snapshot"]
_STOP_STEP = "listen_stop with this runtime_epoch and a new request_id stops it"
_NO_AUDIO_ACTIONS = {
    "stale": [("Input IO started, but no frame arrived while listen_start waited, so capture is stale "
               "(capture_no_frames). Check the source app and the device"), _STOP_STEP],
    None: [("Input IO started, but no frame had arrived when this receipt was written; listen_observe "
            "shows whether frames arrive"), _STOP_STEP],
}
_NO_SNAPSHOT_ACTIONS = [("The runtime answered, but its snapshot didn't arrive, so this receipt has no "
                         "routes or device; call listen_observe"),
                        "listen_events with after set to this cursor reads every event from the start"]
_ENDED_ACTIONS = [("listen_stop with this runtime_epoch and a new request_id ends the runtime; capture won't "
                   "resume"),
                  "Then fix the input and call listen_start with a new request_id"]
_STOPPING_STEP = ("The runtime began stopping ({cause}) while this start waited, so it isn't capturing. "
                  "listen_request_status with this request_id shows whether its terminal is sealed, and "
                  "listen_observe then summarizes it; listen_start with a new request_id starts again")
_STOPPED_UNVERIFIED_STEP = ("The runtime stopped ({cause}) while this start waited, and its terminal "
                            "record doesn't verify, so Listen can't say what it captured. Keep the session "
                            "directory unchanged; listen_start with a new request_id starts again")
_ROUTE_KEYS = ("route_id", "role", "channels", "channels_exposed", "availability", "metering",
               "semantic_status", "reason", "route_calibration_id")
_CAPTURE_KEYS = ("adapter", "device", "device_instance_epoch", "topology_epoch", "format", "clocks",
                 "stream_epoch")


def _running_receipt(root, session, request_id, runtime_dir, runtime, outcome, common):
    snapshot = _observe_once(runtime_dir)
    observation = snapshot or {}
    observed, capture = observation.get("runtime") or {}, observation.get("capture") or {}
    ping = outcome["ping"] or {}
    state = capture.get("state") or ping.get("capture_state") or "unknown"
    frames = capture.get("frames_received", ping.get("frames_received"))
    stopping = (observed.get("state") or ping.get("runtime_state")) in ("stopping", "stopped")
    ended = state in ("ended", "failed")
    if stopping:
        return _stopped_receipt(root, session, request_id, runtime_dir, runtime,
                                {"terminal": None, "exit_code": None,
                                 "stop_cause": (observed.get("stop") or {}).get("cause"),
                                 "ping": {"runtime_state": observed.get("state") or ping.get("runtime_state"),
                                          "frames_received": frames}},
                                {**common, "start_outcome": "stopped_during_start"})
    start_outcome = ("capture_ended_during_start" if ended
                     else "capturing" if snapshot is not None and type(frames) is int and frames > 0
                     else outcome["outcome"])
    if ended:
        status, actions = "partial", _ENDED_ACTIONS
    elif snapshot is None:
        status, actions = "partial", _NO_SNAPSHOT_ACTIONS
    elif start_outcome == "capturing":
        status, actions = "ok", _CAPTURING_ACTIONS
    else:
        status, actions = "partial", _NO_AUDIO_ACTIONS["stale" if state == "stale" else None]
    return receipt(request_id, status=status,
                   runtime={**runtime, **{k: observed.get(k) for k in ("pid", "started_monotonic_ns",
                                                                     "scheduled_stop_monotonic_ns")}},
                   cursor={"runtime_epoch": runtime["runtime_epoch"], "sequence": 0},
                   capture={"state": state,
                            "ended_cause": capture.get("ended_cause", ping.get("capture_cause")),
                            **{k: capture.get(k) for k in _CAPTURE_KEYS},
                            "frames_received": frames,
                            "routes": [{**{k: r.get(k) for k in _ROUTE_KEYS},
                                        **{k: r[k] for k in ("isolated_cue",) if k in r}}
                                       for r in observation.get("routes") or []]},
                   capture_snapshot="observed" if snapshot is not None else "unavailable",
                   device_instance_changed_since_previous_runtime=_device_changed(
                       root, common["previous_runtime"], capture.get("device")),
                   coverage=coverage(_audio_source(session, read=type(frames) is int and frames > 0),
                                     capture=state),
                   notices=order_notices(observation.get("notices") or _session_notices(session["config"])),
                   next_actions=actions, **{**common, "start_outcome": start_outcome})


_REFUSAL_STEPS = {
    "device_not_found": "listen_devices lists each input's uid; prepare a new session with one of them.",
    "device_not_alive": "Reconnect the input, then start again with a new request_id.",
    "device_hogged": ("Another app is using the input exclusively. Free it there, then start again with a "
                      "new request_id."),
    "sample_rate_mismatch": ("Prepare a new session with the device's sample rate as "
                             "device.expected_sample_rate; Listen never resamples."),
    "format_not_float32": "This input's format isn't 32-bit float; Listen never converts it.",
    "master_channels_not_exposed": ("The input has fewer channels than the master route declares; check "
                                    "listen_devices and prepare a new session."),
    "no_metered_channels_exposed": ("The input exposes none of the declared master or unlabeled channels; "
                                    "check listen_devices and prepare a new session."),
    "helper_build_changed": "Build the helper again with listen_build_helper, then prepare a new session.",
    "device_changed_before_start": ("The input's format, channel layout or a property Listen reads changed, or "
                                    "couldn't be read, just before capture began "
                                    "(capture_refusal.detail.exit_event_type says which). Check it with "
                                    "listen_devices, then prepare a new session if it changed."),
    "stale_after_below_block_period": ("This input's blocks arrive too far apart for health.stale_after_ms "
                                       "(capture_refusal.block_period has both); prepare a new session "
                                       "with a stale_after_ms above twice the block period."),
}
_HELPER_STEP = ("The helper didn't start cleanly; capture_refusal.detail says why. Start again with a new "
                "request_id.")


def _refusal_actions(reasons, authorization=None):
    steps = [_REFUSAL_STEPS.get(reason, _HELPER_STEP) for reason in reasons
             if reason != "microphone_not_authorized"]
    return list(dict.fromkeys(steps)) or [permission_notice(authorization)["next_step"]]


def _refused_receipt(root, session, request_id, runtime_dir, runtime, outcome, common):
    handle, record = outcome["terminal"]
    refusal = (record.get("capture") or {}).get("refusal") or {}
    reasons = list(refusal.get("reasons") or [])
    detail = refusal.get("detail") if isinstance(refusal.get("detail"), dict) else {}
    authorization = detail.get("microphone_authorization") or "unknown"
    permission_only = (reasons == ["microphone_not_authorized"]
                       and authorization in _AUTHORIZATIONS_THE_OPERATOR_CAN_CHANGE)
    permission = "microphone_not_authorized" in reasons
    return receipt(request_id, status="needs_input" if permission_only else "unsupported",
                   capture_refusal={"reasons": reasons, "refused_at": refusal.get("refused_at"),
                                    "inventory": None if refusal.get("inventory") is None
                                    else list(refusal["inventory"])[:32],
                                    "detail": refusal.get("detail"),
                                    **{key: refusal[key] for key in ("open_detail", "block_period")
                                       if key in refusal}},
                   refused_runtime={**runtime, "terminal": handle, "exit_code": outcome["exit_code"]},
                   coverage=coverage(_audio_source(session, read=False), capture="refused"),
                   notices=[permission_notice(authorization)] if permission else [],
                   next_actions=_refusal_actions(reasons, authorization), **common)


def _stopped_receipt(root, session, request_id, runtime_dir, runtime, outcome, common):
    terminal, reason = outcome["terminal"], None
    if terminal is None:
        try:
            terminal = listen_host.read_terminal(root, runtime_dir)
        except PocketError:
            reason = "terminal_integrity_mismatch"
    if terminal is None:
        frames = (outcome["ping"] or {}).get("frames_received")
        cause = outcome.get("stop_cause") or "a stop request"
        step = (_STOPPED_UNVERIFIED_STEP if reason else _STOPPING_STEP).format(cause=cause)
        return receipt(request_id, status="partial",
                       stopped_runtime={**runtime, "terminal": None, "exit_code": outcome["exit_code"]},
                       runtime_state=(outcome["ping"] or {}).get("runtime_state"),
                       **({"reason": reason} if reason else {}),
                       coverage=coverage(_audio_source(session, read=type(frames) is int and frames > 0),
                                         capture="stopped"),
                       next_actions=[step], **common)
    handle, record = terminal
    return receipt(request_id, status="partial",
                   stopped_runtime={**runtime, "terminal": handle, "exit_code": outcome["exit_code"]},
                   terminal=_terminal_summary(handle, record, include_handle=False),
                   coverage=coverage(_record_source(record, _audio_source(session)), capture="stopped"),
                   next_actions=[(f"The runtime was stopped ({record.get('cause')}) while this start waited "
                                  "for audio; terminal says what it captured. listen_start with a new "
                                  "request_id starts again")], **common)


def _failed_receipt(root, session, request_id, runtime_dir, runtime, outcome, common):
    failure, terminal = outcome["startup_failure"] or {}, outcome["terminal"]
    error = failure.get("error") or (terminal[1].get("error") if terminal else None)
    if terminal is not None and not failure:
        final = (terminal[1].get("capture") or {}).get("final_state")
        extra = {"terminal": _terminal_summary(terminal[0], terminal[1], include_handle=False)}
        step = ("The runtime started, then failed while listen_start waited; startup_failure.error and "
                "terminal say why and what its capture did. Start again with a new request_id after fixing "
                "it.")
    elif outcome.get("startup_failure_record") == "unverified":
        final, extra = "not_started", {}
        step = ("The runtime exited during startup, and its startup-failure record doesn't verify; "
                "startup_failure.stderr_tail says what it printed. Start again with a new request_id after "
                "fixing it.")
    elif failure.get("stage") == "after_capture_started":
        final, extra = "unknown", {}
        step = ("The runtime failed after its capture started, and its terminal record couldn't be sealed, "
                "so no record summarizes what it captured; its journal in the runtime directory and "
                "startup_failure.stderr_tail are what remain. Start again with a new request_id after "
                "fixing it.")
    elif failure.get("stage") == "refused":
        final, extra = "refused", {}
        step = ("Capture was refused (startup_failure.refusal.reasons says why), but the runtime couldn't seal "
                "its terminal record, such as on a full disk; startup_failure.stderr_tail says what it printed. "
                "Fix both, then start again with a new request_id.")
    elif outcome.get("startup_failure_record") == "missing":
        final, extra = "not_started", {}
        step = ("The runtime exited during startup without recording why; startup_failure.exit_code and "
                "startup_failure.stderr_tail are what remain. Start again with a new request_id after fixing "
                "it.")
    else:
        final, extra = "not_started", {}
        step = ("The runtime exited during startup; startup_failure says why. Start again with a new "
                "request_id after fixing it.")
    return receipt(request_id, status="failed",
                   startup_failure={"stage": failure.get("stage"), "error": error,
                                    **({"refusal": failure["refusal"]} if "refusal" in failure else {}),
                                    "record": outcome.get("startup_failure_record"),
                                    "exit_code": outcome["exit_code"],
                                    "stderr_tail": (outcome["stderr_tail"] or "")[-2048:] or None},
                   failed_runtime={**runtime, "terminal": terminal[0] if terminal else None},
                   coverage=coverage(_record_source(terminal[1], _audio_source(session)) if terminal
                                     else _audio_source(session, read=False), capture=final or "unknown"),
                   next_actions=[step], **extra, **common)


def _unconfirmed_receipt(root, session, request_id, runtime_dir, runtime, outcome, common):
    connected = (Path(runtime_dir) / listen_host.CONNECTION_NAME).exists()
    return receipt(request_id, status="outcome_unknown", runtime=runtime, lease_state="held",
                   reason="lease_held_socket_unreachable" if connected else "lease_held_no_connection",
                   coverage=coverage(_audio_source(session, read=False), capture="unknown"),
                   next_actions=["Call listen_observe; the runtime may still be starting",
                                 "listen_request_status shows this request; it won't run again"], **common)


def listen_observe(session_dir: str, expected_runtime_epoch: str | None = None) -> dict:
    """Read fresh capture, clock, stream, route and delivery health from the running runtime.

    Returns the observation with a cursor for listen_events. Nothing is cached. A named epoch
    that differs from the running one is reported as a field, never as an error.
    """
    _require_posix()
    root, session = _session(session_dir)
    if expected_runtime_epoch is not None and not isinstance(expected_runtime_epoch, str):
        raise PocketError("expected_runtime_epoch must be a runtime epoch or null", hint=_EPOCH_HINT)
    current = listen_host.current_runtime(root)

    def matches(epoch):
        if expected_runtime_epoch is None:
            return {}
        return {"expected_runtime_epoch_matches": epoch is not None and expected_runtime_epoch == epoch}

    def not_running(state):
        return receipt(status="needs_input", runtime={"state": state, "runtime_epoch": None},
                       previous_runtime=_newest_record(root), notices=[notice("runtime_not_running")],
                       coverage=coverage(_audio_source(session, read=False), capture="not_running"),
                       next_actions=["listen_start with a new request_id"], **matches(None))

    def unreachable(epoch, reason):
        return receipt(status="outcome_unknown", reason=reason,
                       runtime={"state": "unreachable", "runtime_epoch": epoch},
                       coverage=coverage(_audio_source(session, read=False), capture="unknown"),
                       next_actions=["Call listen_observe again; Listen never signals a process",
                                     "listen_request_status shows the start request"], **matches(epoch))

    if current["state"] in ("never_started", "not_running"):
        return not_running(current["state"])
    if current["state"] == "unreachable" or current["runtime_dir"] is None:
        return unreachable(current["runtime_epoch"], "lease_held_no_connection")
    try:
        result = listen_host.call(current["runtime_dir"], "observe", timeout_s=5.0)
    except listen_host.RuntimeUnreachable as error:
        again = listen_host.current_runtime(root)
        if again["state"] in ("never_started", "not_running"):
            return not_running(again["state"])
        return unreachable(again["runtime_epoch"], _unreachable_reason(error))
    observation, cursor = result["observation"], result["cursor"]
    found = matches(cursor["runtime_epoch"])
    changed = ([] if found.get("expected_runtime_epoch_matches", True) else
               [notice("runtime_epoch_changed", runtime_epoch=cursor["runtime_epoch"])])
    return receipt(status="ok", observation=observation, cursor=cursor,
                   notices=order_notices(observation["notices"] + changed), coverage=observation["coverage"],
                   next_actions=["listen_events with after set to this cursor reads what happens next"],
                   **found)


def listen_events(session_dir: str, after: ListenCursor, routes: list[str] | None = None,
                  kinds: list[ListenEventKind] | None = None, max_events: int = 64, max_bytes: int = 65536,
                  wait_ms: int = 0, max_evidence_age_ms: int | None = None,
                  on_expired: ListenOnExpired = "refuse") -> dict:
    """Read bounded events after a cursor: at most 256 events and 256 KiB, waiting at most 1 s.

    Lost events show as missing ranges (status partial, or needs_input or conflict when the
    cursor's runtime has stopped or another is running), cursor_expired or snapshot_required.
    When the cursor's runtime left no terminal that verifies, missing can be empty and the next
    step says loss isn't ruled out. Health events are never filtered by kind, route or age.
    """
    _require_posix()
    root, session = _session(session_dir)
    args = validate_poll_args(after, [r["route_id"] for r in session["config"]["routes"]], routes=routes,
                              kinds=kinds, max_events=max_events, max_bytes=max_bytes, wait_ms=wait_ms,
                              max_evidence_age_ms=max_evidence_age_ms, on_expired=on_expired)
    source = _audio_source(session)
    current = listen_host.current_runtime(root)
    request = {**args, "routes": None if args["routes"] is None else list(args["routes"]),
               "kinds": None if args["kinds"] is None else list(args["kinds"])}
    reason = "lease_held_no_connection"
    if current["state"] == "running" and current["runtime_dir"] is not None:
        try:
            page = listen_host.call(current["runtime_dir"], "events", request,
                                    timeout_s=args["wait_ms"] / 1000 + 3)
        except listen_host.RuntimeUnreachable as error:
            reason, current = _unreachable_reason(error), listen_host.current_runtime(root)
        else:
            return _events_receipt({k: v for k, v in page.items() if k != "status"}, source, root)
    if current["state"] in ("never_started", "not_running"):
        cursor_runtime, missing = _unread_after_stop(root, args["after"])
        return receipt(status="needs_input", events_status="runtime_not_running",
                       runtime={"state": current["state"], "runtime_epoch": None}, missing=missing,
                       cursor_runtime=cursor_runtime,
                       previous_runtime=_newest_record(root), notices=[notice("runtime_not_running")],
                       coverage=coverage(_audio_source(session, read=False), capture="not_running"),
                       next_actions=[*_unread_steps(cursor_runtime, missing), _RESTART_STEP])
    return receipt(status="outcome_unknown", events_status="runtime_unreachable", reason=reason,
                   runtime={"state": "unreachable", "runtime_epoch": current["runtime_epoch"]},
                   coverage=coverage(_audio_source(session, read=False), capture="unknown"),
                   next_actions=["Call listen_observe; Listen never signals a process"])


_RESTART_STEP = "listen_start with a new request_id, then read events with its cursor"
_UNREAD_STEP = ("Sequences {from_sequence} to {through_sequence} of the cursor's runtime were published "
                "after your cursor and can't be read now; its terminal summarizes them, and its journal in "
                "the runtime directory keeps its health events")
_UNREAD_INCOMPLETE_STEP = ("Sequences {from_sequence} to {through_sequence} of the cursor's runtime were "
                           "published after your cursor and can't be read now; its terminal summarizes them. "
                           "Its journal didn't keep every line (cursor_runtime.journal says how, and its "
                           "terminal's broker.publish_hook_errors counts events whose journal hook failed), "
                           "so some of their health events aren't on disk")
_UNREAD_NO_TERMINAL_STEP = ("Sequences {from_sequence} to at least {through_sequence} of the cursor's runtime "
                            "were published after your cursor and can't be read now. It sealed no terminal "
                            "(cursor_runtime.terminal is missing), so later events, such as levels, may have "
                            "been published and lost too; only its journal in the runtime directory remains, "
                            "and it holds health events only")
_UNREAD_NO_TERMINAL_JOURNAL = (". The journal also didn't keep every line (cursor_runtime.journal says how), so "
                               "some of their health events aren't on disk")
_LOST_UNKNOWN_STEPS = {
    "missing": ("The cursor's runtime sealed no terminal, and its journal records nothing after your cursor; "
                "events it holds no line for, such as levels, may still have been published after your "
                "cursor and lost"),
    "integrity_mismatch": ("The cursor's runtime's terminal doesn't verify, so Listen can't say what it "
                           "published after your cursor; events after it may have been lost"),
    "unknown": ("No runtime of this session has the cursor's runtime_epoch, so Listen can't say what it "
                "published after your cursor; events after it may have been lost. Use a cursor from this "
                "session's listen_start or listen_observe"),
}


def _unread_steps(cursor_runtime, missing):
    terminal = None if cursor_runtime is None else cursor_runtime["terminal"]
    if not missing:
        return [_LOST_UNKNOWN_STEPS[terminal]] if terminal in _LOST_UNKNOWN_STEPS else []
    first = missing[0]
    if first["through_is_lower_bound"]:
        kept = "" if first["health_in_range"] == "runtime_journal" else _UNREAD_NO_TERMINAL_JOURNAL
        return [_UNREAD_NO_TERMINAL_STEP.format(**first) + kept]
    step = _UNREAD_STEP if first["health_in_range"] == "runtime_journal" else _UNREAD_INCOMPLETE_STEP
    return [step.format(**first)]


def _journal_complete(record):
    journal = record.get("journal")
    if not isinstance(journal, dict):
        return False
    if record["terminal"] == "sealed":
        return _journal_kept_every_line(journal, record.get("broker") or {})
    return (journal.get("present") is True and journal.get("chain_ok") is True
            and journal.get("torn_tail_bytes") == 0 and journal.get("dropped_lines_marked") == 0
            and journal.get("stopped_writing") is None)


def _journal_kept_every_line(journal, broker):
    return (journal.get("state") == "writing" and journal.get("dropped_lines") == 0
            and journal.get("closed") is True and journal.get("lines_unwritten_at_close") == 0
            and broker.get("publish_hook_errors", 0) == 0)


def _unread_after_stop(root, after):
    row = _runtime_row(root, after["runtime_epoch"])
    record = None if row is None else _runtime_record(root, row)
    found = {"runtime_epoch": after["runtime_epoch"], "terminal": "unknown" if record is None
             else record["terminal"], "last_sequence": None, "journal": None}
    if record is None or record["terminal"] == "integrity_mismatch":
        return found, []
    sealed = record["terminal"] == "sealed"
    last = (record.get("broker") or {}).get("last_sequence") if sealed else record["journal"]["last_sequence"]
    found = {**found, "last_sequence": last if type(last) is int else None, "journal": record.get("journal")}
    if sealed and type(last) is int and last < after["sequence"]:
        raise PocketError(f"after.sequence {after['sequence']} is ahead of that runtime's last event {last}",
                          hint=CURSOR_AHEAD_HINT)
    if type(last) is not int or last <= after["sequence"]:
        return found, []
    kept = "runtime_journal" if _journal_complete(record) else "runtime_journal_incomplete"
    return found, [{"from_sequence": after["sequence"] + 1, "through_sequence": last,
                    "through_is_lower_bound": not sealed, "delivery_class": "all", "cause": "runtime_stopped",
                    "health_in_range": kept}]


_EVENTS_ACTIONS = {
    "cursor_expired": [("Repeat with on_expired resume_from_earliest to page on with exact missing ranges "
                        "and every health event still after your cursor, or call listen_observe for a fresh "
                        "cursor, which starts at the newest event and skips those health events")],
    "snapshot_required": [("Health events after your cursor were lost; call listen_observe and use its "
                           "cursor")],
}


def _events_receipt(page, source, root=None):
    outcome = page["events_status"]
    unread = {}
    if outcome == "page":
        actions = ((["Events listed in missing were evicted before you read them; their levels are gone"]
                    if page.get("missing") else [])
                   + (["Call listen_events again with after set to next_after"] if page.get("more_available")
                      else []))
    elif outcome == "runtime_epoch_mismatch":
        cursor_runtime, missing = ((None, []) if root is None or not isinstance(page.get("after"), dict)
                                   else _unread_after_stop(root, page["after"]))
        unread = {} if cursor_runtime is None else {"cursor_runtime": cursor_runtime, "missing": missing}
        actions = [*_unread_steps(cursor_runtime, missing),
                   (f"The running runtime is {page.get('current_runtime_epoch')}; listen_observe gives its "
                    "cursor")]
    else:
        actions = _EVENTS_ACTIONS[outcome]
    rest = {k: v for k, v in page.items() if k != "coverage"}
    return receipt(status=events_receipt_status(page), coverage=page.get("coverage") or coverage(source),
                   next_actions=actions, **rest, **unread)


def listen_stop(session_dir: str, request_id: str, expected_runtime_epoch: str) -> dict:
    """Stop the named runtime and seal its terminal record. Music and other apps are untouched.

    It stops only the runtime whose epoch you name. A repeated request_id replays the recorded
    receipt, so a new attempt needs a new request_id.
    """
    _require_posix()
    root, session = _session(session_dir)
    if not isinstance(expected_runtime_epoch, str) or not _EPOCH.fullmatch(expected_runtime_epoch):
        raise PocketError("expected_runtime_epoch must be a Listen runtime epoch", hint=_EPOCH_HINT)
    current = _current(root)
    if (current["state"] in ("running", "unreachable") and current["runtime_epoch"] != expected_runtime_epoch
            and not _completed(root, request_id)):
        raise PocketError(f"The running runtime is {current['runtime_epoch']}, not {expected_runtime_epoch}",
                          code="stale_revision", hint=_STALE_EPOCH_HINT)
    executed = False

    def work():
        nonlocal executed
        executed = True
        return _stop(root, session, request_id, expected_runtime_epoch)

    result = run_request(root, request_id, "listen_stop", {"expected_runtime_epoch": expected_runtime_epoch},
                         work)
    return result if executed else _replayed(result, root)


def _completed(root, request_id):
    try:
        return request_status(root, request_id).get("journal_state") == "complete"
    except PocketError:
        return False


def _runtime_row(root, epoch):
    return next((row for row in listen_host.runtime_dirs(root) if row[1] == epoch), None)


def _released(root, epoch):
    if not listen_host.lease_held(root):
        return True
    newest = listen_host.newest_runtime(root)
    return newest is not None and newest[1] != epoch


def _wait_released(root, seconds, epoch=None):
    deadline = time.monotonic() + seconds
    while not _released(root, epoch):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


_HELD = {"lease_released": False, "superseded_by_runtime_epoch": None}


def _lease_after(root, epoch):
    if not listen_host.lease_held(root):
        return {"lease_released": True, "superseded_by_runtime_epoch": None}
    newest = listen_host.newest_runtime(root)
    return {"lease_released": False,
            "superseded_by_runtime_epoch": newest[1] if newest is not None and newest[1] != epoch else None}


def _lease_step(lease):
    if lease["lease_released"]:
        return "listen_start with a new request_id starts a new runtime"
    if lease["superseded_by_runtime_epoch"] is not None:
        return (f"Runtime {lease['superseded_by_runtime_epoch']} now holds this session; call listen_observe "
                "to watch it")
    return "The terminal is sealed, but the runtime still holds its lease; call listen_observe"


_NO_TERMINAL_STEP = ("The runtime stopped without a sealed terminal record; its journal in the runtime "
                     "directory is the remaining evidence")
_DRAIN_STEP = ("Measurement didn't finish within the stop bound: terminal.worker.unmeasured_at_stop_frames "
               "received frames (and any terminal.worker.lag_frames) have no levels, and each route's totals "
               "cover only its frames_measured")
_UPPER_BOUND_STEP = ("Measurement was in the middle of an update the stop couldn't wait for, so "
                     "terminal.worker.unmeasured_at_stop_frames is an upper bound, the levels it had ready "
                     "weren't published, and a near-silence span that had started may have no ended or "
                     "interrupted event")
_JOURNAL_STEP = ("The runtime journal didn't keep every line (terminal.journal: state {state}, {dropped} "
                 "lines dropped, {unwritten} unwritten at close; terminal.broker.publish_hook_errors: {hooks} "
                 "events never reached it); the sealed terminal doesn't depend on it")
_FAILED_WORKER_STEP = ("Level measurement failed during this runtime (terminal.worker.error says why): "
                       "terminal.worker.unmeasured_after_failure_frames received frames have no levels, and "
                       "each route's totals cover only its frames_measured")
_CLOSE_STEP = ("The input adapter didn't close cleanly (terminal.stop.adapter_close is {close}); "
               "terminal.stop.adapter_exit gives what Listen learned of how it exited, such as its exit code "
               "or the error closing raised, and it may still be reading the input")
_OVERRUN_STEP = ("Measurement fell behind during the runtime: {skipped} received frames it skipped "
                 "(terminal.worker.unmeasured_frames beyond those after a failure or at stop) have no "
                 "levels, and each route's totals cover only its frames_measured")
_UNACCOUNTED_STEP = ("Measurement lost track of {unaccounted} frames after its fact queue was full "
                     "(terminal.worker.unaccounted_frames): it can't say whether they arrived. A "
                     "frames_missing event covers those that didn't; any that did have no levels, and each "
                     "route's totals cover only its frames_measured")
_UNDELIVERED_STEP = ("The macOS helper ended with {blocks} blocks ({frames} frames) of audio it had captured "
                     "but not sent, such as when a device change ended capture; no event covers them, so "
                     "capture's frames end before them (terminal.stop.adapter_exit.counters)")
_TAIL_DROPS_STEP = ("The macOS helper counted {frames} frames dropped after the last block capture took; no "
                    "frames_missing event covers them, since capture placed no block after them "
                    "(terminal.stop.helper_drops_after_last_block_frames)")
_AFTER_END_STEP = ("Capture ended ({cause}) before the stop, and the input sent {blocks} more blocks "
                   "({frames} frames) after that, which capture didn't place: no level, health event or "
                   "frame total covers them (terminal.capture.after_end, which also counts input events "
                   "after the end)")
_FAILED_RUNTIME_STEP = "The runtime failed; terminal.error says why"
_CLEAN_CLOSE = (None, "clean", "not_started")


def _sealed_stop(request_id, source, terminal, lease, epoch, *, reply_reason=None):
    handle, record = terminal
    worker, stop = record.get("worker") or {}, record.get("stop") or {}
    caveats = _stop_caveats(record)
    mine = record.get("stop_request_id") == request_id
    other = [] if mine else [(f"Runtime {epoch} was already stopped ({record.get('cause')}) before this "
                              "request reached it; stopped_by says how")]
    return receipt(request_id, status="partial" if caveats else "ok",
                   artifacts={"terminal": handle}, stop_outcome="stopped" if mine else "already_stopped",
                   stopped_by={"cause": record.get("cause"),
                               "stop_request_id": record.get("stop_request_id")},
                   terminal_sealed=True, measurement_drain=worker.get("drain"),
                   measurement_state=worker.get("state"), adapter_close=stop.get("adapter_close"),
                   runtime_epoch=epoch, **lease, replayed=False,
                   **({"stop_reply_reason": reply_reason} if reply_reason else {}),
                   terminal=_terminal_summary(handle, record, include_handle=False),
                   coverage=coverage(_record_source(record, source),
                                     capture=(record.get("capture") or {}).get("final_state")),
                   next_actions=[*other, *caveats, _lease_step(lease)])


def _stop_caveats(record):
    worker, stop = record.get("worker") or {}, record.get("stop") or {}
    close = stop.get("adapter_close")
    steps = (_DRAIN_STEP if worker.get("drain") == "incomplete" else None,
             _UPPER_BOUND_STEP if worker.get("unmeasured_at_stop_is_upper_bound") else None,
             _FAILED_WORKER_STEP if worker.get("state") == "failed" else None,
             _overrun_step(worker),
             _unaccounted_step(worker),
             None if close in _CLEAN_CLOSE else _CLOSE_STEP.format(close=close),
             _undelivered_step(stop),
             _tail_drops_step(stop),
             _after_end_step(record.get("capture") or {}),
             _FAILED_RUNTIME_STEP if record.get("state") == "failed" else None,
             _journal_step(record))
    return [step for step in steps if step is not None]


def _overrun_step(worker):
    skipped_mid_run = max(0, (worker.get("unmeasured_frames") or 0)
                          - (worker.get("unmeasured_after_failure_frames") or 0)
                          - (worker.get("unmeasured_at_stop_frames") or 0))
    return _OVERRUN_STEP.format(skipped=f"{skipped_mid_run:,}") if skipped_mid_run else None


def _unaccounted_step(worker):
    unaccounted = worker.get("unaccounted_frames") or 0
    return _UNACCOUNTED_STEP.format(unaccounted=f"{unaccounted:,}") if unaccounted else None


def _undelivered_step(stop):
    counters = (stop.get("adapter_exit") or {}).get("counters") or {}
    blocks, frames = counters.get("undelivered_blocks"), counters.get("undelivered_frames")
    if type(blocks) is not int or not blocks:
        return None
    return _UNDELIVERED_STEP.format(blocks=f"{blocks:,}",
                                    frames=f"{frames:,}" if type(frames) is int else "an unknown number of")


def _tail_drops_step(stop):
    frames = stop.get("helper_drops_after_last_block_frames")
    if type(frames) is not int or frames <= 0:
        return None
    return _TAIL_DROPS_STEP.format(frames=f"{frames:,}")


def _after_end_step(capture):
    after = capture.get("after_end") or {}
    if not after.get("blocks"):
        return None
    return _AFTER_END_STEP.format(cause=capture.get("ended_cause"), blocks=f"{after['blocks']:,}",
                                  frames=f"{after['frames']:,}")


def _journal_step(record):
    journal, broker = record.get("journal"), record.get("broker") or {}
    if not journal or _journal_kept_every_line(journal, broker):
        return None
    return _JOURNAL_STEP.format(state=journal.get("state"), dropped=journal.get("dropped_lines") or 0,
                                unwritten=journal.get("lines_unwritten_at_close") or 0,
                                hooks=broker.get("publish_hook_errors") or 0)


def _stop(root, session, request_id, epoch):
    source = _audio_source(session)
    row = _runtime_row(root, epoch)
    if not listen_host.lease_held(root):
        return _stopped_or_not_running(root, source, request_id, epoch, row)
    current = listen_host.current_runtime(root)
    if row is None or current["runtime_epoch"] != epoch:
        raise PocketError(f"The running runtime is {current['runtime_epoch']}, not {epoch}",
                          code="stale_revision", hint=_STALE_EPOCH_HINT)
    try:
        reply = listen_host.call(row[2], "stop", {"expected_runtime_epoch": epoch, "request_id": request_id},
                                 timeout_s=8.0)
    except listen_host.RuntimeUnreachable:
        if _wait_released(root, 5.0, epoch):
            return _stopped_or_not_running(root, source, request_id, epoch, row, lost_reply=True)
        return receipt(request_id, status="outcome_unknown", stop_outcome="stop_unconfirmed",
                       lease_state="held", runtime_epoch=epoch, replayed=False,
                       coverage=coverage(_no_audio_read(source), capture="unknown"),
                       next_actions=[f"Call listen_observe to see whether runtime {epoch} is still running",
                                     ("listen_stop with a new request_id is safe; it stops only the named "
                                      "runtime epoch")])
    lease = _lease_after(root, epoch) if _wait_released(root, 5.0, epoch) else _HELD
    try:
        terminal = listen_host.read_terminal(root, row[2])
        reason = None if terminal is not None else "terminal_not_found"
    except PocketError:
        terminal, reason = None, "terminal_integrity_mismatch"
    common = {"stop_outcome": "stopped", "runtime_epoch": epoch, **lease, "replayed": False}
    if terminal is not None:
        return _sealed_stop(request_id, source, terminal, lease, epoch, reply_reason=reply.get("reason"))
    return receipt(request_id, status="partial", terminal_sealed=False,
                   reason=reply.get("reason") or reason or "terminal_not_sealed",
                   coverage=coverage(_no_audio_read(source), capture="stopped"), next_actions=[_NO_TERMINAL_STEP],
                   **common)


def _stopped_or_not_running(root, source, request_id, epoch, row, *, lost_reply=False):
    reason = "runtime_exited_without_terminal"
    try:
        terminal = None if row is None else listen_host.read_terminal(root, row[2])
    except PocketError:
        terminal, reason = None, "terminal_integrity_mismatch"
    if terminal is not None and terminal[1].get("state") in ("stopped", "failed"):
        return _sealed_stop(request_id, source, terminal, _lease_after(root, epoch), epoch)
    previous = _runtime_record(root, row) if row is not None else _newest_record(root)
    common = {"stop_outcome": "not_running", "runtime_epoch": epoch, "runtime_known": row is not None,
              "previous_runtime": previous, "replayed": False, "notices": [notice("runtime_not_running")],
              "coverage": coverage(_no_audio_read(source), capture="not_running")}
    if lost_reply and terminal is None:
        lease = _lease_after(root, epoch)
        return receipt(request_id, status="needs_input", reason=reason, **lease, **common,
                       next_actions=[(f"The stop got no reply, and runtime {epoch} then exited without a "
                                      "terminal that verifies, so Listen can't say whether this request reached "
                                      "it; previous_runtime says what it left"), _lease_step(lease)])
    return receipt(request_id, status="needs_input", **common,
                   next_actions=[("Nothing is running for this session; listen_start with a new request_id "
                                  "starts a new runtime")])


def listen_request_status(session_dir: str, request_id: str) -> dict:
    """Inspect a retained listen_start or listen_stop request without repeating it.

    Adds what the lease and the runtime directories say: whether a runtime is running, which
    runtime this request launched, and the terminal it stopped, among the session's newest 64
    runtimes (runtimes_omitted counts older ones). It never contacts the runtime.
    """
    root, _, session = listen_host.load_session(session_dir)
    base = request_status(root, request_id)
    current = _current(root)
    every = listen_host.runtime_dirs(root)
    rows = every[-SCAN_RUNTIMES:]
    newest = rows[-1][1] if rows else None
    launched, stopped, unverified = None, None, 0
    for ordinal, epoch, path in reversed(rows):
        try:
            launch = listen_host.read_launch(path)
        except PocketError:
            launch, unverified = {}, unverified + 1
        try:
            terminal = listen_host.read_terminal(root, path)
            sealed = "sealed" if terminal is not None else "missing"
        except PocketError:
            terminal, sealed, unverified = None, "integrity_mismatch", unverified + 1
        if launched is None and launch.get("request_id") == request_id:
            launched = {"runtime_epoch": epoch, "ordinal": ordinal, "terminal": sealed,
                        "current": epoch == newest and current["state"] in ("running", "unreachable")}
        if stopped is None and terminal is not None and terminal[1].get("stop_request_id") == request_id:
            stopped = _terminal_summary(*terminal)
    omitted = len(every) - len(rows)
    result = {**base, "runtime": current, "request_runtime": launched, "stop_terminal": stopped,
              "runtimes_unverified": unverified, "runtimes_scanned": len(rows), "runtimes_omitted": omitted,
              "coverage": {**coverage(_audio_source(session, read=False)), **base["coverage"],
                           "runtime_contacted": False},
              "next_actions": _status_actions(base, launched, stopped, current, omitted, unverified)}
    canonical_bytes(result)
    return result


def _status_actions(base, launched, stopped, current, omitted=0, unverified=0):
    unfinished = (base.get("journal_state") in ("started", "lock_without_journal")
                  or base["status"] == "outcome_unknown")
    running = current["state"] in ("running", "unreachable")
    if stopped is not None:
        return [(f"Runtime {stopped['runtime_epoch']} stopped and sealed its terminal for this request; call "
                "listen_observe for the current state")]
    if launched is not None and unfinished:
        epoch = launched["runtime_epoch"]
        if launched["current"]:
            return [(f"The start launched runtime {epoch}, which is running; use listen_observe. This "
                    "request_id won't run again.")]
        return [(f"The start launched runtime {epoch}, which isn't running now; start again with a new "
                "request_id.")]
    if base.get("operation") == "listen_stop" and unfinished and running:
        return [(f"The stop may not have reached runtime {current['runtime_epoch']}; call listen_observe, "
                "then listen_stop with a new request_id if it's still running.")]
    if unfinished and omitted:
        older = "1 older runtime wasn't" if omitted == 1 else f"{omitted} older runtimes weren't"
        return [(f"None of the {SCAN_RUNTIMES} newest runtimes records this request, and {older} read; call "
                 "listen_observe, then use a new request_id.")]
    if unfinished and unverified:
        count = "1 runtime's records don't" if unverified == 1 else f"{unverified} runtimes' records don't"
        return [(f"No verified runtime records this request, but {count} verify, so the request may have "
                 "launched one of them; call listen_observe for what is running now.")]
    if unfinished:
        return ["No runtime records this request; call listen_observe, then use a new request_id."]
    return [*(base.get("next_actions") or []), "Call listen_observe for the current state"]
