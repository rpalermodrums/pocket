<p align="center"><img src="assets/pocket.svg" alt="Pocket — Pip the field mouse in a terracotta record sleeve, with an audio waveform below the wordmark." width="960"></p>

**The musical toolkit for agents.** Pocket connects exact recordings, musical evidence, saved Ableton projects and small listening experiments. Every correction should make the next pass better informed.

Private, early development. Version 0.2 focuses on **Track Map**, **Set Map** and **Transition Lab**, with improvements tested against real sets. There is no transcription-model prerequisite.

| Tool | What it does |
|---|---|
| **Track Map** | Analyze exact source frames, attacks, competing pulse phases, crop sensitivity, local count uncertainty and tonal evidence. Keep musical beat one unresolved. |
| **Set Map** | Start with a compact project summary, then query a timestamp for clips, source frames and relevant controls. Reject stale saved maps. |
| **Transition Lab** | Collect a native trial's media, validate after relocation, separate render identity from signal readiness, and retrieve feedback about the exact audio heard. |

## Install

Python 3.11 or later:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[agent,dev]'
pocket --help
```

Core analysis uses NumPy, SciPy and soundfile. The optional `agent` extra supplies the MCP interface. Audio support follows the installed libsndfile build; decoded WAV/FLAC are recommended for exact comparisons. Live is needed to render a native project trial, not to inspect its saved arrangement.

## Try it locally

The generated demo contains only synthetic signals:

```sh
python examples/make_demo.py /tmp/pocket-demo
pocket track-map /tmp/pocket-demo/pulse.wav --duration 16 --bpm-hint 120
pocket lab create --spec /tmp/pocket-demo/comparison.json --output /tmp/pocket-demo/trial
```

For a saved Ableton project:

```sh
pocket set-map '/path/to/Your Set.als' --output /tmp/set-summary.json
pocket set-region /tmp/set-summary.json 2:22 --duration 32 --output /tmp/region.json
pocket find-clips /tmp/set-summary.json 'Clip name'
pocket map-export /tmp/set-summary.json --output /tmp/full-set-map.json
```

The summary contains an identity-bound handle for later calls. A valid region supplies `analyze_region_frame_args`; pass that exact pair to Track Map. In the CLI, use `--start-frame` and `--frames` without seconds flags. `set-map --full` remains available for an explicit raw inventory.

See [Track Map](docs/track-map.md), [Set Map](docs/set-map.md), [Transition Lab](docs/transition-lab.md), [feedback retrieval](docs/feedback.md) and the [agent workflow](skills/pocket/SKILL.md). Output records may contain local paths: keep them in your local working area, outside Git. The [0.2 release notes](docs/releases/0.2.0.md) describe compatibility changes and limits.

## Work with an agent

`pocket-mcp` serves the same provider records over standard input/output. Its `inspect_set` tool returns a summary and handle; `query_set_region`, `find_clips` and `export_set_map` reuse that handle. These four tools return one compact JSON text record, without a second expanded copy. A local MCP configuration can point to the executable in your virtual environment:

```json
{
  "mcpServers": {
    "pocket": {
      "command": "/absolute/path/to/pocket/.venv/bin/pocket-mcp"
    }
  }
}
```

Configure that local process only for agents you trust with your audio and project files. Pocket does not publish media or contact a model service. It does not change an open Live session on its own.

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

After using these three tools on more transitions, decide whether selective transcription, MuseTok, broader edit support or another tool would help most. Those are evaluation decisions, not a committed feature queue.

## Meet Pip

Pip is Pocket's field mouse: curious ears, a record sleeve for a pocket, and a tail curled like a groove. The [SVG mark](assets/pip.svg) and [README logo](assets/pocket.svg) are original, editable vectors with no external fonts or image dependencies.

Pocket is private while we test and assemble it. A public release and its license are later owner decisions.
