# Pocket guides

Every guide here stands on its own, but they read best in roughly this order.
If you're new, start with the first two.

## Start here

- [What’s Pocket?](about.md): why Pocket exists and where it came from.
- [Your first experiment](getting-started.md): install Pocket and run a complete
  example in a few minutes, with no DAW, model or account.
- [Key ideas](concepts.md): stores, receipts, clocks, occurrences and the kinds
  of evidence, in plain terms, plus a glossary.
- [Use Pocket with an agent](agents.md): connect an AI assistant over MCP and
  give it the playbook.

## Practice and listening

These work anywhere Python runs. No DAW is needed.

- [Musical context and practice](musical-context.md): mark passages, clocks and
  cues in a recording, render exact alternatives, compare them and keep
  attributed reports.
- [Practice review](practice-review.md): a local browser page for listening to a
  comparison and saving what you heard, pinned to the exact audio and interval.
- [Peek](peek.md): look inside a recording. See where sounds start, which pulse
  rates fit, how the tempo drifts and which pitch classes ring, without anything
  being decided for you.

## MIDI and sound

- [MIDI and sound tools](midi.md): import, generate, edit, structure and export
  MIDI material with explicit timing, locks and expression. Also covers audio
  hypotheses, control curves and sound plans.
- [What's supported](midi-capabilities.md): what works on its own, what works
  with Ableton Live, and what's still on the roadmap.

## Ableton Live

Optional adapters for Ableton Live 12. Each guide lists its setup.

- [Thread](thread.md): read a saved set. See its clips, their sources and where
  each moment of the arrangement comes from.
- [Stitch](stitch.md): compare exact renders of a transition, and prepare a
  single controlled change as a separate Live project.
- [Listening notes](feedback.md): find earlier notes about a Stitch trial,
  scoped to the exact audio and span.
- [Baste](baste.md): take a read-only look at the session that's open right now.
- [Pipette](pipette.md): carry one explicitly kept trial into a new project,
  with the original left untouched.

## Records and sets

Tools for planning and playing sets of records. They propose; you decide.

- [Choosing records](selection-interfaces.md): how the selection tools fit
  together, with command-line and MCP examples.
- [Record bags](record-bag.md): a sealed crate of records with honest,
  attributed notes.
- [Weave](weave.md): sketch several possible orders for a set.
- [Whisker](whisker.md): propose what could come next, in a session you and an
  agent share.
- [Local workspace](workspace.md): a small browser page for bags, routes and
  next-record choices.

## Optional connections

Each of these needs its own setup and is never used unless you ask for it.

- [Recording acquisition](acquisition.md): fetch one explicitly chosen,
  authorized recording URL with strict checks.
- [Spotify](spotify-bridge.md): import a playlist's catalog, or create a new
  private playlist from a reviewed plan.
- [Music embeddings](music-embeddings.md): an optional local model for coarse
  "sounds like" retrieval.

## Reference

- [Provider reference](https://rpalermodrums.github.io/pocket/reference/): every callable
  provider, with its Python signature, CLI command and MCP input schema,
  generated from the installed package.
- [Contracts and errors](contracts.md): export machine-readable contracts,
  choose machine errors and retry safely.
- [Architecture](architecture.md): how the library is organized and why.
- [Name changes](compatibility.md): earlier tool names and the aliases that
  still work.
- [Agent skill](../skills/pocket/SKILL.md): the working guide Pocket gives to AI
  agents.

## Project

- [Status and direction](status.md), [changelog](../CHANGELOG.md) and release
  notes for [0.4](releases/0.4.0.md) and [0.2](releases/0.2.0.md).
- [Contributing](../CONTRIBUTING.md), [code of conduct](../CODE_OF_CONDUCT.md)
  and [security](../SECURITY.md).
- [Maintaining the website](site-maintenance.md).

Runnable examples live in [`examples/`](https://github.com/rpalermodrums/pocket/tree/main/examples). They generate their own
test material or take a recording you supply.

Pages named `on-deck`, `set-map`, `set-workshop`, `track-map` and
`transition-lab` are short redirects kept for old links. See
[name changes](compatibility.md).
