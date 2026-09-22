"""Supplied timing templates preserve residuals, clocks, locks and source evidence."""
from __future__ import annotations

import copy
from fractions import Fraction

import pytest
from test_midi_edit_extensions import apply, one_note
from test_midi_qa import import_wire

from pocket_music.artifact_store import read_record
from pocket_music.errors import PocketError
from pocket_music.material import qn, rational
from pocket_music.midi_groove import apply_groove


def groove(**changes):
    return {'op': 'groove', 'cycle_qn': 1,
            'anchors': [{'nominal_qn': 0, 'offset_qn': 0},
                        {'nominal_qn': qn(Fraction(1, 2)), 'offset_qn': qn(Fraction(1, 6))}],
            'phase_qn': 0, 'strength': qn(Fraction(1, 2)),
            'threshold_qn': qn(Fraction(1, 8)), 'ties': 'earlier',
            'note_off': 'follow_onset', 'controller_timeline': 'preserve_existing',
            'time_space': 'clip_qn', **changes}


@pytest.mark.parametrize(('start', 'expected'), [
    (Fraction(1, 2), Fraction(7, 12)),
    (Fraction(9, 16), Fraction(31, 48)),
    (Fraction(-7, 16), Fraction(-17, 48)),
    (Fraction(1, 4), Fraction(1, 4)),
    (Fraction(5, 8), Fraction(17, 24)),
])
def test_exact_template_offset_preserves_residual_and_threshold(start, expected):
    record = one_note(start)
    original = copy.deepcopy(record['notes'][0])
    report = apply_groove(record['notes'], groove())
    assert rational(record['notes'][0]['onset']) == expected
    assert {k: v for k, v in record['notes'][0].items() if k != 'onset'} == {
        k: v for k, v in original.items() if k != 'onset'}
    assert report['off_grid_deviation'] == 'preserved'


def test_periodic_nearest_ties_have_signed_earlier_and_later_meaning():
    earlier, later = one_note(Fraction(-1, 4)), one_note(Fraction(-1, 4))
    apply_groove(earlier['notes'], groove(threshold_qn=qn(Fraction(1, 4))))
    apply_groove(later['notes'], groove(threshold_qn=qn(Fraction(1, 4)), ties='later'))
    assert rational(earlier['notes'][0]['onset']) == Fraction(-1, 6)
    assert rational(later['notes'][0]['onset']) == Fraction(-1, 4)


@pytest.mark.parametrize('changes', [
    {'cycle_qn': True}, {'cycle_qn': 0}, {'phase_qn': .5},
    {'strength': {'n': 1, 'd': 2, 'space': 'seconds'}},
    {'strength': 2}, {'threshold_qn': -1}, {'threshold_qn': 1},
    {'time_space': 'seconds'}, {'note_off': 'fixed'}, {'ties': 'nearest'},
    {'controller_timeline': 'move'}, {'anchors': []},
    {'anchors': [{'nominal_qn': 0, 'offset_qn': 0, 'velocity': 99}]},
    {'anchors': [{'nominal_qn': 0, 'offset_qn': 1}]},
    {'anchors': [{'nominal_qn': 0, 'offset_qn': 0}, {'nominal_qn': 0, 'offset_qn': 0}]},
    {'anchors': [{'nominal_qn': 0, 'offset_qn': qn(Fraction(1, 2))},
                 {'nominal_qn': qn(Fraction(1, 2)), 'offset_qn': 0}]},
    {'anchors': [{'nominal_qn': 0, 'offset_qn': qn(Fraction(-1, 2))},
                 {'nominal_qn': qn(Fraction(1, 2)), 'offset_qn': 0}]},
])
def test_malformed_or_collapsed_templates_refuse_even_without_notes(changes):
    with pytest.raises(PocketError):
        apply_groove([], groove(**changes))


def test_template_identity_normalizes_rational_shorthand():
    first, second = one_note(), one_note()
    one = apply_groove(first['notes'], groove())
    two = apply_groove(second['notes'], groove(cycle_qn=qn(1), anchors=[
        {'nominal_qn': qn(0), 'offset_qn': qn(0)},
        {'nominal_qn': qn(Fraction(1, 2)), 'offset_qn': qn(Fraction(1, 6))}]))
    assert one['template_sha256'] == two['template_sha256']


def test_public_groove_keeps_unselected_fields_and_full_proof(tmp_path):
    record = one_note(Fraction(9, 16))
    result, child = apply(record, tmp_path, [groove()], locks={'selected_fields': ['pitch', 'duration', 'velocity']})
    assert rational(child['notes'][0]['onset']) == Fraction(31, 48)
    assert child['clips'] == record['clips']
    assert child['events'] == record['events']
    proof = read_record(result['edit'], tmp_path / 'store')['operation_reports'][0]
    assert proof['applied'][0]['delta_qn'] == {'n': 1, 'd': 12}
    with pytest.raises(PocketError, match='lock'):
        apply(record, tmp_path, [groove()], request='locked', locks={'selected_fields': ['onset']})
    _, unchanged = apply(record, tmp_path, [groove(strength=0)], request='zero', locks={'selected_fields': ['onset']})
    assert unchanged['notes'] == record['notes']


def test_public_imported_source_controllers_and_raw_stay_exact(tmp_path):
    handle = import_wire(tmp_path)
    parent = read_record(handle, tmp_path / 'store')
    _, child = apply(handle, tmp_path, [groove(phase_qn=qn(Fraction(1, 2)))])
    assert child['events'] == parent['events']
    assert child['sources'] == parent['sources']
    assert child['curves'] == parent['curves']
    assert [n['source_binding'] for n in child['notes']] == [n['source_binding'] for n in parent['notes']]
