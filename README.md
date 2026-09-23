<p align="center"><img src="assets/pocket.svg" alt="Pocket — Pip the field mouse in a record sleeve, beside the Pocket wordmark." width="960"></p>

**The musical toolkit for agents.** Pocket is a Python toolkit with CLI and MCP interfaces for audio analysis, musical coordinates, MIDI operations and evidence-linked edits. Its operations are small and composable; you decide what to build with them.

Pocket is public, open source and in early development.

[Documentation](docs/README.md) · [Agent skill](skills/pocket/SKILL.md) · [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md)

## Quick start

Python 3.11 or later, from a checkout of this repository:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python examples/practice_context.py private/first-practice
pocket context-resolve --spec private/first-practice/resolve-spec.json
```

This generates a synthetic recording, repeats two passages, resolves an internal cue and retains an exact audio comparison. It prints the baseline and alternative WAV paths; `results.json` holds the comparison receipts. Choose a new output directory each time. No DAW, model, login or downloaded recording is needed. The tones are a technical example, not a musical listening test.

Read the [getting started guide](docs/getting-started.md) for expected output and the next steps.

## Capabilities

| Capability | Documentation |
|---|---|
| Describe passages, internal cues and repeated material; compare exact audio | [Musical context and practice](docs/musical-context.md) |
| Inspect timing and tonal evidence in a recording | [Peek](docs/peek.md) |
| Create, inspect and edit MIDI material with explicit constraints | [MIDI and sound tools](docs/midi.md) |
| Explore record routes or choose what comes next | [Weave](docs/weave.md), [Whisker](docs/whisker.md) and the [local workspace](docs/workspace.md) |
| Inspect a saved Ableton arrangement or compare a trial | [Thread](docs/thread.md) and [Stitch](docs/stitch.md) |
| Observe an open Live session or preserve a kept saved trial | [Baste](docs/baste.md) and [Pipette](docs/pipette.md) |

Core musical contexts and practice tools work without a DAW. Native workflows have separate setup and qualification limits. Analysis, technical validation and human listening are different kinds of evidence; a successful render is not a musical verdict.

## Optional interfaces

```sh
python -m pip install -e '.[agent]'       # MCP server
python -m pip install -e '.[midi]'        # MIDI file import/export
python -m pip install -e '.[agent,dev,midi]'  # Full developer test dependencies
```

`pocket-mcp` runs locally over standard input/output. Configure your agent to launch the executable in your virtual environment, then use [the Pocket skill](skills/pocket/SKILL.md). Composable providers share one implementation across Python, flat CLI commands and MCP. [Installed contracts](docs/contracts.md) describe discovery, actual input schemas and opt-in machine errors.

Give the process access only to files you intend it to work with. Core workflows run locally. Optional Spotify, acquisition and model adapters have their own explicit setup and network behavior; they are not part of the first experiment. Keep recordings, credentials and listener notes outside Git.

## Where this is going

Pocket is not limited to electronic music, a particular meter or Ableton. A responsive, living “band in a box” practice partner, including for jazz musicians, is a **post-v1 goal**. Accompaniment, following, comping and trading require separate musical and real-time qualification. See [status and direction](docs/status.md) for the distinction between current tools and future work.

## Project

- [Contribution rules and development setup](CONTRIBUTING.md)
- [Community conduct and private contact](CODE_OF_CONDUCT.md)
- [Report a security vulnerability privately](SECURITY.md)
- [Change history](CHANGELOG.md)

Pocket is licensed under the [MIT License](LICENSE). Bundled third-party code retains its accompanying license notices. A repository license does not grant rights to recordings, models or other independently sourced material.
