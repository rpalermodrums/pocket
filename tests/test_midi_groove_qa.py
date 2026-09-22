"""Independent hand-calculated periodic timing and public interface checks."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
from fractions import Fraction

import pytest
from test_midi_construction_qa import apply
from test_midi_interfaces import cli_call, environment
from test_midi_qa import expressed_material, seal_literal
from test_midi_relationships_qa import material, note, q

from pocket_music.artifact_store import read_record
from pocket_music.errors import PocketError
from pocket_music.material import material_query
from pocket_music.midi_edit import midi_transform


def groove(**changes):
    return {'op': 'groove', 'cycle_qn': 2,
            'anchors': [{'nominal_qn': 0, 'offset_qn': q(Fraction(-1, 8))},
                        {'nominal_qn': 1, 'offset_qn': q(Fraction(1, 8))}],
            'phase_qn': 0, 'strength': 1, 'threshold_qn': 1, 'ties': 'earlier',
            'note_off': 'follow_onset', 'controller_timeline': 'preserve_existing',
            'time_space': 'clip_qn', **changes}


@pytest.mark.parametrize('onset,options,wanted', [
    (Fraction(1, 3), {'strength': q(Fraction(1, 2))}, Fraction(13, 48)),
    (Fraction(1, 2), {}, Fraction(3, 8)),
    (Fraction(1, 2), {'ties': 'later'}, Fraction(5, 8)),
    (Fraction(-1, 2), {}, Fraction(-3, 8)),
    (Fraction(-1, 2), {'ties': 'later'}, Fraction(-5, 8)),
    (Fraction(1, 4), {'phase_qn': q(Fraction(-1, 4))}, Fraction(1, 8)),
    (Fraction(1, 4), {'phase_qn': q(Fraction(-1, 4)), 'ties': 'later'}, Fraction(3, 8)),
    (Fraction(1, 3), {'threshold_qn': q(Fraction(1, 3))}, Fraction(5, 24)),
    (Fraction(1, 3), {'threshold_qn': q(Fraction(1, 4))}, Fraction(1, 3)),
    (Fraction(15, 8), {}, Fraction(7, 4)),
])
def test_qa_groove_hand_expected_residual_ties_phase_threshold_and_cycle(tmp_path, onset, options, wanted):
    record = material([note('n', onset, Fraction(1, 16))])
    result, child = apply(record, tmp_path, [groove(**options)])
    expected = copy.deepcopy(record['notes'][0])
    expected['onset'] = {'space': 'clip_qn', **q(wanted)}
    assert child['notes'] == [expected]
    assert child['clips'] == record['clips']
    proof = read_record(result['edit'], str(tmp_path / 'store'))
    report = proof['operation_reports'][0]
    assert report['off_grid_deviation'] == 'preserved' and report['gate'] == 'unchanged'
    assert report['native'] == 'not_performed'


def test_qa_groove_single_anchor_wrap_has_explicit_negative_occurrence(tmp_path):
    record = material([note('n', 0, Fraction(1, 16))])
    operation = groove(anchors=[{'nominal_qn': 1, 'offset_qn': q(Fraction(1, 4))}])
    result, child = apply(record, tmp_path, [operation])
    proof = read_record(result['edit'], str(tmp_path / 'store'))
    assert proof['operation_reports'][0]['applied'][0]['cycle_index'] == -1
    assert child['notes'][0]['onset'] == {'space': 'clip_qn', **q(Fraction(1, 4))}


@pytest.mark.parametrize('changes', [
    {'cycle_qn': 0}, {'cycle_qn': True}, {'strength': 1.0}, {'strength': 2}, {'threshold_qn': 2},
    {'phase_qn': {'space': 'seconds', 'n': 0, 'd': 1}}, {'anchors': []},
    {'anchors': [{'nominal_qn': 1, 'offset_qn': 0}, {'nominal_qn': 0, 'offset_qn': 0}]},
    {'anchors': [{'nominal_qn': 0, 'offset_qn': 1}, {'nominal_qn': 1, 'offset_qn': 0}]},
    {'anchors': [{'nominal_qn': 0, 'offset_qn': -1}, {'nominal_qn': 1, 'offset_qn': 0}]},
    {'anchors': [{'nominal_qn': 0, 'offset_qn': 0, 'extra': 1}]},
    {'anchors': [{'nominal_qn': 0, 'offset_qn': {'n': 2, 'd': 4}}]},
    {'time_space': 'arrangement_qn'}, {'note_off': 'keep_absolute'}, {'ties': 'random'},
    {'controller_timeline': 'move'}, {'extra': True},
])
def test_qa_groove_invalid_template_refuses_even_empty_selection(tmp_path, changes):
    with pytest.raises(PocketError):
        apply(material([note('n', 0, 1)]), tmp_path, [groove(**changes)], ids=[])


def test_qa_groove_expression_consent_and_exact_curve_ownership(tmp_path):
    record = expressed_material()
    with pytest.raises(PocketError):
        apply(record, tmp_path, [groove()], ids=['note:0'], request='no-consent')
    _, child = apply(record, tmp_path, [groove()], ids=['note:0'], request='allowed', expression_policy='preserve_relative')
    assert child['curves'] == record['curves']
    assert child['notes'][1:] == record['notes'][1:]
    assert child['notes'][0]['expression_refs'] == record['notes'][0]['expression_refs']
    assert child['notes'][0]['onset'] == {'space': 'clip_qn', **q(Fraction(-1, 8))}
    for index, (field, value) in enumerate([('ownership', 'automation'), ('resize_policy', 'preserve_ms')]):
        bad = copy.deepcopy(record)
        bad['curves'][0]['target'][field] = value
        with pytest.raises(PocketError):
            apply(seal_literal(bad), tmp_path, [groove()], ids=['note:0'], request=f'ownership-{index}',
                  expression_policy='preserve_relative')


def test_qa_groove_zero_strength_lock_new_overlap_and_full_evidence(tmp_path):
    record = material([note('a', Fraction(1, 2), Fraction(1, 2)), note('b', 1, Fraction(1, 4))])
    _, exact = apply(record, tmp_path, [groove(strength=0)], ids=['a'], locks={'selected_fields': ['onset']})
    assert exact['notes'] == record['notes']
    with pytest.raises(PocketError, match='lock'):
        apply(record, tmp_path, [groove()], ids=['a'], request='locked', locks={'selected_fields': ['onset']})
    with pytest.raises(PocketError, match='overlap'):
        apply(record, tmp_path, [groove(ties='later')], ids=['a'], request='overlap')


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional MCP extra not installed')
def test_qa_groove_actual_cli_stdio_and_schema(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    record = material([note('n', Fraction(1, 3), Fraction(1, 16))])
    store = str(tmp_path / 'store')
    args = {'material': record, 'selection': material_query(record, store)['selection'],
            'operations': [groove(strength=q(Fraction(1, 2)))], 'store_root': store, 'request_id': 'groove'}
    direct = midi_transform(**args)
    assert cli_call(tmp_path, 'midi_transform', args) == direct

    async def exchange():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'], env=environment())
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            definitions = next(t for t in (await session.list_tools()).tools if t.name == 'midi_transform').inputSchema['$defs']
            variant = next(d for d in definitions.values() if d.get('properties', {}).get('op', {}).get('const') == 'groove')
            assert set(variant['required']) == set(groove()) and variant['additionalProperties'] is False
            response = await session.call_tool('midi_transform', args)
            assert not response.isError and json.loads(response.content[0].text) == direct
            invalid = {**args, 'request_id': 'invalid', 'operations': [groove(strength=True)]}
            assert (await session.call_tool('midi_transform', invalid)).isError
            assert cli_call(tmp_path, 'midi_transform', invalid, success=False)['error'] == 'PocketError'

    asyncio.run(exchange())
