"""Observed runtime-ID metadata qualification for the narrow stock fixture shape.

This predicate only restores seven mapped leaf values in a comparison copy.
Callers must still compare every other protected source field and dependency.
It performs no file or native write and establishes no current-session identity.
"""
from __future__ import annotations

import copy
from fractions import Fraction

from .errors import PocketError
from .native_normalization import BUILD

PROFILE = "live-12.4.5-primary-audio-runtime-ids/v1"


def _only(root, path):
    nodes = root.findall(path)
    if len(nodes) != 1:
        raise PocketError("Runtime-ID qualification requires one unambiguous mapped XML node")
    return nodes[0]


def _value(root, path):
    return _only(root, path).get("Value")


def _name(track):
    node = track.find("Name/EffectiveName")
    return node.get("Value") if node is not None else _value(track, "Name/UserName")


def _leaf(node):
    if (node.tag != "LomId" or set(node.attrib) != {"Value"} or len(node) or
            (node.text or "").strip() or (node.tail or "").strip()):
        raise PocketError("Unsupported runtime-ID leaf shape")


def _inventory(root):
    tracks = _only(root, "LiveSet/Tracks")
    ordered = list(tracks)
    if [n.tag for n in ordered] != ["AudioTrack", "ReturnTrack", "ReturnTrack"]:
        raise PocketError("Runtime-ID profile requires one audio track and two stock returns")
    audio, reverb, delay = ordered
    if audio.findall("DeviceChain/DeviceChain/Devices/*") or len(audio.findall(".//AudioClip")) != 1:
        raise PocketError("Runtime-ID profile requires one primary audio clip and no source devices")
    clip = _only(audio, "DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events/AudioClip")
    roles = [("LiveSet", _only(root, "LiveSet")), ("audio_track", audio), ("audio_clip", clip)]
    for label, track, device_kind in (("return_0", reverb, "Reverb"), ("return_1", delay, "Delay")):
        devices = track.findall("DeviceChain/DeviceChain/Devices/*")
        if len(devices) != 1 or devices[0].tag != device_kind or track.findall(".//AudioClip"):
            raise PocketError("Runtime-ID profile requires exactly the two observed stock return devices")
        roles.extend([(label, track), (label + "_device", devices[0])])
    return roles


def normalize_runtime_identity_values(before, after, observed_saved, observation):
    """Normalize only exact observed 0→runtime-ID leaves in a protected comparison.

    ``before`` and ``after`` have the independently validated owned MIDI track
    removed. ``observed_saved`` is the retained set bound by a validated public
    native observation. The outer adapter must verify all artifact references.
    This pure predicate does not accept a caller-authored XPath allowlist.
    """
    if any(dict(root.attrib) != BUILD for root in (before, after, observed_saved)):
        raise PocketError("Runtime-ID qualification requires the exact observed Live build")
    if observation.get("host", {}).get("runtime_version") != "12.4.5":
        raise PocketError("Runtime-ID observation has the wrong native host version")
    binding = observation.get("saved_binding")
    if not isinstance(binding, dict) or binding.get("build") != BUILD:
        raise PocketError("Runtime-ID qualification requires a verified saved binding")
    if observation["host"].get("file_path") != binding.get("path"):
        raise PocketError("Native path differs from the retained saved binding")
    complete = copy.deepcopy(observed_saved)
    native_tracks = complete.findall("LiveSet/Tracks/MidiTrack")
    if len(native_tracks) != 1 or native_tracks[0].get("Id") != binding.get("track_id"):
        raise PocketError("Runtime-ID observation must bind the single owned MIDI track")
    complete.find("LiveSet/Tracks").remove(native_tracks[0])
    inventories = [_inventory(root) for root in (before, after, complete)]
    for mapped in zip(*inventories, strict=True):
        if len({(role, node.tag, node.get("Id")) for role, node in mapped}) != 1:
            raise PocketError("Saved source roles or durable IDs changed")

    topology = observation.get("topology", {})
    runtime_tracks, returns = topology.get("tracks", []), topology.get("return_tracks", [])
    if len(runtime_tracks) != 2 or len(returns) != 2:
        raise PocketError("Runtime topology differs from the observed stock profile")
    target = observation.get("target", {})
    if (target.get("track_index") != 1 or target.get("clip_index") != 0 or
            target.get("location") != "arrangement"):
        raise PocketError("Runtime-ID profile requires the explicitly bound new primary arrangement layer")
    audio = runtime_tracks[0]
    if audio.get("index") != 0 or audio.get("devices") or len(audio.get("arrangement_clips", [])) != 1:
        raise PocketError("Ambiguous native source track or clip")
    clip = audio["arrangement_clips"][0]
    if clip.get("index") != 0 or clip.get("is_midi_clip") != 0:
        raise PocketError("Runtime source clip is not the mapped primary audio clip")
    for (_, saved_track), runtime in zip(
            [inventories[2][1], inventories[2][3], inventories[2][5]],
            [audio, *returns], strict=True):
        if runtime.get("name") != _name(saved_track):
            raise PocketError("Runtime source track names differ from retained saved roles")
    saved_clip = inventories[2][2][1]
    if clip.get("name") != _value(saved_clip, "Name"):
        raise PocketError("Runtime source clip name differs from retained saved clip")
    try:
        bounds = ((_value(saved_clip, "CurrentStart"), clip.get("start_time")),
                  (_value(saved_clip, "CurrentEnd"), clip.get("end_time")))
        if any(Fraction(a) != Fraction(b) for a, b in bounds):
            raise PocketError("Runtime source clip bounds differ from retained saved clip")
    except (TypeError, ValueError, ZeroDivisionError) as error:
        raise PocketError("Invalid runtime source clip bounds") from error
    values = [topology.get("song", {}).get("runtime_id"), audio.get("runtime_id"), clip.get("runtime_id")]
    for index, runtime, kind in ((0, returns[0], "Reverb"), (1, returns[1], "Delay")):
        devices = runtime.get("devices", [])
        if (runtime.get("index") != index or runtime.get("arrangement_clips") or len(devices) != 1 or
                devices[0].get("index") != 0 or devices[0].get("class_name") != kind):
            raise PocketError("Runtime return topology differs from the retained stock devices")
        values.extend([runtime.get("runtime_id"), devices[0].get("runtime_id")])
    if any(type(value) is not int or not 1 <= value <= 2**31 - 1 for value in values) or len(set(values)) != 7:
        raise PocketError("Runtime-ID mapping must contain seven unique observed positive IDs")
    normalized = copy.deepcopy(after)
    normalized_roles = _inventory(normalized)
    changes = []
    for (role, original), (_, candidate), (_, normalized_node), value in zip(
            inventories[0], inventories[1], normalized_roles, values, strict=True):
        old, new = _only(original, "LomId"), _only(candidate, "LomId")
        _leaf(old)
        _leaf(new)
        if old.get("Value") != "0":
            raise PocketError("Runtime-ID profile only qualifies initialization from literal zero")
        if new.get("Value") == "0":
            continue
        if new.get("Value") != str(value):
            raise PocketError("Saved runtime ID does not equal the exact observed role ID")
        _only(normalized_node, "LomId").set("Value", "0")
        changes.append({"role": role, "saved_id": candidate.get("Id"), "before": "0", "after": str(value)})
    return normalized, {"profile": PROFILE, "changed_fields": len(changes), "changes": changes,
                        "coverage": "observed_runtime_metadata_only", "current_session_identity": False,
                        "other_source_fields": "require_separate_complete_comparison"}
