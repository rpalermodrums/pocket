# Pocket documentation

Pocket exposes composable music operations through Python, CLI and MCP.
These guides cover installation, provider inputs, artifact contracts and adapter limits.

Start with [installation and a synthetic example](getting-started.md), or read
[installed contracts](contracts.md) and [architecture](architecture.md).
See [status and direction](status.md) for current capabilities and future work.

## Use the tools

- [Musical context and practice](musical-context.md): clocks, internal cues,
  repeated passages and independently verified audio without a DAW.
- [MIDI and sound tools](midi.md) and [capability matrix](midi-capabilities.md).
- [Peek](peek.md), [Thread](thread.md), [Stitch](stitch.md): recording analysis,
  saved-set inspection and exact comparisons.
- [Selection interfaces](selection-interfaces.md), [Weave](weave.md),
  [Whisker](whisker.md), [workspace](workspace.md), [record bags](record-bag.md).
- [Acquisition](acquisition.md), [Spotify](spotify-bridge.md),
  [optional music embeddings](music-embeddings.md).
- [Baste](baste.md), [Pipette](pipette.md), [feedback](feedback.md).
- [Architecture](architecture.md) and [compatibility](compatibility.md).
- [Installed contracts and error handling](contracts.md).

The old-name pages remain short compatibility links. Reusable examples live in
`examples/`; they generate fixtures or accept a supplied recording.

## Local development material

Working plans, field notes, design exports, personal acceptance reports and test
audio belong under **`private/`**, which Git ignores. They are not required for
installation, running examples or tests. A fresh checkout does not contain them.

Local layout:

- `private/docs/reference/`: planning and acceptance notes, plus the documentation
  snapshot retained during cleanup. Current callable contracts live in these
  tracked usage guides and in provider discovery.
- `private/docs/architecture/`: the broader HTML/Markdown/CSV design package.
- `private/audio/`: original test recordings, retained receipts and render stores.

Never commit private recordings or listener notes. Generate test signals at
runtime. Keep API usage, compatibility guarantees and tool instructions tracked
so an agent can use Pocket from a fresh checkout.
