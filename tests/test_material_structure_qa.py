"""Independent adversarial phrase-ledger QA; explicit synthetic musical material."""
from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path

import pytest

from pocket_music.artifact_store import canonical_bytes, put_bytes, put_record, read_record
from pocket_music.errors import PocketError
from pocket_music.material import finalize_material, make_note, new_material, resolve_selection
from pocket_music.material_structure import material_structure


def q(n, d=1):
    number = Fraction(n, d)
    return {'n': number.numerator, 'd': number.denominator}


def attribution(evidence=None):
    return {'actor': 'Independent fixture author', 'actor_kind': 'agent',
            'statement': 'A boundary hypothesis; no listening assertion',
            'uncertainty': ['The pickup may belong to the preceding phrase', 'An alternative boundary remains valid'],
            'evidence': evidence or []}


def literal():
    rows = [make_note('pickup', Fraction(-1, 2), 1, 60, 73, release=37),
            make_note('sustain', 0, 2, 64, 81, release=55),
            make_note('answer', 1, Fraction(1, 4), 67, 66, release=18),
            make_note('other-clip', 0, 1, 72, 71)]
    return new_material('external-independent-phrase-input', tracks=[{'id': 'track-a'}, {'id': 'track-b'}], notes=rows,
        clips=[{'id': 'clip-a', 'track_id': 'track-a', 'origin': {'space': 'phrase_qn', **q(0)},
                'length_qn': q(4), 'loop': False, 'note_ids': ['pickup', 'sustain', 'answer'], 'event_ids': [], 'curve_ids': []},
               {'id': 'clip-b', 'track_id': 'track-b', 'origin': {'space': 'arrangement_qn', **q(100)},
                'length_qn': q(4), 'loop': False, 'note_ids': ['other-clip'], 'event_ids': [], 'curve_ids': []}],
        provenance={'provider': 'external_fixture_no_generation'})


def definition(material=None):
    record = literal() if material is None else material
    nodes = []
    for node_id, clip, start, end, members in [('motif', 'clip-a', -1, 1, ['sustain', 'pickup']),
                                             ('answer', 'clip-a', 1, 2, ['answer']),
                                             ('return', 'clip-b', 0, 2, ['other-clip'])]:
        nodes.append({'node_id': node_id, 'kind': 'motif_reference' if node_id == 'motif' else 'phrase',
                      'label': node_id, 'material_key': 'source', 'material_revision': record['revision_sha256'],
                      'clip_id': clip, 'space': 'clip_qn', 'span_qn': {'start': q(start), 'end': q(end)},
                      'note_ids': members, 'attribution': attribution()})
    return {'label': 'Explicit independent phrase ledger', 'materials': [{'key': 'source', 'material': record}],
            'nodes': nodes, 'relations': [
                {'relation_id': 'variation', 'from_node': 'motif', 'to_node': 'answer', 'kind': 'variation',
                 'identity_claims': ['rhythm', 'perceptual'], 'attribution': attribution()},
                {'relation_id': 'return-link', 'from_node': 'answer', 'to_node': 'return', 'kind': 'return',
                 'identity_claims': ['exact_events'], 'attribution': attribution()}],
            'attribution': attribution(), 'cycle_policy': 'reject'}


def create(tmp_path, data=None, request_id='create'):
    store = str(tmp_path / 'store')
    result = material_structure('create', store, request_id=request_id, definition=data or definition())
    return result['artifacts']['structure'], store


def query(handle, store, **kwargs):
    return material_structure('query', store, structure=handle, **kwargs)


def test_qa_literal_material_independence_handle_equivalence_and_exact_source_preservation(tmp_path, monkeypatch):
    import builtins
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name in {'mido', 'soundfile'} or any(term in name for term in ('native_candidates', 'instruments', 'baste', 'midi_generate')):
            raise AssertionError('Unrelated dependency required: ' + name)
        return original(name, *args, **kwargs)
    source = literal()
    frozen = copy.deepcopy(source)
    data = definition(source)
    untouched = copy.deepcopy(data)
    monkeypatch.setattr(builtins, '__import__', guarded)
    handle, store = create(tmp_path, data)
    assert source == frozen and data == untouched
    supplied = put_record(source, store)
    by_handle = copy.deepcopy(data)
    by_handle['materials'][0]['material'] = supplied
    same, _ = create(tmp_path, by_handle, 'handle-equivalent')
    assert same == handle and read_record(supplied, store) == frozen
    stored = read_record(handle, store)
    assert stored['coverage']['human_listening'] == 'not_established'
    assert stored['coverage']['native_execution'] is False


def test_qa_pickup_gate_crossing_order_uncertainty_and_other_clip_origin_remain_distinct(tmp_path):
    data = definition()
    handle, store = create(tmp_path, data)
    record = read_record(handle, store)
    motif, answer, other = record['nodes']
    assert motif['note_ids'] == ['sustain', 'pickup']
    expected = resolve_selection(data['materials'][0]['material'], {'clip_ids': ['clip-a'],
                                  'note_ids': ['pickup', 'sustain'], 'span_qn': [q(-1), q(1)]})
    assert motif['selection_sha256'] == expected['selection_sha256']
    assert motif['gate_crossing_count'] == 1
    assert other['source_clip_origin'] == {'space': 'arrangement_qn', **q(100)}
    assert motif['source_clip_origin'] == {'space': 'phrase_qn', **q(0)}
    assert all(node['attribution'] == attribution() for node in record['nodes'])
    assert record['relations'] == data['relations']
    assert record['coverage']['motif_identity'] == 'attributed_not_inferred'
    assert answer['note_ids'] == ['answer']


def test_qa_empty_selection_does_not_mean_audio_silence_or_implicit_all_notes(tmp_path):
    data = definition()
    data['nodes'][0]['note_ids'] = []
    handle, store = create(tmp_path, data)
    record = read_record(handle, store)
    assert record['nodes'][0]['note_count'] == 0
    result = query(handle, store, section='members', node_ids=['motif'])
    assert result['rows'] == [] and result['total'] == 0 and result['next_cursor'] is None
    assert 'silence' not in json.dumps(result).lower()


@pytest.mark.parametrize('case', ['cross_clip', 'onset_at_end', 'unknown_clip', 'stale_revision', 'wrong_space',
                                  'duplicate_note', 'duplicate_node', 'duplicate_material', 'unknown_field',
                                  'reverse_span', 'span_past_clip', 'nonreduced', 'bool_rational'])
def test_qa_invalid_membership_scope_and_contract_never_publish_material_or_graph(tmp_path, case):
    data = definition()
    node = data['nodes'][0]
    if case == 'cross_clip': node['note_ids'] = ['other-clip']
    elif case == 'onset_at_end': node['note_ids'].append('answer')
    elif case == 'unknown_clip': node['clip_id'] = 'absent'
    elif case == 'stale_revision': node['material_revision'] = '0' * 64
    elif case == 'wrong_space': node['space'] = 'arrangement_qn'
    elif case == 'duplicate_note': node['note_ids'].append('sustain')
    elif case == 'duplicate_node': data['nodes'].append(copy.deepcopy(node))
    elif case == 'duplicate_material': data['materials'].append(copy.deepcopy(data['materials'][0]))
    elif case == 'unknown_field': node['quantize'] = True
    elif case == 'reverse_span': node['span_qn'] = {'start': q(2), 'end': q(1)}
    elif case == 'span_past_clip': node['span_qn']['end'] = q(5)
    elif case == 'nonreduced': node['span_qn']['start'] = {'n': -2, 'd': 2}
    else: node['span_qn']['start'] = {'n': True, 'd': 1}
    with pytest.raises(PocketError):
        create(tmp_path, data)
    assert not (tmp_path / 'store/artifacts').exists()


@pytest.mark.parametrize('case', ['cycle', 'self', 'duplicate_edge', 'unresolved', 'duplicate_claim'])
def test_qa_graph_relations_do_not_create_implicit_loops_or_unbound_links(tmp_path, case):
    data = definition()
    edge = copy.deepcopy(data['relations'][0])
    edge['relation_id'] = 'new-link'
    if case == 'cycle': edge.update(from_node='return', to_node='motif')
    elif case == 'self': edge['to_node'] = edge['from_node']
    elif case == 'unresolved': edge['to_node'] = 'missing-node'
    elif case == 'duplicate_claim': edge.update(kind='sequence', identity_claims=['rhythm', 'rhythm'])
    data['relations'].append(edge)
    with pytest.raises(PocketError):
        create(tmp_path, data)


def test_qa_declared_derivation_requires_exact_parent_revision_and_logical_material_identity(tmp_path):
    source = literal()
    child = copy.deepcopy(source)
    child['parent_revision'] = source['revision_sha256']
    child['notes'][1]['velocity']['value'] = 89
    child = finalize_material(child)
    data = definition(source)
    data['materials'].append({'key': 'child', 'material': child})
    data['nodes'][1].update(material_key='child', material_revision=child['revision_sha256'])
    data['relations'][0]['kind'] = 'derivation'
    handle, store = create(tmp_path, data)
    assert read_record(handle, store)['coverage']['derivation_links'] == 'declared_material_lineage_verified'
    bad = copy.deepcopy(data)
    bad_child = bad['materials'][1]['material']
    bad_child['parent_revision'] = '0' * 64
    bad_child = finalize_material(bad_child)
    bad['materials'][1]['material'] = bad_child
    bad['nodes'][1]['material_revision'] = bad_child['revision_sha256']
    with pytest.raises(PocketError, match='lineage'):
        create(tmp_path, bad, 'unrelated')


def test_qa_rich_opaque_material_is_referenced_without_promoting_fidelity_or_editing(tmp_path):
    source = literal()
    source['events'] = [{'id': 'pedal', 'time': {'space': 'clip_qn', **q(0)}, 'order': 0,
                         'message_type': 'control_change', 'is_meta': False, 'bytes': [176, 64, 127]}]
    source['clips'][0]['event_ids'] = ['pedal']
    source['coverage'] = {'editing_allowed': False, 'issues': [{'code': 'opaque_native_expression'}],
                          'expression': 'retained_opaque'}
    source = finalize_material(source)
    handle, store = create(tmp_path, definition(source))
    graph = read_record(handle, store)
    retained = read_record(graph['materials'][0]['material'], store)
    assert retained == source and retained['coverage']['editing_allowed'] is False
    assert graph['coverage']['expression'] == 'retained_in_material_not_interpreted'


@pytest.mark.parametrize('field', ['selection_sha256', 'note_count', 'source_clip_origin', 'gate_crossing_count', 'coverage', 'material_revision'])
def test_qa_stored_derived_fields_and_coverage_are_recomputed_not_trusted(tmp_path, field):
    handle, store = create(tmp_path)
    record = read_record(handle, store)
    if field == 'coverage': record['coverage']['human_listening'] = 'approved'
    elif field == 'source_clip_origin': record['nodes'][0][field] = {'space': 'arrangement_qn', **q(0)}
    elif field in ('note_count', 'gate_crossing_count'): record['nodes'][0][field] += 1
    else: record['nodes'][0][field] = '0' * 64
    altered = put_record(record, store)
    with pytest.raises(PocketError):
        query(altered, store)


@pytest.mark.parametrize('duplicate', [False, True])
def test_qa_query_rejects_forged_evidence_schema_even_after_valid_duplicate(tmp_path, duplicate):
    store = str(tmp_path / 'store')
    evidence = put_record({'schema': 'pocket.qa-evidence/v1', 'source': 'synthetic'}, store)
    data = definition()
    data['attribution']['evidence'] = [evidence]
    handle, _ = create(tmp_path, data)
    record = read_record(handle, store)
    forged = {**evidence, 'artifact_schema': 'pocket.invented-schema/v1'}
    record['attribution']['evidence'] = [evidence, forged] if duplicate else [forged]
    altered = put_record(record, store)
    with pytest.raises(PocketError, match='schema'):
        query(altered, store)


def test_qa_opaque_json_named_attribution_artifact_is_not_recursively_interpreted(tmp_path):
    store = str(tmp_path / 'store')
    evidence = put_bytes(b'opaque source bytes', store, 'notes.json', 'pocket.binary-asset/v1')
    data = definition()
    data['attribution']['evidence'] = [evidence]
    handle, _ = create(tmp_path, data)
    assert read_record(handle, store)['attribution']['evidence'] == [evidence]
    query(handle, store)


def test_qa_material_nested_dependency_tamper_invalidates_query_and_create_replay(tmp_path):
    store = str(tmp_path / 'store')
    raw = put_bytes(b'external original bytes', store, 'original.bin', 'pocket.binary-asset/v1')
    source = literal()
    source['sources'] = [{'kind': 'opaque_fixture', 'raw': raw}]
    source = finalize_material(source)
    data = definition(source)
    handle, _ = create(tmp_path, data)
    (Path(store) / raw['artifact_uri']).write_bytes(b'changed original evidence')
    with pytest.raises(PocketError, match='integrity'):
        query(handle, store)
    with pytest.raises(PocketError, match='integrity'):
        create(tmp_path, data)


def test_qa_bounded_forward_pages_bind_section_filter_and_structure_identity(tmp_path):
    handle, store = create(tmp_path)
    pages = []
    cursor = None
    while True:
        page = query(handle, store, section='nodes', limit=1, max_bytes=4096, cursor=cursor)
        assert len(canonical_bytes(page)) <= 4096 and page['returned'] == 1
        assert all('note_ids' not in row for row in page['rows'])
        pages.extend(row['node_id'] for row in page['rows'])
        following = page['next_cursor']
        assert following is None or following != cursor
        if following is None: break
        cursor = following
    assert pages == ['motif', 'answer', 'return']
    first = query(handle, store, section='nodes', limit=1)
    for changed in [{'section': 'relations'}, {'section': 'nodes', 'node_ids': ['motif']}]:
        with pytest.raises(PocketError, match='cursor'):
            query(handle, store, cursor=first['next_cursor'], **changed)
    other_data = definition()
    other_data['label'] = 'New immutable graph'
    other, _ = create(tmp_path, other_data, 'other')
    with pytest.raises(PocketError, match='cursor'):
        query(other, store, section='nodes', cursor=first['next_cursor'])


def test_qa_member_order_and_partial_selection_pages_do_not_claim_completeness(tmp_path):
    handle, store = create(tmp_path)
    first = query(handle, store, section='members', node_ids=['motif'], limit=1)
    second = query(handle, store, section='members', node_ids=['motif'], limit=1, cursor=first['next_cursor'])
    assert first['rows'] == [{'note_id': 'sustain', 'member_index': 0}]
    assert second['rows'] == [{'note_id': 'pickup', 'member_index': 1}]
    assert first['total'] == 2 and first['omitted'] == 1 and first['next_cursor'] is not None
    assert second['next_cursor'] is None and second['node'] == first['node']
    edges = query(handle, store, section='relations', node_ids=['answer'])
    assert len(edges['rows']) == 2 and edges['filter_scope'] == 'either_endpoint'


@pytest.mark.parametrize('kwargs', [{'limit': True}, {'limit': 257}, {'max_bytes': 65537}, {'max_bytes': True},
                                   {'section': 'members'}, {'section': 'members', 'node_ids': ['motif', 'answer']},
                                   {'section': 'nodes', 'node_ids': ['missing']}, {'section': 'nodes', 'node_ids': ['motif', 'motif']},
                                   {'section': 'nodes', 'cursor': 'not-a-cursor'}])
def test_qa_query_limits_and_filters_are_strict(tmp_path, kwargs):
    handle, store = create(tmp_path)
    with pytest.raises(PocketError):
        query(handle, store, **kwargs)


def test_qa_byte_budget_counts_unicode_and_metadata_and_never_returns_nonprogress_cursor(tmp_path):
    data = definition()
    data['nodes'][0]['label'] = '🎵' * 200
    data['nodes'][0]['attribution']['statement'] = 'Long attributed hypothesis ' * 70
    handle, store = create(tmp_path, data)
    with pytest.raises(PocketError, match='byte budget'):
        query(handle, store, section='nodes', max_bytes=1024)
    result = query(handle, store, section='nodes', max_bytes=65536)
    assert len(canonical_bytes(result)) <= 65536 and result['rows']


def test_qa_parent_structure_chain_revalidates_and_enforces_declared_bound(tmp_path):
    parent, store = create(tmp_path)
    for index in range(1, 32):
        data = definition()
        data['parent_structure'] = parent
        parent, _ = create(tmp_path, data, f'parent-{index}')
    query(parent, store)
    excessive = definition()
    excessive['parent_structure'] = parent
    with pytest.raises(PocketError, match='32'):
        create(tmp_path, excessive, 'too-deep')
    bad = definition()
    bad['parent_structure'] = put_record({'schema': 'pocket.unrelated/v1'}, store)
    with pytest.raises(PocketError):
        create(tmp_path, bad, 'wrong-parent')


def test_qa_structure_artifacts_relocate_and_parent_tamper_remains_visible(tmp_path):
    parent, store = create(tmp_path)
    data = definition()
    data['parent_structure'] = parent
    child, _ = create(tmp_path, data, 'child')
    moved = tmp_path / 'relocated-store'
    Path(store).rename(moved)
    query(child, str(moved))
    (moved / parent['artifact_uri']).write_bytes(b'changed ancestor graph')
    with pytest.raises(PocketError, match='integrity'):
        query(child, str(moved))
