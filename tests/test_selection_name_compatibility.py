"""Weave/Whisker aliases operate on the same saved plans and live revisions."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional agent extra')
def test_mcp_old_and_new_names_share_feedback_and_revision_guards(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from test_selection_interfaces import entries

    from pocket_music.record_bag import create_record_bag
    from pocket_music.weave import load_set_plan

    bag = create_record_bag(entries(), tmp_path / 'bag', title='Generated alias fixture')

    async def check():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'],
            env={'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()

            async def call(name, args):
                result = await session.call_tool(name, args)
                assert not result.isError, result.content
                assert result.structuredContent is None
                assert len(result.content) == 1 and len(result.content[0].text.splitlines()) == 1
                return json.loads(result.content[0].text)

            plan = await call('plan_set_routes', {'bag_handle': bag['handle'],
                'brief': {'setting': 'warm_up', 'track_count': 4}, 'seed': 7,
                'output_dir': str(tmp_path / 'legacy-plan')})
            original = load_set_plan(plan['handle'])
            route = plan['routes'][0]
            feedback = await call('weave_feedback', {'plan_handle': plan['handle'],
                'route_id': route['route_id'], 'disposition': 'avoid',
                'from_track_id': route['track_ids'][0], 'to_track_id': route['track_ids'][1],
                'output_dir': str(tmp_path / 'feedback')})
            replanned = await call('replan_set', {'plan_handle': feedback['handle'], 'seed': 7,
                                                 'output_dir': str(tmp_path / 'replan')})
            assert replanned['summary']['feedback_applied_count'] == 1
            assert load_set_plan(plan['handle']) == original

            current = await call('prepare_session', {'bag_handle': bag['handle'],
                'output_dir': str(tmp_path / 'session'), 'current_track_id': 'r0'})
            args = {'session_dir': current['session_dir']}
            assert await call('whisker_snapshot', args) == await call('session_snapshot', args)
            options = await call('whisker', args)
            assert options == await call('session_options', args)
            update = {**args, 'expected_revision': current['revision'], 'expected_sha256': current['sha256'],
                      'action': 'choose', 'track_id': options['options'][0]['track_id']}
            chosen = await call('whisker_update', update)
            assert chosen['revision'] == current['revision'] + 1
            stale = await session.call_tool('update_session', update)
            assert stale.isError
            assert await call('session_snapshot', args) == chosen

    asyncio.run(asyncio.wait_for(check(), timeout=60))
