"""Independent adversarial expectations: literal records and a tiny wire decoder.

These tests are file verification only, never native or listening evidence.
"""
import builtins
import copy
import hashlib
import json
import shutil
import struct
import xml.etree.ElementTree as ET
from fractions import Fraction

import pytest

from pocket_music.artifact_store import put_bytes, put_record, read_bytes, read_record, run_request
from pocket_music.errors import PocketError
from pocket_music.material import material_import, material_query, validate_material
from pocket_music.midi_analysis import midi_analyze
from pocket_music.midi_edit import midi_transform
from pocket_music.midi_generate import midi_generate
from pocket_music.midi_io import midi_export


def seal_literal(record):
    record = copy.deepcopy(record)
    record["revision_sha256"] = hashlib.sha256(json.dumps(
        {k: v for k, v in record.items() if k != "revision_sha256"},
        sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()).hexdigest()
    return record


def literal_material():
    notes = [{"id": f"note:{i}", "voice_id": "voice:external", "role_ref": "user:fixture",
              "onset": {"space": "clip_qn", "n": n, "d": d}, "duration_qn": {"n": 1, "d": 4},
              "pitch": {"midi_note": pitch, "cents_offset": 0, "tuning_ref": "tuning:12tet-a440"},
              "velocity": {"value": 80, "domain": "midi1_7bit"},
              "release_velocity": {"value": 37, "domain": "midi1_7bit"}, "channel": 1,
              "mute": False, "expression_refs": [], "source_binding": None, "derived_from": []}
             for i, (n, d, pitch) in enumerate([(0, 1, 60), (1, 3, 64), (10, 7, 67)])]
    return seal_literal({"schema": "pocket.material/v1", "material_id": "external:literal-fixture",
        "revision_sha256": "", "parent_revision": None, "sources": [],
        "tracks": [{"id": "track:external", "name": "External phrase"}],
        "clips": [{"id": "clip:external", "track_id": "track:external", "origin": {"space": "phrase_qn", "n": 0, "d": 1},
                   "length_qn": {"n": 4, "d": 1}, "loop": False, "note_ids": [x["id"] for x in notes],
                   "event_ids": [], "curve_ids": []}],
        "notes": notes, "events": [], "curves": [], "tempo_map_ref": None, "meter_map_ref": None,
        "coverage": {"editing_allowed": True, "issues": []}, "provenance": {"provider": "external-fixture"}})


def vlq(value):
    result = [value & 127]
    while value >> 7:
        value >>= 7
        result.insert(0, 128 | (value & 127))
    return bytes(result)


def smf(tracks, ppq=480, format=1):
    return b"MThd" + struct.pack(">IHHH", 6, format, len(tracks), ppq) + b"".join(
        b"MTrk" + struct.pack(">I", len(t)) + t for t in tracks)


def expressive_smf(ppq=480):
    conductor = (b"\x00\xff\x51\x03\x07\xa1\x20\x00\xff\x58\x04\x04\x02\x18\x08"
                 + vlq(2 * ppq) + b"\xff\x51\x03\x06\x1a\x80" + vlq(2 * ppq)
                 + b"\xff\x58\x04\x03\x02\x18\x08\x00\xff\x2f\x00")
    music = (b"\x00\xb0\x40\x00\x00\xb0\x0b\x40\x00\x90\x3c\x50"
             + vlq(ppq // 4) + b"\xe0\x00\x50" + vlq(ppq // 4)
             + b"\xb0\x0b\x60\x00\xb0\x0b\x28" + vlq(ppq // 2)
             + b"\x80\x3c\x25\x00\xe0\x00\x40\x00\x90\x40\x46" + vlq(ppq)
             + b"\x90\x40\x00\x00\xf0\x04\x7d\x01\x02\xf7" + vlq(2 * ppq) + b"\xff\x2f\x00")
    return smf([conductor, music], ppq)


def decode_wire(payload):
    """Independent limited SMF decoder; no production codec or Mido involved."""
    assert payload[:4] == b"MThd"
    size, format, count, ppq = struct.unpack(">IHHH", payload[4:14])
    offset, tracks = 8 + size, []
    for _ in range(count):
        assert payload[offset:offset + 4] == b"MTrk"
        length = struct.unpack(">I", payload[offset + 4:offset + 8])[0]
        data = payload[offset + 8:offset + 8 + length]
        offset += 8 + length
        cursor, tick, status, rows = 0, 0, None, []

        def variable(data=data):
            nonlocal cursor
            value = 0
            while True:
                byte = data[cursor]
                cursor += 1
                value = (value << 7) | (byte & 127)
                if byte < 128:
                    return value

        while cursor < len(data):
            tick += variable()
            if data[cursor] >= 128:
                status = data[cursor]
                cursor += 1
            assert status is not None
            if status == 255:
                kind = data[cursor]
                cursor += 1
                length = variable()
                value = bytes((255, kind)) + data[cursor:cursor + length]
            elif status in (240, 247):
                length = variable()
                value = bytes((status,)) + data[cursor:cursor + length]
            else:
                length = 1 if status >> 4 in (12, 13) else 2
                value = bytes((status,)) + data[cursor:cursor + length]
            cursor += length
            rows.append((Fraction(tick, ppq), value))
        tracks.append(rows)
    return format, ppq, tracks


def import_wire(tmp_path, payload=None, request_id="wire"):
    payload = expressive_smf() if payload is None else payload
    path = tmp_path / (request_id + ".mid")
    path.write_bytes(payload)
    result = material_import({"kind": "smf", "path": str(path), "expected_sha256": hashlib.sha256(payload).hexdigest()},
                              str(tmp_path / "store"), request_id)
    return result["material"]


@pytest.mark.parametrize("ppq", [96, 480, 9600])
def test_qa_independent_wire_notes_expression_order_release_and_tempo(tmp_path, ppq):
    material = import_wire(tmp_path, expressive_smf(ppq))
    store = str(tmp_path / "store")
    record = read_record(material, store)
    assert [(n["pitch"]["midi_note"], n["onset"]["n"], n["duration_qn"], n["release_velocity"]["value"])
            for n in record["notes"]] == [(60, 0, {"n": 1, "d": 1}, 37), (64, 1, {"n": 1, "d": 1}, 0)]
    selection = material_query(material, store)["selection"]
    changed = midi_transform(material, selection, [{"op": "transpose", "semitones": 1}], store, "edit")
    result = midi_export(changed["material"], store, str(tmp_path / "out.mid"), "export", ppq=ppq)
    _, _, tracks = decode_wire(read_bytes(result["midi"], store))
    assert [(t, value) for t, value in tracks[1] if value[:2] == b"\xb0\x0b"] == [
        (0, b"\xb0\x0b\x40"), (Fraction(1, 2), b"\xb0\x0b\x60"), (Fraction(1, 2), b"\xb0\x0b\x28")]
    assert (1, b"\x80\x3d\x25") in tracks[1]
    assert (2, b"\x90\x41\x00") in tracks[1]
    assert (2, b"\xf0\x7d\x01\x02\xf7") in tracks[1]
    assert [(t, value) for t, value in tracks[0] if value[:2] == b"\xff\x51"] == [
        (0, b"\xff\x51\x07\xa1\x20"), (2, b"\xff\x51\x06\x1a\x80")]


def test_qa_trailing_rest_survives_export(tmp_path):
    material = literal_material()
    output = midi_export(material, str(tmp_path / "store"), str(tmp_path / "out.mid"), "export")
    assert decode_wire(read_bytes(output["midi"], str(tmp_path / "store")))[2][0][-1] == (4, b"\xff\x2f")


def test_qa_imported_thinning_can_remove_notes_without_corrupting_raw_source(tmp_path):
    material = import_wire(tmp_path)
    store = str(tmp_path / "store")
    original = read_record(material, store)
    selected = material_query(material, store)["selection"]
    result = midi_transform(material, selected, [{"op": "thin", "every": 2, "offset": 0}], store, "thin")
    child = read_record(result["material"], store)
    assert [n["pitch"]["midi_note"] for n in child["notes"]] == [60]
    assert child["events"] == original["events"]
    exported = midi_export(result["material"], store, str(tmp_path / "thin.mid"), "export")
    notes = [data[1] for _, data in decode_wire(read_bytes(exported["midi"], store))[2][1]
             if data[0] == 0x90 and data[2]]
    assert notes == [60]


def test_qa_external_record_replaces_generation_and_lock_preserves_other_fields(tmp_path):
    record = literal_material()
    store = str(tmp_path / "store")
    imported = material_import({"kind": "material", "material": record}, store, "external")
    selection = material_query(imported["material"], store, selection={"note_ids": ["note:1"]})["selection"]
    changed = midi_transform(imported["material"], selection, [{"op": "transpose", "semitones": -12}], store, "edit",
                             locks={"selected_fields": ["onset", "duration", "velocity", "release_velocity"]})
    child = read_record(changed["material"], store)
    assert child["notes"][0] == record["notes"][0] and child["notes"][2] == record["notes"][2]
    expected = copy.deepcopy(record["notes"][1])
    expected["pitch"]["midi_note"] = 52
    assert child["notes"][1] == expected
    assert midi_analyze(changed["material"], store)["listening"] == "not_performed"
    with pytest.raises(PocketError, match="lock"):
        midi_transform(imported["material"], selection, [{"op": "shift", "delta_qn": 1}], store, "locked",
                       locks={"selected_fields": ["onset"]})


def expressed_material():
    record = literal_material()
    curve = {"schema": "pocket.curve/v1", "id": "curve:pressure", "curve_id": "curve:pressure",
        "target": {"kind": "per_note_pressure", "target_id": "pressure:note:0", "scope": "note",
                   "unit": "normalized", "value_min": 0, "value_max": 1, "quantized": False, "values": [],
                   "ownership": "none", "value_mode": "absolute", "note_id": "note:0", "resize_policy": "stretch_with_gate"},
        "space": "note_relative_qn", "interpolation": "linear",
        "points": [{"time": {"n": 0, "d": 1}, "value": 0, "order": 0},
                   {"time": {"n": 1, "d": 4}, "value": 0.8, "order": 0}],
        "parent": None, "context": None, "executable": False,
        "provenance": {"provider": "external-fixture"}}
    record["curves"] = [curve]
    record["notes"][0]["expression_refs"] = [curve["id"]]
    record["clips"][0]["curve_ids"] = [curve["id"]]
    return seal_literal(record)


def test_qa_expression_stays_exact_and_unsupported_resize_refuses(tmp_path):
    record = expressed_material()
    store = str(tmp_path)
    result = material_import({"kind": "material", "material": record}, store, "expression")
    selection = material_query(result["material"], store, selection={"note_ids": ["note:0"]})["selection"]
    shifted = midi_transform(result["material"], selection, [{"op": "shift", "delta_qn": {"n": 1, "d": 8}}], store, "shift")
    child = read_record(shifted["material"], store)
    assert child["curves"] == record["curves"]
    assert child["notes"][1:] == record["notes"][1:]
    assert child["notes"][0]["expression_refs"] == ["curve:pressure"]
    with pytest.raises(PocketError, match="expression|Expressive"):
        midi_transform(result["material"], selection, [{"op": "resize", "duration_qn": 1}], store, "resize")


def test_qa_per_note_expression_cannot_bind_a_different_note(tmp_path):
    record = expressed_material()
    record["curves"][0]["target"]["note_id"] = "note:2"
    with pytest.raises(PocketError):
        material_import({"kind": "material", "material": seal_literal(record)}, str(tmp_path), "wrong-note")


@pytest.mark.parametrize("field,bad", [("onset", None), ("pitch", []), ("source_binding", 4)])
def test_qa_external_malformed_records_raise_domain_error(tmp_path, field, bad):
    record = literal_material()
    record["notes"][0][field] = bad
    with pytest.raises(PocketError):
        material_import({"kind": "material", "material": seal_literal(record)}, str(tmp_path), "malformed")


@pytest.mark.parametrize("mutation", [
    lambda r: r["notes"][0].update(probability=0.5),
    lambda r: r.update(sources=[3]),
    lambda r: r.update(tempo_map_ref="not-a-handle"),
])
def test_qa_external_undefined_or_malformed_structures_rejected(tmp_path, mutation):
    record = literal_material()
    mutation(record)
    with pytest.raises(PocketError):
        material_import({"kind": "material", "material": seal_literal(record)}, str(tmp_path), "malformed")


def test_qa_external_tempo_map_cannot_silently_disappear_on_export(tmp_path):
    store = str(tmp_path / "store")
    imported = import_wire(tmp_path)
    imported_record = read_record(imported, store)
    record = literal_material()
    record["tempo_map_ref"] = imported_record["tempo_map_ref"]
    record = seal_literal(record)
    try:
        result = midi_export(record, store, str(tmp_path / "tempo.mid"), "tempo")
    except PocketError as error:
        assert "tempo" in str(error).lower() or "map" in str(error).lower()
        return
    tracks = decode_wire(read_bytes(result["midi"], store))[2]
    assert [(t, value) for track in tracks for t, value in track if value[:2] == b"\xff\x51"] == [
        (0, b"\xff\x51\x07\xa1\x20"), (2, b"\xff\x51\x06\x1a\x80")]


@pytest.mark.parametrize("payload", [
    smf([bytes.fromhex("00 90 3c 40 78 80 3c 20")], format=0),
    smf([bytes.fromhex("00 ff 2f 00")], format=0) + b"MTrk\x00\x00\x00\x04\x00\xff\x2f\x00",
    smf([bytes.fromhex("81 80 80 80 00 ff 2f 00")], format=0),
])
def test_qa_malformed_smf_framing_never_certified(tmp_path, payload):
    try:
        material = import_wire(tmp_path, payload)
    except PocketError:
        return
    record = read_record(material, str(tmp_path / "store"))
    assert record["coverage"]["editing_allowed"] is False
    assert record["coverage"]["issues"]


@pytest.mark.parametrize("mode", ["type2", "smpte"])
def test_qa_type2_and_smpte_stay_distinct_opaque_profiles(tmp_path, mode):
    payload = smf([bytes.fromhex("00 90 3c 40 78 80 3c 20 00 ff 2f 00")],
                  format=2 if mode == "type2" else 0, ppq=480 if mode == "type2" else 0xE728)
    handle = import_wire(tmp_path, payload)
    record = read_record(handle, str(tmp_path / "store"))
    assert record["coverage"]["editing_allowed"] is False
    expected = "asynchronous_smf_type_2" if mode == "type2" else "smpte_requires_mapping"
    assert expected in {issue["code"] for issue in record["coverage"]["issues"]}
    assert read_bytes(record["sources"][0]["raw"], str(tmp_path / "store")) == payload


def test_qa_sustain_and_ambiguous_overlapping_notes_are_not_repaired(tmp_path):
    track = bytes.fromhex("00 b0 40 7f 00 90 3c 40 78 90 3c 50 78 80 3c 20 78 80 3c 30 "
                          "00 b0 40 00 00 ff 2f 00")
    handle = import_wire(tmp_path, smf([track], format=0))
    store = str(tmp_path / "store")
    record = read_record(handle, store)
    assert len(record["notes"]) == 2 and len({n["id"] for n in record["notes"]}) == 2
    assert [e["bytes"] for e in record["events"] if e["message_type"] == "control_change"] == [[176, 64, 127], [176, 64, 0]]
    assert record["coverage"]["editing_allowed"] is False
    with pytest.raises(PocketError, match="fidelity"):
        midi_transform(handle, material_query(handle, store)["selection"], [{"op": "resize", "duration_qn": 1}], store, "resize")


def test_qa_source_aliases_and_stale_cursor_and_tampered_retry(tmp_path):
    store = str(tmp_path / "store")
    source = {"kind": "material", "material": literal_material()}
    result = material_import(source, store, "external")
    handle = result["material"]
    page = material_query(handle, store, query="events", limit=1)
    changed_source = copy.deepcopy(source)
    changed_source["material"]["notes"][0]["velocity"]["value"] = 81
    changed_source["material"] = seal_literal(changed_source["material"])
    other = material_import(changed_source, store, "another")
    with pytest.raises(PocketError, match="Stale"):
        material_query(other["material"], store, query="events", cursor=page["next_cursor"])
    (tmp_path / "store" / handle["artifact_uri"]).write_bytes(b"tampered")
    with pytest.raises(PocketError, match="integrity"):
        material_import(source, store, "external")


def test_qa_relocated_store_uses_same_handles_and_receipts(tmp_path):
    source = {"kind": "material", "material": literal_material()}
    result = material_import(source, str(tmp_path / "one"), "external")
    shutil.copytree(tmp_path / "one", tmp_path / "two")
    assert material_import(source, str(tmp_path / "two"), "external") == result
    assert material_query(result["material"], str(tmp_path / "two")) == material_query(result["material"], str(tmp_path / "one"))


def test_qa_no_mido_is_required_for_external_symbolic_work(tmp_path, monkeypatch):
    original = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "mido" or name.startswith(("mido.", "pocket_music.baste", "pocket_music.instruments")):
            raise ImportError("QA: unrelated runtime deliberately unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    record = literal_material()
    result = material_import({"kind": "material", "material": record}, str(tmp_path), "external")
    selected = material_query(result["material"], str(tmp_path))["selection"]
    edit = midi_transform(result["material"], selected, [{"op": "velocity", "value": 90}], str(tmp_path), "velocity")
    assert midi_analyze(edit["material"], str(tmp_path))["measurements"]["note_count"] == 3
    with pytest.raises(PocketError, match="optional MIDI"):
        midi_export(edit["material"], str(tmp_path), str(tmp_path / "blocked.mid"), "export")


def test_qa_crash_journal_refuses_automatic_redispatch(tmp_path):
    calls = []

    def fail():
        calls.append(1)
        raise KeyboardInterrupt("simulated interruption")

    with pytest.raises(KeyboardInterrupt):
        run_request(str(tmp_path), "crash", "test", {"x": 1}, fail)
    with pytest.raises(PocketError, match="did not complete"):
        run_request(str(tmp_path), "crash", "test", {"x": 1}, fail)
    assert calls == [1]
    journal = json.loads((tmp_path / "requests/crash/journal.json").read_text())
    assert journal["state"] == "failed"


def test_qa_retry_revalidates_nested_original_artifact(tmp_path):
    store = str(tmp_path / "store")
    handle = import_wire(tmp_path)
    record = read_record(handle, store)
    (tmp_path / "store" / record["sources"][0]["raw"]["artifact_uri"]).write_bytes(b"tampered original artifact")
    payload = expressive_smf()
    source = {"kind": "smf", "path": str(tmp_path / "wire.mid"), "expected_sha256": hashlib.sha256(payload).hexdigest()}
    with pytest.raises(PocketError, match="integrity"):
        material_import(source, store, "wire")


def test_qa_active_request_lock_refuses_second_execution(tmp_path):
    lock = tmp_path / "requests/active/lock"
    lock.mkdir(parents=True)
    calls = []
    with pytest.raises(PocketError, match="active|interrupted"):
        run_request(str(tmp_path), "active", "test", {}, lambda: calls.append(1))
    assert calls == [] and lock.is_dir()


def test_qa_handles_refuse_symlink_escape(tmp_path):
    handle = put_record(literal_material(), str(tmp_path / "store"))
    path = tmp_path / "store" / handle["artifact_uri"]
    outside = tmp_path / "outside.json"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(PocketError, match="escapes|symlink"):
        read_record(handle, str(tmp_path / "store"))


def test_qa_literal_pattern_expected_onsets(tmp_path):
    result = midi_generate({"role": "fixture", "pitch": 48, "cell_qn": 4, "cell": [0, {"n": 3, "d": 2}, 3],
                            "length_qn": 32, "enter_qn": 8, "exit_qn": 28, "gate_qn": {"n": 1, "d": 4},
                            "velocities": [64, 76, 64]}, str(tmp_path), "pattern")
    a = read_record(result["alternatives"][0], str(tmp_path))
    expected = [(8, 1), (19, 2), (11, 1), (12, 1), (27, 2), (15, 1), (16, 1), (35, 2),
                (19, 1), (20, 1), (43, 2), (23, 1), (24, 1), (51, 2), (27, 1)]
    assert [(n["onset"]["n"], n["onset"]["d"]) for n in a["notes"]] == expected
    assert read_record(result["no_addition"], str(tmp_path))["notes"] == []
    assert validate_material(a)["provenance"]["musical_judgment"] == "not_evaluated"


@pytest.mark.parametrize("budget", [1024, 1500])
def test_qa_query_budget_includes_common_receipt_envelope(tmp_path, budget):
    try:
        result = material_query(literal_material(), str(tmp_path), query="events", max_bytes=budget)
    except PocketError as error:
        assert "budget" in str(error).lower()
        return
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) <= budget


def test_qa_retry_rechecks_input_evidence_even_when_receipt_does_not_repeat_it(tmp_path):
    source = put_record({"schema": "pocket.test-source/v1", "value": 1}, str(tmp_path))
    calls = []

    def work():
        calls.append(1)
        return {"schema": "pocket.test-receipt/v1", "status": "ok"}

    run_request(str(tmp_path), "input-replay", "test", {"source": source}, work)
    (tmp_path / source["artifact_uri"]).write_text("corrupted input evidence")
    with pytest.raises(PocketError, match="integrity"):
        run_request(str(tmp_path), "input-replay", "test", {"source": source}, work)
    assert calls == [1]


def test_qa_evidence_graph_rejects_nonfinite_record_payload(tmp_path):
    source = put_bytes(b'{"schema":"pocket.test-source/v1","value":NaN}', str(tmp_path),
                       "record.json", "pocket.test-source/v1")
    calls = []
    with pytest.raises(PocketError, match="finite|JSON"):
        run_request(str(tmp_path), "nonfinite-graph", "test", {"source": source},
                    lambda: calls.append(1) or {"status": "ok"})
    assert calls == []


def qualified_native_fixture(tmp_path, mutate=None):
    from test_thread import Fixture

    from pocket_music.thread_queries import inspect_set_summary

    fixture = Fixture(tmp_path)
    fixture.root.attrib = {"MajorVersion": "5", "MinorVersion": "12.0_12402", "SchemaChangeCount": "5",
        "Creator": "Ableton Live 12.4.5", "Revision": "225ce5e356e024356d5210512bae46fb466f6968"}
    clip = fixture.clip(fixture.track(kind="MidiTrack"), midi=True, start=8, end=12,
                        source_start=0, source_end=4, loop=False)
    clip.remove(clip.find("Notes"))
    notes = ET.fromstring('''<Notes><KeyTracks><KeyTrack Id="0"><Notes>
        <MidiNoteEvent Time="0.5" Duration="0.25" Velocity="73" OffVelocity="37" NoteId="1"/>
        <MidiNoteEvent Time="2" Duration="0.5" Velocity="81" OffVelocity="42" NoteId="2"/>
        </Notes><MidiKey Value="60"/></KeyTrack></KeyTracks>
        <PerNoteEventStore><EventLists/></PerNoteEventStore><NoteProbabilityGroups/>
        <ProbabilityGroupIdGenerator><NextId Value="1"/></ProbabilityGroupIdGenerator>
        <NoteIdGenerator><NextId Value="3"/></NoteIdGenerator></Notes>''')
    clip.append(notes)
    if mutate is not None:
        mutate(fixture.root, notes)
    path = fixture.save()
    snapshot = path.read_bytes()
    handle = inspect_set_summary(path, cache_dir=tmp_path / "cache")["handle"]
    source = {"kind": "live_clip", "thread_handle": handle, "clip_id": "track:10/clip:0"}
    result = material_import(source, str(tmp_path / "store"), "qualified-import")
    assert path.read_bytes() == snapshot
    return result["material"], snapshot, source


def test_qa_qualified_empty_native_metadata_edit_keeps_source_and_requires_export_loss_approval(tmp_path):
    handle, original_bytes, _ = qualified_native_fixture(tmp_path)
    store = str(tmp_path / "store")
    material = read_record(handle, store)
    assert material["coverage"]["editing_allowed"] is True
    assert material["coverage"]["native"] == "saved_inspection_only"
    assert material["coverage"]["expression"] == "absent_in_qualified_note_tree"
    assert read_bytes(material["sources"][0]["original_set"], store) == original_bytes
    selected = material_query(handle, store, selection={"note_ids": [material["notes"][0]["id"]]})["selection"]
    changed = midi_transform(handle, selected, [{"op": "shift", "delta_qn": {"n": 1, "d": 8}}],
        store, "saved-note-shift", locks={"selected_fields": ["pitch", "duration_qn", "velocity", "release_velocity", "source_binding"]})
    after = read_record(changed["material"], store)
    assert after["notes"][0]["onset"] == {"space": "clip_qn", "n": 5, "d": 8}
    assert after["notes"][1] == material["notes"][1]
    assert after["sources"] == material["sources"]
    assert read_bytes(after["sources"][0]["original_set"], store) == original_bytes
    with pytest.raises(PocketError, match="opaque_native_note_payload"):
        midi_export(changed["material"], store, str(tmp_path / "default.mid"), "refuse-native-loss")
    result = midi_export(changed["material"], store, str(tmp_path / "approved.mid"), "approved-native-loss",
                         loss_policy="approved", approved_losses=["opaque_native_note_payload"])
    assert result["coverage"]["fidelity"]["losses"] == ["opaque_native_note_payload"]


@pytest.mark.parametrize("mutation", [
    "wrong_build", "populated_expression", "probability", "counter", "missing_counter", "unknown_wrapper",
    "note_probability", "significant_text", "duplicate_note_id", "reordered_metadata",
    "clip_muted", "clip_groove", "clip_offset", "clip_automation",
])
def test_qa_native_empty_metadata_qualification_rejects_unobserved_shapes(tmp_path, mutation):
    def mutate(root, notes):
        if mutation == "wrong_build":
            root.set("Revision", "different-build")
        elif mutation == "populated_expression":
            ET.SubElement(notes.find("PerNoteEventStore/EventLists"), "EventList", NoteId="1")
        elif mutation == "probability":
            ET.SubElement(notes.find("NoteProbabilityGroups"), "Group", Id="1")
        elif mutation == "counter":
            notes.find("NoteIdGenerator/NextId").set("Value", "2")
        elif mutation == "missing_counter":
            notes.remove(notes.find("NoteIdGenerator"))
        elif mutation == "unknown_wrapper":
            notes.find("PerNoteEventStore").set("Unknown", "true")
        elif mutation == "note_probability":
            notes.find("KeyTracks/KeyTrack/Notes/MidiNoteEvent").set("Probability", "0.5")
        elif mutation == "significant_text":
            notes.find("PerNoteEventStore/EventLists").text = "unknown native state"
        elif mutation == "duplicate_note_id":
            notes.findall("KeyTracks/KeyTrack/Notes/MidiNoteEvent")[1].set("NoteId", "1")
        elif mutation == "clip_muted":
            root.find(".//MidiClip/Disabled").set("Value", "true")
        elif mutation == "clip_groove":
            groove = ET.SubElement(root.find(".//MidiClip"), "GrooveSettings")
            ET.SubElement(groove, "GrooveId", Value="1")
        elif mutation == "clip_offset":
            root.find(".//MidiClip/Loop/StartRelative").set("Value", "0.125")
        elif mutation == "clip_automation":
            envelopes = ET.SubElement(ET.SubElement(root.find(".//MidiClip"), "Envelopes"), "Envelopes")
            ET.SubElement(envelopes, "ClipEnvelope", Id="0")
        else:
            child = notes.find("PerNoteEventStore")
            notes.remove(child)
            notes.append(child)
    handle, original_bytes, _ = qualified_native_fixture(tmp_path, mutate)
    store = str(tmp_path / "store")
    material = read_record(handle, store)
    assert material["coverage"]["editing_allowed"] is False
    assert material["coverage"]["native_note_metadata_profile"] is None
    assert read_bytes(material["sources"][0]["original_set"], store) == original_bytes
    with pytest.raises(PocketError, match="strict note editing"):
        midi_transform(handle, material_query(handle, store)["selection"], [{"op": "velocity", "value": 90}],
                        store, "must-not-edit")


def test_qa_material_query_rejects_schema_forgery_after_valid_same_identity_reference(tmp_path):
    """An earlier good handle must not authorize a later differently typed handle."""
    evidence = put_record({'schema': 'pocket.qa-material-evidence/v1', 'origin': 'external fixture'}, tmp_path)
    material = literal_material()
    material['provenance']['evidence'] = [evidence, {**evidence, 'artifact_schema': 'pocket.forged-evidence/v1'}]
    material = seal_literal(material)
    with pytest.raises(PocketError, match='schema'):
        material_query(material, str(tmp_path))


def test_qa_material_reference_graph_refuses_depth_before_unbounded_recursion(tmp_path):
    """Valid individual records cannot make a public material read unbounded."""
    evidence = put_record({'schema': 'pocket.qa-material-evidence/v1', 'leaf': True}, tmp_path)
    for index in range(140):
        evidence = put_record({'schema': 'pocket.qa-material-evidence/v1', 'index': index, 'parent': evidence}, tmp_path)
    material = literal_material()
    material['provenance']['evidence'] = evidence
    material = seal_literal(material)
    with pytest.raises(PocketError, match='bound'):
        material_query(material, str(tmp_path))


def test_qa_material_deep_inline_data_returns_bounded_domain_error(tmp_path):
    """Check depth before canonicalization/deepcopy can exhaust Python recursion."""
    material = literal_material()
    nested = {'fixture': True}
    for _ in range(1500):
        nested = {'child': nested}
    material['provenance']['deep_inline'] = nested
    with pytest.raises(PocketError, match='bound'):
        material_query(material, str(tmp_path))
