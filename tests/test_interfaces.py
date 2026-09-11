"""Test the installed interfaces, including a real local MCP stdio exchange."""

import asyncio
import importlib.util
import json
import subprocess
import sys

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
