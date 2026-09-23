# Installed contracts and errors

Python, flat CLI commands and registered MCP tools call the same providers in
`PUBLIC_CAPABILITIES`. Discover supported artifact schemas and profiles with
`capabilities_list`; a proposed architecture facade is not an installed API.
The general HTTP/OpenAPI facade is not implemented. The existing loopback
workspace serves its own browser routes for Weave/Whisker; those routes are
separate from the provider contracts exported here.

With the optional `agent` extra installed, generate contracts into a new folder:

```sh
python examples/export_contracts.py private/docs/contracts/current
```

The export includes Python signatures, CLI commands, actual MCP input schemas,
the complete MCP manifest (including legacy tools), the optional error schema,
and file hashes. It reads discovery without invoking providers or a DAW. Output
receipts remain dynamic dictionaries: this export does not invent closed output
schemas. Existing destinations are refused so earlier contracts remain intact.

## Opt-in machine errors

Legacy errors remain the default. For registered provider dispatch, opt in with:

```sh
pocket --error-format v2 context-query --spec arguments.json
POCKET_ERROR_FORMAT=v2 pocket-mcp
```

The envelope contains `schema: pocket.error/v2`, `code`, `error` (exception class)
and the unchanged `message`. CLI errors still exit 2; successful receipts are
unchanged. CLI argument-parser usage errors and legacy branded MCP tools retain
their established error handling. MCP v2 envelopes are JSON text in tool errors,
not successful structured outputs.

Codes distinguish invalid requests/arguments, I/O failures, request-ID conflicts,
incomplete requests, stale revisions, source mismatch, ambiguous mappings,
unsupported profiles, locked fields and evidence mismatch. Specific codes come
from explicit failure sites, never guesses from message wording. Unclassified
provider failures remain `invalid_request`; no retry guarantee is implied.

## Recovery

1. Retain the request ID, exact input handles and error. Read request status using
   the existing journal/status surface before retrying an interrupted operation.
2. An identical successful request replays a reverified receipt. Changed inputs
   require a new request ID. Never remove a lock to manufacture successful replay.
3. Resolve stale revisions against an exact retained revision; identify the source
   clock or occurrence when mapping is ambiguous. Do not round or guess a cue.
4. A failed artifact check needs intact retained evidence. Relocate the complete
   store, not only its final audio. Output audio alone does not prove lineage.

Planning exports and recording-specific evidence belong in ignored `private/`.
This guide, examples and provider contracts remain usable from a fresh checkout.
