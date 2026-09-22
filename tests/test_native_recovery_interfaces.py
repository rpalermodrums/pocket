"""Actual transports over synthetic recovery evidence, never native acceptance."""
import asyncio
import importlib.util
import json
import subprocess
import sys

import pytest
from test_candidate_abandonment_qa import (
    abandonment_setup,  # noqa: F401
    stop_synthetic_old_process,
    writer_bridge,  # noqa: F401
)
from test_midi_interfaces import environment
from test_native_midi_writer_qa import public_writer_setup

from pocket_music.native_candidates import candidate_native_abandon, candidate_native_reconcile
from pocket_music.native_midi import native_midi_write


def isolated_launcher(tmp_path, lease):
    launcher = tmp_path / "isolated_transport.py"
    launcher.write_text(
        "import importlib, sys\nfrom pathlib import Path\n"
        "from pocket_music import native_midi\n"
        f"native_midi._host_lease_path = lambda: Path({str(lease)!r})\n"
        "module = sys.argv.pop(1)\nsys.exit(importlib.import_module(module).main())\n"
    )
    return launcher


def transport_cli(tmp_path, launcher, name, value, success=True):
    spec = tmp_path / "transport-input.json"
    spec.write_text(json.dumps(value))
    completed = subprocess.run(
        [sys.executable, str(launcher), "pocket_music.cli", name.replace('_', '-'), "--spec", str(spec)],
        env=environment(), capture_output=True, text=True, timeout=20, check=False,
    )
    assert completed.returncode == (0 if success else 2), completed.stderr
    return json.loads(completed.stdout if success else completed.stderr)


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="Optional MCP extra not installed")
def test_abandonment_cli_stdio_provider_equivalence(abandonment_setup, tmp_path):  # noqa: F811
    args, _, old, _, _, _ = abandonment_setup
    stop_synthetic_old_process(old)
    # Only relocate the global host lease into the same synthetic fixture used
    # by the Max bridge. Both real entrypoints and all validation remain intact.
    launcher = isolated_launcher(tmp_path, old['host_lease'])

    def call_cli(value, success=True):
        return transport_cli(tmp_path, launcher, 'candidate_native_abandon', value, success)

    result = call_cli(args)
    assert result == candidate_native_abandon(**args)
    assert result["coverage"]["native_outcome"] == "unknown"
    assert result["workspace"]["state"] == "abandoned_native_unknown"

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exchange():
        params = StdioServerParameters(command=sys.executable, args=[str(launcher), "pocket_music.mcp_server"],
                                      env=environment())
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            response = await session.call_tool("candidate_native_abandon", args)
            assert not response.isError, response.content
            assert json.loads(response.content[0].text) == result
            invalid = {**args, "expected_revision": True, "request_id": "bad-recovery"}
            assert (await session.call_tool("candidate_native_abandon", invalid)).isError
            assert call_cli(invalid, success=False)["error"] == "PocketError"

    asyncio.run(asyncio.wait_for(exchange(), timeout=30))
    assert len(old["log"].read_text().splitlines()) == 1


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="Optional MCP extra not installed")
def test_fresh_writer_cli_then_stdio_velocity_and_reconciliation(writer_bridge, tmp_path, monkeypatch):  # noqa: F811
    args, host, _, _, _ = public_writer_setup(tmp_path, writer_bridge, monkeypatch)
    launcher = isolated_launcher(tmp_path, host['host_lease'])
    inserted = transport_cli(tmp_path, launcher, 'native_midi_write', args)
    assert inserted['status'] == 'ok'
    assert inserted == native_midi_write(**args)
    modified_args = {**args, 'request_id': 'stdio-velocity',
                     'expected_revision': inserted['workspace']['revision'],
                     'expected_observation': inserted['artifacts']['after_observation'],
                     'edit': {'kind': 'set_velocity', 'note_id': 101, 'velocity': 89,
                              'insertion_terminal': inserted['artifacts']['terminal']}}

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exchange():
        params = StdioServerParameters(command=sys.executable, args=[str(launcher), 'pocket_music.mcp_server'],
                                      env=environment())
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()

            async def call(name, value):
                response = await session.call_tool(name, value)
                assert not response.isError, response.content
                return json.loads(response.content[0].text)

            modified = await call('native_midi_write', modified_args)
            assert modified['status'] == 'ok'
            assert modified == native_midi_write(**modified_args)
            assert modified == transport_cli(tmp_path, launcher, 'native_midi_write', modified_args)
            recovery_args = {'store_root': args['store_root'], 'workspace_id': args['workspace_id'],
                             'expected_revision': modified['workspace']['revision'],
                             'terminal': modified['artifacts']['terminal'], 'request_id': 'stdio-reconcile',
                             'attribution': {'actor': 'Synthetic interface reviewer', 'actor_kind': 'agent',
                                             'observed_at': '2026-09-17T00:00:00+00:00',
                                             'reason': 'Verify terminal cleanup without another dispatch'}}
            recovered = await call('candidate_native_reconcile', recovery_args)
            assert recovered == candidate_native_reconcile(**recovery_args)
            assert recovered == transport_cli(tmp_path, launcher, 'candidate_native_reconcile', recovery_args)
            assert recovered['coverage']['native_redispatched'] is False
            malformed = {**modified_args, 'request_id': 'bad-note',
                         'edit': {**modified_args['edit'], 'velocity': True}}
            assert (await session.call_tool('native_midi_write', malformed)).isError
            assert transport_cli(tmp_path, launcher, 'native_midi_write', malformed, False)['error'] == 'PocketError'

    asyncio.run(asyncio.wait_for(exchange(), timeout=30))
    assert len(host['log'].read_text().splitlines()) == 2
    assert not host['host_lease'].exists()
