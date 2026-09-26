# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical rational-time control curves, independent of instruments and DAWs."""
from __future__ import annotations

import copy
import re
import uuid
from fractions import Fraction
from typing import Literal

from .artifact_store import ArtifactHandle, digest, put_record, read_record, receipt, run_request
from .errors import PocketError
from .instrument_types import CurveOperation, CurveTarget
from .instruments._validation import boolean, choice, domain, fields, integer, number, text

SPACES = {"arrangement_qn", "clip_qn", "phrase_qn", "note_relative_qn"}
KINDS = {"cc", "pitch_bend", "channel_pressure", "per_note_pitch", "per_note_pressure", "per_note_slide",
         "host_parameter", "macro"}


def _fraction(value, label="time"):
    fields(value, ("n", "d"), label=label)
    integer(value["n"], label + " numerator", -(2**53), 2**53)
    integer(value["d"], label + " denominator", 1, 2**31)
    result = Fraction(value["n"], value["d"])
    if result.numerator != value["n"] or result.denominator != value["d"]:
        raise PocketError(f"{label} must be reduced rational quarter notes")
    return result


def _rational(value):
    return {"n": value.numerator, "d": value.denominator}


def _target(value):
    fields(value, ("kind", "target_id", "scope", "unit", "value_min", "value_max", "quantized", "values",
                   "ownership", "value_mode"), ("channel", "note_id", "resize_policy", "parameter_layout_sha256"),
           "curve target")
    choice(value["kind"], KINDS, "curve target kind")
    choice(value["scope"], {"channel", "note", "instance"}, "curve target scope")
    choice(value["ownership"], {"none", "host_automation", "macro", "remote", "unknown"}, "curve ownership")
    choice(value["value_mode"], {"absolute", "additive", "multiplicative"}, "value mode")
    for key in ("target_id", "unit"):
        text(value[key], key)
    number(value["value_min"], "minimum")
    number(value["value_max"], "maximum")
    # The minimum need not itself be a permitted enum; validate one declared value if present.
    sample = value["values"][0] if isinstance(value["values"], list) and value["values"] else value["value_min"]
    domain(sample, value["value_min"], value["value_max"], value["quantized"], value["values"], "target domain")
    expected_scope = "note" if value["kind"].startswith("per_note_") else (
        "instance" if value["kind"] in {"host_parameter", "macro"} else "channel")
    if value["scope"] != expected_scope:
        raise PocketError("Target kind and scope disagree")
    if value["scope"] == "channel":
        integer(value.get("channel"), "channel", 1, 16)
    if value["scope"] == "note":
        text(value.get("note_id"), "note identity")
        choice(value.get("resize_policy"), {"stretch_with_gate", "preserve_fraction", "crop", "preserve_ms"},
               "note expression resize policy")
    if value["scope"] == "instance":
        layout = value.get("parameter_layout_sha256")
        if not isinstance(layout, str) or not re.fullmatch(r"[0-9a-f]{64}", layout):
            raise PocketError("Instance curves require a parameter-layout fingerprint")


def _points(points, target, interpolation):
    if not isinstance(points, list) or not 1 <= len(points) <= 4096:
        raise PocketError("Curve requires 1–4096 ordered points")
    previous = None
    for point in points:
        fields(point, ("time", "value", "order"), label="curve point")
        time = _fraction(point["time"])
        integer(point["order"], "point order", 0, 1000000)
        key = (time, point["order"])
        if previous is not None and (key <= previous or (key[0] == previous[0] and interpolation != "step")):
            raise PocketError("Curve points must increase in time/order; equal times require explicit step semantics")
        previous = key
        domain(point["value"], target["value_min"], target["value_max"], target["quantized"], target["values"],
               "curve point value")


def _validate(record):
    fields(record, ("schema", "curve_id", "target", "space", "interpolation", "points", "parent", "context",
                    "executable", "provenance"), label="curve record")
    if record["schema"] != "pocket.curve/v1" or record["executable"] is not False:
        raise PocketError("Expected file-only pocket.curve/v1")
    text(record["curve_id"], "curve identity")
    _target(record["target"])
    choice(record["space"], SPACES, "time space")
    choice(record["interpolation"], {"linear", "step"}, "curve interpolation")
    if record["target"]["quantized"] and record["interpolation"] != "step":
        raise PocketError("Quantized destinations require step interpolation")
    if record["target"]["scope"] == "note" and record["space"] != "note_relative_qn":
        raise PocketError("Per-note expression requires an explicit note-relative time space")
    if record["space"] == "note_relative_qn" and record["target"]["scope"] != "note":
        raise PocketError("Note-relative time requires a note target")
    _points(record["points"], record["target"], record["interpolation"])


def _linear_value(first, last, time):
    begin, end = _fraction(first["time"]), _fraction(last["time"])
    if begin == end:
        return last["value"]
    return first["value"] + float((time - begin) / (end - begin)) * (last["value"] - first["value"])


def _simplify(points, tolerance):
    # Iterative Ramer-Douglas-Peucker with vertical error in the declared unit.
    keep, pending = {0, len(points) - 1}, [(0, len(points) - 1)]
    while pending:
        left, right = pending.pop()
        if right - left < 2:
            continue
        distance, split = max((abs(points[i]["value"] - _linear_value(points[left], points[right],
                                                                        _fraction(points[i]["time"]))), i)
                              for i in range(left + 1, right))
        if distance > tolerance:
            keep.add(split)
            pending.extend(((left, split), (split, right)))
    result = [points[i] for i in sorted(keep)]
    error = 0.0
    for left, right in zip(sorted(keep), sorted(keep)[1:]):
        error = max(error, max((abs(points[i]["value"] - _linear_value(points[left], points[right],
                                                                      _fraction(points[i]["time"])))
                                for i in range(left, right + 1)), default=0))
    return result, error


def curve_transform(*, store_root: str, request_id: str, operations: list[CurveOperation],
                    curve: ArtifactHandle | None = None, target: CurveTarget | None = None,
                    context: ArtifactHandle | None = None,
                    conflict_policy: Literal["reject_existing", "plan_override"] = "reject_existing",
                    allow_discontinuity_change: bool = False) -> dict:
    """Create/edit a file curve. Application stays unavailable until native qualification."""
    choice(conflict_policy, {"reject_existing", "plan_override"}, "conflict policy")
    boolean(allow_discontinuity_change, "allow_discontinuity_change")
    if (not isinstance(operations, list) or not 1 <= len(operations) <= 64
            or any(not isinstance(operation, dict) for operation in operations)):
        raise PocketError("Provide 1–64 explicit curve operations")
    inputs = {"curve": curve, "target": target, "context": context, "operations": operations,
              "conflict_policy": conflict_policy, "allow_discontinuity_change": allow_discontinuity_change}

    def work():
        if context is not None:
            read_record(context, store_root, "pocket.context/v1")
        if curve is not None:
            if target is not None:
                raise PocketError("A curve edit cannot silently replace its target")
            record = copy.deepcopy(read_record(curve, store_root, "pocket.curve/v1"))
            _validate(record)
            if record["context"] is not None:
                read_record(record["context"], store_root, "pocket.context/v1")
            if context is not None and context != record["context"]:
                raise PocketError("A curve edit cannot silently replace its coordinate context")
            record["parent"] = curve
        else:
            _target(target)
            if operations[0].get("op") != "create":
                raise PocketError("A new curve starts with create")
            record = {"schema": "pocket.curve/v1", "curve_id": str(uuid.uuid5(uuid.NAMESPACE_URL, digest(inputs))),
                      "target": copy.deepcopy(target), "space": None, "interpolation": None, "points": [],
                      "parent": None, "context": context, "executable": False,
                      "provenance": {"provider": "pocket.curve_transform", "version": 1}}
        if record["target"]["ownership"] != "none" and conflict_policy == "reject_existing":
            raise PocketError("Existing or unknown automation ownership conflicts; use explicit plan_override to plan")
        original_count, approximation_error = len(record["points"]), 0.0
        approximation_steps = []
        for index, operation in enumerate(operations):
            if not isinstance(operation, dict):
                raise PocketError("Curve operation must be an object")
            op = operation.get("op")
            if op == "create":
                fields(operation, ("op", "space", "points", "interpolation"), label="create operation")
                if curve is not None or index != 0:
                    raise PocketError("create is allowed only at the start of a new curve")
                record.update({key: copy.deepcopy(operation[key]) for key in ("space", "points", "interpolation")})
            elif op == "shift":
                fields(operation, ("op", "amount"), label="shift operation")
                shift = _fraction(operation["amount"], "shift")
                record["points"] = [{**p, "time": _rational(_fraction(p["time"]) + shift)} for p in record["points"]]
            elif op == "scale_time":
                fields(operation, ("op", "factor"), ("origin",), "scale_time operation")
                factor = _fraction(operation["factor"], "time factor")
                if factor <= 0:
                    raise PocketError("Time scaling must be positive")
                origin = _fraction(operation.get("origin", {"n": 0, "d": 1}), "origin")
                record["points"] = [{**p, "time": _rational(origin + (_fraction(p["time"]) - origin) * factor)}
                                    for p in record["points"]]
            elif op in {"scale_value", "offset"}:
                field = "factor" if op == "scale_value" else "amount"
                fields(operation, ("op", field), label=op + " operation")
                value = number(operation[field], field)
                record["points"] = [{**p, "value": p["value"] * value if op == "scale_value" else p["value"] + value}
                                    for p in record["points"]]
                if op == "scale_value":
                    approximation_error *= abs(value)
            elif op == "smooth":
                fields(operation, ("op", "window"), label="smooth operation")
                window = integer(operation["window"], "smoothing window", 3, 101)
                if window % 2 == 0:
                    raise PocketError("Smoothing window must be odd")
                if record["target"]["quantized"]:
                    raise PocketError("Smoothing an enumerated destination is unsupported")
                if record["interpolation"] == "step" and not allow_discontinuity_change:
                    raise PocketError("Smoothing steps requires explicit discontinuity permission")
                points = record["points"]
                smoothed = []
                step_error = 0.0
                for i, point in enumerate(points):
                    nearby = points[max(0, i - window // 2):min(len(points), i + window // 2 + 1)]
                    value = sum(p["value"] for p in nearby) / len(nearby)
                    step_error = max(step_error, abs(value - point["value"]))
                    smoothed.append({**point, "value": value})
                record["points"] = smoothed
                approximation_error += step_error
                approximation_steps.append({"operation_index": index, "op": op, "max_value_error": step_error})
            elif op == "simplify":
                fields(operation, ("op", "tolerance"), label="simplify operation")
                tolerance = number(operation["tolerance"], "simplification tolerance")
                if tolerance < 0 or record["interpolation"] != "linear":
                    raise PocketError("Simplification requires linear interpolation and a nonnegative tolerance")
                record["points"], error = _simplify(record["points"], tolerance)
                approximation_error += error
                approximation_steps.append({"operation_index": index, "op": op, "max_value_error": error})
            elif op == "splice":
                fields(operation, ("op", "start", "end", "points"), label="splice operation")
                start, end = _fraction(operation["start"]), _fraction(operation["end"])
                if end <= start:
                    raise PocketError("Splice requires an increasing closed interval")
                _points(operation["points"], record["target"], record["interpolation"])
                if any(not start <= _fraction(p["time"]) <= end for p in operation["points"]):
                    raise PocketError("Splice points must lie inside the declared interval")
                kept = [p for p in record["points"] if not start <= _fraction(p["time"]) <= end]
                record["points"] = sorted(kept + copy.deepcopy(operation["points"]),
                                          key=lambda p: (_fraction(p["time"]), p["order"]))
            else:
                raise PocketError(f"Unsupported curve operation: {op!r}")
            _validate(record)
        handle = put_record(record, store_root)
        edit = {"schema": "pocket.curve-edit/v1", "parent": curve, "child": handle, "operations": operations,
                "inverse": {"operation": "restore_exact_artifact" if curve else "discard_new_derivative", "curve": curve},
                "target_preserved": True, "source_unchanged": True, "executable": False,
                "approximation_max_value_error": approximation_error,
                "approximation_error_kind": "accumulated_bound_scaled_by_explicit_value_transforms",
                "approximation_steps": approximation_steps,
                "error_unit": record["target"]["unit"], "conflict_policy": conflict_policy}
        return receipt(request_id, artifacts={"curve": handle, "edit": put_record(edit, store_root)},
                       executable=False, change_summary={"points_before": original_count, "points_after": len(record["points"])},
                       coverage={"rational_time": "exact", "native_application": "unavailable",
                                 "value_domain": record["target"]["unit"], "human_listening": "not_performed"},
                       approximation_max_value_error=approximation_error,
                       approximation_error_kind="accumulated_bound_scaled_by_explicit_value_transforms",
                       warnings=(["Native ownership override is planned only; no automation was changed"]
                                 if conflict_policy == "plan_override" else []),
                       next_actions=["Use a separately qualified native or MIDI export route to apply this curve"])

    return run_request(store_root, request_id, "curve_transform", inputs, work)
