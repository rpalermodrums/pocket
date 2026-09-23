"""Explicit, independently verifiable gain envelopes at retained occurrence joins."""
from __future__ import annotations

import io

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
from .context_types import Attribution
from .coordinates import bounded_list, fields, integer, text
from .errors import PocketError
from .musical_context import attribution as validate_attribution
from .musical_context import context_receipt
from .practice_audio import MAX_OUTPUT_BYTES, _signal, load_practice_render
from .practice_envelope_types import JoinEnvelope

SCHEMA = "pocket.practice-envelope/v1"
COMPARISON_SCHEMA = "pocket.practice-processed-comparison/v1"
PROFILE = "linear-loop-join-envelope/v1"
PROCESSING = {"profile": PROFILE, "output_subtype": "DOUBLE", "fades": True, "normalization": False,
              "resampling": False, "channel_conversion": False, "time_stretch": False, "mixing": False,
              "crossfade": False, "frame_count_changed": False}
COVERAGE = {"profile": PROFILE, "decoded_source_samples_exact": False, "native_execution": False,
            "provider_playback": False, "human_listening": "not_performed"}


def _gains(parent, joins):
    bounded_list(joins, "joins", 1, 32)
    frames, rate = parent["signal"]["frames"], parent["signal"]["sample_rate"]
    boundaries = {m["output_span_frames"][0] for m in parent["mappings"][1:]}
    previous_end = -1
    gain = np.ones(frames, dtype=np.float64)
    for join in joins:
        fields(join, {"boundary_frame", "fade_out_frames", "fade_in_frames", "curve"})
        boundary = integer(join["boundary_frame"], "boundary_frame", 1, frames - 1)
        out = integer(join["fade_out_frames"], "fade_out_frames", 1, rate // 4)
        into = integer(join["fade_in_frames"], "fade_in_frames", 1, rate // 4)
        if join["curve"] != "linear":
            raise PocketError("Only an explicit linear join envelope is supported", code="unsupported_profile")
        if boundary not in boundaries:
            raise PocketError("Envelope boundary must be an actual occurrence join")
        start, end = boundary - out, boundary + into
        if start < 0 or end > frames or start < previous_end:
            raise PocketError("Envelope windows must be ordered, in bounds and nonoverlapping")
        # Both adjacent seam samples reach zero. Single-frame sides are zero;
        # longer sides include unity at the outer edge. No samples move or mix.
        gain[start:boundary] = np.linspace(1.0, 0.0, out) if out > 1 else 0.0
        gain[boundary:end] = np.linspace(0.0, 1.0, into)
        previous_end = end
    return gain


def _processed(parent, joins, store_root):
    gain = _gains(parent, joins)
    samples, rate = sf.read(io.BytesIO(read_bytes(parent["audio"], store_root)), dtype="float64", always_2d=True)
    return samples * gain[:, None], rate


@call_memo("practice_envelope")
def load_practice_envelope(handle, store_root):
    _verify_handles(handle, store_root)
    record = read_record(handle, store_root, SCHEMA)
    fields(record, {"schema", "parent_render", "context", "occurrence_ids", "timeline_clock_id", "mappings",
                    "joins", "attribution", "audio", "processing", "input_signal", "signal", "listening", "musical_verdict"})
    validate_attribution(record["attribution"])
    parent = load_practice_render(record["parent_render"], store_root)
    expected, rate = _processed(parent, record["joins"], store_root)
    if (canonical_bytes(record["processing"]) != canonical_bytes(PROCESSING)
            or record["listening"] != "not_reviewed" or record["musical_verdict"] is not None
            or any(canonical_bytes(record[k]) != canonical_bytes(parent[k])
                   for k in ("context", "occurrence_ids", "timeline_clock_id", "mappings"))
            or canonical_bytes(record["input_signal"]) != canonical_bytes(parent["signal"])):
        raise PocketError("Join envelope provenance mismatch", code="evidence_mismatch")
    if not isinstance(record["audio"], dict) or record["audio"].get("artifact_schema") != "pocket.render-audio/v1":
        raise PocketError("Envelope requires an identified audio artifact")
    payload = read_bytes(record["audio"], store_root)
    if len(payload) > MAX_OUTPUT_BYTES:
        raise PocketError("Practice audio exceeds 64 MiB")
    with sf.SoundFile(io.BytesIO(payload)) as audio:
        if (audio.subtype != "DOUBLE" or audio.samplerate != rate or audio.frames != len(expected)
                or audio.channels != expected.shape[1]
                or not np.array_equal(audio.read(dtype="float64", always_2d=True), expected)):
            raise PocketError("Join envelope samples differ from declared processing", code="evidence_mismatch")
    if canonical_bytes(_signal(payload, rate, expected.shape[1], len(expected))) != canonical_bytes(record["signal"]):
        raise PocketError("Join envelope signal evidence mismatch", code="evidence_mismatch")
    return record


@per_call_verification
def practice_envelope(store_root: str, request_id: str, render: ArtifactHandle,
                      joins: list[JoinEnvelope], attribution: Attribution) -> dict:
    """Create a declared linear fade-out/in at explicit joins; timing and baseline stay fixed."""
    inputs = {"render": render, "joins": joins, "attribution": attribution}
    def work():
        validate_attribution(attribution)
        parent = load_practice_render(render, store_root)
        samples, rate = _processed(parent, joins, store_root)
        output = io.BytesIO()
        sf.write(output, samples, rate, format="WAV", subtype="DOUBLE")
        payload = output.getvalue()
        signal = _signal(payload, rate, samples.shape[1], len(samples))
        audio = put_bytes(payload, store_root, "join-envelope.wav", "pocket.render-audio/v1")
        record = {"schema": SCHEMA, "parent_render": render,
                  **{k: parent[k] for k in ("context", "occurrence_ids", "timeline_clock_id", "mappings")},
                  "joins": joins, "attribution": attribution, "audio": audio, "processing": PROCESSING,
                  "input_signal": parent["signal"], "signal": signal,
                  "listening": "not_reviewed", "musical_verdict": None}
        handle = put_record(record, store_root)
        load_practice_envelope(handle, store_root)
        warnings = ([] if parent["signal"]["usable_for_expectation"] else ["Input: " + parent["signal"]["disposition"]])
        if not signal["usable_for_expectation"]:
            warnings.append("Output: " + signal["disposition"])
        return context_receipt(request_id=request_id, artifacts={"render": handle, "audio": audio},
                               coverage=COVERAGE, warnings=warnings,
                               change_summary={"joins": len(joins), "frames": len(samples), "timing_changed": False})
    return run_request(store_root, request_id, "practice_envelope", inputs, work)


def _comparison(baseline, variants, question, store_root):
    text(question, "question", 2000)
    bounded_list(variants, "variants", 1, 8)
    if len({canonical_bytes(h) for h in variants}) != len(variants):
        raise PocketError("Comparison requires distinct derivative identities")
    original = load_practice_render(baseline, store_root)
    processed = [load_practice_envelope(h, store_root) for h in variants]
    if any(r["parent_render"] != baseline for r in processed):
        raise PocketError("Processed comparison requires derivatives of its exact baseline", code="source_mismatch")
    return [original, *processed]


@per_call_verification
def practice_compare_processed(store_root: str, request_id: str, baseline: ArtifactHandle,
                               variants: list[ArtifactHandle], question: str) -> dict:
    """Compare explicit join derivatives with their unchanged exact parent; no automatic verdict."""
    inputs = {"baseline": baseline, "variants": variants, "question": question}
    def work():
        renders = _comparison(**inputs, store_root=store_root)
        ready = all(r["signal"]["usable_for_expectation"] for r in renders)
        handle = put_record({"schema": COMPARISON_SCHEMA, **inputs, "context": renders[0]["context"],
            "signal_ready": ready, "listening": "not_reviewed", "musical_verdict": None}, store_root)
        return context_receipt(request_id=request_id, artifacts={"comparison": handle},
            coverage={**COVERAGE, "signal_ready": ready},
            warnings=[] if ready else ["Baseline or derivative needs signal review before listening"])
    return run_request(store_root, request_id, "practice_compare_processed", inputs, work)


def load_processed_comparison(handle, store_root):
    _verify_handles(handle, store_root)
    record = read_record(handle, store_root, COMPARISON_SCHEMA)
    fields(record, {"schema", "baseline", "variants", "question", "context", "signal_ready", "listening", "musical_verdict"})
    renders = _comparison(record["baseline"], record["variants"], record["question"], store_root)
    if (record["context"] != renders[0]["context"] or record["listening"] != "not_reviewed"
            or record["musical_verdict"] is not None
            or record["signal_ready"] is not all(r["signal"]["usable_for_expectation"] for r in renders)):
        raise PocketError("Processed comparison evidence mismatch", code="evidence_mismatch")
    return record, renders
