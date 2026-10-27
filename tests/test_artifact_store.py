# SPDX-License-Identifier: AGPL-3.0-only
import json
from pathlib import Path

import pytest

from pocket_music.artifact_store import digest, put_record, read_record, receipt, request_status, run_request
from pocket_music.errors import PocketError


def test_relocation_integrity_and_retry(tmp_path):
    root = tmp_path / 'store'
    handle = put_record({'schema': 'test/v1', 'value': 3}, root)
    result = run_request(root, 'one', 'test', {'value': 3},
                         lambda: receipt('one', artifacts={'record': handle}))
    assert run_request(root, 'one', 'test', {'value': 3}, lambda: pytest.fail('repeated')) == result
    with pytest.raises(PocketError, match='idempotency_conflict'):
        run_request(root, 'one', 'test', {'value': 4}, dict)
    moved = tmp_path / 'moved'
    root.rename(moved)
    assert read_record(handle, moved)['value'] == 3
    Path(moved, handle['artifact_uri']).write_bytes(b'changed')
    with pytest.raises(PocketError, match='integrity'):
        run_request(moved, 'one', 'test', {'value': 3}, dict)


def test_failed_interrupted_and_nonfinite(tmp_path):
    with pytest.raises(PocketError, match='finite'):
        digest({'value': float('nan')})
    def fail():
        raise RuntimeError('crash')
    with pytest.raises(RuntimeError):
        run_request(tmp_path, 'failed', 'test', {}, fail)
    journal = json.loads((tmp_path / 'requests/failed/journal.json').read_text())
    assert journal['state'] == 'failed'
    with pytest.raises(PocketError, match='did not complete'):
        run_request(tmp_path, 'failed', 'test', {}, dict)
    (tmp_path / 'requests/interrupted/lock').mkdir(parents=True)
    with pytest.raises(PocketError, match='interrupted'):
        run_request(tmp_path, 'interrupted', 'test', {}, dict)


def test_traversal_symlink_and_immutable_collision(tmp_path):
    handle = put_record({'schema': 'test/v1'}, tmp_path)
    with pytest.raises(PocketError):
        read_record({**handle, 'artifact_uri': '../outside'}, tmp_path)
    path = tmp_path / handle['artifact_uri']
    backup = tmp_path / 'original'
    path.rename(backup)
    path.symlink_to(backup)
    with pytest.raises(PocketError, match='symlink'):
        read_record(handle, tmp_path)
    with pytest.raises(PocketError, match='symlink'):
        put_record({'schema': 'test/v1'}, tmp_path)


def test_retry_checks_transitive_evidence_and_schema(tmp_path):
    evidence = put_record({'schema': 'test.evidence/v1', 'value': 3}, tmp_path)
    plan = put_record({'schema': 'test.plan/v1', 'evidence': [evidence, evidence]}, tmp_path)
    run_request(tmp_path, 'nested', 'test', {}, lambda: receipt(artifacts={'plan': plan}))
    (tmp_path / evidence['artifact_uri']).write_bytes(b'corrupted nested evidence')
    with pytest.raises(PocketError, match='integrity'):
        run_request(tmp_path, 'nested', 'test', {}, lambda: pytest.fail('redispatched'))


def test_public_request_status_does_not_claim_lock_ownership_or_external_state(tmp_path):
    assert request_status(str(tmp_path), 'missing')['status'] == 'needs_input'
    handle = put_record({'schema': 'test/v1'}, tmp_path)
    run_request(tmp_path, 'complete', 'test', {}, lambda: receipt(artifacts={'record': handle}))
    status = request_status(str(tmp_path), 'complete')
    assert status['status'] == 'ok'
    assert status['coverage']['external_state'] == 'requires_provider_revalidation'
    lock = tmp_path / 'requests/complete/lock'
    lock.mkdir()
    assert request_status(str(tmp_path), 'complete')['status'] == 'outcome_unknown'
    assert lock.exists()
    (tmp_path / handle['artifact_uri']).write_bytes(b'bad')
    with pytest.raises(PocketError, match='integrity'):
        request_status(str(tmp_path), 'complete')
