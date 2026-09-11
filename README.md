<p align="center"><img src="assets/pocket.svg" alt="Pocket — Pip the field mouse in a terracotta record sleeve, with an audio waveform below the wordmark." width="960"></p>

**The musical toolkit for agents.** Pocket connects exact recordings, musical evidence, saved Ableton projects and small listening experiments. Every correction should make the next pass better informed.

Private, early development. Version 0.3 adds **Weave** and **Whisker** to Peek, Thread and Stitch. Plan several routes from one record bag, then keep a small, editable set of next-record options. Musical proposals stay separate from listening judgments.

| Tool | What it does |
|---|---|
| **Weave** | Explore reproducible routes with anchors, exclusions, explicit transition ideas and feedback scoped to a route or pair. |
| **Whisker** | Suggest next records for holding, lifting or changing direction; preserve manual choices in a shared session. |
| **Peek** | Analyze exact source frames, attacks, competing pulse phases, crop sensitivity, local count uncertainty and tonal evidence. Keep musical beat one unresolved. |
| **Thread** | Start with a compact project summary, then query a timestamp for clips, source frames and relevant controls. Reject stale saved maps. |
| **Stitch** | Collect a native trial's media, validate after relocation, separate render identity from signal readiness, and retrieve feedback about the exact audio heard. |

Weave shapes a route through records. Whisker is Pip feeling out what comes next. These complete the five-tool suite alongside Peek, Thread and Stitch; record bags and the descriptive Spotify, acquisition and embedding adapters support their work.

**Pipette** is reserved for a future tool. It has no implementation, command or MCP endpoint yet.

## Install

Python 3.11 or later:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[agent,dev]'
pocket --help
```

Core analysis and selection use NumPy, SciPy and soundfile. Record selection works without a model installation. The optional `agent` extra supplies the MCP interface. Audio support follows the installed libsndfile build; decoded WAV/FLAC are recommended for exact comparisons. Live is needed to render a native project trial, not to inspect its saved arrangement.

## Plan and choose records

Create a sealed bag from a user list or observed Spotify catalogue, then open its
local workspace:

```sh
pocket bag create --spec bag-spec.json --output new-bag-directory
pocket workspace --workspace-dir private-session --bag-handle bag-result.json
```

The first command prints a JSON result; save it as `bag-result.json` for the second.
The workspace prints a loopback URL and stays open until Ctrl-C. Human and agent
choices use the same bag, plans and session revisions. Try the three starting
briefs—warm-up, peak time and after-hours—then respond to specific routes or pairs.

See the [selection interface guide](docs/selection-interfaces.md) for a complete
specification example, command/output meanings and MCP inputs, or read
[record bags](docs/record-bag.md), [Weave](docs/weave.md),
[Whisker](docs/whisker.md) and the [workspace](docs/workspace.md).
[Spotify](docs/spotify-bridge.md) transfers deterministic catalogues and reviewed
fresh-playlist plans. [Acquisition](docs/acquisition.md) retains an explicitly
selected source's original codec and provenance. [Optional local embeddings](docs/music-embeddings.md)
provide qualified retrieval evidence; they are not required for selection.

## Inspect and test music

The generated demo contains only synthetic signals:

```sh
python examples/make_demo.py /tmp/pocket-demo
pocket peek /tmp/pocket-demo/pulse.wav --duration 16 --bpm-hint 120
pocket stitch create --spec /tmp/pocket-demo/comparison.json --output /tmp/pocket-demo/trial
```

For a saved Ableton project:

```sh
pocket thread '/path/to/Your Set.als' --output /tmp/set-summary.json
pocket thread-region /tmp/set-summary.json 2:22 --duration 32 --output /tmp/region.json
pocket thread-find-clips /tmp/set-summary.json 'Clip name'
pocket thread-export /tmp/set-summary.json --output /tmp/full-set-map.json
```

The summary contains an identity-bound handle for later calls. A valid region supplies `analyze_region_frame_args`; pass that exact pair to Peek. In the CLI, use `--start-frame` and `--frames` without seconds flags. `thread --full` remains available for an explicit raw inventory.

See [Peek](docs/peek.md), [Thread](docs/thread.md), [Stitch](docs/stitch.md), [feedback retrieval](docs/feedback.md) and the [agent workflow](skills/pocket/SKILL.md). Output records may contain local paths: keep them in your local working area, outside Git. The [0.2 release notes](docs/releases/0.2.0.md) describe compatibility changes and limits.

## Work with an agent

`pocket-mcp` serves the same provider records over standard input/output. Its `thread` tool returns a summary and handle; `thread_region`, `thread_find_clips` and `thread_export` reuse that handle. These four tools return one compact JSON text record, without a second expanded copy. `peek` analyzes a source region, and `stitch` creates a comparison trial. A local MCP configuration can point to the executable in your virtual environment:

```json
{
  "mcpServers": {
    "pocket": {
      "command": "/absolute/path/to/pocket/.venv/bin/pocket-mcp"
    }
  }
}
```

Configure that local process only for agents you trust with your audio and project files. Pocket does not upload recordings to a model service or change an open Live session on its own. Spotify execution and source discovery/acquisition contact external services only when explicitly invoked. Tokens remain in the local process environment, never tool arguments. Selection/model preparation stays outside the performance decision loop.

The [name compatibility guide](docs/compatibility.md) maps the previous names to these tools. Old commands and imports remain accepted; serialized formats, field keys, flags, caches and output artifacts are unchanged.

## The musical rules

- A tempo match does not identify beat one. Keep pulse, bar and phrase hypotheses separate.
- A key label does not prove a melodic overlap works. Inspect the local parts and listen to the handoff.
- A listener can approve one aspect of a trial while rejecting another. Preserve the exact scope of the correction.
- No implicit fades, normalization or crop expansion. Use the **phrase handoff approach** when the musical experiment calls for it; there is no universal crossfade recipe.
- An openable project, a successful export and a good transition are different results. Report each honestly.

## Development and next decisions

```sh
python -m pytest
```

Tests generate small signals and saved-project fixtures. Full recordings, renders, model weights and real listener notes stay outside the repository. [Architecture](docs/architecture.md) describes the shared contracts and extraction from earlier production work. [Evaluation](docs/evaluation.md) records the production field checks and what still needs listening tests. The [implementation plan](docs/implementation-plan.md) records scope, ownership and acceptance gates.

The [selection plan](docs/selection-plan.md) defines this cycle, and the [0.3 field notes](docs/selection-field-notes.md) record what actual library, model, acquisition and shared-session tests changed. Reproducible proposals, validated recording identity and successful model execution are different from a good set. Use listening feedback to decide what to keep; optional embeddings remain an experiment, with their observed limitations documented beside the provider.

## Meet Pip

Pip is Pocket's field mouse: curious ears, a record sleeve for a pocket, and a tail curled like a groove. The [SVG mark](assets/pip.svg) and [README logo](assets/pocket.svg) are original, editable vectors with no external fonts or image dependencies.

Pocket is private while we test and assemble it. A public release and its license are later owner decisions.
