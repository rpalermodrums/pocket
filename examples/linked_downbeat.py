"""Land a repeated passage's internal downbeat on its handover without moving the clip or tempo step.

Run: python examples/linked_downbeat.py /path/to/new/private-directory
The destination must not exist. This is a synthetic technical demo, not musical acceptance.

A generated 16 kHz click track has two passages. A (120 BPM) starts on its bar one. B (96 BPM)
starts with a one-beat pickup, so its bar one is inside the passage. The timeline plays A twice,
then B twice. Three positions stay distinct records even where they share a number:

  * tempo-change point: the time map steps from 120 to 96 BPM at quarter note 16;
  * clip boundary: occurrence B1 starts at quarter note 16 (B2 at 25);
  * source downbeat: the authored bar-one anchor at source frame 90000, one beat after B's
    first frame (80000).

The exact PCM renderer refuses an occurrence whose tempo changes inside it, so the 96 BPM step
sits on the A2|B1 clip boundary. H1 slips the source window of every B occurrence (a linked edit
names each repeat) by one B beat. Locks keep A, the B clip placement and every anchor's kind and
position fixed; no context edit can change the time map. B's bar one then lands on both B clip
boundaries. Each B keeps its nine-beat length, so it gains the next source beat at its end: B's
following bar one. H2 shifts B's clip placement instead; the context accepts it as intent, and
the edit reports that exact PCM cannot render it.
"""
import argparse
import json
import shlex
from pathlib import Path

import numpy as np
import soundfile as sf

from pocket_music import (
    audio_region_capture,
    context_create,
    context_edit,
    context_edit_query,
    context_resolve,
    musical_time,
    practice_compare_revisions,
    practice_query,
    practice_render,
)
from pocket_music.artifact_store import read_record
from pocket_music.assets import sha256_file
from pocket_music.errors import PocketError

RATE = 16000
A_BEAT = 8000                    # frames per quarter note at 120 BPM
B_BEAT = 10000                   # frames per quarter note at 96 BPM
A_SPAN = [0, 64000]              # two 4/4 bars, bar one at the first frame
B_START = 80000                  # the pickup beat
B_BAR_ONE = B_START + B_BEAT     # source frame 90000
B_SPAN = [B_START, B_START + 9 * B_BEAT]   # pickup plus two bars
SOURCE_FRAMES = B_START + 11 * B_BEAT      # B material continues after B_SPAN
AUTHOR = {"actor": "linked-downbeat example", "actor_kind": "agent",
          "statement": "Generated click track with authored bar-one positions",
          "uncertainty": ["Synthetic material; no musical judgement or listening"]}


def q(value):
    return {"n": value, "d": 1}


def click_track():
    """Decaying clicks on every beat; bar ones are louder and higher."""
    audio = np.zeros(SOURCE_FRAMES)
    beats = [(i * A_BEAT, i % 4 == 0) for i in range(8)]
    beats += [(B_START, False)] + [(B_BAR_ONE + i * B_BEAT, i % 4 == 0) for i in range(10)]
    t = np.arange(1200) / RATE
    for frame, accent in beats:
        audio[frame:frame + 1200] += (0.6 if accent else 0.25) * np.cos(
            2 * np.pi * (880 if accent else 440) * t) * np.exp(-t * 60)
    return audio


def run(destination):
    destination.mkdir(parents=True, exist_ok=False)
    store = str(destination / "store")
    source = destination / "synthetic-click-track.wav"
    sf.write(source, click_track(), RATE, subtype="PCM_16")
    region = audio_region_capture(store_root=store, request_id="capture", source={
        "path": str(source), "expected_sha256": sha256_file(source), "start_frame": 0,
        "frames": SOURCE_FRAMES, "source_origin": "independently_acquired"})["artifacts"]["region"]
    time_map = musical_time("create", store, request_id="clock", definition={
        "source_context": {"schema": "pocket.time-context/v1", "context_id": "linked-downbeat-clock",
                           "attribution": "Authored synthetic timeline; the step is not a detected tempo"},
        "domain_qn": {"start": q(0), "end": q(40)},
        "tempo": [{"at_qn": q(0), "bpm": q(120), "interpolation": "step"},
                  {"at_qn": q(16), "bpm": q(96), "interpolation": "step"}],
        "host_origin": {"arrangement_qn": q(0), "host_seconds": q(0)},
    })["artifacts"]["time_map"]
    occurrences = [{"occurrence_id": name, "source_clock_id": "recording", "timeline_clock_id": "practice",
                    "source_span_frames": span, "timeline_span_qn": [q(start), q(end)]}
                   for name, span, start, end in [("A1", A_SPAN, 0, 8), ("A2", A_SPAN, 8, 16),
                                                  ("B1", B_SPAN, 16, 25), ("B2", B_SPAN, 25, 34)]]
    anchors = [{"anchor_id": anchor_id, "kind": kind, "label": label, "attribution": AUTHOR,
                "position": {"clock_id": "recording", "space": "source_frame", "value": frame}}
               for anchor_id, kind, label, frame in [
                   ("a-bar-one", "bar_one", "A bar one, at its first frame", A_SPAN[0]),
                   ("b-bar-one", "bar_one", "B bar one, one beat after its first frame", B_BAR_ONE),
                   ("b-pickup", "onset", "B pickup, its first frame", B_START)]]
    context = context_create(store, "context", {
        "context_id": "linked-downbeat", "title": "A to B handover with an internal B downbeat",
        "attribution": AUTHOR, "sources": [{"clock_id": "recording", "region": region}],
        "timelines": [{"clock_id": "practice", "time_map": time_map}],
        "occurrences": occurrences, "anchors": anchors, "materials": []})["artifacts"]["context"]
    order = ["A1", "A2", "B1", "B2"]
    baseline = practice_render(store, "baseline", context, order)["artifacts"]["render"]

    # H1: slip every B occurrence's source window by one B beat. Locks refuse any change to A,
    # to the B clip boundaries or to an anchor's kind or position; the time map is not an edit target.
    locks = ([{"section": "occurrences", "object_id": o, "fields": ["source_span_frames", "timeline_span_qn"]}
              for o in ("A1", "A2")]
             + [{"section": "occurrences", "object_id": o, "fields": ["timeline_span_qn"]}
                for o in ("B1", "B2")]
             + [{"section": "anchors", "object_id": a, "fields": ["position", "kind"]}
                for a in ("a-bar-one", "b-bar-one", "b-pickup")])
    slip = {"kind": "occurrence_slip_source", "occurrence_ids": ["B1", "B2"], "delta_frames": B_BEAT}
    h1 = context_edit(store, "h1-slip", context, [slip], locks,
                      {**AUTHOR, "statement": "H1: land B's bar one on each B clip boundary"})
    child = h1["artifacts"]["context"]
    proof = context_edit_query(store, h1["artifacts"]["edit"])["summary"]  # recomputed from the exact parent
    variant = practice_render(store, "h1", child, order)["artifacts"]["render"]

    # H2: move B's clip placement one beat earlier instead. That crosses A2's end and the tempo step.
    shift = {"kind": "occurrence_shift_timeline", "occurrence_ids": ["B1", "B2"], "delta_qn": q(-1)}
    h2 = context_edit(store, "h2-shift", context, [shift], [],
                      {**AUTHOR, "statement": "H2: move B's clip one beat earlier (intent only)"})
    h2_proof = context_edit_query(store, h2["artifacts"]["edit"])["summary"]

    # Each occurrence is paired with itself at identical output frames, so the review page may keep
    # the playhead position when switching between the baseline and H1.
    pairs = [{"baseline_occurrence_id": m["occurrence_id"], "variant_occurrence_id": m["occurrence_id"],
              "baseline_interval_frames": m["output_span_frames"],
              "variant_interval_frames": m["output_span_frames"]}
             for m in read_record(baseline, store)["mappings"]]
    comparison = practice_compare_revisions(
        store, "comparison", baseline, [variant], [h1["artifacts"]["edit"]],
        [{"variant": variant, "pairs": pairs}],
        "Should B's bar one land on its clip boundary at the A to B handover and at B's repeat (H1), "
        "or keep its pickup there (baseline)? Both keep nine-beat B occurrences, so H1 has a one-beat bar "
        "just before the repeat and the baseline has five beats between the downbeats either side of it. "
        "Synthetic click track.")

    def resolve(ctx, anchor_id, occurrence_id):
        try:
            return context_resolve(store, ctx, "practice", "arrangement_qn", anchor_id=anchor_id,
                                   occurrence_id=occurrence_id)["output"]["value"]
        except PocketError as error:
            return {"unresolved": str(error)}

    def positions(ctx):
        return {f"{a}@{o}": resolve(ctx, a, o) for a, occurrences in [
            ("a-bar-one", ("A1", "A2")), ("b-bar-one", ("B1", "B2")), ("b-pickup", ("B1", "B2"))]
            for o in occurrences}

    results = {
        "tempo_steps": musical_time("query", store, time_map=time_map, section="tempo")["rows"],
        "clip_boundaries_qn": {"A2|B1": q(16), "B1|B2": q(25)},
        "source_downbeat_frame": B_BAR_ONE, "pickup_frame": B_START,
        "context": context,
        "baseline": {"render": baseline, "audio": read_record(baseline, store)["audio"],
                     "positions_qn": positions(context)},
        "h1": {"edit": h1["artifacts"]["edit"], "context": child, "locks": proof["locks"],
               "changes": proof["changes"], "renderability": proof["renderability"], "render": variant,
               "audio": read_record(variant, store)["audio"], "positions_qn": positions(child)},
        "h2": {"edit": h2["artifacts"]["edit"], "context": h2["artifacts"]["context"],
               "renderability": h2_proof["renderability"]},
        "comparison": comparison,
        "verified_comparison": practice_query(store, comparison["artifacts"]["comparison"]),
        "listening": "not_performed",
    }
    (destination / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    (destination / "comparison.json").write_text(json.dumps(comparison, indent=2) + "\n")
    review = ["pocket", "practice-review", "--store-root", store,
              "--comparison-file", str(destination / "comparison.json"),
              "--session-dir", str(destination / "review"), "--port", "0"]
    return {"results": str(destination / "results.json"), "review": review,
            "review_command": shlex.join(review), "listening": "not_performed"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    result = run(args.destination.expanduser().resolve())
    print(json.dumps(result, indent=2))
    print(f"\nListen and save your own report with:\n{result['review_command']}")
