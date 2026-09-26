# SPDX-License-Identifier: AGPL-3.0-only
"""Independent candidate/runtime evidence attacks; synthetic files only."""
import copy
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import test_native_midi as reader_fixtures
from test_candidate_runtime_identity import runtime_fixture, write_xml
from test_native_candidates import snapshot

from pocket_music.artifact_store import put_record, read_record
from pocket_music.assets import sha256_file
from pocket_music.errors import PocketError
from pocket_music.native_candidates import candidate_seal, load_candidate_record, validate_candidate


@pytest.fixture
def bridge(tmp_path):
    yield from reader_fixtures.bridge.__wrapped__(tmp_path)

def relocated(fixture, tmp_path):
    result = candidate_seal(**fixture['args'])
    destination = tmp_path / 'only-immutable-artifacts'
    shutil.copytree(Path(fixture['store']) / 'artifacts', destination / 'artifacts')
    return result, str(destination)


def test_qa_runtime_qualification_preserves_original_and_distinguishes_historical_empty_observation(tmp_path, bridge):
    f = runtime_fixture(tmp_path, bridge, empty_observed=True)
    immutable_preparation = copy.deepcopy(read_record(f['prepared']['artifacts']['preparation'], f['store']))
    sealed, store = relocated(f, tmp_path)
    trial = load_candidate_record(sealed['artifacts']['candidate'], store)
    assert snapshot(f['source'].parent) == f['original']
    assert read_record(f['prepared']['artifacts']['preparation'], store) == immutable_preparation
    assert trial['preservation']['native_notes_matched'] == 1
    qualification = trial['preservation']['runtime_identity']
    assert qualification['changed_fields'] == 7
    assert qualification['current_session_identity'] is False
    assert qualification['observation_source_preservation']['owned_note_content'] == 'not_compared_to_final_material'
    assert trial['listening'] == 'not_reviewed' and trial['provider_native_observation'] is False


@pytest.mark.parametrize('field,value', [('Velocity', '77'), ('OffVelocity', '63'), ('Duration', '0.5'),
                                        ('Time', '0.25'), ('Probability', '0.5'), ('VelocityDeviation', '1')])
def test_qa_runtime_normalization_cannot_hide_a_final_note_change(tmp_path, bridge, field, value):
    f = runtime_fixture(tmp_path, bridge)
    f['final'].find('.//MidiNoteEvent').set(field, value)
    write_xml(f['saved'], f['final'])
    f['args']['expected_sha256'] = f['args']['native_report']['saved_als_sha256'] = sha256_file(f['saved'])
    with pytest.raises(PocketError):
        candidate_seal(**f['args'])
    assert snapshot(f['source'].parent) == f['original']


@pytest.mark.parametrize('path', [
    'LiveSet/Tracks/AudioTrack/DeviceChain/Mixer/Pan/Manual',
    'LiveSet/Tracks/AudioTrack/DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events/AudioClip/IsWarped',
])
def test_qa_observed_source_field_drift_is_not_forgiven_when_final_restores_it(tmp_path, bridge, path):
    def change(root):
        node = root.find(path)
        if node is None:
            ET.SubElement(root.find('LiveSet/Tracks/AudioTrack'), 'UnknownSourceMutation', Value='1')
        else:
            node.set('Value', ('false' if node.get('Value') == 'true' else 'true') if node.tag == 'IsWarped' else '0.7')
    f = runtime_fixture(tmp_path, bridge, observed_mutation=change)
    with pytest.raises(PocketError, match='protected source|Unknown'):
        candidate_seal(**f['args'])


@pytest.mark.parametrize('schema_field', ['raw_notes', 'saved_als'])
def test_qa_relocated_runtime_evidence_rejects_opaque_artifact_family_substitution(tmp_path, bridge, schema_field):
    f = runtime_fixture(tmp_path, bridge)
    sealed, store = relocated(f, tmp_path)
    trial = read_record(sealed['artifacts']['candidate'], store)
    observation = read_record(trial['runtime_identity_observation'], store)
    handle = observation['raw_notes'] if schema_field == 'raw_notes' else observation['saved_binding']['saved_als']
    handle['artifact_schema'] = 'pocket.unrelated-opaque-data/v1'
    trial['runtime_identity_observation'] = put_record(observation, store)
    tampered = put_record(trial, store)
    with pytest.raises(PocketError, match='schema|artifact|saved-set|observation|runtime|preservation'):
        validate_candidate(tampered, store)


def test_qa_runtime_full_source_binding_cannot_be_replaced_by_matching_note_values(tmp_path, bridge):
    f = runtime_fixture(tmp_path, bridge)
    sealed, store = relocated(f, tmp_path)
    trial = read_record(sealed['artifacts']['candidate'], store)
    observation = read_record(trial['runtime_identity_observation'], store)
    # It is impossible to move the evidence to a same-named sibling workspace
    # merely by changing a convenience path: full journal/host binding must agree.
    observation['saved_binding']['path'] = '/somewhere-else/' + Path(observation['saved_binding']['path']).parent.name + '/candidate.als'
    trial['runtime_identity_observation'] = put_record(observation, store)
    with pytest.raises(PocketError):
        validate_candidate(put_record(trial, store), store)


def test_qa_runtime_evidence_does_not_authorize_extra_instrument_or_private_asset(tmp_path, bridge):
    f = runtime_fixture(tmp_path, bridge)
    operator = f['final'].find('LiveSet/Tracks/MidiTrack/DeviceChain/DeviceChain/Devices/Operator')
    sample = ET.SubElement(operator, 'SampleRef')
    reference = ET.SubElement(sample, 'FileRef')
    ET.SubElement(reference, 'Path', Value='/outside/uncollected.wav')
    write_xml(f['saved'], f['final'])
    f['args']['expected_sha256'] = f['args']['native_report']['saved_als_sha256'] = sha256_file(f['saved'])
    with pytest.raises(PocketError, match='dependencies'):
        candidate_seal(**f['args'])
