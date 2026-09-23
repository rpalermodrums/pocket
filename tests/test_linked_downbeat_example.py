"""The linked-downbeat example keeps the tempo step, clip boundaries and source downbeat distinct."""
import hashlib
import importlib.util
import io
import json
import threading
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from pocket_music import (
    context_edit,
    context_query,
    context_resolve,
    musical_time,
    practice_preview,
    practice_query,
)
from pocket_music.artifact_store import read_bytes, read_record
from pocket_music.errors import PocketError
from pocket_music.practice_audio import practice_render

ROOT = Path(__file__).resolve().parents[1]
ORDER = ["A1", "A2", "B1", "B2"]
A_OUTPUT = 128000                                 # A1 + A2: two copies of source [0, 64000)
B_FRAMES = 90000                                  # every B occurrence keeps its source duration


def q(value):
    return {"n": value, "d": 1}


@pytest.fixture(scope="module")
def example(tmp_path_factory):
    spec = importlib.util.spec_from_file_location("linked_downbeat", ROOT / "examples/linked_downbeat.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    destination = tmp_path_factory.mktemp("linked-downbeat") / "run"
    printed = module.run(destination)
    results = json.loads((destination / "results.json").read_text())
    return destination, str(destination / "store"), results, printed


def decoded(store, render):
    audio = read_bytes(read_record(render, store)["audio"], store)
    return sf.read(io.BytesIO(audio), dtype="float64", always_2d=True)[0][:, 0]


def accent_onsets(samples):
    """First frame of each accented click: a sample above 0.4 after 100 frames without one."""
    loud = np.abs(samples) > 0.4
    return [int(i) for i in np.flatnonzero(loud) if not loud[max(0, i - 100):i].any()]


def test_only_the_linked_source_windows_move(example):
    _, store, results, _ = example
    parent, child = results["context"], results["h1"]["context"]
    assert results["h1"]["changes"] == [
        {"object_id": o, "path": f"/definition/occurrences/{i}/source_span_frames",
         "before": [80000, 170000], "after": [90000, 180000]} for i, o in ((2, "B1"), (3, "B2"))]
    before, after = (read_record(c, store)["definition"] for c in (parent, child))
    assert after["timelines"] == before["timelines"]           # the same time map handle: tempo step untouched
    assert after["anchors"] == before["anchors"]               # the source downbeat claim is unchanged
    assert [o["timeline_span_qn"] for o in after["occurrences"]] == \
        [o["timeline_span_qn"] for o in before["occurrences"]]  # clip boundaries are unchanged
    time_map = before["timelines"][0]["time_map"]
    steps = musical_time("query", store, time_map=time_map, section="tempo")["rows"]
    assert [(s["at_qn"], s["bpm"]) for s in steps] == [(q(0), q(120)), (q(16), q(96))]
    assert context_query(store, parent)["status"] == context_query(store, child)["status"] == "ok"


def test_bar_one_lands_on_each_clip_boundary_and_the_pickup_is_no_longer_played(example):
    _, store, results, _ = example
    parent, child = results["context"], results["h1"]["context"]

    def at(ctx, anchor, occurrence):
        return context_resolve(store, ctx, "practice", "arrangement_qn", anchor_id=anchor,
                               occurrence_id=occurrence)["output"]["value"]

    assert (at(parent, "b-bar-one", "B1"), at(parent, "b-bar-one", "B2")) == (q(17), q(26))
    assert (at(child, "b-bar-one", "B1"), at(child, "b-bar-one", "B2")) == (q(16), q(25))
    for ctx in (parent, child):
        assert (at(ctx, "a-bar-one", "A1"), at(ctx, "a-bar-one", "A2")) == (q(0), q(8))
    assert at(parent, "b-pickup", "B1") == q(16)
    for occurrence in ("B1", "B2"):
        with pytest.raises(PocketError, match="extrapolation is unsupported"):
            at(child, "b-pickup", occurrence)
    assert results["h1"]["positions_qn"]["b-bar-one@B1"] == q(16)
    assert results["h1"]["renderability"] == [
        {"occurrence_ids": ["B1", "B2"], "exact_pcm_compatible": True, "reason": None}]


def test_output_samples_match_an_independent_source_oracle(example):
    destination, store, results, _ = example
    source = sf.read(destination / "synthetic-click-track.wav", dtype="float64", always_2d=True)[0][:, 0]
    baseline, variant = decoded(store, results["baseline"]["render"]), decoded(store, results["h1"]["render"])
    a = np.concatenate([source[0:64000]] * 2)
    np.testing.assert_array_equal(baseline, np.concatenate([a, source[80000:170000], source[80000:170000]]))
    np.testing.assert_array_equal(variant, np.concatenate([a, source[90000:180000], source[90000:180000]]))
    assert len(variant) == len(baseline) == A_OUTPUT + 2 * B_FRAMES
    # Baseline: B's pickup sits on each handover and its bar one follows one 96 BPM beat later.
    b_accents = [f for f in accent_onsets(baseline) if f >= A_OUTPUT]
    assert b_accents == [A_OUTPUT + k * B_FRAMES + off for k in (0, 1) for off in (10000, 50000)]
    # H1: B's bar one starts exactly at each clip boundary; each B gains one beat at its end.
    h1_accents = [f for f in accent_onsets(variant) if f >= A_OUTPUT]
    assert h1_accents == [A_OUTPUT + k * B_FRAMES + off for k in (0, 1) for off in (0, 40000, 80000)]
    a_accents = [0, 32000, 64000, 96000]                 # bar one of each 4/4 bar in A1 and A2
    assert [f for f in accent_onsets(variant) if f < A_OUTPUT] == a_accents
    assert [f for f in accent_onsets(baseline) if f < A_OUTPUT] == a_accents


def test_baseline_is_unchanged_and_both_renders_revalidate(example):
    _, store, results, _ = example
    for key in ("baseline", "h1"):
        render = results[key]["render"]
        audio = read_bytes(read_record(render, store)["audio"], store)
        assert hashlib.sha256(audio).hexdigest() == results[key]["audio_sha256"]
        assert practice_query(store, render)["status"] == "ok"
    assert results["baseline"]["positions_qn"]["b-bar-one@B1"] == q(17)


def test_timeline_shift_is_intent_only_and_exact_pcm_refuses_it(example):
    _, store, results, _ = example
    assert results["h2"]["renderability"][0]["exact_pcm_compatible"] is False
    assert "stretch" in results["h2"]["renderability"][0]["reason"]
    spans = {o["occurrence_id"]: o["timeline_span_qn"]
             for o in read_record(results["h2"]["context"], store)["definition"]["occurrences"]}
    assert spans["B1"] == [q(15), q(24)] and spans["A2"] == [q(8), q(16)]   # overlaps A2 and straddles the step
    with pytest.raises(PocketError, match="contiguous and ordered"):
        practice_render(store, "h2-render", results["h2"]["context"], ORDER)
    with pytest.raises(PocketError, match="time stretch") as refused:
        practice_render(store, "h2-render-b", results["h2"]["context"], ["B1", "B2"])
    assert refused.value.code == "unsupported_profile"


def test_locks_refuse_moving_a_or_a_clip_boundary(example):
    _, store, results, _ = example
    parent = results["context"]
    attribution = read_record(parent, store)["definition"]["attribution"]
    locks = [{"section": "occurrences", "object_id": "A1", "fields": ["source_span_frames", "timeline_span_qn"]},
             {"section": "occurrences", "object_id": "B1", "fields": ["timeline_span_qn"]}]
    with pytest.raises(PocketError, match="locked field"):
        context_edit(store, "slip-a", parent, [{"kind": "occurrence_slip_source", "occurrence_ids": ["A1"],
                                               "delta_frames": 8000}], locks, attribution)
    with pytest.raises(PocketError, match="locked field"):
        context_edit(store, "shift-b", parent, [{"kind": "occurrence_shift_timeline", "occurrence_ids": ["B1"],
                                                "delta_qn": q(-1)}], locks, attribution)


def test_unlinked_slip_moves_only_the_named_repeat(example):
    _, store, results, _ = example
    parent = results["context"]
    attribution = read_record(parent, store)["definition"]["attribution"]
    child = context_edit(store, "slip-b1-only", parent, [{"kind": "occurrence_slip_source", "occurrence_ids": ["B1"],
                                                          "delta_frames": 10000}], [], attribution)["artifacts"]["context"]
    positions = [context_resolve(store, child, "practice", "arrangement_qn", anchor_id="b-bar-one",
                                 occurrence_id=o)["output"]["value"] for o in ("B1", "B2")]
    assert positions == [q(16), q(26)]   # nothing infers that B2 repeats B1; a linked edit names both


def test_review_keeps_position_when_switching_and_records_no_listening(example, tmp_path):
    from test_practice_review import call, state

    from pocket_music.practice_review import _load_handle, _make_server
    destination, store, results, printed = example
    assert printed["listening"] == results["listening"] == "not_performed"
    assert printed["review"][:2] == ["pocket", "practice-review"]
    comparison = _load_handle(str(destination / "comparison.json"), "comparison")
    assert comparison == results["comparison"]["artifacts"]["comparison"]
    assert comparison["artifact_schema"] == "pocket.practice-revision-comparison/v1"
    assert results["verified_comparison"]["status"] == "ok"
    server = _make_server(store, comparison, str(tmp_path / "session"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        summary = state(server)
        assert summary["alignment"]["synchronized_switching"] is True
        assert summary["report_count"] == 0
        status, _, _ = call(server, "/api/review/previews",
                            {"expected_revision": summary["revision"], "item_id": "variant-1"})
        assert status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    preview = practice_preview(store, "preview-h1", results["h1"]["render"], "browser-pcm16-original-rate/v1")
    assert preview["coverage"]["parent_samples_exact"] is True   # PCM16 source: no rounding at all
