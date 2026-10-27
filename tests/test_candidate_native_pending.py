# SPDX-License-Identifier: AGPL-3.0-only
"""File lifecycle tests with a fake terminal validator, never native acceptance."""
import copy
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest
from test_native_candidates import prepare, seal_args, snapshot

from pocket_music import native_candidates
from pocket_music.artifact_store import put_bytes, put_record, read_record
from pocket_music.errors import PocketError
from pocket_music.native_candidates import (
    candidate_cancel,
    candidate_inspect,
    candidate_native_reconcile,
    candidate_seal,
    native_workspace,
)

ATTRIBUTION = {'actor': 'Synthetic lifecycle test', 'actor_kind': 'agent',
               'observed_at': '2026-09-16T00:00:00+00:00', 'reason': 'Verified fake terminal fixture only'}


@pytest.fixture
def terminal_validator(monkeypatch):
    module = types.ModuleType('pocket_music.native_midi')
    module.validate_native_terminal = lambda handle, store: read_record(handle, store)
    module.release_native_host_lease = lambda handle, store: None
    monkeypatch.setitem(sys.modules, 'pocket_music.native_midi', module)
    return module


def fixture(tmp_path):
    prepared, preparation, source = prepare(tmp_path, 'with_material')
    seal = seal_args(prepared, preparation['store_root'], with_material=True)
    path = Path(seal['saved_als'])
    scope = {key: seal[key] for key in ('store_root', 'workspace_id', 'expected_revision', 'saved_als')}
    scope['expected_sha256'] = seal['expected_sha256']
    saved = put_bytes(path.read_bytes(), scope['store_root'], 'observed.als', 'pocket.saved-set/v1')
    observation = put_record({'schema': 'pocket.native-midi-observation/v1', 'saved_als': saved,
                              'saved_als_sha256': saved['sha256'], 'notes': [],
                              'coverage': 'synthetic lifecycle fixture only'}, scope['store_root'])
    binding = {'request_id': 'native-one', 'request_key': '1' * 64, 'operation': 'insert_empty',
               'input_sha256': '2' * 64, 'session_nonce': '3' * 32, 'package_sha256': '4' * 64,
               'expected_observation': observation}
    return scope, binding, seal, source


def state(scope):
    return json.loads((Path(scope['saved_als']).parent / 'workspace.json').read_text())


def terminal(pending, store, *, outcome='verified_readback'):
    record = read_record(pending['pending'], store)
    payload = {key: record[key] for key in ('request_id', 'request_key', 'input_sha256', 'session_nonce',
        'package_sha256', 'operation', 'workspace_id', 'pending_revision', 'expected_observation', 'saved_als_sha256')}
    payload.update(schema='pocket.native-midi-terminal/v1', before_observation=record['expected_observation'],
        after_observation=record['expected_observation'] if outcome == 'verified_readback' else None,
        bridge_journal=put_record({'schema': 'pocket.fake-bridge-journal/v1', 'basis': 'synthetic'}, store),
        outcome=outcome, dispatch_count=1 if outcome == 'verified_readback' else 0)
    return put_record(payload, store)


def unknown(scope, binding):
    with native_workspace(**scope) as session:
        pending = session.begin(**binding)
    assert state(scope)['state'] == 'outcome_unknown'
    return pending


def reconcile_args(scope, handle):
    return {'store_root': scope['store_root'], 'workspace_id': scope['workspace_id'],
            'expected_revision': state(scope)['revision'], 'terminal': handle,
            'attribution': ATTRIBUTION, 'request_id': 'reconcile-one'}


def test_begin_is_durable_and_serializes_against_all_other_workspace_mutators(tmp_path):
    scope, binding, seal, source = fixture(tmp_path)
    original = snapshot(source.parent)
    with native_workspace(**scope) as session:
        pending = session.begin(**binding)
        observed = state(scope)
        assert observed['state'] == 'host_pending' and observed['revision'] == pending['pending_revision'] == 2
        assert read_record(observed['native_pending'], scope['store_root'])['request_key'] == binding['request_key']
        assert candidate_inspect(scope['store_root'], scope['workspace_id'], 2)['coverage']['workspace_lock_present'] is True
        for operation in [
            lambda: candidate_cancel(scope['store_root'], scope['workspace_id'], 2, 'cancel-during'),
            lambda: candidate_seal(**{**seal, 'expected_revision': 2, 'request_id': 'seal-during'}),
        ]:
            with pytest.raises(PocketError, match='busy or interrupted'):
                operation()
    assert not (Path(scope['saved_als']).parent / '.workspace-lock').exists()
    assert state(scope)['state'] == 'outcome_unknown' and state(scope)['revision'] == 3
    assert snapshot(source.parent) == original


def test_exception_retains_pending_after_context_lock_releases(tmp_path):
    scope, binding, seal, _ = fixture(tmp_path)
    with pytest.raises(TimeoutError), native_workspace(**scope) as session:
        session.begin(**binding)
        raise TimeoutError('reply lost after possible host dispatch')
    observed = candidate_inspect(scope['store_root'], scope['workspace_id'], 3)
    assert observed['workspace']['native_pending']
    assert observed['workspace']['native_pending_error'] == 'reply lost after possible host dispatch'
    for operation in [
        lambda: candidate_cancel(scope['store_root'], scope['workspace_id'], 3, 'cancel-after'),
        lambda: candidate_seal(**{**seal, 'expected_revision': 3}),
    ]:
        with pytest.raises(PocketError, match='pending or uncertain'):
            operation()
    with pytest.raises(PocketError, match='pending or uncertain'), native_workspace(**{**scope, 'expected_revision': 3}):
        pytest.fail('A second native writer acquired quarantined workspace')


def test_failure_before_begin_does_not_create_native_operation(tmp_path):
    scope, _, _, _ = fixture(tmp_path)
    before = state(scope)
    with pytest.raises(ValueError), native_workspace(**scope):
        raise ValueError('no host write was prepared')
    assert state(scope) == before


@pytest.mark.parametrize('outcome', ['verified_readback', 'refused_before_dispatch'])
def test_verified_terminal_finishes_and_preserves_sources(tmp_path, terminal_validator, outcome):
    scope, binding, seal, source = fixture(tmp_path)
    original = snapshot(source.parent)
    with native_workspace(**scope) as session:
        pending = session.begin(**binding)
        handle = terminal(pending, scope['store_root'], outcome=outcome)
        done = session.finish(handle)
    assert done['revision'] == 3 and state(scope)['state'] == 'awaiting_native'
    assert 'native_pending' not in state(scope)
    assert state(scope)['last_native_terminal'] == handle
    sealed = candidate_seal(**{**seal, 'expected_revision': 3})
    assert sealed['workspace']['state'] == 'sealed'
    assert snapshot(source.parent) == original


def test_missing_adapter_validator_cannot_release_quarantine(tmp_path, monkeypatch):
    module = types.ModuleType('pocket_music.native_midi')
    monkeypatch.setitem(sys.modules, 'pocket_music.native_midi', module)
    scope, binding, _, _ = fixture(tmp_path)
    with pytest.raises(PocketError, match='validator is unavailable'), native_workspace(**scope) as session:
        pending = session.begin(**binding)
        session.finish(terminal(pending, scope['store_root']))
    assert state(scope)['state'] == 'outcome_unknown'


@pytest.mark.parametrize('key,value', [
    ('request_id', 'another-request'), ('request_key', 'a' * 64), ('input_sha256', 'b' * 64),
    ('session_nonce', 'c' * 32), ('package_sha256', 'd' * 64), ('operation', 'set_velocity'),
    ('workspace_id', 'workspace-' + '0' * 24), ('pending_revision', 1),
    ('pending_revision', 2.0),
    ('saved_als_sha256', 'e' * 64), ('dispatch_count', True), ('outcome', 'outcome_unknown'),
])
def test_mismatched_or_nonterminal_evidence_never_releases(tmp_path, terminal_validator, key, value):
    scope, binding, _, _ = fixture(tmp_path)
    pending = unknown(scope, binding)
    handle = terminal(pending, scope['store_root'])
    record = read_record(handle, scope['store_root'])
    record[key] = value
    handle = put_record(record, scope['store_root'])
    with pytest.raises(PocketError):
        candidate_native_reconcile(**reconcile_args(scope, handle))
    assert state(scope)['state'] == 'outcome_unknown' and state(scope)['revision'] == 3


def test_public_reconciliation_is_attributed_idempotent_and_does_not_redispatch(tmp_path, terminal_validator):
    scope, binding, _, _ = fixture(tmp_path)
    pending = unknown(scope, binding)
    handle = terminal(pending, scope['store_root'])
    args = reconcile_args(scope, handle)
    result = candidate_native_reconcile(**args)
    assert result['workspace']['revision'] == 4
    assert result['coverage']['native_redispatched'] is False
    assert state(scope)['history'][-1]['reconciliation'] == ATTRIBUTION
    assert result['artifacts']['native_pending'] == pending['pending']
    assert candidate_native_reconcile(**args) == result


def test_reconcile_completes_exact_lease_cleanup_after_direct_finish(tmp_path, terminal_validator):
    scope, binding, _, _ = fixture(tmp_path)
    with native_workspace(**scope) as session:
        pending = session.begin(**binding)
        handle = terminal(pending, scope['store_root'])
        session.finish(handle)
    original = state(scope)
    releases = []
    terminal_validator.release_native_host_lease = lambda actual, store: releases.append(actual)
    args = reconcile_args(scope, handle)
    result = candidate_native_reconcile(**args)
    assert result['coverage']['terminal_previously_persisted'] is True
    assert result['workspace']['revision'] == 3
    assert state(scope) == original
    assert candidate_native_reconcile(**args) == result
    assert releases == [handle, handle]


def test_reconcile_replays_after_post_persist_lease_release_error(tmp_path, terminal_validator):
    scope, binding, _, _ = fixture(tmp_path)
    pending = unknown(scope, binding)
    handle = terminal(pending, scope['store_root'])
    args = reconcile_args(scope, handle)
    def failed_release(*_):
        raise PocketError('Synthetic release I/O failure')
    terminal_validator.release_native_host_lease = failed_release
    with pytest.raises(PocketError, match='release I/O'):
        candidate_native_reconcile(**args)
    assert state(scope)['state'] == 'awaiting_native'
    terminal_validator.release_native_host_lease = lambda actual, store: None
    assert candidate_native_reconcile(**args)['workspace']['revision'] == 4
    with pytest.raises(PocketError, match='idempotency_conflict'):
        candidate_native_reconcile(**{**args, 'attribution': {**ATTRIBUTION, 'reason': 'Changed inputs'}})


def test_reconciliation_requires_fresh_workspace_revision_and_no_stolen_lock(tmp_path, terminal_validator):
    scope, binding, _, _ = fixture(tmp_path)
    pending = unknown(scope, binding)
    args = reconcile_args(scope, terminal(pending, scope['store_root']))
    with pytest.raises(PocketError, match='Stale workspace'):
        candidate_native_reconcile(**{**args, 'expected_revision': 2, 'request_id': 'stale-reconcile'})
    lock = Path(scope['saved_als']).parent / '.workspace-lock'
    lock.mkdir()
    with pytest.raises(PocketError, match='busy or interrupted'):
        candidate_native_reconcile(**args)
    assert lock.exists() and state(scope)['state'] == 'outcome_unknown'


@pytest.mark.parametrize('what', ['saved_set', 'original_source', 'dependency'])
def test_changed_files_during_pending_are_not_adopted(tmp_path, terminal_validator, what):
    scope, binding, _, source = fixture(tmp_path)
    pending = unknown(scope, binding)
    handle = terminal(pending, scope['store_root'])
    chosen = {'saved_set': Path(scope['saved_als']), 'original_source': source,
              'dependency': next(Path(scope['saved_als']).parent.glob('Samples/Imported/*'))}[what]
    chosen.write_bytes(chosen.read_bytes() + b'concurrent external change')
    with pytest.raises(PocketError, match='changed'):
        candidate_native_reconcile(**reconcile_args(scope, handle))
    assert state(scope)['state'] == 'outcome_unknown'


def test_cached_reconcile_checks_nested_terminal_evidence(tmp_path, terminal_validator):
    scope, binding, _, _ = fixture(tmp_path)
    pending = unknown(scope, binding)
    handle = terminal(pending, scope['store_root'])
    args = reconcile_args(scope, handle)
    candidate_native_reconcile(**args)
    journal = read_record(handle, scope['store_root'])['bridge_journal']
    (Path(scope['store_root']) / journal['artifact_uri']).write_bytes(b'tampered')
    with pytest.raises(PocketError, match='integrity'):
        candidate_native_reconcile(**args)


def test_cached_reconcile_checks_original_pending_history(tmp_path, terminal_validator):
    scope, binding, _, _ = fixture(tmp_path)
    pending = unknown(scope, binding)
    args = reconcile_args(scope, terminal(pending, scope['store_root']))
    candidate_native_reconcile(**args)
    current = state(scope)
    original = read_record(pending['pending'], scope['store_root'])
    original['input_sha256'] = 'a' * 64
    replacement = put_record(original, scope['store_root'])
    next(entry for entry in current['history'] if entry['state'] == 'host_pending')['pending'] = replacement
    native_candidates._write_json(Path(scope['saved_als']).parent / 'workspace.json', current)
    with pytest.raises(PocketError, match='history'):
        candidate_native_reconcile(**args)


def test_containment_and_internal_symlink_refusal(tmp_path):
    scope, _, _, source = fixture(tmp_path)
    with pytest.raises(PocketError, match='inside'), native_workspace(**{**scope, 'saved_als': str(source)}):
        pytest.fail('Outside path accepted')
    alias = Path(scope['saved_als']).with_name('alias.als')
    alias.symlink_to(Path(scope['saved_als']).name)
    with pytest.raises(PocketError, match='symlink'), native_workspace(**{**scope, 'saved_als': str(alias)}):
        pytest.fail('Symlink accepted')


def test_mutable_native_set_cannot_share_writable_hard_links(tmp_path):
    scope, _, _, _ = fixture(tmp_path)
    os.link(scope['saved_als'], Path(scope['saved_als']).with_name('shared.als'))
    with pytest.raises(PocketError, match='shared hard links'), native_workspace(**scope):
        pytest.fail('Shared mutable inode accepted')


def test_native_scope_rechecks_original_material_evidence_before_begin(tmp_path):
    scope, _, _, _ = fixture(tmp_path)
    preparation = read_record(state(scope)['preparation'], scope['store_root'])
    material = preparation['material']
    (Path(scope['store_root']) / material['artifact_uri']).write_bytes(b'changed original material')
    with pytest.raises(PocketError, match='integrity'), native_workspace(**scope):
        pytest.fail('Damaged original material admitted to native write scope')
    assert state(scope)['state'] == 'awaiting_native' and state(scope)['revision'] == 1


def test_durable_state_is_written_before_begin_returns(tmp_path, monkeypatch):
    scope, binding, _, _ = fixture(tmp_path)
    observed = []
    fsync = os.fsync

    def witness(fd):
        observed.append(os.fstat(fd).st_mode)
        return fsync(fd)

    monkeypatch.setattr(native_candidates.os, 'fsync', witness)
    with native_workspace(**scope) as session:
        session.begin(**binding)
        assert len(observed) >= 3  # Immutable pending bytes plus workspace file and directory.
        assert state(scope)['state'] == 'host_pending'


def test_same_request_cannot_be_dispatched_twice_after_terminal(tmp_path, terminal_validator):
    scope, binding, _, _ = fixture(tmp_path)
    with native_workspace(**scope) as session:
        pending = session.begin(**binding)
        session.finish(terminal(pending, scope['store_root']))
    with (native_workspace(**{**scope, 'expected_revision': 3}) as session,
          pytest.raises(PocketError, match='already registered')):
        session.begin(**binding)
    assert state(scope)['state'] == 'awaiting_native'


def test_workspace_state_cannot_be_changed_behind_held_lock(tmp_path):
    scope, _, _, _ = fixture(tmp_path)
    with native_workspace(**scope) as session:
        altered = copy.deepcopy(state(scope))
        altered['revision'] += 1
        native_candidates._write_json(Path(scope['saved_als']).parent / 'workspace.json', altered)
        with pytest.raises(PocketError, match='changed while'):
            session._check()


def test_process_crash_retains_lock_and_durable_pending_but_inspection_still_works(tmp_path):
    scope, binding, _, _ = fixture(tmp_path)
    script = '''import json, os, sys
from pocket_music.native_candidates import native_workspace
scope, binding = json.loads(sys.argv[1])
with native_workspace(**scope) as session:
    session.begin(**binding)
    os._exit(23)
'''
    result = subprocess.run([sys.executable, '-c', script, json.dumps([scope, binding])],
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 23, result.stderr
    inspected = candidate_inspect(scope['store_root'], scope['workspace_id'], 2)
    assert inspected['workspace']['state'] == 'host_pending'
    assert inspected['coverage']['workspace_lock_present'] is True
    with pytest.raises(PocketError, match='busy or interrupted'):
        candidate_cancel(scope['store_root'], scope['workspace_id'], 2, 'cancel-crashed')
