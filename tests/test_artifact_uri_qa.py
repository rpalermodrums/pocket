"""Independent canonical artifact URI and unchanged integrity checks."""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from test_native_candidates import prepare, seal_args, snapshot

from pocket_music.artifact_store import (
    _verify_handles,
    put_bytes,
    put_record,
    read_bytes,
    read_record,
    receipt,
    run_request,
)
from pocket_music.errors import PocketError
from pocket_music.native_candidates import candidate_seal, load_candidate_record, validate_candidate


def artifact(tmp_path, filename='record.json'):
    payload = b'{"schema":"qa.uri-record/v1","value":"preserved"}'
    handle = put_bytes(payload, tmp_path, filename, 'qa.uri-record/v1')
    return handle, payload


def alias(uri, kind, store):
    _, sha, filename = uri.split('/')
    values = {
        'double-before-hash': f'artifacts//{sha}/{filename}',
        'double-before-name': f'artifacts/{sha}//{filename}',
        'long-separators': f'artifacts/{"/" * 10000}{sha}/{"/" * 10000}{filename}',
        'leading-dot': f'./{uri}',
        'interior-dot': f'artifacts/./{sha}/{filename}',
        'filename-dot': f'artifacts/{sha}/./{filename}',
        'trailing-slash': uri + '/',
        'trailing-dot': uri + '/.',
        'parent-traversal': f'artifacts/{sha}/../{sha}/{filename}',
        'absolute': str(store / uri),
        'absolute-double': '//' + str(store / uri).lstrip('/'),
        'backslash': uri.replace('/', '\\'),
        'empty': '',
        'nul': uri + '\0',
        'bytes': uri.encode(),
        'none': None,
    }
    return values[kind]


@pytest.mark.parametrize('kind', [
    'double-before-hash', 'double-before-name', 'long-separators', 'leading-dot', 'interior-dot',
    'filename-dot', 'trailing-slash', 'trailing-dot', 'parent-traversal', 'absolute',
    'absolute-double', 'backslash', 'empty', 'nul', 'bytes', 'none',
])
def test_qa_uri_aliases_fail_as_domain_errors_and_preserve_files(tmp_path, kind):
    handle, payload = artifact(tmp_path)
    changed = {**handle, 'artifact_uri': alias(handle['artifact_uri'], kind, tmp_path)}
    before = snapshot(tmp_path)
    for operation in [read_bytes, read_record]:
        with pytest.raises(PocketError):
            operation(changed, tmp_path)
    assert read_bytes(handle, tmp_path) == payload
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('filename', ['record.json', 'clip.v1.mid', 'A-name_2.midi', 'a..bin', 'Z' * 128])
def test_qa_every_supported_filename_keeps_exact_uri_bytes_and_relocation(tmp_path, filename):
    original = tmp_path / 'original'
    handle, payload = artifact(original, filename)
    assert handle['artifact_uri'] == f'artifacts/{hashlib.sha256(payload).hexdigest()}/{filename}'
    assert read_bytes(handle, original) == payload
    moved = tmp_path / 'moved'
    shutil.copytree(original, moved)
    assert read_bytes(handle, moved) == payload
    _verify_handles({'first': handle, 'second': copy.deepcopy(handle)}, moved)
    assert (original / handle['artifact_uri']).stat().st_ino != (moved / handle['artifact_uri']).stat().st_ino


def test_qa_nested_alias_cannot_be_hidden_by_an_already_valid_handle(tmp_path):
    valid, _ = artifact(tmp_path)
    aliased = {**valid, 'artifact_uri': valid['artifact_uri'].replace('/', '//')}
    parent = put_record({'schema': 'qa.uri-parent/v1', 'evidence': [valid, aliased]}, tmp_path)
    before = snapshot(tmp_path)
    with pytest.raises(PocketError):
        _verify_handles(parent, tmp_path)
    assert snapshot(tmp_path) == before


def test_qa_successful_request_replay_rejects_nested_alias_without_redispatch(tmp_path):
    valid, _ = artifact(tmp_path)
    result = run_request(tmp_path, 'canonical-replay', 'qa-uri', {},
                         lambda: receipt(artifacts={'record': valid}))
    journal_path = tmp_path / 'requests/canonical-replay/journal.json'
    journal = json.loads(journal_path.read_bytes())
    # Retain a self-consistent historical receipt whose only bad claim is a URI
    # alias; the shared verifier must still check that entire receipt graph.
    journal['receipt']['artifacts']['record']['artifact_uri'] = valid['artifact_uri'].replace('/', '//')
    from pocket_music.artifact_store import digest
    journal['receipt_sha256'] = digest(journal['receipt'])
    journal_path.write_text(json.dumps(journal))
    before = snapshot(tmp_path)
    with pytest.raises(PocketError):
        run_request(tmp_path, 'canonical-replay', 'qa-uri', {}, lambda: pytest.fail('redispatched'))
    assert snapshot(tmp_path) == before
    assert read_bytes(result['artifacts']['record'], tmp_path)


def test_qa_canonical_uri_does_not_weaken_hash_or_address_validation(tmp_path):
    handle, payload = artifact(tmp_path)
    before = snapshot(tmp_path)
    for changed in [
        {**handle, 'sha256': '0' * 64},
        {**handle, 'artifact_uri': handle['artifact_uri'].replace(handle['sha256'], '0' * 64)},
        {**handle, 'schema': 'qa.not-handle/v1'},
        {**handle, 'extra': 'not allowed'},
    ]:
        with pytest.raises(PocketError):
            read_bytes(changed, tmp_path)
    assert snapshot(tmp_path) == before
    path = tmp_path / handle['artifact_uri']
    path.write_bytes(payload + b'changed')
    tampered = snapshot(tmp_path)
    with pytest.raises(PocketError, match='integrity'):
        read_bytes(handle, tmp_path)
    assert snapshot(tmp_path) == tampered


@pytest.mark.parametrize('level', ['file', 'hash-directory', 'artifacts-directory'])
def test_qa_canonical_spelling_still_refuses_symlink_aliases(tmp_path, level):
    store = tmp_path / 'store'
    handle, _ = artifact(store)
    path = store / handle['artifact_uri']
    selected = path if level == 'file' else path.parent if level == 'hash-directory' else store / 'artifacts'
    destination = tmp_path / 'retained-outside'
    selected.rename(destination)
    selected.symlink_to(destination, target_is_directory=destination.is_dir())
    before = destination.read_bytes() if destination.is_file() else snapshot(destination)
    with pytest.raises(PocketError, match='symlink|escapes'):
        read_bytes(handle, store)
    after = destination.read_bytes() if destination.is_file() else snapshot(destination)
    assert after == before and selected.is_symlink()


def test_qa_candidate_public_receipt_cannot_expand_through_uri_alias(tmp_path):
    prepared, args, _ = prepare(tmp_path)
    sealed = candidate_seal(**seal_args(prepared, args['store_root']))
    candidate = sealed['artifacts']['candidate']
    before = snapshot(tmp_path)
    valid = validate_candidate(candidate, args['store_root'])
    assert len((json.dumps(valid, indent=2) + '\n').encode()) < 4096
    changed = {**candidate, 'artifact_uri': alias(candidate['artifact_uri'], 'long-separators',
                                               Path(args['store_root']))}
    for operation in [load_candidate_record, validate_candidate]:
        with pytest.raises(PocketError):
            operation(changed, args['store_root'])
    assert validate_candidate(candidate, args['store_root']) == valid
    assert snapshot(tmp_path) == before
