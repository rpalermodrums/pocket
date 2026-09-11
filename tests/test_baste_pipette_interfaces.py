"""Real CLI and stdio exchanges, with generated evidence and a simulated device."""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import test_baste
from test_pipette import promotion_args

from pocket_music.pipette import validate_promotion
from pocket_music.thread_queries import find_clips

bridge = test_baste.bridge


def environment(tmp_path):
    # Simulate process presence only. All observation requests go to the isolated
    # fixture descriptor, never to a running Ableton instance on this computer.
    folder = tmp_path / "bin"
    folder.mkdir()
    ps = folder / "ps"
    ps.write_text("#!/bin/sh\nprintf 'Live\\n'\n")
    ps.chmod(0o700)
    return {**os.environ, "PATH": str(folder) + os.pathsep + os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}


def test_cli_promotes_validates_builds_and_reads_fresh(tmp_path, bridge):
    env = environment(tmp_path)

    def call(*args):
        result = subprocess.run([sys.executable, "-m", "pocket_music.cli", *map(str, args)],
                                env=env, capture_output=True, text=True, timeout=30, check=False)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    args, _ = promotion_args(tmp_path)
    destination = args.pop("output_dir")
    spec = tmp_path / "promote-spec.json"
    spec.write_text(json.dumps(args))
    result = call("pipette", "promote", "--spec", spec, "--output", destination)
    check = {"promotion_dir": destination, "expected_lineage_sha256": result["lineage_sha256"]}
    spec.write_text(json.dumps(check))
    validated = call("pipette", "validate", "--spec", spec)
    assert validated["child_als_sha256"] == validate_promotion(**check)["child_als_sha256"]
    assert find_clips(validated["thread"]["handle"])["total_matches"] == 2
    built = call("baste-device", "--output", tmp_path / "device")
    assert Path(built["device_path"]).is_file() and built["loaded_in_live"] is False
    first = call("baste", "--bridge-dir", bridge)
    second = call("baste", "--bridge-dir", bridge)
    assert first["disposition"] == second["disposition"] == "ok"
    assert first["request_id"] != second["request_id"]


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="Optional agent extra not installed")
def test_mcp_typed_inputs_and_complete_promotion_exchange(tmp_path, bridge):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    args, _ = promotion_args(tmp_path)
    env = environment(tmp_path)

    async def exchange():
        params = StdioServerParameters(command=sys.executable, args=["-m", "pocket_music.mcp_server"], env=env)
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name: t for t in (await session.list_tools()).tools}
            assert {"baste", "baste_build_device", "pipette", "pipette_validate"} <= tools.keys()
            assert tools["baste"].annotations.readOnlyHint is True
            assert tools["baste"].annotations.idempotentHint is False
            assert tools["pipette"].annotations.readOnlyHint is False
            schema = tools["pipette"].inputSchema
            ref = schema["properties"]["decision"]["$ref"].split("/")[-1]
            assert set(schema["$defs"][ref]["required"]) == {"action", "actor", "actor_kind", "reason"}

            async def call(name, arguments):
                response = await session.call_tool(name, arguments)
                assert not response.isError, response.content
                assert response.structuredContent is None
                assert len(response.content) == 1 and len(response.content[0].text.splitlines()) == 1
                return json.loads(response.content[0].text)

            invalid = await session.call_tool("pipette", {k: v for k, v in args.items() if k != "decision"})
            assert invalid.isError and not Path(args["output_dir"]).exists()
            kept = await call("pipette", args)
            checked = await call("pipette_validate", {
                "promotion_dir": kept["promotion_dir"], "expected_lineage_sha256": kept["lineage_sha256"]})
            found = await call("thread_find_clips", {"handle": checked["thread"]["handle"], "query": ""})
            assert found["total_matches"] == 2
            device = await call("baste_build_device", {"output_dir": str(tmp_path / "device")})
            assert Path(device["device_path"]).is_file()
            first = await call("baste", {"bridge_dir": str(bridge)})
            second = await call("baste", {"bridge_dir": str(bridge)})
            assert first["disposition"] == second["disposition"] == "ok"
            assert first["request_id"] != second["request_id"]
            invalid = await session.call_tool("baste", {"timeout_seconds": -1})
            assert invalid.isError

    asyncio.run(asyncio.wait_for(exchange(), timeout=45))
