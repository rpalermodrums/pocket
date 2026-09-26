# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic independent expectations for canonical material and bounded queries."""
import copy
import json
from fractions import Fraction

import pytest

from pocket_music.artifact_store import read_record
from pocket_music.errors import PocketError
from pocket_music.material import (
    finalize_material,
    material_import,
    material_query,
    rational,
    validate_material,
)
from pocket_music.midi_analysis import midi_analyze
from pocket_music.midi_edit import midi_transform
from pocket_music.midi_generate import midi_generate


def brief():
    return {'role': 'synthetic_percussion', 'pitch': 38, 'cell_qn': 4,
            'cell': [0, {'n': 3, 'd': 2}, 3], 'length_qn': 32,
            'enter_qn': 8, 'exit_qn': 28, 'gate_qn': {'n': 1, 'd': 4},
            'velocities': [64, 76], 'variation_qn': {'n': 1, 'd': 8}}


def generated(tmp_path):
    return midi_generate(brief(), str(tmp_path), 'generate')


def test_explicit_pattern_two_alternatives_and_no_addition(tmp_path):
    result = generated(tmp_path)
    a, b = [read_record(h, tmp_path) for h in result['alternatives']]
    assert [rational(n['onset']) for n in a['notes']] == [Fraction(base) + offset
        for base in (8, 12, 16, 20, 24) for offset in (0, Fraction(3, 2), 3)]
    assert [n['velocity']['value'] for n in a['notes'][:3]] == [64, 76, 64]
    assert all(n['pitch']['midi_note'] == 38 for n in a['notes'])
    assert all(rational(n['duration_qn']) == Fraction(1, 4) for n in a['notes'])
    assert read_record(result['no_addition'], tmp_path)['notes'] == []
    for index, (left, right) in enumerate(zip(a['notes'], b['notes'])):
        change = rational(right['onset']) - rational(left['onset'])
        assert abs(change) == Fraction(1, 8) if index >= 3 and index % 3 == 2 else change == 0
    assert midi_generate(brief(), str(tmp_path), 'generate') == result
    second = midi_generate(brief(), str(tmp_path), 'generate-again')
    assert second['alternatives'] == result['alternatives']


def test_query_hash_locks_and_exact_diff(tmp_path):
    a = generated(tmp_path)['alternatives'][0]
    record = read_record(a, tmp_path)
    select = material_query(a, str(tmp_path), selection={'note_ids': [record['notes'][1]['id']]})['selection']
    edited = midi_transform(a, select, [{'op': 'transpose', 'semitones': 7}], str(tmp_path), 'edit',
                            locks={'selected_fields': ['onset', 'duration', 'velocity', 'expression_shape']})
    child = read_record(edited['material'], tmp_path)
    assert child['notes'][1]['pitch']['midi_note'] == 45
    assert record['notes'][0] == child['notes'][0]
    assert all(old == new for index, (old, new) in enumerate(zip(record['notes'], child['notes'])) if index != 1)
    assert edited['change_summary']['changed'][0]['fields'] == {
        'pitch': {'before': record['notes'][1]['pitch'], 'after': child['notes'][1]['pitch']}}
    with pytest.raises(PocketError, match='lock'):
        midi_transform(a, select, [{'op': 'transpose', 'semitones': 1}], str(tmp_path), 'locked',
                       locks={'selected_fields': ['pitch']})
    with pytest.raises(PocketError, match='Stale'):
        midi_transform(edited['material'], select, [{'op': 'velocity', 'value': 80}], str(tmp_path), 'stale')
    with pytest.raises(PocketError, match='idempotency_conflict'):
        midi_generate({**brief(), 'pitch': 40}, str(tmp_path), 'generate')


def test_bounded_cursor_and_external_material_replacement(tmp_path):
    a = generated(tmp_path)['alternatives'][0]
    page = material_query(a, str(tmp_path), query='events', limit=2, max_bytes=4096)
    assert len(page['records']) == 2 and len(json.dumps(page).encode()) < 4096
    second = material_query(a, str(tmp_path), query='events', limit=2, cursor=page['next_cursor'], max_bytes=4096)
    assert second['records'][0]['id'] != page['records'][0]['id']
    with pytest.raises(PocketError, match='Stale'):
        material_query(a, str(tmp_path), query='voices', cursor=page['next_cursor'])
    external = read_record(a, tmp_path)
    imported = material_import({'kind': 'material', 'material': external}, str(tmp_path), 'external')
    assert imported['material'] == a
    report = midi_analyze(imported['material'], str(tmp_path))
    assert report['basis'] == 'symbolic' and report['listening'] == 'not_performed'
    assert report['measurements']['pitch']['pitch_class_counts'] == {'2': 15}


def test_malformed_rational_hash_ids_and_mutability(tmp_path):
    record = read_record(generated(tmp_path)['alternatives'][0], tmp_path)
    for bad in ({'n': 2, 'd': 4}, {'n': True, 'd': 1}, {'n': 1, 'd': 0}, 0.5):
        with pytest.raises(PocketError):
            rational(bad)
    with pytest.raises(PocketError, match='hash'):
        validate_material({**record, 'material_id': 'changed'})
    duplicate = copy.deepcopy(record)
    duplicate['notes'][1]['id'] = duplicate['notes'][0]['id']
    with pytest.raises(PocketError, match='Duplicate'):
        finalize_material(duplicate)
    bad = copy.deepcopy(record)
    bad['notes'][0]['velocity']['value'] = 0
    with pytest.raises(PocketError):
        finalize_material(bad)
    result = validate_material(record)
    result['notes'][0]['velocity']['value'] = 90
    assert record['notes'][0]['velocity']['value'] == 64


def test_overlap_and_nonfinite_refusal(tmp_path):
    a = generated(tmp_path)['alternatives'][0]
    record = read_record(a, tmp_path)
    selected = material_query(a, str(tmp_path), selection={'note_ids': [record['notes'][1]['id']]})['selection']
    with pytest.raises(PocketError, match='overlap'):
        midi_transform(a, selected, [{'op': 'shift', 'delta_qn': {'n': -3, 'd': 2}}], str(tmp_path), 'overlap')
    record['notes'][0]['pitch']['cents_offset'] = float('nan')
    with pytest.raises(PocketError, match='finite'):
        finalize_material(record)


def test_transport_models_refuse_coercion_and_unknown_constraints(tmp_path):
    from pydantic import TypeAdapter, ValidationError

    from pocket_music.material_types import MaterialRecord, PatternBrief
    adapter = TypeAdapter(PatternBrief)
    with pytest.raises(ValidationError):
        adapter.validate_python({**brief(), 'pitch': True})
    with pytest.raises(ValidationError):
        adapter.validate_python({**brief(), 'constraints': {'max_notes': 1}})
    record = read_record(generated(tmp_path)['alternatives'][0], tmp_path)
    validated = TypeAdapter(MaterialRecord).validate_python(record)
    assert validated == record
    assert type(validated['notes'][0]['pitch']['cents_offset']) is int
    assert material_query(record, str(tmp_path))['schema'] == 'pocket.operation-receipt/v1'
    assert midi_analyze(record, str(tmp_path))['schema'] == 'pocket.operation-receipt/v1'


def saved_plain_live_fixture(tmp_path, mutation=None):
    """Synthetic exact-build tree matching the observed empty/default metadata."""
    import xml.etree.ElementTree as ET

    from test_thread import Fixture

    from pocket_music.thread_queries import inspect_set_summary

    fixture = Fixture(tmp_path)
    fixture.root.attrib = {'MajorVersion': '5', 'MinorVersion': '12.0_12402', 'SchemaChangeCount': '5',
        'Creator': 'Ableton Live 12.4.5', 'Revision': '225ce5e356e024356d5210512bae46fb466f6968'}
    clip = fixture.clip(fixture.track(kind='MidiTrack'), midi=True, source_start=0,
                        source_end=8, start=4, end=12)
    notes = clip.find('Notes')
    key = notes.find('KeyTracks/KeyTrack')
    events = key.find('Notes')
    key[:] = [events, key.find('MidiKey')]
    events[0].attrib = {'Time': '0', 'Duration': '0.25', 'Velocity': '70', 'OffVelocity': '51', 'NoteId': '1'}
    ET.SubElement(events, 'MidiNoteEvent', Time='2', Duration='0.5', Velocity='80', OffVelocity='63', NoteId='2')
    ET.SubElement(ET.SubElement(notes, 'PerNoteEventStore'), 'EventLists')
    ET.SubElement(notes, 'NoteProbabilityGroups')
    ET.SubElement(ET.SubElement(notes, 'ProbabilityGroupIdGenerator'), 'NextId', Value='1')
    ET.SubElement(ET.SubElement(notes, 'NoteIdGenerator'), 'NextId', Value='3')
    if mutation:
        mutation(fixture.root, notes)
    path = fixture.save()
    handle = inspect_set_summary(path, cache_dir=tmp_path / 'cache')['handle']
    return path, {'kind': 'live_clip', 'thread_handle': handle, 'clip_id': 'track:10/clip:0'}


def test_observed_empty_native_metadata_allows_exact_edit_preserving_original_bytes(tmp_path):
    from pocket_music.artifact_store import read_bytes
    from pocket_music.midi_io import midi_export

    path, source = saved_plain_live_fixture(tmp_path)
    original, stamp = path.read_bytes(), path.stat().st_mtime_ns
    store = str(tmp_path / 'store')
    imported = material_import(source, store, 'saved-read')
    before = read_record(imported['material'], store)
    assert before['coverage']['editing_allowed'] is True
    assert before['coverage']['expression'] == 'absent_in_qualified_note_tree'
    assert before['coverage']['native_note_metadata_profile'] == 'live-12.4.5-empty-note-metadata/v1'
    assert before['coverage']['native'] == 'saved_inspection_only'
    assert read_bytes(before['sources'][0]['original_set'], store) == original
    raw_before = read_bytes(before['sources'][0]['raw'], store)
    select = material_query(imported['material'], store,
        selection={'note_ids': [before['notes'][0]['id']]})['selection']
    edited = midi_transform(imported['material'], select,
        [{'op': 'velocity', 'value': 73}, {'op': 'shift', 'delta_qn': {'n': 1, 'd': 8}}], store, 'edit-readback',
        locks={'selected_fields': ['pitch', 'duration', 'release_velocity', 'source_binding']})
    after = read_record(edited['material'], store)
    assert after['notes'][1] == before['notes'][1]
    expected = copy.deepcopy(before['notes'][0])
    expected['onset'] = {'space': 'clip_qn', 'n': 1, 'd': 8}
    expected['velocity']['value'] = 73
    assert after['notes'][0] == expected
    assert read_bytes(after['sources'][0]['raw'], store) == raw_before
    assert read_bytes(after['sources'][0]['original_set'], store) == original
    # SMF cannot carry native IDs/counters; keep the explicit degradation policy.
    with pytest.raises(PocketError, match='opaque_native_note_payload'):
        midi_export(edited['material'], store, str(tmp_path / 'unapproved.mid'), 'export-unapproved')
    exported = midi_export(edited['material'], store, str(tmp_path / 'edited.mid'), 'export',
        loss_policy='approved', approved_losses=['opaque_native_note_payload'])
    assert exported['coverage']['fidelity']['losses'] == ['opaque_native_note_payload']
    reparsed = material_import({'kind': 'smf', 'path': str(tmp_path / 'edited.mid'),
                                'expected_sha256': exported['midi']['sha256']}, store, 'reimport')
    result = read_record(reparsed['material'], store)
    fields = ('pitch', 'onset', 'duration_qn', 'velocity', 'release_velocity', 'channel', 'mute')
    assert [{k: n[k] for k in fields} for n in result['notes']] == [{k: n[k] for k in fields} for n in after['notes']]
    assert rational(result['clips'][0]['length_qn']) == 8
    assert path.read_bytes() == original and path.stat().st_mtime_ns == stamp


@pytest.mark.parametrize('mutation', [
    'expression', 'probability', 'counter_used', 'counter_fraction', 'counter_group', 'unknown',
    'extra_attribute', 'metadata_text', 'different_build', 'duplicate_note_id', 'missing_counter',
    'wrong_order', 'event_probability', 'clip_expression', 'clip_offset', 'clip_groove',
])
def test_unqualified_native_metadata_remains_opaque_and_noneditable(tmp_path, mutation):
    import xml.etree.ElementTree as ET

    def change(root, notes):
        clip = root.find('.//MidiClip')
        if mutation == 'clip_expression':
            container = ET.SubElement(ET.SubElement(clip, 'Envelopes'), 'Envelopes')
            ET.SubElement(container, 'UnknownExpression', Value='1')
        elif mutation == 'clip_offset':
            clip.find('Loop/StartRelative').set('Value', '1')
        elif mutation == 'clip_groove':
            ET.SubElement(ET.SubElement(clip, 'GrooveSettings'), 'GrooveId', Value='1')
        elif mutation == 'expression':
            ET.SubElement(notes.find('PerNoteEventStore/EventLists'), 'PerNoteEventList', NoteId='1')
        elif mutation == 'probability':
            ET.SubElement(notes.find('NoteProbabilityGroups'), 'ProbabilityGroup', Id='1')
        elif mutation.startswith('counter_'):
            target = notes.find('ProbabilityGroupIdGenerator/NextId' if mutation == 'counter_group'
                                else 'NoteIdGenerator/NextId')
            target.set('Value', {'counter_used': '2', 'counter_fraction': '3.5', 'counter_group': '2'}[mutation])
        elif mutation == 'unknown':
            ET.SubElement(notes, 'FutureExpressionStore')
        elif mutation == 'extra_attribute':
            notes.find('PerNoteEventStore/EventLists').set('Enabled', 'true')
        elif mutation == 'metadata_text':
            notes.find('PerNoteEventStore/EventLists').text = 'unrecognized expression payload'
        elif mutation == 'different_build':
            root.set('Revision', 'unqualified-build')
        elif mutation == 'duplicate_note_id':
            notes.find('KeyTracks/KeyTrack/Notes')[1].set('NoteId', '1')
        elif mutation == 'missing_counter':
            notes.remove(notes.find('NoteIdGenerator'))
        elif mutation == 'wrong_order':
            notes[:] = list(reversed(notes))
        elif mutation == 'event_probability':
            notes.find('KeyTracks/KeyTrack/Notes')[0].set('Probability', '0.5')

    path, source = saved_plain_live_fixture(tmp_path, change)
    before = path.read_bytes()
    store = str(tmp_path / 'store')
    imported = material_import(source, store, 'saved-read')
    record = read_record(imported['material'], store)
    assert record['coverage']['editing_allowed'] is False
    assert record['coverage']['native_note_metadata_profile'] is None
    selected = material_query(imported['material'], store,
        selection={'note_ids': [record['notes'][0]['id']]})['selection']
    with pytest.raises(PocketError, match='fidelity profile'):
        midi_transform(imported['material'], selected, [{'op': 'velocity', 'value': 73}], store, 'edit')
    assert path.read_bytes() == before
