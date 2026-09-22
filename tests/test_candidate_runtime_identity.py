"""Full retained reader graph tests. Fake Max transport is not native acceptance."""
from __future__ import annotations

import copy
import gzip
import json
import os
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from test_native_candidates import CONTEXT, material_fixture, seal_args, snapshot, source_fixture
from test_native_midi import FAKE_MAX, NODE, fixture_core
from test_native_midi import bridge as bridge  # noqa: PLC0414 - expose pytest fixture
from test_thread import value

from pocket_music.artifact_store import put_record, read_record
from pocket_music.assets import sha256_file
from pocket_music.errors import PocketError
from pocket_music.native_candidates import (
    candidate_prepare,
    candidate_seal,
    load_candidate_record,
    native_workspace,
)
from pocket_music.native_midi import build_native_midi_writer, load_native_midi_observation, native_midi_read
from pocket_music.native_normalization import BUILD


def write_xml(path, root):
    path.write_bytes(gzip.compress(ET.tostring(root), mtime=0))


def runtime_fixture(tmp_path, bridge, *, observed_mutation=None, empty_observed=False, outside=False,
                    writer_package=None):
    """Create complete independently serialized reader evidence via local bridge."""
    source = source_fixture(tmp_path)
    root = ET.fromstring(gzip.decompress(source.read_bytes()))
    root.attrib = dict(BUILD)
    value(root.find("LiveSet"), "LomId", 0)
    audio = root.find("LiveSet/Tracks/AudioTrack")
    value(audio, "LomId", 0)
    value(audio.find(".//AudioClip"), "LomId", 0)
    for track_id, name, device_id in (("2", "Reverb", "2"), ("3", "Delay", "7")):
        track = ET.SubElement(root.find("LiveSet/Tracks"), "ReturnTrack", Id=track_id)
        value(ET.SubElement(track, "Name"), "EffectiveName", name)
        value(track, "LomId", 0)
        device = ET.SubElement(ET.SubElement(ET.SubElement(ET.SubElement(track, "DeviceChain"), "DeviceChain"), "Devices"), name, Id=device_id)
        value(device, "LomId", 0)
    write_xml(source, root)
    original = snapshot(source.parent)
    store = str(tmp_path / "store")
    prepared = candidate_prepare(str(source), sha256_file(source), store, "prepare-runtime", "with_material",
                                 context=CONTEXT, material=material_fixture(store),
                                 layer={"track_name": "Pocket Layer", "instrument_device": "Operator"})
    args = seal_args(prepared, store, True, "seal-runtime")
    saved = Path(args["saved_als"])
    root = ET.fromstring(gzip.decompress(saved.read_bytes()))
    tracks = root.find("LiveSet/Tracks")
    owned = tracks.find("MidiTrack")
    tracks.remove(owned)
    tracks.insert(1, owned)
    owned.find("DeviceChain/MainSequencer/Sample").tag = "ClipTimeable"
    clip = owned.find(".//MidiClip")
    value(clip, "Name", "Probe")
    clip.find(".//MidiNoteEvent").set("NoteId", "1")
    if writer_package is not None:
        writer = ET.SubElement(owned.find("DeviceChain/DeviceChain/Devices"), "MxDeviceAudioEffect", Id="101")
        ref = ET.SubElement(ET.SubElement(writer, "MxPatchRef"), "FileRef")
        value(ref, "Path", str(writer_package / "Native MIDI Writer.amxd"))
        value(ref, "RelativePathType", 1)
        value(ref, "RelativePath", "")
        value(ref, "Type", 2)
    final = copy.deepcopy(root)
    if empty_observed:
        clip.find("Notes/KeyTracks").clear()
    if observed_mutation:
        observed_mutation(root)
    observed_path = saved
    if outside:
        observed_path = tmp_path / "different-workspace" / saved.name
        observed_path.parent.mkdir()
    write_xml(observed_path, root)
    core = fixture_core(str(observed_path))
    target_clip = {**core["topology"]["tracks"][0]["arrangement_clips"][0], "start_time": 4, "end_time": 8,
                   "loop_end": 4, "end_marker": 4}
    source_clip = {**target_clip, "runtime_id": 5, "name": "Fixture", "is_midi_clip": 0,
                   "start_time": 0, "end_time": 16, "loop_end": 8, "end_marker": 8}
    core["topology"] = {"song": {"runtime_id": 1}, "tracks": [
        {"index": 0, "runtime_id": 4, "name": "Synthetic test track", "devices": [], "arrangement_clips": [source_clip]},
        {"index": 1, "runtime_id": 2, "name": "Pocket Layer", "devices": [
            {"index": 0, "runtime_id": 6, "name": "Operator", "class_name": "Operator"}], "arrangement_clips": [target_clip]}],
        "return_tracks": [{"index": index, "runtime_id": rid, "name": name, "devices": [
            {"index": 0, "runtime_id": rid + 1, "name": name, "class_name": name}], "arrangement_clips": []}
            for index, rid, name in ((0, 8, "Reverb"), (1, 10, "Delay"))]}
    core["target"].update(track_index=1, path="live_set tracks 1 arrangement_clips 0")
    if writer_package is not None:
        core["topology"]["tracks"][1]["devices"].append(
            {"index": 1, "runtime_id": 7, "name": "Native MIDI Writer", "class_name": "MxDeviceAudioEffect"})
    core["notes"] = [] if empty_observed else [{**core["notes"][0], "pitch": 48, "duration": 1, "velocity": 76, "release_velocity": 64}]
    core["raw_notes_json"] = json.dumps({"notes": core["notes"]})
    for name, guard in core["guards"]["clip"].items():
        if name in target_clip and guard["status"] == "available":
            guard.update(value=target_clip[name], raw=[target_clip[name]])
    live = bridge(core)
    observation = native_midi_read(store, "read-runtime", {"location": "arrangement", "track_index": 1, "clip_index": 0},
                                  saved_binding={"saved_als": str(observed_path), "expected_sha256": sha256_file(observed_path),
                                                 "track_id": "99", "clip_id": "1"}, bridge_dir=live["folder"])["artifacts"]["observation"]
    paths = ("LiveSet/LomId", "LiveSet/Tracks/AudioTrack/LomId", "LiveSet/Tracks/AudioTrack/DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events/AudioClip/LomId",
             "LiveSet/Tracks/ReturnTrack[@Id='2']/LomId", "LiveSet/Tracks/ReturnTrack[@Id='2']/DeviceChain/DeviceChain/Devices/Reverb/LomId",
             "LiveSet/Tracks/ReturnTrack[@Id='3']/LomId", "LiveSet/Tracks/ReturnTrack[@Id='3']/DeviceChain/DeviceChain/Devices/Delay/LomId")
    for path, runtime_id in zip(paths, (1, 4, 5, 8, 9, 10, 11), strict=True):
        final.find(path).set("Value", str(runtime_id))
    if writer_package is None:
        write_xml(saved, final)
    args["expected_sha256"] = args["native_report"]["saved_als_sha256"] = sha256_file(saved)
    args["runtime_identity_observation"] = observation
    return {"args": args, "prepared": prepared, "source": source, "original": original, "final": final,
            "observation": observation, "store": store, "saved": saved, "live": live}


@pytest.fixture
def writer_bridge(tmp_path):
    if NODE is None:
        pytest.skip("Node.js is required for loopback transport tests")
    package = tmp_path / "writer-package"
    store = str(tmp_path / "store")
    built = build_native_midi_writer(str(package), store, "build-writer")
    module = package / "node_modules/max-api"
    module.mkdir(parents=True)
    (module / "index.js").write_text("require('os').homedir = () => process.env.TEST_HOST_ROOT;\n" + FAKE_MAX)
    folder = tmp_path / "writer-bridge"
    processes = []

    def start(core):
        core_path = tmp_path / "writer-core.json"
        core_path.write_text(json.dumps(core))
        process = subprocess.Popen([NODE, str(package / "bridge.js")], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env={**os.environ, "POCKET_NATIVE_MIDI_WRITER_DIR": str(folder),
                                        "TEST_HOST_ROOT": str(tmp_path / "synthetic-native-host"),
                                        "TEST_CORE": str(core_path), "TEST_LOG": str(tmp_path / "writer-dispatches")})
        processes.append(process)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if list(folder.glob("connection-*.json")):
                return {"folder": str(folder), "package": package, "built": built, "process": process}
            if process.poll() is not None:
                pytest.fail(process.communicate()[1].decode())
            time.sleep(0.02)
        pytest.fail("No synthetic writer bridge descriptor")
    yield start, package, built["artifacts"]["device"]
    for process in processes:
        process.terminate()
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


def writer_scope(fixture):
    return {key: fixture["args"][key] for key in ("store_root", "workspace_id", "expected_revision", "saved_als", "expected_sha256")}


def test_before_write_source_and_package_check_accepts_only_exact_bound_files(tmp_path, writer_bridge):
    start, package, device = writer_bridge
    fixture = runtime_fixture(tmp_path, start, writer_package=package, empty_observed=True)
    with native_workspace(**writer_scope(fixture)) as session:
        report = session.validate_observation(fixture["observation"], device)
        assert report["protected_source_xml"] == "matched_after_checked_metadata"
        assert report["native_dispatch"] is False
        assert len(session.native_dependencies) == 7
    assert snapshot(fixture["source"].parent) == fixture["original"]


@pytest.mark.parametrize("attack", ["package_bytes", "package_symlink", "source_gain", "groove", "extra_sample"])
def test_before_write_refuses_unrelated_source_or_package_changes(tmp_path, writer_bridge, attack):
    start, package, device = writer_bridge
    def mutate(root):
        if attack == "source_gain":
            root.find(".//AudioClip/SampleVolume").set("Value", "0.4")
        elif attack == "groove":
            value(ET.SubElement(root.find(".//MidiClip"), "GrooveSettings"), "GrooveId", 4)
        elif attack == "extra_sample":
            ET.SubElement(ET.SubElement(root.find(".//Operator"), "SampleRef"), "FileRef")
    fixture = runtime_fixture(tmp_path, start, writer_package=package, empty_observed=True, observed_mutation=mutate)
    if attack == "package_bytes":
        (package / "reader.js").write_bytes(b"changed")
    elif attack == "package_symlink":
        path = package / "reader.js"
        moved = tmp_path / "moved-reader.js"
        path.rename(moved)
        path.symlink_to(moved)
    with pytest.raises(PocketError), native_workspace(**writer_scope(fixture)) as session:
        session.validate_observation(fixture["observation"], device)
    state = json.loads((fixture["saved"].parent / "workspace.json").read_bytes())
    assert state["state"] == "awaiting_native" and state["revision"] == 1


def test_writer_package_rechecked_after_preflight_before_pending_mutation(tmp_path, writer_bridge):
    start, package, device = writer_bridge
    fixture = runtime_fixture(tmp_path, start, writer_package=package, empty_observed=True)
    with native_workspace(**writer_scope(fixture)) as session:
        session.validate_observation(fixture["observation"], device)
        (package / "reader.js").write_bytes(b"changed after inspection")
        with pytest.raises(PocketError, match="package changed"):
            session._check()


def test_full_reader_evidence_seals_exact_source_and_survives_relocation(tmp_path, bridge):
    fixture = runtime_fixture(tmp_path, bridge)
    args = fixture["args"]
    assert load_native_midi_observation(fixture["observation"], fixture["store"])["saved_binding"]["saved_als"]["artifact_schema"] == "pocket.live-set-bytes/v1"
    result = candidate_seal(**args)
    trial = load_candidate_record(result["artifacts"]["candidate"], fixture["store"])
    assert trial["preservation"]["runtime_identity"]["changed_fields"] == 7
    assert trial["preservation"]["normalization"]["changed_fields"] == 0
    assert trial["runtime_identity_observation"] == fixture["observation"]
    assert trial["provider_native_observation"] is False
    assert trial["listening"] == "not_reviewed"
    assert snapshot(fixture["source"].parent) == fixture["original"]
    relocated = tmp_path / "relocated"
    shutil.copytree(Path(fixture["store"]) / "artifacts", relocated / "artifacts")
    assert load_candidate_record(result["artifacts"]["candidate"], str(relocated)) == trial
    assert candidate_seal(**args) == result


def test_observed_empty_layer_does_not_claim_final_note_readback(tmp_path, bridge):
    fixture = runtime_fixture(tmp_path, bridge, empty_observed=True)
    result = candidate_seal(**fixture["args"])
    trial = load_candidate_record(result["artifacts"]["candidate"], fixture["store"])
    runtime = trial["preservation"]["runtime_identity"]
    assert runtime["observation_source_preservation"]["owned_note_content"] == "not_compared_to_final_material"
    assert trial["preservation"]["native_notes_matched"] == 1


@pytest.mark.parametrize("path,attribute,new", [
    (".//AudioClip/SampleVolume", "Value", "0.6"),
    (".//AudioClip/WarpMarkers/WarpMarker", "BeatTime", "42"),
    ("LiveSet/Tracks/AudioTrack/AutomationEnvelopes/Envelopes/AutomationEnvelope/Automation/Events/FloatEvent", "Value", "0.9"),
    ("LiveSet/Tracks/AudioTrack/DeviceChain/Mixer/Volume/Manual", "Value", "0.2"),
])
def test_intermediate_source_drift_refused_even_when_final_restores_source(tmp_path, bridge, path, attribute, new):
    fixture = runtime_fixture(tmp_path, bridge, observed_mutation=lambda root: root.find(path).set(attribute, new))
    with pytest.raises(PocketError, match="protected source|Unknown"):
        candidate_seal(**fixture["args"])


def test_other_workspace_observation_refused(tmp_path, bridge):
    fixture = runtime_fixture(tmp_path, bridge, outside=True)
    with pytest.raises(PocketError, match="original exact candidate workspace"):
        candidate_seal(**fixture["args"])


@pytest.mark.parametrize("attack", ["wrong_runtime_id", "unknown_source_field", "final_note", "remaining_max", "groove"])
def test_final_protected_and_material_guards_remain_exact(tmp_path, bridge, attack):
    fixture = runtime_fixture(tmp_path, bridge)
    final = fixture["final"]
    if attack == "wrong_runtime_id":
        final.find("LiveSet/LomId").set("Value", "42")
    elif attack == "unknown_source_field":
        ET.SubElement(final.find("LiveSet/MainTrack"), "Unexpected", Value="1")
    elif attack == "final_note":
        final.find(".//MidiNoteEvent").set("Velocity", "77")
    elif attack == "groove":
        value(ET.SubElement(final.find(".//MidiClip"), "GrooveSettings"), "GrooveId", 4)
        value(final.find("LiveSet"), "GlobalGrooveAmount", 0)
    else:
        ET.SubElement(final.find("LiveSet/Tracks/MidiTrack/DeviceChain/DeviceChain/Devices"), "MxDeviceMidiEffect", Id="101")
    write_xml(fixture["saved"], final)
    args = fixture["args"]
    args["expected_sha256"] = args["native_report"]["saved_als_sha256"] = sha256_file(fixture["saved"])
    with pytest.raises(PocketError):
        candidate_seal(**args)


@pytest.mark.parametrize("attack", ["observation_schema", "raw_bytes", "environment_source", "environment_asset", "observation_omitted"])
def test_relocated_validator_reverifies_complete_evidence(tmp_path, bridge, attack):
    fixture = runtime_fixture(tmp_path, bridge)
    result = candidate_seal(**fixture["args"])
    store = str(tmp_path / "moved")
    shutil.copytree(Path(fixture["store"]) / "artifacts", Path(store) / "artifacts")
    trial = read_record(result["artifacts"]["candidate"], store)
    if attack == "observation_schema":
        trial["runtime_identity_observation"]["artifact_schema"] = "pocket.other/v1"
    elif attack == "observation_omitted":
        del trial["runtime_identity_observation"]
    elif attack == "raw_bytes":
        record = read_record(trial["runtime_identity_observation"], store)
        (Path(store) / record["raw_notes"]["artifact_uri"]).write_bytes(b"invalid")
    else:
        environment = read_record(trial["normalization_environment"], store)
        if attack == "environment_source":
            environment["source_environment"]["sources"][0]["path"] = "/invented/source.als"
        else:
            environment["collected_assets"][0]["sha256"] = "f" * 64
        trial["normalization_environment"] = put_record(environment, store)
    altered = put_record(trial, store)
    with pytest.raises(PocketError):
        load_candidate_record(altered, store)
