"""Independent literal/structural material expectations, not native acceptance."""
from __future__ import annotations

import builtins
import copy
import json
from fractions import Fraction

import pytest
from test_midi_qa import decode_wire, expressed_material, import_wire, literal_material, seal_literal, smf
from test_midi_relationships_qa import material, note, q

from pocket_music.artifact_store import read_bytes, read_record
from pocket_music.errors import PocketError
from pocket_music.material import material_query
from pocket_music.midi_edit import midi_transform
from pocket_music.midi_io import midi_export


def literal(**changes):
    return {'onset_qn': q(Fraction(13, 6)), 'duration_qn': q(Fraction(5, 12)),
            'pitch': {'midi_note': 71, 'cents_offset': 0, 'tuning_ref': 'tuning:12tet-a440'},
            'velocity': {'value': 73, 'domain': 'midi1_7bit'},
            'release_velocity': {'value': 37, 'domain': 'midi1_7bit'}, 'channel': 2,
            'mute': False, 'voice_id': 'voice:literal', 'role_ref': 'role:answer', **changes}


def add(record, rows=None, **changes):
    return {'op': 'add', 'clip_id': record['clips'][0]['id'],
            'notes': [literal()] if rows is None else rows,
            'controller_timeline': 'preserve_existing', 'time_space': 'clip_qn', **changes}


def split(offsets=None, **changes):
    return {'op': 'split', 'offsets_qn': [q(Fraction(1, 6)), q(Fraction(1, 2))] if offsets is None else offsets,
            'controller_timeline': 'preserve_existing', 'time_space': 'note_relative_qn',
            'articulation': 'retrigger', **changes}


def merge(**changes):
    return {'op': 'merge', 'controller_timeline': 'preserve_existing',
            'time_space': 'clip_qn', 'articulation': 'remove_retriggers', **changes}


def apply(record, tmp_path, operations, *, ids=None, request='edit', **options):
    store = str(tmp_path / 'store')
    selection = material_query(record, store, selection=None if ids is None else {'note_ids': ids})['selection']
    result = midi_transform(record, selection, operations, store, request, **options)
    return result, read_record(result['material'], store)


def contiguous():
    return material([note('a', Fraction(1, 3), Fraction(1, 6)),
                     note('b', Fraction(1, 2), Fraction(1, 3)),
                     note('c', Fraction(5, 6), Fraction(1, 3)), note('outside', 3, 1, 73)])


def test_qa_add_literal_empty_selection_exact_and_no_hidden_generation(tmp_path, monkeypatch):
    old_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == 'mido' or name.startswith(('mido.', 'pocket_music.midi_generate',
                                             'pocket_music.native', 'pocket_music.instruments')):
            raise AssertionError('Pure material construction imported unrelated provider')
        return old_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', guarded)
    record = literal_material()
    before = copy.deepcopy(record)
    result, child = apply(record, tmp_path, [add(record)], ids=[])
    assert child['notes'][:3] == before['notes']
    created = child['notes'][3]
    wanted = literal()
    assert created == {'id': created['id'], 'onset': {'space': 'clip_qn', **wanted.pop('onset_qn')},
                       **wanted, 'derived_from': [], 'expression_refs': [], 'source_binding': None}
    assert created['id'] not in {n['id'] for n in record['notes']}
    for key in ('events', 'curves', 'sources', 'tempo_map_ref', 'meter_map_ref', 'tracks'):
        assert child[key] == before[key]
    assert child['clips'][0] == {**before['clips'][0], 'note_ids': [*before['clips'][0]['note_ids'], created['id']]}
    assert record == before
    assert result['coverage']['native'] == result['coverage']['listening'] == 'not_performed'
    proof = read_record(result['edit'], str(tmp_path / 'store'))
    assert read_record(proof['inverse']['restore_parent'], str(tmp_path / 'store')) == record


def test_qa_add_identity_canonical_time_and_frozen_selection(tmp_path):
    record = literal_material()
    ops = [add(record, [literal(onset_qn=2, duration_qn=1)]), {'op': 'velocity', 'value': 99}]
    first, child = apply(record, tmp_path, ops, ids=['note:0'])
    assert child['notes'][0]['velocity']['value'] == 99
    assert child['notes'][-1]['velocity']['value'] == 73
    second, other = apply(record, tmp_path, ops, ids=['note:0'], request='other')
    assert child == other and first['material'] == second['material']
    spelled = copy.deepcopy(ops)
    spelled[0]['notes'][0].update(onset_qn=q(2), duration_qn=q(1))
    _, canonical = apply(record, tmp_path, spelled, ids=['note:0'], request='canonical')
    assert canonical['notes'][-1]['id'] == child['notes'][-1]['id']


def test_qa_split_exact_rational_gates_and_fixed_selection(tmp_path):
    record = material([note('source', Fraction(1, 3), Fraction(5, 6)), note('outside', 2, 1, 72)])
    _, child = apply(record, tmp_path, [split(), {'op': 'velocity', 'value': 99}], ids=['source'])
    pieces = sorted((n for n in child['notes'] if n['id'] != 'outside'), key=lambda n: Fraction(n['onset']['n'], n['onset']['d']))
    assert [(n['onset'], n['duration_qn']) for n in pieces] == [
        ({'space': 'clip_qn', **q(Fraction(1, 3))}, q(Fraction(1, 6))),
        ({'space': 'clip_qn', **q(Fraction(1, 2))}, q(Fraction(1, 3))),
        ({'space': 'clip_qn', **q(Fraction(5, 6))}, q(Fraction(1, 3)))]
    assert all(n['derived_from'] == ['source'] and n['source_binding'] is None for n in pieces)
    assert all(n['velocity']['value'] == 80 and n['release_velocity']['value'] == 37 for n in pieces)
    assert len({n['id'] for n in pieces}) == 3 and 'source' not in {n['id'] for n in child['notes']}
    assert next(n for n in child['notes'] if n['id'] == 'outside') == record['notes'][1]


def test_qa_merge_exact_sorted_lineage_and_preservation(tmp_path):
    record = contiguous()
    record['notes'] = [record['notes'][2], record['notes'][0], record['notes'][3], record['notes'][1]]
    record = seal_literal(record)
    result, child = apply(record, tmp_path, [merge()], ids=['c', 'a', 'b'])
    joined = next(n for n in child['notes'] if n['id'] != 'outside')
    assert joined['onset'] == {'space': 'clip_qn', **q(Fraction(1, 3))}
    assert joined['duration_qn'] == q(Fraction(5, 6))
    assert joined['derived_from'] == ['a', 'b', 'c']
    assert joined['id'] not in {'a', 'b', 'c'} and joined['source_binding'] is None
    assert joined['velocity'] == {'value': 80, 'domain': 'midi1_7bit'}
    assert joined['release_velocity'] == {'value': 37, 'domain': 'midi1_7bit'}
    assert next(n for n in child['notes'] if n['id'] == 'outside') == next(n for n in record['notes'] if n['id'] == 'outside')
    proof = read_record(result['edit'], str(tmp_path / 'store'))
    assert proof['semantic_diff']['deleted'] == ['a', 'b', 'c']
    assert proof['semantic_diff']['inserted'] == [joined['id']]


@pytest.mark.parametrize('field,value', [
    ('onset_qn', True), ('onset_qn', 1.0), ('onset_qn', {'n': 2, 'd': 2}),
    ('onset_qn', {'space': 'seconds', 'n': 1, 'd': 1}), ('onset_qn', -1),
    ('duration_qn', 0), ('duration_qn', 9), ('duration_qn', {'n': 1, 'd': -2}),
    ('pitch', {'midi_note': True, 'cents_offset': 0, 'tuning_ref': 'tuning:12tet-a440'}),
    ('pitch', {'midi_note': 128, 'cents_offset': 0, 'tuning_ref': 'tuning:12tet-a440'}),
    ('pitch', {'midi_note': 60, 'cents_offset': 0, 'tuning_ref': 'tuning:12tet-a440', 'extra': 1}),
    ('velocity', {'value': 0, 'domain': 'midi1_7bit'}),
    ('release_velocity', {'value': 0.0, 'domain': 'midi1_7bit'}),
    ('channel', True), ('channel', 0), ('channel', 17), ('mute', 0), ('voice_id', ''), ('role_ref', 12),
    ('source_binding', None), ('id', 'caller-id'), ('expression_refs', []),
])
def test_qa_add_malformed_literal_refuses_without_publishing(tmp_path, field, value):
    record = literal_material()
    with pytest.raises(PocketError):
        apply(record, tmp_path, [add(record, [literal(**{field: value})])], ids=[])
    assert not list((tmp_path / 'store' / 'artifacts').glob('*'))


@pytest.mark.parametrize('offsets', [[], [0], [-1], [1], [True], [0.25],
                                    [q(Fraction(1, 2)), q(Fraction(1, 2))],
                                    [q(Fraction(1, 2)), q(Fraction(1, 4))],
                                    [{'n': 2, 'd': 4}], [{'space': 'clip_qn', 'n': 1, 'd': 4}]])
def test_qa_split_invalid_offsets(tmp_path, offsets):
    with pytest.raises(PocketError):
        apply(material([note('n', 0, 1)]), tmp_path, [split(offsets)])


@pytest.mark.parametrize('field,value', [('velocity', {'value': 81, 'domain': 'midi1_7bit'}),
    ('release_velocity', {'value': 38, 'domain': 'midi1_7bit'}), ('channel', 2), ('voice_id', 'other'),
    ('role_ref', 'other'), ('mute', True),
    ('pitch', {'midi_note': 60, 'cents_offset': 1, 'tuning_ref': 'tuning:12tet-a440'}),
    ('onset', {'space': 'clip_qn', **q(Fraction(2, 3))})])
def test_qa_merge_heterogeneous_or_gapped_refuses(tmp_path, field, value):
    record = contiguous()
    record['notes'][1][field] = value
    with pytest.raises(PocketError):
        apply(seal_literal(record), tmp_path, [merge()], ids=['a', 'b', 'c'])


@pytest.mark.parametrize('field', ['onset', 'duration', 'pitch', 'velocity', 'release_velocity',
    'channel', 'mute', 'expression_shape', 'voice_id', 'role_ref', 'source_binding', 'derived_from'])
@pytest.mark.parametrize('operation', ['split', 'merge'])
def test_qa_replacement_refuses_every_selected_lock(tmp_path, field, operation):
    record = contiguous()
    with pytest.raises(PocketError, match='lock'):
        apply(record, tmp_path, [split([q(Fraction(1, 12))]) if operation == 'split' else merge()],
              ids=['a', 'b', 'c'], locks={'selected_fields': [field]})


def test_qa_expression_opaque_sources_and_empty_replacement_refuse(tmp_path):
    expressive = expressed_material()
    with pytest.raises(PocketError):
        apply(expressive, tmp_path, [split([q(Fraction(1, 8))])], ids=['note:0'], request='expression')
    record = contiguous()
    record['notes'][0]['source_binding'] = {'native_id': 1, 'kind': 'live'}
    with pytest.raises(PocketError):
        apply(seal_literal(record), tmp_path, [merge()], ids=['a', 'b', 'c'], request='opaque')
    for index, operation in enumerate([split(), merge()]):
        with pytest.raises(PocketError):
            apply(literal_material(), tmp_path, [operation], ids=[], request=f'empty-{index}')
    with pytest.raises(PocketError):
        apply(contiguous(), tmp_path, [{'op': 'delete', 'controller_timeline': 'preserve_existing'}, split()],
              ids=['a', 'b', 'c'], request='deleted')


def test_qa_add_overlap_refuses_touching_allowed_and_inherited_split_overlap_refuses(tmp_path):
    record = material([note('n', 0, 1)])
    bad = literal(onset_qn=q(Fraction(1, 2)), duration_qn=1, channel=1,
                  pitch=record['notes'][0]['pitch'])
    with pytest.raises(PocketError, match='overlap'):
        apply(record, tmp_path, [add(record, [bad])], ids=[], request='overlap')
    _, child = apply(record, tmp_path, [add(record, [{**bad, 'onset_qn': 1}])], ids=[], request='touch')
    assert len(child['notes']) == 2
    overlapping = material([note('a', 0, 1), note('b', Fraction(1, 4), 1)])
    with pytest.raises(PocketError, match='overlap'):
        apply(overlapping, tmp_path, [split([q(Fraction(1, 2))])], ids=['a'], request='inherited')


def test_qa_late_failure_retry_conflict_tamper_and_publication_crash(tmp_path, monkeypatch):
    import pocket_music.midi_edit as editing

    record = literal_material()
    with pytest.raises(PocketError):
        apply(record, tmp_path, [add(record), {'op': 'velocity', 'value': 0}], request='failed')
    assert not list((tmp_path / 'store' / 'artifacts').glob('*'))
    with pytest.raises(PocketError, match='did not complete'):
        apply(record, tmp_path, [add(record), {'op': 'velocity', 'value': 0}], request='failed')
    first, _ = apply(record, tmp_path, [add(record)], ids=[], request='ok')
    second, _ = apply(record, tmp_path, [add(record)], ids=[], request='ok')
    assert first == second
    with pytest.raises(PocketError, match='idempotency_conflict'):
        apply(record, tmp_path, [add(record, [literal(channel=3)])], ids=[], request='ok')
    (tmp_path / 'store' / first['material']['artifact_uri']).write_bytes(b'tampered')
    with pytest.raises(PocketError, match='integrity'):
        apply(record, tmp_path, [add(record)], ids=[], request='ok')
    real = editing.put_record
    calls = []

    def fail(record, store):
        calls.append(record['schema'])
        if len(calls) == 2:
            raise OSError('independent injected publication failure')
        return real(record, store)

    monkeypatch.setattr(editing, 'put_record', fail)
    with pytest.raises(OSError, match='publication failure'):
        apply(record, tmp_path, [add(record)], ids=[], request='crash')
    journal = json.loads((tmp_path / 'store' / 'requests' / 'crash' / 'journal.json').read_text())
    assert journal['state'] == 'failed' and 'receipt' not in journal
    with pytest.raises(PocketError, match='did not complete'):
        apply(record, tmp_path, [add(record)], ids=[], request='crash')


def wire_fixture():
    return smf([(b'\x00\xff\x01\x01X\x00\xb0\x01\x0b\x00\xb0\x01\x63\x00\xb0\x40\x7f'
                b'\x00\x90\x3c\x50\x78\x80\x3c\x25\x00\x90\x3c\x50'
                b'\x78\x90\x3c\x00\x78\xb0\x40\x00\x78\xff\x2f\x00')], 480, format=0)


def test_qa_split_wire_retriggers_source_bytes_stamps_and_controller_order(tmp_path):
    handle = import_wire(tmp_path, wire_fixture())
    store = str(tmp_path / 'store')
    record = read_record(handle, store)
    source = tmp_path / 'wire.mid'
    stat = source.stat()
    before = (source.read_bytes(), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
    result, child = apply(record, tmp_path, [split([q(Fraction(1, 8))])], ids=[record['notes'][0]['id']])
    assert child['events'] == record['events'] and child['sources'] == record['sources']
    assert set(child['coverage']['source_only_note_event_ids']) == {
        record['notes'][0]['source_binding']['on_event_id'], record['notes'][0]['source_binding']['off_event_id']}
    output = midi_export(result['material'], store, str(tmp_path / 'out.mid'), 'export', ppq=480)
    rows = decode_wire(read_bytes(output['midi'], store))[2][0]
    assert [(time, data) for time, data in rows if data[0] == 0xB0] == [
        (0, b'\xb0\x01\x0b'), (0, b'\xb0\x01\x63'), (0, b'\xb0\x40\x7f'),
        (Fraction(3, 4), b'\xb0\x40\x00')]
    assert [(time, data[2]) for time, data in rows if data[0] == 0x90 and data[2]] == [
        (0, 80), (Fraction(1, 8), 80), (Fraction(1, 4), 80)]
    assert [(time, data[2]) for time, data in rows if data[0] == 0x80] == [
        (Fraction(1, 8), 37), (Fraction(1, 4), 37)]
    assert (Fraction(1, 2), b'\x90\x3c\x00') in rows
    assert rows[-1] == (1, b'\xff\x2f')
    stat = source.stat()
    assert (source.read_bytes(), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size) == before
    assert read_record(handle, store) == record


def test_qa_merge_wire_removes_only_retriggers_and_retains_sources(tmp_path):
    payload = wire_fixture().replace(b'\x78\x90\x3c\x00', b'\x78\x80\x3c\x25')
    handle = import_wire(tmp_path, payload)
    store = str(tmp_path / 'store')
    record = read_record(handle, store)
    result, child = apply(record, tmp_path, [merge()])
    assert len(child['notes']) == 1 and child['notes'][0]['duration_qn'] == q(Fraction(1, 2))
    assert len(child['coverage']['source_only_note_event_ids']) == 4
    assert child['events'] == record['events']
    output = midi_export(result['material'], store, str(tmp_path / 'merged.mid'), 'export', ppq=480)
    rows = decode_wire(read_bytes(output['midi'], store))[2][0]
    assert [(time, data) for time, data in rows if data[0] >> 4 in (8, 9)] == [
        (0, b'\x90\x3c\x50'), (Fraction(1, 2), b'\x80\x3c\x25')]


def test_qa_long_lineage_receipt_bounded_full_proof_preserved(tmp_path):
    keys = ['note:' + '𝄞' * 1500 + str(i) for i in range(3)]
    record = material([note(key, i, 1) for i, key in enumerate(keys)])
    store = str(tmp_path / 'store')
    selection = material_query(record, store, max_bytes=65536)['selection']
    result = midi_transform(record, selection, [merge()], store, 'large')
    child = read_record(result['material'], store)
    assert len((json.dumps(result, ensure_ascii=True, indent=2) + '\n').encode()) <= 16384
    assert child['notes'][0]['derived_from'] == keys
    proof = read_record(result['edit'], str(tmp_path / 'store'))
    assert proof['semantic_diff']['deleted'] == sorted(keys)


def test_qa_declared_bounds_and_forged_selection(tmp_path, monkeypatch):
    import pocket_music.midi_edit as editing

    record = literal_material()
    for index, operation in enumerate([add(record, []), add(record, [literal()] * 4097),
        split([q(Fraction(i, 100)) for i in range(1, 66)]), add(record, clip_id='missing')]):
        with pytest.raises(PocketError):
            apply(record, tmp_path, [operation], request=f'bound-{index}')
    monkeypatch.setattr(editing, 'MAX_NOTES', 3)
    with pytest.raises(PocketError):
        apply(record, tmp_path, [add(record)], ids=[], request='note-limit')
    store = str(tmp_path / 'store')
    selection = material_query(record, store)['selection']
    with pytest.raises(PocketError, match='Stale'):
        midi_transform(record, {**selection, 'selection_sha256': '0' * 64}, [add(record)], store, 'stale')
