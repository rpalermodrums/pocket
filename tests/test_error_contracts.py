"""Opt-in transport errors retain legacy behavior and stable machine dispositions."""
import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from pocket_music.artifact_store import run_request
from pocket_music.error_contracts import JSON_SCHEMA, error_envelope
from pocket_music.errors import PocketError


def test_error_envelope_and_request_conflict_codes(tmp_path):
    old = PocketError('Example')
    assert str(old) == 'Example' and isinstance(old, ValueError)
    assert error_envelope(old) == {'schema': 'pocket.error/v2', 'code': 'invalid_request',
                                  'error': 'PocketError', 'message': 'Example'}
    run_request(str(tmp_path), 'one', 'test', {'n': 1}, lambda: {'status': 'ok'})
    with pytest.raises(PocketError) as raised:
        run_request(str(tmp_path), 'one', 'test', {'n': 2}, dict)
    assert raised.value.code == 'idempotency_conflict'
    assert 'idempotency_conflict' in str(raised.value)
    assert error_envelope(FileNotFoundError('Missing'))['code'] == 'io_error'
    with pytest.raises(ValueError, match='Unknown'):
        error_envelope(old, code='guessed_retry')


def test_cli_legacy_and_opt_in_v2_agree_on_exit_and_message(tmp_path):
    path = tmp_path/'bad.json'
    path.write_text(json.dumps({'store_root': str(tmp_path/'store'), 'context': {}, 'target_clock_id': 'unknown',
                                'target_space': 'source_frame'}))
    results = []
    for options in [[], ['--error-format', 'v2']]:
        result = subprocess.run([sys.executable, '-m', 'pocket_music.cli', *options, 'context-resolve', '--spec', str(path)],
                                text=True, capture_output=True, timeout=30, check=False)
        assert result.returncode == 2 and not result.stdout
        results.append(json.loads(result.stderr))
    assert set(results[0]) == {'error', 'message'}
    assert results[1] == {'schema': 'pocket.error/v2', 'code': 'invalid_request', **results[0]}


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional agent extra')
def test_real_mcp_v2_input_and_provider_errors(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    async def run():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'],
            env={'PYTHONPATH': str(Path(__file__).resolve().parents[1]/'src'), 'POCKET_ERROR_FORMAT': 'v2'})
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            base = {'store_root': str(tmp_path), 'context': {}, 'target_clock_id': 'x', 'target_space': 'source_frame'}
            typed = await session.call_tool('context_resolve', base)
            assert typed.isError
            assert json.loads(typed.content[0].text)['code'] == 'invalid_arguments'
            invalid = await session.call_tool('capabilities_list', {'limit': 0})
            assert invalid.isError
            envelope = json.loads(invalid.content[0].text)
            assert envelope['schema'] == 'pocket.error/v2' and envelope['code'] == 'invalid_request'
    asyncio.run(asyncio.wait_for(run(), 60))


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional agent extra')
def test_exported_contracts_are_actual_discovery_and_validate(tmp_path):
    import hashlib

    from jsonschema import Draft202012Validator

    spec = importlib.util.spec_from_file_location('export_contracts', Path(__file__).resolve().parents[1]/'examples/export_contracts.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    destination = tmp_path/'contracts'
    result = module.export_contracts(destination)
    data = json.loads((destination/'installed-contracts.json').read_text())
    from pocket_music.capabilities import PUBLIC_CAPABILITIES
    assert {r['name'] for r in data['registered_providers']} == {r[0] for r in PUBLIC_CAPABILITIES}
    assert result['registered_providers'] == len(PUBLIC_CAPABILITIES)
    assert data['http'] == {'implemented': False, 'openapi': None}
    for tool in data['mcp_tools']:
        Draft202012Validator.check_schema(tool['inputSchema'])
    Draft202012Validator(JSON_SCHEMA).validate(error_envelope(PocketError('Example')))
    for entry in json.loads((destination/'manifest.json').read_text())['files']:
        assert hashlib.sha256((destination/entry['path']).read_bytes()).hexdigest() == entry['sha256']
    with pytest.raises(FileExistsError):
        module.export_contracts(destination)
