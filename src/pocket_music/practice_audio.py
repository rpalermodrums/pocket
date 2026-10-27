# SPDX-License-Identifier: AGPL-3.0-only
"""A bounded standalone practice path built on existing capture and evidence.

No model, MIDI codec, instrument or DAW is needed. Explicit occurrences are
concatenated at their original rate. DSP, time stretch, mixing, fades and
playback are deliberately not part of this profile.
"""
from __future__ import annotations

import io
from fractions import Fraction
from itertools import pairwise
from typing import Literal

import numpy as np
import soundfile as sf

from .artifact_store import (
    ArtifactHandle,
    _verify_handles,
    call_memo,
    canonical_bytes,
    per_call_verification,
    put_bytes,
    put_record,
    read_bytes,
    read_record,
    run_request,
)
from .audio_evidence import measure_audio, validate_feedback_report
from .coordinates import bounded_list, fields, fraction, integer, text
from .errors import PocketError
from .musical_context import context_receipt, load_context
from .time_maps import musical_time

RENDER_SCHEMA = "pocket.practice-render/v1"
COMPARISON_SCHEMA = "pocket.practice-comparison/v1"
FEEDBACK_SCHEMA = "pocket.practice-feedback/v1"
FEEDBACK_V2_SCHEMA = "pocket.practice-feedback/v2"
PREVIEW_SCHEMA = "pocket.practice-preview/v1"
PROFILE = "exact-pcm-occurrences/v1"
PROCESSING = {"profile": PROFILE, "output_subtype": "DOUBLE", "fades": False, "normalization": False,
              "resampling": False, "channel_conversion": False, "time_stretch": False, "mixing": False}
MAX_OUTPUT_BYTES = 64 * 1024 * 1024


def _sequence(context, occurrence_ids, store_root):
    record, clocks = load_context(context, store_root)
    bounded_list(occurrence_ids, "occurrence_ids", 1, 32)
    for identifier in occurrence_ids:
        text(identifier, "occurrence_id", 120)
    if len(set(occurrence_ids)) != len(occurrence_ids):
        raise PocketError("Repeated audio needs separate occurrence identities")
    available = {o["occurrence_id"]: o for o in record["definition"]["occurrences"]}
    if any(identifier not in available for identifier in occurrence_ids):
        raise PocketError("Unknown occurrence in this context revision")
    selected = [available[identifier] for identifier in occurrence_ids]
    timeline_id = selected[0]["timeline_clock_id"]
    if any(o["timeline_clock_id"] != timeline_id for o in selected):
        raise PocketError("A practice render requires one explicit timeline clock")
    for previous, following in pairwise(selected):
        if previous["timeline_span_qn"][1] != following["timeline_span_qn"][0]:
            raise PocketError("Practice occurrences must be contiguous and ordered; gaps and overlaps need another profile")
    time_map, _ = clocks.timelines[timeline_id]
    tempo, offset = [], 0
    while True:
        page = musical_time("query", store_root, time_map=time_map, section="tempo", offset=offset, limit=256)
        tempo.extend(page["rows"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    rate = channels = None
    total = 0
    mappings = []
    for occurrence in selected:
        region = clocks.sources[occurrence["source_clock_id"]]
        original = region["original"]
        if rate is None:
            rate, channels = original["sample_rate"], original["channels"]
        if (rate, channels) != (original["sample_rate"], original["channels"]) or channels not in (1, 2):
            raise PocketError("Practice sources must share rate and mono/stereo layout; no implicit conversion")
        start, end = occurrence["source_span_frames"]
        begin_qn, end_qn = map(fraction, occurrence["timeline_span_qn"])
        frames_per_qn = Fraction(end - start) / (end_qn - begin_qn)
        # Check every intersecting step, not just the endpoints: opposite tempo
        # changes could have the right total duration but demand internal warping.
        for index, step in enumerate(tempo):
            step_start = fraction(step["at_qn"])
            step_end = fraction(tempo[index + 1]["at_qn"]) if index + 1 < len(tempo) else end_qn
            if (max(begin_qn, step_start) < min(end_qn, step_end)
                    and frames_per_qn != 60 * rate / fraction(step["bpm"])):
                raise PocketError("Authored occurrence requires time stretch; exact PCM profile refuses it", code="unsupported_profile",
                                  hint="Give each rendered occurrence a timeline_span_qn that lasts exactly as long "
                                       "as its source_span_frames at the tempo in effect over that span. This "
                                       "profile never time-stretches.")
        mappings.append({"occurrence_id": occurrence["occurrence_id"],
                         "source_clock_id": occurrence["source_clock_id"],
                         "source_sha256": original["sha256"], "source_span_frames": [start, end],
                         "output_span_frames": [total, total + end - start],
                         "timeline_span_qn": occurrence["timeline_span_qn"]})
        total += end - start
        if total > 120 * rate or total * channels * 8 + 128 > MAX_OUTPUT_BYTES:
            raise PocketError("Practice render exceeds 120 seconds or 64 MiB")
    return clocks, selected, mappings, rate, channels, total, timeline_id


def _samples(clocks, occurrence, store_root):
    region = clocks.sources[occurrence["source_clock_id"]]
    payload = read_bytes(region["crop"], store_root)
    first = occurrence["source_span_frames"][0] - region["interval"]["start_frame"]
    count = occurrence["source_span_frames"][1] - occurrence["source_span_frames"][0]
    with sf.SoundFile(io.BytesIO(payload)) as audio:
        audio.seek(first)
        samples = audio.read(count, dtype="float64", always_2d=True)
    if len(samples) != count or not np.isfinite(samples).all():
        raise PocketError("Retained source is incomplete or nonfinite")
    return samples


def _signal(payload, rate, channels, frames):
    return measure_audio(io.BytesIO(payload), {"settings": {"sample_rate": rate, "channels": channels},
                         "expected_frames": frames, "frame_tolerance": 0, "signal_expectation": "audible"})


@call_memo("practice_render")
def load_practice_render(handle, store_root):
    """Recheck mapping, signal and decoded source samples, including after relocation."""
    _verify_handles(handle, store_root)
    record = read_record(handle, store_root, RENDER_SCHEMA)
    fields(record, {"schema", "context", "occurrence_ids", "timeline_clock_id", "mappings", "audio",
                    "processing", "signal", "listening", "musical_verdict"})
    if (canonical_bytes(record["processing"]) != canonical_bytes(PROCESSING) or record["listening"] != "not_reviewed"
            or record["musical_verdict"] is not None):
        raise PocketError("Practice render evidence profile mismatch")
    clocks, selected, mappings, rate, channels, frames, timeline_id = _sequence(
        record["context"], record["occurrence_ids"], store_root)
    if canonical_bytes(record["mappings"]) != canonical_bytes(mappings) or record["timeline_clock_id"] != timeline_id:
        raise PocketError("Practice render mapping differs from its context")
    if not isinstance(record["audio"], dict) or record["audio"].get("artifact_schema") != "pocket.render-audio/v1":
        raise PocketError("Practice render requires an identified audio artifact")
    payload = read_bytes(record["audio"], store_root)
    if len(payload) > MAX_OUTPUT_BYTES:
        raise PocketError("Practice audio exceeds 64 MiB")
    if canonical_bytes(_signal(payload, rate, channels, frames)) != canonical_bytes(record["signal"]):
        raise PocketError("Practice signal evidence mismatch")
    with sf.SoundFile(io.BytesIO(payload)) as audio:
        if (audio.subtype != "DOUBLE" or audio.frames != frames or audio.samplerate != rate
                or audio.channels != channels):
            raise PocketError("Practice audio differs from declared exact PCM profile")
        for occurrence in selected:
            expected = _samples(clocks, occurrence, store_root)
            actual = audio.read(len(expected), dtype="float64", always_2d=True)
            if not np.array_equal(expected, actual):
                raise PocketError("Practice render samples differ from retained sources")
    return record


@per_call_verification
def practice_render(store_root: str, request_id: str, context: ArtifactHandle,
                    occurrence_ids: list[str]) -> dict:
    """Render exact original-rate passages/repetitions as a new immutable DOUBLE WAV."""
    def work():
        clocks, selected, mappings, rate, channels, frames, timeline_id = _sequence(
            context, occurrence_ids, store_root)
        output = io.BytesIO()
        with sf.SoundFile(output, mode="w", format="WAV", subtype="DOUBLE", samplerate=rate, channels=channels) as audio:
            for occurrence in selected:
                audio.write(_samples(clocks, occurrence, store_root))
        payload = output.getvalue()
        signal = _signal(payload, rate, channels, frames)
        audio = put_bytes(payload, store_root, "practice.wav", "pocket.render-audio/v1")
        record = {"schema": RENDER_SCHEMA, "context": context, "occurrence_ids": occurrence_ids,
                  "timeline_clock_id": timeline_id, "mappings": mappings, "audio": audio,
                  "processing": PROCESSING, "signal": signal, "listening": "not_reviewed", "musical_verdict": None}
        render = put_record(record, store_root)
        load_practice_render(render, store_root)
        return context_receipt(request_id=request_id, artifacts={"render": render, "audio": audio},
                               change_summary={"frames": frames, "occurrences": len(selected)},
                               coverage={"profile": PROFILE, "decoded_source_samples_exact": True,
                                         "native_execution": False, "human_listening": "not_performed"},
                               warnings=[] if signal["usable_for_expectation"] else [signal["disposition"]])
    return run_request(store_root, request_id, "practice_render",
                       {"context": context, "occurrence_ids": occurrence_ids}, work)


def _comparison(baseline, variants, question, allow_duration_mismatch, store_root):
    text(question, "question", 2000)
    bounded_list(variants, "variants", 1, 8)
    if not isinstance(allow_duration_mismatch, bool):
        raise PocketError("allow_duration_mismatch must be a boolean")
    handles = [baseline, *variants]
    if len({canonical_bytes(h) for h in handles}) != len(handles):
        raise PocketError("Comparison requires distinct render identities")
    renders = [load_practice_render(h, store_root) for h in handles]
    if any(r["context"] != renders[0]["context"] for r in renders):
        raise PocketError("This comparison profile requires one exact context revision")
    if len({(r["signal"]["sample_rate"], r["signal"]["channels"]) for r in renders}) != 1:
        raise PocketError("Comparison rate and channel layouts must match")
    if not allow_duration_mismatch and len({r["signal"]["frames"] for r in renders}) != 1:
        raise PocketError("Unequal durations require allow_duration_mismatch=True")
    return renders


@per_call_verification
def practice_compare(store_root: str, request_id: str, baseline: ArtifactHandle,
                     variants: list[ArtifactHandle], question: str,
                     allow_duration_mismatch: bool = False) -> dict:
    """Bind an explicit baseline and alternatives; signal checks never choose a winner."""
    inputs = {"baseline": baseline, "variants": variants, "question": question,
              "allow_duration_mismatch": allow_duration_mismatch}
    def work():
        renders = _comparison(**inputs, store_root=store_root)
        ready = all(r["signal"]["usable_for_expectation"] for r in renders)
        record = {"schema": COMPARISON_SCHEMA, **inputs, "context": renders[0]["context"],
                  "signal_ready": ready, "listening": "not_reviewed", "musical_verdict": None}
        handle = put_record(record, store_root)
        return context_receipt(request_id=request_id, artifacts={"comparison": handle},
                               coverage={"signal_ready": ready, "provider_playback": False,
                                         "human_listening": "not_performed"},
                               warnings=[] if ready else ["One or more renders need signal review before listening"])
    return run_request(store_root, request_id, "practice_compare", inputs, work)


def load_practice_audio(handle, store_root):
    """Dispatch declared audio profiles without broadening the exact PCM reader."""
    if isinstance(handle, dict) and handle.get("artifact_schema") == "pocket.practice-envelope/v1":
        from .practice_envelopes import load_practice_envelope
        return load_practice_envelope(handle, store_root)
    return load_practice_render(handle, store_root)


def load_comparison(handle, store_root):
    if isinstance(handle, dict) and handle.get("artifact_schema") == "pocket.practice-processed-comparison/v1":
        from .practice_envelopes import load_processed_comparison
        return load_processed_comparison(handle, store_root)
    if isinstance(handle, dict) and handle.get("artifact_schema") == "pocket.practice-revision-comparison/v1":
        from .practice_comparisons import load_revision_comparison
        return load_revision_comparison(handle, store_root)
    _verify_handles(handle, store_root)
    record = read_record(handle, store_root, COMPARISON_SCHEMA)
    fields(record, {"schema", "baseline", "variants", "question", "allow_duration_mismatch", "context",
                    "signal_ready", "listening", "musical_verdict"})
    renders = _comparison(record["baseline"], record["variants"], record["question"],
                          record["allow_duration_mismatch"], store_root)
    if (record["context"] != renders[0]["context"] or record["listening"] != "not_reviewed"
            or record["musical_verdict"] is not None
            or record["signal_ready"] is not all(r["signal"]["usable_for_expectation"] for r in renders)):
        raise PocketError("Comparison evidence mismatch")
    return record, renders


def _reviewed_preview(preview, render, store_root):
    """Fully revalidate a declared preview and require it to derive from the selected render."""
    if not isinstance(preview, dict) or preview.get("artifact_schema") != PREVIEW_SCHEMA:
        raise PocketError("Feedback preview must be an exact practice preview handle")
    from .practice_previews import load_practice_preview
    record, _ = load_practice_preview(preview, store_root)
    if canonical_bytes(record["parent"]) != canonical_bytes(render):
        raise PocketError("Feedback preview is not the selected render's preview", code="source_mismatch",
                          hint="Pass a preview that practice_preview made from this render, or omit preview to "
                               "report on the render itself.")
    return record


def _render_interval(reviewed_interval, preview):
    # The only preview profile maps frames one-to-one with a zero offset.
    offset = preview["frame_mapping"]["parent_offset_frames"]
    return [reviewed_interval[0] + offset, reviewed_interval[1] + offset]


@per_call_verification
def practice_feedback(store_root: str, request_id: str, comparison: ArtifactHandle, render: ArtifactHandle,
                      interval_frames: list[int], actor: str, actor_kind: Literal["human", "agent"], note: str,
                      decision: Literal["keep", "revise", "reject", "no_addition"] | None = None,
                      preview: ArtifactHandle | None = None) -> dict:
    """Attach an attributed listening report to exact compared audio and frame bounds.

    Without `preview` this is the original v1 report on the retained render. With
    a declared preview of that render, `interval_frames` address the preview that
    was reviewed; a v2 record keeps both it and the mapped render interval.
    """
    inputs = {"comparison": comparison, "render": render, "interval_frames": interval_frames,
              "actor": actor, "actor_kind": actor_kind, "note": note, "decision": decision}
    if preview is not None:
        inputs["preview"] = preview  # v1 request identities stay unchanged
    def work():
        comparison_record, renders = load_comparison(comparison, store_root)
        handles = [comparison_record["baseline"], *comparison_record["variants"]]
        if render not in handles:
            raise PocketError("Feedback render is not in this comparison")
        evidence = renders[handles.index(render)]
        kind = "attributed_human_listening" if actor_kind == "human" else "agent_report"
        if preview is None:
            validate_feedback_report(interval_frames, evidence["signal"]["frames"], actor, actor_kind, note, decision)
            handle = put_record({"schema": FEEDBACK_SCHEMA, **inputs, "evidence_kind": kind,
                                 "render_sha256": evidence["audio"]["sha256"]}, store_root)
            return context_receipt(request_id=request_id, artifacts={"feedback": handle},
                                   coverage={"listening": kind, "provider_playback": False})
        reviewed = _reviewed_preview(preview, render, store_root)
        validate_feedback_report(interval_frames, reviewed["format"]["frames"], actor, actor_kind, note, decision)
        handle = put_record({"schema": FEEDBACK_V2_SCHEMA, "comparison": comparison, "render": render,
                             "render_sha256": evidence["audio"]["sha256"],
                             "interval_frames": _render_interval(interval_frames, reviewed),
                             "reviewed_audio": {"kind": "declared_preview", "preview": preview,
                                                "preview_sha256": reviewed["audio"]["sha256"],
                                                "profile": reviewed["profile"], "interval_frames": interval_frames,
                                                "frame_mapping": reviewed["frame_mapping"]["kind"]},
                             "actor": actor, "actor_kind": actor_kind, "note": note, "decision": decision,
                             "evidence_kind": kind}, store_root)
        return context_receipt(request_id=request_id, artifacts={"feedback": handle},
                               coverage={"listening": kind, "provider_playback": False,
                                         "reviewed_audio": "declared_preview", "preview_profile": reviewed["profile"]})
    return run_request(store_root, request_id, "practice_feedback", inputs, work)


def _load_feedback_v2(handle, store_root, cache):
    _verify_handles(handle, store_root)
    record = read_record(handle, store_root, FEEDBACK_V2_SCHEMA)
    fields(record, {"schema", "comparison", "render", "render_sha256", "interval_frames", "reviewed_audio",
                    "actor", "actor_kind", "note", "decision", "evidence_kind"})
    reviewed = record["reviewed_audio"]
    fields(reviewed, {"kind", "preview", "preview_sha256", "profile", "interval_frames", "frame_mapping"})
    key = canonical_bytes(record["comparison"])
    if key not in cache:
        cache[key] = load_comparison(record["comparison"], store_root)
    comparison, renders = cache[key]
    handles = [comparison["baseline"], *comparison["variants"]]
    if record["render"] not in handles:
        raise PocketError("Feedback render is not in this comparison")
    render = renders[handles.index(record["render"])]
    preview = _reviewed_preview(reviewed["preview"], record["render"], store_root)
    validate_feedback_report(reviewed["interval_frames"], preview["format"]["frames"], record["actor"],
                             record["actor_kind"], record["note"], record["decision"])
    kind = "attributed_human_listening" if record["actor_kind"] == "human" else "agent_report"
    if (reviewed["kind"] != "declared_preview" or reviewed["profile"] != preview["profile"]
            or reviewed["preview_sha256"] != preview["audio"]["sha256"]
            or reviewed["frame_mapping"] != preview["frame_mapping"]["kind"]
            or record["interval_frames"] != _render_interval(reviewed["interval_frames"], preview)
            or record["evidence_kind"] != kind or record["render_sha256"] != render["audio"]["sha256"]):
        raise PocketError("Feedback preview, interval, attribution or render identity mismatch",
                          code="evidence_mismatch")
    return record, render


def load_practice_feedback(handle, store_root, cache=None):
    """Validate exact comparison membership, render identity and attributed interval."""
    if isinstance(handle, dict) and handle.get("artifact_schema") == FEEDBACK_V2_SCHEMA:
        return _load_feedback_v2(handle, store_root, {} if cache is None else cache)
    _verify_handles(handle, store_root)
    record = read_record(handle, store_root, FEEDBACK_SCHEMA)
    cache = {} if cache is None else cache
    fields(record, {"schema", "comparison", "render", "interval_frames", "actor", "actor_kind", "note",
                    "decision", "evidence_kind", "render_sha256"})
    key = canonical_bytes(record["comparison"])
    if key not in cache:
        cache[key] = load_comparison(record["comparison"], store_root)
    comparison, renders = cache[key]
    handles = [comparison["baseline"], *comparison["variants"]]
    if record["render"] not in handles:
        raise PocketError("Feedback render is not in this comparison")
    render = renders[handles.index(record["render"])]
    validate_feedback_report(record["interval_frames"], render["signal"]["frames"], record["actor"],
                             record["actor_kind"], record["note"], record["decision"])
    kind = "attributed_human_listening" if record["actor_kind"] == "human" else "agent_report"
    if record["evidence_kind"] != kind or record["render_sha256"] != render["audio"]["sha256"]:
        raise PocketError("Feedback attribution or render identity mismatch", code="evidence_mismatch")
    return record, render


@per_call_verification
def practice_query(store_root: str, artifact: ArtifactHandle,
                   section: Literal["summary", "mappings"] = "summary", offset: int = 0, limit: int = 32) -> dict:
    """Verify retained practice records; mappings use bounded pagination."""
    integer(offset, "offset", 0)
    integer(limit, "limit", 1, 64)
    if section not in ("summary", "mappings"):
        raise PocketError("Unknown practice query section")
    _verify_handles(artifact, store_root)
    record = read_record(artifact, store_root)
    schema = record["schema"]
    audio_schema = schema
    extra_coverage = {}
    mappings = None
    if schema in (RENDER_SCHEMA, "pocket.practice-envelope/v1"):
        record = load_practice_audio(artifact, store_root)
        mappings = record["mappings"]
    elif schema in (COMPARISON_SCHEMA, "pocket.practice-revision-comparison/v1", "pocket.practice-processed-comparison/v1"):
        record, _ = load_comparison(artifact, store_root)
    elif schema == FEEDBACK_SCHEMA:
        record, render = load_practice_feedback(artifact, store_root)
        audio_schema = render["schema"]
    elif schema == FEEDBACK_V2_SCHEMA:
        record, render = load_practice_feedback(artifact, store_root)
        audio_schema = PREVIEW_SCHEMA
        extra_coverage = {"parent_profile": render["processing"]["profile"], "reviewed_audio": "declared_preview"}
    elif schema == PREVIEW_SCHEMA:
        from .practice_previews import load_practice_preview
        record, parent = load_practice_preview(artifact, store_root)
        # One-to-one frames: parent occurrence mappings address preview frames unchanged.
        mappings = parent["mappings"]
        extra_coverage = {"parent_profile": parent["processing"]["profile"],
                          "frame_mapping": "identity", "device_output_verified": False}
    else:
        raise PocketError("Unknown practice artifact schema")
    profile = ("browser-pcm16-original-rate/v1" if audio_schema == PREVIEW_SCHEMA else
               "linear-loop-join-envelope/v1" if audio_schema in
               ("pocket.practice-envelope/v1", "pocket.practice-processed-comparison/v1") else PROFILE)
    common = {"artifacts": {"artifact": artifact},
              "coverage": {"profile": profile, "provider_playback": False, **extra_coverage}}
    if section == "summary":
        if offset:
            raise PocketError("Summary offset must be zero")
        return context_receipt(**common, summary={k: v for k, v in record.items() if k != "mappings"})
    if mappings is None:
        raise PocketError("Only a render contains source mappings")
    rows = mappings[offset:offset + limit]
    return context_receipt(**common, rows=rows, total=len(mappings), offset=offset,
                           next_offset=offset + len(rows) if offset + len(rows) < len(mappings) else None)
