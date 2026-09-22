"""Independent declared-clock QA; no native clock or audio listening claims."""
import asyncio
import builtins
import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from pocket_music.artifact_store import put_bytes, put_record, read_record
from pocket_music.errors import PocketError
from pocket_music.time_maps import musical_time


def q(n, d=1):
    return {"n": n, "d": d}


def declared():
    return {"source_context": {"schema": "pocket.time-context/v1", "context_id": "qa-declared-clock",
                               "attribution": "Independent literal rational expectations; no host observation"},
            "domain_qn": {"start": q(-1), "end": q(9)},
            "tempo": [{"at_qn": q(-1), "bpm": q(90), "interpolation": "step"},
                      {"at_qn": q(3), "bpm": q(144), "interpolation": "step"},
                      {"at_qn": q(7), "bpm": q(75), "interpolation": "step"}],
            "host_origin": {"arrangement_qn": q(2), "host_seconds": q(5, 7)}}


def create(tmp_path, definition=None, request="clock"):
    return musical_time(operation="create", store_root=str(tmp_path), request_id=request,
                        definition=definition or declared())["artifacts"]["time_map"]


def converted(tmp_path, handle, points, target="host_seconds", source="arrangement_qn", **kwargs):
    return musical_time(operation="convert", store_root=str(tmp_path), time_map=handle,
                        positions=[{"space": source, "value": point} for point in points],
                        target_space=target, **kwargs)


def test_qa_literal_step_tempo_bidirectional_with_negative_pickup_no_optional_dependencies(tmp_path, monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name in {"mido", "soundfile"} or any(x in name for x in ("native_candidates", "instruments", "baste")):
            raise AssertionError("Unrelated dependency requested")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    handle = create(tmp_path)
    points = [q(-1), q(2), q(3), q(5), q(7), q(9)]
    expected = [q(-9, 7), q(5, 7), q(29, 21), q(31, 14), q(64, 21), q(488, 105)]
    result = converted(tmp_path, handle, points)
    assert [row["output"]["value"] for row in result["results"]] == expected
    back = converted(tmp_path, handle, expected, target="arrangement_qn", source="host_seconds")
    assert [row["output"]["value"] for row in back["results"]] == points
    assert result["coverage"]["native_execution"] is False
    with pytest.raises(PocketError, match="outside"):
        converted(tmp_path, handle, [q(-2)])


def test_qa_partial_bars_and_local_cycle_do_not_invent_meter(tmp_path):
    definition = declared()
    definition.update(bar_one_qn=q(0), meter=[
        {"at_qn": q(-1), "numerator": 4, "denominator": 4, "bar_number": 0, "partial_previous_bar": False},
        {"at_qn": q(0), "numerator": 4, "denominator": 4, "bar_number": 1, "partial_previous_bar": True},
        {"at_qn": q(6), "numerator": 3, "denominator": 4, "bar_number": 3, "partial_previous_bar": True}],
        cycles=[{"cycle_id": "seven-eighths", "origin_qn": q(0), "length_qn": q(7, 2),
                 "annotation": "A local cell, no host meter or downbeat claim"}])
    before = copy.deepcopy(definition)
    handle = create(tmp_path, definition)
    result = converted(tmp_path, handle, [q(-1), q(0), q(7, 2), q(5), q(6)], target="bar_display")
    rows = [r["output"] for r in result["results"]]
    assert [(r["bar_number"], r["offset_qn"], r["partial_bar"]) for r in rows] == [
        (0, q(0), True), (1, q(0), False), (1, q(7, 2), False), (2, q(1), True), (3, q(0), False)]
    assert rows[2]["meter"] == [4, 4] and rows[2]["denominator_beat"] == q(9, 2)
    assert rows[3]["bar_end_qn"] == q(6)
    assert definition == before
    assert read_record(handle, str(tmp_path))["definition"]["cycles"] == before["cycles"]


def render_definition(tmp_path):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as file:
        file.setparams((1, 2, 8000, 8000, "NONE", "not compressed"))
        file.writeframes(b"\x00\x00" * 8000)
    audio = put_bytes(buffer.getvalue(), str(tmp_path), "qa.wav", "pocket.render-audio/v1")
    definition = declared()
    definition.update(domain_qn={"start": q(0), "end": q(4)},
        tempo=[{"at_qn": q(0), "bpm": q(120), "interpolation": "step"}],
        host_origin={"arrangement_qn": q(0), "host_seconds": q(0)},
        render_origin={"audio": audio, "sample_rate": 8000, "frame": 0, "host_seconds": q(0)})
    return definition


def test_qa_frame_conversion_exact_half_rounding_bounds_and_audio_tamper(tmp_path):
    definition = render_definition(tmp_path)
    handle = create(tmp_path, definition)
    rows = converted(tmp_path, handle, [q(0), q(2)], target="render_frame")["results"]
    assert [row["output"]["value"] for row in rows] == [0, 8000]
    with pytest.raises(PocketError, match="explicit.*quantization"):
        converted(tmp_path, handle, [q(1, 8000)], target="render_frame")
    result = converted(tmp_path, handle, [q(1, 8000)], target="render_frame",
        quantization={"policy": "nearest_half_away_from_zero", "max_error_frames": q(1, 2)})
    row = result["results"][0]
    assert row["output"]["value"] == 1
    assert row["quantization"]["exact_frame"] == q(1, 2)
    assert row["quantization"]["error_frames"] == q(1, 2)
    assert row["quantization"]["error_seconds"] == q(1, 16000)
    with pytest.raises(PocketError, match="exceeds"):
        converted(tmp_path, handle, [q(1, 8000)], target="render_frame",
            quantization={"policy": "nearest_half_away_from_zero", "max_error_frames": q(1, 3)})
    with pytest.raises(PocketError, match="bounds"):
        converted(tmp_path, handle, [q(3)], target="render_frame")
    (tmp_path / definition["render_origin"]["audio"]["artifact_uri"]).write_bytes(b"tampered audio")
    with pytest.raises(PocketError, match="integrity|readable audio"):
        converted(tmp_path, handle, [q(0)])


@pytest.mark.parametrize("interpolation", ["linear", "exponential", "bezier"])
def test_qa_unsupported_ramps_refuse_before_publication(tmp_path, interpolation):
    definition = declared()
    definition["tempo"][1]["interpolation"] = interpolation
    with pytest.raises(PocketError, match="step"):
        create(tmp_path, definition)
    assert not (tmp_path / "artifacts").exists()


def test_qa_source_context_tamper_and_forged_coverage_refuse(tmp_path):
    definition = declared()
    source = put_bytes(b"declared clock source", str(tmp_path), "clock.bin", "pocket.binary-asset/v1")
    definition["source_context"]["source"] = source
    handle = create(tmp_path, definition)
    original = read_record(handle, str(tmp_path))
    original["coverage"]["native_execution"] = True
    forged = put_record(original, str(tmp_path))
    with pytest.raises(PocketError, match="coverage"):
        musical_time(operation="query", store_root=str(tmp_path), time_map=forged)
    (tmp_path / source["artifact_uri"]).write_bytes(b"changed")
    with pytest.raises(PocketError, match="integrity"):
        musical_time(operation="query", store_root=str(tmp_path), time_map=handle)


def test_qa_time_actual_cli_and_bounded_pagination(tmp_path):
    args = {"operation": "create", "store_root": str(tmp_path), "request_id": "transport", "definition": declared()}
    direct = musical_time(**args)
    spec = tmp_path / "create.json"
    spec.write_text(json.dumps(args))
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    process = subprocess.run([sys.executable, "-m", "pocket_music.cli", "musical-time", "--spec", str(spec)],
                             env=env, capture_output=True, text=True, check=False, timeout=20)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == direct
    first = musical_time(operation="query", store_root=str(tmp_path), time_map=direct["artifacts"]["time_map"],
                         section="tempo", limit=2)
    assert len(first["rows"]) == 2 and first["next_offset"] == 2
    second = musical_time(operation="query", store_root=str(tmp_path), time_map=direct["artifacts"]["time_map"],
                          section="tempo", limit=2, offset=2)
    assert len(second["rows"]) == 1 and second["next_offset"] is None
    with pytest.raises(PocketError, match="list bound"):
        converted(tmp_path, direct["artifacts"]["time_map"], [q(0)] * 513)


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="Optional MCP extra not installed")
def test_qa_time_actual_stdio_matches_direct_and_rejects_coercion(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exchange():
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        params = StdioServerParameters(command=sys.executable, args=["-m", "pocket_music.mcp_server"], env=env)
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            args = {"operation": "create", "store_root": str(tmp_path), "request_id": "stdio", "definition": declared()}
            direct = musical_time(**args)
            result = await session.call_tool("musical_time", args)
            assert not result.isError
            assert json.loads(result.content[0].text) == direct
            query = {"operation": "convert", "store_root": str(tmp_path), "time_map": direct["artifacts"]["time_map"],
                     "positions": [{"space": "arrangement_qn", "value": q(5)}], "target_space": "host_seconds"}
            result = await session.call_tool("musical_time", query)
            assert not result.isError
            assert json.loads(result.content[0].text) == musical_time(**query)
            invalid = copy.deepcopy(args)
            invalid["definition"]["tempo"][0]["bpm"]["n"] = True
            assert (await session.call_tool("musical_time", invalid)).isError

    asyncio.run(exchange())
