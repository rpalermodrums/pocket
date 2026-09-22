"""Independent bounded candidate response and preservation checks; synthetic only."""
from __future__ import annotations

import copy
import json
import shutil
import tracemalloc
from pathlib import Path

import pytest
from test_native_candidates import prepare, seal_args, snapshot

from pocket_music import native_candidates as native
from pocket_music.artifact_store import put_record, read_bytes, read_record
from pocket_music.errors import PocketError


def workspace(tmp_path, rows):
    prepared, inputs, source = prepare(tmp_path)
    path = Path(prepared['workspace']['mutable_als_path']).parent / 'workspace.json'
    state = json.loads(path.read_bytes())
    state['history'] = rows
    path.write_text(json.dumps(state, ensure_ascii=False))
    args = {'store_root': inputs['store_root'], 'workspace_id': state['workspace_id'], 'expected_revision': 1}
    return args, path, state, source


def encoded_size(value):
    return len((json.dumps(value, indent=2, allow_nan=False) + '\n').encode())


def nested(depth):
    result = {'value': 'exact preserved expression description'}
    for _ in range(depth):
        result = {'child': result}
    return result


def test_qa_default_and_explicit_zero_omit_history_without_editing_any_evidence(tmp_path):
    rows = [{'revision': i, 'state': 'synthetic-native-history', 'detail': '🎼\n漢字' * 150}
            for i in range(450)]
    args, path, _, source = workspace(tmp_path, rows)
    before = snapshot(tmp_path)
    default = native.candidate_inspect(**args)
    assert default == native.candidate_inspect(**args, history_limit=0)
    assert default['history'] == {'schema': 'pocket.workspace-history-page/v1', 'total': 450,
                                  'rows': [], 'next_cursor': None}
    assert 'history' not in default['workspace']
    assert encoded_size(default) < 3072
    assert json.loads(path.read_bytes())['history'] == rows
    assert source.is_file() and snapshot(tmp_path) == before


def test_qa_unicode_and_nested_pages_are_exact_ordered_complete_and_bounded(tmp_path):
    rows = [{'revision': i, 'state': 'synthetic-native-history', 'detail': '🎼\n漢字' * 125,
             'nested': nested(8), 'ordinal': i} for i in range(19)]
    args, path, _, _ = workspace(tmp_path, rows)
    before = path.read_bytes()
    cursor = None
    collected = []
    cursors = set()
    for _ in range(20):
        result = native.candidate_inspect(**args, history_limit=20, history_cursor=cursor)
        page = result['history']
        assert 0 < len(page['rows']) <= 20
        assert encoded_size(result) <= 16 * 1024
        assert encoded_size(page['rows']) <= 12 * 1024
        collected.extend(copy.deepcopy(page['rows']))
        assert collected == rows[:len(collected)]
        if page['next_cursor'] is None:
            break
        cursor = page['next_cursor']
        assert cursor not in cursors
        cursors.add(cursor)
        # Mutating an output object cannot alter the stored history or next page.
        page['rows'][0]['nested']['child']['spoof'] = True
    else:
        pytest.fail('History cursor did not make bounded forward progress')
    assert collected == rows and path.read_bytes() == before


@pytest.mark.parametrize('kind', ['same_revision_history', 'same_revision_summary', 'revision', 'other_workspace'])
def test_qa_cursor_is_bound_to_complete_state_and_workspace(tmp_path, kind):
    rows = [{'revision': i, 'state': 'retained', 'detail': str(i)} for i in range(4)]
    args, path, state, _ = workspace(tmp_path, rows)
    cursor = native.candidate_inspect(**args, history_limit=1)['history']['next_cursor']
    if kind == 'same_revision_history':
        state['history'][-1]['detail'] = 'Changed outside first page'
    elif kind == 'same_revision_summary':
        state['native_pending_error'] = 'Concurrent status changed'
    elif kind == 'revision':
        state['revision'] += 1
        args['expected_revision'] += 1
    else:
        other = tmp_path / 'second'
        other.mkdir()
        args, path, state, _ = workspace(other, rows)
    path.write_text(json.dumps(state))
    before = snapshot(tmp_path)
    with pytest.raises(PocketError, match='same workspace revision and state'):
        native.candidate_inspect(**args, history_limit=1, history_cursor=cursor)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('revision', [True, False, 1.0, '1', -1, 2])
def test_qa_invalid_or_stale_revision_refuses_before_output(tmp_path, revision):
    args, _, _, _ = workspace(tmp_path, [{'state': 'retained'}])
    before = snapshot(tmp_path)
    with pytest.raises(PocketError, match='revision'):
        native.candidate_inspect(**{**args, 'expected_revision': revision}, history_limit=1)
    assert snapshot(tmp_path) == before


def test_qa_oversized_later_entry_cannot_disappear_or_be_truncated(tmp_path):
    rows = [{'state': 'small-first'}, {'state': 'large', 'detail': '🎹' * 1500}, {'state': 'small-last'}]
    args, path, _, _ = workspace(tmp_path, rows)
    before = snapshot(tmp_path)
    result = native.candidate_inspect(**args, history_limit=20)
    assert result['history']['rows'] == rows[:1]
    cursor = result['history']['next_cursor']
    assert cursor is not None
    with pytest.raises(PocketError, match='One workspace history entry'):
        native.candidate_inspect(**args, history_limit=20, history_cursor=cursor)
    assert json.loads(path.read_bytes())['history'] == rows
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('depth', [200, 600, 1500, 10000])
def test_qa_deep_retained_history_fails_as_domain_error_without_mutation(tmp_path, depth):
    args, path, state, _ = workspace(tmp_path, [])
    # Construct the wire bytes directly so the fixture encoder does not preclude
    # malformed input that the actual provider must bound independently.
    placeholder = 'SYNTHETIC-DEEP-HISTORY'
    state['history'] = [{'state': 'retained', 'detail': placeholder}]
    payload = json.dumps(state).replace(json.dumps(placeholder), '{"child":' * depth + '0' + '}' * depth)
    path.write_text(payload)
    before = snapshot(tmp_path)
    with pytest.raises(PocketError):
        native.candidate_inspect(**args, history_limit=1)
    assert snapshot(tmp_path) == before


def test_qa_oversized_current_error_refuses_explicitly_and_remains_retained(tmp_path):
    args, path, state, _ = workspace(tmp_path, [])
    state['native_pending_error'] = '🎵' * 2000
    path.write_text(json.dumps(state))
    before = snapshot(tmp_path)
    with pytest.raises(PocketError, match='16 KiB response bound'):
        native.candidate_inspect(**args)
    assert snapshot(tmp_path) == before


def sealed(tmp_path):
    prepared, inputs, _ = prepare(tmp_path)
    args = seal_args(prepared, inputs['store_root'])
    result = native.candidate_seal(**args)
    return args, result


def relocated(tmp_path, args):
    destination = tmp_path / 'portable-artifacts'
    shutil.copytree(Path(args['store_root']) / 'artifacts', destination / 'artifacts')
    return str(destination)


def test_qa_public_summary_loader_and_seal_replay_preserve_full_records_and_journals(tmp_path):
    args, result = sealed(tmp_path)
    candidate = result['artifacts']['candidate']
    store = args['store_root']
    before = snapshot(tmp_path)
    trial = native.load_candidate_record(candidate, store)
    public = native.validate_candidate(candidate, store)
    assert public['schema'] == 'pocket.operation-receipt/v1'
    assert public['artifacts'] == {'candidate': candidate, 'candidate_als': trial['candidate_als']}
    assert public['change_summary']['protected_source_xml'] == trial['preservation']['protected_source_xml']
    assert public['change_summary']['note_tolerance_qn'] == trial['preservation']['note_tolerance_qn']
    assert public['coverage']['human_listening'] == 'not_reviewed'
    assert public['coverage']['musical_decision'] is None
    assert public['coverage']['provider_native_observation'] is False
    assert 'normalization' not in public['change_summary']
    assert encoded_size(public) < 4096
    assert native.candidate_seal(**args) == result
    assert read_record(candidate, store) == trial
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('field,value', [
    ('listening', 'human-approved'), ('decision', {'action': 'keep'}),
    ('provider_native_observation', 0), ('saved_als_sha256', '0' * 64),
    ('preservation', {'protected_source_xml': 'matched'}),
])
def test_qa_public_and_internal_load_share_proof_invalidations(tmp_path, field, value):
    args, result = sealed(tmp_path)
    store = relocated(tmp_path, args)
    trial = read_record(result['artifacts']['candidate'], store)
    trial[field] = value
    altered = put_record(trial, store)
    before = snapshot(tmp_path)
    failures = []
    for operation in [native.load_candidate_record, native.validate_candidate]:
        with pytest.raises(PocketError) as error:
            operation(altered, store)
        failures.append(str(error.value))
    assert failures[0] == failures[1]
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('family', ['qa.not-a-saved-set/v1', '🎵' * 2000], ids=['wrong-family', 'oversized-family'])
def test_qa_opaque_saved_set_family_cannot_be_substituted_or_expand_public_summary(tmp_path, family):
    args, result = sealed(tmp_path)
    store = relocated(tmp_path, args)
    trial = read_record(result['artifacts']['candidate'], store)
    original_saved = read_bytes(trial['candidate_als'], store)
    trial['candidate_als']['artifact_schema'] = family
    altered = put_record(trial, store)
    before = snapshot(tmp_path)
    for operation in [native.load_candidate_record, native.validate_candidate]:
        with pytest.raises(PocketError):
            operation(altered, store)
    assert read_bytes(trial['candidate_als'], store) == original_saved
    assert snapshot(tmp_path) == before


def test_qa_deep_history_is_bounded_before_pretty_json_expansion(tmp_path):
    args, path, state, _ = workspace(tmp_path, [])
    state['history'] = [{'state': 'retained', 'detail': 'DEPTH-PLACEHOLDER'}]
    payload = json.dumps(state).replace('"DEPTH-PLACEHOLDER"', '{"child":' * 2000 + '0' + '}' * 2000)
    path.write_text(payload)
    before = path.read_bytes()
    assert len(payload) < 24 * 1024
    tracemalloc.start()
    try:
        with pytest.raises(PocketError):
            native.candidate_inspect(**args, history_limit=1)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # A small input must be rejected before quadratic indentation allocation.
    # This is deliberately a generous process-local budget, not a timing test.
    assert peak < 16 * 1024 * 1024
    assert path.read_bytes() == before
