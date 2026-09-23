"""Immutable host-independent workspaces with explicit clocks and occurrences.

This is separate from the selection browser's pocket.workspace/v1 and the
qualified native candidate's pocket.context/v1. Positions are scoped to an
exact context handle. Affine occurrence maps are authored intent, not a claim
that the source has been beat-tracked, stretched, rendered or listened to.
"""
from __future__ import annotations

import copy
from fractions import Fraction
from typing import Literal

from .artifact_store import (
    ArtifactHandle,
    _verify_handles,
    call_memo,
    canonical_bytes,
    put_record,
    read_record,
    receipt,
    run_request,
)
from .audio_regions import load_audio_region
from .context_types import ClockPosition, MusicalContextDefinition
from .coordinates import bounded_list, fields, fraction, integer, rational_json, text
from .errors import PocketError
from .time_maps import musical_time

SCHEMA = "pocket.musical-context/v1"
SCHEMA_V2 = "pocket.musical-context/v2"
PROFILE = "authored-occurrences-exact-step/v1"
COVERAGE = {"profile": PROFILE, "interpretation": "authored_not_inferred",
            "occurrence_mapping": "explicit_affine_source_frames_to_quarter_notes",
            "native_execution": False, "rendered": False, "human_listening": "not_performed"}

COVERAGE_V2 = {**COVERAGE, "interpretation": "explicit_evidence_bound_selections"}


def context_receipt(**kwargs):
    """Keep the existing receipt envelope; identify the new contract explicitly."""
    return receipt(provenance={"provider": "pocket", "contract": "musical-context-v1",
                               "canonicalizer": "json-v1"}, **kwargs)


def attribution(value):
    fields(value, {"actor", "actor_kind", "statement", "uncertainty"})
    text(value["actor"], "actor", 240)
    if value["actor_kind"] not in ("human", "agent"):
        raise PocketError("Attribution actor_kind must be human or agent")
    text(value["statement"], "statement", 2000)
    bounded_list(value["uncertainty"], "uncertainty", maximum=16)
    for item in value["uncertainty"]:
        text(item, "uncertainty item", 1000)


def _unique(rows, key):
    identifiers = []
    for row in rows:
        if not isinstance(row, dict) or key not in row:
            raise PocketError(f"Expected an explicit {key}")
        text(row[key], key, 120)
        identifiers.append(row[key])
    if len(set(identifiers)) != len(identifiers):
        raise PocketError(f"Duplicate {key}")


def _span(value, label, parse):
    bounded_list(value, label, 2, 2)
    start, end = (parse(v) for v in value)
    if end <= start:
        raise PocketError(f"{label} must be increasing and nonempty")
    return start, end


class ContextClocks:
    """Validated local lookup; time integration stays in the existing provider."""

    def __init__(self, definition, store_root):
        self.definition, self.store_root = definition, store_root
        self.sources, self.timelines = {}, {}
        for source in definition["sources"]:
            fields(source, {"clock_id", "region"})
            self.sources[source["clock_id"]] = load_audio_region(source["region"], store_root)
        for timeline in definition["timelines"]:
            fields(timeline, {"clock_id", "time_map"})
            result = musical_time("query", store_root, time_map=timeline["time_map"])
            self.timelines[timeline["clock_id"]] = (timeline["time_map"], result["summary"])

    def timeline_convert(self, clock_id, space, value, target):
        if clock_id not in self.timelines:
            raise PocketError("Unknown timeline clock identity")
        handle, _ = self.timelines[clock_id]
        result = musical_time("convert", self.store_root, time_map=handle,
                              positions=[{"space": space, "value": value}], target_space=target)
        return result["results"][0]["output"]

    def position(self, position):
        fields(position, {"clock_id", "space", "value"})
        text(position["clock_id"], "clock_id", 120)
        clock_id, space = position["clock_id"], position["space"]
        if clock_id in self.sources:
            if space != "source_frame":
                raise PocketError("Source clocks require original source_frame coordinates")
            region = self.sources[clock_id]
            return Fraction(integer(position["value"], "source frame", region["interval"]["start_frame"],
                                    region["interval"]["end_frame_exclusive"]))
        if clock_id in self.timelines:
            # The map validates spaces, bounds, rational reduction and render identity.
            return fraction(self.timeline_convert(clock_id, space, position["value"], "arrangement_qn")["value"])
        raise PocketError("Unknown clock identity in this context revision")


def _validate(definition, store_root):
    fields(definition, {"context_id", "title", "attribution", "sources", "timelines",
                        "occurrences", "anchors", "materials"})
    text(definition["context_id"], "context_id", 120)
    text(definition["title"], "title", 240)
    attribution(definition["attribution"])
    for key in ("sources", "timelines", "materials"):
        bounded_list(definition[key], key, maximum=32)
    bounded_list(definition["occurrences"], "occurrences", maximum=256)
    bounded_list(definition["anchors"], "anchors", maximum=256)
    _unique([*definition["sources"], *definition["timelines"]], "clock_id")
    _unique(definition["occurrences"], "occurrence_id")
    _unique(definition["anchors"], "anchor_id")
    clocks = ContextClocks(definition, store_root)
    if definition["materials"]:
        from .material import load_material
        for material in definition["materials"]:
            load_material(material, store_root)
    for occurrence in definition["occurrences"]:
        fields(occurrence, {"occurrence_id", "source_clock_id", "timeline_clock_id",
                            "source_span_frames", "timeline_span_qn"})
        source_id, timeline_id = occurrence["source_clock_id"], occurrence["timeline_clock_id"]
        text(source_id, "source_clock_id", 120)
        text(timeline_id, "timeline_clock_id", 120)
        if source_id not in clocks.sources or timeline_id not in clocks.timelines:
            raise PocketError("Occurrence refers to an unknown source or timeline clock")
        start, end = _span(occurrence["source_span_frames"], "source span",
                           lambda v: integer(v, "source frame", 0))
        for value in (start, end):
            clocks.position({"clock_id": source_id, "space": "source_frame", "value": value})
        _span(occurrence["timeline_span_qn"], "timeline span", fraction)
        for value in occurrence["timeline_span_qn"]:
            clocks.position({"clock_id": timeline_id, "space": "arrangement_qn", "value": value})
    for anchor in definition["anchors"]:
        fields(anchor, {"anchor_id", "kind", "label", "position", "attribution"})
        if anchor["kind"] not in ("pulse", "bar_one", "phrase_start", "onset", "other"):
            raise PocketError("Unknown authored anchor kind")
        text(anchor["label"], "anchor label", 240)
        attribution(anchor["attribution"])
        clocks.position(anchor["position"])
    return clocks


@call_memo("context")
def load_context(handle, store_root):
    """Validate bounded ancestry once, then evidence bindings in parent-first order."""
    _verify_handles(handle, store_root)
    chain, current_handle = [], handle
    while current_handle is not None:
        if len(chain) >= 33:
            raise PocketError("Context revision ancestry exceeds 32 parents")
        current = read_record(current_handle, store_root)
        schema = current.get("schema")
        if schema not in (SCHEMA, SCHEMA_V2):
            raise PocketError("Unknown musical context schema")
        fields(current, {"schema", "definition", "parent", "coverage"} | ({"bindings"} if schema == SCHEMA_V2 else set()))
        expected = COVERAGE if schema == SCHEMA else COVERAGE_V2
        if canonical_bytes(current["coverage"]) != canonical_bytes(expected):
            raise PocketError("Musical context profile mismatch")
        chain.append((current_handle, current))
        current_handle = current["parent"]
    ancestors, interpretations = {}, {}
    root_id = None
    for current_handle, current in reversed(chain):
        clocks = _validate(current["definition"], store_root)
        if root_id is None:
            root_id = current["definition"]["context_id"]
        if current["definition"]["context_id"] != root_id:
            raise PocketError("Parent belongs to a different musical context")
        if current["schema"] == SCHEMA_V2:
            from .interpretations import validate_context_bindings
            validate_context_bindings(current, store_root, ancestors, interpretations)
        elif current["parent"] is not None and ancestors[canonical_bytes(current["parent"])]["schema"] == SCHEMA_V2:
            raise PocketError("Cannot downgrade a bound context to v1 and discard interpretation bindings")
        ancestors[canonical_bytes(current_handle)] = current
    return chain[0][1], clocks


def context_create(store_root: str, request_id: str, definition: MusicalContextDefinition,
                   parent: ArtifactHandle | None = None) -> dict:
    """Create an immutable context revision; parent is lineage, never a mutable head."""
    def work():
        _validate(definition, store_root)
        if parent is not None:
            previous, _ = load_context(parent, store_root)
            if previous["schema"] != SCHEMA:
                raise PocketError("Use context_edit to revise a bound v2 context")
            if previous["definition"]["context_id"] != definition["context_id"]:
                raise PocketError("Parent belongs to a different musical context")
        record = {"schema": SCHEMA, "definition": copy.deepcopy(definition),
                  "parent": parent, "coverage": copy.deepcopy(COVERAGE)}
        handle = put_record(record, store_root)
        load_context(handle, store_root)
        return context_receipt(request_id=request_id, artifacts={"context": handle},
                               coverage=copy.deepcopy(COVERAGE),
                               change_summary={key: len(definition[key]) for key in
                                               ("sources", "timelines", "occurrences", "anchors", "materials")})
    return run_request(store_root, request_id, "context_create", {"definition": definition, "parent": parent}, work)


def context_query(store_root: str, context: ArtifactHandle,
                  section: Literal["summary", "sources", "timelines", "occurrences", "anchors", "materials", "bindings"] = "summary",
                  offset: int = 0, limit: int = 32) -> dict:
    """Inspect a bounded context page without starting a host or inferring music."""
    integer(offset, "offset", 0)
    integer(limit, "limit", 1, 64)
    record, _ = load_context(context, store_root)
    common = {"artifacts": {"context": context}, "coverage": copy.deepcopy(record["coverage"])}
    data = record["definition"]
    if section == "summary":
        if offset:
            raise PocketError("Summary offset must be zero")
        return context_receipt(**common, summary={"context_id": data["context_id"], "title": data["title"],
                               "attribution": data["attribution"], "parent": record["parent"],
                               "counts": {s: len(data[s]) for s in
                                          ("sources", "timelines", "occurrences", "anchors", "materials")}})
    if section == "bindings":
        rows = record.get("bindings", [])
        selected = rows[offset:offset + limit]
        return context_receipt(**common, section=section, rows=selected, total=len(rows), offset=offset,
                               next_offset=offset + len(selected) if offset + len(selected) < len(rows) else None)
    if section not in ("sources", "timelines", "occurrences", "anchors", "materials"):
        raise PocketError("Unknown context section")
    rows = data[section][offset:offset + limit]
    return context_receipt(**common, section=section, rows=rows, total=len(data[section]), offset=offset,
                           next_offset=offset + len(rows) if offset + len(rows) < len(data[section]) else None)


def context_resolve(store_root: str, context: ArtifactHandle, target_clock_id: str,
                    target_space: Literal["source_frame", "arrangement_qn", "host_seconds", "render_frame"],
                    position: ClockPosition | None = None, anchor_id: str | None = None,
                    occurrence_id: str | None = None) -> dict:
    """Resolve an explicit point. Repeated passages require an occurrence identity.

    This profile refuses fractional sample frames, extrapolation, cross-timeline
    inference and source-to-source shortcuts. An end boundary can be addressed
    as a point, although audio spans themselves remain end-exclusive.
    """
    if (position is None) == (anchor_id is None):
        raise PocketError("Supply exactly one position or anchor_id")
    text(target_clock_id, "target_clock_id", 120)
    if target_space not in ("source_frame", "arrangement_qn", "host_seconds", "render_frame"):
        raise PocketError("Unsupported target space")
    if occurrence_id is not None:
        text(occurrence_id, "occurrence_id", 120)
    record, clocks = load_context(context, store_root)
    if anchor_id is not None:
        text(anchor_id, "anchor_id", 120)
        anchors = [a for a in record["definition"]["anchors"] if a["anchor_id"] == anchor_id]
        if not anchors:
            raise PocketError("Unknown anchor in this context revision")
        position = anchors[0]["position"]
    value = clocks.position(position)
    source_id = position["clock_id"]
    selected = None
    if source_id != target_clock_id:
        forward = source_id in clocks.sources and target_clock_id in clocks.timelines
        reverse = source_id in clocks.timelines and target_clock_id in clocks.sources
        if not (forward or reverse):
            raise PocketError("No explicit source-to-timeline mapping for these clock identities")
        candidates = []
        for occurrence in record["definition"]["occurrences"]:
            if (occurrence["source_clock_id"] != (source_id if forward else target_clock_id)
                    or occurrence["timeline_clock_id"] != (target_clock_id if forward else source_id)):
                continue
            source_span = [Fraction(v) for v in occurrence["source_span_frames"]]
            timeline_span = [fraction(v) for v in occurrence["timeline_span_qn"]]
            start, end = source_span if forward else timeline_span
            if start <= value <= end and (occurrence_id is None or occurrence["occurrence_id"] == occurrence_id):
                candidates.append((occurrence, source_span, timeline_span))
        if not candidates:
            raise PocketError("No occurrence maps this position; extrapolation is unsupported")
        if len(candidates) != 1:
            raise PocketError("Ambiguous repeated passage; supply occurrence_id", code="ambiguous_mapping")
        selected, source_span, timeline_span = candidates[0]
        left, right = (source_span, timeline_span) if forward else (timeline_span, source_span)
        value = right[0] + (value - left[0]) * (right[1] - right[0]) / (left[1] - left[0])
    elif occurrence_id is not None:
        raise PocketError("Same-clock conversion does not consume an occurrence_id")
    if target_clock_id in clocks.sources:
        if target_space != "source_frame" or value.denominator != 1:
            raise PocketError("Source output requires an exact integer source_frame; no implicit rounding")
        output = {"space": target_space, "value": integer(value.numerator, "source frame", 0)}
    else:
        if target_space == "source_frame":
            raise PocketError("Timeline clocks do not have source_frame coordinates")
        output = clocks.timeline_convert(target_clock_id, "arrangement_qn", rational_json(value), target_space)
    return context_receipt(artifacts={"context": context}, input=position, anchor_id=anchor_id,
                           occurrence_id=selected["occurrence_id"] if selected else None,
                           output={"clock_id": target_clock_id, **output}, coverage=copy.deepcopy(record["coverage"]))
