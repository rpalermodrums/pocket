"""Independent final-material QA: exact edits and relocated evidence validation."""
import copy
import gzip
import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from test_native_candidates import CONTEXT, material_fixture, seal_args, snapshot, source_fixture
from test_thread import value

from pocket_music.artifact_store import put_bytes, put_record, read_bytes, read_record
from pocket_music.assets import sha256_file
from pocket_music.auditions import (
    attach_candidate_render,
    audition_plan,
    promote_candidate,
    validate_candidate_promotion,
)
from pocket_music.errors import PocketError
from pocket_music.material import finalize_material, make_note
from pocket_music.native_candidates import (
    candidate_prepare,
    candidate_seal,
    load_candidate_record,
    validate_candidate,
)


def setup(tmp_path, *, evidence=False):
    store = str(tmp_path / 'store')
    parent = read_record(material_fixture(store), store)
    parent['notes'].append(make_note('retained-second-note', 2, 1, 52, 67, release=29))
    parent['clips'][0]['note_ids'].append('retained-second-note')
    parent = finalize_material(parent)
    parent_handle = put_record(parent, store)
    source = source_fixture(tmp_path)
    source_before = snapshot(source.parent)
    preparation = candidate_prepare(str(source), sha256_file(source), store, 'prepare-independent-amendment',
        mode='with_material', material=parent_handle, context=CONTEXT,
        layer={'track_name': 'Pocket Layer', 'instrument_device': 'Operator'})
    args = seal_args(preparation, store, True)
    child = copy.deepcopy(parent)
    child['parent_revision'] = parent['revision_sha256']
    child['notes'][0]['velocity']['value'] = 89
    child['provenance']['last_edit'] = {'actor': 'Independent synthetic author', 'native_execution': False}
    if evidence:
        raw = put_bytes(b'Attributed fixture evidence only', store, 'evidence.bin', 'pocket.binary-asset/v1')
        record = put_record({'schema': 'qa.attributed-evidence/v1', 'raw': raw}, store)
        child['provenance']['last_edit']['evidence'] = [record]
        args['native_report']['evidence'] = [record]
    child = finalize_material(child)
    args['final_material'] = put_record(child, store)
    path = Path(args['saved_als'])
    tree = ET.fromstring(gzip.decompress(path.read_bytes()))
    tree.find('.//MidiNoteEvent').set('Velocity', '89')
    key = ET.SubElement(tree.find('.//MidiClip/Notes/KeyTracks'), 'KeyTrack', Id='1')
    value(key, 'MidiKey', 52)
    ET.SubElement(ET.SubElement(key, 'Notes'), 'MidiNoteEvent', Time='2', Duration='1', Velocity='67', OffVelocity='29')
    save_tree(args, tree)
    return args, preparation, parent, child, source, source_before


def save_tree(args, tree):
    path = Path(args['saved_als'])
    path.write_bytes(gzip.compress(ET.tostring(tree), mtime=0))
    args['expected_sha256'] = args['native_report']['saved_als_sha256'] = sha256_file(path)


def relocated(tmp_path, args, candidate):
    destination = tmp_path / 'relocated'
    shutil.copytree(Path(args['store_root']) / 'artifacts', destination / 'artifacts')
    return destination, load_candidate_record(candidate, str(destination))


def test_qa_amendment_retains_both_materials_and_source_exactly(tmp_path):
    args, prepared, parent, child, source, original = setup(tmp_path)
    original_preparation = read_bytes(prepared['artifacts']['preparation'], args['store_root'])
    result = candidate_seal(**args)
    trial = load_candidate_record(result['artifacts']['candidate'], args['store_root'])
    report = read_record(trial['material_amendment'], args['store_root'])
    assert report['velocity_before']['value'] == 76 and report['velocity_after']['value'] == 89
    assert report['changed_note_id'] == 'test-note-1'
    assert report['invariants']['native_note_id_retention'] == 'not_established'
    assert report['invariants']['human_listening'] == 'not_established'
    assert trial['provider_native_observation'] is False and trial['listening'] == 'not_reviewed'
    assert read_record(report['parent'], args['store_root']) == parent
    assert read_record(report['child'], args['store_root']) == child
    assert read_bytes(prepared['artifacts']['preparation'], args['store_root']) == original_preparation
    assert parent['notes'][1] == child['notes'][1]
    assert snapshot(source.parent) == original
    assert candidate_seal(**args) == result


@pytest.mark.parametrize('same_value', [False, True])
def test_qa_only_attack_value_may_change_not_velocity_schema(tmp_path, same_value):
    args, _, parent, child, _, _ = setup(tmp_path)
    child['notes'][0]['velocity']['undeclared_expression'] = {'probability': 0.5}
    if same_value:
        child['notes'][0]['velocity']['value'] = parent['notes'][0]['velocity']['value']
        tree = ET.fromstring(gzip.decompress(Path(args['saved_als']).read_bytes()))
        tree.find('.//MidiNoteEvent').set('Velocity', '76')
        save_tree(args, tree)
    args['final_material'] = put_record(finalize_material(child), args['store_root'])
    with pytest.raises(PocketError):
        candidate_seal(**args)


@pytest.mark.parametrize('field', ['release_velocity', 'pitch', 'duration_qn', 'onset', 'source_binding'])
def test_qa_selected_velocity_does_not_unlock_nested_note_fields(tmp_path, field):
    args, _, _, child, _, _ = setup(tmp_path)
    note = child['notes'][0]
    if field == 'release_velocity': note[field]['value'] += 1
    elif field == 'pitch': note[field]['midi_note'] += 1
    elif field == 'duration_qn': note[field]['n'] = 2
    elif field == 'onset': note[field]['n'] = 1
    else: note[field] = {'kind': 'forged'}
    args['final_material'] = put_record(finalize_material(child), args['store_root'])
    with pytest.raises(PocketError):
        candidate_seal(**args)
    assert json.loads((Path(args['saved_als']).parent / 'workspace.json').read_bytes())['state'] == 'awaiting_native'


def test_qa_relocated_amendment_rejects_schema_forgery_after_valid_duplicate_evidence(tmp_path):
    args, _, _, _, _, _ = setup(tmp_path, evidence=True)
    result = candidate_seal(**args)
    destination, trial = relocated(tmp_path, args, result['artifacts']['candidate'])
    final = read_record(trial['material'], destination)
    good = final['provenance']['last_edit']['evidence'][0]
    final['provenance']['last_edit']['evidence'].append({**good, 'artifact_schema': 'qa.false-evidence/v1'})
    final = finalize_material(final)
    trial['material'] = put_record(final, destination)
    amendment = read_record(trial['material_amendment'], destination)
    amendment.update(child=trial['material'], final_revision=final['revision_sha256'])
    trial['material_amendment'] = put_record(amendment, destination)
    forged = put_record(trial, destination)
    with pytest.raises(PocketError, match='schema'):
        validate_candidate(forged, str(destination))


def test_qa_relocated_trial_validates_native_report_evidence_bytes(tmp_path):
    args, _, _, _, _, _ = setup(tmp_path)
    native_raw = put_bytes(b'Native save attribution evidence', args['store_root'], 'native.bin', 'pocket.binary-asset/v1')
    native_report = put_record({'schema': 'qa.native-attribution/v1', 'raw': native_raw}, args['store_root'])
    args['native_report']['evidence'] = [native_report]
    result = candidate_seal(**args)
    destination, _ = relocated(tmp_path, args, result['artifacts']['candidate'])
    (destination / native_raw['artifact_uri']).write_bytes(b'Changed native evidence')
    with pytest.raises(PocketError, match='integrity'):
        validate_candidate(result['artifacts']['candidate'], str(destination))


def test_qa_trial_must_recompute_amendment_not_accept_attributed_claims(tmp_path):
    args, _, _, _, _, _ = setup(tmp_path)
    result = candidate_seal(**args)
    destination, trial = relocated(tmp_path, args, result['artifacts']['candidate'])
    report = read_record(trial['material_amendment'], destination)
    report.update(changed_note_id='retained-second-note', policy='arbitrary_edit/v1')
    trial['material_amendment'] = put_record(report, destination)
    with pytest.raises(PocketError, match='amendment'):
        validate_candidate(put_record(trial, destination), str(destination))


def test_qa_source_or_extra_max_device_still_blocks_amended_seal(tmp_path):
    args, _, _, _, source, original = setup(tmp_path)
    tree = ET.fromstring(gzip.decompress(Path(args['saved_als']).read_bytes()))
    devices = tree.find('.//MidiTrack/DeviceChain/DeviceChain/Devices')
    ET.SubElement(devices, 'MxDeviceAudioEffect', Id='101')
    save_tree(args, tree)
    with pytest.raises(PocketError, match='stock Operator'):
        candidate_seal(**args)
    assert snapshot(source.parent) == original


def test_qa_amendment_promotes_and_relocates_without_losing_original_material(tmp_path):
    args, prepared, _, _, source, original = setup(tmp_path)
    result = candidate_seal(**args)
    candidate = result['artifacts']['candidate']
    baseline = candidate_prepare(str(source), sha256_file(source), args['store_root'], 'baseline-independent',
                                 context=CONTEXT)
    baseline_args = seal_args(baseline, args['store_root'], request_id='baseline-seal')
    baseline_candidate = candidate_seal(**baseline_args)['artifacts']['candidate']
    plan_handle = audition_plan(args['store_root'], 'comparison', [candidate], baseline_candidate,
        [{'n': 4, 'd': 1}, {'n': 8, 'd': 1}], 'Synthetic evidence; no export/listening claim',
        sample_rate=8000)['artifacts']['audition_plan']
    plan = read_record(plan_handle, args['store_root'])
    render = tmp_path / 'synthetic.wav'
    sf.write(render, np.full((plan['expected_frames'], 2), 0.01), 8000, subtype='FLOAT')
    attached = attach_candidate_render(args['store_root'], 'attach', candidate, plan_handle, str(render),
        sha256_file(render), plan['settings'], {'actor': 'Synthetic fixture', 'actor_kind': 'agent',
        'observed_at': '2026-09-16T00:00:00+00:00', 'candidate_sha256': args['expected_sha256'],
        'export_completed': True, 'arrangement_only': True, 'no_missing_media': True})
    promoted = promote_candidate(args['store_root'], 'promote', candidate, attached['artifacts']['attachment'],
        {'action': 'keep', 'actor': 'Independent synthetic QA', 'actor_kind': 'agent',
         'reason': 'Technical artifact check only'}, str(tmp_path / 'package'))
    moved = tmp_path / 'relocated-package'
    Path(promoted['promotion_dir']).rename(moved)
    assert validate_candidate_promotion(str(moved), promoted['lineage_sha256'])['musical_verdict'] == 'agent_technical_keep'
    trial = read_record(candidate, moved / 'evidence')
    amendment = read_record(trial['material_amendment'], moved / 'evidence')
    assert amendment['parent'] == read_record(prepared['artifacts']['preparation'], args['store_root'])['material']
    assert read_record(amendment['parent'], moved / 'evidence')['notes'][0]['velocity']['value'] == 76
    assert read_record(amendment['child'], moved / 'evidence')['notes'][0]['velocity']['value'] == 89
    (moved / 'evidence' / amendment['parent']['artifact_uri']).write_bytes(b'corrupt historical material')
    with pytest.raises(PocketError):
        validate_candidate_promotion(str(moved), promoted['lineage_sha256'])
    assert snapshot(source.parent) == original
