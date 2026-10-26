# SPDX-License-Identifier: MIT
"""Generate an overlap, move one linked pickup, and prepare an exact review.

Run: python examples/audio_score_audition.py /path/to/new-directory
The destination must not exist. No audio is played and no listening is claimed.
"""
import argparse
import hashlib
import io
import json
import shlex
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

from pocket_music import (
    audio_region_capture,
    audio_score_audition,
    audio_score_create,
    audio_score_query,
    context_create,
    musical_time,
    practice_feedback,
    practice_feedback_query,
    practice_preview,
    practice_query,
)

RATE = 48000
AUTHOR = {"actor": "generated audio-score example", "actor_kind": "agent",
          "statement": "Authored synthetic positions; verify a literal correction",
          "uncertainty": ["No beat detection, human listening or native execution"]}


def q(n, d=1):
    return {"n": n, "d": d}


def save(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def example(destination):
    destination.mkdir(parents=True, exist_ok=False)
    store = str(destination / "store")
    backing = (0.05 * np.sin(np.arange(RATE * 16) * 0.013)).astype(np.float32)
    voice = (0.12 * np.sin(np.arange(RATE * 4) * 0.041)).astype(np.float32)
    voice[24000] = 0.4  # A named generated attack, not a detected sung syllable.
    sources = []
    for name, samples in (("backing", backing), ("voice", voice)):
        output = io.BytesIO()
        sf.write(output, samples, RATE, format="WAV", subtype="FLOAT")
        payload = output.getvalue()
        path = destination / (name + ".wav")
        with path.open("xb") as stream:
            stream.write(payload)
        captured = audio_region_capture(store_root=store, request_id="capture-" + name, source={
            "path": str(path), "expected_sha256": hashlib.sha256(payload).hexdigest(),
            "start_frame": 0, "frames": len(samples), "source_origin": "user_recording"})
        sources.append({"clock_id": name, "region": captured["artifacts"]["region"]})
    time_map = musical_time("create", store, request_id="time", definition={
        "source_context": {"schema": "pocket.time-context/v1", "context_id": "example-clock",
                           "attribution": "Authored 120 BPM quarter-note reference"},
        "domain_qn": {"start": q(0), "end": q(40)},
        "tempo": [{"at_qn": q(0), "bpm": q(120), "interpolation": "step"}],
        "host_origin": {"arrangement_qn": q(0), "host_seconds": q(0)}})["artifacts"]["time_map"]
    occurrences = [{"occurrence_id": "backing", "source_clock_id": "backing", "timeline_clock_id": "set",
                    "source_span_frames": [0, 768000], "timeline_span_qn": [q(0), q(32)]}]
    for name, start, source_start in (("call", 4, 0), ("response", 12, 96000), ("unrelated-repeat", 20, 0)):
        occurrences.append({"occurrence_id": name, "source_clock_id": "voice", "timeline_clock_id": "set",
                            "source_span_frames": [source_start, source_start + 96000],
                            "timeline_span_qn": [q(start), q(start + 4)]})
    context = context_create(store, "context", {
        "context_id": "example", "title": "Generated backing and linked voice", "attribution": AUTHOR,
        "sources": sources, "timelines": [{"clock_id": "set", "time_map": time_map}], "materials": [],
        "occurrences": occurrences, "anchors": [{"anchor_id": "pickup", "kind": "onset",
            "label": "Named generated attack", "attribution": AUTHOR,
            "position": {"clock_id": "voice", "space": "source_frame", "value": 24000}}]})["artifacts"]["context"]
    passages = []
    for name, members, role in (("foundation", ["backing"], "backing"),
                                ("linked-voice", ["call", "response"], "voice"),
                                ("later-repeat", ["unrelated-repeat"], "voice")):
        passages.append({"passage_id": name, "label": name, "attribution": AUTHOR,
                         "members": [{"occurrence_id": member, "role": role} for member in members]})
    score = audio_score_create(store, "score", context, {"score_id": "example", "title": "A linked pickup",
        "attribution": AUTHOR, "passages": passages, "relations": []})["artifacts"]["score"]
    recipe = {"profile": "same-rate-overlay-linear/v1", "controls": [
        {"occurrence_id": o["occurrence_id"], "gain": q(1), "points": []} for o in occurrences]}
    # One sixteenth in this quarter-note reference: 1/4 qn = 0.125 s = 6000 frames.
    comparison = audio_score_audition(store, "audition", score, "linked-voice", "pickup", "call", "set", q(1, 4),
        [{"section": "occurrences", "object_id": name, "fields": ["source_span_frames", "timeline_span_qn"]}
         for name in ("backing", "unrelated-repeat")], AUTHOR, [q(0), q(32)], recipe,
        "Does the linked pickup land later? Generated audio; listening pending.")
    artifacts = comparison["artifacts"]
    expected = {}
    for label, shift in (("baseline", 0), ("variant", 6000)):
        mixed = backing.astype(np.float64).copy()
        mixed[96000 + shift:192000 + shift] += voice[:96000]
        mixed[288000 + shift:384000 + shift] += voice[96000:]
        mixed[480000:576000] += voice[:96000]
        summary = practice_query(store, artifacts[label])["summary"]
        actual, rate = sf.read(Path(store) / summary["audio"]["artifact_uri"], dtype="float64")
        if rate != RATE or not np.array_equal(actual, mixed):
            raise RuntimeError("Independent PCM oracle failed")
        expected[label] = mixed
        preview = practice_preview(store, "preview-" + label, artifacts[label], "browser-pcm16-original-rate/v1")
        save(destination / (label + "-preview.json"), preview)
    if not np.array_equal(expected["baseline"][480000:576000], expected["variant"][480000:576000]):
        raise RuntimeError("Unrelated repeat changed")
    summary = audio_score_query(store, artifacts["score"])["summary"]
    if summary["parent"] != score or summary["context_edit"] != artifacts["edit"]:
        raise RuntimeError("Revised score lost its exact correction ancestry")
    report = practice_feedback(store, "numerical-report", artifacts["comparison"], artifacts["variant"],
        [0, 768000], AUTHOR["actor"], "agent",
        "Independent PCM oracle verified a 6000-frame linked shift and unchanged unrelated repeat. No listening.")
    reports = practice_feedback_query(store, [report["artifacts"]["feedback"]], render=artifacts["variant"])
    if reports["items"][0]["evidence_kind"] != "agent_report":
        raise RuntimeError("Numerical report attribution changed")
    save(destination / "comparison.json", comparison)
    save(destination / "reports.json", [report])
    spec = {"store_root": store, "comparison_file": str(destination / "comparison.json"),
            "session_dir": str(destination / "review"), "reports_file": str(destination / "reports.json"), "port": 0}
    save(destination / "review-spec.json", spec)
    source_root = Path(__file__).resolve().parents[1] / "src"
    command = shlex.join(["env", f"PYTHONPATH={source_root}", sys.executable, "-m", "pocket_music.cli", "practice-review",
                         "--store-root", store, "--comparison-file", spec["comparison_file"],
                         "--session-dir", spec["session_dir"], "--reports-file", spec["reports_file"], "--port", "0"])
    with (destination / "review-command.txt").open("x") as stream:
        stream.write(command + "\n")
    save(destination / "verification.json", {"sample_rate": RATE, "shift_frames": 6000,
        "independent_PCM_oracle": "passed", "exact_score_lineage": "passed", "human_listening": "not_performed",
        "musical_verdict": None, "native_execution": False, "feedback": report["artifacts"]["feedback"]})
    print(command)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="New output directory; existing paths are refused")
    args = parser.parse_args()
    try:
        example(args.destination.expanduser().resolve())
    except FileExistsError:
        parser.error("Destination or output file already exists; choose a new directory")
