# SPDX-License-Identifier: AGPL-3.0-only
"""b2b discovery, CLI and MCP wiring, and static checks on the b2b modules and tests."""
from __future__ import annotations

import ast
import asyncio
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from pocket_music import cli
from pocket_music.artifact_store import canonical_bytes
from pocket_music.b2b_adapters import SYNTHETIC_PROFILE
from pocket_music.capabilities import _UNAVAILABLE, PUBLIC_CAPABILITIES, capabilities_list

SRC = Path(__file__).resolve().parents[1] / "src" / "pocket_music"
TESTS = Path(__file__).resolve().parent
B2B = ("b2b_prepare", "b2b_authorize", "b2b_observe", "b2b_claim", "b2b_act", "b2b_release", "b2b_revoke",
       "b2b_events", "b2b_request_status")
READS = {"b2b_observe", "b2b_events", "b2b_request_status"}
GATES = ("b2b_serato_adapter", "b2b_listen_evidence", "b2b_instrument_extension", "b2b_monitor_exemption",
         "b2b_soft_takeover", "b2b_issuer_credential")
SHUTTLE_ROWS = (
    ('shuttle_build_bridge', 'shuttle', 'performance', 'build_bridge', False,
     'Build the optional local Serato/FLX4 bridge without starting it.'),
    ('shuttle_prepare', 'shuttle', 'performance', 'prepare', False,
     'Snapshot one local crate and prepare an unordered Whisker session.'),
    ('shuttle_observe', 'shuttle', 'performance', 'observe', True,
     'Read fresh supported Serato state and actual master signal; no listening claim.'),
    ('shuttle_options', 'shuttle', 'performance', 'options', True,
     'Propose next records using the crate, history and observed current deck.'),
    ('shuttle_act', 'shuttle', 'performance', 'act', False,
     'One revision-checked bounded move with native observation, replay and FLX4 takeover.'),
    ('shuttle_request_status', 'shuttle', 'performance', 'request_status', True,
     'Inspect a retained native request without redispatch.'),
)


def _rows(**filters) -> dict:
    rows, cursor = [], None
    while True:
        page = capabilities_list(limit=50, cursor=cursor, **filters)
        rows += page["capabilities"]
        cursor = page["next_cursor"]
        if cursor is None:
            return {row.get("public_tool") or row["proposed_tool"]: row for row in rows}


def test_discovery_rows_status_side_effects_and_output_schemas(monkeypatch):
    rows = _rows(domain="collaboration")
    assert set(B2B) | set(GATES) == set(rows)
    supported = sys.platform in ("darwin", "linux")
    for name in B2B:
        row = rows[name]
        assert row["required_profile"] == "synthetic-two-deck/v1" and row["native_verified"] is False
        assert row["provider"] == f"pocket_music.b2b.{name}" and row["implementation_status"] == "implemented"
        expected = "unsupported" if not supported else "available" if name == "b2b_request_status" else (
            "requires_optional_setup")
        assert row["status"] == expected, name
        assert "no Serato, controller, MIDI or audio device" in row["prerequisites"][0]
        assert row["output_schema"] == {"b2b_observe": "pocket.b2b-observation/v1",
                                        "b2b_events": "pocket.b2b-events/v1",
                                        "b2b_request_status": "pocket.b2b-request-status/v1"}.get(
                                            name, "pocket.operation-receipt/v1")
    assert rows["b2b_act"]["side_effects"] == ["synthetic_adapter_controls", "b2b_control_journal"]
    assert rows["b2b_prepare"]["side_effects"] == ["new_local_artifacts", "b2b_host_session"]
    assert rows["b2b_authorize"]["side_effects"] == ["b2b_policy_proposal", "b2b_control_journal"]
    assert rows["b2b_claim"]["side_effects"] == ["b2b_authority_grant", "trusted_page_notice",
                                                 "b2b_control_journal"]
    for name in ("b2b_release", "b2b_revoke"):
        assert rows[name]["side_effects"] == ["b2b_authority_reduction", "b2b_control_journal"]
    assert all(rows[name]["side_effects"] == [] for name in READS)
    monkeypatch.setattr(sys, "platform", "win32")
    elsewhere = _rows(domain="collaboration")
    assert {elsewhere[name]["status"] for name in B2B} == {"unsupported"}


def test_discovery_gates_are_not_implemented_and_expose_no_serato_resources():
    rows = _rows(domain="collaboration")
    for name in GATES:
        row = rows[name]
        assert row["public_tool"] is None and row["implementation_status"] == "not_implemented"
        assert row["status"] == "documented_unprobed" and row["native_verified"] is False
    named = [r["resource_id"] for r in SYNTHETIC_PROFILE["resources"]] + list(SYNTHETIC_PROFILE["aliases"])
    assert named and all(n.startswith(("synthetic/", "controller/synthetic/")) for n in named)
    assert "serato/" not in json.dumps([rows[name] for name in B2B]).lower()
    assert SYNTHETIC_PROFILE["native"] is False


def test_b2b_discovery_pages_stay_within_4096_bytes_and_lists_are_fresh():
    cursor, seen = None, []
    while True:
        page = capabilities_list(domain="collaboration", limit=1, cursor=cursor)
        assert len(canonical_bytes(page)) <= 4096
        seen += page["capabilities"]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(B2B) + len(GATES)
    before = capabilities_list(domain="collaboration", limit=50)
    expected = copy.deepcopy(before)
    for row in before["capabilities"]:
        for value in row.values():
            if isinstance(value, list):
                value.append("poisoned")
    assert capabilities_list(domain="collaboration", limit=50) == expected


def test_shuttle_rows_and_the_performance_launch_gate_are_unchanged():
    assert tuple(row for row in PUBLIC_CAPABILITIES if row[1] == "shuttle") == SHUTTLE_ROWS
    assert ('performance_launch', 'performance', 'launch',
            'E9 scheduling, takeover, panic and 30-minute soak') in _UNAVAILABLE
    names = [row[0] for row in PUBLIC_CAPABILITIES]
    assert names.index("b2b_prepare") == names.index("shuttle_request_status") + 1
    bridge = capabilities_list(domain="performance", operation="build_bridge")["capabilities"]
    assert [row["public_tool"] for row in bridge] == ["shuttle_build_bridge"]
    performance = _rows(domain="performance")
    assert set(performance) == {row[0] for row in SHUTTLE_ROWS} | {"performance_launch"}


def test_cli_group_and_flat_commands_reach_the_same_provider(tmp_path, monkeypatch, capsys):
    import pocket_music.b2b as b2b_module

    calls = []
    for name in B2B:
        monkeypatch.setattr(b2b_module, name,
                            lambda *a, _name=name, **k: calls.append((_name, k)) or {"ok": 1})
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"session_dir": "private/b2b-session"}))
    assert cli.main(["b2b", "observe", "--spec", str(spec)]) == 0
    assert cli.main(["b2b-observe", "--spec", str(spec)]) == 0
    assert calls == [("b2b_observe", {"session_dir": "private/b2b-session"})] * 2
    status_spec = tmp_path / "status.json"
    status_spec.write_text(json.dumps({"session_dir": "s", "request_id": "r-1"}))
    assert cli.main(["b2b", "request-status", "--spec", str(status_spec)]) == 0
    assert calls[-1] == ("b2b_request_status", {"session_dir": "s", "request_id": "r-1"})
    prepare = tmp_path / "prepare.json"
    prepare.write_text(json.dumps({"runtime_dir": "private/b2b/run-1"}))
    assert cli.main(["b2b", "prepare", "--spec", str(prepare), "--output", "private/b2b-session"]) == 0
    assert calls[-1] == ("b2b_prepare", {"runtime_dir": "private/b2b/run-1",
                                         "output_dir": "private/b2b-session"})
    for group_action in ("authorize", "claim", "act", "release", "revoke", "events"):
        assert cli._SPEC_OPERATIONS["b2b"][group_action] == ("b2b", f"b2b_{group_action}", None)
    capsys.readouterr()


def _server():
    from pocket_music.mcp_server import build_server

    return build_server()


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="Optional agent extra")
def test_mcp_schemas_carry_nested_enums_and_forbid_extra_fields():
    from jsonschema import Draft202012Validator
    from mcp.server.fastmcp.exceptions import ToolError

    server = _server()
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    for name in B2B:
        schema = tools[name].inputSchema
        Draft202012Validator.check_schema(schema)
        assert schema["additionalProperties"] is False
        assert tools[name].description == next(row[5] for row in PUBLIC_CAPABILITIES if row[0] == name)
    act = json.dumps(tools["b2b_act"].inputSchema)
    assert '"enum": ["set", "ramp", "nudge", "set_state"]' in act
    definitions = tools["b2b_act"].inputSchema["$defs"]
    assert definitions["B2bAction"]["additionalProperties"] is False
    assert "issuer" not in json.dumps({name: tools[name].inputSchema for name in B2B})
    base = {"session_dir": "s", "request_id": "r-1", "expected_session_epoch": "h.1",
            "expected_control_revision": 0, "expected_state_sha256": "0" * 64, "grant_id": "g-1",
            "not_after_monotonic_ns": 1}
    for action in ({"kind": "set", "resource": "x", "value": True},
                   {"kind": "set", "resource": "x", "value": 1, "issuer": "person"},
                   {"kind": "jump", "resource": "x"}):
        with pytest.raises(ToolError, match="Invalid b2b_act arguments"):
            asyncio.run(server.call_tool("b2b_act", {**base, "action": action}))


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="Optional agent extra")
def test_mcp_annotations_for_b2b_and_shuttle_unchanged():
    tools = {tool.name: tool.annotations for tool in asyncio.run(_server().list_tools())}
    for name in B2B:
        hints = tools[name]
        assert hints.readOnlyHint is (name in READS)
        assert hints.idempotentHint is (name != "b2b_prepare")
        assert hints.destructiveHint is False and hints.openWorldHint is False
    shuttle = {name: tools[name].idempotentHint for name in (row[0] for row in SHUTTLE_ROWS)}
    assert shuttle == {"shuttle_build_bridge": False, "shuttle_prepare": False, "shuttle_observe": True,
                       "shuttle_options": True, "shuttle_act": True, "shuttle_request_status": True}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def test_broker_journal_and_adapter_modules_use_only_the_injected_clock():
    for name in ("b2b_broker", "b2b_journal", "b2b_adapters", "b2b_ui"):
        tree = _tree(SRC / f"{name}.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("now", "utcnow", "today"):
                # datetime.now() and datetime.datetime.now() both read the wall clock
                assert "datetime" not in ast.unparse(node.value), f"{name} reads {ast.unparse(node)}"
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                assert node.value.id != "time", f"{name} reads time.{node.attr}"
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                plain = isinstance(node, ast.Import)
                modules = [alias.name for alias in node.names] if plain else [node.module]
                assert "time" not in modules, f"{name} imports time"


def test_b2b_tests_never_sleep():
    for path in [*TESTS.glob("test_b2b_*.py"), TESTS / "b2b_harness.py"]:
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Call):
                func = node.func
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                assert called != "sleep", f"{path.name}:{node.lineno} sleeps"
            if isinstance(node, ast.ImportFrom) and node.module in ("time", "asyncio"):
                assert "sleep" not in [alias.name for alias in node.names], path.name


def test_only_admit_step_calls_adapter_submit():
    tree = _tree(SRC / "b2b_broker.py")
    callers = []
    for function in ast.walk(tree):
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(function):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "submit"):
                    callers.append(function.name)
    assert callers == ["_admit_step"]


_FORBIDDEN = {"subprocess", "ctypes", "urllib.request", "http.client", "requests", "httpx", "mido", "rtmidi",
              "sounddevice", "soundfile", "pyaudio", "multiprocessing"}


def test_b2b_modules_import_no_subprocess_ctypes_network_clients_or_shuttle():
    for path in sorted(SRC.glob("b2b*.py")):
        imported = set()
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(("." * node.level) + (node.module or ""))
        tops = {name.lstrip(".").split(".")[0] for name in imported} | {name.lstrip(".") for name in imported}
        assert not tops & _FORBIDDEN, (path.name, tops & _FORBIDDEN)
        assert not any("shuttle" in name for name in imported), path.name
        if path.stem not in ("b2b_host", "b2b_ui"):
            assert not tops & {"socketserver", "http"}, path.name
        if path.stem not in ("b2b", "b2b_host", "b2b_ui"):
            assert "socket" not in tops, path.name
