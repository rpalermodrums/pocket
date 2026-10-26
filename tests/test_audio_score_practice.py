# SPDX-License-Identifier: AGPL-3.0-only
"""Generated audio proves bounded overlap, literal timing and retained selection lineage."""
import copy
import io
import shutil

import numpy as np
import pytest
import soundfile as sf

from pocket_music.artifact_store import put_bytes, put_record, read_bytes, read_record
from pocket_music.assets import sha256_file
from pocket_music.audio_regions import audio_region_capture
from pocket_music.audio_score import audio_score_create
from pocket_music.audio_score_practice import audio_score_audition, audio_score_render
from pocket_music.errors import PocketError
from pocket_music.musical_context import context_create
from pocket_music.practice_audio import practice_feedback, practice_query, practice_render
from pocket_music.practice_feedback_query import practice_feedback_query
from pocket_music.practice_previews import practice_preview
from pocket_music.time_maps import musical_time


def q(n, d=1):
    from fractions import Fraction
    value = Fraction(n, d)
    return {"n": value.numerator, "d": value.denominator}


def score_fixture(tmp_path, channels=1):
    """Public generated acceptance fixture: linked call/response and an unrelated same-source repeat."""
    store = str(tmp_path / "store")
    rate = 48000
    backing = (0.05 * np.sin(np.arange(rate * 16) * 0.013)).astype(np.float32)
    voice = (0.12 * np.sin(np.arange(rate * 4) * 0.041)).astype(np.float32)
    voice[24000] = 0.4  # A named synthetic attack, not a claim about sung words or acoustic beat detection.
    if channels == 2:
        backing = np.column_stack((backing, backing * 0.8))
        voice = np.column_stack((voice, voice * 0.8))
    regions, source_paths = {}, {}
    for name, samples in (("backing", backing), ("voice", voice)):
        path = tmp_path / (name + ".wav")
        sf.write(path, samples, rate, subtype="FLOAT")
        source_paths[name] = path
        regions[name] = audio_region_capture(store_root=store, request_id="capture-" + name, source={
            "path": str(path), "expected_sha256": sha256_file(path), "start_frame": 0, "frames": len(samples),
            "source_origin": "user_recording"})["artifacts"]["region"]
    time_definition = {"source_context": {"schema": "pocket.time-context/v1", "context_id": "score-clock",
                                          "attribution": "Generated 120 BPM reference; no beat inference"},
                       "domain_qn": {"start": q(-16), "end": q(512)},
                       "tempo": [{"at_qn": q(-16), "bpm": q(120), "interpolation": "step"}],
                       "host_origin": {"arrangement_qn": q(0), "host_seconds": q(10)}}
    time_map = musical_time("create", store, request_id="time", definition=time_definition)["artifacts"]["time_map"]
    author = {"actor": "generated fixture", "actor_kind": "agent", "statement": "Literal generated test",
              "uncertainty": ["No human listening or native execution"]}
    definition = {"context_id": "score", "title": "Generated overlap", "attribution": author,
                  "sources": [{"clock_id": name, "region": region} for name, region in regions.items()],
                  "timelines": [{"clock_id": "set", "time_map": time_map}], "materials": [],
                  "occurrences": [
                      {"occurrence_id": "backing", "source_clock_id": "backing", "timeline_clock_id": "set",
                       "source_span_frames": [0, 768000], "timeline_span_qn": [q(0), q(32)]},
                      *[{"occurrence_id": name, "source_clock_id": "voice", "timeline_clock_id": "set",
                         "source_span_frames": span, "timeline_span_qn": [q(start), q(start + 4)]}
                        for name, span, start in (("call", [0, 96000], 4), ("response", [96000, 192000], 12),
                                                  ("unrelated-repeat", [0, 96000], 20))]],
                  "anchors": [{"anchor_id": "voice-attack", "kind": "onset", "label": "Named synthetic attack",
                               "position": {"clock_id": "voice", "space": "source_frame", "value": 24000},
                               "attribution": author}]}
    context = context_create(store, "context", definition)["artifacts"]["context"]
    score_definition = {"score_id": "overlap", "title": "Backing with linked phrase", "attribution": author,
                        "passages": [{"passage_id": name, "label": name, "attribution": author,
                                      "members": [{"occurrence_id": occurrence, "role": role} for occurrence in ids]}
                                     for name, ids, role in (("foundation", ["backing"], "backing"),
                                         ("vocal-phrase", ["call", "response"], "voice"),
                                         ("later", ["unrelated-repeat"], "voice"))], "relations": []}
    score = audio_score_create(store, "score", context, score_definition)["artifacts"]["score"]
    recipe = {"profile": "same-rate-overlay-linear/v1", "controls": [
        {"occurrence_id": o["occurrence_id"], "gain": q(1), "points": []} for o in definition["occurrences"]]}
    render_args = {"store_root": store, "request_id": "render", "score": score, "timeline_clock_id": "set",
                   "interval_qn": [q(0), q(32)], "recipe": recipe}
    audition_args = {**render_args, "request_id": "audition", "passage_id": "vocal-phrase", "anchor_id": "voice-attack",
                     "occurrence_id": "call", "delta_qn": q(1, 4), "locks": [
                         {"section": "occurrences", "object_id": "backing", "fields": ["source_span_frames", "timeline_span_qn"]},
                         {"section": "occurrences", "object_id": "unrelated-repeat", "fields": ["source_span_frames", "timeline_span_qn"]}],
                     "attribution": author, "question": "Does the named attack land later? Generated test; no listening."}
    return {"store": store, "context": context, "score": score, "definition": definition,
            "score_definition": score_definition, "author": author, "recipe": recipe,
            "source_paths": source_paths, "source_samples": {"backing": backing, "voice": voice},
            "render_args": render_args, "audition_args": audition_args}


def _audio(store, handle):
    record = read_record(handle, store)
    samples, rate = sf.read(io.BytesIO(read_bytes(record["audio"], store)), dtype="float64", always_2d=True)
    return record, samples, rate


def _oracle(f, shift=0):
    base = np.asarray(f["source_samples"]["backing"], dtype=np.float64).reshape(-1, 1).copy()
    voice = np.asarray(f["source_samples"]["voice"], dtype=np.float64).reshape(-1, 1)
    base[96000 + shift:192000 + shift] += voice[:96000]
    base[288000 + shift:384000 + shift] += voice[96000:]
    base[480000:576000] += voice[:96000]
    return base


def test_fixed_window_correction_actual_6000_frames_and_kept_lineage(tmp_path):
    f = score_fixture(tmp_path)
    result = audio_score_audition(**f["audition_args"])
    assert audio_score_audition(**f["audition_args"]) == result
    store, artifacts = f["store"], result["artifacts"]
    baseline, before, rate = _audio(store, artifacts["baseline"])
    variant, after, _ = _audio(store, artifacts["variant"])
    assert rate == 48000 and np.array_equal(before, _oracle(f)) and np.array_equal(after, _oracle(f, 6000))
    assert np.array_equal(before[480000:576000], after[480000:576000])
    comparison = practice_query(store, artifacts["comparison"])["summary"]
    assert comparison["anchor_positions"]["before"]["value"] == q(5)
    assert comparison["anchor_positions"]["after"]["value"] == q(21, 4)
    assert comparison["listening"] == "not_reviewed" and comparison["musical_verdict"] is None
    assert variant["score"] == artifacts["score"] and baseline["score"] == f["score"]
    assert baseline["interval_qn"] == variant["interval_qn"] == [q(0), q(32)]
    child = read_record(artifacts["score"], store)
    assert child["parent"] == f["score"] and child["context_edit"] == artifacts["edit"]
    preview = practice_preview(store, "preview", artifacts["variant"], "browser-pcm16-original-rate/v1")["artifacts"]["preview"]
    feedback = practice_feedback(store, "feedback", artifacts["comparison"], artifacts["variant"], [96000, 192000],
                                "generated fixture", "agent", "Synthetic numerical test, no listening", "keep", preview)["artifacts"]["feedback"]
    rows = practice_feedback_query(store, [feedback], render=artifacts["variant"], decision="keep")["items"]
    assert rows[0]["evidence_kind"] == "agent_report" and rows[0]["render"] == artifacts["variant"]
    assert practice_query(store, artifacts["variant"])["summary"]["score"] == artifacts["score"]
    assert result["coverage"]["native_execution"] is False and result["coverage"]["human_listening"] == "not_performed"
    relocated = tmp_path / "relocated"
    shutil.copytree(store, relocated)
    shutil.rmtree(store)
    for path in f["source_paths"].values():
        path.unlink()
    assert practice_feedback_query(str(relocated), [feedback], render=artifacts["variant"], decision="keep")["items"] == rows


def test_explicit_scalar_and_local_linear_points_and_partial_window(tmp_path):
    f = score_fixture(tmp_path)
    recipe = copy.deepcopy(f["recipe"])
    recipe["controls"][1].update(gain=q(1, 2), points=[{"at_frame": 0, "gain": q(0)}, {"at_frame": 95999, "gain": q(1)}])
    args = {**f["render_args"], "recipe": recipe, "interval_qn": [q(5), q(7)]}
    handle = audio_score_render(**args)["artifacts"]["render"]
    record, actual, _ = _audio(f["store"], handle)
    expected = f["source_samples"]["backing"][120000:168000].astype(np.float64)
    voice = f["source_samples"]["voice"][24000:72000].astype(np.float64)
    expected += voice * (np.arange(24000, 72000, dtype=np.float64) / 95999) * 0.5
    assert np.allclose(actual[:, 0], expected, rtol=0, atol=2e-17)
    assert record["mappings"][1]["source_span_frames"] == [24000, 72000]
    assert record["processing"]["mixing"] and not record["processing"]["normalization"]


@pytest.mark.parametrize("change,match", [
    (lambda a: a.update(delta_qn=q(0)), "nonzero"),
    (lambda a: a.update(delta_qn={"n": 2, "d": 8}), "reduced rational"),
    (lambda a: a.update(timeline_clock_id="other"), "explicit timeline"),
    (lambda a: a.update(occurrence_id="unrelated-repeat"), "belong"),
    (lambda a: a.update(anchor_id="missing"), "source onset"),
    (lambda a: a.update(interval_qn=[q(0), q(5)]), "contain.*onset"),
    (lambda a: a.update(delta_qn=q(1, 7)), "integer frames"),
    (lambda a: a["locks"].append({"section": "occurrences", "object_id": "call", "fields": ["timeline_span_qn"]}), "locked"),
])
def test_correction_refuses_ambiguous_or_unrepresentable_intent(tmp_path, change, match):
    f = score_fixture(tmp_path)
    change(f["audition_args"])
    with pytest.raises(PocketError, match=match):
        audio_score_audition(**f["audition_args"])


@pytest.mark.parametrize("change,match", [
    (lambda a: a["recipe"].update(profile="guess-tempo"), "Unsupported"),
    (lambda a: a["recipe"]["controls"].pop(), "every score occurrence"),
    (lambda a: a["recipe"]["controls"].append(a["recipe"]["controls"][0]), "unique"),
    (lambda a: a["recipe"]["controls"][0].update(gain=q(5)), "between zero and four"),
    (lambda a: a["recipe"]["controls"][0].update(gain={"n": True, "d": 1}), "bounded integer"),
    (lambda a: a["recipe"]["controls"][0].update(points=[{"at_frame": 2, "gain": q(0)}]), "explicitly cover"),
    (lambda a: a["recipe"]["controls"][0].update(points=[{"at_frame": i, "gain": q(1)} for i in range(33)]), "points"),
    (lambda a: a.update(interval_qn=[q(0), q(300)]), "120 seconds or 64 MiB"),
    (lambda a: a.update(interval_qn=[q(1, 7), q(32)]), "integer frames"),
])
def test_render_bounds_checked_before_sample_allocation(tmp_path, monkeypatch, change, match):
    f = score_fixture(tmp_path)
    change(f["render_args"])
    monkeypatch.setattr("pocket_music.audio_score_practice._mix", lambda *a: pytest.fail("allocated before refusing"))
    with pytest.raises(PocketError, match=match):
        audio_score_render(**f["render_args"])


def test_stereo_byte_limit_before_allocation(tmp_path, monkeypatch):
    f = score_fixture(tmp_path, channels=2)
    monkeypatch.setattr("pocket_music.audio_score_practice._mix", lambda *a: pytest.fail("allocated before refusing"))
    with pytest.raises(PocketError, match="64 MiB"):
        audio_score_render(**{**f["render_args"], "interval_qn": [q(0), q(240)]})


def test_exact_legacy_profile_still_refuses_overlap(tmp_path):
    f = score_fixture(tmp_path)
    with pytest.raises(PocketError, match="gaps and overlaps"):
        practice_render(f["store"], "legacy", f["context"], ["backing", "call"])


@pytest.mark.parametrize("field", ["mappings", "recipe", "audio", "audio_header", "listening"])
def test_rehashed_render_cannot_forge_samples_or_claim_listening(tmp_path, field):
    f = score_fixture(tmp_path)
    render = audio_score_render(**f["render_args"])["artifacts"]["render"]
    record = read_record(render, f["store"])
    if field == "mappings":
        record[field][0]["source_span_frames"][0] += 1
    elif field == "recipe":
        record[field]["controls"][0]["gain"] = q(2)
    elif field == "listening":
        record[field] = "approved"
    elif field == "audio_header":
        record["audio"] = put_bytes(b"not a wave", f["store"], "forged.wav", "pocket.render-audio/v1")
    else:
        data = io.BytesIO()
        sf.write(data, np.zeros((768000, 1)), 48000, format="WAV", subtype="DOUBLE")
        record[field] = put_bytes(data.getvalue(), f["store"], "forged.wav", "pocket.render-audio/v1")
    with pytest.raises(PocketError, match="evidence mismatch|differ from declared|readable declared WAV"):
        practice_query(f["store"], put_record(record, f["store"]))


@pytest.mark.parametrize("field", ["delta_qn", "anchor_positions", "baseline", "signal_ready"])
def test_rehashed_comparison_cannot_forge_ancestry_or_window(tmp_path, field):
    f = score_fixture(tmp_path)
    result = audio_score_audition(**f["audition_args"])
    record = read_record(result["artifacts"]["comparison"], f["store"])
    if field == "delta_qn":
        record[field] = q(1, 2)
    elif field == "anchor_positions":
        record[field]["after"]["value"] = q(99)
    elif field == "baseline":
        record[field] = result["artifacts"]["variant"]
    else:
        record[field] = False
    with pytest.raises(PocketError, match="ancestry|distinct|coverage"):
        practice_query(f["store"], put_record(record, f["store"]))


@pytest.mark.parametrize("field,value", [("source_span_frames", False), ("output_span_frames", 0.0)])
def test_rehashed_render_requires_exact_integer_mapping_types(tmp_path, field, value):
    f = score_fixture(tmp_path)
    render = audio_score_render(**f["render_args"])["artifacts"]["render"]
    record = read_record(render, f["store"])
    record["mappings"][0][field][0] = value
    with pytest.raises(PocketError, match="mapping or profile evidence mismatch"):
        practice_query(f["store"], put_record(record, f["store"]), section="mappings")


@pytest.mark.parametrize("field,value", [("anchor_n", 5.0), ("anchor_d", True), ("recipe_gain", 1.0)])
def test_rehashed_comparison_requires_exact_anchor_and_recipe_types(tmp_path, field, value):
    f = score_fixture(tmp_path)
    comparison = audio_score_audition(**f["audition_args"])["artifacts"]["comparison"]
    record = read_record(comparison, f["store"])
    if field == "recipe_gain":
        record["recipe"]["controls"][0]["gain"]["n"] = value
    else:
        record["anchor_positions"]["before"]["value"][field.removeprefix("anchor_")] = value
    with pytest.raises(PocketError, match="anchor evidence mismatch|common window/recipe"):
        practice_query(f["store"], put_record(record, f["store"]))


def test_same_source_repeat_is_not_rendered_twice_from_shared_membership(tmp_path):
    f = score_fixture(tmp_path)
    definition = copy.deepcopy(f["score_definition"])
    definition["passages"].append({"passage_id": "motif-reference", "label": "same call",
        "members": [{"occurrence_id": "call", "role": "motif"}], "attribution": f["author"]})
    score = audio_score_create(f["store"], "shared-score", f["context"], definition)["artifacts"]["score"]
    handle = audio_score_render(**{**f["render_args"], "score": score})["artifacts"]["render"]
    _, samples, _ = _audio(f["store"], handle)
    assert np.array_equal(samples, _oracle(f))


def test_loopback_review_labels_processed_audio_and_proves_common_window(tmp_path):
    from pocket_music.practice_review import _Review
    f = score_fixture(tmp_path)
    result = audio_score_audition(**f["audition_args"])
    review = _Review(f["store"], result["artifacts"]["comparison"], tmp_path / "review")
    try:
        summary = review.summary()
        assert summary["alignment"]["synchronized_switching"]
        assert "fixed timeline window" in summary["comparison"]["family"]
        assert all(not item["processing"]["source_exact"] for item in summary["items"])
        assert summary["report_count"] == 0 and summary["listening"] == "not_recorded_by_playback"
    finally:
        review.lock.close()


def _score_with_occurrences(f, occurrences):
    definition = copy.deepcopy(f["definition"])
    definition["occurrences"] = occurrences
    context = context_create(f["store"], "other-context", definition)["artifacts"]["context"]
    score_definition = copy.deepcopy(f["score_definition"])
    score_definition["passages"] = [{"passage_id": f"part-{i}", "label": f"part-{i}", "attribution": f["author"],
        "members": [{"occurrence_id": o["occurrence_id"], "role": "authored"} for o in occurrences[i:i + 32]]}
        for i in range(0, len(occurrences), 32)]
    score = audio_score_create(f["store"], "other-score", context, score_definition)["artifacts"]["score"]
    recipe = {"profile": "same-rate-overlay-linear/v1", "controls": [
        {"occurrence_id": o["occurrence_id"], "gain": q(1), "points": []} for o in occurrences]}
    return {**f["render_args"], "score": score, "recipe": recipe}


def test_large_score_can_render_a_small_window_but_active_bound_still_applies(tmp_path, monkeypatch):
    f = score_fixture(tmp_path)
    original = f["definition"]["occurrences"][1]
    occurrences = [{**original, "occurrence_id": f"repeat-{i}", "timeline_span_qn": [q(i * 4), q(i * 4 + 4)]}
                   for i in range(33)]
    args = _score_with_occurrences(f, occurrences)
    handle = audio_score_render(**{**args, "interval_qn": [q(0), q(2)]})["artifacts"]["render"]
    record, samples, _ = _audio(f["store"], handle)
    assert len(record["mappings"]) == 1
    assert np.array_equal(samples[:, 0], f["source_samples"]["voice"][:48000].astype(np.float64))
    monkeypatch.setattr("pocket_music.audio_score_practice._mix", lambda *a: pytest.fail("allocated before refusing"))
    with pytest.raises(PocketError, match="active audition occurrences"):
        audio_score_render(**{**args, "request_id": "too-many-active", "interval_qn": [q(0), q(132)]})


def test_concurrency_limit_before_allocation(tmp_path, monkeypatch):
    f = score_fixture(tmp_path)
    original = f["definition"]["occurrences"][1]
    args = _score_with_occurrences(f, [{**original, "occurrence_id": f"layer-{i}"} for i in range(9)])
    monkeypatch.setattr("pocket_music.audio_score_practice._mix", lambda *a: pytest.fail("allocated before refusing"))
    with pytest.raises(PocketError, match="eight simultaneous"):
        audio_score_render(**args)


def test_time_stretch_is_refused_and_float_overload_is_never_clamped(tmp_path):
    f = score_fixture(tmp_path)
    occurrences = copy.deepcopy(f["definition"]["occurrences"])
    occurrences[1]["timeline_span_qn"] = [q(4), q(9)]
    args = _score_with_occurrences(f, occurrences)
    with pytest.raises(PocketError, match="time stretch"):
        audio_score_render(**args)
    recipe = copy.deepcopy(f["recipe"])
    recipe["controls"][1].update(gain=q(4), points=[{"at_frame": 0, "gain": q(4)}, {"at_frame": 95999, "gain": q(4)}])
    handle = audio_score_render(**{**f["render_args"], "request_id": "overload", "recipe": recipe})["artifacts"]["render"]
    record, audio, _ = _audio(f["store"], handle)
    assert np.max(np.abs(audio)) > 1 and record["signal"]["sample_overload_count"] > 0
    assert not record["signal"]["usable_for_expectation"]
    with pytest.raises(PocketError, match="never clamps or normalizes"):
        practice_preview(f["store"], "overload-preview", handle, "browser-pcm16-original-rate/v1")


def test_replay_revalidates_retained_source_and_request_identity(tmp_path):
    f = score_fixture(tmp_path)
    result = audio_score_render(**f["render_args"])
    with pytest.raises(PocketError, match="different inputs"):
        audio_score_render(**{**f["render_args"], "interval_qn": [q(0), q(16)]})
    region = read_record(read_record(f["context"], f["store"])["definition"]["sources"][1]["region"], f["store"])
    from pathlib import Path
    path = Path(f["store"]) / region["crop"]["artifact_uri"]
    path.write_bytes(path.read_bytes()[:-1] + b"x")
    with pytest.raises(PocketError, match="integrity mismatch"):
        audio_score_render(**f["render_args"])
    with pytest.raises(PocketError, match="integrity mismatch"):
        practice_query(f["store"], result["artifacts"]["render"])


@pytest.mark.parametrize("rate,channels", [(44100, 1), (48000, 2)])
def test_no_implicit_sample_rate_or_channel_conversion(tmp_path, monkeypatch, rate, channels):
    f = score_fixture(tmp_path)
    path = tmp_path / "different-format.wav"
    sf.write(path, np.full((192000, channels), 0.1, dtype=np.float32), rate, subtype="FLOAT")
    region = audio_region_capture(store_root=f["store"], request_id="different-format", source={
        "path": str(path), "expected_sha256": sha256_file(path), "start_frame": 0, "frames": 192000,
        "source_origin": "user_recording"})["artifacts"]["region"]
    f["definition"]["sources"][1]["region"] = region
    args = _score_with_occurrences(f, f["definition"]["occurrences"])
    monkeypatch.setattr("pocket_music.audio_score_practice._mix", lambda *a: pytest.fail("allocated before refusing"))
    with pytest.raises(PocketError, match="matching original rate"):
        audio_score_render(**args)


def test_returned_coverage_cannot_mutate_the_provider_policy(tmp_path, monkeypatch):
    from pocket_music import audio_score_practice as provider
    expected = copy.deepcopy(provider.COVERAGE)
    monkeypatch.setattr(provider, "COVERAGE", copy.deepcopy(expected))
    f = score_fixture(tmp_path)
    result = audio_score_render(**f["render_args"])
    result["coverage"]["native_execution"] = True
    result["coverage"]["human_listening"] = "invented"
    assert provider.COVERAGE == expected
    again = audio_score_render(**{**f["render_args"], "request_id": "independent-render"})
    assert again["coverage"] == expected
