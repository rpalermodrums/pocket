"""The linked-downbeat example keeps the tempo step, clip boundaries and source downbeat distinct."""
import hashlib
import importlib.util
import io
import json
import shlex
import threading
import uuid
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from pocket_music import (
    context_edit,
    context_edit_query,
    context_query,
    context_resolve,
    interpretation_create,
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
H1_LOCKS = [
    {"section": "occurrences", "object_id": "A1", "fields": ["source_span_frames", "timeline_span_qn"]},
    {"section": "occurrences", "object_id": "A2", "fields": ["source_span_frames", "timeline_span_qn"]},
    {"section": "occurrences", "object_id": "B1", "fields": ["timeline_span_qn"]},
    {"section": "occurrences", "object_id": "B2", "fields": ["timeline_span_qn"]},
    {"section": "anchors", "object_id": "a-bar-one", "fields": ["kind", "position"]},
    {"section": "anchors", "object_id": "b-bar-one", "fields": ["kind", "position"]},
    {"section": "anchors", "object_id": "b-pickup", "fields": ["kind", "position"]},
]


def q(value):
    return {"n": value, "d": 1}


def fresh(name):
    """A new request ID: a refused request keeps its journal, so a rerun must not reuse it."""
    return f"{name}-{uuid.uuid4().hex}"


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


def normalized(locks):
    rows = ({**lock, "fields": sorted(lock["fields"])} for lock in locks)
    return sorted(rows, key=lambda lock: lock["object_id"])


def test_only_the_linked_source_windows_move(example):
    _, store, results, _ = example
    parent, child = results["context"], results["h1"]["context"]
    proof = context_edit_query(store, results["h1"]["edit"])["summary"]
    assert proof["changes"] == results["h1"]["changes"] == [
        {"object_id": o, "path": f"/definition/occurrences/{i}/source_span_frames",
         "before": [80000, 170000], "after": [90000, 180000]} for i, o in ((2, "B1"), (3, "B2"))]
    assert normalized(proof["locks"]) == normalized(results["h1"]["locks"]) == normalized(H1_LOCKS)
    before, after = (read_record(c, store)["definition"] for c in (parent, child))
    assert after["timelines"] == before["timelines"]           # same time map handle: tempo step untouched
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
    # Baseline: B's pickup sits on each B clip boundary and its bar one follows one 96 BPM beat
    # later, so five beats separate B1's second downbeat from B2's first.
    b_accents = [f for f in accent_onsets(baseline) if f >= A_OUTPUT]
    assert b_accents == [A_OUTPUT + k * B_FRAMES + off for k in (0, 1) for off in (10000, 50000)]
    # H1: B's bar one starts exactly at each B clip boundary. The beat each B gains at its end is
    # B's following bar one, so a lone downbeat precedes B2's (quarter notes 24 and 25) and ends B2.
    h1_accents = [f for f in accent_onsets(variant) if f >= A_OUTPUT]
    assert h1_accents == [A_OUTPUT + k * B_FRAMES + off for k in (0, 1) for off in (0, 40000, 80000)]
    a_accents = [0, 32000, 64000, 96000]                 # bar one of each 4/4 bar in A1 and A2
    assert [f for f in accent_onsets(variant) if f < A_OUTPUT] == a_accents
    assert [f for f in accent_onsets(baseline) if f < A_OUTPUT] == a_accents


def test_stored_audio_bytes_match_their_handles_and_both_renders_revalidate(example):
    _, store, results, _ = example
    for key in ("baseline", "h1"):
        audio = results[key]["audio"]
        assert read_record(results[key]["render"], store)["audio"] == audio
        on_disk = (Path(store) / audio["artifact_uri"]).read_bytes()
        assert hashlib.sha256(on_disk).hexdigest() == audio["sha256"]
        assert practice_query(store, results[key]["render"])["status"] == "ok"
    assert results["baseline"]["audio"]["sha256"] != results["h1"]["audio"]["sha256"]
    assert results["baseline"]["positions_qn"]["b-bar-one@B1"] == q(17)


def test_timeline_shift_is_intent_only_and_exact_pcm_refuses_it(example):
    _, store, results, _ = example
    assert results["h2"]["renderability"][0]["exact_pcm_compatible"] is False
    assert "stretch" in results["h2"]["renderability"][0]["reason"]
    spans = {o["occurrence_id"]: o["timeline_span_qn"]
             for o in read_record(results["h2"]["context"], store)["definition"]["occurrences"]}
    # B1 now overlaps A2 and straddles the tempo step at quarter note 16.
    assert spans["B1"] == [q(15), q(24)] and spans["A2"] == [q(8), q(16)]
    with pytest.raises(PocketError, match="contiguous and ordered"):
        practice_render(store, fresh("h2-render"), results["h2"]["context"], ORDER)
    with pytest.raises(PocketError, match="time stretch") as refused:
        practice_render(store, fresh("h2-render-b"), results["h2"]["context"], ["B1", "B2"])
    assert refused.value.code == "unsupported_profile"


def test_the_example_locks_refuse_moving_a_a_clip_boundary_or_an_anchor(example):
    _, store, results, _ = example
    parent = results["context"]
    locks = read_record(results["h1"]["edit"], store)["locks"]
    assert normalized(locks) == normalized(H1_LOCKS)
    attribution = read_record(parent, store)["definition"]["attribution"]
    for operation in [
            {"kind": "occurrence_slip_source", "occurrence_ids": ["A1"], "delta_frames": 8000},
            {"kind": "occurrence_shift_timeline", "occurrence_ids": ["B1", "B2"], "delta_qn": q(-1)}]:
        with pytest.raises(PocketError, match="locked field"):
            context_edit(store, fresh("locked"), parent, [operation], locks, attribution)
    # An authored alternative reading (the pickup is bar one) is a valid rebind, but the locks refuse it.
    other = interpretation_create(store, fresh("pickup-reading"), parent, "recording",
                                  {"kind": "bar_one", "status": "authored", "source_frame_q": q(80000)},
                                  attribution)["artifacts"]["interpretation"]
    rebind = {"kind": "anchor_rebind", "anchor_id": "b-bar-one", "binding_id": "pickup-reading",
              "interpretation": other}
    assert context_edit(store, fresh("rebind-unlocked"), parent, [rebind], [], attribution)["status"] == "ok"
    with pytest.raises(PocketError, match="locked field"):
        context_edit(store, fresh("rebind"), parent, [rebind], locks, attribution)


def test_unlinked_slip_moves_only_the_named_repeat(example):
    _, store, results, _ = example
    parent = results["context"]
    attribution = read_record(parent, store)["definition"]["attribution"]
    slip = {"kind": "occurrence_slip_source", "occurrence_ids": ["B1"], "delta_frames": 10000}
    child = context_edit(store, "slip-b1-only", parent, [slip], [], attribution)["artifacts"]["context"]
    positions = [context_resolve(store, child, "practice", "arrangement_qn", anchor_id="b-bar-one",
                                 occurrence_id=o)["output"]["value"] for o in ("B1", "B2")]
    assert positions == [q(16), q(26)]   # nothing infers that B2 repeats B1; a linked edit names both


def test_printed_review_command_opens_the_comparison_and_records_no_listening(example, tmp_path):
    from test_practice_review import call, state

    from pocket_music.cli import parser
    from pocket_music.practice_review import _load_handle, _make_server
    destination, store, results, printed = example
    assert printed["listening"] == results["listening"] == "not_performed"
    assert printed["review"] == ["pocket", "practice-review", "--store-root", store,
                                 "--comparison-file", str(destination / "comparison.json"),
                                 "--session-dir", str(destination / "review"), "--port", "0"]
    assert shlex.split(printed["review_command"]) == printed["review"]
    parsed = parser().parse_args(printed["review"][1:])   # the installed CLI accepts the printed command
    assert (parsed.command, parsed.store_root, parsed.comparison_file, parsed.port) == \
        ("practice-review", store, str(destination / "comparison.json"), 0)
    argv = dict(zip(printed["review"][2::2], printed["review"][3::2], strict=True))
    comparison = _load_handle(argv["--comparison-file"], "comparison")
    assert comparison == results["comparison"]["artifacts"]["comparison"]
    assert comparison["artifact_schema"] == "pocket.practice-revision-comparison/v1"
    assert results["verified_comparison"]["status"] == "ok"
    server = _make_server(argv["--store-root"], comparison, str(tmp_path / "session"))
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
    preview = practice_preview(store, fresh("preview-h1"), results["h1"]["render"],
                               "browser-pcm16-original-rate/v1")
    assert preview["coverage"]["parent_samples_exact"] is True   # PCM16 source: no rounding at all
