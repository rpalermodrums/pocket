# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in transport errors retain legacy behavior and stable machine dispositions."""
import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from test_context_edits import apply, slip
from test_interpretations import choose, evidence_fixture
from test_musical_context import fixture

from pocket_music.artifact_store import request_status, run_request
from pocket_music.error_contracts import (
    CODE_HINTS,
    CODES,
    FAILED_REQUEST_HINT,
    JSON_SCHEMA,
    JSON_SCHEMA_V3,
    error_envelope,
)
from pocket_music.errors import PocketError
from pocket_music.musical_context import context_create, context_query, context_resolve

# The v2 contract is closed; v3 is how hints are added. Pin v2 so it cannot drift.
V2_SCHEMA = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
             "additionalProperties": False, "required": ["schema", "code", "error", "message"],
             "properties": {"schema": {"const": "pocket.error/v2"}, "code": {"enum": [
                 "invalid_request", "invalid_arguments", "io_error", "idempotency_conflict", "request_not_complete",
                 "stale_revision", "source_mismatch", "ambiguous_mapping", "unsupported_profile", "locked_field",
                 "evidence_mismatch"]}, "error": {"type": "string"}, "message": {"type": "string"}}}
AMBIGUOUS = 'Ambiguous repeated passage; supply occurrence_id'
AMBIGUOUS_HINT = 'Name the occurrence with occurrence_id; context_query with section "occurrences" lists them.'


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
    unavailable = tmp_path/'store-is-a-file'
    unavailable.write_text('Preserve this file')
    io_spec = {'store_root': str(unavailable), 'request_id': 'io-error', 'definition': {
        'context_id': 'io-fixture', 'title': 'I/O failure fixture',
        'attribution': {'actor': 'Fixture', 'actor_kind': 'agent', 'statement': 'Technical check', 'uncertainty': []},
        'sources': [], 'timelines': [], 'occurrences': [], 'anchors': [], 'materials': []}}
    spec_path = tmp_path/'io-spec.json'
    spec_path.write_text(json.dumps(io_spec))
    cli = subprocess.run([sys.executable, '-m', 'pocket_music.cli', '--error-format', 'v2',
                          'context-create', '--spec', str(spec_path)], capture_output=True, text=True,
                         timeout=30, check=False)
    assert cli.returncode == 2
    cli_error = json.loads(cli.stderr)
    assert cli_error['code'] == 'io_error'
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
            unavailable_result = await session.call_tool('context_create', io_spec)
            assert unavailable_result.isError
            assert json.loads(unavailable_result.content[0].text) == cli_error
    asyncio.run(asyncio.wait_for(run(), 60))
    assert unavailable.read_text() == 'Preserve this file'


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
    assert json.loads((destination/'error-v2.schema.json').read_text()) == V2_SCHEMA
    assert json.loads((destination/'error-v3.schema.json').read_text()) == data['error_envelope_v3'] == JSON_SCHEMA_V3
    for entry in json.loads((destination/'manifest.json').read_text())['files']:
        assert hashlib.sha256((destination/entry['path']).read_bytes()).hexdigest() == entry['sha256']
    with pytest.raises(FileExistsError):
        module.export_contracts(destination)


def _v2_and_v3(error, **options):
    v2, v3 = error_envelope(error, **options), error_envelope(error, version='v3', **options)
    assert set(v2) == {'schema', 'code', 'error', 'message'} and v2['schema'] == 'pocket.error/v2'
    assert {k: v for k, v in v3.items() if k != 'hint'} == {**v2, 'schema': 'pocket.error/v3'}
    return v2, v3


def test_v3_hints_name_the_fix_for_real_wrong_python_calls(tmp_path):
    assert JSON_SCHEMA == V2_SCHEMA
    store, context, definition, _, _ = fixture(tmp_path)
    args = {'store_root': store, 'context': context, 'anchor_id': 'internal-one',
            'target_clock_id': 'practice', 'target_space': 'arrangement_qn'}
    # A cue inside a repeated passage: the legacy message, type and code are unchanged.
    with pytest.raises(PocketError) as ambiguous:
        context_resolve(**args)
    assert str(ambiguous.value) == AMBIGUOUS and ambiguous.value.code == 'ambiguous_mapping'
    v2, v3 = _v2_and_v3(ambiguous.value)
    assert v2 == {'schema': 'pocket.error/v2', 'code': 'ambiguous_mapping', 'error': 'PocketError', 'message': AMBIGUOUS}
    assert v3['hint'] == AMBIGUOUS_HINT
    # Following the hint needs only the calls it names; nothing is guessed for the caller.
    listed = [row['occurrence_id'] for row in context_query(store, context, 'occurrences')['rows']]
    assert listed == ['first', 'again', 'alternative']
    assert context_resolve(**args, occurrence_id='again')['occurrence_id'] == 'again'
    with pytest.raises(PocketError) as unknown:
        context_resolve(**{**args, 'anchor_id': 'downbeat'})
    assert _v2_and_v3(unknown.value)[1]['hint'].startswith('context_query with section "anchors"')

    # A reused request ID with changed inputs, then an interrupted request left by a crash.
    with pytest.raises(PocketError) as conflict:
        context_create(store, 'context', {**definition, 'title': 'Renamed'})
    assert _v2_and_v3(conflict.value)[1]['hint'] == CODE_HINTS['idempotency_conflict']
    (Path(store)/'requests/crashed/lock').mkdir(parents=True)
    with pytest.raises(PocketError) as interrupted:
        context_create(store, 'crashed', definition)
    assert interrupted.value.code == 'request_not_complete'
    assert 'request_status' in _v2_and_v3(interrupted.value)[1]['hint']
    status = request_status(store, 'crashed')
    assert status['status'] == 'outcome_unknown' and status['coverage']['lock_present']

    # A locked field in a write: the journal has recorded that request ID as failed.
    (tmp_path/'edit').mkdir()
    edit = fixture(tmp_path/'edit')
    lock = [{'section': 'occurrences', 'object_id': 'first', 'fields': ['source_span_frames']}]
    with pytest.raises(PocketError) as locked:
        apply(edit, [slip()], lock)
    hint = _v2_and_v3(locked.value)[1]['hint']
    assert hint.startswith('Revise operations so every field named in locks') and hint.endswith(FAILED_REQUEST_HINT)
    with pytest.raises(PocketError, match='idempotency_conflict'):
        apply(edit, [slip(['alternative'])], lock)
    assert apply(edit, [slip(['alternative'])], lock, request='edit-alternative')['status'] == 'ok'
    assert _v2_and_v3(FileNotFoundError('Missing'))[1]['hint'] == CODE_HINTS['io_error']
    assert 'hint' not in _v2_and_v3(PocketError('Example'))[1]
    # A site hint belongs to its own code; an explicit transport code uses that code's hint.
    assert _v2_and_v3(ambiguous.value, code='invalid_arguments')[1]['hint'] == CODE_HINTS['invalid_arguments']
    with pytest.raises(ValueError, match='version'):
        error_envelope(ambiguous.value, version='v4')


def test_v3_hint_for_mixed_evidence_revisions(tmp_path):
    f = evidence_fixture(tmp_path)
    _, _, _, _, evidence, _, original = f
    # The annotation came from the corrected revision, but the expected revision is the earlier one.
    with pytest.raises(PocketError) as stale:
        choose(f, evidence={**evidence, 'expected_revision': original['sha256']})
    assert str(stale.value) == 'Stale interpretation evidence revision'
    hint = _v2_and_v3(stale.value)[1]['hint']
    assert hint.startswith('Pass the hypotheses handle you chose annotation_id from')
    assert hint.endswith(FAILED_REQUEST_HINT)
    assert choose(f, request='corrected')['status'] == 'ok'


@pytest.mark.skipif(importlib.util.find_spec('jsonschema') is None, reason='jsonschema arrives with the agent extra')
def test_v3_schema_accepts_every_code_hint_and_v2_stays_closed(tmp_path):
    from jsonschema import Draft202012Validator, ValidationError
    Draft202012Validator.check_schema(JSON_SCHEMA_V3)
    for code in CODES:
        envelope = error_envelope(PocketError('Example', code=code), version='v3')
        Draft202012Validator(JSON_SCHEMA_V3).validate(envelope)
        assert ('hint' in envelope) is (code != 'invalid_request')
        def fail(code=code):
            raise PocketError('Example', code=code)
        with pytest.raises(PocketError) as failed:
            run_request(str(tmp_path), f'fails-{code}', 'test', {}, fail)
        envelope = error_envelope(failed.value, version='v3')
        Draft202012Validator(JSON_SCHEMA_V3).validate(envelope)
        assert envelope['hint'].endswith(FAILED_REQUEST_HINT) and envelope['hint'].count(FAILED_REQUEST_HINT) == 1
    with pytest.raises(ValidationError):
        Draft202012Validator(JSON_SCHEMA).validate({**error_envelope(PocketError('Example')), 'hint': 'Extra'})


def _cli(tmp_path, name, spec, *options):
    path = tmp_path/f'{name}-{len(list(tmp_path.glob("*.json")))}.json'
    path.write_text(json.dumps(spec))
    result = subprocess.run([sys.executable, '-m', 'pocket_music.cli', *options, name, '--spec', str(path)],
                            text=True, capture_output=True, timeout=30, check=False)
    assert result.returncode == 2 and not result.stdout
    return json.loads(result.stderr)


def test_cli_v3_adds_hints_while_legacy_and_v2_stay_unchanged(tmp_path):
    store, context, _, _, _ = fixture(tmp_path)
    spec = {'store_root': store, 'context': context, 'anchor_id': 'internal-one',
            'target_clock_id': 'practice', 'target_space': 'arrangement_qn'}
    legacy, v2, v3 = (_cli(tmp_path, 'context-resolve', spec, *options)
                      for options in ([], ['--error-format', 'v2'], ['--error-format', 'v3']))
    assert legacy == {'error': 'PocketError', 'message': AMBIGUOUS}
    assert v2 == {'schema': 'pocket.error/v2', 'code': 'ambiguous_mapping', **legacy}
    assert v3 == {**v2, 'schema': 'pocket.error/v3', 'hint': AMBIGUOUS_HINT}
    # A misspelled argument name reaches the provider call; v3 names the contract to check.
    typo = {**spec, 'occurence_id': 'again'}
    v2, v3 = (_cli(tmp_path, 'context-resolve', typo, '--error-format', version) for version in ('v2', 'v3'))
    assert v2['code'] == 'invalid_arguments' and 'hint' not in v2
    assert v3 == {**v2, 'schema': 'pocket.error/v3', 'hint': CODE_HINTS['invalid_arguments']}


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None, reason='Optional agent extra')
def test_real_mcp_v3_hints_name_arguments_and_v2_is_unchanged(tmp_path):
    from jsonschema import Draft202012Validator
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    store, context, definition, _, _ = fixture(tmp_path)
    base = {'store_root': store, 'context': context, 'anchor_id': 'internal-one',
            'target_clock_id': 'practice', 'target_space': 'arrangement_qn'}
    missing = {k: v for k, v in base.items() if k != 'target_space'}
    calls = [
        ('context_resolve', base, AMBIGUOUS_HINT),
        ('context_resolve', {**base, 'occurence_id': 'again'},
         "Remove arguments the tool doesn't declare. Its input schema in tools/list lists every accepted name."),
        ('context_resolve', missing, 'Supply the required argument target_space.'),
        ('capabilities_list', {'limit': '5'}, 'Make limit match the tool\'s input schema exactly. Values are '
         'validated strictly and never coerced; for example, the string "5" is not an integer.'),
        ('context_create', {'store_root': store, 'request_id': 'context',
                            'definition': {**definition, 'title': 'Renamed'}}, CODE_HINTS['idempotency_conflict']),
    ]
    async def run(version):
        params = StdioServerParameters(command=sys.executable, args=['-m', 'pocket_music.mcp_server'],
            env={'PYTHONPATH': str(Path(__file__).resolve().parents[1]/'src'), 'POCKET_ERROR_FORMAT': version})
        envelopes = []
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            for name, arguments, _ in calls:
                result = await session.call_tool(name, arguments)
                assert result.isError
                envelopes.append(json.loads(result.content[0].text))
        return envelopes
    v2s, v3s = (asyncio.run(asyncio.wait_for(run(version), 60)) for version in ('v2', 'v3'))
    for v2, v3, (_, _, hint) in zip(v2s, v3s, calls, strict=True):
        Draft202012Validator(JSON_SCHEMA).validate(v2)
        Draft202012Validator(JSON_SCHEMA_V3).validate(v3)
        assert v3 == {**v2, 'schema': 'pocket.error/v3', 'hint': hint}
    assert [e['code'] for e in v2s] == ['ambiguous_mapping', 'invalid_arguments', 'invalid_arguments',
                                        'invalid_arguments', 'idempotency_conflict']
