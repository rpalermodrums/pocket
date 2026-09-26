# SPDX-License-Identifier: AGPL-3.0-only
"""Forward-only filesystem ownership/recovery; no native app is involved."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_candidate_native_pending import ATTRIBUTION, fixture, state, terminal
from test_candidate_native_pending import terminal_validator as terminal_validator  # noqa: PLC0414

from pocket_music.artifact_store import read_record
from pocket_music.errors import PocketError
from pocket_music.native_candidates import (
    _workspace,
    candidate_inspect,
    candidate_native_reconcile,
    native_workspace,
)


def crash(scope, binding, completed=False):
    script = '''import json, os, sys
from pocket_music.native_candidates import native_workspace
scope, binding = json.loads(sys.argv[1])
with native_workspace(**scope) as session:
    session.begin(**binding)
    os._exit(23)
'''
    result = subprocess.run([sys.executable, "-c", script, json.dumps([scope, binding])],
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 23, result.stderr
    folder = Path(scope["saved_als"]).parent
    return folder, json.loads((folder / ".workspace-lock/owner.json").read_bytes())


def test_hard_process_crash_recovery_requires_exact_terminal_and_retains_marker(tmp_path, terminal_validator):
    scope, binding, _, _ = fixture(tmp_path)
    folder, marker = crash(scope, binding)
    inode = (folder / ".workspace-owner-lock").stat().st_ino
    pending = {"pending": state(scope)["native_pending"]}
    handle = terminal(pending, scope["store_root"])
    result = candidate_native_reconcile(scope["store_root"], scope["workspace_id"], 2,
                                        handle, ATTRIBUTION, "recover-crash")
    assert result["workspace"]["revision"] == 4
    assert state(scope)["state"] == "awaiting_native"
    evidence = [entry["evidence"] for entry in state(scope)["history"] if entry["state"] == "workspace_lock_recovered"]
    assert len(evidence) == 1
    record = read_record(evidence[0], scope["store_root"])
    assert record["prior_owner"] == marker
    assert record["ownership_evidence"] == "exclusive_stable_flock_acquired"
    assert record["native_outcome"] == "not_inferred_from_process_lock"
    assert not (folder / ".workspace-lock").exists()
    assert (folder / ".workspace-owner-lock").stat().st_ino == inode


def test_live_cooperating_process_prevents_even_explicit_recovery(tmp_path):
    scope, binding, _, _ = fixture(tmp_path)
    script = '''import json, sys
from pocket_music.native_candidates import native_workspace
scope,binding=json.loads(sys.argv[1])
with native_workspace(**scope) as session:
 session.begin(**binding)
 print("held",flush=True)
 sys.stdin.readline()
'''
    process = subprocess.Popen([sys.executable, "-c", script, json.dumps([scope, binding])],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "held"
        with pytest.raises(PocketError, match="cooperating owner"), _workspace(
                scope["store_root"], scope["workspace_id"], 2, recover_native=True):
            pytest.fail("Live owner was displaced")
        assert candidate_inspect(scope["store_root"], scope["workspace_id"], 2)["coverage"]["workspace_lock_present"]
    finally:
        process.communicate("release\n", timeout=10)


@pytest.mark.parametrize("attack", ["ownerless", "marker_extra", "wrong_workspace", "wrong_inode", "stable_replaced", "stable_symlink", "stable_hardlink"])
def test_legacy_or_tampered_mutex_is_never_reclaimed(tmp_path, attack):
    scope, binding, _, _ = fixture(tmp_path)
    folder, marker = crash(scope, binding)
    stable = folder / ".workspace-owner-lock"
    owner = folder / ".workspace-lock/owner.json"
    if attack == "ownerless":
        owner.unlink()
    elif attack == "marker_extra":
        marker["unknown"] = True
        owner.write_text(json.dumps(marker))
    elif attack == "wrong_workspace":
        marker["workspace_id"] = "workspace-" + "f" * 24
        owner.write_text(json.dumps(marker))
    elif attack == "wrong_inode":
        marker["stable_inode"] += 1
        owner.write_text(json.dumps(marker))
    elif attack == "stable_replaced":
        stable.rename(folder / "retained-original-stable")
        stable.write_bytes(b"")
    elif attack == "stable_symlink":
        moved = folder / "moved-stable"
        stable.rename(moved)
        stable.symlink_to(moved)
    else:
        os.link(stable, folder / "aliased-stable")
    with pytest.raises(PocketError), _workspace(scope["store_root"], scope["workspace_id"], 2, recover_native=True):
        pytest.fail("Unqualified retained mutex was reclaimed")
    assert (folder / ".workspace-lock").exists()
    assert state(scope)["state"] == "host_pending" and state(scope)["revision"] == 2


def test_owner_marker_change_during_session_is_detected_and_retained(tmp_path):
    scope, _, _, _ = fixture(tmp_path)
    owner = Path(scope["saved_als"]).parent / ".workspace-lock/owner.json"
    with pytest.raises(PocketError, match="ownership changed"), native_workspace(**scope) as session:
        record = json.loads(owner.read_bytes())
        record["acquisition_token"] = "e" * 32
        owner.write_text(json.dumps(record))
        session._check()
    assert owner.exists()


def test_material_import_does_not_depend_on_native_platform_lock_support():
    script = '''import sys
sys.modules["fcntl"] = None
from pocket_music.native_candidates import _workspace
from pocket_music.material import new_material
from pocket_music.errors import PocketError
assert new_material("portable-file-only", tracks=[], clips=[], notes=[])["schema"] == "pocket.material/v1"
try:
 with _workspace("unused-store", "workspace-" + "0" * 24, 1):
  raise AssertionError("Native platform lock unexpectedly available")
except PocketError as error:
 assert "POSIX file locks" in str(error)
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
