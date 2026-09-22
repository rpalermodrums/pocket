"""Real CLI/stdio transports with a synthetic Max bridge, never native acceptance."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys

import pytest
from test_midi_interfaces import cli_call, environment
from test_native_midi import TARGET, bridge  # noqa: F401

from pocket_music.native_midi import (
    build_native_midi_device,
    build_native_midi_writer,
    native_midi_read,
    native_midi_status,
)


@pytest.mark.parametrize("name,provider", [("build_native_midi_device", build_native_midi_device),
                                         ("build_native_midi_writer", build_native_midi_writer)])
def test_device_build_cli_parity_and_unavailable_host(tmp_path, name, provider):
    args = {"output_dir": str(tmp_path / "device"), "store_root": str(tmp_path / "store"),
            "request_id": "build-cli"}
    result = cli_call(tmp_path, name, args)
    assert result == provider(**args)
    read_args = {"store_root": args["store_root"], "request_id": "absent-cli",
                 "target": TARGET, "bridge_dir": str(tmp_path / "missing-bridge")}
    unavailable = cli_call(tmp_path, "native_midi_read", read_args)
    assert unavailable == native_midi_read(**read_args)
    assert unavailable["status"] == "unsupported"
    assert unavailable["coverage"]["native_dispatched"] is False


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="Optional MCP extra not installed")
def test_reader_cli_stdio_offline_pages_and_status_share_provider(bridge, tmp_path):  # noqa: F811
    host = bridge()
    args = {"store_root": host["store"], "request_id": "fresh-cli", "target": TARGET,
            "bridge_dir": host["folder"], "limit": 1}
    observed = cli_call(tmp_path, "native_midi_read", args)
    assert observed == native_midi_read(**args)
    assert len(host["log"].read_text().splitlines()) == 1

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exchange():
        params = StdioServerParameters(command=sys.executable, args=["-m", "pocket_music.mcp_server"],
                                      env=environment())
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert {"build_native_midi_device", "build_native_midi_writer", "native_midi_read",
                    "native_midi_status"} <= names

            async def call(name, value):
                response = await session.call_tool(name, value)
                assert not response.isError, response.content
                return json.loads(response.content[0].text)

            page_args = {"store_root": host["store"], "request_id": "historical-stdio",
                         "observation": observed["artifacts"]["observation"], "limit": 1}
            page = await call("native_midi_read", page_args)
            assert page == native_midi_read(**page_args)
            assert page == cli_call(tmp_path, "native_midi_read", page_args)
            assert page["notes"] == observed["notes"]
            assert page["notes"][0]["release_velocity"] == 37
            status_args = {"store_root": host["store"], "request_id": "status-stdio",
                           "native_request": observed["native_request"], "bridge_dir": host["folder"]}
            status = await call("native_midi_status", status_args)
            assert status == native_midi_status(**status_args)
            assert status == cli_call(tmp_path, "native_midi_status", status_args)
            assert status["journal_state"] == "complete"
            assert status["coverage"]["redispatched"] is False
            invalid = {**args, "request_id": "invalid-target", "target": {**TARGET, "track_index": True}}
            assert (await session.call_tool("native_midi_read", invalid)).isError
            assert cli_call(tmp_path, "native_midi_read", invalid, success=False)["error"] == "PocketError"

    asyncio.run(asyncio.wait_for(exchange(), timeout=30))
    assert len(host["log"].read_text().splitlines()) == 1
