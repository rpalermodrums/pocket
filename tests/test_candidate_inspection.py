# SPDX-License-Identifier: AGPL-3.0-only
"""Workspace inspection is a bounded current summary with explicit history pages."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_native_candidates import prepare

from pocket_music import native_candidates
from pocket_music.errors import PocketError


def fixture(tmp_path, entries=0):
    prepared, inputs, _ = prepare(tmp_path)
    path = Path(prepared["workspace"]["mutable_als_path"]).parent / "workspace.json"
    state = json.loads(path.read_bytes())
    state["history"].extend({"state": "synthetic-history", "revision": index + 2, "request_id": f"example-{index}"}
                            for index in range(entries))
    path.write_text(json.dumps(state) + "\n")
    args = {"store_root": inputs["store_root"], "workspace_id": state["workspace_id"], "expected_revision": 1}
    return args, path, state


def test_default_inspection_is_bounded_and_preserves_complete_history(tmp_path):
    args, path, state = fixture(tmp_path, 1000)
    before = path.read_bytes()
    result = native_candidates.candidate_inspect(**args)
    assert "history" not in result["workspace"]
    assert result["workspace"]["preparation"] == state["preparation"]
    assert result["history"] == {"schema": "pocket.workspace-history-page/v1", "total": 1002,
                                 "rows": [], "next_cursor": None}
    assert len(json.dumps(result).encode()) < 3072
    assert path.read_bytes() == before


def test_history_pages_are_exact_ordered_and_stable(tmp_path):
    args, path, state = fixture(tmp_path, 8)
    before = path.read_bytes()
    rows, cursor = [], None
    while True:
        result = native_candidates.candidate_inspect(**args, history_limit=3, history_cursor=cursor)
        assert len(result["history"]["rows"]) <= 3
        rows.extend(result["history"]["rows"])
        cursor = result["history"]["next_cursor"]
        if cursor is None:
            break
    assert rows == state["history"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("value", [True, -1, 21, 1.0, "2", None])
def test_invalid_history_limits_refuse(tmp_path, value):
    args, _, _ = fixture(tmp_path)
    with pytest.raises(PocketError, match="history_limit"):
        native_candidates.candidate_inspect(**args, history_limit=value)


@pytest.mark.parametrize("value", [True, b"cursor", "", "history-v1.x.0", "x" * 1000])
def test_invalid_history_cursors_refuse(tmp_path, value):
    args, _, _ = fixture(tmp_path)
    with pytest.raises(PocketError, match="cursor"):
        native_candidates.candidate_inspect(**args, history_limit=1, history_cursor=value)


def test_history_cursor_refuses_new_revision_same_revision_mutation_and_wrong_workspace(tmp_path):
    args, path, state = fixture(tmp_path, 4)
    cursor = native_candidates.candidate_inspect(**args, history_limit=1)["history"]["next_cursor"]
    with pytest.raises(PocketError, match="page limit"):
        native_candidates.candidate_inspect(**args, history_cursor=cursor)
    state["history"][0]["extra"] = "Concurrent change without revision"
    path.write_text(json.dumps(state))
    with pytest.raises(PocketError, match="same workspace revision"):
        native_candidates.candidate_inspect(**args, history_limit=1, history_cursor=cursor)
    state["revision"] = 2
    path.write_text(json.dumps(state))
    with pytest.raises(PocketError, match="same workspace revision"):
        native_candidates.candidate_inspect(**{**args, "expected_revision": 2}, history_limit=1, history_cursor=cursor)
    other = tmp_path / "other"
    other.mkdir()
    other_args, _, _ = fixture(other)
    with pytest.raises(PocketError, match="same workspace revision"):
        native_candidates.candidate_inspect(**other_args, history_limit=1, history_cursor=cursor)


def test_history_byte_bound_returns_explicit_next_page_without_truncating_rows(tmp_path):
    args, path, state = fixture(tmp_path, 20)
    for row in state["history"]:
        row["retained_synthetic_detail"] = "x" * 3000
    path.write_text(json.dumps(state))
    result = native_candidates.candidate_inspect(**args, history_limit=20)
    rows = result["history"]["rows"]
    assert 0 < len(rows) < 20
    assert rows == state["history"][:len(rows)]
    assert result["history"]["next_cursor"] is not None
    assert len(json.dumps(result).encode()) < 16 * 1024


def test_oversized_single_row_refuses_without_rewriting_state(tmp_path):
    args, path, state = fixture(tmp_path)
    state["history"][0]["retained_synthetic_detail"] = "x" * (12 * 1024)
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    with pytest.raises(PocketError, match="One workspace history entry"):
        native_candidates.candidate_inspect(**args, history_limit=1)
    assert path.read_bytes() == before


def test_history_byte_budget_accounts_for_cli_unicode_escaping_and_indentation(tmp_path):
    args, path, state = fixture(tmp_path, 4)
    for row in state["history"]:
        row["retained_synthetic_detail"] = "🎵" * 500
    path.write_text(json.dumps(state))
    result = native_candidates.candidate_inspect(**args, history_limit=20)
    rows = result["history"]["rows"]
    assert 0 < len(rows) < len(state["history"])
    assert rows == state["history"][:len(rows)]
    assert result["history"]["next_cursor"] is not None
    assert len((json.dumps(result, indent=2) + "\n").encode()) <= 16 * 1024


def test_concurrent_state_change_still_refuses_inspection(tmp_path, monkeypatch):
    args, path, state = fixture(tmp_path)
    verify = native_candidates._verify_dependencies

    def changed(*values):
        verify(*values)
        path.write_text(json.dumps({**state, "revision": 2}))

    monkeypatch.setattr(native_candidates, "_verify_dependencies", changed)
    with pytest.raises(PocketError, match="Workspace changed during inspection"):
        native_candidates.candidate_inspect(**args)


@pytest.mark.parametrize("limit", [0, 1])
def test_metadata_depth_refuses_before_pretty_json_or_copy(tmp_path, monkeypatch, limit):
    args, path, state = fixture(tmp_path)
    nested = {"leaf": "retained"}
    for _ in range(200):
        nested = {"child": nested}
    state["history"][0]["nested"] = nested
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    original = json.dumps

    def forbid_pretty(*values, **options):
        assert options.get("indent") is None, "Depth must be checked before pretty formatting"
        return original(*values, **options)

    monkeypatch.setattr(native_candidates.json, "dumps", forbid_pretty)
    monkeypatch.setattr(native_candidates.copy, "deepcopy", lambda *_: pytest.fail("Depth must be checked before copying"))
    with pytest.raises(PocketError, match="depth of 128"):
        native_candidates.candidate_inspect(**args, history_limit=limit)
    assert path.read_bytes() == before
