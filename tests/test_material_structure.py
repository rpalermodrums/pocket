# SPDX-License-Identifier: AGPL-3.0-only
"""File-only phrase graph acceptance, with source and composition checks."""
import copy
import json
import shutil
from fractions import Fraction

import pytest

from pocket_music.artifact_store import canonical_bytes, put_bytes, put_record, read_bytes, read_record
from pocket_music.errors import PocketError
from pocket_music.material import (
    finalize_material,
    make_note,
    material_query,
    new_material,
    resolve_selection,
)
from pocket_music.material_structure import material_structure
from pocket_music.midi_edit import midi_transform


def attributed(statement='Synthetic phrase boundary; no listening performed'):
    return {'actor': 'fixture author', 'actor_kind': 'agent', 'statement': statement,
            'uncertainty': ['A phrase boundary is a supplied hypothesis.'], 'evidence': []}


def source_material(*, second_clip=False, count=4):
    notes = [make_note(f'n:{index}', index - 1, Fraction(3, 2) if index == 1 else Fraction(1, 4),
                       60 + index % 12, 70 + index % 30, release=35)
             for index in range(count)]
    clips = [{'id': 'clip:one', 'track_id': 'track:one', 'origin': {'space': 'phrase_qn', 'n': 12, 'd': 1},
              'length_qn': {'n': count + 1, 'd': 1}, 'loop': False,
              'note_ids': [n['id'] for n in notes], 'event_ids': [], 'curve_ids': []}]
    if second_clip:
        other = make_note('n:other', 0, Fraction(1, 4), 60, 77)
        notes.append(other)
        clips.append({**copy.deepcopy(clips[0]), 'id': 'clip:two', 'note_ids': ['n:other']})
    return new_material('phrase-fixture', tracks=[{'id': 'track:one', 'name': 'External material'}],
                        clips=clips, notes=notes, provenance={'provider': 'external fixture author'})


def node(material, key='a', node_id='phrase:a', notes=None, start=-1, end=1, clip='clip:one'):
    return {'node_id': node_id, 'kind': 'phrase', 'label': node_id,
            'material_key': key, 'material_revision': material['revision_sha256'], 'clip_id': clip,
            'space': 'clip_qn', 'span_qn': {'start': {'n': start, 'd': 1}, 'end': {'n': end, 'd': 1}},
            'note_ids': ['n:0', 'n:1'] if notes is None else notes, 'attribution': attributed()}


def relation(start='phrase:a', end='phrase:b', kind='variation', relation_id='relation:ab'):
    return {'relation_id': relation_id, 'from_node': start, 'to_node': end, 'kind': kind,
            'identity_claims': ['rhythm', 'perceptual'], 'attribution': attributed('A supplied relationship')}


def definition(material=None):
    material = source_material() if material is None else material
    return {'label': 'Phrase alternatives', 'materials': [{'key': 'a', 'material': material}],
            'nodes': [node(material)], 'relations': [], 'attribution': attributed(), 'cycle_policy': 'reject'}


def create(tmp_path, value=None, request='create'):
    return material_structure('create', str(tmp_path), request, definition() if value is None else value)


def query(tmp_path, handle, **kwargs):
    return material_structure('query', str(tmp_path), structure=handle, **kwargs)


def test_external_material_handle_equivalence_and_replay(tmp_path):
    external = source_material()
    before = copy.deepcopy(external)
    supplied = definition(external)
    result = create(tmp_path, supplied)
    handle = result['artifacts']['structure']
    assert supplied == definition(before) and external == before
    assert result['schema'] == 'pocket.operation-receipt/v1'
    assert result['coverage']['human_listening'] == 'not_established'
    assert result['coverage']['motif_identity'] == 'attributed_not_inferred'
    assert create(tmp_path, supplied) == result
    supplied_handle = definition(before)
    supplied_handle['materials'][0]['material'] = put_record(external, tmp_path)
    assert create(tmp_path, supplied_handle, 'via-handle')['artifacts']['structure'] == handle
    # Byte-valid but noncanonical source serialization normalizes to the same identity.
    unusual = put_bytes(json.dumps(external, indent=2).encode(), tmp_path, 'external.json', external['schema'])
    supplied_handle['materials'][0]['material'] = unusual
    assert create(tmp_path, supplied_handle, 'via-noncanonical')['artifacts']['structure'] == handle
    with pytest.raises(PocketError, match='idempotency_conflict'):
        create(tmp_path, {**supplied, 'label': 'Different'}, 'create')


def test_exact_spans_keep_crossing_gates_and_external_bytes(tmp_path):
    material = source_material()
    raw = canonical_bytes(material)
    handle = put_record(material, tmp_path)
    supplied = definition(material)
    supplied['nodes'].append(node(material, node_id='empty', notes=[], start=0, end=1))
    result = create(tmp_path, supplied)
    graph = read_record(result['artifacts']['structure'], tmp_path)
    assert graph['nodes'][0]['gate_crossing_count'] == 1
    assert graph['nodes'][0]['source_clip_origin'] == {'space': 'phrase_qn', 'n': 12, 'd': 1}
    assert graph['nodes'][1]['note_count'] == 0
    assert read_bytes(handle, tmp_path) == raw
    assert material['notes'][1]['duration_qn'] == {'n': 3, 'd': 2}
    assert material['notes'][1]['release_velocity']['value'] == 35
    assert graph['nodes'][0]['selection_sha256'] == resolve_selection(material, {'note_ids': ['n:0', 'n:1']})['selection_sha256']
    supplied['nodes'][0]['note_ids'].append('n:2')  # onset exactly at exclusive end = 1
    with pytest.raises(PocketError, match='outside its onset span'):
        create(tmp_path, supplied, 'boundary')


def test_cross_clip_same_position_never_becomes_same_occurrence(tmp_path):
    material = source_material(second_clip=True)
    value = definition(material)
    value['nodes'] = [node(material, notes=['n:1'], start=0, end=1),
                      node(material, node_id='other', notes=['n:other'], start=0, end=1, clip='clip:two')]
    graph = read_record(create(tmp_path, value)['artifacts']['structure'], tmp_path)
    assert graph['nodes'][0]['selection_sha256'] != graph['nodes'][1]['selection_sha256']
    assert [n['clip_id'] for n in graph['nodes']] == ['clip:one', 'clip:two']
    value['nodes'][0]['note_ids'] = ['n:other']
    with pytest.raises(PocketError, match='another clip'):
        create(tmp_path, value, 'wrong-occurrence')


def test_composed_members_selection_edit_and_declared_derivation(tmp_path):
    parent = source_material()
    original = create(tmp_path, definition(parent))['artifacts']['structure']
    page = query(tmp_path, original, section='members', node_ids=['phrase:a'])
    selection = material_query(page['node']['material'], str(tmp_path),
                               selection={'note_ids': [r['note_id'] for r in page['rows']]})['selection']
    assert selection['selection_sha256'] == page['node']['selection_sha256']
    edited = midi_transform(parent, selection, [{'op': 'velocity', 'value': 90}], str(tmp_path), 'edit',
                            locks={'selected_fields': ['onset', 'duration', 'pitch', 'release_velocity']})
    child = read_record(edited['material'], tmp_path)
    value = definition(parent)
    value['materials'].append({'key': 'b', 'material': child})
    value['nodes'].append(node(child, 'b', 'phrase:b'))
    value['relations'] = [relation(kind='derivation')]
    value['parent_structure'] = original
    new = create(tmp_path, value, 'derived')['artifacts']['structure']
    assert query(tmp_path, new)['rows'][0]['parent_structure'] == original
    assert child['parent_revision'] == parent['revision_sha256']
    assert child['notes'][2:] == parent['notes'][2:]
    for old, changed in zip(parent['notes'][:2], child['notes'][:2]):
        assert {k: v for k, v in old.items() if k != 'velocity'} == {k: v for k, v in changed.items() if k != 'velocity'}
    unrelated = finalize_material({**child, 'material_id': 'unrelated'})
    value['materials'][1]['material'] = unrelated
    value['nodes'][1]['material_revision'] = unrelated['revision_sha256']
    with pytest.raises(PocketError, match='parent lineage'):
        create(tmp_path, value, 'unrelated')


@pytest.mark.parametrize('mutation', [
    lambda d: d.update(unknown=True),
    lambda d: d['nodes'][0].update(unknown=True),
    lambda d: d['nodes'][0].update(material_revision='0' * 64),
    lambda d: d['nodes'][0].update(clip_id='missing'),
    lambda d: d['nodes'][0].update(material_key='missing'),
    lambda d: d['nodes'][0].update(note_ids=['missing']),
    lambda d: d['nodes'][0].update(note_ids=['n:0', 'n:0']),
    lambda d: d['nodes'][0]['span_qn'].update(start={'n': True, 'd': 1}),
    lambda d: d['nodes'][0]['span_qn'].update(start={'n': -2, 'd': 2}),
    lambda d: d['nodes'][0]['span_qn'].update(start={'n': 0, 'd': 0}),
    lambda d: d['nodes'][0]['span_qn'].update(start={'n': -(2**53) - 1, 'd': 1}),
    lambda d: d['nodes'][0]['span_qn'].update(start={'n': 1, 'd': 1}),
    lambda d: d['nodes'][0]['span_qn'].update(end={'n': 6, 'd': 1}),
    lambda d: d['nodes'][0].update(space='arrangement_qn'),
    lambda d: d['materials'].append(copy.deepcopy(d['materials'][0])),
    lambda d: d['nodes'].append(copy.deepcopy(d['nodes'][0])),
    lambda d: d.update(cycle_policy='allow'),
    lambda d: d['attribution'].update(actor_kind='inferred'),
    lambda d: d['attribution'].update(statement=''),
    lambda d: d['attribution'].update(uncertainty=['x'] * 9),
])
def test_malformed_definition_never_publishes_structure(tmp_path, mutation):
    value = definition()
    mutation(value)
    with pytest.raises(PocketError):
        create(tmp_path, value)
    assert not list((tmp_path / 'artifacts').glob('*/record.json'))


def linked_definition():
    value = definition()
    material = value['materials'][0]['material']
    value['nodes'].extend([node(material, node_id='phrase:b'), node(material, node_id='phrase:a2')])
    value['relations'] = [relation(), relation('phrase:b', 'phrase:a2', 'return', 'relation:ba2')]
    return value


def test_returns_use_distinct_occurrences_and_claims_remain_attributed(tmp_path):
    value = linked_definition()
    value['relations'][0]['identity_claims'] = ['exact_events', 'perceptual']
    result = create(tmp_path, value)
    graph = read_record(result['artifacts']['structure'], tmp_path)
    assert [n['node_id'] for n in graph['nodes']] == ['phrase:a', 'phrase:b', 'phrase:a2']
    assert graph['relations'][0]['identity_claims'] == ['exact_events', 'perceptual']
    assert graph['coverage']['motif_identity'] == 'attributed_not_inferred'
    value['relations'].append(relation('phrase:a2', 'phrase:a', 'repeat', 'cycle'))
    with pytest.raises(PocketError, match='cycle rejected') as error:
        create(tmp_path, value, 'cycle')
    assert len(str(error.value)) < 1200


@pytest.mark.parametrize('mutation', [
    lambda d: d['relations'][0].update(to_node='missing'),
    lambda d: d['relations'][0].update(to_node='phrase:a'),
    lambda d: d['relations'][1].update(relation_id='relation:ab'),
    lambda d: d['relations'].append({**copy.deepcopy(d['relations'][0]), 'relation_id': 'duplicate-edge'}),
    lambda d: d['relations'][0].update(identity_claims=['rhythm', 'rhythm']),
    lambda d: d['relations'][0].update(identity_claims=['harmony_certified']),
    lambda d: d['relations'][0].update(kind='schedule'),
    lambda d: d['relations'][0].update(confidence=1),
])
def test_malformed_relationships_refused(tmp_path, mutation):
    value = linked_definition()
    mutation(value)
    with pytest.raises(PocketError):
        create(tmp_path, value)


def test_bounded_pages_keep_full_receipt_with_forward_cursors(tmp_path):
    material = source_material(count=80)
    value = definition(material)
    value['nodes'] = [node(material, node_id=f'phrase:{i}', notes=[f'n:{i}'], start=-1, end=81) for i in range(80)]
    value['nodes'][0]['note_ids'] = [n['id'] for n in material['notes']][::-1]
    handle = create(tmp_path, value)['artifacts']['structure']
    cursor, ids, sizes = None, [], []
    while True:
        page = query(tmp_path, handle, section='nodes', cursor=cursor, limit=4, max_bytes=5000)
        sizes.append(len(canonical_bytes(page)))
        assert page['rows'] and page['returned'] <= 4
        assert all('note_ids' not in row and row['note_ids_omitted'] == row['note_count'] for row in page['rows'])
        ids.extend(row['node_id'] for row in page['rows'])
        assert page['next_cursor'] != cursor or cursor is None
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert ids == [f'phrase:{i}' for i in range(80)] and max(sizes) <= 5000
    cursor, members = None, []
    while True:
        page = query(tmp_path, handle, section='members', node_ids=['phrase:0'], limit=7, cursor=cursor, max_bytes=2500)
        assert len(canonical_bytes(page)) <= 2500
        members.extend(page['rows'])
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert [row['note_id'] for row in members] == [f'n:{i}' for i in range(79, -1, -1)]
    assert [row['member_index'] for row in members] == list(range(80))
    with pytest.raises(PocketError, match='byte budget'):
        query(tmp_path, handle, section='nodes', max_bytes=1024)


def test_cursor_is_bound_to_structure_section_filter_and_safe_offset(tmp_path):
    handle = create(tmp_path, linked_definition())['artifacts']['structure']
    first = query(tmp_path, handle, section='nodes', limit=1)
    assert first['next_cursor']
    for kwargs in ({'section': 'relations'}, {'section': 'nodes', 'node_ids': ['phrase:a']},
                   {'section': 'nodes', 'cursor': 'garbage'}, {'section': 'nodes', 'cursor': first['next_cursor'].split(':')[0] + ':+1'}):
        with pytest.raises(PocketError):
            query(tmp_path, handle, cursor=kwargs.pop('cursor', first['next_cursor']), **kwargs)
    other = create(tmp_path, {**linked_definition(), 'label': 'New graph'}, 'new')['artifacts']['structure']
    with pytest.raises(PocketError, match='Stale'):
        query(tmp_path, other, section='nodes', cursor=first['next_cursor'])
    filtered = query(tmp_path, handle, section='relations', node_ids=['phrase:b'])
    assert filtered['filter_scope'] == 'either_endpoint' and len(filtered['rows']) == 2
    assert query(tmp_path, handle, section='nodes', node_ids=[])['rows'] == []
    with pytest.raises(PocketError):
        query(tmp_path, handle, section='members', node_ids=[])
    with pytest.raises(PocketError):
        query(tmp_path, handle, section='nodes', node_ids=['phrase:a', 'phrase:a'])


def test_parent_chain_maximum_includes_new_child(tmp_path):
    value = definition()
    parent = None
    for index in range(32):
        if parent is not None:
            value['parent_structure'] = parent
        parent = create(tmp_path, value, f'parent-{index}')['artifacts']['structure']
    assert query(tmp_path, parent)['rows'][0]['counts']['nodes'] == 1
    value['parent_structure'] = parent
    with pytest.raises(PocketError, match='chain exceeds 32'):
        create(tmp_path, value, 'too-deep')


def test_transitive_source_corruption_rejects_query_replay_and_parent(tmp_path):
    raw = put_bytes(b'synthetic immutable original', tmp_path, 'source.bin', 'application/octet-stream')
    evidence = put_record({'schema': 'fixture.evidence/v1', 'source': raw}, tmp_path)
    value = definition()
    value['attribution']['evidence'] = [evidence]
    result = create(tmp_path, value)
    handle = result['artifacts']['structure']
    (tmp_path / raw['artifact_uri']).write_bytes(b'changed source')
    for operation in (lambda: query(tmp_path, handle), lambda: create(tmp_path, value),
                      lambda: create(tmp_path, {**definition(), 'parent_structure': handle}, 'child')):
        with pytest.raises(PocketError, match='integrity'):
            operation()


def test_structure_and_material_tamper_and_relocation(tmp_path):
    store = tmp_path / 'original'
    handle = create(store)['artifacts']['structure']
    original = query(store, handle)
    moved = tmp_path / 'moved'
    shutil.copytree(store, moved)
    shutil.rmtree(store)
    assert query(moved, handle) == original
    record = read_record(handle, moved)
    record['nodes'][0]['selection_sha256'] = '0' * 64
    forged = put_record(record, moved)
    with pytest.raises(PocketError, match='derived identities'):
        query(moved, forged)
    material = read_record(handle, moved)['materials'][0]['material']
    (moved / material['artifact_uri']).write_bytes(b'changed material')
    with pytest.raises(PocketError, match='integrity'):
        query(moved, handle)


def test_strict_transport_models_preserve_literals_and_reject_coercion():
    from pydantic import TypeAdapter, ValidationError

    from pocket_music.structure_types import StructureDefinition

    value = definition()
    assert TypeAdapter(StructureDefinition).validate_python(value) == value
    for mutation in (lambda d: d['nodes'][0]['span_qn']['start'].update(n=True),
                     lambda d: d['nodes'][0]['span_qn']['start'].update(n='-1'),
                     lambda d: d['nodes'][0].update(unknown='drop me'),
                     lambda d: d['materials'][0]['material']['notes'][0]['pitch'].update(midi_note=True)):
        invalid = copy.deepcopy(value)
        mutation(invalid)
        with pytest.raises(ValidationError):
            TypeAdapter(StructureDefinition).validate_python(invalid)


@pytest.mark.parametrize('kwargs', [{'limit': True}, {'limit': '2'}, {'limit': 257}, {'max_bytes': 1000},
                                     {'max_bytes': False}, {'max_bytes': 65537}, {'section': 'all'}])
def test_query_invalid_scalar_limits(tmp_path, kwargs):
    handle = create(tmp_path)['artifacts']['structure']
    with pytest.raises(PocketError):
        query(tmp_path, handle, **kwargs)


@pytest.mark.parametrize('field,maximum', [('materials', 64), ('nodes', 1024), ('relations', 4096)])
def test_definition_array_limits_are_explicit(tmp_path, field, maximum):
    value = linked_definition()
    value[field] = [copy.deepcopy(value[field][0]) for _ in range(maximum + 1)]
    with pytest.raises(PocketError, match=f'{maximum} entries'):
        create(tmp_path, value)


def test_total_membership_and_member_array_bounds(tmp_path):
    value = definition()
    value['nodes'][0]['note_ids'] = ['n:0'] * 4097
    with pytest.raises(PocketError, match='4096 entries'):
        create(tmp_path, value, 'member-limit')
    source = source_material(count=17)
    value = definition(source)
    value['nodes'] = [node(source, node_id=f'node:{i}', notes=[n['id'] for n in source['notes']], end=18)
                      for i in range(1024)]
    with pytest.raises(PocketError, match='16384 declared memberships'):
        create(tmp_path, value, 'total-limit')


def test_cursor_rejects_nontext_without_leaking_builtin_errors(tmp_path):
    handle = create(tmp_path)['artifacts']['structure']
    for cursor in (b'cursor:1', 1, True, [], {}, '', 'x' * 201):
        with pytest.raises(PocketError, match='cursor'):
            query(tmp_path, handle, cursor=cursor)
