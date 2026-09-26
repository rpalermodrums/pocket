# SPDX-License-Identifier: AGPL-3.0-only
"""Evidence-bound choices, independent of detector confidence and declared clocks."""
from __future__ import annotations

import copy
import math
from fractions import Fraction

from .artifact_store import (
    ArtifactHandle,
    _verify_handles,
    canonical_bytes,
    put_record,
    read_record,
    run_request,
)
from .audio_region_analysis import audio_region_query, load_audio_region_hypotheses
from .audio_regions import load_audio_region
from .context_types import Attribution
from .coordinates import bounded_list, fields, fraction, integer, text
from .errors import PocketError
from .interpretation_types import InterpretationBinding, InterpretationClaim, InterpretationEvidence
from .musical_context import attribution, context_receipt, load_context

SCHEMA = "pocket.interpretation/v1"
COVERAGE = {"profile": "source-bound-interpretations/v1", "changes_analysis": False,
            "changes_tempo_map": False, "human_listening": "not_performed", "native_execution": False}


def _source(context_record, clock_id, store_root):
    text(clock_id, "source_clock_id", 120)
    sources = [s for s in context_record["definition"]["sources"] if s["clock_id"] == clock_id]
    if not sources:
        raise PocketError("Interpretation requires an exact source clock in its context")
    return sources[0]["region"], load_audio_region(sources[0]["region"], store_root)


def _evidence(evidence, source_handle, store_root):
    if evidence is None:
        return None
    fields(evidence, {"hypotheses", "expected_revision", "annotation_id"})
    text(evidence["annotation_id"], "annotation_id", 240)
    hypotheses = evidence["hypotheses"]
    if not isinstance(hypotheses, dict) or evidence["expected_revision"] != hypotheses.get("sha256"):
        raise PocketError("Stale interpretation evidence revision", code="stale_revision")
    wrapper = load_audio_region_hypotheses(hypotheses, store_root)
    if wrapper["region"] != source_handle:
        raise PocketError("Interpretation evidence belongs to another source or crop", code="source_mismatch")
    selected, superseded, cursor = None, set(), None
    for _ in range(4097):  # Provider enforces 4096 retained annotations; each page must advance.
        result = audio_region_query(store_root=store_root, hypotheses=hypotheses, view="annotations",
                                    cursor=cursor, limit=128, max_bytes=65536)
        if not result["items"] and not result["complete"]:
            raise PocketError("Interpretation evidence exceeds the bounded query profile")
        for row in result["items"]:
            local = row["local"]
            superseded.update(local["supersedes"])
            if local["annotation_id"] == evidence["annotation_id"]:
                selected = row
        if result["complete"]:
            break
        cursor = result["next_cursor"]
    else:
        raise PocketError("Interpretation evidence pagination exceeds bounds")
    if selected is None:
        raise PocketError("Annotation does not belong to the specified evidence revision")
    if evidence["annotation_id"] in superseded:
        raise PocketError("Selected annotation is superseded in this evidence revision", code="stale_revision")
    return selected


def _interval(value, region):
    bounded_list(value, "claim interval", 2, 2)
    start = integer(value[0], "claim start", region["interval"]["start_frame"],
                    region["interval"]["end_frame_exclusive"] - 1)
    integer(value[1], "claim end", start + 1, region["interval"]["end_frame_exclusive"])


def _claim(claim, region, row):
    if not isinstance(claim, dict) or claim.get("kind") not in ("onset", "pulse", "bar_one", "phrase_start"):
        raise PocketError("Unknown musical interpretation kind")
    kind, status = claim["kind"], claim.get("status")
    if status not in ("selected", "authored", "unresolved"):
        raise PocketError("Unknown musical interpretation status")
    if status == "unresolved":
        fields(claim, {"kind", "status", "interval_frames"})
        _interval(claim["interval_frames"], region)
    elif kind == "pulse":
        fields(claim, {"kind", "status", "bpm", "interval_frames"})
        bpm = claim["bpm"]
        if isinstance(bpm, bool) or not isinstance(bpm, (int, float)) or not math.isfinite(bpm) or not 20 <= bpm <= 400:
            raise PocketError("Pulse claim requires a finite bounded BPM estimate")
        _interval(claim["interval_frames"], region)
    else:
        fields(claim, {"kind", "status", "source_frame_q"})
        frame = fraction(claim["source_frame_q"], "source frame")
        if not region["interval"]["start_frame"] <= frame < region["interval"]["end_frame_exclusive"]:
            raise PocketError("Interpretation point exceeds retained source crop")
    if row is None:
        if status == "selected":
            raise PocketError("A selected interpretation requires an exact evidence candidate")
        return
    annotation, projection = row["local"]["annotation"], row["original_projection"]
    evidence_kind = annotation["kind"]
    if evidence_kind == "abstention":
        if status != "unresolved" or claim["interval_frames"] != [projection["start_frame"], projection["end_frame_exclusive"]]:
            raise PocketError("Abstention can support only an unresolved exact interval")
        return
    if status == "unresolved":
        raise PocketError("Unresolved evidence must identify an abstention, not a chosen candidate")
    if status == "authored":
        # Support remains contextual evidence. The caller, not the detector, owns the new claim.
        return
    if kind == "pulse":
        if (evidence_kind != "pulse_candidate" or claim["bpm"] != annotation["bpm"]
                or claim["interval_frames"] != [projection["start_frame"], projection["end_frame_exclusive"]]):
            raise PocketError("Selected pulse must preserve its exact candidate estimate and interval")
        return
    allowed = {"onset": ("attack",), "bar_one": ("learned_downbeat",), "phrase_start": ("phrase_anchor",)}
    if evidence_kind not in allowed[kind]:
        raise PocketError("Detector candidate does not assert this claim; use an explicit authored interpretation")
    expected = (Fraction(projection["source_frame"]) if evidence_kind == "attack"
                else fraction(projection["source_frame_q"]) if evidence_kind == "learned_downbeat"
                else Fraction(projection["start_frame"]))
    if fraction(claim["source_frame_q"]) != expected:
        raise PocketError("Selected interpretation changes the candidate source coordinate")


def _validate_record(record, context_record, store_root):
    fields(record, {"schema", "context", "source_clock_id", "claim", "attribution", "evidence", "coverage"})
    if record["schema"] != SCHEMA or canonical_bytes(record["coverage"]) != canonical_bytes(COVERAGE):
        raise PocketError("Interpretation profile mismatch")
    attribution(record["attribution"])
    source_handle, region = _source(context_record, record["source_clock_id"], store_root)
    row = _evidence(record["evidence"], source_handle, store_root)
    _claim(record["claim"], region, row)
    return row


def load_interpretation(handle, store_root):
    _verify_handles(handle, store_root)
    record = read_record(handle, store_root, SCHEMA)
    fields(record, {"schema", "context", "source_clock_id", "claim", "attribution", "evidence", "coverage"})
    context_record, _ = load_context(record["context"], store_root)
    row = _validate_record(record, context_record, store_root)
    return record, row


def interpretation_create(store_root: str, request_id: str, context: ArtifactHandle,
                          source_clock_id: str, claim: InterpretationClaim, attribution: Attribution,
                          evidence: InterpretationEvidence | None = None) -> dict:
    """Retain an explicit source-bound choice; no grid, edit, confidence promotion or playback."""
    inputs = {"context": context, "source_clock_id": source_clock_id, "claim": claim,
              "attribution": attribution, "evidence": evidence}
    def work():
        context_record, _ = load_context(context, store_root)
        record = {"schema": SCHEMA, **copy.deepcopy(inputs), "coverage": copy.deepcopy(COVERAGE)}
        _validate_record(record, context_record, store_root)
        handle = put_record(record, store_root)
        return context_receipt(request_id=request_id, artifacts={"interpretation": handle}, coverage=COVERAGE)
    result = run_request(store_root, request_id, "interpretation_create", inputs, work)
    load_interpretation(result["artifacts"]["interpretation"], store_root)
    return result


def interpretation_query(store_root: str, interpretation: ArtifactHandle) -> dict:
    """Read the claim and its exact retained evidence, including original rational positions."""
    record, row = load_interpretation(interpretation, store_root)
    return context_receipt(artifacts={"interpretation": interpretation}, summary=record,
                           selected_evidence=row, coverage=COVERAGE)


def _anchor(binding, interpretation, author):
    if not isinstance(binding, dict) or binding.get("kind") not in ("anchor", "selection"):
        raise PocketError("Unknown interpretation binding kind")
    fields(binding, {"kind", "binding_id"} | ({"anchor_id", "label"} if binding["kind"] == "anchor" else set()))
    text(binding["binding_id"], "binding_id", 120)
    if binding["kind"] == "selection":
        return None
    text(binding["anchor_id"], "anchor_id", 120)
    text(binding["label"], "anchor label", 240)
    claim = interpretation["claim"]
    if "source_frame_q" not in claim:
        raise PocketError("Only a resolved point interpretation can bind an anchor")
    frame = fraction(claim["source_frame_q"])
    if frame.denominator != 1:
        raise PocketError("Context anchor requires an exact integer frame; retain fractional evidence as a selection", code="unsupported_profile")
    return {"anchor_id": binding["anchor_id"], "label": binding["label"], "kind": claim["kind"],
            "position": {"clock_id": interpretation["source_clock_id"], "space": "source_frame", "value": frame.numerator},
            "attribution": copy.deepcopy(author)}


def validate_context_bindings(record, store_root, ancestors, cache):
    """Context reader hook: bounded ancestry lookup prevents recursive context graph expansion."""
    bindings = record["bindings"]
    bounded_list(bindings, "interpretation bindings", maximum=64)
    ids, anchor_ids = set(), set()
    for item in bindings:
        fields(item, {"interpretation", "binding", "attribution"})
        attribution(item["attribution"])
        key = canonical_bytes(item["interpretation"])
        if key not in cache:
            interpretation = read_record(item["interpretation"], store_root, SCHEMA)
            fields(interpretation, {"schema", "context", "source_clock_id", "claim", "attribution", "evidence", "coverage"})
            origin = ancestors.get(canonical_bytes(interpretation["context"]))
            if origin is None:
                raise PocketError("Bound interpretation origin is not an exact context ancestor")
            _validate_record(interpretation, origin, store_root)
            cache[key] = interpretation
        interpretation = cache[key]
        origin = ancestors.get(canonical_bytes(interpretation["context"]))
        if origin is None:
            raise PocketError("Bound interpretation origin is not an exact context ancestor")
        if _source(origin, interpretation["source_clock_id"], store_root)[0] != _source(record, interpretation["source_clock_id"], store_root)[0]:
            raise PocketError("Bound interpretation source clock changed")
        anchor = _anchor(item["binding"], interpretation, item["attribution"])
        identifier = item["binding"]["binding_id"]
        if identifier in ids:
            raise PocketError("Duplicate interpretation binding identity")
        ids.add(identifier)
        if anchor is not None:
            if anchor["anchor_id"] in anchor_ids or anchor not in record["definition"]["anchors"]:
                raise PocketError("Bound interpretation anchor differs from the context")
            anchor_ids.add(anchor["anchor_id"])


def context_bind_interpretation(store_root: str, request_id: str, context: ArtifactHandle,
                                interpretation: ArtifactHandle, binding: InterpretationBinding,
                                attribution: Attribution) -> dict:
    """Publish a v2 child with an explicit selection or point anchor; v1 parents stay unchanged."""
    from .musical_context import COVERAGE_V2, SCHEMA_V2
    inputs = {"context": context, "interpretation": interpretation, "binding": binding, "attribution": attribution}
    def work():
        record, _ = load_context(context, store_root)
        chosen, _ = load_interpretation(interpretation, store_root)
        if chosen["context"] != context:
            raise PocketError("Interpretation binding requires its exact origin context revision")
        definition = copy.deepcopy(record["definition"])
        anchor = _anchor(binding, chosen, attribution)
        if anchor is not None:
            if any(a["anchor_id"] == anchor["anchor_id"] for a in definition["anchors"]):
                raise PocketError("Binding would overwrite an anchor; use an explicit context edit")
            definition["anchors"].append(anchor)
        bindings = copy.deepcopy(record.get("bindings", []))
        bindings.append({"interpretation": interpretation, "binding": copy.deepcopy(binding),
                         "attribution": copy.deepcopy(attribution)})
        child = put_record({"schema": SCHEMA_V2, "definition": definition, "parent": context,
                            "bindings": bindings, "coverage": copy.deepcopy(COVERAGE_V2)}, store_root)
        load_context(child, store_root)
        return context_receipt(request_id=request_id, artifacts={"context": child}, coverage=COVERAGE_V2)
    result = run_request(store_root, request_id, "context_bind_interpretation", inputs, work)
    load_context(result["artifacts"]["context"], store_root)
    return result
