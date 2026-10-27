# SPDX-License-Identifier: AGPL-3.0-only
"""Real transport exchanges use the same providers, handles and failure semantics."""
import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_musical_context import fixture

import pocket_music
from pocket_music.artifact_store import read_record
from pocket_music.capabilities import capabilities_list


def calls(store, context, definition):
    return [
        ("context_create", {"store_root": store, "request_id": "transport-create", "definition": definition}),
        ("context_query", {"store_root": store, "context": context, "section": "anchors"}),
        ("context_resolve", {"store_root": store, "context": context, "target_clock_id": "practice",
                             "target_space": "host_seconds", "anchor_id": "internal-one", "occurrence_id": "again"}),
        ("practice_render", {"store_root": store, "request_id": "transport-render", "context": context,
                             "occurrence_ids": ["first"]}),
    ]


def comparison_calls(store, first, alternative):
    return {"store_root": store, "request_id": "transport-compare", "baseline": first,
            "variants": [alternative], "question": "Synthetic comparison contract check"}


def test_cli_context_practice_parity_and_request_conflict(tmp_path):
    store, context, definition, _, _ = fixture(tmp_path)
    counter = 0
    def exchange(name, spec):
        nonlocal counter
        counter += 1
        path = tmp_path / f"spec-{counter}.json"
        path.write_text(json.dumps(spec))
        result = subprocess.run([sys.executable, "-m", "pocket_music.cli", name.replace("_", "-"), "--spec", str(path)],
                                 capture_output=True, text=True, timeout=30, check=False)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data == getattr(pocket_music, name)(**spec)
        return data
    for name, args in calls(store, context, definition):
        result = exchange(name, args)
    first = result["artifacts"]["render"]
    alternative = exchange("practice_render", {"store_root": store, "request_id": "transport-alt",
                                               "context": context, "occurrence_ids": ["alternative"]})["artifacts"]["render"]
    comparison = exchange("practice_compare", comparison_calls(store, first, alternative))["artifacts"]["comparison"]
    feedback = exchange("practice_feedback", {"store_root": store, "request_id": "transport-feedback",
        "comparison": comparison, "render": alternative, "interval_frames": [50, 7000], "actor": "synthetic test",
        "actor_kind": "agent", "note": "No listening claimed", "decision": "revise"})["artifacts"]["feedback"]
    exchange("practice_query", {"store_root": store, "artifact": feedback})
    spec = {"store_root": store, "request_id": "transport-render", "context": context, "occurrence_ids": ["again"]}
    path = tmp_path / "conflict.json"
    path.write_text(json.dumps(spec))
    result = subprocess.run([sys.executable, "-m", "pocket_music.cli", "practice-render", "--spec", str(path)],
                             capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 2 and "idempotency_conflict" in json.loads(result.stderr)["message"]


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="Optional agent extra is not installed")
def test_mcp_typed_contracts_full_flow_and_ambiguous_occurrence(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    store, context, definition, _, _ = fixture(tmp_path)
    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "pocket_music.mcp_server"],
                                       env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            schemas = {tool.name: tool.inputSchema for tool in (await session.list_tools()).tools}
            schema = schemas["context_create"]
            definition_ref = schema["properties"]["definition"]["$ref"].rsplit("/", 1)[1]
            assert {"sources", "timelines", "occurrences", "anchors"} <= set(schema["$defs"][definition_ref]["required"])
            assert schema["$defs"]["ClockPosition"]["additionalProperties"] is False
            async def exchange(name, spec):
                result = await session.call_tool(name, spec)
                assert not result.isError, result.content
                assert result.structuredContent is None and len(result.content) == 1
                data = json.loads(result.content[0].text)
                assert data == getattr(pocket_music, name)(**spec)
                return data
            for name, args in calls(store, context, definition):
                result = await exchange(name, args)
            first = result["artifacts"]["render"]
            alternative = (await exchange("practice_render", {"store_root": store, "request_id": "transport-alt",
                "context": context, "occurrence_ids": ["alternative"]}))["artifacts"]["render"]
            comparison = (await exchange("practice_compare", comparison_calls(store, first, alternative)))["artifacts"]["comparison"]
            report = await exchange("practice_feedback", {"store_root": store, "request_id": "transport-feedback",
                "comparison": comparison, "render": alternative, "interval_frames": [0, 1], "actor": "test agent",
                "actor_kind": "agent", "note": "Transport fixture; no listening performed"})
            await exchange("practice_query", {"store_root": store, "artifact": report["artifacts"]["feedback"]})
            ambiguous = await session.call_tool("context_resolve", {
                "store_root": store, "context": context, "target_clock_id": "practice",
                "target_space": "arrangement_qn", "anchor_id": "internal-one"})
            assert ambiguous.isError and "Ambiguous" in str(ambiguous.content)
            invalid = await session.call_tool("context_resolve", {
                "store_root": store, "context": context, "target_clock_id": "practice", "target_space": "arrangement_qn",
                "position": {"clock_id": "recording", "space": "source_frame", "value": True}})
            assert invalid.isError
    asyncio.run(asyncio.wait_for(run(), 60))


def test_discovery_declares_actual_profiles_and_artifacts(tmp_path):
    store, context, _, _, _ = fixture(tmp_path)
    rows = {r["public_tool"]: r for domain in ("context", "practice")
            for r in capabilities_list(domain=domain, limit=50)["capabilities"]}
    assert {"context_create", "context_query", "context_resolve", "practice_render",
            "practice_compare", "practice_feedback", "practice_query"} <= set(rows)
    for name, row in rows.items():
        assert callable(getattr(pocket_music, name))
        assert row["status"] == "available" and row["native_verified"] is False
        assert bool(row["side_effects"]) == (name not in ("context_query", "context_resolve", "practice_query", "context_edit_query", "practice_feedback_query"))
    result = pocket_music.practice_render(store, "render", context, ["first"])
    assert {h["artifact_schema"] for h in result["artifacts"].values()} == set(rows["practice_render"]["returned_artifact_schemas"])
    assert read_record(result["artifacts"]["render"], store)["musical_verdict"] is None
