"""Permanent supervised abandonment; native closure helpers are explicit fakes."""
import copy
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_candidate_runtime_identity import runtime_fixture, writer_scope
from test_candidate_runtime_identity import writer_bridge as writer_bridge  # noqa: PLC0414
from test_native_candidates import snapshot
from test_native_midi import bridge as bridge  # noqa: PLC0414

from pocket_music import native_candidates, native_midi
from pocket_music.artifact_store import put_record, read_bytes, read_record
from pocket_music.errors import PocketError
from pocket_music.native_candidates import (
    candidate_cancel,
    candidate_inspect,
    candidate_native_abandon,
    candidate_native_reconcile,
    candidate_seal,
    native_workspace,
)

ATTRIBUTION = {"actor": "Synthetic supervised closure", "actor_kind": "agent",
               "observed_at": "2026-09-16T00:00:00+00:00", "reason": "Test permanent abandonment only",
               "all_old_live_max_instances_stopped": True, "old_writer_unloaded": True,
               "old_candidate_closed_without_saving": True, "no_native_dispatch_in_flight": True}


@pytest.fixture
def recovery_helpers(monkeypatch):
    releases = []
    def observe(pending, fresh, store, bridge_dir=None):
        return put_record({"schema": "pocket.native-abandonment-observation/v1", "pending": pending,
                           "fresh_observation": fresh, "basis": "synthetic recovery helper seam"}, store)
    monkeypatch.setattr(native_midi, "observe_native_abandonment", observe, raising=False)
    monkeypatch.setattr(native_midi, "release_native_abandoned_lease", lambda handle, store: releases.append(handle), raising=False)
    return releases


def setup(tmp_path, writer_bridge, bridge):
    start, package, _device = writer_bridge
    old = runtime_fixture(tmp_path, start, writer_package=package, empty_observed=True)
    observation = read_record(old["observation"], old["store"])
    with native_workspace(**writer_scope(old)) as session:
        pending = session.begin(request_id="uncertain-edit", request_key="1" * 64, operation="insert_empty",
                                input_sha256="2" * 64, session_nonce=observation["session_nonce"],
                                package_sha256=observation["package_sha256"], expected_observation=old["observation"])
    fresh_dir = tmp_path / "new-session"
    fresh_dir.mkdir()
    fresh = runtime_fixture(fresh_dir, bridge, empty_observed=True)
    record = read_record(fresh["observation"], fresh["store"])
    fresh["saved"].write_bytes(read_bytes(record["saved_binding"]["saved_als"], fresh["store"]))
    shutil.copytree(Path(fresh["store"]) / "artifacts", Path(old["store"]) / "artifacts", dirs_exist_ok=True)
    args = {"store_root": old["store"], "workspace_id": old["args"]["workspace_id"], "expected_revision": 3,
            "pending": pending["pending"], "fresh_observation": fresh["observation"],
            "attribution": ATTRIBUTION, "request_id": "abandon", "bridge_dir": str(tmp_path / "old-bridge")}
    return old, fresh, args


def test_permanent_abandonment_preserves_unknown_outcome_all_files_and_replay(tmp_path, writer_bridge, bridge, recovery_helpers):
    old, _, args = setup(tmp_path, writer_bridge, bridge)
    original = snapshot(old["source"].parent)
    saved = old["saved"].read_bytes()
    result = candidate_native_abandon(**args)
    assert result["status"] == "ok"
    assert result["workspace"]["state"] == "abandoned_native_unknown"
    assert result["workspace"]["revision"] == 4
    assert result["coverage"]["native_outcome"] == "unknown"
    assert result["coverage"]["native_rollback"] is False
    assert result["coverage"]["resumable"] is False
    observed = candidate_inspect(args["store_root"], args["workspace_id"], 4)
    assert observed["workspace"]["native_pending"] == args["pending"]
    assert old["saved"].read_bytes() == saved
    assert snapshot(old["source"].parent) == original
    assert candidate_native_abandon(**args) == result
    assert recovery_helpers == [result["artifacts"]["abandonment"]] * 2
    for action in (
        lambda: candidate_cancel(args["store_root"], args["workspace_id"], 4, "cancel-abandoned"),
        lambda: candidate_seal(**{**old["args"], "expected_revision": 4}),
        lambda: candidate_native_reconcile(args["store_root"], args["workspace_id"], 4,
                                            put_record({"schema": "pocket.native-midi-terminal/v1"}, args["store_root"]),
                                            {key: ATTRIBUTION[key] for key in ("actor", "actor_kind", "observed_at", "reason")}, "reconcile-abandoned"),
    ):
        with pytest.raises(PocketError):
            action()
    with pytest.raises(PocketError), native_workspace(**{**writer_scope(old), "expected_revision": 4}):
        pytest.fail("Abandoned workspace resumed")


@pytest.mark.parametrize("key", ["all_old_live_max_instances_stopped", "old_writer_unloaded",
                                "old_candidate_closed_without_saving", "no_native_dispatch_in_flight"])
def test_every_closure_declaration_is_required_true(tmp_path, writer_bridge, bridge, recovery_helpers, key):
    old, _, args = setup(tmp_path, writer_bridge, bridge)
    args["attribution"] = {**ATTRIBUTION, key: False}
    with pytest.raises(PocketError):
        candidate_native_abandon(**args)
    assert json.loads((old["saved"].parent / "workspace.json").read_bytes())["state"] == "outcome_unknown"
    assert recovery_helpers == []


@pytest.mark.parametrize("attack", ["old_observation", "saved_changed", "source_changed", "fresh_changed", "stale_revision", "wrong_pending", "retained_lock"])
def test_abandonment_refuses_stale_or_ambiguous_evidence(tmp_path, writer_bridge, bridge, recovery_helpers, attack):
    old, fresh, args = setup(tmp_path, writer_bridge, bridge)
    if attack == "old_observation":
        args["fresh_observation"] = old["observation"]
    elif attack == "saved_changed":
        old["saved"].write_bytes(b"changed saved state")
    elif attack == "source_changed":
        old["source"].write_bytes(b"changed source")
    elif attack == "fresh_changed":
        fresh["saved"].write_bytes(b"changed fresh project")
    elif attack == "stale_revision":
        args["expected_revision"] = 2
    elif attack == "retained_lock":
        (old["saved"].parent / ".workspace-lock").mkdir()
    else:
        pending = read_record(args["pending"], old["store"])
        pending["request_key"] = "3" * 64
        args["pending"] = put_record(pending, old["store"])
    with pytest.raises(PocketError):
        candidate_native_abandon(**args)
    assert recovery_helpers == []


def test_verified_abandonment_can_retry_lease_cleanup_after_persistence(tmp_path, writer_bridge, bridge, recovery_helpers, monkeypatch):
    old, _, args = setup(tmp_path, writer_bridge, bridge)
    def fail(*_):
        raise PocketError("Synthetic lease cleanup failure")
    monkeypatch.setattr(native_midi, "release_native_abandoned_lease", fail)
    with pytest.raises(PocketError, match="cleanup failure"):
        candidate_native_abandon(**args)
    assert json.loads((old["saved"].parent / "workspace.json").read_bytes())["state"] == "abandoned_native_unknown"
    monkeypatch.setattr(native_midi, "release_native_abandoned_lease", lambda *_: None)
    assert candidate_native_abandon(**args)["workspace"]["revision"] == 4


def test_changed_attribution_cannot_replay_the_same_abandonment_request(tmp_path, writer_bridge, bridge, recovery_helpers):
    _, _, args = setup(tmp_path, writer_bridge, bridge)
    candidate_native_abandon(**args)
    changed = copy.deepcopy(args)
    changed["attribution"]["reason"] = "Different declaration"
    with pytest.raises(PocketError, match="idempotency_conflict"):
        candidate_native_abandon(**changed)


@pytest.mark.parametrize("healthy_old_bridge", [False, True])
def test_real_recovery_helpers_full_graph_and_matching_lease(tmp_path, writer_bridge, bridge, monkeypatch, healthy_old_bridge):
    old, _, args = setup(tmp_path, writer_bridge, bridge)
    args["bridge_dir"] = old["live"]["folder"]
    pending = read_record(args["pending"], args["store_root"])
    lease = {"schema": "pocket.native-midi-host-lease/v1", "request_sha256": "a" * 64,
             **{key: pending[key] for key in ("session_nonce", "request_key", "package_sha256", "workspace_id", "pending_revision")}}
    lease_path = tmp_path / "isolated-lease" / "host-lease.json"
    lease_path.parent.mkdir()
    lease_path.write_text(json.dumps(lease))
    monkeypatch.setattr(native_midi, "_host_lease_path", lambda: lease_path)
    if healthy_old_bridge:
        with pytest.raises(PocketError, match="remains healthy"):
            candidate_native_abandon(**args)
        assert json.loads(lease_path.read_bytes()) == lease
    else:
        process = old["live"]["process"]
        process.terminate()
        process.communicate(timeout=5)
        result = candidate_native_abandon(**args)
        assert result["workspace"]["state"] == "abandoned_native_unknown"
        assert not lease_path.exists()
        retained = read_record(result["artifacts"]["abandonment"], args["store_root"])
        observation = read_record(retained["recovery_observation"], args["store_root"])
        assert observation["lease_record"] == lease
        assert observation["old_bridge_health"] in {"not_present", "not_healthy"}
        assert candidate_native_abandon(**args) == result


def test_supervised_abandon_can_recover_new_marked_process_crash(tmp_path, writer_bridge, bridge, recovery_helpers):
    old, _, args = setup(tmp_path, writer_bridge, bridge)
    script = '''import json, os, sys
from pocket_music.native_candidates import _workspace
store,workspace,revision=json.loads(sys.argv[1])
with _workspace(store,workspace,revision):
 os._exit(23)
'''
    process = subprocess.run([sys.executable, "-c", script,
                              json.dumps([args["store_root"], args["workspace_id"], 3])],
                             capture_output=True, text=True, timeout=10, check=False)
    assert process.returncode == 23, process.stderr
    result = candidate_native_abandon(**args)
    assert result["workspace"]["state"] == "abandoned_native_unknown"
    assert result["workspace"]["revision"] == 5
    state = json.loads((old["saved"].parent / "workspace.json").read_bytes())
    assert len([entry for entry in state["history"] if entry["state"] == "workspace_lock_recovered"]) == 1


@pytest.mark.parametrize("already_completed", [False, True])
def test_abandonment_requires_recent_new_evidence_but_completed_replay_is_historical(tmp_path, writer_bridge, bridge, recovery_helpers, monkeypatch, already_completed):
    _, _, args = setup(tmp_path, writer_bridge, bridge)
    original = candidate_native_abandon(**args) if already_completed else None
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(minutes=6)
    monkeypatch.setattr(native_candidates, "datetime", Later)
    if already_completed:
        assert candidate_native_abandon(**args) == original
    else:
        with pytest.raises(PocketError, match="last five minutes"):
            candidate_native_abandon(**args)
