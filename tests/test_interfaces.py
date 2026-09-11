"""Test the installed interfaces, including a real local MCP stdio exchange."""

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from pocket_music.assets import identify_audio


def _audio(tmp_path):
    path = tmp_path / "signal.wav"
    signal = np.zeros(8000 * 4)
    signal[np.arange(1000, len(signal), 4000)] = 0.5
    sf.write(path, signal, 8000, subtype="FLOAT")
    return path


def _cli(*args):
    return subprocess.run([sys.executable, "-m", "pocket_music.cli", *map(str, args)],
                          text=True, capture_output=True, timeout=45)


def test_cli_uses_same_asset_contract_and_refuses_output_overwrite(tmp_path):
    path = _audio(tmp_path)
    result = _cli("identify", path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == identify_audio(path)
    output = tmp_path / "map.json"
    output.write_text("preserved")
    result = _cli("identify", path, "--output", output)
    assert result.returncode == 2
    assert output.read_text() == "preserved"


def test_cli_bad_source_and_nonfinite_region_are_structured_errors(tmp_path):
    missing = _cli("identify", tmp_path / "missing.wav")
    assert missing.returncode == 2 and "message" in json.loads(missing.stderr)
    result = _cli("track-map", _audio(tmp_path), "--duration", "nan")
    assert result.returncode == 2 and "message" in json.loads(result.stderr)


def test_cli_set_handle_workflow_full_export_and_stale_rejection(tmp_path):
    from test_transition_lab import als_fixture

    source_set, _ = als_fixture(tmp_path)
    overview = tmp_path / "overview.json"
    result = _cli("set-map", source_set, "--cache-dir", tmp_path / "cache", "--output", overview)
    assert result.returncode == 0, result.stderr
    summary = json.loads(overview.read_text())
    assert summary["schema"] == "pocket.set-summary/v1"
    assert "tracks" not in summary and "clips" not in summary
    region = _cli("set-region", overview, "0:00", "--duration", 3)
    assert region.returncode == 0, region.stderr
    assert len(json.loads(region.stdout)["clips"]) == 1
    bare = tmp_path / "handle.json"
    bare.write_text(json.dumps(summary["handle"]))
    found = _cli("find-clips", bare, "track:100/clip:0")
    assert found.returncode == 0, found.stderr
    assert json.loads(found.stdout)["total_matches"] == 1
    full = tmp_path / "full.json"
    exported = _cli("map-export", bare, "--output", full)
    assert exported.returncode == 0, exported.stderr
    assert json.loads(full.read_text())["schema"] == "pocket.set-map/v1"
    legacy = _cli("set-map", source_set, "--full")
    assert legacy.returncode == 0, legacy.stderr
    assert json.loads(legacy.stdout)["clips"] == json.loads(full.read_text())["clips"]
    before = full.read_bytes()
    assert _cli("map-export", bare, "--output", full).returncode == 2
    assert full.read_bytes() == before
    # Even changing an active dependency invalidates the cross-process handle.
    (tmp_path / "source.wav").touch()
    stale = _cli("set-region", overview, "0:00", "--duration", 3)
    assert stale.returncode == 2 and "message" in json.loads(stale.stderr)


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="Optional agent extra is not installed")
def test_mcp_stdio_lists_tools_and_returns_same_identity(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    path = _audio(tmp_path)

    async def exchange():
        params = StdioServerParameters(command=sys.executable, args=["-m", "pocket_music.mcp_server"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert {"identify_audio", "analyze_region", "inspect_set", "create_trial",
                        "record_feedback", "prepare_native_trial", "attach_completed_render"} <= names
                result = await session.call_tool("identify_audio", {"path": str(path)})
                assert not result.isError
                data = result.structuredContent
                if data is None:
                    data = json.loads(next(block.text for block in result.content if block.type == "text"))
                assert data == identify_audio(path)
                invalid = await session.call_tool("analyze_region", {
                    "path": str(path), "start_seconds": 0, "duration_seconds": -1,
                })
                assert invalid.isError

    asyncio.run(asyncio.wait_for(exchange(), timeout=45))


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="Optional agent extra is not installed")
def test_mcp_all_original_tools_use_discoverable_typed_inputs(tmp_path):
    """Replay a real stdio trial, not just discovery or a read-only smoke test."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from pocket_music.assets import sha256_file
    from test_transition_lab import als_fixture

    source_set, _ = als_fixture(tmp_path)
    source = tmp_path / "source.wav"

    async def exchange():
        params = StdioServerParameters(command=sys.executable, args=["-m", "pocket_music.mcp_server"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                schemas = {t.name: t.inputSchema for t in (await session.list_tools()).tools}
                trial_schema = schemas["create_trial"]
                assert trial_schema["properties"]["variants"]["type"] == "array"
                item = trial_schema["properties"]["variants"]["items"]
                definition = trial_schema["$defs"][item["$ref"].rsplit("/", 1)[1]]
                assert {"source_path", "label"} <= set(definition["required"])
                assert definition["properties"]["start_frame"]["type"] == "integer"
                assert trial_schema["properties"]["allow_duration_mismatch"]["type"] == "boolean"
                assert schemas["record_feedback"]["properties"]["start_frame"]["type"] == "integer"
                assert schemas["prepare_native_trial"]["properties"]["shift_beats"]["type"] == "number"
                assert schemas["attach_completed_render"]["properties"]["expected_frames"]["type"] == "integer"
                handle_schema = schemas["query_set_region"]
                handle_ref = handle_schema["properties"]["handle"]["$ref"].rsplit("/", 1)[1]
                assert {"cache_sha256", "set_sha256", "cache_path"} <= set(
                    handle_schema["$defs"][handle_ref]["required"])

                async def call(name, args):
                    result = await session.call_tool(name, args)
                    assert not result.isError, result.content
                    if name in {"inspect_set", "query_set_region", "find_clips", "export_set_map"}:
                        assert result.structuredContent is None
                        assert len(result.content) == 1
                        # One compact JSON record; no duplicated/pretty-printed transport copy.
                        assert len(result.content[0].text.splitlines()) == 1
                    return result.structuredContent or json.loads(next(b.text for b in result.content
                                                                       if b.type == "text"))

                identity = await call("identify_audio", {"path": str(source)})
                await call("analyze_region", {"path": str(source), "duration_seconds": 3})
                summary = await call("inspect_set", {"path": str(source_set),
                                                     "cache_dir": str(tmp_path / "cache")})
                assert summary["schema"] == "pocket.set-summary/v1" and "clips" not in summary
                handle = summary["handle"]
                region = await call("query_set_region", {"handle": handle, "start_seconds": "0:00",
                                                         "duration_seconds": 3})
                assert len(region["clips"]) == 1
                found = await call("find_clips", {"handle": handle, "query": "track:100/clip:0"})
                assert found
                exported = await call("export_set_map", {"handle": handle,
                                                          "output_path": str(tmp_path / "raw-map.json")})
                mapped = json.loads(Path(exported["path"]).read_text())
                position = await call("source_position", {"set_map": mapped, "clip_id": "track:100/clip:0",
                                                          "arrangement_beat": 2})
                await call("arrangement_position", {"set_map": mapped, "clip_id": "track:100/clip:0",
                                                      "source_seconds": position["source_seconds"]})
                trial = await call("create_trial", {
                    "output_dir": str(tmp_path / "agent-trial"),
                    "variants": [{"source_path": str(source), "label": "Generated fixture",
                                  "expected_sha256": identity["sha256"]}],
                    "start_frame": 0, "frames": 1000, "allow_duration_mismatch": False,
                })
                await call("record_feedback", {
                    "trial_dir": trial["trial_dir"], "variant_id": "v01",
                    "output_sha256": trial["variants"][0]["output"]["sha256"],
                    "start_frame": 10, "end_frame": 20, "scope": "bar_phase",
                    "note": "Generated fixture claim, not an actual listening judgment",
                })
                stored = await call("query_feedback", {
                    "trial_dir": trial["trial_dir"], "variant_id": "v01", "scope": "bar_phase",
                    "start_frame": 15, "end_frame": 21,
                })
                assert len(stored["notes"]) == 1
                assert stored["notes"][0]["start_frame"] == 10
                assert stored["musical_verdict"] is None
                native = await call("prepare_native_trial", {
                    "source_als": str(source_set), "output_dir": str(tmp_path / "agent-native"),
                    "clip_id": "track:100/clip:0", "shift_beats": 1,
                    "export_start_beat": 0, "export_length_beats": 8,
                    "expected_als_sha256": sha256_file(source_set),
                })
                checked = await call("validate_native_trial", {
                    "trial_dir": native["trial_dir"],
                    "expected_candidate_sha256": native["candidate_sha256"],
                })
                assert checked["ready_to_compare"] is False
                settings = {"rendered_track": "Main", "sample_rate": 8000, "channels": 2,
                            "normalization": False, "mono": False, "loop_render": False, "dither": "none"}
                # A generated artifact checks the provider contract. It does not
                # claim that this fixture audio was produced by native rendering.
                attached = await call("attach_completed_render", {
                    "trial_dir": native["trial_dir"], "rendered_audio": str(source),
                    "expected_candidate_sha256": native["candidate_sha256"],
                    "rendered_start_beat": 0, "rendered_length_beats": 8,
                    "expected_frames": 32000, "settings": settings, "export_completed": True,
                })
                assert attached["musical_verdict"] is None
                assert attached["signal"]["disposition"] == "usable_signal"
                assert attached["ready_to_compare"] is False  # no native observation supplied
                bad_dir = tmp_path / "bad-agent-trial"
                invalid = await session.call_tool("create_trial", {
                    "output_dir": str(bad_dir), "variants": [{"source_path": str(source)}],
                    "start_frame": 0, "frames": 100,
                })
                assert invalid.isError and not bad_dir.exists()
                invalid = await session.call_tool("prepare_native_trial", {
                    "source_als": str(source_set), "output_dir": str(bad_dir),
                    "clip_id": "track:100/clip:0", "shift_beats": 0,
                    "export_start_beat": 0, "export_length_beats": 8,
                    "expected_als_sha256": sha256_file(source_set),
                })
                assert invalid.isError and not bad_dir.exists()

    asyncio.run(asyncio.wait_for(exchange(), timeout=60))
