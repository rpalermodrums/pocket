"""Exercise public practice tools on explicit passages in a local recording.

All artifacts and measurements go to a new destination outside the repository.
The variants are boundary probes, not approved musical loops. No audio is acquired
by this recipe, and no listener report or edit approval is invented.
"""
from __future__ import annotations

import argparse
import json
from fractions import Fraction
from functools import partial
from pathlib import Path

import numpy as np
import soundfile as sf

from pocket_music import (
    audio_region_capture,
    audio_region_hypotheses,
    audio_region_query,
    context_create,
    context_query,
    context_resolve,
    identify_audio,
    musical_time,
    practice_compare,
    practice_feedback,
    practice_query,
    practice_render,
)
from pocket_music.assets import sha256_file
from pocket_music.errors import PocketError


def q(value):
    value = Fraction(value)
    return {"n": value.numerator, "d": value.denominator}


def reject(call, contains):
    try:
        call()
    except PocketError as error:
        assert contains in str(error), str(error)
        return str(error)
    raise AssertionError("Expected rejection: " + contains)


def exercise(source: Path, destination: Path, starts: list[Fraction]):
    identity = identify_audio(source)
    rate = identity["sample_rate"]
    positions = [s * rate for s in starts]
    if (not positions or any(p.denominator != 1 or p < 0 or p + 20 * rate > identity["frames"]
                             for p in positions)):
        raise ValueError("Every start must be an exact source frame with twenty seconds available")
    destination.mkdir(parents=True, exist_ok=False)
    store = str(destination / "store")
    reports = []
    for index, position in enumerate(positions):
        start, name = int(position), f"passage-{index + 1}"
        attribution = {"actor": "Pocket reference exercise", "actor_kind": "agent",
                       "statement": "Technical passage probe; estimated pulse and detected attacks are not musical approval.",
                       "uncertainty": ["No human listening", "No established meter, bar one or phrase boundary"]}
        capture = audio_region_capture(store_root=store, request_id=name + "-capture", source={
            "path": str(source), "expected_sha256": identity["sha256"], "start_frame": start,
            "frames": 20 * rate, "source_origin": "independently_acquired"})
        region = capture["artifacts"]["region"]
        hypotheses = audio_region_hypotheses(store_root=store, request_id=name + "-analysis",
            region={"kind": "captured", "region": region}, analysis={"kind": "peek", "settings": {
                "bpm_hint": None, "beats_per_bar": 4}}, attribution=attribution)["artifacts"]["hypotheses"]
        annotations, cursor = [], None
        while True:
            page = audio_region_query(store_root=store, hypotheses=hypotheses, view="annotations",
                                      limit=64, cursor=cursor, max_bytes=65536)
            assert page["status"] == "ok", page
            annotations.extend(page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        pulses = [r for r in annotations if r["local"]["annotation"]["kind"] == "pulse_candidate"]
        attacks = [r for r in annotations if r["local"]["annotation"]["kind"] == "attack"
                   and start + rate <= r["original_projection"]["source_frame"] < start + 3 * rate]
        if not attacks:
            raise PocketError("This probe needs an observed internal attack; choose another explicit passage")
        anchor = attacks[0]
        cue = anchor["original_projection"]["source_frame"]
        # A declared nominal clock preserves original rate. It is not beat tracking.
        bpm = Fraction(str(round(pulses[0]["local"]["annotation"]["bpm"], 2))) if pulses else Fraction(120)
        clock_reason = "rounded leading pulse hypothesis" if pulses else "technical clock only; pulse abstained"
        quarter_length = Fraction(8) * bpm / 60
        time_map = musical_time("create", store, request_id=name + "-clock", definition={
            "source_context": {"schema": "pocket.time-context/v1", "context_id": name,
                               "attribution": clock_reason + "; no verified meter or downbeat"},
            "domain_qn": {"start": q(0), "end": q(4 * quarter_length)},
            "tempo": [{"at_qn": q(0), "bpm": q(bpm), "interpolation": "step"}],
            "host_origin": {"arrangement_qn": q(0), "host_seconds": q(0)}})["artifacts"]["time_map"]
        begin, count, shift = start + rate // 2, 8 * rate, Fraction(rate, 20)
        if shift.denominator != 1:
            raise PocketError("The explicit 50 ms boundary probe must resolve to an exact frame")
        occurrences = [{"occurrence_id": label, "source_clock_id": "recording", "timeline_clock_id": "practice",
                        "source_span_frames": [begin + delta, begin + delta + count],
                        "timeline_span_qn": [q(i * quarter_length), q((i + 1) * quarter_length)]}
                       for i, (label, delta) in enumerate([
                           ("baseline", 0), ("baseline-repeat", 0), ("shifted", int(shift)), ("shifted-repeat", int(shift))])]
        definition = {"context_id": name, "title": "Exact acoustic recording boundary probe", "attribution": attribution,
                      "sources": [{"clock_id": "recording", "region": region}],
                      "timelines": [{"clock_id": "practice", "time_map": time_map}],
                      "occurrences": occurrences, "materials": [],
                      "anchors": [{"anchor_id": "detected-onset", "kind": "onset", "label": "Observed internal attack",
                                   "position": {"clock_id": "recording", "space": "source_frame", "value": cue},
                                   "attribution": {**attribution, "statement": "Authored selection of retained Peek attack " +
                                                   anchor["local"]["annotation_id"]}}]}
        context = context_create(store, name + "-context", definition)["artifacts"]["context"]
        resolve_args = {"store_root": store, "context": context, "target_clock_id": "practice",
                        "target_space": "host_seconds", "anchor_id": "detected-onset"}
        ambiguity = reject(partial(context_resolve, **resolve_args), "Ambiguous")
        resolved = context_resolve(**resolve_args, occurrence_id="baseline-repeat")
        expected = Fraction(8) + Fraction(cue - begin, rate)
        assert resolved["output"]["value"] == q(expected)
        inverse = context_resolve(store, context, "recording", "source_frame", position=resolved["output"],
                                  occurrence_id="baseline-repeat")
        assert inverse["output"]["value"] == cue
        conflict = reject(partial(context_create, store, name + "-context", {**definition, "title": "changed"}),
                          "idempotency_conflict")
        rendered = []
        for label, ids, delta in [("baseline", ["baseline", "baseline-repeat"], 0),
                                  ("shifted", ["shifted", "shifted-repeat"], int(shift))]:
            result = practice_render(store, name + "-" + label, context, ids)
            assert result == practice_render(store, name + "-" + label, context, ids)
            with sf.SoundFile(source) as audio:
                audio.seek(begin + delta)
                expected_audio = audio.read(count, dtype="float64", always_2d=True)
            audio_path = Path(store) / result["artifacts"]["audio"]["artifact_uri"]
            actual, actual_rate = sf.read(audio_path, dtype="float64", always_2d=True)
            assert actual_rate == rate and np.array_equal(actual, np.concatenate([expected_audio] * 2))
            summary = practice_query(store, result["artifacts"]["render"])["summary"]
            rendered.append({"label": label, "result": result, "signal": summary["signal"],
                             "audio_relative": str(audio_path.relative_to(destination)),
                             "original_start_frame": begin + delta, "decoded_samples_exact": True})
        comparison = practice_compare(store, name + "-comparison", rendered[0]["result"]["artifacts"]["render"],
            [rendered[1]["result"]["artifacts"]["render"]],
            "Do the internal cue and repetition join feel better at either explicit boundary? Musical review pending.")
        feedback = practice_feedback(store, name + "-technical-report", comparison["artifacts"]["comparison"],
            rendered[0]["result"]["artifacts"]["render"], [0, 2 * count], "Pocket reference exercise", "agent",
            "Exact decoded samples, source mapping, repeat occurrence, inverse conversion and replay verified. No listening performed.")
        assert practice_query(store, feedback["artifacts"]["feedback"])["summary"]["evidence_kind"] == "agent_report"
        reports.append({"name": name, "capture_start_frame": start, "capture_seconds": float(starts[index]),
                        "region": region, "hypotheses": hypotheses, "context": context,
                        "context_summary": context_query(store, context)["summary"],
                        "pulse_candidates_bpm": [r["local"]["annotation"]["bpm"] for r in pulses],
                        "nominal_clock_bpm": q(bpm), "clock_basis": clock_reason,
                        "analysis_probe_beats_per_bar": 4, "anchor_evidence": anchor,
                        "resolved": resolved, "ambiguity_refused": ambiguity, "changed_request_refused": conflict,
                        "rendered": rendered, "comparison": comparison, "feedback": feedback})
        (destination / "results.json").write_text(json.dumps({"source": identity, "passages": reports,
            "human_listening": "not_performed", "musical_verdict": None}, indent=2) + "\n")
        print(json.dumps({"passage": name, "start_seconds": float(starts[index]), "pulse_candidates": len(pulses),
                          "signal_ready": comparison["coverage"]["signal_ready"], "exact_samples": True}), flush=True)
    assert sha256_file(source) == identity["sha256"]
    return reports


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--start-seconds", type=Fraction, nargs="+", required=True,
                        help="Explicit starts of twenty-second captures; each must resolve to an integer frame")
    args = parser.parse_args()
    exercise(args.source.expanduser().resolve(), args.destination.expanduser().resolve(), args.start_seconds)
