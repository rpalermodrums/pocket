# Working on Pocket

Pocket is private during its initial testing period. Do not change repository visibility without the owner's explicit instruction.

The current scope includes Track Map, Set Map, Transition Lab, Set Workshop and On Deck. Selection has optional Spotify catalog/playlist, source-acquisition and local-embedding adapters plus a loopback browser workspace. Transcription models, a general audio editor and full-set bounce automation remain follow-up decisions, not implied dependencies.

Preserve these distinctions:

- A recording is identified by its bytes, not its song title or filename.
- Source sample time, arrangement beats, pulse, bar one and phrase start are separate coordinates/concepts.
- Measured evidence, algorithmic interpretation and listener feedback are separate records. Keep conflicting hypotheses; do not silently turn model confidence into an edit.
- A saved project describes supported control intent. A rendered file establishes output audio. Neither is a musical listening verdict.
- Experiments write new directories; never overwrite the user's project, media or prior trial. No implicit fades, normalization, resampling or crop expansion.
- Keep recordings, full mixes, model weights, local paths and real listener notes out of Git. Generate test media at runtime. Inspect the staged file list before committing.

Both CLI and MCP must call the same public library functions. Add regression tests for actual failure modes. Keep adapters replaceable and optional model environments isolated.

For selection work, preserve a deterministic annotated baseline alongside creative variants. Slot contours are planning intent, not measurements. Keep route/pair feedback scoped to exact bag and brief identities; keep live decisions in a revision-checked shared session. Do not interpret playlist order, heuristic score or embedding similarity as audition, phrase alignment or mix compatibility. Models operate on independently sourced local audio and user-authored intent, not imported Spotify content. Never run inference or network requests in the live suggestion loop.

The owner handles version control unless a request explicitly authorizes Git writes. The initial repository creation and implementation commit were authorized; do not assume blanket authorization for unrelated later pushes.
