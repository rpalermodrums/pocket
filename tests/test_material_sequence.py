"""Independent expected placements, preservation and refusal checks for sequences."""
import copy
import json
import shutil
from fractions import Fraction

import pytest

from pocket_music.artifact_store import canonical_bytes, put_bytes, put_record, read_bytes, read_record
from pocket_music.errors import PocketError
from pocket_music.material import (
    finalize_material,
    load_material,
    make_note,
    material_query,
    new_material,
    qn,
)
from pocket_music.material_sequence import material_sequence
from pocket_music.midi_edit import midi_transform


def source():
    notes = [make_note('n:a', Fraction(1, 3), Fraction(1, 6), 60, 73, release=29,
                       role='answer', voice='voice:answer'),
             make_note('n:b', 1, Fraction(1, 4), 67, 86, release=41, voice='voice:lead')]
    return new_material('sequence-external', tracks=[{'id': 't:a', 'name': 'User material'}],
        clips=[{'id': 'c:a', 'track_id': 't:a', 'origin': {'space': 'arrangement_qn', 'n': 99, 'd': 1},
                'length_qn': qn(2), 'loop': False, 'note_ids': ['n:a'], 'event_ids': [], 'curve_ids': []},
               {'id': 'c:b', 'track_id': 't:a', 'origin': {'space': 'phrase_qn', 'n': -7, 'd': 1},
                'length_qn': qn(3), 'loop': False, 'note_ids': ['n:b'], 'event_ids': [], 'curve_ids': []}],
        notes=notes)


def definition(material=None):
    record = source() if material is None else material
    return {'label': 'Later return', 'materials': [{'key': 'a', 'material': record}],
            'clock': {'schema': 'pocket.time-context/v1', 'context_id': 'clock:declared',
                      'attribution': 'Fixture author explicitly chose these placements.'},
            'origin': {'space': 'arrangement_qn', 'n': 64, 'd': 1}, 'length_qn': qn(10),
            'occurrences': [
                {'occurrence_id': 'first', 'material_key': 'a', 'material_revision': record['revision_sha256'],
                 'clip_id': 'c:a', 'at_qn': qn(0)},
                {'occurrence_id': 'response', 'material_key': 'a', 'material_revision': record['revision_sha256'],
                 'clip_id': 'c:b', 'at_qn': qn(2)},
                {'occurrence_id': 'return', 'material_key': 'a', 'material_revision': record['revision_sha256'],
                 'clip_id': 'c:a', 'at_qn': qn(6)}],
            'controller_policy': 'reject_present', 'expression_policy': 'reject_present',
            'overlap_policy': 'reject_same_channel_pitch'}


def run(tmp_path, value=None, request='sequence'):
    return material_sequence(store_root=str(tmp_path), request_id=request,
                             definition=definition() if value is None else value)


def test_exact_cross_clip_placements_new_ids_and_retained_whole_parents(tmp_path):
    value = definition()
    before = copy.deepcopy(value)
    result = run(tmp_path, value)
    child = load_material(result['artifacts']['material'], tmp_path)
    report = read_record(result['artifacts']['sequence'], tmp_path)
    assert value == before
    assert [note['onset'] for note in child['notes']] == [
        {'space': 'clip_qn', 'n': 1, 'd': 3}, {'space': 'clip_qn', 'n': 3, 'd': 1},
        {'space': 'clip_qn', 'n': 19, 'd': 3}]
    assert [note['derived_from'] for note in child['notes']] == [['n:a'], ['n:b'], ['n:a']]
    assert len({note['id'] for note in child['notes']}) == 3
    assert len({note['voice_id'] for note in child['notes']}) == 3
    for note, original in zip(child['notes'], [source()['notes'][0], source()['notes'][1], source()['notes'][0]], strict=True):
        for key in set(note) - {'id', 'onset', 'voice_id', 'derived_from'}:
            assert note[key] == original[key]
    assert child['parent_revision'] is None
    assert child['clips'][0]['length_qn'] == qn(10)
    assert child['clips'][0]['origin'] == before['origin']
    assert read_record(child['sources'][0]['material'], tmp_path) == source()
    assert report['occurrences'][0]['source_origin'] == source()['clips'][0]['origin']
    assert report['occurrences'][1]['source_origin'] == source()['clips'][1]['origin']
    assert len(report['note_derivations']) == 3
    assert result['coverage']['native_execution'] is False
    assert len(canonical_bytes(result)) < 16000


def test_inline_handle_noncanonical_parity_retry_and_relocation(tmp_path):
    value = definition()
    direct = run(tmp_path, value)
    assert run(tmp_path, value) == direct
    for index, handle in enumerate([put_record(source(), tmp_path),
                                   put_bytes(json.dumps(source(), indent=2).encode(), tmp_path,
                                             'external.json', 'pocket.material/v1')]):
        supplied = copy.deepcopy(value)
        supplied['materials'][0]['material'] = handle
        assert run(tmp_path, supplied, f'handle-{index}')['artifacts'] == direct['artifacts']
    shorthand = copy.deepcopy(value)
    shorthand['length_qn'] = 10
    for occurrence in shorthand['occurrences']:
        occurrence['at_qn'] = occurrence['at_qn']['n']
    assert run(tmp_path, shorthand, 'integer-shorthand')['artifacts'] == direct['artifacts']
    moved = tmp_path / 'relocated'
    shutil.copytree(tmp_path / 'artifacts', moved / 'artifacts')
    assert load_material(direct['artifacts']['material'], moved)['notes']
    changed = {**value, 'label': 'different'}
    with pytest.raises(PocketError, match='idempotency_conflict'):
        run(tmp_path, changed)


def test_composed_external_material_selected_edit_and_second_sequence(tmp_path):
    first = run(tmp_path)
    child = load_material(first['artifacts']['material'], tmp_path)
    selection = material_query(first['artifacts']['material'], str(tmp_path), query='events',
                               selection={'note_ids': [child['notes'][1]['id']]})['selection']
    edited = midi_transform(material=first['artifacts']['material'], store_root=str(tmp_path),
                            request_id='selected-edit', selection=selection,
                            operations=[{'op': 'velocity', 'value': 99}],
                            locks={'selected_fields': ['pitch', 'onset']})
    changed = load_material(edited['artifacts']['material'], tmp_path)
    assert [note['velocity']['value'] for note in changed['notes']] == [73, 99, 73]
    assert read_bytes(first['artifacts']['material'], tmp_path) == canonical_bytes(child)
    value = definition(child)
    value['occurrences'] = [{'occurrence_id': 'whole', 'material_key': 'a',
                            'material_revision': child['revision_sha256'],
                            'clip_id': child['clips'][0]['id'], 'at_qn': qn(0)}]
    repeated = run(tmp_path, value, 'second-sequence')
    assert len(load_material(repeated['artifacts']['material'], tmp_path)['notes']) == 3


@pytest.mark.parametrize('mutation,match', [
    (lambda d: d.update(extra=True), 'unexpected'),
    (lambda d: d['clock'].update(unchecked=True), 'unexpected'),
    (lambda d: d['clock'].update(schema='pocket.fake/v1'), 'clock|context'),
    (lambda d: d['origin'].update(space='host_seconds'), 'space'),
    (lambda d: d['origin'].update(n=True), 'integer'),
    (lambda d: d['length_qn'].update(d=0), 'range'),
    (lambda d: d['length_qn'].update(n=0), 'positive'),
    (lambda d: d['occurrences'][0].update(at_qn={'n': 2, 'd': 2}), 'reduced'),
    (lambda d: d['occurrences'][0].update(at_qn=False), 'fields'),
    (lambda d: d['occurrences'][0].update(material_revision='stale'), 'Stale'),
    (lambda d: d['occurrences'][0].update(clip_id='absent'), 'Unresolved'),
    (lambda d: d['occurrences'][0].update(material_key='absent'), 'Unresolved'),
    (lambda d: d['occurrences'][0].update(at_qn=qn(-1)), 'fit'),
    (lambda d: d['occurrences'][0].update(at_qn=qn(9)), 'including rests'),
    (lambda d: d['occurrences'][1].update(occurrence_id='first'), 'Duplicate'),
    (lambda d: d['materials'].append(copy.deepcopy(d['materials'][0])), 'Duplicate'),
    (lambda d: d.update(controller_policy='drop'), 'policy'),
    (lambda d: d.update(expression_policy='drop'), 'policy'),
    (lambda d: d.update(overlap_policy='allow'), 'policy'),
    (lambda d: d.update(occurrences=[]), 'entries'),
])
def test_malformed_stale_ambiguous_definitions_refuse(tmp_path, mutation, match):
    value = definition()
    mutation(value)
    with pytest.raises(PocketError, match=match):
        run(tmp_path, value)
    assert not (tmp_path / 'artifacts').exists()


@pytest.mark.parametrize('change,match', [
    (lambda m: m['coverage'].update(editing_allowed=False), 'editable'),
    (lambda m: m['clips'][0].update(groove='unknown'), 'unexpected'),
    (lambda m: m['notes'][0].update(source_binding={'kind': 'native'}), 'bindings'),
    (lambda m: m['sources'].append({'kind': 'live_clip'}), 'source semantics'),
    (lambda m: m['notes'][0].update(onset={'space': 'clip_qn', 'n': -1, 'd': 1}), 'inside'),
    (lambda m: m['notes'][0].update(duration_qn=qn(3)), 'inside'),
])
def test_opaque_source_and_cross_boundary_gates_refuse(tmp_path, change, match):
    material = source()
    change(material)
    material = finalize_material(material)
    with pytest.raises(PocketError, match=match):
        run(tmp_path, definition(material))


def test_ordered_raw_controller_is_never_silently_discarded(tmp_path):
    material = source()
    material['events'] = [{'id': 'e:sustain', 'time': {'space': 'clip_qn', 'n': 0, 'd': 1},
                           'order': 0, 'message_type': 'control_change', 'is_meta': False,
                           'bytes': [176, 64, 127]}]
    material['clips'][0]['event_ids'] = ['e:sustain']
    material = finalize_material(material)
    with pytest.raises(PocketError, match='events/controllers'):
        run(tmp_path, definition(material))


def test_same_channel_pitch_overlap_refuses_but_endpoint_contact_passes(tmp_path):
    value = definition()
    value['occurrences'][2]['at_qn'] = qn(0)
    with pytest.raises(PocketError, match='overlapping gates'):
        run(tmp_path, value)
    assert not (tmp_path / 'artifacts').exists()
    value['occurrences'][2]['at_qn'] = qn(Fraction(1, 6))
    result = run(tmp_path, value, 'touching')
    assert result['change_summary']['notes'] == 3


def test_distinct_pitch_polyphony_empty_clip_and_rest_length(tmp_path):
    simultaneous = source()
    simultaneous['notes'][1]['onset'] = copy.deepcopy(simultaneous['notes'][0]['onset'])
    value = definition(finalize_material(simultaneous))
    value['occurrences'][1]['at_qn'] = qn(0)
    assert run(tmp_path, value)['change_summary']['notes'] == 3
    material = source()
    material['clips'][0]['note_ids'] = []
    material['notes'] = material['notes'][1:]
    material = finalize_material(material)
    result = run(tmp_path, definition(material), 'empty')
    assert result['change_summary']['notes'] == 1
    assert load_material(result['artifacts']['material'], tmp_path)['clips'][0]['length_qn'] == qn(10)


def test_parent_tampering_breaks_retry_and_derived_material_load(tmp_path):
    result = run(tmp_path)
    record = load_material(result['artifacts']['material'], tmp_path)
    source_path = tmp_path / record['sources'][0]['material']['artifact_uri']
    source_path.write_bytes(b'corrupt')
    with pytest.raises(PocketError, match='integrity'):
        run(tmp_path)
    with pytest.raises(PocketError, match='integrity'):
        load_material(result['artifacts']['material'], tmp_path)


def test_occurrence_budget_and_output_budget_refuse_before_publication(tmp_path):
    value = definition()
    value['occurrences'] *= 86
    with pytest.raises(PocketError, match='entries'):
        run(tmp_path, value)
    material = source()
    material['notes'] = [make_note(f'n:{i}', 0, 1, i % 128, 73) for i in range(4097)]
    material['clips'][0]['note_ids'] = [note['id'] for note in material['notes']]
    material['clips'][1]['note_ids'] = []
    material = finalize_material(material)
    with pytest.raises(PocketError, match='8192'):
        run(tmp_path, definition(material), 'output-bound')


def test_failed_publication_is_retained_and_not_retried(tmp_path, monkeypatch):
    import importlib
    module = importlib.import_module('pocket_music.material_sequence')
    def fail(*args):
        raise OSError('injected publication failure')
    monkeypatch.setattr(module, 'put_record', fail)
    with pytest.raises(OSError, match='injected'):
        run(tmp_path)
    journal = json.loads((tmp_path / 'requests/sequence/journal.json').read_text())
    assert journal['state'] == 'failed'
    with pytest.raises(PocketError, match='did not complete'):
        run(tmp_path)
