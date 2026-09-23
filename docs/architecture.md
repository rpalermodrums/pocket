# Architecture

Pocket is a local Python package. The `pocket` command line and the optional
`pocket-mcp` server call the same functions that Python code does. They are
three doors into one implementation, not three implementations. No hosted
service, model download or running DAW is needed for the core tools.

Pocket's goal is an agentic toolkit for music across practice, composition and
performance. The design choices below follow from one principle: **a musical
decision should rest on evidence you can inspect, and every experiment should be
reversible.** For a gentler introduction to the vocabulary, read
[key ideas](concepts.md) first.

The [musical context layer](musical-context.md) is the foundation for explicit
clocks, authored anchors and repeated occurrences. Its original-rate practice
renderer shares signal-evidence code with the Ableton audition adapter. Provider
discovery (`capabilities_list`) always describes what is actually installed.

## Records and clocks

An **asset** identifies exact file bytes and decoded sample metadata. An analysis **region** keeps its original-source frame bounds. A **Thread** describes saved clip and control intent, with separate arrangement beats and source positions. A **handle** identifies an immutable compressed snapshot and the saved project/dependency versions that make it applicable. A **trial** binds its sources, explicit variation, generated output bytes and scoped listener feedback.

Source seconds are not arrangement seconds. A clip's physical start is not necessarily its musical cue. Warped source mappings use the saved markers; natural passages use the supported tempo map. Unsupported mapping cases return an explicit unknown instead of an approximate answer disguised as exact.

JSON schema identifiers mark the record family/version. The package uses validated provider functions and generated-fixture regression tests rather than a separate schema compiler. Changes to output contracts need tests and a version decision. Native trial manifests are now v2; query/summary/handle records introduce their own v1 families, while Peek's additive evidence retains its v1 family and records analysis version 1.1.2.

## Provider boundaries

- `assets.py`: stable file hashing and decoded metadata. Identical bytes retain identity after renaming; clean/explicit versions cannot collapse by title.
- `peek.py` and `rhythm_continuity.py`: bounded signal evidence, independent band-phase modes, crop refits, local phase/count warnings and tonal content. Scores are heuristic; no automatic edit follows them.
- `thread.py` and `timing.py`: read saved Live XML, resolve active source references and supported source/arrangement mappings. Stock control inspection does not evaluate device DSP.
- `thread_queries.py` and `source_frames.py`: immutable snapshot handles, bounded timestamp queries, explicit omission budgets and inward conversion to complete source frames. Queries verify the snapshot, ALS and dependency versions; unsupported mappings remain unknown.
- `stitch.py`: exact comparisons, collected native candidates, relocation validation, signal/readiness checks and feedback recording. Native observation fields are supplied reports; the provider does not operate Live or independently certify an export.
- `feedback.py`: sealed output/interval/scope retrieval with pagination and no synthesized verdict.
- `baste.py` and `devices/baste/`: fresh live observation through the actual read-only Max for Live reader and authenticated local transport. Saved maps remain separate from momentary runtime IDs. Device sources are package data, not an optional simulated implementation.
- `pipette.py`: explicit kept-trial promotion, current evidence checks, collected-reference-only child rewrites and sealed lineage. Calls existing Stitch validation and normal Thread summary/handle queries; never merges unsaved state.
- `cli.py` and `mcp_server.py`: the common tool surface. MCP inputs have explicit nested types. Four summary/query tools emit one compact JSON text record to avoid duplicate transport expansion; their parsed records match the providers.

The integer source-frame pair is the preferred boundary from a set query to Peek. The conversion snaps at most 0.1 sample of floating-point overshoot at a file endpoint, and otherwise rounds inward. It records every adjustment and never interprets mapping precision as certainty about a musical downbeat.

Native v2 preparation first collects supported active dependencies and proves the declared reference rewrites and media shift through XML rollback/readback. Validation after moving the project uses collected relative files and hashes. Render attachment preserves independent artifact, signal, loading and export labels. `ready_to_compare` requires usable expected signal and appropriate reported native observations; it never becomes a perceptual verdict.

Analysis and project outputs can contain local paths. Keep those in a local working directory outside the repository. The package never uploads audio. An MCP client runs with the filesystem access of the local server process; use it with trusted agents.

## Built on

Pocket stands on a small set of dependencies, each chosen for a specific job:

- [NumPy](https://numpy.org/), [SciPy](https://scipy.org/) and
  [soundfile](https://github.com/bastibe/python-soundfile) (with libsndfile)
  provide numerical and audio-file primitives.
- The [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
  powers the optional agent interface.
- [Mido](https://github.com/mido/mido) handles optional Standard MIDI File
  import and export.
- Ableton Live 12 and Max for Live are the first supported native hosts. Pocket
  reads Live's saved-set format and ships its own Max for Live devices.
- Optional model adapters run [BeatThis](https://github.com/CPJKU/beat_this)
  (learned pulse), [Basic Pitch](https://github.com/spotify/basic-pitch) (note
  hypotheses) and [LAION CLAP](https://huggingface.co/laion/clap-htsat-unfused)
  (music embeddings) in separate environments you set up yourself. Pocket's
  note decoder adapts part of Basic Pitch under its Apache-2.0 license, which is
  bundled alongside it.

Core tests generate their own material. Third-party recordings and model
weights are never test fixtures in Git.

## What comes next

Use Peek, Thread and Stitch on unfamiliar transitions, and record where each
tool's representation was useful, ambiguous or wrong. That evidence decides
what to build next: better beat and downbeat providers, selective note
transcription, symbolic music models, broader editing or deeper native
integration.

The test is whether a tool improves a musical choice or removes repeated work.
A plausible MIDI file, a more confident model or more infrastructure isn't
enough on its own.

[Baste](baste.md) observes a live session, and [Pipette](pipette.md) preserves
an explicitly kept saved candidate. Neither expands Stitch's editing or its Max
dependency collection. A future live writer will need its own separate
qualification.
