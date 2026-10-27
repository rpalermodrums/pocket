# SPDX-License-Identifier: AGPL-3.0-only
"""Independent complete-comparison checks of the narrow seven-ID predicate."""
import copy
import xml.etree.ElementTree as ET

import pytest
from test_native_runtime_identity import fixture

from pocket_music.errors import PocketError
from pocket_music.native_runtime_identity import normalize_runtime_identity_values

ROLES = ('LiveSet', 'audio_track', 'audio_clip', 'return_0', 'return_0_device', 'return_1', 'return_1_device')


def test_qa_seven_qualified_leaves_do_not_mutate_any_input_or_establish_current_identity():
    inputs = fixture()
    before_bytes = [ET.tostring(tree) for tree in inputs[:3]]
    observation_before = copy.deepcopy(inputs[3])
    result, report = normalize_runtime_identity_values(*inputs)
    assert ET.tostring(result) == before_bytes[0]
    assert [ET.tostring(tree) for tree in inputs[:3]] == before_bytes
    assert inputs[3] == observation_before
    assert tuple(change['role'] for change in report['changes']) == ROLES
    assert report['current_session_identity'] is False
    assert report['coverage'] == 'observed_runtime_metadata_only'


@pytest.mark.parametrize('index', range(7))
def test_qa_each_runtime_role_requires_its_exact_observed_value(index):
    before, after, saved, observation = fixture()
    list(after.iter('LomId'))[index].set('Value', str(1000 + index))
    with pytest.raises(PocketError):
        normalize_runtime_identity_values(before, after, saved, observation)


@pytest.mark.parametrize('index', range(7))
def test_qa_each_preexisting_nonzero_runtime_role_blocks_normalization(index):
    before, after, saved, observation = fixture()
    list(before.iter('LomId'))[index].set('Value', '1')
    with pytest.raises(PocketError, match='literal zero'):
        normalize_runtime_identity_values(before, after, saved, observation)


@pytest.mark.parametrize('mutation', ['audio_gain', 'sample_path', 'clip_gate', 'return_dsp', 'additional_lom'])
def test_qa_unknown_source_change_stays_visible_to_mandatory_complete_comparison(mutation):
    before, after, saved, observation = fixture()
    audio = after.find('LiveSet/Tracks/AudioTrack')
    if mutation == 'audio_gain':
        ET.SubElement(audio.find('DeviceChain'), 'Gain', Value='0.1')
    elif mutation == 'sample_path':
        ET.SubElement(audio.find('.//AudioClip'), 'FilePath', Value='/other/private.wav')
    elif mutation == 'clip_gate':
        audio.find('.//AudioClip/CurrentEnd').set('Value', '119')
    elif mutation == 'return_dsp':
        ET.SubElement(after.find('.//Reverb'), 'Decay', Value='12')
    else:
        ET.SubElement(audio.find('.//AudioClip'), 'LomIdView', Value='99')
    output, report = normalize_runtime_identity_values(before, after, saved, observation)
    assert ET.tostring(output) != ET.tostring(before)
    copied_after = copy.deepcopy(after)
    for leaf in copied_after.iter('LomId'):
        leaf.set('Value', '0')
    assert ET.tostring(output) == ET.tostring(copied_after)
    assert report['other_source_fields'] == 'require_separate_complete_comparison'


@pytest.mark.parametrize('mutation', ['second_source_clip', 'return_clip', 'target_reordered', 'duplicate_leaf', 'tail', 'new_build_field'])
def test_qa_mapping_ambiguity_and_unqualified_shape_refuse(mutation):
    before, after, saved, observation = fixture()
    if mutation == 'second_source_clip':
        audio = saved.find('LiveSet/Tracks/AudioTrack')
        ET.SubElement(audio, 'AudioClip', Id='1')
    elif mutation == 'return_clip':
        ET.SubElement(saved.find('LiveSet/Tracks/ReturnTrack'), 'AudioClip', Id='1')
    elif mutation == 'target_reordered':
        observation['target']['track_index'] = 0
    elif mutation == 'duplicate_leaf':
        ET.SubElement(after.find('LiveSet/Tracks/AudioTrack'), 'LomId', Value='24')
    elif mutation == 'tail':
        after.find('LiveSet/LomId').tail = 'unexpected content'
    else:
        saved.set('FutureVersion', '1')
    with pytest.raises(PocketError):
        normalize_runtime_identity_values(before, after, saved, observation)
