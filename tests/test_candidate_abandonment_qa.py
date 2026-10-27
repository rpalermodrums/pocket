# SPDX-License-Identifier: AGPL-3.0-only
"""Independent supervised abandonment with actual providers and synthetic LiveAPI."""
import json
import shutil
from pathlib import Path

import pytest
import test_native_midi_writer_qa as writer_qa
from test_native_candidates import snapshot

from pocket_music import native_candidates, native_midi
from pocket_music.artifact_store import read_record
from pocket_music.assets import sha256_file
from pocket_music.errors import PocketError


@pytest.fixture
def writer_bridge(tmp_path):
    yield from writer_qa.writer_bridge.__wrapped__(tmp_path)


@pytest.fixture
def abandonment_setup(tmp_path, writer_bridge, monkeypatch):
    args, live, source, original, _ = writer_qa.public_writer_setup(tmp_path, writer_bridge, monkeypatch, 'lost_result')
    uncertain = native_midi.native_midi_write(**{**args, 'timeout_seconds': .1})
    assert uncertain['status'] == 'outcome_unknown'
    assert len(live['log'].read_text().splitlines()) == 1
    fresh_path = tmp_path / 'fresh-other-project/Other.als'
    fresh_path.parent.mkdir()
    shutil.copyfile(args['saved_als'], fresh_path)
    other = writer_bridge(saved_input=fresh_path)
    observed = native_midi.native_midi_read(args['store_root'], 'qa-fresh-closure-observation',
        {'location': 'arrangement', 'track_index': 1, 'clip_index': 0},
        saved_binding={'saved_als': str(fresh_path), 'expected_sha256': sha256_file(fresh_path),
                       'track_id': '99', 'clip_id': '1'}, bridge_dir=str(other['directory']))
    old_state = json.loads(live['state'].read_bytes())
    abandon = {'store_root': args['store_root'], 'workspace_id': args['workspace_id'],
        'expected_revision': old_state['revision'], 'pending': old_state['native_pending'],
        'fresh_observation': observed['artifacts']['observation'], 'request_id': 'qa-abandon-unknown',
        'bridge_dir': str(live['directory']), 'attribution': {
            'actor': 'Synthetic supervised fixture', 'actor_kind': 'agent',
            'observed_at': read_record(observed['artifacts']['observation'], args['store_root'])['read_ended_at'],
            'reason': 'Synthetic old process is explicitly stopped; no real native session is controlled',
            'all_old_live_max_instances_stopped': True, 'old_writer_unloaded': True,
            'old_candidate_closed_without_saving': True, 'no_native_dispatch_in_flight': True}}
    return abandon, args, live, other, source, original


def stop_synthetic_old_process(live):
    live['process'].terminate()
    live['process'].communicate(timeout=5)
    assert live['process'].returncode is not None


def test_qa_abandonment_requires_old_bridge_unavailable_even_with_explicit_attribution(abandonment_setup):
    abandon, _, live, _, source, original = abandonment_setup
    state_before = live['state'].read_bytes()
    lease_before = live['host_lease'].read_bytes()
    with pytest.raises(PocketError, match='healthy'):
        native_candidates.candidate_native_abandon(**abandon)
    assert live['state'].read_bytes() == state_before
    assert live['host_lease'].read_bytes() == lease_before
    assert snapshot(source.parent) == original


def test_qa_abandonment_is_permanent_unknown_preserves_pending_and_replays_without_native_dispatch(abandonment_setup):
    abandon, args, live, _, source, original = abandonment_setup
    stop_synthetic_old_process(live)
    saved_before = Path(args['saved_als']).read_bytes()
    result = native_candidates.candidate_native_abandon(**abandon)
    assert result['status'] == 'ok'
    assert result['workspace']['state'] == 'abandoned_native_unknown'
    assert result['coverage']['native_outcome'] == 'unknown'
    assert result['coverage']['human_listening'] == 'not_established'
    assert result['coverage']['sealable'] is False and result['coverage']['resumable'] is False
    record = read_record(result['artifacts']['abandonment'], args['store_root'])
    assert record['pending'] == abandon['pending'] and record['outcome'] == 'unknown'
    assert record['policy'] == 'permanently_unsealable_no_resume'
    assert record['source_preservation']['closure'] == 'attributed_supervision_not_process_death_proof'
    final_state = json.loads(live['state'].read_bytes())
    assert final_state['native_pending'] == abandon['pending']
    assert 'last_native_terminal' not in final_state and not live['host_lease'].exists()
    assert native_candidates.candidate_native_abandon(**abandon) == result
    with pytest.raises(PocketError):
        native_candidates.candidate_cancel(args['store_root'], args['workspace_id'], final_state['revision'], 'qa-abandoned-cancel')
    with pytest.raises(PocketError), native_candidates.native_workspace(args['store_root'], args['workspace_id'],
            final_state['revision'], args['saved_als'], args['expected_sha256']):
        pytest.fail('Abandoned native workspace resumed')
    assert Path(args['saved_als']).read_bytes() == saved_before
    assert snapshot(source.parent) == original
    assert len(live['log'].read_text().splitlines()) == 1


@pytest.mark.parametrize('mutation', ['missing_confirmation', 'same_session', 'changed_old_saved', 'different_lease'])
def test_qa_abandonment_refuses_unqualified_closure_or_changed_immutable_scope(abandonment_setup, mutation):
    abandon, args, live, _, source, original = abandonment_setup
    stop_synthetic_old_process(live)
    if mutation == 'missing_confirmation':
        abandon['attribution']['no_native_dispatch_in_flight'] = False
    elif mutation == 'same_session':
        abandon['fresh_observation'] = args['expected_observation']
    elif mutation == 'changed_old_saved':
        path = Path(args['saved_als'])
        path.write_bytes(path.read_bytes() + b'changed')
    else:
        lease = json.loads(live['host_lease'].read_bytes())
        lease['request_key'] = 'f' * 64
        live['host_lease'].write_text(json.dumps(lease))
    before = live['state'].read_bytes()
    lease_before = live['host_lease'].read_bytes()
    with pytest.raises(PocketError):
        native_candidates.candidate_native_abandon(**abandon)
    assert live['state'].read_bytes() == before and live['host_lease'].read_bytes() == lease_before
    assert snapshot(source.parent) == original
