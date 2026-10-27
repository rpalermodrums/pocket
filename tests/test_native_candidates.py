# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic validation tests, explicitly not native save/reopen acceptance."""
import gzip
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from test_thread import Fixture, value

from pocket_music.artifact_store import put_record
from pocket_music.assets import sha256_file
from pocket_music.errors import PocketError
from pocket_music.material import make_note, new_material
from pocket_music.native_candidates import (
    candidate_cancel,
    candidate_inspect,
    candidate_prepare,
    candidate_seal,
    load_candidate_record,
)

CONTEXT = {"arrangement_start_qn": {"n": 4, "d": 1}, "length_qn": {"n": 4, "d": 1},
           "tempo_bpm": 120, "meter": [4, 4]}


def source_fixture(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    f = Fixture(source)
    track = f.track()
    f.clip(track, start=0, end=16, source_start=0, source_end=8, name="audio.wav")
    sf.write(source / "audio.wav", np.sin(np.arange(160000) * .11) * .01, 8000)
    return f.save()


def snapshot(folder):
    return {str(p.relative_to(folder)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in folder.rglob("*") if p.is_file()}


def material_fixture(store):
    note = make_note("test-note-1", 0, 1, 48, 76)
    material = new_material("synthetic-native-test", tracks=[{"id": "track-1"}], notes=[note], clips=[{
        "id": "clip-1", "track_id": "track-1", "origin": {"space": "phrase_qn", "n": 0, "d": 1},
        "length_qn": {"n": 4, "d": 1}, "loop": False, "note_ids": [note["id"]],
        "event_ids": [], "curve_ids": []}])
    return put_record(material, store)


def prepare(tmp_path, mode="clone_only", request_id="prepare"):
    parent = source_fixture(tmp_path) if not (tmp_path / "source").exists() else tmp_path / "source/fixture.als"
    store = str(tmp_path / "store")
    args = {"source_als": str(parent), "expected_source_sha256": sha256_file(parent),
            "store_root": store, "request_id": request_id, "mode": mode, "context": CONTEXT}
    if mode == "with_material":
        args.update(material=material_fixture(store), layer={"track_name": "Pocket Layer", "instrument_device": "Operator"})
    return candidate_prepare(**args), args, parent


def add_synthetic_layer(path):
    root = ET.fromstring(gzip.decompress(path.read_bytes()))
    track = ET.SubElement(root.find("LiveSet/Tracks"), "MidiTrack", Id="99")
    value(ET.SubElement(track, "Name"), "EffectiveName", "Pocket Layer")
    chain = ET.SubElement(track, "DeviceChain")
    devices = ET.SubElement(ET.SubElement(chain, "DeviceChain"), "Devices")
    ET.SubElement(devices, "Operator", Id="100")
    seq = ET.SubElement(chain, "MainSequencer")
    events = ET.SubElement(ET.SubElement(ET.SubElement(seq, "Sample"), "ArrangerAutomation"), "Events")
    clip = ET.SubElement(events, "MidiClip", Id="1", Time="4")
    value(clip, "CurrentStart", 4)
    value(clip, "CurrentEnd", 8)
    loop = ET.SubElement(clip, "Loop")
    value(loop, "LoopOn", False)
    keys = ET.SubElement(ET.SubElement(ET.SubElement(clip, "Notes"), "KeyTracks"), "KeyTrack", Id="0")
    value(keys, "MidiKey", 48)
    ET.SubElement(ET.SubElement(keys, "Notes"), "MidiNoteEvent", Time="0", Duration="1", Velocity="76", OffVelocity="64")
    path.write_bytes(gzip.compress(ET.tostring(root), mtime=0))


def seal_args(prepared, store, with_material=False, request_id="seal"):
    workspace = prepared["workspace"]
    path = Path(workspace["mutable_als_path"])
    if with_material:
        add_synthetic_layer(path)
    candidate_hash = sha256_file(path)
    report = {"actor": "Synthetic fixture; no native observation", "actor_kind": "agent",
              "observed_at": "2026-09-16T00:00:00+00:00", "host_version": "synthetic-Live-fixture",
              "saved_als_sha256": candidate_hash, "saved": True, "reopened": True,
              "missing_media": False, "device_missing": False}
    args = {"store_root": store, "workspace_id": workspace["workspace_id"], "expected_revision": 1,
            "saved_als": str(path), "expected_sha256": candidate_hash, "native_report": report,
            "request_id": request_id}
    if with_material:
        report["instrument_recipe_verified"] = True
        args["instrument_state"] = put_record({"schema": "pocket.instrument-state/v1",
                                               "coverage": "synthetic test only"}, store)
    return args


def sealed_fixture(tmp_path, mode="clone_only", prepare_id="prepare", seal_id="seal"):
    prepared, prepare_args, parent = prepare(tmp_path, mode, prepare_id)
    args = seal_args(prepared, prepare_args["store_root"], mode == "with_material", seal_id)
    return candidate_seal(**args), prepared, prepare_args, parent


def test_prepare_copies_sources_and_idempotently_returns_verified_receipt(tmp_path):
    parent = source_fixture(tmp_path)
    before = snapshot(parent.parent)
    prepared, args, _ = prepare(tmp_path)
    assert snapshot(parent.parent) == before
    assert candidate_prepare(**args) == prepared
    candidate = Path(prepared["workspace"]["mutable_als_path"])
    assert candidate.stat().st_ino != parent.stat().st_ino
    copied = next(candidate.parent.glob("Samples/Imported/*"))
    assert copied.stat().st_ino != (parent.parent / "audio.wav").stat().st_ino
    checked = candidate_inspect(args["store_root"], prepared["workspace"]["workspace_id"], 1)
    assert checked["coverage"]["source_bytes_preserved"] is True


def test_changed_request_input_cannot_reuse_workspace(tmp_path):
    _, args, _ = prepare(tmp_path)
    with pytest.raises(PocketError, match="idempotency_conflict"):
        candidate_prepare(**{**args, "context": None})


@pytest.mark.parametrize("target", ["source", "media", "copy"])
def test_changed_source_or_dependency_blocks_seal(tmp_path, target):
    prepared, args, parent = prepare(tmp_path)
    seal = seal_args(prepared, args["store_root"])
    chosen = {"source": parent, "media": parent.parent / "audio.wav",
              "copy": next(Path(seal["saved_als"]).parent.glob("Samples/Imported/*"))}[target]
    chosen.write_bytes(chosen.read_bytes() + b"changed")
    with pytest.raises(PocketError, match="changed"):
        candidate_seal(**seal)


def test_source_context_mismatch_rejected(tmp_path):
    parent = source_fixture(tmp_path)
    root = ET.fromstring(gzip.decompress(parent.read_bytes()))
    for event in root.findall("LiveSet/MainTrack/AutomationEnvelopes/Envelopes/AutomationEnvelope/Automation/Events/*"):
        event.set("Value", "125")
    parent.write_bytes(gzip.compress(ET.tostring(root)))
    with pytest.raises(PocketError, match="tempo"):
        prepare(tmp_path)


def test_exact_stock_material_can_seal_but_never_claims_provider_native_observation(tmp_path):
    sealed, prepared, args, _parent = sealed_fixture(tmp_path, "with_material")
    trial = load_candidate_record(sealed["artifacts"]["candidate"], args["store_root"])
    assert trial["preservation"]["native_notes_matched"] == 1
    assert trial["provider_native_observation"] is False
    assert trial["native_verification"] == "attributed_save_reopen"
    assert trial["listening"] == "not_reviewed"
    assert sealed["workspace"]["revision"] == 4
    with pytest.raises(PocketError, match="Stale workspace"):
        candidate_inspect(args["store_root"], prepared["workspace"]["workspace_id"], 1)


@pytest.mark.parametrize("mutation", ["source_xml", "note", "release", "device", "placement", "loop"])
def test_unapproved_saved_change_blocks_seal(tmp_path, mutation):
    prepared, args, parent = prepare(tmp_path, "with_material")
    before = snapshot(parent.parent)
    seal = seal_args(prepared, args["store_root"], True)
    path = Path(seal["saved_als"])
    root = ET.fromstring(gzip.decompress(path.read_bytes()))
    if mutation == "source_xml":
        root.find("LiveSet/Tracks/AudioTrack/Name/EffectiveName").set("Value", "changed")
    elif mutation in {"note", "release"}:
        root.find(".//MidiNoteEvent").set("Velocity" if mutation == "note" else "OffVelocity", "37")
    elif mutation == "device":
        root.find(".//Operator").tag = "PluginDevice"
    elif mutation == "placement":
        root.find(".//MidiClip/CurrentStart").set("Value", "5")
    else:
        root.find(".//MidiClip/Loop/LoopOn").set("Value", "true")
    path.write_bytes(gzip.compress(ET.tostring(root)))
    seal["expected_sha256"] = seal["native_report"]["saved_als_sha256"] = sha256_file(path)
    with pytest.raises(PocketError):
        candidate_seal(**seal)
    assert snapshot(parent.parent) == before
    assert json.loads((path.parent / "workspace.json").read_text())["state"] == "awaiting_native"


@pytest.mark.parametrize("flag,value", [("saved", False), ("reopened", False), ("missing_media", True),
                                       ("device_missing", True), ("saved", 1)])
def test_native_report_requires_real_boolean_complete_reopen(tmp_path, flag, value):
    prepared, args, _ = prepare(tmp_path)
    seal = seal_args(prepared, args["store_root"])
    seal["native_report"][flag] = value
    with pytest.raises(PocketError):
        candidate_seal(**seal)


def test_tampered_sealed_candidate_rejected(tmp_path):
    sealed, _, args, _ = sealed_fixture(tmp_path)
    handle = sealed["artifacts"]["candidate_als"]
    (Path(args["store_root"]) / handle["artifact_uri"]).write_bytes(b"corrupt")
    with pytest.raises(PocketError, match="integrity"):
        load_candidate_record(sealed["artifacts"]["candidate"], args["store_root"])


def test_cancel_retains_files_and_prevents_seal(tmp_path):
    prepared, args, parent = prepare(tmp_path)
    before = snapshot(parent.parent)
    path = Path(prepared["workspace"]["mutable_als_path"])
    original_copy = path.read_bytes()
    cancelled = candidate_cancel(args["store_root"], prepared["workspace"]["workspace_id"], 1, "cancel")
    assert cancelled["status"] == "cancelled"
    assert path.read_bytes() == original_copy
    assert snapshot(parent.parent) == before
    seal = seal_args(prepared, args["store_root"])
    seal["expected_revision"] = 2
    with pytest.raises(PocketError, match="awaiting_native"):
        candidate_seal(**seal)


def test_prepare_copy_failure_leaves_no_usable_workspace_and_retry_requires_inspection(tmp_path, monkeypatch):
    from pocket_music import native_candidates

    parent = source_fixture(tmp_path)
    before = snapshot(parent.parent)
    original = native_candidates._collect_dependencies

    def fail_after_copy(*args):
        original(*args)
        raise PocketError("Injected interruption after copy")

    monkeypatch.setattr(native_candidates, "_collect_dependencies", fail_after_copy)
    with pytest.raises(PocketError, match="Injected"):
        prepare(tmp_path)
    assert snapshot(parent.parent) == before
    assert list((tmp_path / "store/workspaces").iterdir()) == []
    journal = json.loads((tmp_path / "store/requests/prepare/journal.json").read_text())
    assert journal["state"] == "failed"
    monkeypatch.setattr(native_candidates, "_collect_dependencies", original)
    with pytest.raises(PocketError, match="inspect"):
        prepare(tmp_path)


def test_preparation_retry_reverifies_mutable_workspace_presence(tmp_path):
    prepared, args, _ = prepare(tmp_path)
    Path(prepared["workspace"]["mutable_als_path"]).unlink()
    with pytest.raises(PocketError, match="missing"):
        candidate_prepare(**args)


def test_modified_source_ledger_cannot_bypass_source_check(tmp_path):
    prepared, args, _ = prepare(tmp_path)
    path = Path(prepared["workspace"]["mutable_als_path"])
    env = path.parent / "environment.json"
    record = json.loads(env.read_text())
    record["sources"] = []
    env.write_text(json.dumps(record))
    with pytest.raises(PocketError, match="environment"):
        candidate_seal(**seal_args(prepared, args["store_root"]))


def test_seal_retry_rechecks_current_native_path(tmp_path):
    prepared, args, _ = prepare(tmp_path)
    seal = seal_args(prepared, args["store_root"])
    candidate_seal(**seal)
    Path(seal["saved_als"]).write_bytes(b"changed mutable candidate")
    with pytest.raises(PocketError, match="changed"):
        candidate_seal(**seal)


def imported_note_fixture(tmp_path, prefix=b"", suffix=b"", off=b"\x80\x30\x40"):
    import struct

    from pocket_music.material import material_import

    # Independent wire input: C3, velocity76, 1qn, release64, 3qn rest.
    track = prefix + b"\x00\x90\x30\x4c\x83\x60" + off + suffix + b"\x8b\x20\xff\x2f\x00"
    wire = b"MThd" + struct.pack(">IHHH", 6, 0, 1, 480) + b"MTrk" + struct.pack(">I", len(track)) + track
    source = tmp_path / "external.mid"
    source.write_bytes(wire)
    store = str(tmp_path / "store")
    imported = material_import({"kind": "smf", "path": str(source), "expected_sha256": sha256_file(source)},
                               store, "import-external")
    return imported["material"], source


def test_external_note_only_smf_replaces_generation_and_preserves_original_bytes(tmp_path):
    from pocket_music.artifact_store import read_bytes, read_record

    material, midi_source = imported_note_fixture(tmp_path)
    parent = source_fixture(tmp_path)
    before, midi_before = snapshot(parent.parent), snapshot(tmp_path)["external.mid"]
    store = str(tmp_path / "store")
    args = {"source_als": str(parent), "expected_source_sha256": sha256_file(parent), "store_root": store,
            "request_id": "external-native", "mode": "with_material", "material": material,
            "context": CONTEXT, "layer": {"track_name": "Pocket Layer", "instrument_device": "Operator"}}
    prepared = candidate_prepare(**args)
    sealed = candidate_seal(**seal_args(prepared, store, with_material=True))
    trial = load_candidate_record(sealed["artifacts"]["candidate"], store)
    assert trial["material"] == material and trial["preservation"]["native_notes_matched"] == 1
    assert read_bytes(read_record(material, store)["sources"][0]["raw"], store) == midi_source.read_bytes()
    assert snapshot(parent.parent) == before
    assert (midi_source.read_bytes(), midi_source.stat().st_mtime_ns) == midi_before


@pytest.mark.parametrize("prefix", [b"\x00\xb0\x40\x7f", b"\x00\xe0\x00\x40",
                                    b"\x00\xff\x03\x01x", b"\x00\xff\x51\x03\x07\xa1\x20"])
def test_native_imported_wire_does_not_silently_drop_controller_or_meta(tmp_path, prefix):
    from pocket_music.native_candidates import _validate_material

    material, _ = imported_note_fixture(tmp_path, prefix=prefix)
    with pytest.raises(PocketError, match="only complete|tempo/meter"):
        _validate_material(material, str(tmp_path / "store"))


@pytest.mark.parametrize("change", ["pitch", "attack", "release", "onset", "duration", "track", "issues", "order"])
def test_native_imported_wire_refuses_projection_or_evidence_mismatch(tmp_path, change):
    from pocket_music.artifact_store import read_record
    from pocket_music.material import finalize_material
    from pocket_music.native_candidates import _validate_material

    material, _ = imported_note_fixture(tmp_path)
    store = str(tmp_path / "store")
    record = read_record(material, store)
    note = record["notes"][0]
    if change == "pitch":
        note["pitch"]["midi_note"] = 49
    elif change in {"attack", "release"}:
        note["velocity" if change == "attack" else "release_velocity"]["value"] += 1
    elif change == "onset":
        note["onset"]["n"] = 1
    elif change == "duration":
        note["duration_qn"]["n"] = 2
    elif change == "track":
        record["events"][0]["track_index"] = 1
    elif change == "issues":
        record["coverage"]["issues"] = [{"code": "unresolved_lifecycle"}]
    else:
        record["events"][0]["order"], record["events"][1]["order"] = 1, 0
    altered = put_record(finalize_material(record), store)
    with pytest.raises(PocketError, match="projection|only complete|ordering"):
        _validate_material(altered, store)


def test_native_wire_note_on_zero_refuses_known_live_release_degradation(tmp_path):
    from pocket_music.native_candidates import _validate_material

    material, _ = imported_note_fixture(tmp_path, off=b"\x90\x30\x00")
    with pytest.raises(PocketError, match="known unsupported"):
        _validate_material(material, str(tmp_path / "store"))


def test_native_wire_raw_reference_integrity_is_rechecked(tmp_path):
    from pocket_music.artifact_store import read_record
    from pocket_music.native_candidates import _validate_material

    material, _ = imported_note_fixture(tmp_path)
    raw = read_record(material, str(tmp_path / "store"))["sources"][0]["raw"]
    (tmp_path / "store" / raw["artifact_uri"]).write_bytes(b"changed raw evidence")
    with pytest.raises(PocketError, match="integrity"):
        _validate_material(material, str(tmp_path / "store"))


def test_nonwhitespace_unknown_xml_tail_cannot_bypass_source_preservation(tmp_path):
    prepared, args, _ = prepare(tmp_path)
    seal = seal_args(prepared, args["store_root"])
    path = Path(seal["saved_als"])
    root = ET.fromstring(gzip.decompress(path.read_bytes()))
    root.find("LiveSet/Tracks/AudioTrack").tail = "unexpected source content"
    path.write_bytes(gzip.compress(ET.tostring(root)))
    seal["expected_sha256"] = seal["native_report"]["saved_als_sha256"] = sha256_file(path)
    with pytest.raises(PocketError, match="Unknown protected"):
        candidate_seal(**seal)
