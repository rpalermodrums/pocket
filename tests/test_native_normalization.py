"""Synthetic metadata predicates, never copied native projects or musical evidence."""
import copy
import xml.etree.ElementTree as ET

import pytest

from pocket_music.errors import PocketError
from pocket_music.native_normalization import ALGORITHMS, BUILD, DETAIL_VIEWS, PROFILE, normalize_metadata


def value(parent, tag, text):
    return ET.SubElement(parent, tag, Value=str(text))


def ref(parent, relative, absolute, ident=None):
    node = ET.SubElement(parent, "FileRef", **({"Id": ident} if ident is not None else {}))
    for tag, val in (("RelativePathType", "1"), ("RelativePath", relative), ("Path", absolute),
                     ("Type", "2"), ("OriginalFileSize", "16"), ("OriginalCrc", "123")):
        value(node, tag, val)
    return node


def fixture():
    root = ET.Element("Ableton", **BUILD)
    song = ET.SubElement(root, "LiveSet")
    value(song, "NextPointeeId", "100")
    tracks = ET.SubElement(song, "Tracks")
    audio = ET.SubElement(tracks, "AudioTrack", Id="1")
    value(audio, "IsContentSelectedInDocument", "true")
    chain = ET.SubElement(audio, "DeviceChain")
    mixer = ET.SubElement(chain, "Mixer")
    value(mixer, "LastSelectedClipEnvelopeIndex", "2")
    value(mixer, "Volume", "0.8")
    view = ET.SubElement(chain, "ClipEnvelopeChooserViewState")
    value(view, "SelectedDevice", "1")
    value(view, "SelectedEnvelope", "2")
    value(view, "PreferModulationVisible", "true")
    seq = ET.SubElement(chain, "MainSequencer")
    events = ET.SubElement(ET.SubElement(ET.SubElement(seq, "Sample"), "ArrangerAutomation"), "Events")
    clip = ET.SubElement(events, "AudioClip", Id="1", Time="0")
    value(clip, "IsWarped", "false")
    value(ET.SubElement(clip, "Loop"), "LoopOn", "false")
    sample = ET.SubElement(clip, "SampleRef")
    active = ET.SubElement(sample, "FileRef")
    value(active, "RelativePath", "Samples/Imported/source.wav")
    value(active, "Path", "/fixtures/workspaces/new/Samples/Imported/source.wav")
    value(sample, "LastModDate", "1000")
    value(sample, "SamplesToAutoWarp", "1")
    original = ET.SubElement(ET.SubElement(ET.SubElement(sample, "SourceContext"), "SourceContext"), "OriginalFileRef")
    ref(original, "../input.wav", "/fixtures/input.wav", "0")
    markers = ET.SubElement(clip, "WarpMarkers")
    ET.SubElement(markers, "WarpMarker", Id="4", SecTime="0", BeatTime="0")
    ET.SubElement(markers, "WarpMarker", Id="5", SecTime="0.5", BeatTime="1")
    ret = ET.SubElement(tracks, "ReturnTrack", Id="2")
    devices = ET.SubElement(ET.SubElement(ET.SubElement(ret, "DeviceChain"), "DeviceChain"), "Devices")
    for tag in ("Reverb", "Delay"):
        dev = ET.SubElement(devices, tag, Id="1" if tag == "Reverb" else "2")
        preset = ET.SubElement(ET.SubElement(ET.SubElement(dev, "LastPresetRef"), "Value"), "FilePresetRef")
        ref(preset, "../default.adv", "/fixtures/default.adv")
    value(ET.SubElement(song, "Transport"), "CurrentTime", "0")
    value(song, "HighlightedTrackIndex", "0")
    value(ET.SubElement(song, "AutoColorPickerForPlayerAndGroupTracks"), "NextColorIndex", "13")
    selection = ET.SubElement(song, "TimeSelection")
    value(selection, "AnchorTime", "0")
    value(selection, "OtherTime", "120")
    ET.SubElement(ET.SubElement(song, "SequencerNavigator"), "ClientSize", X="797", Y="527")
    for name in sorted(DETAIL_VIEWS):
        value(song, name, "false")
    algorithms = ET.SubElement(song, "NoteAlgorithms")
    for index, name in enumerate(sorted(ALGORITHMS)):
        algo = ET.SubElement(algorithms, name, Id=str(index))
        remotes = ET.SubElement(algo, "NamedKeyMidiRemoteables")
        ET.SubElement(remotes, "NamedRemoteableKeyMidi", Id="1", Name="A")
        ET.SubElement(remotes, "NamedRemoteableKeyMidi", Id="2", Name="B")
    pitch_filter = ET.SubElement(algorithms, "PitchFilterAlgorithm", Id="12")
    ET.SubElement(pitch_filter, "NamedKeyMidiRemoteables")
    pitch_classes = ET.SubElement(pitch_filter, "PitchClasses")
    for index in range(12):
        ET.SubElement(pitch_classes, "RemoteableBool", Id=str(index), Value="false")
    value(pitch_filter, "DrumPad", "128")
    value(pitch_filter, "Invert", "false")
    environment = {"schema": "pocket.native-normalization-environment/v1", "profile": PROFILE,
                   "source_directory": "/fixtures/source", "saved_directory": "/fixtures/workspaces/new",
                   "collected_assets": [{"relative_path": "Samples/Imported/source.wav", "sha256": "a" * 64,
                                         "size_bytes": 16, "mtime_seconds": 2000}]}
    return root, environment


def mutate(before, path, attribute, after):
    child = copy.deepcopy(before)
    child.find(path).set(attribute, after)
    return child


@pytest.mark.parametrize("path,attribute,after,rule", [
    ("LiveSet/NextPointeeId", "Value", "101", "next_pointee_monotonic_above_assigned_ids"),
    (".//AudioTrack/IsContentSelectedInDocument", "Value", "false", "source_track_selection_ui_flag"),
    (".//AudioTrack/DeviceChain/Mixer/LastSelectedClipEnvelopeIndex", "Value", "1", "source_clip_envelope_ui_index"),
    (".//AudioTrack/DeviceChain/ClipEnvelopeChooserViewState/SelectedEnvelope", "Value", "1", "source_envelope_chooser_ui_index"),
    (".//AudioClip/SampleRef/LastModDate", "Value", "2000", "collected_asset_mtime_with_verified_byte_identity"),
    (".//AudioClip/SampleRef/SamplesToAutoWarp", "Value", "0", "auto_warp_pending_cleared_on_unchanged_unwarped_unlooped_clip"),
    (".//AudioClip/WarpMarkers/WarpMarker", "Id", "6", "ordered_warp_marker_local_id_only"),
    ("LiveSet/Transport/CurrentTime", "Value", "17.25", "transport_cursor_only"),
    ("LiveSet/HighlightedTrackIndex", "Value", "1", "highlighted_track_ui_index"),
    ("LiveSet/AutoColorPickerForPlayerAndGroupTracks/NextColorIndex", "Value", "14", "new_track_color_allocator_increment"),
    ("LiveSet/TimeSelection/AnchorTime", "Value", "64", "ui_time_selection_only_not_render_range"),
    ("LiveSet/TimeSelection/OtherTime", "Value", "92", "ui_time_selection_only_not_render_range"),
    ("LiveSet/SequencerNavigator/ClientSize", "Y", "491", "sequencer_view_height_ui_only"),
])
def test_only_specific_observed_scalar_or_id_predicate_normalizes(path, attribute, after, rule):
    before, environment = fixture()
    saved = mutate(before, path, attribute, after)
    original_saved = ET.tostring(saved)
    result, report = normalize_metadata(before, saved, environment)
    assert ET.tostring(result) == ET.tostring(before)
    assert ET.tostring(saved) == original_saved
    assert report["changed_fields"] == 1
    assert report["changes"][0]["rule"] == rule
    assert report["provider_native_certification"] is False


@pytest.mark.parametrize("algorithm", sorted(ALGORITHMS))
def test_observed_algorithm_id_renumber_requires_same_names_empty_mappings(algorithm):
    before, environment = fixture()
    path = f"LiveSet/NoteAlgorithms/{algorithm}/NamedKeyMidiRemoteables/NamedRemoteableKeyMidi"
    saved = mutate(before, path, "Id", "11")
    _, report = normalize_metadata(before, saved, environment)
    assert report["changes"][0]["rule"] == "unassigned_algorithm_remote_local_id_only"
    saved.find(path).set("Name", "Other")
    with pytest.raises(PocketError, match="Unknown"):
        normalize_metadata(before, saved, environment)


def test_dormant_reference_rebase_and_local_id_keep_absolute_source_and_other_fields():
    before, environment = fixture()
    saved = copy.deepcopy(before)
    original = saved.find(".//OriginalFileRef/FileRef")
    original.set("Id", "9")
    original.find("RelativePath").set("Value", "../../input.wav")
    for node in saved.findall(".//LastPresetRef/Value/FilePresetRef/FileRef/RelativePath"):
        node.set("Value", "../../default.adv")
    result, report = normalize_metadata(before, saved, environment)
    assert ET.tostring(result) == ET.tostring(before)
    assert report["changed_fields"] == 4
    assert report["rule_counts"]["dormant_reference_lexical_rebase_same_absolute_hint"] == 3
    original.find("OriginalCrc").set("Value", "124")
    with pytest.raises(PocketError, match="Unknown"):
        normalize_metadata(before, saved, environment)


@pytest.mark.parametrize("name", sorted(DETAIL_VIEWS))
def test_only_observed_detail_view_boolean_paths_are_permitted(name):
    before, environment = fixture()
    saved = mutate(before, "LiveSet/" + name, "Value", "true")
    _, report = normalize_metadata(before, saved, environment)
    assert report["changes"][0]["rule"] == "detail_view_open_ui_boolean"
    saved.find("LiveSet/" + name).set("Value", "1")
    with pytest.raises(PocketError, match="Unknown"):
        normalize_metadata(before, saved, environment)


def add_default_time_filter(root):
    node = ET.SubElement(root.find("LiveSet/NoteAlgorithms"), "TimeFilterAlgorithm", Id="13")
    ET.SubElement(node, "NamedKeyMidiRemoteables")
    for tag, val in (("Start", "0"), ("Length", "0.25"), ("Repeat", "0"), ("Invert", "false")):
        value(node, tag, val)
    return node


def test_exact_appended_unassigned_default_is_the_only_added_algorithm():
    before, environment = fixture()
    saved = copy.deepcopy(before)
    add_default_time_filter(saved)
    normalized, report = normalize_metadata(before, saved, environment)
    assert ET.tostring(normalized) == ET.tostring(before)
    assert report["changes"][0]["rule"] == "appended_exact_unassigned_time_filter_default"


@pytest.mark.parametrize("change", ["Start", "Length", "Repeat", "Invert", "mapping", "extra", "position", "id"])
def test_added_algorithm_nondefault_fields_or_mappings_refuse(change):
    before, environment = fixture()
    saved = copy.deepcopy(before)
    added = add_default_time_filter(saved)
    if change in {"Start", "Length", "Repeat", "Invert"}:
        added.find(change).set("Value", "1")
    elif change == "mapping":
        ET.SubElement(added.find("NamedKeyMidiRemoteables"), "KeyMidi", Key="A")
    elif change == "extra":
        ET.SubElement(added, "Unknown", Value="0")
    elif change == "id":
        added.set("Id", "12")
    else:
        parent = saved.find("LiveSet/NoteAlgorithms")
        parent.remove(added)
        parent.insert(0, added)
    with pytest.raises(PocketError, match="Unknown"):
        normalize_metadata(before, saved, environment)


@pytest.mark.parametrize("path,attribute,after", [
    (".//AudioTrack/DeviceChain/Mixer/Volume", "Value", "0.9"),
    (".//AudioClip/WarpMarkers/WarpMarker", "BeatTime", "0.01"),
    (".//AudioClip/WarpMarkers/WarpMarker", "SecTime", "0.01"),
    (".//AudioClip", "Time", "1"),
    (".//AudioClip/SampleRef/FileRef/Path", "Value", "/other.wav"),
    (".//AudioClip/SampleRef/LastModDate", "Value", "1999"),
    (".//AudioClip/SampleRef/SamplesToAutoWarp", "Value", "2"),
    ("LiveSet/Transport/CurrentTime", "Value", "nan"),
    ("LiveSet/NextPointeeId", "Value", "99"),
    ("LiveSet/HighlightedTrackIndex", "Value", "9"),
    ("LiveSet/AutoColorPickerForPlayerAndGroupTracks/NextColorIndex", "Value", "15"),
    (".//AudioTrack/IsContentSelectedInDocument", "Value", "1"),
    (".//OriginalFileRef/FileRef/RelativePath", "Value", "../../different.wav"),
])
def test_audio_control_warp_identity_or_unqualified_metadata_always_refuses(path, attribute, after):
    before, environment = fixture()
    with pytest.raises(PocketError, match="Unknown"):
        normalize_metadata(before, mutate(before, path, attribute, after), environment)


@pytest.mark.parametrize("flag", ["IsWarped", "Loop/LoopOn"])
def test_auto_warp_completion_not_permitted_for_warped_or_looped_source(flag):
    before, environment = fixture()
    before.find(".//AudioClip/" + flag).set("Value", "true")
    saved = mutate(before, ".//AudioClip/SampleRef/SamplesToAutoWarp", "Value", "0")
    with pytest.raises(PocketError, match="Unknown"):
        normalize_metadata(before, saved, environment)


def test_next_pointee_checked_against_added_track_ids_and_duplicate_warp_ids_refuse():
    before, environment = fixture()
    saved = mutate(before, "LiveSet/NextPointeeId", "Value", "101")
    full = copy.deepcopy(saved)
    ET.SubElement(ET.SubElement(full.find("LiveSet/Tracks"), "MidiTrack", Id="3"), "Pointee", Id="102")
    with pytest.raises(PocketError, match="Unknown"):
        normalize_metadata(before, saved, environment, complete_saved=full)
    saved = mutate(before, ".//AudioClip/WarpMarkers/WarpMarker", "Id", "5")
    with pytest.raises(PocketError, match="Unknown"):
        normalize_metadata(before, saved, environment)


def test_algorithm_renumber_does_not_ignore_assigned_key_midi_mapping():
    before, environment = fixture()
    ET.SubElement(before.find("LiveSet"), "KeyMidi", Key="A")
    saved = mutate(before, ".//NamedRemoteableKeyMidi", "Id", "10")
    with pytest.raises(PocketError, match="Unknown"):
        normalize_metadata(before, saved, environment)


def test_unknown_build_and_unknown_structure_never_get_normalization():
    before, environment = fixture()
    saved = copy.deepcopy(before)
    before.set("Revision", "other-build")
    saved.set("Revision", "other-build")
    with pytest.raises(PocketError, match="exact observed"):
        normalize_metadata(before, saved, environment)


def test_default_pitch_filter_id_block_preserves_ordered_false_values():
    before, environment = fixture()
    saved = copy.deepcopy(before)
    for index, node in enumerate(saved.findall(".//PitchFilterAlgorithm/PitchClasses/RemoteableBool")):
        node.set("Id", str(index + 264))
    normalized, report = normalize_metadata(before, saved, environment)
    assert ET.tostring(normalized) == ET.tostring(before)
    assert report["rule_counts"]["unassigned_default_pitch_filter_ordered_local_ids_only"] == 12


@pytest.mark.parametrize("change", ["value", "drum", "invert", "gap", "count", "mapping", "reference", "other_parent"])
def test_pitch_filter_renumber_cannot_hide_changed_state_or_references(change):
    before, environment = fixture()
    saved = copy.deepcopy(before)
    parent = saved.find(".//PitchFilterAlgorithm")
    for index, node in enumerate(parent.findall("PitchClasses/RemoteableBool")):
        node.set("Id", str(index + 264))
    if change == "value":
        parent.find("PitchClasses/RemoteableBool").set("Value", "true")
    elif change == "drum":
        parent.find("DrumPad").set("Value", "127")
    elif change == "invert":
        parent.find("Invert").set("Value", "true")
    elif change == "gap":
        parent.find("PitchClasses/RemoteableBool").set("Id", "263")
    elif change == "count":
        parent.find("PitchClasses").remove(parent.find("PitchClasses/RemoteableBool"))
    elif change == "mapping":
        ET.SubElement(parent.find("NamedKeyMidiRemoteables"), "KeyMidi", Key="A")
    elif change == "reference":
        for root in (before, saved):
            ET.SubElement(root.find("LiveSet"), "RemoteableRef", Value="1")
    else:
        parent.tag = "OtherAlgorithm"
    with pytest.raises(PocketError, match="Unknown"):
        normalize_metadata(before, saved, environment)
    before, environment = fixture()
    saved = copy.deepcopy(before)
    ET.SubElement(saved.find("LiveSet"), "UnknownControl", Value="1")
    with pytest.raises(PocketError, match="Unknown"):
        normalize_metadata(before, saved, environment)
