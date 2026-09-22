"""Independent role expectations for a pure seven-leaf metadata predicate."""
import copy
import xml.etree.ElementTree as ET

import pytest

from pocket_music.errors import PocketError
from pocket_music.native_normalization import BUILD
from pocket_music.native_runtime_identity import normalize_runtime_identity_values


def child(root, path, **attributes):
    for tag in path.split("/"):
        found = root.find(tag)
        root = ET.SubElement(root, tag) if found is None else found
    root.attrib.update(attributes)
    return root


def fixture():
    before = ET.Element("Ableton", BUILD)
    child(before, "LiveSet/LomId", Value="0")
    tracks = child(before, "LiveSet/Tracks")
    audio = ET.SubElement(tracks, "AudioTrack", Id="8")
    child(audio, "LomId", Value="0")
    child(audio, "Name/EffectiveName", Value="Source")
    child(audio, "DeviceChain/DeviceChain/Devices")
    clip = child(audio, "DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events/AudioClip", Id="0")
    child(clip, "Name", Value="Context")
    child(clip, "CurrentStart", Value="0")
    child(clip, "CurrentEnd", Value="120")
    child(clip, "LomId", Value="0")
    for track_id, name, device_id in (("2", "Reverb", "2"), ("3", "Delay", "7")):
        track = ET.SubElement(tracks, "ReturnTrack", Id=track_id)
        child(track, "Name/EffectiveName", Value=name)
        child(track, "LomId", Value="0")
        device = child(track, "DeviceChain/DeviceChain/Devices/" + name, Id=device_id)
        child(device, "LomId", Value="0")
    after = copy.deepcopy(before)
    values = (21, 24, 25, 28, 29, 30, 31)
    for node, value in zip(after.iter("LomId"), values, strict=True):
        node.set("Value", str(value))
    observed = copy.deepcopy(before)
    target = ET.Element("MidiTrack", Id="9")
    observed.find("LiveSet/Tracks").insert(1, target)
    observation = {
        "host": {"runtime_version": "12.4.5", "file_path": "/fixture/candidate.als"},
        "saved_binding": {"build": BUILD, "path": "/fixture/candidate.als", "track_id": "9", "clip_id": "0"},
        "target": {"location": "arrangement", "track_index": 1, "clip_index": 0},
        "topology": {
            "song": {"runtime_id": 21},
            "tracks": [{"index": 0, "runtime_id": 24, "name": "Source", "devices": [],
                        "arrangement_clips": [{"index": 0, "runtime_id": 25, "name": "Context",
                                               "is_midi_clip": 0, "start_time": 0, "end_time": 120}]},
                       {"index": 1, "runtime_id": 22, "name": "Layer"}],
            "return_tracks": [
                {"index": 0, "runtime_id": 28, "name": "Reverb", "arrangement_clips": [],
                 "devices": [{"index": 0, "runtime_id": 29, "class_name": "Reverb"}]},
                {"index": 1, "runtime_id": 30, "name": "Delay", "arrangement_clips": [],
                 "devices": [{"index": 0, "runtime_id": 31, "class_name": "Delay"}]},
            ]},
    }
    return before, after, observed, observation


def test_exact_seven_observed_ids_only_comparison_copy_changes():
    before, after, observed, observation = fixture()
    raw_after = ET.tostring(after)
    normalized, report = normalize_runtime_identity_values(before, after, observed, observation)
    assert ET.tostring(normalized) == ET.tostring(before)
    assert ET.tostring(after) == raw_after
    assert report["changed_fields"] == 7
    assert [r["after"] for r in report["changes"]] == ["21", "24", "25", "28", "29", "30", "31"]
    assert report["current_session_identity"] is False


@pytest.mark.parametrize("attack", ["wrong_id", "nonzero_before", "extra_attribute", "child", "text",
                                    "duplicate_id", "wrong_build", "wrong_version", "wrong_path", "reorder",
                                    "different_clip", "different_bounds", "extra_device", "changed_saved_id"])
def test_malformed_or_unobserved_mapping_refused(attack):
    before, after, observed, observation = fixture()
    if attack == "wrong_id":
        after.find("LiveSet/LomId").set("Value", "42")
    elif attack == "nonzero_before":
        before.find("LiveSet/LomId").set("Value", "21")
    elif attack == "extra_attribute":
        after.find("LiveSet/LomId").set("ignored", "true")
    elif attack == "child":
        ET.SubElement(after.find("LiveSet/LomId"), "Unknown")
    elif attack == "text":
        after.find("LiveSet/LomId").text = "unknown"
    elif attack == "duplicate_id":
        observation["topology"]["song"]["runtime_id"] = 24
    elif attack == "wrong_build":
        observed.set("Creator", "other")
    elif attack == "wrong_version":
        observation["host"]["runtime_version"] = "12.4.6"
    elif attack == "wrong_path":
        observation["host"]["file_path"] = "/different.als"
    elif attack == "reorder":
        observation["topology"]["return_tracks"].reverse()
    elif attack == "different_clip":
        observation["topology"]["tracks"][0]["arrangement_clips"][0]["name"] = "Other"
    elif attack == "different_bounds":
        observation["topology"]["tracks"][0]["arrangement_clips"][0]["end_time"] = 119
    elif attack == "extra_device":
        observation["topology"]["tracks"][0]["devices"] = [{"runtime_id": 42}]
    else:
        observed.find("LiveSet/Tracks/AudioTrack").set("Id", "80")
    with pytest.raises(PocketError):
        normalize_runtime_identity_values(before, after, observed, observation)


def test_unrelated_source_changes_and_unknown_lom_nodes_remain_visible():
    before, after, observed, observation = fixture()
    child(after, "LiveSet/MainTrack/Gain", Value="0.5")
    child(after, "LiveSet/MainTrack/LomIdView", Value="99")
    normalized, report = normalize_runtime_identity_values(before, after, observed, observation)
    assert normalized.find("LiveSet/MainTrack/Gain").get("Value") == "0.5"
    assert normalized.find("LiveSet/MainTrack/LomIdView").get("Value") == "99"
    assert ET.tostring(normalized) != ET.tostring(before)
    assert report["other_source_fields"] == "require_separate_complete_comparison"
