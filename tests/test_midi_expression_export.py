"""Expression serialization boundaries and source/plan preservation."""
from __future__ import annotations

import copy
import json
from fractions import Fraction

import pytest
from test_midi_expression import configuration, expressive
from test_midi_qa import decode_wire, seal_literal, smf

from pocket_music.artifact_store import put_record, read_bytes, read_record
from pocket_music.errors import PocketError
from pocket_music.material import qn
from pocket_music.midi_expression import midi_expression_plan
from pocket_music.midi_io import import_smf, midi_export


def export(tmp_path, record=None, config=None, **kwargs):
    record = expressive() if record is None else record
    config = configuration(record['clips'][0]['id']) if config is None else config
    return midi_export(record, str(tmp_path / 'store'), str(tmp_path / 'out.mid'), 'export',
                       expression=config, **kwargs)


@pytest.mark.parametrize('kind', ['smf0', 'smf1'])
def test_inline_and_plan_handle_encode_identical_events_without_changing_source(tmp_path, kind):
    record, config = expressive(), configuration()
    original = copy.deepcopy(record)
    root = str(tmp_path / 'store')
    result = export(tmp_path, record, config, format=kind)
    plan = midi_expression_plan(record, store_root=root, request_id='plan', **config)
    composed = midi_export(record, root, str(tmp_path / 'composed.mid'), 'composed', format=kind,
                           expression=plan['plan'])
    assert result['midi'] == composed['midi'] and result['sidecar'] == composed['sidecar']
    proof = read_record(plan['plan'], root)
    _, _, tracks = decode_wire(read_bytes(result['midi'], root))
    channel = [(time, list(data)) for time, data in tracks[0] if data[0] != 255]
    assert channel == [(Fraction(row['time_qn']['n'], row['time_qn']['d']), row['bytes']) for row in proof['events']]
    assert record == original and read_record(proof['material'], root) == original
    assert result['coverage']['fidelity']['expression_encoding']['native_verified'] is False
    assert len(json.dumps(result, indent=2).encode()) < 16384
    assert export(tmp_path, record, config, format=kind) == result


def test_tail_reset_extends_eot_but_horizon_does_not_add_silence(tmp_path):
    record = expressive(two=False)
    record['clips'][0]['length_qn'] = qn(2)
    result = export(tmp_path, seal_literal(record))
    sidecar = read_record(result['sidecar'], tmp_path / 'store')
    proof = sidecar['expression_encoding']
    assert proof['tail_extension_qn'] == qn(1) and proof['encoded_end_qn'] == qn(3)
    _, _, tracks = decode_wire(read_bytes(result['midi'], tmp_path / 'store'))
    assert tracks[0][-1] == (Fraction(3), bytes([255, 47]))


@pytest.mark.parametrize('meta', [b'\xff\x20\x01\x00', b'\xff\x21\x01\x01', b'\xff\x7f\x01\x01'])
def test_imported_routing_or_opaque_metadata_refuses_before_output(tmp_path, meta):
    raw = smf([b'\x00' + meta + b'\x00\x90\x3c\x64\x60\x80\x3c\x25\x00\xff\x2f\x00'],
              ppq=96, format=0)
    record = import_smf(raw, tmp_path / 'store')
    with pytest.raises(PocketError, match='metadata'):
        export(tmp_path, record)
    assert not (tmp_path / 'out.mid').exists()


def test_imported_metadata_order_release_and_original_bytes_retained(tmp_path):
    raw = smf([(b'\x00\xff\x51\x03\x07\xa1\x20\x00\xff\x06\x01A\x00\xff\x06\x01B'
                b'\x00\x90\x3c\x64\x60\x80\x3c\x25\x60\xff\x2f\x00')], ppq=96, format=0)
    record = import_smf(raw, tmp_path / 'store')
    result = export(tmp_path, record)
    _, _, tracks = decode_wire(read_bytes(result['midi'], tmp_path / 'store'))
    assert [bytes(row[1]) for row in tracks[0][:3]] == [b'\xff\x51\x07\xa1\x20', b'\xff\x06A', b'\xff\x06B']
    assert (Fraction(1), b'\x81\x3c\x25') in tracks[0]
    assert read_bytes(record['sources'][0]['raw'], tmp_path / 'store') == raw


def test_self_consistent_forged_plan_refuses(tmp_path):
    root = str(tmp_path / 'store')
    result = midi_expression_plan(expressive(), store_root=root, request_id='plan', **configuration())
    proof = read_record(result['plan'], root)
    proof['events'][0]['bytes'][2] = 127
    forged = put_record(proof, root)
    with pytest.raises(PocketError, match='recomputed'):
        export(tmp_path, config=forged)
    assert not (tmp_path / 'out.mid').exists()


def test_expression_does_not_silently_relax_ppq_or_allow_cc_combination(tmp_path):
    record = expressive(two=False)
    record['curves'][0]['points'][1]['time'] = qn(Fraction(1, 7))
    with pytest.raises(PocketError, match='PPQ'):
        export(tmp_path, seal_literal(record), ppq=96)
    with pytest.raises(PocketError, match='combined'):
        export(tmp_path, cc_step_bindings=[])
    assert not (tmp_path / 'out.mid').exists()


def test_existing_output_never_overwritten_and_replay_detects_changed_bytes(tmp_path):
    result = export(tmp_path)
    (tmp_path / 'out.mid').write_bytes(b'changed')
    with pytest.raises(PocketError, match='changed'):
        export(tmp_path)
    with pytest.raises(PocketError, match='overwrites'):
        midi_export(expressive(), str(tmp_path / 'store'), str(tmp_path / 'out.mid'), 'new',
                    expression=configuration())
    assert read_bytes(result['midi'], tmp_path / 'store').startswith(b'MThd')
