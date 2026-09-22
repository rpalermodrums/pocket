"""Independent request/status/promotion integration QA; generated local evidence only."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_auditions import promotion_args
from test_candidate_interfaces import cli_call, environment, preparation_args
from test_native_candidates import snapshot

from pocket_music.artifact_store import (
    digest,
    put_bytes,
    put_record,
    read_record,
    receipt,
    request_status,
    run_request,
)
from pocket_music.auditions import promote_candidate, validate_candidate_promotion
from pocket_music.errors import PocketError


def completed(root, request="complete", outcome="ok"):
    nested = put_record({"schema": "pocket.qa-evidence/v1", "fact": "synthetic bytes"}, root)
    plan = put_record({"schema": "pocket.qa-plan/v1", "evidence": [nested, nested]}, root)
    result = run_request(root, request, "qa_status", {"source": nested},
                         lambda: receipt(request, status=outcome, artifacts={"plan": plan}))
    return result, nested


def test_qa_status_is_read_only_for_missing_started_failed_and_complete_requests(tmp_path):
    missing = request_status(str(tmp_path / "absent-store"), "missing")
    assert missing["status"] == "needs_input" and not (tmp_path / "absent-store").exists()
    completed(tmp_path)
    before = snapshot(tmp_path)
    state = request_status(str(tmp_path), "complete")
    assert state["journal_state"] == "complete" and state["coverage"]["artifact_integrity"] == "verified"
    assert state["coverage"]["redispatched"] is False
    assert state["coverage"]["external_state"] == "requires_provider_revalidation"
    assert snapshot(tmp_path) == before

    def fail():
        raise RuntimeError("Injected failure; no native work")
    with pytest.raises(RuntimeError):
        run_request(tmp_path, "failed", "qa_failure", {}, fail)
    failed_before = snapshot(tmp_path)
    state = request_status(str(tmp_path), "failed")
    assert state["status"] == "failed" and "Injected" in state["error"]
    assert snapshot(tmp_path) == failed_before
    with pytest.raises(PocketError, match="did not complete"):
        run_request(tmp_path, "failed", "qa_failure", {}, lambda: pytest.fail("redispatched"))


def test_qa_concurrent_observation_never_steals_active_lock_or_dispatches_again(tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []

    def work():
        calls.append("once")
        started.set()
        assert release.wait(5), "QA coordination timed out"
        return receipt("active")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_request, tmp_path, "active", "qa", {}, work)
        assert started.wait(5)
        try:
            before = snapshot(tmp_path)
            current = request_status(str(tmp_path), "active")
            assert current["status"] == "outcome_unknown" and current["journal_state"] == "started"
            assert current["coverage"]["lock_present"] is True
            assert (tmp_path / "requests/active/lock").is_dir() and snapshot(tmp_path) == before
            with pytest.raises(PocketError, match="active or interrupted"):
                run_request(tmp_path, "active", "qa", {}, lambda: pytest.fail("concurrent dispatch"))
        finally:
            release.set()
        future.result(5)
    assert calls == ["once"] and request_status(str(tmp_path), "active")["status"] == "ok"


def test_qa_orphan_lock_retained_even_without_any_journal(tmp_path):
    lock = tmp_path / "requests/orphan/lock"
    lock.mkdir(parents=True)
    result = request_status(str(tmp_path), "orphan")
    assert result["status"] == "outcome_unknown" and result["journal_state"] == "lock_without_journal"
    assert lock.is_dir() and not (lock.parent / "journal.json").exists()


@pytest.mark.parametrize("request_name", ["../escape", "/absolute", "a/b", "", ".hidden", "x" * 97, True])
def test_qa_status_request_containment_and_identifier_validation(tmp_path, request_name):
    with pytest.raises(PocketError):
        request_status(str(tmp_path), request_name)
    assert not (tmp_path / "requests").exists()


def test_qa_status_cannot_follow_journal_or_request_directory_symlinks(tmp_path):
    completed(tmp_path / "real")
    root = tmp_path / "linked"
    (root / "requests").mkdir(parents=True)
    (root / "requests/complete").symlink_to(tmp_path / "real/requests/complete", target_is_directory=True)
    with pytest.raises(PocketError, match="escapes|symlink"):
        request_status(str(root), "complete")


@pytest.mark.parametrize("target", ["nested_bytes", "schema", "receipt", "path_escape"])
def test_qa_completed_status_rechecks_transitive_graph_and_schema(tmp_path, target):
    _result, nested = completed(tmp_path)
    path = tmp_path / "requests/complete/journal.json"
    journal = json.loads(path.read_bytes())
    if target == "nested_bytes":
        (tmp_path / nested["artifact_uri"]).write_bytes(b"changed leaf evidence")
    elif target == "receipt":
        journal["receipt"]["coverage"]["invented"] = True
        path.write_text(json.dumps(journal))
    else:
        false = {**nested, "artifact_schema": "pocket.false-family/v1"} if target == "schema" else {
            **nested, "artifact_uri": "../outside"}
        outer = put_record({"schema": "pocket.qa-plan/v1", "nested": false}, tmp_path)
        journal["receipt"]["artifacts"] = {"plan": outer}
        journal["receipt_sha256"] = digest(journal["receipt"])
        path.write_text(json.dumps(journal))
    before = snapshot(tmp_path)
    with pytest.raises(PocketError):
        request_status(str(tmp_path), "complete")
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("field,value", [("operation", "x" * 100000), ("operation", {"arbitrary": "object"}),
                                         ("operation", None), ("input_sha256", "bad"),
                                         ("state", {}), ("state", ["complete"]),
                                         ("request_id", "wrong"), ("schema", "wrong")])
def test_qa_request_status_rejects_corrupt_or_unbounded_envelope(tmp_path, field, value):
    completed(tmp_path)
    path = tmp_path / "requests/complete/journal.json"
    record = json.loads(path.read_bytes())
    record[field] = value
    path.write_text(json.dumps(record))
    with pytest.raises(PocketError):
        request_status(str(tmp_path), "complete")


def test_qa_status_does_not_expand_a_large_completed_receipt(tmp_path):
    run_request(tmp_path, "large", "qa_large", {}, lambda: receipt("large", change_summary={"text": "z" * 100000}))
    result = request_status(str(tmp_path), "large")
    assert len(json.dumps(result).encode()) < 4096 and "change_summary" not in result.get("summary", {})


def test_qa_completed_failed_operation_retains_its_distinct_result_status(tmp_path):
    completed(tmp_path, outcome="failed")
    result = request_status(str(tmp_path), "complete")
    assert result["journal_state"] == "complete"
    assert result["status"] == result["receipt_status"] == "failed"
    assert result["coverage"]["artifact_integrity"] == "verified"


def test_qa_status_refuses_oversized_journal_before_parsing_it(tmp_path, monkeypatch):
    from pocket_music import artifact_store
    completed(tmp_path)
    path = tmp_path / "requests/complete/journal.json"
    with path.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024 + 1)
    monkeypatch.setattr(artifact_store.json, "loads", lambda *args, **kwargs: pytest.fail("Parsed oversized journal"))
    with pytest.raises(PocketError):
        request_status(str(tmp_path), "complete")


def test_qa_transitive_graph_depth_bound_rejects_without_work(tmp_path):
    value = {"schema": "pocket.qa-deep/v1"}
    for _ in range(140):
        value = {"schema": "pocket.qa-deep/v1", "child": value}
    handle = put_record(value, tmp_path)
    with pytest.raises(PocketError, match="bounds"):
        run_request(tmp_path, "too-deep", "qa", {"handle": handle}, lambda: pytest.fail("work called"))
    assert not (tmp_path / "requests/too-deep/lock").exists()


def test_qa_related_omission_retains_established_input_hash_lineage_and_retry(tmp_path):
    args, parent = promotion_args(tmp_path)
    before = snapshot(parent.parent)
    result = promote_candidate(**args)
    journal = json.loads((Path(args["store_root"]) / "requests/promote/journal.json").read_bytes())
    old_inputs = {key: args[key] for key in ("candidate", "attachment", "decision", "output_dir")}
    old_inputs["portability_target"] = "editable_same_environment"
    assert journal["input_sha256"] == digest({"operation": "promote_candidate", "inputs": old_inputs})
    assert "related_artifacts" not in read_record(result["artifacts"]["lineage"], args["store_root"])
    assert promote_candidate(**args, related_artifacts=None) == result
    assert snapshot(parent.parent) == before
    with pytest.raises(PocketError, match="idempotency_conflict"):
        promote_candidate(**args, related_artifacts=[])


@pytest.mark.parametrize("content", [b"opaque source bytes with a .json filename", b'{"ordinary":"raw JSON bytes"}',
                                     b'{"schema":"pocket.artifact-handle/v1","artifact_uri":"../untrusted-content"}'])
def test_qa_related_raw_json_named_bytes_remain_opaque_and_exact(tmp_path, content):
    args, parent = promotion_args(tmp_path)
    related = put_bytes(content, args["store_root"], "external.json", "pocket.binary-asset/v1")
    before = snapshot(parent.parent)
    result = promote_candidate(**args, related_artifacts=[related])
    moved = tmp_path / "moved-package"
    Path(result["promotion_dir"]).rename(moved)
    checked = validate_candidate_promotion(str(moved), result["lineage_sha256"])
    assert checked["native_relocated_reopen"] == "not_verified"
    assert (moved / "evidence" / related["artifact_uri"]).read_bytes() == content
    assert snapshot(parent.parent) == before


def test_qa_related_nested_records_are_copied_and_verified_after_store_relocation(tmp_path):
    args, parent = promotion_args(tmp_path)
    store = args["store_root"]
    raw = put_bytes(b"independently supplied synthetic native preset", store, "preset.adv", "pocket.native-preset/v1")
    nested = put_record({"schema": "pocket.qa-evidence/v1", "raw": raw}, store)
    graph = put_record({"schema": "pocket.qa-related/v1", "children": [nested, nested]}, store)
    before = snapshot(parent.parent)
    result = promote_candidate(**args, related_artifacts=[graph])
    moved = tmp_path / "relocated"
    shutil.move(result["promotion_dir"], moved)
    shutil.move(store, tmp_path / "archived-store")
    validate_candidate_promotion(str(moved), result["lineage_sha256"])
    assert read_record(graph, moved / "evidence")["children"] == [nested, nested]
    assert (moved / "evidence" / raw["artifact_uri"]).read_bytes().startswith(b"independently")
    assert snapshot(parent.parent) == before
    (moved / "evidence" / raw["artifact_uri"]).write_bytes(b"tampered moved evidence")
    with pytest.raises(PocketError, match="integrity|changed"):
        validate_candidate_promotion(str(moved), result["lineage_sha256"])


@pytest.mark.parametrize("kind", ["schema", "missing_nested", "too_many", "wrong_container", "undeclared_field"])
def test_qa_bad_related_graph_cannot_publish(tmp_path, kind):
    args, _ = promotion_args(tmp_path)
    raw = put_bytes(b"source", args["store_root"], "source.bin", "pocket.binary-asset/v1")
    if kind == "schema":
        related = [put_bytes(b'{"schema":"pocket.actual/v1"}', args["store_root"], "record.json", "pocket.wrong/v1")]
    elif kind == "missing_nested":
        record = put_record({"schema": "pocket.qa-related/v1", "raw": raw}, args["store_root"])
        (Path(args["store_root"]) / raw["artifact_uri"]).unlink()
        related = [record]
    elif kind == "too_many":
        related = [raw] * 33
    elif kind == "undeclared_field":
        related = [{**raw, "undeclared": True}]
    else:
        related = {"handle": raw}
    with pytest.raises(PocketError):
        promote_candidate(**args, related_artifacts=related)
    assert not Path(args["output_dir"]).exists()


def test_qa_cli_matches_mcp_rejection_of_undeclared_related_handle_fields(tmp_path):
    args, _ = promotion_args(tmp_path)
    handle = put_bytes(b"source", args["store_root"], "source.bin", "pocket.binary-asset/v1")
    args["related_artifacts"] = [{**handle, "undeclared": True}]
    assert cli_call(tmp_path, "promote_candidate", args, success=False)["error"] == "PocketError"
    assert not Path(args["output_dir"]).exists()


def test_qa_additive_stitch_pipette_and_package_aliases_share_provider_functions():
    import pocket_music
    from pocket_music import auditions, native_candidates, pipette, stitch
    for name in ("candidate_prepare", "candidate_inspect", "candidate_cancel", "candidate_seal", "validate_candidate"):
        assert getattr(stitch, name) is getattr(native_candidates, name)
    for name in ("audition_plan", "attach_candidate_render", "audition_feedback"):
        assert getattr(stitch, name) is getattr(auditions, name)
    assert pipette.promote_candidate is auditions.promote_candidate
    assert pipette.validate_candidate_promotion is auditions.validate_candidate_promotion
    assert pocket_music.request_status is request_status
    assert pipette.promote_trial is not pipette.promote_candidate


def test_qa_actual_cli_request_status_and_related_promotion(tmp_path):
    args, _ = promotion_args(tmp_path)
    related = put_record({"schema": "pocket.qa-comparison-note/v1", "basis": "synthetic"}, args["store_root"])
    args["related_artifacts"] = [related]
    expected = promote_candidate(**args)
    assert cli_call(tmp_path, "promote_candidate", args) == expected
    status_args = {"store_root": args["store_root"], "request_id": args["request_id"]}
    assert cli_call(tmp_path, "request_status", status_args) == request_status(**status_args)
    assert cli_call(tmp_path, "request_status", {**status_args, "request_id": "missing"})["status"] == "needs_input"
    assert cli_call(tmp_path, "request_status", {**status_args, "request_id": "../escape"}, success=False)["error"] == "PocketError"


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="Optional MCP extra unavailable")
def test_qa_actual_stdio_request_status_related_promotion_and_original_json(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from pocket_music.native_candidates import candidate_prepare
    args, _ = promotion_args(tmp_path)
    related = put_record({"schema": "pocket.qa-related/v1", "observations": []}, args["store_root"])
    args["related_artifacts"] = [related]
    expected = promote_candidate(**args)
    prepare_args = preparation_args(tmp_path, "integer-json-parity")
    assert type(prepare_args["context"]["tempo_bpm"]) is int
    prepared = candidate_prepare(**prepare_args)

    async def exchange():
        params = StdioServerParameters(command=sys.executable, args=["-m", "pocket_music.mcp_server"], env=environment())
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert tools["request_status"].annotations.readOnlyHint is True
            assert tools["request_status"].inputSchema["additionalProperties"] is False
            assert "related_artifacts" in tools["promote_candidate"].inputSchema["properties"]
            for name, arguments, wanted in [
                ("candidate_prepare", prepare_args, prepared), ("promote_candidate", args, expected),
                ("request_status", {"store_root": args["store_root"], "request_id": "promote"},
                 request_status(args["store_root"], "promote"))]:
                response = await session.call_tool(name, arguments)
                assert not response.isError, response.content
                assert len(response.content) == 1 and response.structuredContent is None
                text = response.content[0].text
                assert len(text.encode()) < 65536 and len(text.splitlines()) == 1
                assert json.loads(text) == wanted
            for malformed in [{"store_root": args["store_root"], "request_id": True},
                              {"store_root": args["store_root"], "request_id": "promote", "steal_lock": True}]:
                response = await session.call_tool("request_status", malformed)
                assert response.isError
            wrong = copy.deepcopy(args)
            wrong["related_artifacts"][0]["undeclared"] = True
            assert (await session.call_tool("promote_candidate", wrong)).isError
    asyncio.run(asyncio.wait_for(exchange(), timeout=90))
