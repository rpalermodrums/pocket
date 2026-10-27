# SPDX-License-Identifier: AGPL-3.0-only
"""Actual CLI/MCP composition for explicit file-only canonical curve export."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from test_midi_cc_export_qa import cc_curve, cc_rows, expected_cc
from test_midi_interfaces import cli_call, environment
from test_midi_qa import decode_wire, literal_material, seal_literal

from pocket_music.artifact_store import read_bytes, read_record
from pocket_music.curves import curve_transform
from pocket_music.material import material_import
from pocket_music.midi_io import midi_export


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional MCP extra not installed')
def test_qa_public_curve_to_export_direct_cli_mcp_composition_uses_external_material(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    store = str(tmp_path / 'store')
    literal = literal_material()
    source = copy.deepcopy(literal)
    curve = cc_curve()
    curve_args = {'store_root': store, 'request_id': 'curve-public', 'target': curve['target'],
                  'operations': [{'op': 'create', 'space': 'clip_qn', 'interpolation': 'step',
                                  'points': curve['points']}]}
    imported_args = {'store_root': store, 'request_id': 'external-material',
                     'source': {'kind': 'material', 'material': literal}}

    async def exchange():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'],
                                       env=environment())
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listed = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert {'curve_transform', 'material_import', 'midi_export'} <= listed.keys()
            schema = listed['midi_export'].inputSchema
            assert 'cc_step_bindings' in schema['properties']
            bindings = [value for value in schema.get('$defs', {}).values()
                        if isinstance(value, dict) and {'controller', 'same_tick_order'} <= value.get('properties', {}).keys()]
            assert len(bindings) == 2
            assert all(value.get('additionalProperties') is False for value in bindings)
            assert {frozenset(value['required']) for value in bindings} == {
                frozenset({'curve_id', 'controller', 'same_tick_order'}),
                frozenset({'curve', 'clip_id', 'controller', 'same_tick_order'})}
            assert all(value['properties']['controller']['enum'] == [1, 11] for value in bindings)

            async def call(name, arguments):
                response = await session.call_tool(name, arguments)
                assert not response.isError, response.content
                return json.loads(response.content[0].text)

            created = await call('curve_transform', curve_args)
            assert created == curve_transform(**curve_args) == cli_call(tmp_path, 'curve_transform', curve_args)
            imported = cli_call(tmp_path, 'material_import', imported_args)
            assert imported == await call('material_import', imported_args) == material_import(**imported_args)
            curve_handle = created['artifacts']['curve']
            curve_before = read_bytes(curve_handle, store)
            material_before = read_bytes(imported['material'], store)
            route = [{'curve': curve_handle, 'clip_id': literal['clips'][0]['id'],
                      'controller': 11, 'same_tick_order': 'before_existing'}]
            # Direct export can consume the musician's literal notes as-is.
            # Composed export consumes the exact public import handle. Both use
            # the same external public curve handle; neither edits the master.
            direct_args = {'material': literal, 'store_root': store, 'output_path': str(tmp_path / 'direct.mid'),
                           'request_id': 'direct-export', 'cc_step_bindings': route}
            direct = midi_export(**direct_args)
            composed_args = {**direct_args, 'material': imported['material'],
                             'output_path': str(tmp_path / 'composed.mid'), 'request_id': 'composed-export'}
            composed = await call('midi_export', composed_args)
            assert composed == cli_call(tmp_path, 'midi_export', composed_args) == midi_export(**composed_args)
            assert await call('midi_export', direct_args) == direct
            assert direct['midi'] == composed['midi'] and direct['sidecar'] == composed['sidecar']
            _, _, tracks = decode_wire(read_bytes(composed['midi'], store))
            assert [(time, wire) for _, time, wire in cc_rows(tracks)] == expected_cc()
            assert literal == source and literal['curves'] == []
            assert read_bytes(curve_handle, store) == curve_before
            assert read_bytes(imported['material'], store) == material_before
            sidecar = read_record(composed['sidecar'], store)
            assert sidecar['material'] == imported['material']
            assert sidecar['cc_step_encoding']['bindings'][0]['curve'] == curve_handle
            assert sidecar['native_roundtrip'] == sidecar['listening'] == 'not_performed'

            for index, patch in enumerate([{'controller': True}, {'controller': '11'}, {'controller': 11.0},
                                            {'controller': 64}, {'curve_id': 'mixed-shape'}, {'extra': True}]):
                output = tmp_path / f'bad-{index}.mid'
                request = f'bad-{index}'
                bad = {**composed_args, 'request_id': request, 'output_path': str(output),
                       'cc_step_bindings': [{**route[0], **patch}]}
                assert (await session.call_tool('midi_export', bad)).isError
                assert cli_call(tmp_path, 'midi_export', bad, success=False)['error'] == 'PocketError'
                assert not output.exists()
                assert not (Path(store) / 'requests' / request).exists()

    asyncio.run(asyncio.wait_for(exchange(), timeout=45))


def test_qa_actual_cli_reports_extreme_curve_integer_as_domain_error(tmp_path):
    curve = cc_curve()
    curve['points'][0]['value'] = 10 ** 400
    material = literal_material()
    material['curves'] = [curve]
    material['clips'][0]['curve_ids'] = [curve['id']]
    material = seal_literal(material)
    args = {'material': material, 'store_root': str(tmp_path / 'store'),
            'output_path': str(tmp_path / 'bad.mid'), 'request_id': 'bad-huge-integer',
            'cc_step_bindings': [{'curve_id': curve['id'], 'controller': 11,
                                  'same_tick_order': 'before_existing'}]}
    error = cli_call(tmp_path, 'midi_export', args, success=False)
    assert error['error'] == 'PocketError'
    assert not (tmp_path / 'bad.mid').exists()
