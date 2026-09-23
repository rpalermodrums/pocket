"""Independent clock/occurrence oracles and a generated, no-DAW practice path."""
from __future__ import annotations

import copy
import io
import shutil
from fractions import Fraction

import numpy as np
import pytest
import soundfile as sf

from pocket_music.artifact_store import put_bytes, put_record, read_bytes, read_record
from pocket_music.assets import sha256_file
from pocket_music.audio_regions import audio_region_capture
from pocket_music.errors import PocketError
from pocket_music.musical_context import context_create, context_query, context_resolve
from pocket_music.practice_audio import practice_compare, practice_feedback, practice_query, practice_render
from pocket_music.time_maps import musical_time


def q(n, d=1):
    value = Fraction(n, d)
    return {"n": value.numerator, "d": value.denominator}


def fixture(tmp_path):
    store = str(tmp_path / "store")
    source = tmp_path / "generated.wav"
    # Different, precisely located passages, not a claimed acoustic acceptance fixture.
    samples = np.sin(np.arange(32000) * 0.013) * np.linspace(0.1, 0.6, 32000)
    sf.write(source, samples, 8000, subtype="PCM_16")
    region = audio_region_capture(store_root=store, request_id="capture", source={
        "path": str(source), "expected_sha256": sha256_file(source), "start_frame": 700, "frames": 20000,
        "source_origin": "independently_acquired"})["artifacts"]["region"]
    time_definition = {"source_context": {"schema": "pocket.time-context/v1", "context_id": "practice-clock",
                                         "attribution": "Synthetic authored timing; no beat estimation"},
                       "domain_qn": {"start": q(-1), "end": q(8)},
                       "tempo": [{"at_qn": q(-1), "bpm": q(120), "interpolation": "step"}],
                       "host_origin": {"arrangement_qn": q(0), "host_seconds": q(10)}}
    time_map = musical_time("create", store, request_id="time", definition=time_definition)["artifacts"]["time_map"]
    author = {"actor": "synthetic fixture", "actor_kind": "agent", "statement": "Authored test positions",
              "uncertainty": ["This test does not establish a musical downbeat"]}
    definition = {"context_id": "practice", "title": "Recorded passage practice", "attribution": author,
                  "sources": [{"clock_id": "recording", "region": region}],
                  "timelines": [{"clock_id": "practice", "time_map": time_map}],
                  "occurrences": [{"occurrence_id": name, "source_clock_id": "recording",
                                   "timeline_clock_id": "practice", "source_span_frames": source_span,
                                   "timeline_span_qn": [q(start), q(start + 2)]}
                                  for name, source_span, start in [("first", [1700, 9700], 0),
                                                                   ("again", [1700, 9700], 2),
                                                                   ("alternative", [11000, 19000], 4)]],
                  "anchors": [{"anchor_id": "internal-one", "kind": "bar_one", "label": "Authored internal one",
                               "position": {"clock_id": "recording", "space": "source_frame", "value": 3700},
                               "attribution": author}], "materials": []}
    handle = context_create(store, "context", definition)["artifacts"]["context"]
    return store, handle, definition, source, time_definition


def test_internal_anchor_repeated_occurrence_and_exact_inverse(tmp_path):
    store, context, _, _, _ = fixture(tmp_path)
    args = {"store_root": store, "context": context, "anchor_id": "internal-one",
            "target_clock_id": "practice", "target_space": "arrangement_qn"}
    with pytest.raises(PocketError, match="Ambiguous.*occurrence_id"):
        context_resolve(**args)
    first = context_resolve(**args, occurrence_id="first")
    again = context_resolve(**args, occurrence_id="again")
    assert first["output"]["value"] == q(1, 2)
    assert again["output"]["value"] == q(5, 2)
    seconds = context_resolve(**{**args, "target_space": "host_seconds"}, occurrence_id="again")
    assert seconds["output"]["value"] == q(45, 4)
    inverse = context_resolve(store, context, "recording", "source_frame", position=seconds["output"],
                              occurrence_id="again")
    assert inverse["output"]["value"] == 3700
    assert first["coverage"]["interpretation"] == "authored_not_inferred"


@pytest.mark.parametrize("changes,match", [
    ({"target_clock_id": "other"}, "No explicit"),
    ({"occurrence_id": "absent"}, "No occurrence"),
    ({"target_space": "source_frame"}, "Timeline clocks"),
    ({"position": {"clock_id": "recording", "space": "source_frame", "value": True}}, "integer"),
    ({"position": {"clock_id": "recording", "space": "source_frame", "value": 699}}, "integer"),
    ({"position": {"clock_id": "recording", "space": "host_seconds", "value": q(1)}}, "original source_frame"),
])
def test_clock_identity_space_bounds_and_boolean_failures(tmp_path, changes, match):
    store, context, _, _, _ = fixture(tmp_path)
    args = {"store_root": store, "context": context, "target_clock_id": "practice", "target_space": "arrangement_qn",
            "position": {"clock_id": "recording", "space": "source_frame", "value": 3700}, "occurrence_id": "first"}
    with pytest.raises(PocketError, match=match):
        context_resolve(**{**args, **changes})


def test_fractional_sample_is_refused_instead_of_rounded(tmp_path):
    store, context, _, _, _ = fixture(tmp_path)
    with pytest.raises(PocketError, match="exact integer"):
        context_resolve(store, context, "recording", "source_frame", occurrence_id="first",
                        position={"clock_id": "practice", "space": "arrangement_qn", "value": q(1, 3)})


def test_context_revisions_request_identity_and_bounded_queries(tmp_path):
    store, context, definition, _, _ = fixture(tmp_path)
    assert context_create(store, "context", definition)["artifacts"]["context"] == context
    changed = copy.deepcopy(definition)
    changed["anchors"][0]["position"]["value"] = 5700
    with pytest.raises(PocketError, match="idempotency_conflict"):
        context_create(store, "context", changed)
    child = context_create(store, "revision", changed, parent=context)["artifacts"]["context"]
    assert child != context
    assert context_query(store, child)["summary"]["parent"] == context
    assert context_query(store, context, "anchors")["rows"][0]["position"]["value"] == 3700
    page = context_query(store, context, "occurrences", limit=1)
    assert page["next_offset"] == 1 and page["total"] == 3
    wrong = {**changed, "context_id": "another"}
    with pytest.raises(PocketError, match="different musical context"):
        context_create(store, "wrong-parent", wrong, parent=context)


@pytest.mark.parametrize("change,match", [
    (lambda d: d["sources"].append(d["sources"][0]), "Duplicate clock_id"),
    (lambda d: d["occurrences"][0].update(source_span_frames=[700, 20701]), "integer"),
    (lambda d: d["occurrences"][0].update(timeline_span_qn=[q(0), {"n": 4, "d": 2}]), "reduced"),
    (lambda d: d["anchors"][0].update(kind="clip_start"), "anchor kind"),
])
def test_invalid_context_definitions_are_not_published(tmp_path, change, match):
    store, _, definition, _, _ = fixture(tmp_path)
    change(definition)
    with pytest.raises(PocketError, match=match):
        context_create(store, "bad", definition)


def test_no_daw_practice_loop_comparison_feedback_and_relocation(tmp_path, monkeypatch):
    import builtins
    original_import = builtins.__import__
    def guarded(name, *args, **kwargs):
        if any(part in name for part in ("native_candidates", "auditions", "instruments", "baste")) or name == "mido":
            raise AssertionError("Standalone practice requested unrelated adapter " + name)
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    store, context, _, source, _ = fixture(tmp_path)
    original_bytes = source.read_bytes()
    source_samples, _ = sf.read(source, always_2d=True)
    first = practice_render(store, "first-render", context, ["first"])
    loop = practice_render(store, "loop-render", context, ["first", "again"])
    alternative = practice_render(store, "alternative-render", context, ["alternative"])
    audio, rate = sf.read(io.BytesIO(read_bytes(loop["artifacts"]["audio"], store)), always_2d=True)
    assert rate == 8000
    assert np.array_equal(audio, np.concatenate([source_samples[1700:9700]] * 2))
    mappings = practice_query(store, loop["artifacts"]["render"], "mappings", limit=1)
    assert mappings["rows"][0]["source_span_frames"] == [1700, 9700]
    assert mappings["rows"][0]["output_span_frames"] == [0, 8000] and mappings["next_offset"] == 1
    comparison = practice_compare(store, "compare", first["artifacts"]["render"],
                                  [alternative["artifacts"]["render"]], "Which passage is useful to practise?")
    assert comparison["coverage"]["human_listening"] == "not_performed"
    report = practice_feedback(store, "report", comparison["artifacts"]["comparison"],
                               alternative["artifacts"]["render"], [2000, 6000], "test agent", "agent",
                               "Synthetic report for transport and binding checks", "revise")
    summary = practice_query(store, report["artifacts"]["feedback"])["summary"]
    assert summary["evidence_kind"] == "agent_report"
    assert summary["render_sha256"] == alternative["artifacts"]["audio"]["sha256"]
    assert source.read_bytes() == original_bytes
    moved = tmp_path / "relocated"
    shutil.copytree(store, moved)
    source.unlink()
    assert practice_query(str(moved), report["artifacts"]["feedback"])["summary"] == summary


def test_unsupported_realizations_and_duration_comparisons_are_explicit(tmp_path):
    store, context, definition, _, _ = fixture(tmp_path)
    for ids, message in [(["first", "first"], "separate occurrence"), (["first", "alternative"], "contiguous"),
                         (["again", "first"], "contiguous"), (["absent"], "Unknown occurrence")]:
        with pytest.raises(PocketError, match=message):
            practice_render(store, "fail-" + "-".join(ids), context, ids)
    first = practice_render(store, "one", context, ["first"])["artifacts"]["render"]
    loop = practice_render(store, "two", context, ["first", "again"])["artifacts"]["render"]
    with pytest.raises(PocketError, match="Unequal durations"):
        practice_compare(store, "different", first, [loop], "Question")
    assert practice_compare(store, "explicit", first, [loop], "Question", True)["status"] == "ok"
    definition["occurrences"][0]["timeline_span_qn"] = [q(0), q(3)]
    stretched = context_create(store, "stretch-context", definition)["artifacts"]["context"]
    # Coordinate intent is representable; rendering support is a separate fact.
    assert context_resolve(store, stretched, "practice", "arrangement_qn", anchor_id="internal-one",
                           occurrence_id="first")["output"]["value"] == q(3, 4)
    with pytest.raises(PocketError, match="time stretch"):
        practice_render(store, "stretch", stretched, ["first"])


def test_tempo_changes_with_correct_total_duration_still_require_warp(tmp_path):
    store, _, definition, _, timing = fixture(tmp_path)
    # 1 qn @ 180 + 1 qn @ 90 = 1 second overall, but is not original-rate PCM.
    timing["tempo"] = [{"at_qn": q(-1), "bpm": q(180), "interpolation": "step"},
                       {"at_qn": q(1), "bpm": q(90), "interpolation": "step"}]
    handle = musical_time("create", store, request_id="ramped-steps", definition=timing)["artifacts"]["time_map"]
    definition["timelines"][0]["time_map"] = handle
    context = context_create(store, "varying-context", definition)["artifacts"]["context"]
    with pytest.raises(PocketError, match="time stretch"):
        practice_render(store, "varying-render", context, ["first"])


def test_forged_audio_and_evidence_are_rejected_even_with_valid_handles(tmp_path):
    store, context, _, _, _ = fixture(tmp_path)
    rendered = practice_render(store, "render", context, ["first"])
    record = read_record(rendered["artifacts"]["render"], store)
    record["mappings"][0]["source_span_frames"] = [1701, 9701]
    with pytest.raises(PocketError, match="mapping differs"):
        practice_query(store, put_record(record, store))
    record = read_record(rendered["artifacts"]["render"], store)
    payload = read_bytes(record["audio"], store)
    # Reverse samples: preserves signal measures and duration but violates source mapping.
    samples, rate = sf.read(io.BytesIO(payload), always_2d=True)
    output = io.BytesIO()
    sf.write(output, samples[::-1], rate, format="WAV", subtype="DOUBLE")
    record["audio"] = put_bytes(output.getvalue(), store, "changed.wav", "pocket.render-audio/v1")
    from pocket_music.practice_audio import _signal
    record["signal"] = _signal(output.getvalue(), rate, 1, len(samples))
    with pytest.raises(PocketError, match="samples differ"):
        practice_query(store, put_record(record, store))


def test_tampered_source_artifact_invalidates_replay_and_read(tmp_path):
    store, context, definition, _, _ = fixture(tmp_path)
    rendered = practice_render(store, "render", context, ["first"])
    region = read_record(definition["sources"][0]["region"], store)
    from pathlib import Path
    path = Path(store) / region["crop"]["artifact_uri"]
    path.write_bytes(path.read_bytes()[:-1] + b"x")
    with pytest.raises(PocketError, match="integrity"):
        practice_render(store, "render", context, ["first"])
    with pytest.raises(PocketError, match="integrity"):
        practice_query(store, rendered["artifacts"]["render"])


def test_rational_import_remains_same_object():
    from pocket_music.material_types import Rational as old
    from pocket_music.music_types import Rational as neutral
    from pocket_music.time_types import Rational as time
    assert old is neutral is time


def test_original_rate_render_is_not_coupled_to_native_120_bpm_profile(tmp_path):
    store, _, definition, _, timing = fixture(tmp_path)
    timing["tempo"][0]["bpm"] = q(150)
    time_map = musical_time("create", store, request_id="150", definition=timing)["artifacts"]["time_map"]
    definition["timelines"][0]["time_map"] = time_map
    definition["occurrences"] = [{**definition["occurrences"][0], "source_span_frames": [1700, 8100]}]
    context = context_create(store, "150-context", definition)["artifacts"]["context"]
    result = practice_render(store, "150-render", context, ["first"])
    assert result["change_summary"]["frames"] == 6400
    assert context_resolve(store, context, "practice", "arrangement_qn", anchor_id="internal-one")["output"]["value"] == q(5, 8)


def test_feedback_rejects_other_render_out_of_bounds_and_false_human_attribution(tmp_path):
    store, context, _, _, _ = fixture(tmp_path)
    first = practice_render(store, "first", context, ["first"])["artifacts"]["render"]
    alternative = practice_render(store, "alt", context, ["alternative"])["artifacts"]["render"]
    again = practice_render(store, "again", context, ["again"])["artifacts"]["render"]
    comparison = practice_compare(store, "compare", first, [alternative], "Question")["artifacts"]["comparison"]
    with pytest.raises(PocketError, match="not in this comparison"):
        practice_feedback(store, "outside", comparison, again, [0, 100], "fixture", "agent", "Test")
    with pytest.raises(PocketError, match="outside this exact render"):
        practice_feedback(store, "bounds", comparison, first, [0, 8001], "fixture", "agent", "Test")
    feedback = practice_feedback(store, "agent", comparison, first, [0, 8000], "fixture", "agent", "Test")["artifacts"]["feedback"]
    record = read_record(feedback, store)
    record["evidence_kind"] = "attributed_human_listening"
    with pytest.raises(PocketError, match="attribution or render identity"):
        practice_query(store, put_record(record, store))


@pytest.mark.parametrize("field,value,match", [
    ("audio", "not-a-handle", "identified audio artifact"),
    ("processing", {"profile": "exact-pcm-occurrences/v1", "output_subtype": "DOUBLE", "fades": 0,
                    "normalization": False, "resampling": False, "channel_conversion": False,
                    "time_stretch": False, "mixing": False}, "evidence profile"),
])
def test_malformed_retained_records_fail_as_contract_errors(tmp_path, field, value, match):
    store, context, _, _, _ = fixture(tmp_path)
    render = practice_render(store, "render", context, ["first"])["artifacts"]["render"]
    record = read_record(render, store)
    record[field] = value
    with pytest.raises(PocketError, match=match):
        practice_query(store, put_record(record, store))


def test_float_overloads_are_preserved_and_comparison_is_not_signal_ready(tmp_path):
    store, _, definition, source, _ = fixture(tmp_path)
    samples, rate = sf.read(source, always_2d=True)
    samples[3700, 0] = 1.05
    sf.write(source, samples, rate, subtype="FLOAT")
    original = source.read_bytes()
    region = audio_region_capture(store_root=store, request_id="float-capture", source={
        "path": str(source), "expected_sha256": sha256_file(source), "start_frame": 700, "frames": 20000,
        "source_origin": "independently_acquired"})["artifacts"]["region"]
    definition["sources"][0]["region"] = region
    context = context_create(store, "float-context", definition)["artifacts"]["context"]
    baseline = practice_render(store, "float-baseline", context, ["first", "again"])
    decoded, _ = sf.read(io.BytesIO(read_bytes(baseline["artifacts"]["audio"], store)), always_2d=True)
    assert decoded[2000, 0] == decoded[10000, 0] == float(np.float32(1.05))
    assert baseline["warnings"] == ["sample_overload"]
    alternate = practice_render(store, "float-alternate", context, ["alternative"])
    compared = practice_compare(store, "float-compare", baseline["artifacts"]["render"],
                                [alternate["artifacts"]["render"]], "Technical overload preservation", True)
    assert compared["coverage"]["signal_ready"] is False
    assert source.read_bytes() == original
