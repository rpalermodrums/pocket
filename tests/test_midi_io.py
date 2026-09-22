"""Generated SMF bytes and independent wire expectations; no private music fixtures."""
import hashlib
import struct
from fractions import Fraction

import mido
import pytest

from pocket_music.artifact_store import read_bytes, read_record
from pocket_music.errors import PocketError
from pocket_music.material import material_import, material_query, rational
from pocket_music.midi_edit import midi_transform
from pocket_music.midi_io import midi_export


def smf(payload, ppq=480, kind=0):
    return b'MThd' + struct.pack('>IHHH', 6, kind, 1, ppq) + b'MTrk' + struct.pack('>I', len(payload)) + payload


def fixture_bytes():
    # Independently authored MIDI wire bytes: tempo/meter, sustain, release,
    # pitch bend, note-on-zero, SysEx and nonstandard meta. Delta 240 = 0x81 0x70.
    return smf(bytes.fromhex('00 ff 51 03 07 a1 20 00 ff 58 04 04 02 18 08 '
        '00 b0 40 7f 00 90 3c 5a 00 e0 00 50 81 70 80 3c 2d '
        '00 b0 40 00 00 90 3e 4d 81 70 90 3e 00 '
        '00 f0 03 01 02 f7 00 ff 70 03 01 02 03 00 ff 2f 00'))


def import_bytes(tmp_path, payload=None, name='source.mid', request='import'):
    payload = payload if payload is not None else fixture_bytes()
    path = tmp_path / name
    path.write_bytes(payload)
    stamp = path.stat().st_mtime_ns
    result = material_import({'kind': 'smf', 'path': str(path),
                              'expected_sha256': hashlib.sha256(payload).hexdigest()},
                             str(tmp_path / 'store'), request)
    assert path.read_bytes() == payload and path.stat().st_mtime_ns == stamp
    return result, path


def test_independent_wire_roundtrip_preserves_original_and_controller_order(tmp_path):
    imported, source = import_bytes(tmp_path)
    store = str(tmp_path / 'store')
    record = read_record(imported['material'], store)
    assert [(rational(n['onset']), rational(n['duration_qn']), n['pitch']['midi_note'],
             n['velocity']['value'], n['release_velocity']['value']) for n in record['notes']] == [
                 (0, Fraction(1, 2), 60, 90, 45), (Fraction(1, 2), Fraction(1, 2), 62, 77, 0)]
    assert record['notes'][1]['source_binding']['off_encoding'] == 'note_on'
    assert record['coverage']['editing_allowed'] is True
    original = midi_export(imported['material'], store, str(tmp_path / 'copy.mid'), 'original', format='smf0')
    assert (tmp_path / 'copy.mid').read_bytes() == source.read_bytes()
    assert read_bytes(original['midi'], store) == source.read_bytes()
    selection = material_query(imported['material'], store)['selection']
    changed = midi_transform(imported['material'], selection, [{'op': 'transpose', 'semitones': 12}], store, 'transpose')
    result = midi_export(changed['material'], store, str(tmp_path / 'edited.mid'), 'export', format='smf0', ppq=9600)
    actual = mido.MidiFile(tmp_path / 'edited.mid')
    assert actual.ticks_per_beat == 9600
    tick, rows = 0, []
    for msg in actual.tracks[0]:
        tick += msg.time
        rows.append((tick, msg.type, getattr(msg, 'note', None), getattr(msg, 'velocity', None)))
    assert [r for r in rows if r[1] in ('note_on', 'note_off')] == [
        (0, 'note_on', 72, 90), (4800, 'note_off', 72, 45),
        (4800, 'note_on', 74, 77), (9600, 'note_on', 74, 0)]
    assert [msg.type for msg in actual.tracks[0]][:5] == ['set_tempo', 'time_signature', 'control_change', 'note_on', 'pitchwheel']
    assert [msg.value for msg in actual.tracks[0] if msg.type == 'control_change'] == [127, 0]
    assert any(msg.type == 'unknown_meta' and list(msg.data) == [1, 2, 3] for msg in actual.tracks[0])
    assert result['coverage']['fidelity']['roundtrip']['events_equal'] is True


def test_ambiguous_overlap_and_unmatched_off_are_opaque_not_repaired(tmp_path):
    payload = smf(bytes.fromhex('00 90 3c 40 00 90 3c 50 78 80 3c 20 78 80 3c 30 00 80 3d 00 00 ff 2f 00'))
    imported, _ = import_bytes(tmp_path, payload)
    store = str(tmp_path / 'store')
    record = read_record(imported['material'], store)
    assert len(record['notes']) == 2  # Equal-pitch duplicate attacks are retained.
    assert {i['code'] for i in record['coverage']['issues']} == {'ambiguous_same_pitch_overlap', 'unmatched_note_off'}
    with pytest.raises(PocketError, match='fidelity'):
        midi_transform(imported['material'], material_query(imported['material'], store)['selection'],
                       [{'op': 'velocity', 'value': 99}], store, 'bad-edit')
    midi_export(imported['material'], store, str(tmp_path / 'unchanged.mid'), 'raw-copy', format='smf0')
    assert (tmp_path / 'unchanged.mid').read_bytes() == payload
    with pytest.raises(PocketError, match='Ambiguous'):
        midi_export(imported['material'], store, str(tmp_path / 'reencoded.mid'), 'reencode', ppq=9600)


def test_stale_source_export_destination_and_exact_ppq(tmp_path):
    imported, source = import_bytes(tmp_path)
    store = str(tmp_path / 'store')
    source.write_bytes(b'changed')
    with pytest.raises(PocketError, match='Stale'):
        material_import({'kind': 'smf', 'path': str(source), 'expected_sha256': hashlib.sha256(fixture_bytes()).hexdigest()}, store, 'import')
    first = read_record(imported['material'], store)['notes'][0]['id']
    select = material_query(imported['material'], store, selection={'note_ids': [first]})['selection']
    edited = midi_transform(imported['material'], select, [{'op': 'shift', 'delta_qn': {'n': 1, 'd': 7}}], store, 'seventh')
    with pytest.raises(PocketError, match='Unrepresentable'):
        midi_export(edited['material'], store, str(tmp_path / 'bad-ppq.mid'), 'bad-ppq', ppq=9600)
    result = midi_export(edited['material'], store, str(tmp_path / 'exact.mid'), 'exact')
    assert result['coverage']['fidelity']['ppq'] == 14
    (tmp_path / 'exact.mid').write_bytes(b'changed')
    with pytest.raises(PocketError, match='changed'):
        midi_export(edited['material'], store, str(tmp_path / 'exact.mid'), 'exact')


def test_malformed_file_and_unterminated_note(tmp_path):
    with pytest.raises(PocketError, match='Malformed'):
        import_bytes(tmp_path, b'not midi')
    imported, _ = import_bytes(tmp_path, smf(bytes.fromhex('00 90 3c 40 78 ff 2f 00')), 'unterminated.mid', 'unterminated')
    record = read_record(imported['material'], tmp_path / 'store')
    assert record['notes'] == []
    assert record['coverage']['issues'][0]['code'] == 'unterminated_note'


def test_saved_live_notes_are_projected_without_native_acceptance(tmp_path):
    from test_thread import Fixture

    from pocket_music.thread_queries import inspect_set_summary
    fixture = Fixture(tmp_path)
    fixture.clip(fixture.track(kind='MidiTrack'), midi=True)
    path = fixture.save()
    handle = inspect_set_summary(path, cache_dir=tmp_path / 'cache')['handle']
    result = material_import({'kind': 'live_clip', 'thread_handle': handle, 'clip_id': 'track:10/clip:0'},
                             str(tmp_path / 'store'), 'native-import')
    record = read_record(result['material'], tmp_path / 'store')
    assert record['notes'][0]['pitch']['midi_note'] == 60
    assert rational(record['notes'][0]['duration_qn']) == Fraction(1, 4)
    assert record['notes'][0]['source_binding']['raw_attributes']['Probability'] == '.5'
    assert record['coverage']['editing_allowed'] is False
    assert record['coverage']['native'] == 'saved_inspection_only'


def test_externally_modified_import_cannot_reuse_original_bytes_as_its_export(tmp_path):
    from pocket_music.material import finalize_material
    imported, _ = import_bytes(tmp_path)
    store = str(tmp_path / 'store')
    external = read_record(imported['material'], store)
    external['notes'][0]['pitch']['midi_note'] = 65
    # Valid external edits need no private call history. Stale importer provenance
    # does not authorize replacing externally supplied notes with original bytes.
    external = finalize_material(external)
    result = midi_export(external, store, str(tmp_path / 'external.mid'), 'external-export', format='smf0')
    assert result['coverage']['fidelity']['roundtrip']['status'] == 'verified_reparse'
    actual = mido.MidiFile(tmp_path / 'external.mid')
    assert [x.note for x in actual.tracks[0] if x.type == 'note_on' and x.velocity > 0] == [65, 62]


def test_invalid_event_classification_and_lifecycle_binding_refuse(tmp_path):
    from pocket_music.material import finalize_material
    imported, _ = import_bytes(tmp_path)
    store = str(tmp_path / 'store')
    external = read_record(imported['material'], store)
    external['notes'][0]['source_binding']['off_event_id'] = 'unknown'
    with pytest.raises(PocketError, match='lifecycle'):
        finalize_material(external)
    external = read_record(imported['material'], store)
    external['events'][0]['message_type'] = 'key_signature'
    with pytest.raises(PocketError, match='declared type'):
        finalize_material(external)


def test_thinning_retains_original_wire_evidence_but_exports_only_surviving_notes(tmp_path):
    imported, _ = import_bytes(tmp_path)
    store = str(tmp_path / 'store')
    before = read_record(imported['material'], store)
    changed = midi_transform(imported['material'], material_query(imported['material'], store)['selection'],
                             [{'op': 'thin', 'every': 2}], store, 'thin')
    after = read_record(changed['material'], store)
    assert after['events'] == before['events']
    assert len(after['coverage']['source_only_note_event_ids']) == 2
    midi_export(changed['material'], store, str(tmp_path / 'thin.mid'), 'thin-export')
    actual = mido.MidiFile(tmp_path / 'thin.mid')
    assert [x.note for t in actual.tracks for x in t if x.type == 'note_on' and x.velocity > 0] == [60]
    assert [x.value for t in actual.tracks for x in t if x.type == 'control_change'] == [127, 0]


def test_export_preserves_trailing_rests_and_empty_baseline_length(tmp_path):
    from test_material import brief

    from pocket_music.midi_generate import midi_generate
    store = str(tmp_path / 'store')
    result = midi_generate(brief(), store, 'gen-rests')
    for index, handle in enumerate([result['alternatives'][0], result['no_addition']]):
        path = tmp_path / f'rests-{index}.mid'
        midi_export(handle, store, str(path), f'export-rests-{index}')
        midi = mido.MidiFile(path)
        assert sum(m.time for m in midi.tracks[0]) == 32 * midi.ticks_per_beat
        imported = material_import({'kind': 'smf', 'path': str(path),
                    'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}, store, f'read-rests-{index}')
        assert rational(read_record(imported['material'], store)['clips'][0]['length_qn']) == 32
