# Working on Pocket

Pocket is private during its initial testing period. Do not change repository visibility without the owner's explicit instruction.

The current scope includes Peek, Thread, Stitch, Weave, Whisker, Baste and Pipette. Selection has optional Spotify catalog/playlist, source-acquisition and local-embedding adapters plus a loopback browser workspace. Transcription models, a general audio editor and full-set bounce automation remain follow-up decisions, not implied dependencies.

Prefer the canonical tool names in new docs and calls. Baste is a fresh read-only Live observer; Pipette promotes explicit kept saved Stitch candidates into new projects. Preserve compatibility aliases and existing serialized identifiers; [name compatibility](docs/compatibility.md) distinguishes interface names from stable provider functions and data formats.

Preserve these distinctions:

- A recording is identified by its bytes, not its song title or filename.
- Source sample time, arrangement beats, pulse, bar one and phrase start are separate coordinates/concepts.
- Measured evidence, algorithmic interpretation and listener feedback are separate records. Keep conflicting hypotheses; do not silently turn model confidence into an edit.
- A saved project describes supported control intent. A rendered file establishes output audio. Neither is a musical listening verdict.
- Experiments write new directories; never overwrite the user's project, media or prior trial. No implicit fades, normalization, resampling or crop expansion.
- Keep recordings, full mixes, model weights, local paths and real listener notes out of Git. Generate test media at runtime. Inspect the staged file list before committing.

Both CLI and MCP must call the same public library functions. Add regression tests for actual failure modes. Keep adapters replaceable and optional model environments isolated.

For Baste, keep LiveAPI capabilities read-only and observations momentary. Never
turn runtime IDs into durable handles or silently return partial/old success.
Run the unchanged Max reader against its FakeLiveAPI capability tests and perform
separate native acceptance; generated tests are not evidence of Live behavior.
Coordinate the single Live instance with other work before native checks.
For Pipette, reverify every selected identity and signal disposition, require an
attributed keep, preserve parent/trial bytes, and seal lineage before publishing
the child. Only declared collected dependency hints may change from the candidate;
all other saved XML and media must be preserved. Use normal Thread validation.

For selection work, preserve a deterministic annotated baseline alongside creative variants. Slot contours are planning intent, not measurements. Keep route/pair feedback scoped to exact bag and brief identities; keep live decisions in a revision-checked shared session. Do not interpret playlist order, heuristic score or embedding similarity as audition, phrase alignment or mix compatibility. Models operate on independently sourced local audio and user-authored intent, not imported Spotify content. Never run inference or network requests in the live suggestion loop.

The owner handles version control unless a request explicitly authorizes Git writes. The initial repository creation and implementation commit were authorized; do not assume blanket authorization for unrelated later pushes.
