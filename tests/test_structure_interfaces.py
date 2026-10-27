# SPDX-License-Identifier: AGPL-3.0-only
"""Public phrase retrieval and composition, using independently authored material."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pocket_music.artifact_store import read_record
from pocket_music.errors import PocketError
from pocket_music.material import material_import, material_query
from pocket_music.material_structure import material_structure
from pocket_music.midi_edit import midi_transform

ROOT = Path(__file__).resolve().parents[1]


def arguments(tmp_path, request="structure-interfaces"):
    spec = json.loads((ROOT / "examples/midi-workflow/structure-create.json").read_text())
    return {**spec, "store_root": str(tmp_path), "request_id": request}


def cli(args, tmp_path):
    spec = tmp_path / "structure-input.json"
    spec.write_text(json.dumps(args))
    result = subprocess.run(
        [sys.executable, "-m", "pocket_music.cli", "material-structure", "--spec", str(spec)],
        capture_output=True, text=True, check=False, timeout=20,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    return result


def test_structure_external_material_cli_replay_and_member_pages(tmp_path):
    args = arguments(tmp_path)
    direct = material_structure(**args)
    command = cli(args, tmp_path)
    assert command.returncode == 0, command.stderr
    assert json.loads(command.stdout) == direct
    query = {"operation": "query", "store_root": str(tmp_path),
             "structure": direct["artifacts"]["structure"], "section": "members",
             "node_ids": ["entrance"], "limit": 1}
    first = material_structure(**query)
    second = material_structure(**query, cursor=first["next_cursor"])
    assert [first["rows"][0]["note_id"], second["rows"][0]["note_id"]] == [
        "note:external-0", "note:external-1"]
    assert second["next_cursor"] is None
    command = cli(query, tmp_path)
    assert command.returncode == 0 and json.loads(command.stdout) == first


def test_structure_members_compose_with_public_selected_edit_and_handle_substitution(tmp_path):
    args = arguments(tmp_path)
    original_record = args["definition"]["materials"][0]["material"]
    original = copy.deepcopy(original_record)
    result = material_structure(**args)
    page = material_structure(operation="query", store_root=str(tmp_path),
                              structure=result["artifacts"]["structure"],
                              section="members", node_ids=["entrance"])
    imported = material_import({"kind": "material", "material": original_record}, str(tmp_path), "import")
    selection = material_query(imported["material"], str(tmp_path),
                               selection={"note_ids": [r["note_id"] for r in page["rows"]]})["selection"]
    edit = midi_transform(imported["material"], selection, [{"op": "velocity", "value": 88}],
                          str(tmp_path), "phrase-revision", locks={"outside_selection": "all",
                          "selected_fields": ["pitch", "onset", "duration_qn", "release_velocity"]})
    child = read_record(edit["material"], str(tmp_path))
    assert [n["velocity"]["value"] for n in child["notes"]] == [88, 88, 80]
    assert child["notes"][2] == original["notes"][2]
    for before, after in zip(original["notes"], child["notes"]):
        assert all(before[key] == after[key] for key in ("pitch", "onset", "duration_qn", "release_velocity"))
    assert original_record == original
    handles = copy.deepcopy(args)
    handles["request_id"] = "handle-equivalent"
    handles["definition"]["materials"][0]["material"] = imported["material"]
    assert material_structure(**handles)["artifacts"] == result["artifacts"]
    stale = copy.deepcopy(args)
    stale["request_id"] = "stale-child-binding"
    stale["definition"]["materials"][0]["material"] = edit["material"]
    with pytest.raises(PocketError):
        material_structure(**stale)


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="Optional MCP extra absent")
def test_structure_actual_stdio_schema_parity_and_strict_rejection(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exchange():
        parameters = StdioServerParameters(command=sys.executable, args=["-m", "pocket_music.mcp_server"],
                                            env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            catalog = await session.list_tools()
            descriptor = next(t for t in catalog.tools if t.name == "material_structure")
            assert descriptor.inputSchema["additionalProperties"] is False
            args = arguments(tmp_path)
            direct = material_structure(**args)
            result = await session.call_tool("material_structure", args)
            assert not result.isError and json.loads(result.content[0].text) == direct
            query = {"operation": "query", "store_root": str(tmp_path),
                     "structure": direct["artifacts"]["structure"], "section": "nodes", "limit": 1}
            result = await session.call_tool("material_structure", query)
            assert not result.isError and json.loads(result.content[0].text) == material_structure(**query)
            for change in ("boolean-rational", "extra-attribution"):
                invalid = copy.deepcopy(args)
                invalid["request_id"] = change
                if change == "boolean-rational":
                    invalid["definition"]["nodes"][0]["span_qn"]["start"]["n"] = False
                else:
                    invalid["definition"]["attribution"]["native_verified"] = True
                with pytest.raises(PocketError):
                    material_structure(**invalid)
                assert (await session.call_tool("material_structure", invalid)).isError
                assert cli(invalid, tmp_path).returncode != 0

    asyncio.run(exchange())
