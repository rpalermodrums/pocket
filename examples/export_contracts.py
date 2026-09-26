# SPDX-License-Identifier: MIT
"""Generate installed Python/CLI/MCP contracts into a new local documentation folder.

Requires the optional agent extra. It uses actual MCP discovery and the shared
provider registry; it does not execute a tool, connect a host, or invent HTTP.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import inspect
import json
from pathlib import Path

from pocket_music import __version__
from pocket_music.capabilities import PUBLIC_CAPABILITIES
from pocket_music.error_contracts import JSON_SCHEMA
from pocket_music.mcp_server import build_server


async def build_contracts():
    server = build_server(error_format='legacy')
    tools = await server.list_tools()
    manifest = [t.model_dump(mode='json', exclude_none=True) for t in tools]
    schemas = {t.name: t.inputSchema for t in tools}
    registered = []
    for name, module, domain, operation, read_only, description in PUBLIC_CAPABILITIES:
        provider = getattr(importlib.import_module('pocket_music.'+module), name)
        registered.append({'name': name, 'domain': domain, 'operation': operation, 'read_only': read_only,
            'description': description, 'python': {'provider': 'pocket_music.'+module+'.'+name,
            'signature': str(inspect.signature(provider))},
            'cli': {'command': 'pocket '+name.replace('_', '-')+' --spec <arguments.json>',
                    'error_format_v2': 'pocket --error-format v2 '+name.replace('_', '-')+' --spec <arguments.json>'},
            'mcp': {'name': name, 'input_schema': schemas[name], 'structured_output': False,
                    'success_encoding': 'one compact JSON TextContent; same public provider receipt'},
            'output_contract': 'Dynamic dict receipt; artifact schema identities are in capability discovery. No closed output schema is claimed.'})
    registry_bytes = json.dumps(registered, sort_keys=True, separators=(',', ':')).encode()
    return {'schema': 'pocket.installed-contracts/v1', 'package_version': __version__,
            'provider_contracts_sha256': hashlib.sha256(registry_bytes).hexdigest(),
            'scope': 'All registered composable provider Python/flat-CLI/MCP bindings; complete MCP discovery also includes legacy branded tools and aliases.',
            'http': {'implemented': False, 'openapi': None},
            'registered_providers': registered, 'mcp_tools': manifest, 'error_envelope_v2': JSON_SCHEMA,
            'errors': {'legacy_default': True, 'cli_v2': 'Global --error-format v2; dispatch errors only; exit 2 remains unchanged.',
                       'mcp_v2': 'POCKET_ERROR_FORMAT=v2 opts registered strict providers into JSON tool-error text. Legacy branded tools retain their established SDK errors.',
                       'successful_receipts': 'Unchanged pocket.operation-receipt/v1; no retry or native-outcome inference.'}}


def export_contracts(destination: Path):
    if destination.exists():
        raise FileExistsError('Choose a new contract export directory')
    data = asyncio.run(build_contracts())
    destination.mkdir(parents=True, exist_ok=False)
    files = {'installed-contracts.json': data, 'mcp-tools.json': data['mcp_tools'],
             'python-cli.json': data['registered_providers'], 'error-v2.schema.json': data['error_envelope_v2']}
    for name, value in files.items():
        (destination/name).write_text(json.dumps(value, indent=2)+'\n')
    manifest = [{'path': name, 'sha256': hashlib.sha256((destination/name).read_bytes()).hexdigest()} for name in files]
    (destination/'manifest.json').write_text(json.dumps({'schema': 'pocket.contract-export/v1', 'files': manifest}, indent=2)+'\n')
    return {'registered_providers': len(data['registered_providers']), 'mcp_tools': len(data['mcp_tools']),
            'files': len(files)+1, 'http_implemented': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    print(json.dumps(export_contracts(args.destination.expanduser().resolve())))
