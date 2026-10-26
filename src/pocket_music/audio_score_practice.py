# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded original-rate score overlays and source-onset correction auditions.

This is a separate profile from exact PCM concatenation. It mixes only declared
occurrences and amplitude controls; it never stretches, resamples, normalizes,
clamps, infers a downbeat, or executes a native host.
"""
from __future__ import annotations

import copy
import io

import numpy as np
import soundfile as sf

from .artifact_store import (
    ArtifactHandle,
    _verify_handles,
    call_memo,
    canonical_bytes,
    digest,
    per_call_verification,
    put_bytes,
    put_record,
    read_bytes,
    read_record,
    run_request,
)
from .audio_score_practice_types import AudioScoreRecipe
from .context_edit_types import ContextLock
from .context_edits import _apply, context_edit, load_context_edit
from .context_types import Attribution
from .coordinates import bounded_list, fields, fraction, integer, rational_json, text
from .errors import PocketError
from .music_types import Rational
from .musical_context import attribution as validate_attribution
from .musical_context import context_receipt, context_resolve, load_context
from .practice_audio import MAX_OUTPUT_BYTES, _signal
from .time_maps import musical_time

RENDER_SCHEMA = "pocket.audio-score-render/v1"
COMPARISON_SCHEMA = "pocket.audio-score-comparison/v1"
PROFILE = "same-rate-overlay-linear/v1"
MAX_OCCURRENCES = 32
MAX_CONTROLS = 256
MAX_CONCURRENT = 8
MAX_POINTS = 32
PROCESSING = {"profile": PROFILE, "output_subtype": "DOUBLE", "mixing": True,
              "amplitude_controls": "explicit_scalar_and_occurrence_local_linear_points",
              "summation_order": "context_occurrence_order", "normalization": False,
              "clamping": False, "resampling": False, "channel_conversion": False,
              "time_stretch": False, "implicit_fades": False, "automatic_alignment": False}
COVERAGE = {"profile": PROFILE, "decoded_source_samples_exact": False,
            "native_execution": False, "provider_playback": False,
            "human_listening": "not_performed", "musical_verdict": None}


def _gain(value):
    result = fraction(value, "amplitude gain")
    if not 0 <= result <= 4:
        raise PocketError("Amplitude gain must be between zero and four")
    return result


def _window(interval_qn):
    bounded_list(interval_qn, "audition interval_qn", 2, 2)
    start, end = map(fraction, interval_qn)
    if end <= start:
        raise PocketError("Audition window must have positive duration")
    return start, end


def _controls(recipe, occurrences):
    fields(recipe, {"profile", "controls"})
    if recipe["profile"] != PROFILE:
        raise PocketError("Unsupported audio score processing profile", code="unsupported_profile")
    bounded_list(recipe["controls"], "occurrence controls", 1, MAX_CONTROLS)
    available = {o["occurrence_id"]: o for o in occurrences}
    result = {}
    for control in recipe["controls"]:
        fields(control, {"occurrence_id", "gain", "points"})
        identifier = control["occurrence_id"]
        text(identifier, "controlled occurrence", 120)
        if identifier not in available or identifier in result:
            raise PocketError("Controls require unique exact score occurrence identities")
        _gain(control["gain"])
        bounded_list(control["points"], "amplitude points", maximum=MAX_POINTS)
        count = available[identifier]["source_span_frames"][1] - available[identifier]["source_span_frames"][0]
        previous = -1
        for point in control["points"]:
            fields(point, {"at_frame", "gain"})
            frame = integer(point["at_frame"], "occurrence-local amplitude frame", 0, count - 1)
            if frame <= previous:
                raise PocketError("Amplitude points must have strictly increasing local frames")
            _gain(point["gain"])
            previous = frame
        if control["points"] and (len(control["points"]) < 2 or control["points"][0]["at_frame"] != 0
                                   or control["points"][-1]["at_frame"] != count - 1):
            raise PocketError("Linear amplitude points must explicitly cover occurrence frames zero through N-1")
        result[identifier] = control
    if set(result) != set(available):
        raise PocketError("Declare one amplitude control for every score occurrence on this timeline")
    return result


def _tempo(clocks, timeline_id, spans, store_root):
    time_map, _ = clocks.timelines[timeline_id]
    rows, offset = [], 0
    while True:
        page = musical_time("query", store_root, time_map=time_map, section="tempo", offset=offset, limit=256)
        rows.extend(page["rows"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    effective = set()
    for index, row in enumerate(rows):
        left = fraction(row["at_qn"])
        right = fraction(rows[index + 1]["at_qn"]) if index + 1 < len(rows) else max(b for _, b in spans)
        if any(max(a, left) < min(b, right) for a, b in spans):
            effective.add(fraction(row["bpm"]))
    if len(effective) != 1:
        raise PocketError("Audio score overlay requires constant tempo across the window and active occurrences",
                          code="unsupported_profile")
    return next(iter(effective))


def _frame(value):
    if value.denominator != 1:
        raise PocketError("Audio score placement requires exact integer frames; no implicit rounding",
                          code="unsupported_profile")
    return integer(value.numerator, "overlay frame", 0)


def _plan(score, context, clocks, timeline_clock_id, interval_qn, recipe, store_root):
    text(timeline_clock_id, "timeline_clock_id", 120)
    if timeline_clock_id not in clocks.timelines:
        raise PocketError("Unknown exact score timeline clock")
    start, end = _window(interval_qn)
    for qn in interval_qn:
        clocks.position({"clock_id": timeline_clock_id, "space": "arrangement_qn", "value": qn})
    member_ids = {m["occurrence_id"] for p in score["definition"]["passages"] for m in p["members"]}
    occurrences = [o for o in context["definition"]["occurrences"]
                   if o["occurrence_id"] in member_ids and o["timeline_clock_id"] == timeline_clock_id]
    bounded_list(occurrences, "score timeline occurrences", 1, MAX_CONTROLS)
    controls = _controls(recipe, occurrences)
    active = [o for o in occurrences if max(start, fraction(o["timeline_span_qn"][0]))
              < min(end, fraction(o["timeline_span_qn"][1]))]
    if not active:
        raise PocketError("Audition window contains no score occurrence")
    bounded_list(active, "active audition occurrences", 1, MAX_OCCURRENCES)
    spans = [(start, end), *(tuple(map(fraction, o["timeline_span_qn"])) for o in active)]
    bpm = _tempo(clocks, timeline_clock_id, spans, store_root)
    originals = [clocks.sources[o["source_clock_id"]]["original"] for o in active]
    rate, channels = originals[0]["sample_rate"], originals[0]["channels"]
    if channels not in (1, 2) or any((r["sample_rate"], r["channels"]) != (rate, channels) for r in originals):
        raise PocketError("Overlay requires matching original rate and mono/stereo layout; no implicit conversion",
                          code="unsupported_profile")
    frames_per_qn = 60 * rate / bpm
    frame_count = _frame((end - start) * frames_per_qn)
    if frame_count > 120 * rate or frame_count * channels * 8 + 128 > MAX_OUTPUT_BYTES:
        raise PocketError("Audio score render exceeds 120 seconds or 64 MiB")
    mappings, boundaries = [], []
    for occurrence in active:
        left, right = map(fraction, occurrence["timeline_span_qn"])
        source_start, source_end = occurrence["source_span_frames"]
        if source_end - source_start != (right - left) * frames_per_qn:
            raise PocketError("Occurrence requires time stretch; same-rate overlay refuses it", code="unsupported_profile")
        clipped_start, clipped_end = max(start, left), min(end, right)
        source_offset = _frame((clipped_start - left) * frames_per_qn)
        count = _frame((clipped_end - clipped_start) * frames_per_qn)
        first = _frame((clipped_start - start) * frames_per_qn)
        boundaries.extend([(first, 1), (first + count, -1)])
        mappings.append({"occurrence_id": occurrence["occurrence_id"],
                         "source_clock_id": occurrence["source_clock_id"],
                         "source_sha256": clocks.sources[occurrence["source_clock_id"]]["original"]["sha256"],
                         "source_span_frames": [source_start + source_offset, source_start + source_offset + count],
                         "occurrence_source_span_frames": occurrence["source_span_frames"],
                         "output_span_frames": [first, first + count],
                         "timeline_span_qn": [rational_json(clipped_start), rational_json(clipped_end)],
                         "occurrence_timeline_span_qn": occurrence["timeline_span_qn"]})
    concurrent = 0
    for _, change in sorted(boundaries):
        concurrent += change  # End-exclusive: releases precede starts at the same frame.
        if concurrent > MAX_CONCURRENT:
            raise PocketError("Audio score overlay exceeds eight simultaneous occurrences")
    return {"mappings": mappings, "rate": rate, "channels": channels, "frames": frame_count,
            "controls": controls, "clocks": clocks}


def _inputs(score_handle, timeline_clock_id, interval_qn, recipe, store_root):
    from .audio_score import load_audio_score
    score = load_audio_score(score_handle, store_root)
    context, clocks = load_context(score["context"], store_root)
    return _plan(score, context, clocks, timeline_clock_id, interval_qn, recipe, store_root)


def _mix(plan, store_root):
    # All dimensions, concurrency, controls and exact frame conversions were checked before allocation.
    output = np.zeros((plan["frames"], plan["channels"]), dtype=np.float64)
    for mapping in plan["mappings"]:
        region = plan["clocks"].sources[mapping["source_clock_id"]]
        source_start, source_end = mapping["source_span_frames"]
        count = source_end - source_start
        with sf.SoundFile(io.BytesIO(read_bytes(region["crop"], store_root))) as audio:
            audio.seek(source_start - region["interval"]["start_frame"])
            samples = audio.read(count, dtype="float64", always_2d=True)
        if samples.shape != (count, plan["channels"]) or not np.isfinite(samples).all():
            raise PocketError("Retained source is incomplete or nonfinite")
        control = plan["controls"][mapping["occurrence_id"]]
        gain = float(_gain(control["gain"]))
        if control["points"]:
            offset = source_start - mapping["occurrence_source_span_frames"][0]
            gains = np.interp(np.arange(offset, offset + count),
                              [p["at_frame"] for p in control["points"]],
                              [float(_gain(p["gain"])) for p in control["points"]]) * gain
            samples = samples * gains[:, None]
        else:
            samples = samples * gain
        first, last = mapping["output_span_frames"]
        output[first:last] += samples
    if not np.isfinite(output).all():
        raise PocketError("Overlay sum is nonfinite")
    return output


@call_memo("audio_score_render")
def load_audio_score_render(handle, store_root):
    """Reconstruct the actual mix; a rehashed manifest is not signal evidence."""
    _verify_handles(handle, store_root)
    record = read_record(handle, store_root, RENDER_SCHEMA)
    fields(record, {"schema", "score", "context", "timeline_clock_id", "interval_qn", "recipe", "occurrence_ids",
                    "mappings", "processing", "audio", "signal", "listening", "musical_verdict"})
    from .audio_score import load_audio_score
    score = load_audio_score(record["score"], store_root)
    plan = _inputs(record["score"], record["timeline_clock_id"], record["interval_qn"], record["recipe"], store_root)
    if (record["context"] != score["context"] or record["mappings"] != plan["mappings"]
            or record["occurrence_ids"] != [m["occurrence_id"] for m in plan["mappings"]]
            or canonical_bytes(record["processing"]) != canonical_bytes(PROCESSING)
            or record["listening"] != "not_reviewed" or record["musical_verdict"] is not None):
        raise PocketError("Audio score render mapping or profile evidence mismatch", code="evidence_mismatch")
    if not isinstance(record["audio"], dict) or record["audio"].get("artifact_schema") != "pocket.render-audio/v1":
        raise PocketError("Audio score render requires identified audio")
    payload = read_bytes(record["audio"], store_root)
    if len(payload) > MAX_OUTPUT_BYTES:
        raise PocketError("Audio score render exceeds 64 MiB")
    try:
        with sf.SoundFile(io.BytesIO(payload)) as audio:
            if (audio.subtype != "DOUBLE" or audio.frames != plan["frames"] or audio.samplerate != plan["rate"]
                    or audio.channels != plan["channels"]
                    or not np.array_equal(audio.read(dtype="float64", always_2d=True), _mix(plan, store_root))):
                raise PocketError("Audio score samples differ from declared overlay recipe", code="evidence_mismatch")
    except RuntimeError as error:
        raise PocketError("Audio score render is not readable declared WAV audio", code="evidence_mismatch") from error
    if canonical_bytes(_signal(payload, plan["rate"], plan["channels"], plan["frames"])) != canonical_bytes(record["signal"]):
        raise PocketError("Audio score signal evidence mismatch", code="evidence_mismatch")
    return record


@per_call_verification
def audio_score_render(store_root: str, request_id: str, score: ArtifactHandle, timeline_clock_id: str,
                       interval_qn: list[Rational], recipe: AudioScoreRecipe) -> dict:
    """Print a declared bounded original-rate overlay; no native or listening verdict."""
    inputs = {"score": score, "timeline_clock_id": timeline_clock_id, "interval_qn": interval_qn, "recipe": recipe}
    def work():
        from .audio_score import load_audio_score
        score_record = load_audio_score(score, store_root)
        plan = _inputs(score, timeline_clock_id, interval_qn, recipe, store_root)
        output = io.BytesIO()
        sf.write(output, _mix(plan, store_root), plan["rate"], format="WAV", subtype="DOUBLE")
        payload = output.getvalue()
        audio = put_bytes(payload, store_root, "audio-score.wav", "pocket.render-audio/v1")
        signal = _signal(payload, plan["rate"], plan["channels"], plan["frames"])
        record = {"schema": RENDER_SCHEMA, **copy.deepcopy(inputs), "context": score_record["context"],
                  "occurrence_ids": [m["occurrence_id"] for m in plan["mappings"]], "mappings": plan["mappings"],
                  "processing": copy.deepcopy(PROCESSING), "audio": audio, "signal": signal,
                  "listening": "not_reviewed", "musical_verdict": None}
        render = put_record(record, store_root)
        load_audio_score_render(render, store_root)
        return context_receipt(request_id=request_id, artifacts={"render": render, "audio": audio}, coverage=copy.deepcopy(COVERAGE),
                               change_summary={"frames": plan["frames"], "occurrences": len(plan["mappings"])},
                               warnings=[] if signal["usable_for_expectation"] else [signal["disposition"]])
    result = run_request(store_root, request_id, "audio_score_render", inputs, work)
    load_audio_score_render(result["artifacts"]["render"], store_root)
    return result


def _correction(score, passage_id, anchor_id, occurrence_id, timeline_clock_id, delta_qn, interval_qn, store_root):
    for key, value in (("passage_id", passage_id), ("anchor_id", anchor_id), ("occurrence_id", occurrence_id)):
        text(value, key, 120)
    delta = fraction(delta_qn, "timeline correction delta_qn")
    if not delta:
        raise PocketError("Timeline correction must be nonzero")
    context, clocks = load_context(score["context"], store_root)
    passages = {p["passage_id"]: p for p in score["definition"]["passages"]}
    if passage_id not in passages:
        raise PocketError("Unknown exact score passage")
    linked = [m["occurrence_id"] for m in passages[passage_id]["members"]]
    occurrences = {o["occurrence_id"]: o for o in context["definition"]["occurrences"]}
    if occurrence_id not in linked:
        raise PocketError("Named audible occurrence must belong to the selected passage")
    if any(occurrences[i]["timeline_clock_id"] != timeline_clock_id for i in linked):
        raise PocketError("Correction requires one explicit timeline clock for every linked passage member")
    anchors = {a["anchor_id"]: a for a in context["definition"]["anchors"]}
    if anchor_id not in anchors or anchors[anchor_id]["kind"] != "onset":
        raise PocketError("This correction profile requires a named authored source onset")
    position = anchors[anchor_id]["position"]
    occurrence = occurrences[occurrence_id]
    if (position["space"] != "source_frame" or position["clock_id"] != occurrence["source_clock_id"]
            or not occurrence["source_span_frames"][0] <= position["value"] < occurrence["source_span_frames"][1]):
        raise PocketError("Named source onset must be inside the exact selected occurrence")
    before = context_resolve(store_root, score["context"], timeline_clock_id, "arrangement_qn",
                             anchor_id=anchor_id, occurrence_id=occurrence_id)["output"]
    after = {**before, "value": rational_json(fraction(before["value"]) + delta)}
    left, right = _window(interval_qn)
    if not all(left <= fraction(p["value"]) < right for p in (before, after)):
        raise PocketError("Fixed audition window must contain the named onset before and after correction")
    operation = {"kind": "occurrence_shift_timeline", "occurrence_ids": linked, "delta_qn": delta_qn}
    return context, clocks, operation, {"before": before, "after": after}


@call_memo("audio_score_comparison")
def load_audio_score_comparison(handle, store_root):
    _verify_handles(handle, store_root)
    record = read_record(handle, store_root, COMPARISON_SCHEMA)
    keys = {"score", "passage_id", "anchor_id", "occurrence_id", "timeline_clock_id", "delta_qn", "locks",
            "attribution", "interval_qn", "recipe", "question"}
    fields(record, keys | {"schema", "child_score", "context_edit", "anchor_positions", "baseline", "variants",
                           "signal_ready", "coverage", "listening", "musical_verdict"})
    from .audio_score import load_audio_score
    parent = load_audio_score(record["score"], store_root)
    child = load_audio_score(record["child_score"], store_root)
    text(record["question"], "comparison question", 2000)
    validate_attribution(record["attribution"])
    _, _, operation, anchors = _correction(parent, **{k: record[k] for k in
        ("passage_id", "anchor_id", "occurrence_id", "timeline_clock_id", "delta_qn", "interval_qn")}, store_root=store_root)
    edit = load_context_edit(record["context_edit"], store_root)
    if (edit["parent"] != parent["context"] or edit["child"] != child["context"]
            or edit["operations"] != [operation] or edit["locks"] != record["locks"]
            or edit["attribution"] != record["attribution"] or child["parent"] != record["score"]
            or child["context_edit"] != record["context_edit"] or record["anchor_positions"] != anchors):
        raise PocketError("Audio score correction ancestry or anchor evidence mismatch", code="evidence_mismatch")
    bounded_list(record["variants"], "audition variants", 1, 1)
    handles = [record["baseline"], *record["variants"]]
    if handles[0] == handles[1]:
        raise PocketError("Audition requires distinct before and after render identities")
    renders = [load_audio_score_render(h, store_root) for h in handles]
    if len({(r["signal"]["sample_rate"], r["signal"]["channels"], r["signal"]["frames"]) for r in renders}) != 1:
        raise PocketError("Audition requires one common rate, layout and frame count", code="evidence_mismatch")
    for render, score_handle in zip(renders, (record["score"], record["child_score"]), strict=True):
        if (render["score"] != score_handle or any(render[k] != record[k]
                for k in ("timeline_clock_id", "interval_qn", "recipe"))):
            raise PocketError("Audition renders differ from the exact score or common window/recipe", code="evidence_mismatch")
    ready = all(r["signal"]["usable_for_expectation"] for r in renders)
    if (record["signal_ready"] is not ready or canonical_bytes(record["coverage"]) != canonical_bytes(COVERAGE)
            or record["listening"] != "not_reviewed" or record["musical_verdict"] is not None):
        raise PocketError("Audio score comparison verdict or coverage mismatch", code="evidence_mismatch")
    return record, renders


@per_call_verification
def audio_score_audition(store_root: str, request_id: str, score: ArtifactHandle, passage_id: str, anchor_id: str,
                         occurrence_id: str, timeline_clock_id: str, delta_qn: Rational, locks: list[ContextLock],
                         attribution: Attribution, interval_qn: list[Rational], recipe: AudioScoreRecipe,
                         question: str) -> dict:
    """Freeze before/after of one linked source-onset shift; selection remains separate feedback."""
    inputs = {"score": score, "passage_id": passage_id, "anchor_id": anchor_id, "occurrence_id": occurrence_id,
              "timeline_clock_id": timeline_clock_id, "delta_qn": delta_qn, "locks": locks,
              "attribution": attribution, "interval_qn": interval_qn, "recipe": recipe, "question": question}
    def work():
        from .audio_score import audio_score_revise, load_audio_score
        parent = load_audio_score(score, store_root)
        text(question, "comparison question", 2000)
        validate_attribution(attribution)
        context, clocks, operation, anchors = _correction(parent, passage_id, anchor_id, occurrence_id,
                                                         timeline_clock_id, delta_qn, interval_qn, store_root)
        # Preflight both sides and locks before creating derivative artifacts or allocating sample arrays.
        predicted, _, _ = _apply(context, parent["context"], [operation], locks, attribution, store_root)
        _plan(parent, context, clocks, timeline_clock_id, interval_qn, recipe, store_root)
        _plan(parent, predicted, clocks, timeline_clock_id, interval_qn, recipe, store_root)
        prefix = "as-audition-" + digest({"request_id": request_id, "inputs": inputs})[:48]
        edit_result = context_edit(store_root, prefix + "-edit", parent["context"], [operation], locks, attribution)
        edit = edit_result["artifacts"]["edit"]
        revised = audio_score_revise(store_root, prefix + "-score", score, edit)
        child = revised["artifacts"]["score"]
        base = audio_score_render(store_root, prefix + "-before", score, timeline_clock_id, interval_qn, recipe)
        variant = audio_score_render(store_root, prefix + "-after", child, timeline_clock_id, interval_qn, recipe)
        before_handle, after_handle = base["artifacts"]["render"], variant["artifacts"]["render"]
        renders = [load_audio_score_render(h, store_root) for h in (before_handle, after_handle)]
        ready = all(r["signal"]["usable_for_expectation"] for r in renders)
        record = {"schema": COMPARISON_SCHEMA, **copy.deepcopy(inputs), "child_score": child, "context_edit": edit,
                  "anchor_positions": anchors, "baseline": before_handle, "variants": [after_handle],
                  "signal_ready": ready, "coverage": copy.deepcopy(COVERAGE), "listening": "not_reviewed", "musical_verdict": None}
        comparison = put_record(record, store_root)
        load_audio_score_comparison(comparison, store_root)
        return context_receipt(request_id=request_id, artifacts={"score": child, "context": edit_result["artifacts"]["context"],
                                "edit": edit, "baseline": before_handle, "variant": after_handle, "comparison": comparison},
                               coverage={**COVERAGE, "signal_ready": ready}, change_summary={"anchor_positions": anchors},
                               warnings=[] if ready else ["An audition render requires signal review"])
    result = run_request(store_root, request_id, "audio_score_audition", inputs, work)
    load_audio_score_comparison(result["artifacts"]["comparison"], store_root)
    return result
