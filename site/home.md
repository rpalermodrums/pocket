# Composable tools for music

Pocket is a Python toolkit with CLI and MCP interfaces for audio analysis, musical coordinates, MIDI operations and evidence-linked edits. Small operations, explicit inputs and inspectable results. You decide what to build with them.

[Read the documentation](../docs/README.md){: .primary-link }
[Browse the API reference](../reference/index.md){: .secondary-link }

## Python, CLI and MCP

The same public providers power all three interfaces. Use them directly from Python, pass a JSON specification to a CLI command, or expose them to an agent through the local MCP server.

From a source checkout, with Python 3.11 or later:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[agent]'
pocket --help
```

The `agent` extra supplies `pocket-mcp`. Core file operations do not require a DAW or model installation. MIDI file I/O and native/model adapters have separate dependencies and supported profiles.

[Installation and a synthetic example](../docs/getting-started.md) · [Contracts and errors](../docs/contracts.md)

## Explicit coordinates and retained evidence

**Coordinates.** Source sample time, timeline positions, repeated occurrences and internal anchors are represented separately. Mapping between them requires the relevant clock and occurrence identities.

**Artifacts.** Operations retain exact identities, declared processing and lineage. Edits create new versions. Request replay and verification preserve the connection between inputs and results.

**Interpretation.** Measurements, model hypotheses, authored decisions and listening reports are different records. Uncertainty and conflicting reports remain visible.

[Architecture](../docs/architecture.md) · [Musical context](../docs/musical-context.md) · [Capability boundaries](../docs/midi-capabilities.md)

## Reference from the implementation

The API reference is generated from installed Python signatures, the shared provider registry and actual MCP discovery. It includes input schemas and downloadable JSON contracts, with compatibility interfaces identified separately.

Supported profiles and limits are part of the contract. A proposed capability is not an installed tool, and a technical check is not a musical verdict.

[Explore callable providers](../reference/index.md) · [Compatibility](../docs/compatibility.md)

## Open source, early development

Pocket is MIT-licensed. Its scope is music, across instruments, genres and environments; the tools do not prescribe a workflow. Current implementation and future direction are documented separately.

[Project status](../docs/status.md) · [Contributing](../CONTRIBUTING.md) · [Changelog](../CHANGELOG.md) · [GitHub](https://github.com/rpalermodrums/pocket)
