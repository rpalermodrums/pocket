"""Generate an exact, synthetic practice comparison using only public providers.

Run: python examples/practice_context.py /path/to/new/private-directory
The destination must not exist. This is a technical demo, not musical acceptance.
"""
import argparse
import json
from pathlib import Path

import numpy as np
import soundfile as sf

from pocket_music import (
    audio_region_capture,
    context_create,
    context_resolve,
    musical_time,
    practice_compare,
    practice_query,
    practice_render,
)
from pocket_music.assets import sha256_file


def run(destination):
    destination.mkdir(parents=True, exist_ok=False)
    store = str(destination / "store")
    source = destination / "synthetic-recording.wav"
    samples = np.sin(np.arange(24000) * 2 * np.pi * 220 / 8000) * 0.2
    samples[10000:18000] += np.sin(np.arange(8000) * 2 * np.pi * 330 / 8000) * 0.1
    sf.write(source, samples, 8000, subtype="PCM_16")
    region = audio_region_capture(store_root=store, request_id="capture", source={
        "path": str(source), "expected_sha256": sha256_file(source), "start_frame": 1000, "frames": 20000,
        "source_origin": "independently_acquired"})["artifacts"]["region"]
    def q(value):
        return {"n": value, "d": 1}
    time_map = musical_time("create", store, request_id="clock", definition={
        "source_context": {"schema": "pocket.time-context/v1", "context_id": "practice-clock",
                           "attribution": "Authored synthetic demo clock"},
        "domain_qn": {"start": q(0), "end": q(8)},
        "tempo": [{"at_qn": q(0), "bpm": q(120), "interpolation": "step"}],
        "host_origin": {"arrangement_qn": q(0), "host_seconds": q(0)},
    })["artifacts"]["time_map"]
    attribution = {"actor": "synthetic demo", "actor_kind": "agent", "statement": "Authored test timing",
                   "uncertainty": ["No inferred downbeat or human listening"]}
    definition = {
        "context_id": "practice-demo", "title": "Exact passage alternatives", "attribution": attribution,
        "sources": [{"clock_id": "recording", "region": region}],
        "timelines": [{"clock_id": "practice", "time_map": time_map}],
        "occurrences": [{"occurrence_id": name, "source_clock_id": "recording", "timeline_clock_id": "practice",
                         "source_span_frames": span, "timeline_span_qn": [q(index * 2), q(index * 2 + 2)]}
                        for index, (name, span) in enumerate([
                            ("first", [2000, 10000]), ("repeat", [2000, 10000]),
                            ("alternative", [10000, 18000]), ("repeat-alternative", [10000, 18000])])],
        "anchors": [{"anchor_id": "internal-cue", "kind": "onset", "label": "Explicit internal point",
                     "position": {"clock_id": "recording", "space": "source_frame", "value": 4000},
                     "attribution": attribution}], "materials": [],
    }
    context = context_create(store, "context", definition)["artifacts"]["context"]
    resolved = context_resolve(store, context, "practice", "arrangement_qn", anchor_id="internal-cue",
                               occurrence_id="repeat")
    baseline = practice_render(store, "baseline", context, ["first", "repeat"])
    alternative = practice_render(store, "alternative", context, ["alternative", "repeat-alternative"])
    comparison = practice_compare(store, "comparison", baseline["artifacts"]["render"],
                                  [alternative["artifacts"]["render"]], "Which passage should the practice loop use?")
    results = {"context": context, "resolved_anchor": resolved, "baseline": baseline, "alternative": alternative,
               "comparison": comparison,
               "verified_comparison": practice_query(store, comparison["artifacts"]["comparison"])}
    (destination / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    # These specs can be replayed through the CLI or sent unchanged as MCP arguments.
    (destination / "resolve-spec.json").write_text(json.dumps({
        "store_root": store, "context": context, "target_clock_id": "practice", "target_space": "arrangement_qn",
        "anchor_id": "internal-cue", "occurrence_id": "repeat"}, indent=2) + "\n")
    return {"results": str(destination / "results.json"), "baseline_audio": str(Path(store) / baseline["artifacts"]["audio"]["artifact_uri"]),
            "alternative_audio": str(Path(store) / alternative["artifacts"]["audio"]["artifact_uri"]),
            "listening": "not_performed"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.destination.expanduser().resolve()), indent=2))
