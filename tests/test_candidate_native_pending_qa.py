# SPDX-License-Identifier: AGPL-3.0-only
"""Independent quarantine adversaries. Fake evidence is not native acceptance."""
import copy
import json
from pathlib import Path

import pytest
from test_native_candidates import prepare, seal_args, snapshot

from pocket_music import native_candidates, native_midi
from pocket_music.artifact_store import put_record, read_record
from pocket_music.errors import PocketError


def setup(tmp_path):
    prepared, args, source = prepare(tmp_path, 'with_material')
    seal = seal_args(prepared, args['store_root'], with_material=True)
    scope = {key: seal[key] for key in ('store_root', 'workspace_id', 'expected_revision', 'saved_als', 'expected_sha256')}
    observation = put_record({'schema': 'pocket.native-midi-observation/v1', 'fixture': 'independent pending QA'}, args['store_root'])
    binding = {'request_id': 'qa-native', 'request_key': 'a' * 64, 'operation': 'insert_empty',
               'input_sha256': 'b' * 64, 'session_nonce': 'c' * 32, 'package_sha256': 'd' * 64,
               'expected_observation': observation}
    return scope, binding, seal, source


def current(scope):
    return json.loads((Path(scope['saved_als']).parent / 'workspace.json').read_bytes())


def terminal(scope, pending, **changes):
    raw = read_record(pending['pending'], scope['store_root'])
    record = {key: raw[key] for key in ('request_id', 'request_key', 'input_sha256', 'session_nonce',
        'package_sha256', 'operation', 'workspace_id', 'pending_revision', 'expected_observation', 'saved_als_sha256')}
    record.update(schema='pocket.native-midi-terminal/v1', before_observation=raw['expected_observation'],
                  after_observation=None, bridge_journal=put_record({'schema': 'pocket.qa-journal/v1'}, scope['store_root']),
                  outcome='refused_before_dispatch', dispatch_count=0)
    record.update(changes)
    return put_record(record, scope['store_root'])


@pytest.fixture
def accepting_validator(monkeypatch):
    # A deliberate seam: this suite tests workspace release logic separately from
    # the adapter validator, whose availability remains a production gate.
    monkeypatch.setattr(native_midi, 'validate_native_terminal', lambda handle, store: read_record(handle, store), raising=False)


def test_qa_pending_without_terminal_never_certifies_saved_or_listened(tmp_path):
    scope, binding, seal, source = setup(tmp_path)
    source_before = snapshot(source.parent)
    with native_candidates.native_workspace(**scope) as transaction:
        pending = transaction.begin(**binding)
        assert read_record(pending['pending'], scope['store_root'])['saved_als_sha256'] == scope['expected_sha256']
    observed = native_candidates.candidate_inspect(scope['store_root'], scope['workspace_id'], 3)
    assert observed['workspace']['state'] == 'outcome_unknown'
    assert 'native_pending' in observed['workspace']
    for call in (lambda: native_candidates.candidate_cancel(scope['store_root'], scope['workspace_id'], 3, 'qa-cancel'),
                 lambda: native_candidates.candidate_seal(**{**seal, 'expected_revision': 3})):
        with pytest.raises(PocketError, match='pending'):
            call()
    assert snapshot(source.parent) == source_before


@pytest.mark.parametrize('changes', [
    {'outcome': 'refused_before_dispatch', 'dispatch_count': 1},
    {'outcome': 'verified_readback', 'dispatch_count': 1, 'after_observation': None},
    {'outcome': 'cancelled', 'dispatch_count': 0},
    {'dispatch_count': 0.0},
    {'unexpected': 'discard prior dispatch'},
])
def test_qa_terminal_contradictions_keep_original_pending(tmp_path, accepting_validator, changes):
    scope, binding, _, _ = setup(tmp_path)
    with native_candidates.native_workspace(**scope) as transaction:
        pending = transaction.begin(**binding)
        with pytest.raises(PocketError):
            transaction.finish(terminal(scope, pending, **changes))
        assert current(scope)['native_pending'] == pending['pending']
    assert current(scope)['state'] == 'outcome_unknown'


def test_qa_terminal_validator_must_return_exact_evidence(tmp_path, monkeypatch):
    scope, binding, _, _ = setup(tmp_path)
    monkeypatch.setattr(native_midi, 'validate_native_terminal', lambda handle, store: {'verified': True}, raising=False)
    with native_candidates.native_workspace(**scope) as transaction:
        pending = transaction.begin(**binding)
        with pytest.raises(PocketError, match='exact supplied evidence'):
            transaction.finish(terminal(scope, pending))
    assert current(scope)['state'] == 'outcome_unknown'


def test_qa_concurrent_saved_change_inside_terminal_validation_cannot_clear_pending(tmp_path, monkeypatch):
    scope, binding, _, _ = setup(tmp_path)

    def concurrent(handle, store):
        path = Path(scope['saved_als'])
        path.write_bytes(path.read_bytes() + b'concurrent save')
        return read_record(handle, store)

    monkeypatch.setattr(native_midi, 'validate_native_terminal', concurrent, raising=False)
    with native_candidates.native_workspace(**scope) as transaction:
        pending = transaction.begin(**binding)
        with pytest.raises(PocketError, match='changed'):
            transaction.finish(terminal(scope, pending))
    assert current(scope)['state'] == 'outcome_unknown'


def test_qa_saved_path_replaced_by_same_inode_symlink_remains_quarantined(tmp_path, accepting_validator):
    scope, binding, _, _ = setup(tmp_path)
    with native_candidates.native_workspace(**scope) as transaction:
        pending = transaction.begin(**binding)
        saved = Path(scope['saved_als'])
        outside = tmp_path / 'outside-owned-workspace.als'
        saved.rename(outside)
        saved.symlink_to(outside)
        with pytest.raises(PocketError, match='symlink|escapes|changed'):
            transaction.finish(terminal(scope, pending))
    assert current(scope)['state'] == 'outcome_unknown'


def test_qa_pending_request_key_cannot_be_reused_under_new_request_id(tmp_path, accepting_validator):
    scope, binding, _, _ = setup(tmp_path)
    with native_candidates.native_workspace(**scope) as transaction:
        pending = transaction.begin(**binding)
        transaction.finish(terminal(scope, pending))
    with (native_candidates.native_workspace(**{**scope, 'expected_revision': 3}) as transaction,
          pytest.raises(PocketError, match='already|identity|registered')):
        transaction.begin(**{**binding, 'request_id': 'different-public-request'})


def test_qa_pending_handle_graph_detects_reference_alias_schema(tmp_path):
    scope, binding, _, _ = setup(tmp_path)
    original = binding['expected_observation']
    alias = {**original, 'artifact_schema': 'pocket.forged/v1'}
    parent = put_record({'schema': 'pocket.native-midi-observation/v1', 'evidence': [original, alias]}, scope['store_root'])
    with (native_candidates.native_workspace(**scope) as transaction,
          pytest.raises(PocketError, match='schema')):
        transaction.begin(**{**binding, 'expected_observation': parent})
    assert current(scope)['revision'] == 1


def test_qa_failed_finish_does_not_overwrite_concurrently_changed_workspace(tmp_path, accepting_validator):
    scope, binding, _, _ = setup(tmp_path)
    changed = None
    with pytest.raises(PocketError, match='concurrent|changed'), native_candidates.native_workspace(**scope) as transaction:
        transaction.begin(**binding)
        changed = copy.deepcopy(current(scope))
        changed['revision'] += 7
        native_candidates._write_json(Path(scope['saved_als']).parent / 'workspace.json', changed)
    assert current(scope) == changed
    assert current(scope)['state'] == 'host_pending'
