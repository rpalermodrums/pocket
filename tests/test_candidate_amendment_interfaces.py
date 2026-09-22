"""Actual transports for an explicit material amendment; no native acceptance claim."""
from __future__ import annotations

import asyncio
import copy
import gzip
import importlib.util
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from test_candidate_interfaces import cli_call
from test_native_candidates import prepare, seal_args, snapshot

from pocket_music.artifact_store import read_record
from pocket_music.assets import sha256_file
from pocket_music.material import material_query
from pocket_music.midi_edit import midi_transform
from pocket_music.native_candidates import candidate_seal, load_candidate_record


def amended_fixture(tmp_path):
    prepared, preparation_args, parent = prepare(tmp_path, "with_material")
    store = preparation_args["store_root"]
    original = preparation_args["material"]
    selection = material_query(original, store, selection={"note_ids": ["test-note-1"]})["selection"]
    edited = midi_transform(original, selection, [{"op": "velocity", "value": 89}], store, "edit",
                            locks={"outside_selection": "all", "selected_fields": [
                                "pitch", "onset", "duration_qn", "release_velocity"]})
    args = seal_args(prepared, store, True)
    # Independently authored saved XML tests validation, never pretends to be Live.
    path = Path(args["saved_als"])
    root = ET.fromstring(gzip.decompress(path.read_bytes()))
    root.find(".//MidiNoteEvent").set("Velocity", "89")
    path.write_bytes(gzip.compress(ET.tostring(root), mtime=0))
    args["expected_sha256"] = sha256_file(path)
    args["native_report"]["saved_als_sha256"] = args["expected_sha256"]
    args["final_material"] = edited["material"]
    return args, prepared, original, snapshot(parent.parent)


def test_explicit_amendment_cli_receipt_and_lineage(tmp_path):
    args, prepared, original, source_before = amended_fixture(tmp_path)
    direct = candidate_seal(**args)
    assert cli_call(tmp_path, "candidate_seal", args) == direct
    trial = load_candidate_record(direct["artifacts"]["candidate"], args["store_root"])
    amendment = read_record(trial["material_amendment"], args["store_root"])
    preparation = read_record(prepared["artifacts"]["preparation"], args["store_root"])
    assert preparation["material"] == original == amendment["parent"]
    assert trial["material"] == args["final_material"] == amendment["child"]
    assert trial["provider_native_observation"] is False
    assert trial["native_verification"] == "attributed_save_reopen"
    assert snapshot(tmp_path / "source") == source_before


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="Optional MCP extra absent")
def test_amendment_stdio_and_cli_equivalence_and_strict_handle(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exchange():
        parameters = StdioServerParameters(command=sys.executable, args=["-m", "pocket_music.mcp_server"],
                                            env={**os.environ, "PYTHONPATH": str(
                                                Path(__file__).resolve().parents[1] / "src")})
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            descriptor = next(t for t in (await session.list_tools()).tools if t.name == "candidate_seal")
            assert "final_material" in descriptor.inputSchema["properties"]
            args, _, _, _ = amended_fixture(tmp_path)
            direct = candidate_seal(**args)
            result = await session.call_tool("candidate_seal", args)
            assert not result.isError and json.loads(result.content[0].text) == direct
            assert cli_call(tmp_path, "candidate_seal", args) == direct
            invalid = copy.deepcopy(args)
            invalid["request_id"] = "invalid-handle"
            invalid["final_material"]["ignore_locks"] = True
            assert (await session.call_tool("candidate_seal", invalid)).isError
            assert cli_call(tmp_path, "candidate_seal", invalid, success=False)["error"] == "PocketError"

    asyncio.run(exchange())
