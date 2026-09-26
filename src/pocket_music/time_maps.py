# SPDX-License-Identifier: AGPL-3.0-only
"""Exact step-tempo conversion with explicit clocks, partial bars and local cycles.

This file-only profile never infers a DAW clock, a source warp, a groove, or a
loop occurrence. Thread's legacy piecewise-linear BPM mapper remains unchanged.
"""
from __future__ import annotations

import copy
import io
from bisect import bisect_right
from fractions import Fraction
from itertools import pairwise
from typing import Literal

from .artifact_store import ArtifactHandle, digest, put_record, read_bytes, read_record, receipt, run_request
from .coordinates import bounded_list as _list
from .coordinates import fields as _fields
from .coordinates import fraction as _q
from .coordinates import integer as _int
from .coordinates import rational_json as _json
from .coordinates import text as _text
from .errors import PocketError
from .time_types import FrameQuantization, TimeMapDefinition, TimePosition

SCHEMA = "pocket.time-map/v1"
COVERAGE = {"tempo": "exact_rational_step", "clocks": "declared_not_native_verified",
            "meter": "explicit_bar_anchors_and_partial_boundaries", "cycles": "annotation_only",
            "ramps": "unsupported", "groove": "unsupported", "warp": "unsupported",
            "loop_occurrences": "unsupported", "native_execution": False}


def _handle(value):
    _fields(value, {"schema", "artifact_uri", "sha256", "artifact_schema"})
    if value["schema"] != "pocket.artifact-handle/v1":
        raise PocketError("Expected explicit artifact handle")


def _context(context, store_root):
    if not isinstance(context, dict):
        raise PocketError("Explicit source clock context required")
    if context.get("schema") == "pocket.artifact-handle/v1":
        _handle(context)
        record = read_record(context, store_root)
        if record["schema"] == "pocket.material/v1":
            from .material import load_material
            load_material(context, store_root)
        elif record["schema"] != "pocket.context/v1":
            raise PocketError("Time context reference must identify material or context")
    else:
        _fields(context, {"schema", "context_id", "attribution"}, {"source"})
        if context["schema"] != "pocket.time-context/v1":
            raise PocketError("Expected declared pocket.time-context/v1")
        _text(context["context_id"], "context_id", 240)
        _text(context["attribution"], "attribution")
        if "source" in context:
            _handle(context["source"])
            read_bytes(context["source"], store_root)


def _render(render, store_root):
    if render is None:
        return None
    _fields(render, {"audio", "host_seconds", "frame", "sample_rate"})
    _q(render["host_seconds"], "render origin seconds")
    _int(render["frame"], "render origin frame", 0)
    _int(render["sample_rate"], "sample rate", 1, 768000)
    if not isinstance(render["audio"], dict) or render["audio"].get("artifact_schema") != "pocket.render-audio/v1":
        raise PocketError("Render mapping requires an identified pocket.render-audio/v1 artifact")
    _handle(render["audio"])
    import soundfile as sf
    try:
        info = sf.info(io.BytesIO(read_bytes(render["audio"], store_root)))
    except (RuntimeError, ValueError) as error:
        raise PocketError("Render mapping requires readable audio") from error
    if info.samplerate != render["sample_rate"] or render["frame"] > info.frames:
        raise PocketError("Render clock sample rate or origin frame disagrees with the audio")
    return info.frames


def _validate(definition, store_root):
    _fields(definition, {"source_context", "domain_qn", "tempo", "host_origin"},
            {"render_origin", "meter", "bar_one_qn", "cycles"})
    result = copy.deepcopy(definition)
    for name, default in (("render_origin", None), ("meter", []), ("bar_one_qn", None), ("cycles", [])):
        result.setdefault(name, default)
    _context(result["source_context"], store_root)
    _fields(result["domain_qn"], {"start", "end"})
    start, end = (_q(result["domain_qn"][key], "domain " + key) for key in ("start", "end"))
    if start >= end:
        raise PocketError("Musical-time domain must be increasing and finite")
    _fields(result["host_origin"], {"arrangement_qn", "host_seconds"})
    if not start <= _q(result["host_origin"]["arrangement_qn"], "host origin") <= end:
        raise PocketError("Host origin lies outside declared domain")
    _q(result["host_origin"]["host_seconds"], "host seconds")
    _list(result["tempo"], "tempo", 1)
    times = []
    for step in result["tempo"]:
        _fields(step, {"at_qn", "bpm", "interpolation"})
        times.append(_q(step["at_qn"], "tempo position"))
        if _q(step["bpm"], "tempo BPM") <= 0:
            raise PocketError("Tempo BPM must be positive")
        if step["interpolation"] != "step":
            raise PocketError("Only exact step tempo is supported; ramps/curves require another profile")
    if times[0] != start or any(a >= b for a, b in pairwise(times)) or times[-1] >= end:
        raise PocketError("Tempo starts must increase from the domain start and precede its end")
    render_frames = _render(result["render_origin"], store_root)
    _list(result["meter"], "meter")
    previous = None
    for segment in result["meter"]:
        _fields(segment, {"at_qn", "numerator", "denominator", "bar_number", "partial_previous_bar"})
        at = _q(segment["at_qn"], "meter position")
        numerator = _int(segment["numerator"], "meter numerator", 1, 1024)
        denominator = _int(segment["denominator"], "meter denominator", 1, 1024)
        bar = _int(segment["bar_number"], "bar number")
        if denominator & (denominator - 1):
            raise PocketError("Meter denominator must be a power of two")
        if not isinstance(segment["partial_previous_bar"], bool):
            raise PocketError("Partial-bar declaration must be boolean")
        if not start <= at < end or previous is None and (at != start or segment["partial_previous_bar"]):
            raise PocketError("Meter must cover domain from an explicit first bar boundary")
        if previous is not None:
            old_at, old_length, old_bar = previous
            amount = (at - old_at) / old_length
            count = -(-amount.numerator // amount.denominator)
            if (at <= old_at or bar != old_bar + count or
                    segment["partial_previous_bar"] != (amount.denominator != 1)):
                raise PocketError("Meter transition requires continuous bar numbers and explicit partial-bar status")
        previous = at, Fraction(numerator * 4, denominator), bar
    anchor = result["bar_one_qn"]
    if result["meter"]:
        if anchor is None or not any(_q(s["at_qn"]) == _q(anchor, "bar one") and s["bar_number"] == 1
                                    for s in result["meter"]):
            raise PocketError("Bar one must be an explicit meter segment boundary labeled 1")
    elif anchor is not None:
        raise PocketError("Bar-one anchor needs an explicit meter map")
    _list(result["cycles"], "cycles", maximum=512)
    cycle_ids = set()
    for cycle in result["cycles"]:
        _fields(cycle, {"cycle_id", "origin_qn", "length_qn", "annotation"})
        _text(cycle["cycle_id"], "cycle id", 240)
        _text(cycle["annotation"], "cycle annotation")
        if cycle["cycle_id"] in cycle_ids:
            raise PocketError("Duplicate local cycle identity")
        cycle_ids.add(cycle["cycle_id"])
        _q(cycle["origin_qn"], "cycle origin")
        if _q(cycle["length_qn"], "cycle length") <= 0:
            raise PocketError("Local cycle length must be positive")
    return result, render_frames


class _Clock:
    def __init__(self, definition, render_frames):
        self.definition, self.render_frames = definition, render_frames
        self.starts = [_q(step["at_qn"]) for step in definition["tempo"]]
        self.rates = [60 / _q(step["bpm"]) for step in definition["tempo"]]
        self.end = _q(definition["domain_qn"]["end"])
        self.elapsed = [Fraction(0)]
        for i in range(len(self.starts) - 1):
            self.elapsed.append(self.elapsed[-1] + (self.starts[i + 1] - self.starts[i]) * self.rates[i])
            _json(self.elapsed[-1])
        origin = definition["host_origin"]
        self.shift = _q(origin["host_seconds"]) - self._elapsed_at(_q(origin["arrangement_qn"]))
        _json(self.shift)
        _json(self.seconds(self.end))

    def _elapsed_at(self, qn):
        if not self.starts[0] <= qn <= self.end:
            raise PocketError("Time lies outside the explicit map domain; extrapolation is unsupported")
        index = bisect_right(self.starts, qn) - 1
        return self.elapsed[index] + (qn - self.starts[index]) * self.rates[index]

    def seconds(self, qn):
        return self.shift + self._elapsed_at(qn)

    def arrangement(self, seconds):
        elapsed = seconds - self.shift
        if not 0 <= elapsed <= self._elapsed_at(self.end):
            raise PocketError("Host seconds lie outside the explicit map domain")
        index = bisect_right(self.elapsed, elapsed) - 1
        return self.starts[index] + (elapsed - self.elapsed[index]) / self.rates[index]

    def frame(self, seconds):
        render = self.definition["render_origin"]
        if render is None:
            raise PocketError("Render-frame conversion requires an explicit identified render clock")
        return render["frame"] + (seconds - _q(render["host_seconds"])) * render["sample_rate"]

    def position(self, position):
        _fields(position, {"space", "value"})
        if position["space"] == "arrangement_qn":
            qn = _q(position["value"], "arrangement position")
            return qn, self.seconds(qn)
        if position["space"] == "host_seconds":
            seconds = _q(position["value"], "host position")
            return self.arrangement(seconds), seconds
        if position["space"] == "render_frame":
            render = self.definition["render_origin"]
            if render is None:
                raise PocketError("Render-frame conversion requires an explicit identified render clock")
            frame = _int(position["value"], "render frame", 0, self.render_frames)
            seconds = _q(render["host_seconds"]) + Fraction(frame - render["frame"], render["sample_rate"])
            return self.arrangement(seconds), seconds
        raise PocketError("Unsupported time space; warp, loop, groove and source conversions are not inferred")

    def bar(self, qn):
        meter = self.definition["meter"]
        if not meter:
            raise PocketError("Bar display requires an explicit meter map and bar-one anchor")
        index = bisect_right([_q(s["at_qn"]) for s in meter], qn) - 1
        segment = meter[index]
        length = Fraction(4 * segment["numerator"], segment["denominator"])
        offset = qn - _q(segment["at_qn"])
        bars = offset // length
        within = offset - bars * length
        bar_start = _q(segment["at_qn"]) + bars * length
        segment_end = _q(meter[index + 1]["at_qn"]) if index + 1 < len(meter) else bar_start + length
        observed_end = min(bar_start + length, segment_end)
        return {"bar_number": segment["bar_number"] + bars, "offset_qn": _json(within),
                "meter": [segment["numerator"], segment["denominator"]],
                "denominator_beat": _json(1 + within * segment["denominator"] / 4),
                "bar_start_qn": _json(bar_start), "bar_end_qn": _json(observed_end),
                "partial_bar": observed_end < bar_start + length,
                "domain_clipped_end_qn": _json(min(observed_end, self.end)),
                "boundary_at_domain_end": qn == self.end}


def _quantize(frame, quantization):
    if quantization is None:
        if frame.denominator != 1:
            raise PocketError("Noninteger render frame requires explicit nearest quantization and tolerance")
        rounded = frame.numerator
        policy = "exact"
    else:
        _fields(quantization, {"policy", "max_error_frames"})
        if quantization["policy"] != "nearest_half_away_from_zero":
            raise PocketError("Unsupported frame quantization policy")
        tolerance = _q(quantization["max_error_frames"], "frame tolerance")
        if tolerance < 0 or tolerance > Fraction(1, 2):
            raise PocketError("Nearest frame tolerance must lie between zero and one half")
        magnitude = abs(frame)
        rounded = (magnitude + Fraction(1, 2)).numerator // (magnitude + Fraction(1, 2)).denominator
        rounded = -rounded if frame < 0 else rounded
        if abs(rounded - frame) > tolerance:
            raise PocketError("Frame rounding exceeds the declared maximum error")
        policy = quantization["policy"]
    return rounded, {"policy": policy, "exact_frame": _json(frame), "error_frames": _json(rounded - frame)}


def _load(handle, store_root):
    _handle(handle)
    record = read_record(handle, store_root, SCHEMA)
    _fields(record, {"schema", "definition", "source_context_sha256", "coverage"})
    definition, frames = _validate(record["definition"], store_root)
    if (definition != record["definition"] or record["coverage"] != COVERAGE or
            record["source_context_sha256"] != digest(definition["source_context"])):
        raise PocketError("Time-map identity, canonical definition or coverage mismatch")
    return record, _Clock(definition, frames)


def musical_time(operation: Literal["create", "convert", "query"], store_root: str,
                 request_id: str | None = None, definition: TimeMapDefinition | None = None,
                 time_map: ArtifactHandle | None = None, positions: list[TimePosition] | None = None,
                 target_space: Literal["arrangement_qn", "host_seconds", "render_frame", "bar_display"] | None = None,
                 quantization: FrameQuantization | None = None,
                 section: Literal["summary", "tempo", "meter", "cycles"] = "summary",
                 offset: int = 0, limit: int = 100) -> dict:
    """Create, inspect or use a bounded exact map with no DAW or instrument dependency."""
    _int(offset, "query offset", 0)
    _int(limit, "query limit", 1, 256)
    if not isinstance(section, str) or section not in {"summary", "tempo", "meter", "cycles"}:
        raise PocketError("Unknown time-map query section")
    if not isinstance(operation, str):
        raise PocketError("Musical-time operation must be create, convert or query")
    if operation == "create":
        if (definition is None or time_map is not None or positions is not None or target_space is not None or
                quantization is not None or section != "summary" or offset != 0 or limit != 100):
            raise PocketError("Create requires only definition, store_root and request_id")
        def work():
            checked, frames = _validate(definition, store_root)
            _Clock(checked, frames)
            record = {"schema": SCHEMA, "definition": checked,
                      "source_context_sha256": digest(checked["source_context"]), "coverage": copy.deepcopy(COVERAGE)}
            handle = put_record(record, store_root)
            return receipt(request_id, artifacts={"time_map": handle}, coverage=copy.deepcopy(COVERAGE),
                           change_summary={"tempo_steps": len(checked["tempo"]), "meter_segments": len(checked["meter"]),
                                           "local_cycles": len(checked["cycles"])})
        result = run_request(store_root, request_id, "musical_time.create", {"definition": definition}, work)
        _load(result["artifacts"]["time_map"], store_root)
        return result
    if operation not in {"convert", "query"} or definition is not None or time_map is None or request_id is not None:
        raise PocketError("Read operations require time_map and omit definition/request_id")
    record, clock = _load(time_map, store_root)
    common = {"artifacts": {"time_map": time_map}, "coverage": copy.deepcopy(COVERAGE),
              "source_context_sha256": record["source_context_sha256"]}
    if operation == "query":
        if positions is not None or target_space is not None or quantization is not None:
            raise PocketError("Query does not accept conversion arguments")
        if section == "summary":
            if offset != 0:
                raise PocketError("Summary offset must be zero")
            data = clock.definition
            return receipt(**common, summary={"domain_qn": data["domain_qn"], "host_origin": data["host_origin"],
                           "render_origin": data["render_origin"], "bar_one_qn": data["bar_one_qn"],
                           "source_context": data["source_context"],
                           "counts": {s: len(data[s]) for s in ("tempo", "meter", "cycles")}})
        rows = clock.definition[section]
        page = rows[offset:offset + limit]
        return receipt(**common, section=section, rows=page, total=len(rows), offset=offset,
                       next_offset=offset + len(page) if offset + len(page) < len(rows) else None,
                       omitted=len(rows) - len(page))
    if section != "summary" or offset != 0 or limit != 100:
        raise PocketError("Convert does not accept query pagination")
    if not isinstance(target_space, str) or target_space not in {
            "arrangement_qn", "host_seconds", "render_frame", "bar_display"}:
        raise PocketError("Unsupported target time space")
    if quantization is not None and target_space != "render_frame":
        raise PocketError("Frame quantization applies only to render-frame output")
    _list(positions, "positions", 1, 512)
    results = []
    for position in positions:
        qn, seconds = clock.position(position)
        item = {"input": copy.deepcopy(position)}
        if target_space == "arrangement_qn":
            item["output"] = {"space": target_space, "value": _json(qn)}
        elif target_space == "host_seconds":
            item["output"] = {"space": target_space, "value": _json(seconds)}
        elif target_space == "bar_display":
            item["output"] = {"space": target_space, **clock.bar(qn)}
        else:
            exact = clock.frame(seconds)
            if not 0 <= exact <= clock.render_frames:
                raise PocketError("Mapped time lies outside the identified render frame bounds")
            frame, rounding = _quantize(exact, quantization)
            item["output"] = {"space": target_space, "value": frame,
                              "audio_sha256": clock.definition["render_origin"]["audio"]["sha256"]}
            item["quantization"] = {**rounding, "error_seconds": _json(
                Fraction(frame - exact, clock.definition["render_origin"]["sample_rate"]))}
        results.append(item)
    return receipt(**common, results=results)
