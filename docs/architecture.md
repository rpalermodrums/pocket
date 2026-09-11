# One library, shared evidence

Pocket begins with a local Python package. The CLI and optional MCP server invoke the same functions; they are interfaces, not separate implementations. No hosted service, model download or running Live instance is required for the maps and rendered-audio comparisons.

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

## What we reused

Pocket extracts the timing, source-identity, native-readback and comparison ideas developed in the Love and Death production work and the owner's Black Seams projects. It replaces song-specific scripts and hardcoded paths with explicit inputs. Runtime DSP, models and transports have not been copied wholesale.

Black Seams' declarative source/clip contracts were useful architectural precedents. Its incomplete or simulated Live execution paths are not evidence of a working Pocket native backend. The first native trial adapter has a deliberately narrower contract, documented with its limitations.

NumPy, SciPy and soundfile provide numerical and audio-file primitives. The optional interface uses the [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk). Core tests use generated material; third-party recordings and model weights are not test fixtures in Git.

## Next review

Use Peek, Thread and Stitch on unfamiliar transitions. Record where the tool's representation was useful, ambiguous or wrong. Then choose between better beat/downbeat providers, selective note transcription, MuseTok, broader editing or improved native integration.

The decision criterion is whether a tool improves a musical choice or removes repeated work. A plausible MIDI file, more model confidence or more infrastructure is not sufficient on its own.

The [Baste/Pipette plan](baste-pipette-plan.md) evaluates the six proposed tools
and establishes this cycle's dependencies. Baste precedes a future guarded live
parameter writer; Pipette provides saved promotion for a future comparison UI.
Neither expands Stitch's editing or Max dependency collection scope. See
[acceptance](baste-pipette-acceptance.md) for current verification boundaries.
