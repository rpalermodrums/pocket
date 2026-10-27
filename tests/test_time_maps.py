# SPDX-License-Identifier: AGPL-3.0-only
"""Independent rational clock fixtures; no native clocks or listening are implied."""
from __future__ import annotations

import copy
import io
from fractions import Fraction

import numpy as np
import pytest
import soundfile as sf

from pocket_music.artifact_store import digest, put_bytes, put_record, read_record
from pocket_music.errors import PocketError
from pocket_music.time_maps import musical_time


def q(n, d=1):
    value = Fraction(n, d)
    return {"n": value.numerator, "d": value.denominator}


def definition():
    return {"source_context": {"schema": "pocket.time-context/v1", "context_id": "independent-clock",
                               "attribution": "Synthetic fixture authored independently of the mapper"},
            "domain_qn": {"start": q(-2), "end": q(12)},
            "host_origin": {"arrangement_qn": q(0), "host_seconds": q(10)},
            "tempo": [{"at_qn": q(-2), "bpm": q(120), "interpolation": "step"},
                      {"at_qn": q(4), "bpm": q(60), "interpolation": "step"},
                      {"at_qn": q(8), "bpm": q(180), "interpolation": "step"}]}


def create(tmp_path, data=None, request="create"):
    store = str(tmp_path / "store")
    result = musical_time("create", store, request_id=request, definition=data or definition())
    return result["artifacts"]["time_map"], store


def convert(handle, store, values, source="arrangement_qn", target="host_seconds", **kwargs):
    return musical_time("convert", store, time_map=handle,
                        positions=[{"space": source, "value": value} for value in values],
                        target_space=target, **kwargs)


def test_exact_piecewise_integration_inverse_and_negative_pickup_without_native_dependencies(tmp_path, monkeypatch):
    import builtins
    original = builtins.__import__

    def refuse_native(name, *args, **kwargs):
        if name in {"mido", "soundfile"} or any(term in name for term in ("native_candidates", "instruments", "baste")):
            raise AssertionError("Unrelated dependency requested: " + name)
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", refuse_native)
    handle, store = create(tmp_path)
    # -2→0 consumes1s;0→4 consumes2s;4→8 consumes4s;8→12 consumes4/3s.
    starts = [q(-2), q(-1), q(0), q(3), q(4), q(5), q(8), q(9), q(12)]
    expected = [q(9), q(19, 2), q(10), q(23, 2), q(12), q(13), q(16), q(49, 3), q(52, 3)]
    result = convert(handle, store, starts)
    assert [r["output"]["value"] for r in result["results"]] == expected
    inverse = convert(handle, store, expected, source="host_seconds", target="arrangement_qn")
    assert [r["output"]["value"] for r in inverse["results"]] == starts
    assert result["coverage"]["clocks"] == "declared_not_native_verified"
    assert result["coverage"]["native_execution"] is False


def test_exact_fractional_tempo_and_nonzero_arrangement_host_origin(tmp_path):
    data = definition()
    data["tempo"] = [{"at_qn": q(-2), "bpm": q(125, 2), "interpolation": "step"}]
    data["host_origin"] = {"arrangement_qn": q(3, 2), "host_seconds": q(-7, 3)}
    handle, store = create(tmp_path, data)
    # 60/(125/2) =24/25 seconds per quarter; shifted by1/7 quarter.
    result = convert(handle, store, [q(Fraction(3, 2) + Fraction(1, 7))])
    assert result["results"][0]["output"]["value"] == q(Fraction(-7, 3) + Fraction(24, 175))


def with_meter(data):
    result = copy.deepcopy(data)
    result.update(bar_one_qn=q(0), meter=[
        {"at_qn": q(-2), "numerator": 4, "denominator": 4, "bar_number": 0, "partial_previous_bar": False},
        {"at_qn": q(0), "numerator": 4, "denominator": 4, "bar_number": 1, "partial_previous_bar": True},
        {"at_qn": q(7), "numerator": 3, "denominator": 4, "bar_number": 3, "partial_previous_bar": True}])
    return result


def test_partial_meter_bar_boundary_and_pickup_are_explicit_without_reinterpreting_prior_events(tmp_path):
    handle, store = create(tmp_path, with_meter(definition()))
    result = convert(handle, store, [q(-1), q(0), q(6), q(7), q(10), q(12)], target="bar_display")
    rows = [r["output"] for r in result["results"]]
    assert [(r["bar_number"], r["offset_qn"], r["partial_bar"]) for r in rows] == [
        (0, q(1), True), (1, q(0), False), (2, q(2), True),
        (3, q(0), False), (4, q(0), False), (4, q(2), False)]
    assert rows[2]["bar_start_qn"] == q(4) and rows[2]["bar_end_qn"] == q(7)
    assert rows[-1]["bar_end_qn"] == q(13) and rows[-1]["domain_clipped_end_qn"] == q(12)
    assert rows[-1]["boundary_at_domain_end"] is True


def test_seven_eighth_cycle_is_separate_annotation_under_steady_four_four(tmp_path):
    data = definition()
    data["domain_qn"]["start"] = q(0)
    data["tempo"][0]["at_qn"] = q(0)
    data.update(bar_one_qn=q(0), meter=[{"at_qn": q(0), "numerator": 4, "denominator": 4,
                                      "bar_number": 1, "partial_previous_bar": False}],
                cycles=[{"cycle_id": "seven-eighths", "origin_qn": q(0), "length_qn": q(7, 2),
                         "annotation": "Seven eighth-note cell; hearing and grouping remain musician judgment"}])
    handle, store = create(tmp_path, data)
    outputs = convert(handle, store, [q(7, 2), q(7)], target="bar_display")["results"]
    assert [x["output"]["meter"] for x in outputs] == [[4, 4], [4, 4]]
    assert [x["output"]["bar_number"] for x in outputs] == [1, 2]
    stored = read_record(handle, store)
    assert stored["definition"]["cycles"] == data["cycles"]
    assert stored["coverage"]["cycles"] == "annotation_only"


def render_definition(tmp_path):
    store = str(tmp_path / "store")
    buffer = io.BytesIO()
    sf.write(buffer, np.zeros(8000 * 20), 8000, format="WAV", subtype="FLOAT")
    audio = put_bytes(buffer.getvalue(), store, "render.wav", "pocket.render-audio/v1")
    data = definition()
    # Frame8000 corresponds to host second10. This render starts at second9.
    data["render_origin"] = {"audio": audio, "host_seconds": q(10), "frame": 8000, "sample_rate": 8000}
    return data


def test_render_identity_origin_rate_end_bounds_and_exact_reverse(tmp_path):
    data = render_definition(tmp_path)
    handle, store = create(tmp_path, data)
    mapped = convert(handle, store, [q(-2), q(0), q(4), q(8)], target="render_frame")
    assert [r["output"]["value"] for r in mapped["results"]] == [0, 8000, 24000, 56000]
    assert all(r["output"]["audio_sha256"] == data["render_origin"]["audio"]["sha256"] for r in mapped["results"])
    assert all(r["quantization"]["error_frames"] == q(0) for r in mapped["results"])
    inverse = convert(handle, store, [0, 8000, 24000, 56000], source="render_frame", target="arrangement_qn")
    assert [r["output"]["value"] for r in inverse["results"]] == [q(-2), q(0), q(4), q(8)]
    with pytest.raises(PocketError, match="bounded integer"):
        convert(handle, store, [160001], source="render_frame")


def test_frame_rounding_requires_chosen_policy_and_reports_signed_exact_error(tmp_path):
    handle, store = create(tmp_path, render_definition(tmp_path))
    at = q(1, 8000)  # At120BPM this is exactly half a render frame.
    with pytest.raises(PocketError, match="explicit nearest"):
        convert(handle, store, [at], target="render_frame")
    policy = {"policy": "nearest_half_away_from_zero", "max_error_frames": q(1, 2)}
    row = convert(handle, store, [at], target="render_frame", quantization=policy)["results"][0]
    assert row["output"]["value"] == 8001
    assert row["quantization"] == {"policy": "nearest_half_away_from_zero", "exact_frame": q(16001, 2),
                                   "error_frames": q(1, 2), "error_seconds": q(1, 16000)}
    with pytest.raises(PocketError, match="exceeds"):
        convert(handle, store, [at], target="render_frame",
                quantization={**policy, "max_error_frames": q(49, 100)})


@pytest.mark.parametrize("change", ["rate", "frame", "family", "bad_audio", "bool_rate"])
def test_invalid_render_metadata_cannot_be_asserted_by_caller(tmp_path, change):
    data = render_definition(tmp_path)
    render = data["render_origin"]
    if change == "rate":
        render["sample_rate"] = 48000
    elif change == "frame":
        render["frame"] = 160001
    elif change == "family":
        render["audio"]["artifact_schema"] = "pocket.binary-asset/v1"
    elif change == "bad_audio":
        render["audio"] = put_bytes(b"not audio", str(tmp_path / "store"), "bad.wav", "pocket.render-audio/v1")
    else:
        render["sample_rate"] = True
    with pytest.raises(PocketError):
        create(tmp_path, data)


@pytest.mark.parametrize("path,value", [
    (("tempo", 0, "bpm"), q(0)), (("tempo", 0, "bpm"), q(-120)),
    (("tempo", 0, "bpm"), {"n": True, "d": 1}), (("tempo", 0, "bpm"), {"n": 240, "d": 2}),
    (("tempo", 0, "bpm"), {"n": 120, "d": 0}), (("tempo", 0, "interpolation"), "linear"),
    (("tempo", 0, "interpolation"), "bezier"), (("tempo", 1, "at_qn"), q(-2)),
    (("tempo", 2, "at_qn"), q(12)), (("tempo", 0, "at_qn"), q(-1)),
    (("host_origin", "arrangement_qn"), q(13)), (("host_origin", "host_seconds"), float("nan")),
    (("domain_qn", "end"), q(-2)), (("source_context", "attribution"), ""),
    (("source_context", "schema"), "pocket.context/v1")])
def test_invalid_or_unsupported_clock_definition_refuses_before_publication(tmp_path, path, value):
    data = definition()
    parent = data
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    with pytest.raises(PocketError):
        create(tmp_path, data)
    assert not (tmp_path / "store/artifacts").exists()


@pytest.mark.parametrize("mutation", ["partial_missing", "partial_false", "bar_gap", "no_anchor", "wrong_anchor",
                                       "denominator", "numerator", "duplicate", "partial_bool", "unknown_warp"])
def test_meter_and_unsupported_mapping_require_explicit_consistent_contract(tmp_path, mutation):
    data = with_meter(definition())
    if mutation == "partial_missing":
        del data["meter"][1]["partial_previous_bar"]
    elif mutation == "partial_false":
        data["meter"][1]["partial_previous_bar"] = False
    elif mutation == "bar_gap":
        data["meter"][2]["bar_number"] = 4
    elif mutation == "no_anchor":
        del data["bar_one_qn"]
    elif mutation == "wrong_anchor":
        data["bar_one_qn"] = q(1)
    elif mutation == "denominator":
        data["meter"][1]["denominator"] = 3
    elif mutation == "numerator":
        data["meter"][1]["numerator"] = True
    elif mutation == "duplicate":
        data["meter"][2]["at_qn"] = q(0)
    elif mutation == "partial_bool":
        data["meter"][1]["partial_previous_bar"] = 1
    else:
        data["warp"] = []
    with pytest.raises(PocketError):
        create(tmp_path, data)


def test_bounded_query_same_map_identity_and_no_implicit_extrapolation(tmp_path):
    handle, store = create(tmp_path)
    first = musical_time("query", store, time_map=handle, section="tempo", limit=1)
    second = musical_time("query", store, time_map=handle, section="tempo", offset=first["next_offset"], limit=1)
    assert first["rows"] == definition()["tempo"][:1] and second["rows"] == definition()["tempo"][1:2]
    assert first["total"] == 3 and first["omitted"] == 2 and first["artifacts"] == second["artifacts"]
    assert first["source_context_sha256"] == digest(definition()["source_context"])
    for value in [q(-3), q(13)]:
        with pytest.raises(PocketError, match="outside"):
            convert(handle, store, [value])
    for value in [q(8), q(18)]:
        with pytest.raises(PocketError, match="outside"):
            convert(handle, store, [value], source="host_seconds")


@pytest.mark.parametrize("kwargs", [{"limit": True}, {"offset": -1}, {"limit": 257}, {"section": "warp"},
                                     {"section": []}, {"positions": []}])
def test_query_refuses_bad_bounds_and_unused_conversion_fields(tmp_path, kwargs):
    handle, store = create(tmp_path)
    with pytest.raises(PocketError):
        musical_time("query", store, time_map=handle, **kwargs)


@pytest.mark.parametrize("position", [{"space": "source_frame", "value": 0},
                                       {"space": "clip_qn", "value": q(1)},
                                       {"space": "arrangement_qn", "value": True},
                                       {"space": "host_seconds", "value": 10.0},
                                       {"space": "arrangement_qn", "value": {"n": 2, "d": 2}},
                                       {"space": "arrangement_qn", "value": q(0), "groove": True}])
def test_conversion_rejects_implicit_spaces_and_malformed_values(tmp_path, position):
    handle, store = create(tmp_path)
    with pytest.raises(PocketError):
        musical_time("convert", store, time_map=handle, positions=[position], target_space="host_seconds")


def test_changed_map_is_new_identity_and_original_retry_stays_exact(tmp_path):
    source = definition()
    frozen = copy.deepcopy(source)
    handle, store = create(tmp_path, source)
    assert source == frozen
    assert create(tmp_path, source)[0] == handle
    changed = copy.deepcopy(source)
    changed["tempo"][1]["bpm"] = q(90)
    with pytest.raises(PocketError, match="idempotency_conflict"):
        create(tmp_path, changed)
    other, _ = create(tmp_path, changed, "changed-map")
    assert other["sha256"] != handle["sha256"]
    assert convert(handle, store, [q(5)])["results"][0]["output"]["value"] == q(13)
    assert convert(other, store, [q(5)])["results"][0]["output"]["value"] == q(38, 3)


@pytest.mark.parametrize("change", ["context_hash", "coverage", "context_family", "artifact_bytes"])
def test_external_map_or_reference_tamper_refuses(tmp_path, change):
    handle, store = create(tmp_path)
    record = read_record(handle, store)
    if change == "context_hash":
        record["source_context_sha256"] = "0" * 64
    elif change == "coverage":
        record["coverage"]["clocks"] = "native_verified"
    elif change == "context_family":
        record["definition"]["source_context"] = put_record({"schema": "pocket.unrelated/v1"}, store)
        record["source_context_sha256"] = digest(record["definition"]["source_context"])
    else:
        (tmp_path / "store" / handle["artifact_uri"]).write_bytes(b"changed map")
    altered = handle if change == "artifact_bytes" else put_record(record, store)
    with pytest.raises(PocketError):
        musical_time("query", store, time_map=altered)


def test_optional_context_reference_integrity_rechecked_on_query_and_retry(tmp_path):
    data = definition()
    store = str(tmp_path / "store")
    source = put_bytes(b"declared context source", store, "source.bin", "pocket.binary-asset/v1")
    data["source_context"]["source"] = source
    handle, _ = create(tmp_path, data)
    (tmp_path / "store" / source["artifact_uri"]).write_bytes(b"changed source")
    with pytest.raises(PocketError, match="integrity"):
        musical_time("query", store, time_map=handle)
    with pytest.raises(PocketError, match="integrity"):
        create(tmp_path, data)


def test_optional_meter_render_and_response_bounds(tmp_path):
    handle, store = create(tmp_path)
    for target in ("render_frame", "bar_display"):
        with pytest.raises(PocketError, match="requires"):
            convert(handle, store, [q(0)], target=target)
    with pytest.raises(PocketError, match="list bound"):
        convert(handle, store, [q(0)] * 513)
    with pytest.raises(PocketError, match="only to render"):
        convert(handle, store, [q(0)], quantization={"policy": "nearest_half_away_from_zero", "max_error_frames": q(0)})


def test_pydantic_transport_schemas_preserve_rational_values_and_forbid_unknowns(tmp_path):
    from pydantic import TypeAdapter, ValidationError

    from pocket_music.time_types import TimeMapDefinition, TimePosition
    assert TypeAdapter(TimeMapDefinition).validate_python(definition()) == definition()
    for value in [{"space": "arrangement_qn", "value": {"n": True, "d": 1}},
                  {"space": "host_seconds", "value": {"n": 1, "d": 1}, "unknown": True}]:
        with pytest.raises(ValidationError):
            TypeAdapter(TimePosition).validate_python(value)


def test_strict_handles_and_arithmetic_bounds_fail_with_domain_error(tmp_path):
    handle, store = create(tmp_path)
    with pytest.raises(PocketError, match="fields"):
        musical_time("query", store, time_map={**handle, "current": True})
    data = definition()
    # Inputs are individually bounded; their accumulated denominator is not.
    data["tempo"] = [{"at_qn": q(-2), "bpm": q(9007199254740881), "interpolation": "step"},
                     {"at_qn": q(4), "bpm": q(9007199254740847), "interpolation": "step"}]
    with pytest.raises(PocketError, match="bounded integer"):
        create(tmp_path, data, "arithmetic-bound")


def test_negative_pickup_bar_anchor_can_include_bar_numbers_below_zero(tmp_path):
    data = definition()
    data["domain_qn"]["start"] = q(-6)
    data["tempo"][0]["at_qn"] = q(-6)
    data.update(bar_one_qn=q(0), meter=[
        {"at_qn": q(-6), "numerator": 4, "denominator": 4, "bar_number": -1, "partial_previous_bar": False},
        {"at_qn": q(0), "numerator": 4, "denominator": 4, "bar_number": 1, "partial_previous_bar": True}])
    handle, store = create(tmp_path, data)
    rows = convert(handle, store, [q(-5), q(-1), q(0)], target="bar_display")["results"]
    assert [r["output"]["bar_number"] for r in rows] == [-1, 0, 1]


def test_non_quarter_meter_denominator_beats_never_change_quarter_note_time(tmp_path):
    data = definition()
    data["domain_qn"]["start"] = q(0)
    data["tempo"][0]["at_qn"] = q(0)
    data.update(bar_one_qn=q(0), meter=[{"at_qn": q(0), "numerator": 6, "denominator": 8,
                                      "bar_number": 1, "partial_previous_bar": False}])
    handle, store = create(tmp_path, data)
    row = convert(handle, store, [q(1, 2)], target="bar_display")["results"][0]["output"]
    assert row["denominator_beat"] == q(2) and row["offset_qn"] == q(1, 2)
    assert convert(handle, store, [q(1, 2)])["results"][0]["output"]["value"] == q(41, 4)
