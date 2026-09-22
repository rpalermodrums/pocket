"""Strict save metadata predicates observed in one exact Live 12.4.5 build.

This does not certify a DAW, or ignore arbitrary differences. It compares every
element and attribute, permitting only explicitly checked metadata transitions.
Native experiments and source files remain outside repository fixtures.
"""
from __future__ import annotations

import copy
import math
import posixpath
import re
from collections import Counter

from .errors import PocketError

PROFILE = "live-12.4.5-225ce5e356-metadata/v1"
BUILD = {"MajorVersion": "5", "MinorVersion": "12.0_12402", "SchemaChangeCount": "5",
         "Creator": "Ableton Live 12.4.5", "Revision": "225ce5e356e024356d5210512bae46fb466f6968"}
ALGORITHMS = {"ArpeggiateAlgorithm", "SpanAlgorithm", "ConnectAlgorithm", "OrnamentAlgorithm",
              "RecombineAlgorithm", "QuantizeAlgorithm", "StrumAlgorithm", "TimeWarpAlgorithm",
              "RhythmAlgorithm", "StacksAlgorithm", "ShapeAlgorithm", "SeedAlgorithm"}
AUDIO_CLIP = ("Ableton", "LiveSet", "Tracks", "AudioTrack", "DeviceChain", "MainSequencer",
              "Sample", "ArrangerAutomation", "Events", "AudioClip")
SAMPLE = (*AUDIO_CLIP, "SampleRef")
ORIGINAL_REF = (*SAMPLE, "SourceContext", "SourceContext", "OriginalFileRef", "FileRef")
DETAIL_VIEWS = {"ViewStateMainWindowClipDetailOpen", "ViewStateMainWindowHiddenOtherDocViewTypeClipDetailOpen",
                "ViewStateMainWindowHiddenOtherDocViewTypeDeviceDetailOpen", "ViewStateMainWindowDeviceDetailOpen",
                "ViewStateSecondWindowClipDetailOpen", "ViewStateSecondWindowDeviceDetailOpen"}


def _integer(value, minimum=0, maximum=2**31 - 1):
    return (isinstance(value, str) and bool(re.fullmatch(r"0|[1-9][0-9]*", value))
            and minimum <= int(value) <= maximum)


def _value(node, path):
    child = node.find(path)
    return None if child is None else child.get("Value")


def _semantics(node):
    return [node.tag, dict(node.attrib), (node.text or "").strip(), (node.tail or "").strip(),
            [_semantics(child) for child in node]]


def validate_environment(environment):
    required = {"schema", "profile", "source_directory", "saved_directory", "collected_assets"}
    # Standalone predicate callers supply anchors; sealed candidates additionally
    # carry the source observation that the provider binds to preparation.
    if (not isinstance(environment, dict) or set(environment) not in (required, required | {"source_environment"}) or
            environment["schema"] != "pocket.native-normalization-environment/v1" or
            environment["profile"] != PROFILE):
        raise PocketError("Invalid native normalization environment")
    for key in ("source_directory", "saved_directory"):
        value = environment[key]
        if not isinstance(value, str) or not value.startswith("/") or posixpath.normpath(value) != value:
            raise PocketError("Normalization anchors require explicit normalized absolute directories")
    assets = environment["collected_assets"]
    if not isinstance(assets, list) or len(assets) > 10000:
        raise PocketError("Normalization requires a bounded collected asset ledger")
    paths = set()
    for asset in assets:
        if not isinstance(asset, dict) or set(asset) != {"relative_path", "sha256", "size_bytes", "mtime_seconds"}:
            raise PocketError("Invalid normalization asset fields")
        relative = asset["relative_path"]
        if (not isinstance(relative, str) or relative.startswith("/") or ".." in relative.split("/") or
                posixpath.normpath(relative) != relative or relative in paths):
            raise PocketError("Invalid normalization asset path")
        paths.add(relative)
        if not isinstance(asset["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]):
            raise PocketError("Invalid normalization asset identity")
        for key in ("size_bytes", "mtime_seconds"):
            if isinstance(asset[key], bool) or not isinstance(asset[key], int) or asset[key] < 0:
                raise PocketError("Invalid normalization filesystem observation")
    return {asset["relative_path"]: asset for asset in assets}


def _dormant_ref(path):
    if path == ORIGINAL_REF:
        return True
    prefix = ("Ableton", "LiveSet", "Tracks", "ReturnTrack", "DeviceChain", "DeviceChain", "Devices")
    if path[:len(prefix)] != prefix or len(path) <= len(prefix) or path[len(prefix)] not in {"Reverb", "Delay"}:
        return False
    return path[len(prefix) + 1:] in {
        ("LastPresetRef", "Value", "FilePresetRef", "FileRef"),
        ("SourceContext", "Value", "BranchSourceContext", "PresetRef", "FilePresetRef", "FileRef"),
    }


def _rebase(before, after, environment):
    absolute = _value(before, "Path")
    if (absolute != _value(after, "Path") or not isinstance(absolute, str) or not absolute.startswith("/") or
            _value(before, "RelativePathType") != "1" or _value(after, "RelativePathType") != "1" or
            _value(before, "Type") != "2" or _value(after, "Type") != "2"):
        return False
    for node, anchor in ((before, environment["source_directory"]), (after, environment["saved_directory"])):
        relative = _value(node, "RelativePath")
        if not isinstance(relative, str) or relative.startswith("/") or "\\" in relative:
            return False
        if posixpath.normpath(posixpath.join(anchor, relative)) != posixpath.normpath(absolute):
            return False
    return True


def normalize_metadata(before, after, environment, *, complete_saved=None):
    """Return a normalized copy and precise metadata report, or refuse any unknown delta.

    ``after`` has only the separately validated new track removed. ``complete_saved``
    retains that track so the next-pointee allocation check sees every assigned ID.
    The caller verifies the asset ledger against immutable byte identities.
    """
    assets = validate_environment(environment)
    if dict(before.attrib) != BUILD or dict(after.attrib) != BUILD:
        raise PocketError("Native normalization is qualified only for the exact observed Live 12.4.5 build")
    complete_saved = after if complete_saved is None else complete_saved
    if dict(complete_saved.attrib) != BUILD:
        raise PocketError("Full saved set build differs from normalization profile")
    normalized = copy.deepcopy(after)
    original_nodes = {id(copied): original for copied, original in zip(normalized.iter(), after.iter(), strict=True)}
    changes, unknown = [], []
    track_count = len(complete_saved.findall("LiveSet/Tracks/*"))
    target_ids = [int(n.get("Id")) for n in complete_saved.iter()
                  if (n.tag.endswith("Target") or n.tag == "Pointee") and _integer(n.get("Id"))]
    assigned_keys = any(n.tag in {"KeyMidi", "MidiMapping", "KeyMapping"} and (n.attrib or len(n))
                        for root in (before, complete_saved) for n in root.iter())

    old_algorithms, new_algorithms = before.find("LiveSet/NoteAlgorithms"), normalized.find("LiveSet/NoteAlgorithms")
    if (old_algorithms is not None and new_algorithms is not None and
            len(new_algorithms) == len(old_algorithms) + 1 and not assigned_keys and
            old_algorithms.find("TimeFilterAlgorithm") is None and
            [n.tag for n in new_algorithms][:-1] == [n.tag for n in old_algorithms]):
        added = new_algorithms[-1]
        expected_children = [("NamedKeyMidiRemoteables", {}), ("Start", {"Value": "0"}),
                             ("Length", {"Value": "0.25"}), ("Repeat", {"Value": "0"}),
                             ("Invert", {"Value": "false"})]
        if (added.tag == "TimeFilterAlgorithm" and added.attrib == {"Id": "13"} and
                all(n.get("Id") != "13" for n in old_algorithms) and
                [(n.tag, dict(n.attrib)) for n in added] == expected_children and
                not (added.text or "").strip() and not (added.tail or "").strip() and
                all(not len(n) and not (n.text or "").strip() and not (n.tail or "").strip() for n in added)):
            changes.append({"path": "/Ableton/LiveSet/NoteAlgorithms/TimeFilterAlgorithm",
                            "attribute": None, "before": None, "after": _semantics(added),
                            "rule": "appended_exact_unassigned_time_filter_default"})
            new_algorithms.remove(added)

    def allow(node, old, path, indexed_path, rule, attr):
        changes.append({"path": indexed_path, "attribute": attr, "before": old.get(attr),
                        "after": node.get(attr), "rule": rule})
        node.set(attr, old.get(attr))

    def visit(old, new, path, indexed_path, old_ancestors, new_ancestors):
        if (old.tag != new.tag or len(old) != len(new) or set(old.attrib) != set(new.attrib) or
                (old.text or "").strip() != (new.text or "").strip() or
                (old.tail or "").strip() != (new.tail or "").strip()):
            unknown.append(indexed_path + ": structure, attribute set or text changed")
            return
        changed = [key for key in old.attrib if old.get(key) != new.get(key)]
        if changed:
            rule = None
            leaf_value = changed == ["Value"] and set(old.attrib) == {"Value"} and not len(old)
            if leaf_value and path == ("Ableton", "LiveSet", "NextPointeeId"):
                if (_integer(old.get("Value"), 1) and _integer(new.get("Value"), 1) and
                        int(new.get("Value")) >= int(old.get("Value")) and
                        int(new.get("Value")) > max(target_ids, default=0)):
                    rule = "next_pointee_monotonic_above_assigned_ids"
            elif leaf_value and path == ("Ableton", "LiveSet", "Transport", "CurrentTime"):
                try:
                    valid = all(math.isfinite(float(n.get("Value"))) and float(n.get("Value")) >= 0 for n in (old, new))
                except (ValueError, TypeError):
                    valid = False
                if valid:
                    rule = "transport_cursor_only"
            elif leaf_value and path[:3] == ("Ableton", "LiveSet", "TimeSelection") and path[3:] in {
                    ("AnchorTime",), ("OtherTime",)}:
                try:
                    valid = all(math.isfinite(float(n.get("Value"))) and float(n.get("Value")) >= 0 for n in (old, new))
                except (ValueError, TypeError):
                    valid = False
                if valid:
                    rule = "ui_time_selection_only_not_render_range"
            elif leaf_value and len(path) == 3 and path[:2] == ("Ableton", "LiveSet") and path[2] in DETAIL_VIEWS:
                if all(n.get("Value") in {"true", "false"} for n in (old, new)):
                    rule = "detail_view_open_ui_boolean"
            elif (path == ("Ableton", "LiveSet", "SequencerNavigator", "ClientSize") and changed == ["Y"] and
                  set(old.attrib) == {"X", "Y"} and not len(old)):
                if all(_integer(n.get(axis), 1, 100000) for n in (old, new) for axis in ("X", "Y")):
                    rule = "sequencer_view_height_ui_only"
            elif leaf_value and path == ("Ableton", "LiveSet", "HighlightedTrackIndex"):
                if all(_integer(n.get("Value"), 0, max(track_count - 1, 0)) for n in (old, new)):
                    rule = "highlighted_track_ui_index"
            elif leaf_value and path == ("Ableton", "LiveSet", "AutoColorPickerForPlayerAndGroupTracks", "NextColorIndex"):
                if all(_integer(n.get("Value")) for n in (old, new)) and int(new.get("Value")) == int(old.get("Value")) + 1:
                    rule = "new_track_color_allocator_increment"
            elif leaf_value and path == ("Ableton", "LiveSet", "Tracks", "AudioTrack", "IsContentSelectedInDocument"):
                if all(n.get("Value") in {"true", "false"} for n in (old, new)):
                    rule = "source_track_selection_ui_flag"
            elif leaf_value and path == ("Ableton", "LiveSet", "Tracks", "AudioTrack", "DeviceChain", "Mixer",
                                         "LastSelectedClipEnvelopeIndex"):
                if all(_integer(n.get("Value")) for n in (old, new)):
                    rule = "source_clip_envelope_ui_index"
            elif leaf_value and path == ("Ableton", "LiveSet", "Tracks", "AudioTrack", "DeviceChain",
                                         "ClipEnvelopeChooserViewState", "SelectedEnvelope"):
                if all(_integer(n.get("Value")) for n in (old, new)):
                    rule = "source_envelope_chooser_ui_index"
            elif leaf_value and path == (*SAMPLE, "LastModDate"):
                old_ref, new_ref = old_ancestors[-1], new_ancestors[-1]
                relative = _value(old_ref, "FileRef/RelativePath")
                asset = assets.get(relative)
                if (asset and _integer(old.get("Value"), 0, 2**63 - 1) and
                        _value(new_ref, "FileRef/RelativePath") == relative and
                        new.get("Value") == str(asset["mtime_seconds"])):
                    rule = "collected_asset_mtime_with_verified_byte_identity"
            elif leaf_value and path == (*SAMPLE, "SamplesToAutoWarp"):
                old_clip, new_clip = old_ancestors[-2], new_ancestors[-2]
                sample = old_ancestors[-1]
                if (old.get("Value") == "1" and new.get("Value") == "0" and
                        _value(sample, "FileRef/RelativePath") in assets and
                        all(_value(c, "IsWarped") == "false" and _value(c, "Loop/LoopOn") == "false"
                            for c in (old_clip, new_clip))):
                    rule = "auto_warp_pending_cleared_on_unchanged_unwarped_unlooped_clip"
            elif (path == (*AUDIO_CLIP, "WarpMarkers", "WarpMarker") and changed == ["Id"] and
                  set(old.attrib) == {"Id", "SecTime", "BeatTime"} and not len(old)):
                siblings_old, siblings_new = list(old_ancestors[-1]), list(original_nodes[id(new_ancestors[-1])])
                if (all(_integer(n.get("Id")) for n in [*siblings_old, *siblings_new]) and
                        len({n.get("Id") for n in siblings_old}) == len(siblings_old) and
                        len({n.get("Id") for n in siblings_new}) == len(siblings_new) and
                        not any(n.tag in {"WarpMarkerId", "WarpMarkerRef"} for n in complete_saved.iter())):
                    rule = "ordered_warp_marker_local_id_only"
            elif (len(path) == 6 and path[:3] == ("Ableton", "LiveSet", "NoteAlgorithms") and
                  path[3] in ALGORITHMS and path[4:] == ("NamedKeyMidiRemoteables", "NamedRemoteableKeyMidi") and
                  changed == ["Id"] and set(old.attrib) == {"Id", "Name"} and not len(old) and not assigned_keys):
                siblings_old, siblings_new = list(old_ancestors[-1]), list(original_nodes[id(new_ancestors[-1])])
                if (all(n.tag == "NamedRemoteableKeyMidi" and set(n.attrib) == {"Id", "Name"} and not len(n)
                        and _integer(n.get("Id")) for n in [*siblings_old, *siblings_new]) and
                        [n.get("Name") for n in siblings_old] == [n.get("Name") for n in siblings_new] and
                        len({n.get("Id") for n in siblings_old}) == len(siblings_old) and
                        len({n.get("Id") for n in siblings_new}) == len(siblings_new)):
                    rule = "unassigned_algorithm_remote_local_id_only"
            elif (path == ("Ableton", "LiveSet", "NoteAlgorithms", "PitchFilterAlgorithm", "PitchClasses",
                            "RemoteableBool") and changed == ["Id"] and not assigned_keys):
                old_parent = old_ancestors[-2]
                new_parent = original_nodes[id(new_ancestors[-2])]
                def pitch_filter_shape(node):
                    if node.attrib != {"Id": "12"} or [n.tag for n in node] != [
                            "NamedKeyMidiRemoteables", "PitchClasses", "DrumPad", "Invert"]:
                        return False
                    remotes, pitches, drum, invert = list(node)
                    if remotes.attrib or len(remotes) or pitches.attrib or drum.attrib != {"Value": "128"} or invert.attrib != {"Value": "false"}:
                        return False
                    if len(pitches) != 12 or any(n.tag != "RemoteableBool" or set(n.attrib) != {"Id", "Value"}
                            or n.get("Value") != "false" or not _integer(n.get("Id")) or len(n) for n in pitches):
                        return False
                    ids = [int(n.get("Id")) for n in pitches]
                    return ids == list(range(ids[0], ids[0] + 12))
                references = any(("Remoteable" in n.tag and n.tag.endswith(("Id", "Ref"))) or
                                 any("Remoteable" in key and key.endswith(("Id", "Ref")) for key in n.attrib)
                                 for root in (before, complete_saved) for n in root.iter())
                if not references and all(pitch_filter_shape(node) for node in (old_parent, new_parent)):
                    rule = "unassigned_default_pitch_filter_ordered_local_ids_only"
            elif path == ORIGINAL_REF and changed == ["Id"] and set(old.attrib) == {"Id"}:
                if all(_integer(n.get("Id")) for n in (old, new)) and _rebase(old, new, environment):
                    rule = "dormant_original_file_reference_local_id"
            elif (leaf_value and path[-1:] == ("RelativePath",) and _dormant_ref(path[:-1]) and
                  _rebase(old_ancestors[-1], new_ancestors[-1], environment)):
                rule = "dormant_reference_lexical_rebase_same_absolute_hint"
            if rule is None:
                unknown.append(indexed_path + ": unqualified attributes " + ",".join(changed))
            else:
                for attr in changed:
                    allow(new, old, path, indexed_path, rule, attr)
        counts = Counter(child.tag for child in old)
        positions = Counter()
        for left, right in zip(old, new, strict=True):
            position = positions[left.tag]
            positions[left.tag] += 1
            label = left.tag + (f"[{position}]" if counts[left.tag] > 1 else "")
            visit(left, right, (*path, left.tag), indexed_path + "/" + label,
                  [*old_ancestors, old], [*new_ancestors, new])

    visit(before, normalized, ("Ableton",), "/Ableton", [], [])
    if unknown:
        raise PocketError("Unknown protected source XML difference: " + "; ".join(unknown[:12]))
    if _semantics(before) != _semantics(normalized):
        raise PocketError("Normalization failed exact protected XML comparison")
    return normalized, {"profile": PROFILE, "changed_fields": len(changes), "changes": changes,
                        "rule_counts": dict(sorted(Counter(c["rule"] for c in changes).items())),
                        "scope": "observed_metadata_only", "provider_native_certification": False}
