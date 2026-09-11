# Selection field notes — September 2026

Pocket 0.3 adds Weave and Whisker to the existing maps and transition experiments. The first field pass used an actual 884-record library snapshot, three 14-record research bags for warm-up, peak time and after-hours, and independently sourced local audio. Private library metadata, recordings, detailed listening hypotheses and model weights remain outside this repository.

## What the field pass changed

- **Creative exploration needed a scale.** The first stochastic search overwhelmed the small heuristic score differences and produced nearly shuffled routes. The first route is now a deterministic annotation baseline. Later variations use smaller, recorded perturbations, and zero creativity removes that perturbation. This improves adherence to explicit intent; it is not proof of musical quality.
- **A slot needs a trajectory.** Weave now records positional energy targets for a gentle warm-up rise, a peak plateau and an after-hours taper. An explicit target overrides the preset. Track energy remains attributed annotation; no measurement is invented.
- **Lift is relative.** Live hold/lift requests use the current annotated energy when available. A slot average should not accidentally pull a requested lift downward. Explicit targets still win.
- **A model must earn its place.** The first music-specific CLAP checkpoint collapsed contrasting inputs to near-identical vectors in this tested configuration. A smaller pinned unfused control showed coarse separation on jazz, rap, breakbeats and sparse electronics. Only the latter is accepted by the optional adapter. Listener-rated relevance, no-match calibration and transition quality remain open.
- **A successful process exit is not enough.** A real source emitted an Opus packet error with decoder exit code zero. Acquisition correctly preserved a failed receipt. Format inspection and explicit format pinning were added so a reviewed alternate encoding could be tried in a new directory. The alternate decoded completely; a sample-peak flag remained visible for review. Nothing was silently normalized or faded.
- **Human and agent views can collide.** A real browser/CLI handoff verified that a stale browser choice cannot overwrite a newer agent selection. Manual bag choices now preserve the active session history, import retries work after stale-state errors, and history uses readable recording names.

## What passed, and what has not

Generated tests exercise immutable identities, constraints, scoped feedback, deterministic baselines, controlled variation, shared-session concurrency, source failures, typed agent calls and the browser boundary. A wheel-only smoke starts the CLI and serves its bundled UI assets. The browser was checked on desktop and a narrow viewport.

On the development machine, an actual 1,000-record prepared session produced six options at approximately 24 ms p95. The live path performs no model inference or network requests. This is a machine-specific measurement, not a latency guarantee for every bag or filesystem.

The research bags are deliberately shorter than finished 90-minute sets. Full-recording totals and estimated performance durations are separate, and the workspace shows target shortfalls. No automatic route has been declared auditioned. Spotify API writes are covered by injected response/reconciliation tests; account authentication and an actual playlist transfer need their own verification.

## Next priorities

1. Run a small listening trial on the three contrasting bags: compare the curated direction, the baseline and a creative variant. Capture why a specific pair or passage works, rather than rewarding a whole track globally.
2. Prepare exact local editions and passage-level rhythm/harmonic evidence. Use the existing phrase and transition tools to investigate a few difficult joins before expanding each bag into a full set.
3. Add explicit opening/closing or chapter anchors if field use shows relative-order anchors are too loose. Improve local cue/section evidence before adding more model families.
4. Measure retrieval usefulness with listener judgments and a no-match condition. Embeddings are a discovery signal, not a musical judge.

Native playback integration, drag ordering, live deck telemetry and transcription remain later choices. The current shared record/session contracts provide a testable base without committing to a particular deck or model.
