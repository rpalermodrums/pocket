"""Independent real CLI and stdio MCP parity, including invalid-request behavior."""
import asyncio
import copy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_midi_qa import literal_material

from pocket_music.capabilities import capabilities_list
from pocket_music.errors import PocketError
from pocket_music.material import material_import, material_query
from pocket_music.midi_analysis import midi_analyze
from pocket_music.midi_edit import midi_transform
from pocket_music.midi_generate import midi_generate
from pocket_music.midi_io import midi_export


def environment():
    return {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}


def cli_call(tmp_path, name, arguments, *, success=True):
    spec = tmp_path / (name + "-spec.json")
    spec.write_text(json.dumps(arguments))
    result = subprocess.run([sys.executable, "-m", "pocket_music.cli", name.replace("_", "-"), "--spec", str(spec)],
                            env=environment(), capture_output=True, text=True, timeout=20, check=False)
    if success:
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    return json.loads(result.stderr)


def brief():
    return {"role": "explicit fixture", "pitch": 48, "cell_qn": 4, "cell": [0, 1, 3],
            "length_qn": 32, "enter_qn": 8, "exit_qn": 28}


def test_qa_actual_cli_matches_direct_and_external_material_composition(tmp_path):
    args = {"source": {"kind": "material", "material": literal_material()},
            "store_root": str(tmp_path / "store"), "request_id": "external"}
    direct = material_import(**args)
    assert cli_call(tmp_path, "material_import", args) == direct
    query_args = {"material": direct["material"], "store_root": args["store_root"], "query": "events", "limit": 2}
    query = material_query(**query_args)
    assert cli_call(tmp_path, "material_query", query_args) == query
    edit_args = {"material": direct["material"], "selection": query["selection"], "store_root": args["store_root"],
                 "operations": [{"op": "transpose", "semitones": 12}], "request_id": "edit"}
    edited = midi_transform(**edit_args)
    assert cli_call(tmp_path, "midi_transform", edit_args) == edited
    analysis_args = {"material": edited["material"], "store_root": args["store_root"]}
    assert cli_call(tmp_path, "midi_analyze", analysis_args) == midi_analyze(**analysis_args)


@pytest.mark.parametrize("change", [{"pitch": True}, {"pitch": "48"}, {"preserve_velocity": True}])
def test_qa_direct_and_cli_reject_coercion_and_unknown_generation_constraints(tmp_path, change):
    args = {"brief": {**brief(), **change}, "store_root": str(tmp_path / "store"), "request_id": "bad"}
    with pytest.raises(PocketError):
        midi_generate(**args)
    assert cli_call(tmp_path, "midi_generate", args, success=False)["error"] == "PocketError"


def test_qa_capability_schemas_match_actual_public_returns(tmp_path):
    rows = {row["public_tool"]: row for row in capabilities_list(limit=50)["capabilities"] if row["public_tool"]}
    args = {"material": literal_material(), "store_root": str(tmp_path)}
    assert rows["material_query"]["output_schema"] == material_query(**args)["schema"]
    assert rows["midi_analyze"]["output_schema"] == midi_analyze(**args)["schema"]
    cursor = capabilities_list(limit=1)["next_cursor"]
    with pytest.raises(PocketError, match="Stale"):
        capabilities_list(domain="midi", cursor=cursor)


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="Optional MCP extra not installed")
def test_qa_actual_stdio_parity_external_records_and_strict_constraints(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exchange():
        params = StdioServerParameters(command=sys.executable, args=["-m", "pocket_music.mcp_server"], env=environment())
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert {"material_import", "material_query", "midi_generate", "midi_transform", "midi_export",
                    "midi_analyze", "capabilities_list"} <= tools.keys()
            assert "native_midi_write" in tools
            assert not {"native_midi_cancel", "instrument_parameter_set", "candidate_apply"} & tools.keys()
            assert tools["material_query"].annotations.readOnlyHint is True

            async def call(name, args):
                response = await session.call_tool(name, args)
                assert not response.isError, response.content
                assert response.structuredContent is None
                assert len(response.content) == 1 and len(response.content[0].text.splitlines()) == 1
                return json.loads(response.content[0].text)

            imported_args = {"source": {"kind": "material", "material": literal_material()},
                             "store_root": str(tmp_path / "store"), "request_id": "external"}
            expected = material_import(**imported_args)
            assert await call("material_import", imported_args) == expected
            query_args = {"material": expected["material"], "store_root": imported_args["store_root"]}
            query = material_query(**query_args)
            assert await call("material_query", query_args) == query
            edit_args = {**query_args, "selection": query["selection"], "request_id": "edit",
                         "operations": [{"op": "velocity", "value": 91}]}
            edited = midi_transform(**edit_args)
            assert await call("midi_transform", edit_args) == edited
            assert cli_call(tmp_path, "midi_transform", edit_args) == edited
            analyzed = {"material": edited["material"], "store_root": imported_args["store_root"]}
            assert await call("midi_analyze", analyzed) == midi_analyze(**analyzed)
            export_args = {**analyzed, "output_path": str(tmp_path / "external.mid"), "request_id": "export"}
            exported = midi_export(**export_args)
            assert await call("midi_export", export_args) == exported
            assert cli_call(tmp_path, "midi_export", export_args) == exported
            generation_args = {"brief": brief(), "store_root": imported_args["store_root"], "request_id": "generate"}
            generated = midi_generate(**generation_args)
            assert await call("midi_generate", generation_args) == generated
            assert cli_call(tmp_path, "midi_generate", generation_args) == generated
            assert await call("capabilities_list", {"domain": "native", "limit": 50}) == capabilities_list(domain="native", limit=50)
            for index, change in enumerate([{"pitch": True}, {"pitch": "48"}, {"preserve_velocity": True}]):
                response = await session.call_tool("midi_generate", {"brief": {**brief(), **change},
                    "store_root": str(tmp_path / "invalid"), "request_id": f"bad-{index}"})
                assert response.isError, f"MCP silently accepted {change}: {response.content}"
            # Failed transport validation must not publish a result or silently call the provider.
            assert not (tmp_path / "invalid" / "artifacts").exists()

    asyncio.run(asyncio.wait_for(exchange(), timeout=45))


def test_qa_cli_malformed_external_record_is_json_error(tmp_path):
    record = copy.deepcopy(literal_material())
    record["notes"][0]["onset"] = None
    from test_midi_qa import seal_literal
    args = {"source": {"kind": "material", "material": seal_literal(record)},
            "store_root": str(tmp_path / "store"), "request_id": "bad"}
    result = cli_call(tmp_path, "material_import", args, success=False)
    assert result["error"] == "PocketError"
