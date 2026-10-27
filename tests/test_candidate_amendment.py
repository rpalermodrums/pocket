# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic amendment contracts; none of these reports are native evidence."""
import copy
import gzip
import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from test_native_candidates import (
    CONTEXT,
    imported_note_fixture,
    material_fixture,
    prepare,
    seal_args,
    snapshot,
    source_fixture,
)
from test_thread import value

from pocket_music.artifact_store import digest, put_record, read_bytes, read_record
from pocket_music.assets import sha256_file
from pocket_music.errors import PocketError
from pocket_music.material import finalize_material, load_material, make_note, material_digest, material_query
from pocket_music.midi_edit import midi_transform
from pocket_music.native_candidates import candidate_prepare, candidate_seal, load_candidate_record


def fixture(tmp_path, *, imported=False):
    store = str(tmp_path / 'store')
    if imported:
        base_handle, midi = imported_note_fixture(tmp_path)
    else:
        base_handle, midi = material_fixture(store), None
        base = read_record(base_handle, store)
        base['notes'].append(make_note('unselected-note', 2, 1, 50, 61, release=39))
        base['clips'][0]['note_ids'].append('unselected-note')
        base_handle = put_record(finalize_material(base), store)
    source = source_fixture(tmp_path)
    original = snapshot(source.parent)
    prepared = candidate_prepare(str(source), sha256_file(source), store, 'prepare-amendment',
        mode='with_material', material=base_handle, context=CONTEXT,
        layer={'track_name': 'Pocket Layer', 'instrument_device': 'Operator'})
    args = seal_args(prepared, store, with_material=True)
    path = Path(args['saved_als'])
    tree = ET.fromstring(gzip.decompress(path.read_bytes()))
    if not imported:
        key = ET.SubElement(tree.find('.//MidiClip/Notes/KeyTracks'), 'KeyTrack', Id='1')
        value(key, 'MidiKey', 50)
        ET.SubElement(ET.SubElement(key, 'Notes'), 'MidiNoteEvent', Time='2', Duration='1',
                      Velocity='61', OffVelocity='39')
    tree.find('.//MidiNoteEvent').set('Velocity', '89')
    path.write_bytes(gzip.compress(ET.tostring(tree), mtime=0))
    args['expected_sha256'] = args['native_report']['saved_als_sha256'] = sha256_file(path)
    base = read_record(base_handle, store)
    child = copy.deepcopy(base)
    child['parent_revision'] = base['revision_sha256']
    child['notes'][0]['velocity']['value'] = 89
    child['provenance']['last_edit'] = {'actor': 'External synthetic fixture', 'operation': 'velocity'}
    child = finalize_material(child)
    args['final_material'] = put_record(child, store)
    return args, prepared, base, child, source, original, midi


def test_external_child_seals_and_retains_preparation_and_locked_fields(tmp_path):
    args, prepared, base, child, source, original, _ = fixture(tmp_path)
    before_preparation = read_record(prepared['artifacts']['preparation'], args['store_root'])
    result = candidate_seal(**args)
    trial = load_candidate_record(result['artifacts']['candidate'], args['store_root'])
    amendment = read_record(trial['material_amendment'], args['store_root'])
    assert trial['material'] == args['final_material']
    assert read_record(prepared['artifacts']['preparation'], args['store_root']) == before_preparation
    assert amendment['parent'] == before_preparation['material']
    assert amendment['child'] == args['final_material']
    assert amendment['changed_note_id'] == base['notes'][0]['id']
    assert amendment['velocity_before'] == {'value': 76, 'domain': 'midi1_7bit'}
    assert amendment['velocity_after'] == {'value': 89, 'domain': 'midi1_7bit'}
    assert amendment['invariants']['native_note_id_retention'] == 'not_established'
    assert trial['listening'] == 'not_reviewed' and trial['provider_native_observation'] is False
    assert load_material(trial['material'], args['store_root']) == child
    assert candidate_seal(**args) == result
    assert snapshot(source.parent) == original


def test_public_transform_child_composes_without_required_transform_history(tmp_path):
    args, prepared, base, _, _, _, _ = fixture(tmp_path)
    material = read_record(prepared['artifacts']['preparation'], args['store_root'])['material']
    selection = material_query(material, args['store_root'], query='events',
                               selection={'note_ids': [base['notes'][0]['id']]})['selection']
    edited = midi_transform(material, selection, [{'op': 'velocity', 'value': 89}], args['store_root'],
        'symbolic-edit', locks={'outside_selection': 'all', 'selected_fields': [
            'pitch', 'onset', 'duration', 'release_velocity', 'source_binding', 'expression_shape']})
    args['final_material'] = edited['material']
    trial = load_candidate_record(candidate_seal(**args)['artifacts']['candidate'], args['store_root'])
    assert trial['material'] == edited['material']


@pytest.mark.parametrize('attack', [
    'pitch', 'release', 'duration', 'onset', 'channel', 'mute', 'voice', 'source_binding',
    'derived_from', 'two_velocities', 'zero_velocities', 'note_order', 'clip_membership_order',
    'clip_length', 'source_list', 'coverage', 'provenance', 'parent', 'logical_identity',
])
def test_all_other_fields_and_lineage_are_locked(tmp_path, attack):
    args, _, base, child, source, original, _ = fixture(tmp_path)
    note = child['notes'][0]
    if attack == 'pitch':
        note['pitch']['midi_note'] += 1
    elif attack == 'release':
        note['release_velocity']['value'] = 12
    elif attack == 'duration':
        note['duration_qn']['n'] = 2
    elif attack == 'onset':
        note['onset']['n'] = 1
    elif attack == 'channel':
        note['channel'] = 2
    elif attack == 'mute':
        note['mute'] = True
    elif attack == 'voice':
        note['voice_id'] = 'changed-voice'
    elif attack == 'source_binding':
        note['source_binding'] = {'unknown': 'new source'}
    elif attack == 'derived_from':
        note['derived_from'] = ['invented-parent']
    elif attack == 'two_velocities':
        child['notes'][1]['velocity']['value'] = 90
    elif attack == 'zero_velocities':
        note['velocity'] = base['notes'][0]['velocity']
    elif attack == 'note_order':
        child['notes'].reverse()
    elif attack == 'clip_membership_order':
        child['clips'][0]['note_ids'].reverse()
    elif attack == 'clip_length':
        child['clips'][0]['length_qn']['n'] = 5
    elif attack == 'source_list':
        child['sources'].append({'kind': 'material', 'annotation': 'unbound source'})
    elif attack == 'coverage':
        child['coverage']['new_claim'] = True
    elif attack == 'provenance':
        child['provenance']['provider'] = 'replacement'
    elif attack == 'parent':
        child['parent_revision'] = '0' * 64
    else:
        child['material_id'] = 'another-material'
    args['final_material'] = put_record(finalize_material(child), args['store_root'])
    with pytest.raises(PocketError, match='Final material'):
        candidate_seal(**args)
    assert snapshot(source.parent) == original
    state = json.loads((Path(args['saved_als']).parent / 'workspace.json').read_text())
    assert state['state'] == 'awaiting_native' and state['revision'] == 1


def test_final_notes_must_match_actual_saved_native_bytes(tmp_path):
    args, _, _, child, _, _, _ = fixture(tmp_path)
    child['notes'][0]['velocity']['value'] = 88
    args['final_material'] = put_record(finalize_material(child), args['store_root'])
    with pytest.raises(PocketError, match='Saved native notes differ'):
        candidate_seal(**args)


def test_source_protection_is_not_weakened_by_amendment(tmp_path):
    args, _, _, _, _, _, _ = fixture(tmp_path)
    path = Path(args['saved_als'])
    tree = ET.fromstring(gzip.decompress(path.read_bytes()))
    tree.find('LiveSet/Tracks/AudioTrack/Name/EffectiveName').set('Value', 'changed-source')
    path.write_bytes(gzip.compress(ET.tostring(tree), mtime=0))
    args['expected_sha256'] = args['native_report']['saved_als_sha256'] = sha256_file(path)
    with pytest.raises(PocketError, match='protected source XML'):
        candidate_seal(**args)


def test_imported_raw_note_evidence_is_retained_despite_explicit_velocity_amendment(tmp_path):
    args, prepared, base, child, _, _, midi = fixture(tmp_path, imported=True)
    original_wire = midi.read_bytes()
    trial = load_candidate_record(candidate_seal(**args)['artifacts']['candidate'], args['store_root'])
    final = load_material(trial['material'], args['store_root'])
    assert base['events'] == child['events'] == final['events']
    assert base['notes'][0]['source_binding'] == final['notes'][0]['source_binding']
    assert read_bytes(final['sources'][0]['raw'], args['store_root']) == original_wire == midi.read_bytes()
    assert read_record(prepared['artifacts']['preparation'], args['store_root'])['material'] != trial['material']


def test_changed_imported_raw_evidence_cannot_hide_in_amendment(tmp_path):
    args, _, _, child, _, _, _ = fixture(tmp_path, imported=True)
    child['events'][0]['bytes'][-1] = 89
    args['final_material'] = put_record(finalize_material(child), args['store_root'])
    with pytest.raises(PocketError, match='locked structure, source evidence'):
        candidate_seal(**args)


def test_legacy_omitted_and_null_amendment_keep_original_request_identity(tmp_path):
    args, _, base, _, _, _, _ = fixture(tmp_path)
    args.pop('final_material')
    path = Path(args['saved_als'])
    tree = ET.fromstring(gzip.decompress(path.read_bytes()))
    tree.find('.//MidiNoteEvent').set('Velocity', str(base['notes'][0]['velocity']['value']))
    path.write_bytes(gzip.compress(ET.tostring(tree), mtime=0))
    args['expected_sha256'] = args['native_report']['saved_als_sha256'] = sha256_file(path)
    result = candidate_seal(**args)
    assert candidate_seal(**args, final_material=None) == result
    trial = load_candidate_record(result['artifacts']['candidate'], args['store_root'])
    assert 'material_amendment' not in trial
    old_inputs = {key: args[key] for key in ('workspace_id', 'expected_revision', 'saved_als',
        'expected_sha256', 'native_report', 'instrument_state')}
    journal = json.loads((Path(args['store_root']) / 'requests/seal/journal.json').read_text())
    assert journal['input_sha256'] == digest({'operation': 'candidate_seal', 'inputs': old_inputs})


@pytest.mark.parametrize('attack', ['report', 'remove', 'wrong_schema', 'material'])
def test_relocated_validator_recomputes_amendment_and_lineage(tmp_path, attack):
    args, _, base, _, _, _, _ = fixture(tmp_path)
    result = candidate_seal(**args)
    destination = tmp_path / 'relocated'
    shutil.copytree(Path(args['store_root']) / 'artifacts', destination / 'artifacts')
    trial = load_candidate_record(result['artifacts']['candidate'], str(destination))
    if attack == 'report':
        report = read_record(trial['material_amendment'], destination)
        report['invariants']['native_note_id_retention'] = 'verified'
        trial['material_amendment'] = put_record(report, destination)
    elif attack == 'remove':
        trial.pop('material_amendment')
    elif attack == 'wrong_schema':
        trial['material_amendment']['artifact_schema'] = 'pocket.unrelated/v1'
    else:
        trial['material'] = put_record(base, destination)
    forged = put_record(trial, destination)
    with pytest.raises(PocketError):
        load_candidate_record(forged, str(destination))


def test_amendment_retry_refuses_tampered_final_material(tmp_path):
    args, _, _, _, _, _, _ = fixture(tmp_path)
    candidate_seal(**args)
    (Path(args['store_root']) / args['final_material']['artifact_uri']).write_bytes(b'changed')
    with pytest.raises(PocketError, match='integrity'):
        candidate_seal(**args)


def test_direct_inline_final_material_is_rejected_before_workspace_publication(tmp_path):
    args, _, _, child, _, _, _ = fixture(tmp_path)
    args['final_material'] = child
    with pytest.raises(PocketError):
        candidate_seal(**args)
    assert json.loads((Path(args['saved_als']).parent / 'workspace.json').read_text())['state'] == 'awaiting_native'


def test_clone_only_cannot_gain_a_layer_via_final_material(tmp_path):
    prepared, preparation_args, _ = prepare(tmp_path)
    args = seal_args(prepared, preparation_args['store_root'])
    args['final_material'] = material_fixture(args['store_root'])
    with pytest.raises(PocketError, match='with_material'):
        candidate_seal(**args)


def test_malformed_amendment_provenance_fails_as_domain_error(tmp_path):
    args, _, _, child, _, _, _ = fixture(tmp_path)
    child['provenance'] = []
    child['revision_sha256'] = material_digest(child)
    args['final_material'] = put_record(child, args['store_root'])
    with pytest.raises(PocketError, match='provenance'):
        candidate_seal(**args)
