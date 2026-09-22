"""Independent process ownership QA; terminal seam tests no native behavior."""
import json
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest
from test_candidate_native_pending import ATTRIBUTION, fixture, state, terminal
from test_native_candidates import snapshot

from pocket_music import native_candidates
from pocket_music.artifact_store import put_record, read_record
from pocket_music.errors import PocketError

CHILD = '''import json, os, sys
from pocket_music.native_candidates import native_workspace
scope, binding, mode = json.loads(sys.argv[1])
with native_workspace(**scope) as session:
    if mode != 'empty_crash':
        session.begin(**binding)
    print('owned', flush=True)
    if mode == 'hold':
        sys.stdin.readline()
    else:
        os._exit(37)
'''


def child(scope, binding, mode):
    process = subprocess.Popen([sys.executable, '-c', CHILD, json.dumps([scope, binding, mode])],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert select.select([process.stdout], [], [], 10)[0], 'Ownership subprocess did not respond'
    assert process.stdout.readline().strip() == 'owned'
    if mode != 'hold':
        _, errors = process.communicate(timeout=10)
        assert process.returncode == 37, errors
    return process


def paths(scope):
    folder = Path(scope['saved_als']).parent
    return folder, folder / '.workspace-owner-lock', folder / '.workspace-lock/owner.json'


@pytest.fixture
def accepted_terminal(monkeypatch):
    # Filesystem recovery is isolated here. Full C/B terminal validation is
    # independently exercised in test_native_midi_writer_qa.py.
    from pocket_music import native_midi

    releases = []
    monkeypatch.setattr(native_midi, 'validate_native_terminal', lambda handle, store: read_record(handle, store))
    monkeypatch.setattr(native_midi, 'release_native_host_lease', lambda handle, store: releases.append(handle))
    return releases


def test_qa_hard_exit_keeps_unknown_pending_and_only_explicit_reconcile_can_recover(tmp_path, accepted_terminal):
    scope, binding, _, source = fixture(tmp_path)
    original = snapshot(source.parent)
    child(scope, binding, 'crash')
    folder, stable, marker = paths(scope)
    prior_marker = json.loads(marker.read_bytes())
    prior_pending = state(scope)['native_pending']
    stable_identity = (stable.stat().st_dev, stable.stat().st_ino)
    inspected = native_candidates.candidate_inspect(scope['store_root'], scope['workspace_id'], 2)
    assert inspected['coverage']['workspace_lock_present'] is True
    with pytest.raises(PocketError):
        native_candidates.candidate_cancel(scope['store_root'], scope['workspace_id'], 2, 'ordinary-cancel')
    assert state(scope)['revision'] == 2
    handle = terminal({'pending': prior_pending}, scope['store_root'])
    args = {'store_root': scope['store_root'], 'workspace_id': scope['workspace_id'], 'expected_revision': 2,
            'terminal': handle, 'attribution': ATTRIBUTION, 'request_id': 'qa-process-recovery'}
    result = native_candidates.candidate_native_reconcile(**args)
    assert result['workspace'] == {'workspace_id': scope['workspace_id'], 'revision': 4, 'state': 'awaiting_native'}
    history = state(scope)['history']
    recoveries = [item for item in history if item['state'] == 'workspace_lock_recovered']
    assert len(recoveries) == 1
    recovery = read_record(recoveries[0]['evidence'], scope['store_root'])
    assert recovery['prior_owner'] == prior_marker
    assert recovery['native_outcome'] == 'not_inferred_from_process_lock'
    assert any(item.get('pending') == prior_pending for item in history)
    assert not (folder / '.workspace-lock').exists()
    assert stable_identity == (stable.stat().st_dev, stable.stat().st_ino)
    assert native_candidates.candidate_native_reconcile(**args) == result
    assert snapshot(source.parent) == original
    assert accepted_terminal == [handle, handle]


def test_qa_live_process_lock_cannot_be_stolen_even_by_explicit_terminal_reconcile(tmp_path, accepted_terminal):
    scope, binding, _, source = fixture(tmp_path)
    original = snapshot(source.parent)
    process = child(scope, binding, 'hold')
    try:
        before = state(scope)
        handle = terminal({'pending': before['native_pending']}, scope['store_root'])
        with pytest.raises(PocketError, match='owner|busy'):
            native_candidates.candidate_native_reconcile(scope['store_root'], scope['workspace_id'], 2,
                handle, ATTRIBUTION, 'qa-held-owner')
        assert state(scope) == before and accepted_terminal == []
        assert snapshot(source.parent) == original
    finally:
        process.communicate('release\n', timeout=10)


@pytest.mark.parametrize('attack', [
    'ownerless', 'stable_symlink', 'stable_hardlink', 'stable_replaced',
    'marker_symlink', 'marker_hardlink', 'marker_extra', 'directory_refilled',
    'token_changed_shape', 'wrong_workspace', 'wrong_inode',
])
def test_qa_unqualified_retained_owner_never_releases_unknown_workspace(tmp_path, accepted_terminal, attack):
    scope, binding, _, source = fixture(tmp_path)
    original = snapshot(source.parent)
    child(scope, binding, 'crash')
    folder, stable, marker = paths(scope)
    before = state(scope)
    value = json.loads(marker.read_bytes())
    if attack == 'ownerless':
        marker.unlink()
    elif attack in {'stable_symlink', 'marker_symlink'}:
        target = stable if attack == 'stable_symlink' else marker
        moved = folder / ('retained-' + target.name)
        target.rename(moved)
        target.symlink_to(moved)
    elif attack in {'stable_hardlink', 'marker_hardlink'}:
        target = stable if attack == 'stable_hardlink' else marker
        os.link(target, folder / ('aliased-' + target.name))
    elif attack == 'stable_replaced':
        stable.rename(folder / 'original-stable')
        stable.write_bytes(b'')
    elif attack == 'directory_refilled':
        (marker.parent / 'unrelated-owner.txt').write_text('preserve this')
    else:
        if attack == 'marker_extra':
            value['unknown'] = True
        elif attack == 'token_changed_shape':
            value['acquisition_token'] = 'not-a-token'
        elif attack == 'wrong_workspace':
            value['workspace_id'] = 'workspace-' + 'f' * 24
        else:
            value['stable_inode'] += 1
        marker.write_text(json.dumps(value))
    handle = terminal({'pending': before['native_pending']}, scope['store_root'])
    with pytest.raises(PocketError):
        native_candidates.candidate_native_reconcile(scope['store_root'], scope['workspace_id'], 2,
            handle, ATTRIBUTION, 'qa-unqualified-owner')
    assert state(scope) == before and accepted_terminal == []
    assert (folder / '.workspace-lock').exists()
    assert snapshot(source.parent) == original


def test_qa_process_death_before_pending_does_not_authorize_physical_recovery(tmp_path):
    scope, binding, _, _ = fixture(tmp_path)
    child(scope, binding, 'empty_crash')
    before = state(scope)
    with pytest.raises(PocketError, match='pending|terminal'), native_candidates._workspace(
            scope['store_root'], scope['workspace_id'], 1, recover_native=True):
        pytest.fail('Non-pending owner was reclaimed')
    assert state(scope) == before and paths(scope)[2].exists()


def test_qa_bad_terminal_after_valid_physical_recovery_keeps_native_quarantine(tmp_path, accepted_terminal):
    scope, binding, _, source = fixture(tmp_path)
    original = snapshot(source.parent)
    child(scope, binding, 'crash')
    pending = state(scope)['native_pending']
    record = read_record(terminal({'pending': pending}, scope['store_root']), scope['store_root'])
    record['request_key'] = 'f' * 64
    handle = put_record(record, scope['store_root'])
    with pytest.raises(PocketError):
        native_candidates.candidate_native_reconcile(scope['store_root'], scope['workspace_id'], 2,
            handle, ATTRIBUTION, 'qa-wrong-terminal')
    after = state(scope)
    assert after['state'] in {'host_pending', 'outcome_unknown'} and after['native_pending'] == pending
    assert 'last_native_terminal' not in after and accepted_terminal == []
    assert snapshot(source.parent) == original


@pytest.mark.parametrize('attack', ['refill_directory', 'replace_stable', 'modify_marker'])
def test_qa_owner_changes_during_active_context_are_retained_for_inspection(tmp_path, attack):
    scope, _, _, _ = fixture(tmp_path)
    folder, stable, marker = paths(scope)
    with pytest.raises(PocketError), native_candidates.native_workspace(**scope) as session:
        if attack == 'refill_directory':
            (marker.parent / 'other-owner').write_text('preserve')
        elif attack == 'replace_stable':
            stable.rename(folder / 'old-stable')
            stable.write_bytes(b'')
        else:
            value = json.loads(marker.read_bytes())
            value['acquisition_token'] = 'f' * 32
            marker.write_text(json.dumps(value))
        session._check()
    assert marker.parent.exists()
