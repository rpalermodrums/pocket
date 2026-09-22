"""Independent preservation adversaries; all project data is generated here."""
from __future__ import annotations

import copy
import gzip
import os
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from test_native_candidates import CONTEXT, seal_args, source_fixture

from pocket_music.artifact_store import put_record, read_record
from pocket_music.assets import sha256_file
from pocket_music.errors import PocketError
from pocket_music.native_candidates import (
    candidate_prepare,
    candidate_seal,
    load_candidate_record,
    validate_candidate,
)
from pocket_music.native_normalization import normalize_metadata

BUILD = {"MajorVersion": "5", "MinorVersion": "12.0_12402", "SchemaChangeCount": "5",
         "Creator": "Ableton Live 12.4.5", "Revision": "225ce5e356e024356d5210512bae46fb466f6968"}


def leaf(parent, tag, value):
    return ET.SubElement(parent, tag, Value=str(value))


def fixture():
    root = ET.Element("Ableton", BUILD)
    song = ET.SubElement(root, "LiveSet")
    leaf(song, "NextPointeeId", "100")
    leaf(ET.SubElement(song, "Transport"), "CurrentTime", "0")
    track = ET.SubElement(ET.SubElement(song, "Tracks"), "AudioTrack", Id="1")
    chain = ET.SubElement(track, "DeviceChain")
    mixer = ET.SubElement(chain, "Mixer")
    leaf(mixer, "LastSelectedClipEnvelopeIndex", "2")
    leaf(mixer, "Volume", "0.75")
    seq = ET.SubElement(chain, "MainSequencer")
    events = ET.SubElement(ET.SubElement(ET.SubElement(seq, "Sample"), "ArrangerAutomation"), "Events")
    clip = ET.SubElement(events, "AudioClip", Time="0")
    leaf(clip, "IsWarped", "false")
    leaf(ET.SubElement(clip, "Loop"), "LoopOn", "false")
    sample = ET.SubElement(clip, "SampleRef")
    ref = ET.SubElement(sample, "FileRef")
    leaf(ref, "RelativePath", "Samples/Imported/test.wav")
    leaf(ref, "Path", "/new/project/Samples/Imported/test.wav")
    leaf(sample, "LastModDate", "100")
    leaf(sample, "SamplesToAutoWarp", "1")
    markers = ET.SubElement(clip, "WarpMarkers")
    ET.SubElement(markers, "WarpMarker", Id="1", SecTime="0", BeatTime="0")
    ET.SubElement(markers, "WarpMarker", Id="2", SecTime="1", BeatTime="2")
    algorithms = ET.SubElement(song, "NoteAlgorithms")
    algo = ET.SubElement(algorithms, "QuantizeAlgorithm", Id="5")
    remotes = ET.SubElement(algo, "NamedKeyMidiRemoteables")
    ET.SubElement(remotes, "NamedRemoteableKeyMidi", Id="7", Name="Amount")
    ET.SubElement(remotes, "NamedRemoteableKeyMidi", Id="8", Name="Grid")
    pitch = ET.SubElement(algorithms, "PitchFilterAlgorithm", Id="12")
    ET.SubElement(pitch, "NamedKeyMidiRemoteables")
    classes = ET.SubElement(pitch, "PitchClasses")
    for index in range(12):
        ET.SubElement(classes, "RemoteableBool", Id=str(index), Value="false")
    leaf(pitch, "DrumPad", "128")
    leaf(pitch, "Invert", "false")
    environment = {"schema": "pocket.native-normalization-environment/v1",
                   "profile": "live-12.4.5-225ce5e356-metadata/v1",
                   "source_directory": "/source/project", "saved_directory": "/new/project",
                   "collected_assets": [{"relative_path": "Samples/Imported/test.wav", "sha256": "a" * 64,
                                         "size_bytes": 16, "mtime_seconds": 200}]}
    return root, environment


def test_qa_normalization_is_copy_and_unknown_change_is_atomic():
    before, env = fixture()
    after = copy.deepcopy(before)
    after.find("LiveSet/Transport/CurrentTime").set("Value", "8")
    before_bytes, after_bytes = ET.tostring(before), ET.tostring(after)
    normalized, report = normalize_metadata(before, after, env)
    assert ET.tostring(normalized) == before_bytes
    assert ET.tostring(before) == before_bytes and ET.tostring(after) == after_bytes
    assert report["rule_counts"] == {"transport_cursor_only": 1}
    assert report["provider_native_certification"] is False
    after.find(".//Mixer/Volume").set("Value", "0.7")
    damaged_bytes = ET.tostring(after)
    with pytest.raises(PocketError, match="Unknown"):
        normalize_metadata(before, after, env)
    assert ET.tostring(after) == damaged_bytes


@pytest.mark.parametrize("key,value", [("Creator", "Ableton Live 12.4.6"),
                                      ("Revision", "a" * 40), ("MinorVersion", "12.0_12403"),
                                      ("SchemaChangeCount", "6"), ("MajorVersion", "6")])
def test_qa_every_build_qualifier_fails_closed(key, value):
    before, env = fixture()
    after = copy.deepcopy(before)
    before.set(key, value)
    after.set(key, value)
    after.find("LiveSet/Transport/CurrentTime").set("Value", "8")
    with pytest.raises(PocketError, match="exact observed"):
        normalize_metadata(before, after, env)


@pytest.mark.parametrize("kind", ["mapping", "remote_name", "remote_child", "remote_duplicate", "remote_order",
                                  "warp_coordinate", "warp_reference", "warp_duplicate", "active_file",
                                  "new_audio_field", "audio_gain", "wrong_mtime"])
def test_qa_metadata_change_cannot_hide_preservation_failure(kind):
    before, env = fixture()
    after = copy.deepcopy(before)
    remote = after.find(".//NamedRemoteableKeyMidi")
    remote.set("Id", "27")
    if kind == "mapping":
        for root in (before, after):
            ET.SubElement(root.find("LiveSet"), "MidiMapping", TargetId="7", Channel="1")
    elif kind == "remote_name":
        remote.set("Name", "Another parameter")
    elif kind == "remote_child":
        ET.SubElement(remote, "UnknownBinding", Target="3")
    elif kind == "remote_duplicate":
        remote.set("Id", "8")
    elif kind == "remote_order":
        nodes = after.find(".//QuantizeAlgorithm/NamedKeyMidiRemoteables")
        nodes[:] = list(reversed(nodes))
    elif kind == "warp_coordinate":
        after.find(".//WarpMarker").set("BeatTime", "0.0001")
    elif kind == "warp_reference":
        after.find(".//WarpMarker").set("Id", "31")
        for root in (before, after):
            leaf(root.find("LiveSet"), "WarpMarkerRef", "1")
    elif kind == "warp_duplicate":
        after.find(".//WarpMarker").set("Id", "2")
    elif kind == "active_file":
        after.find(".//SampleRef/FileRef/Path").set("Value", "/other/project/test.wav")
    elif kind == "new_audio_field":
        leaf(after.find(".//AudioClip"), "Probability", "0.5")
    elif kind == "audio_gain":
        after.find(".//Mixer/Volume").set("Value", "0.74")
    elif kind == "wrong_mtime":
        after.find(".//SampleRef/LastModDate").set("Value", "201")
    with pytest.raises(PocketError, match="Unknown"):
        normalize_metadata(before, after, env)


def test_qa_added_track_allocator_identity_is_checked_in_full_saved_tree():
    before, env = fixture()
    after = copy.deepcopy(before)
    after.find("LiveSet/NextPointeeId").set("Value", "101")
    full = copy.deepcopy(after)
    added = ET.SubElement(full.find("LiveSet/Tracks"), "MidiTrack", Id="3")
    ET.SubElement(added, "AutomationTarget", Id="101")
    with pytest.raises(PocketError, match="Unknown"):
        normalize_metadata(before, after, env, complete_saved=full)
    full.find(".//AutomationTarget").set("Id", "100")
    assert normalize_metadata(before, after, env, complete_saved=full)[1]["changed_fields"] == 1


def renumber_pitch(root):
    for index, node in enumerate(root.findall(".//PitchFilterAlgorithm/PitchClasses/RemoteableBool")):
        node.set("Id", str(264 + index))


@pytest.mark.parametrize("kind", ["selected", "noncontiguous", "duplicate", "drum", "invert", "mapped",
                                  "extra", "unknown_child", "wrong_order"])
def test_qa_pitch_filter_renumber_cannot_hide_active_settings(kind):
    before, env = fixture()
    after = copy.deepcopy(before)
    renumber_pitch(after)
    pitch = after.find(".//PitchFilterAlgorithm")
    classes = pitch.find("PitchClasses")
    if kind == "selected":
        classes[0].set("Value", "true")
    elif kind == "noncontiguous":
        classes[1].set("Id", "2000")
    elif kind == "duplicate":
        classes[1].set("Id", "264")
    elif kind in {"drum", "invert"}:
        pitch.find("DrumPad" if kind == "drum" else "Invert").set("Value", "127" if kind == "drum" else "true")
    elif kind == "mapped":
        for root in (before, after):
            ET.SubElement(root.find("LiveSet"), "KeyMapping", TargetId="0", Key="a")
    elif kind == "extra":
        classes[0].set("Target", "3")
    elif kind == "unknown_child":
        leaf(pitch, "Unknown", "0")
    elif kind == "wrong_order":
        classes[:] = list(reversed(classes))
    with pytest.raises(PocketError, match="Unknown"):
        normalize_metadata(before, after, env)


def normalized_candidate(tmp_path):
    """Create and seal a synthetic source with one legitimate dormant path rebase."""
    parent = source_fixture(tmp_path)
    root = ET.fromstring(gzip.decompress(parent.read_bytes()))
    root.attrib = dict(BUILD)
    original = ET.SubElement(ET.SubElement(ET.SubElement(root.find(".//AudioClip/SampleRef"),
                                                       "SourceContext"), "SourceContext"), "OriginalFileRef")
    ref = ET.SubElement(original, "FileRef", Id="0")
    for tag, val in (("RelativePathType", "1"), ("RelativePath", "../source/audio.wav"),
                     ("Path", str(parent.parent / "audio.wav")), ("Type", "2")):
        leaf(ref, tag, val)
    parent.write_bytes(gzip.compress(ET.tostring(root), mtime=0))
    store = str(tmp_path / "store")
    prepared = candidate_prepare(str(parent), sha256_file(parent), store, "prepare", context=CONTEXT)
    saved = Path(prepared["workspace"]["mutable_als_path"])
    changed = ET.fromstring(gzip.decompress(saved.read_bytes()))
    relative = changed.find(".//OriginalFileRef/FileRef/RelativePath")
    relative.set("Value", os.path.relpath(parent.parent / "audio.wav", saved.parent))
    saved.write_bytes(gzip.compress(ET.tostring(changed), mtime=0))
    sealed = candidate_seal(**seal_args(prepared, store))
    return store, prepared, sealed, parent


def test_qa_normalized_candidate_relocates_with_bound_immutable_evidence(tmp_path):
    store, _, sealed, parent = normalized_candidate(tmp_path)
    source_before = parent.read_bytes()
    moved = str(tmp_path / "relocated")
    shutil.copytree(Path(store) / "artifacts", Path(moved) / "artifacts")
    checked = load_candidate_record(sealed["artifacts"]["candidate"], moved)
    assert checked["preservation"]["protected_source_xml"] == "matched_after_checked_metadata"
    assert checked["relocated_native_reopen"] == "not_verified"
    assert checked["provider_native_observation"] is False
    assert checked["listening"] == "not_reviewed"
    assert parent.read_bytes() == source_before


@pytest.mark.parametrize("tamper", ["source_anchor", "source_payload", "missing_payload", "asset_hash", "asset_size"])
def test_qa_relocated_normalization_environment_cannot_invent_source_anchor(tmp_path, tamper):
    store, prepared, sealed, _ = normalized_candidate(tmp_path)
    moved = str(tmp_path / "relocated")
    shutil.copytree(Path(store) / "artifacts", Path(moved) / "artifacts")
    trial = read_record(sealed["artifacts"]["candidate"], moved)
    environment = read_record(trial["normalization_environment"], moved)
    # Do not change saved music or the preparation handle. This contradicts the
    # prepared source observation while a parent-relative path still resolves.
    if tamper == "source_anchor":
        environment["source_directory"] = str(Path(environment["source_directory"]).parent / "invented")
    elif tamper == "source_payload":
        environment["source_environment"]["sources"][0]["path"] = "/invented/source.als"
        environment["source_directory"] = "/invented"
    elif tamper == "missing_payload":
        environment.pop("source_environment")
    elif tamper == "asset_hash":
        environment["collected_assets"][0]["sha256"] = "b" * 64
    elif tamper == "asset_size":
        environment["collected_assets"][0]["size_bytes"] += 1
    trial["normalization_environment"] = put_record(environment, moved)
    forged = put_record(trial, moved)
    assert trial["preparation"] == prepared["artifacts"]["preparation"]
    with pytest.raises(PocketError):
        validate_candidate(forged, moved)


def test_qa_only_exact_dormant_pitch_class_id_transition_is_normalized():
    before, env = fixture()
    after = copy.deepcopy(before)
    renumber_pitch(after)
    normalized, report = normalize_metadata(before, after, env)
    assert ET.tostring(normalized) == ET.tostring(before)
    assert report["changed_fields"] == 12
    assert report["provider_native_certification"] is False


@pytest.mark.parametrize("kind", ["wrong_path", "noninteger", "sibling_change", "extra_child"])
def test_qa_envelope_chooser_does_not_normalize_automation_or_unobserved_structure(kind):
    before, env = fixture()
    chain = before.find(".//AudioTrack/DeviceChain")
    chooser = ET.SubElement(chain, "ClipEnvelopeChooserViewState")
    leaf(chooser, "SelectedDevice", "1")
    leaf(chooser, "SelectedEnvelope", "2")
    leaf(chooser, "PreferModulationVisible", "true")
    if kind == "wrong_path":
        chooser.tag = "ClipEnvelope"
    after = copy.deepcopy(before)
    changed = after.find(".//" + chooser.tag)
    changed.find("SelectedEnvelope").set("Value", "1.5" if kind == "noninteger" else "1")
    if kind == "sibling_change":
        changed.find("SelectedDevice").set("Value", "2")
    elif kind == "extra_child":
        leaf(changed, "AutomationValue", "0.5")
    with pytest.raises(PocketError, match="Unknown"):
        normalize_metadata(before, after, env)
