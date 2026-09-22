# Local audio evidence and attributed alternatives

Run with Pocket installed on a POSIX system:

```sh
python examples/audio-hypotheses/example.py
```

The script creates a synthetic four-second PCM click fixture in a new temporary directory, analyzes its exact frames, appends an explicitly authored phrase-span alternative, and queries the retained annotations. It then submits the same inputs to the optional owned local worker and verifies that its completed hypothesis handle equals the synchronous primitive's handle. The printed report identifies the directory and retained artifacts. No model, cloud upload, Live or Serum is used.

The source bytes and detector evidence are immutable. The phrase span is attributed to the example's agent author, with uncertainty; it is not presented as detected or musically approved. No instrument, kick, note, harmony or acceptable added layer is inferred. This example exercises file/workflow behavior, not E8 accuracy or correction cost on a musician's bass, drums or dense mix.

For another source, supply its actual SHA256 and exact `start_frame`/`frames`; the crop must fit within 20 seconds and two million frames. The full original file is retained. Keep private music and real listening notes outside repository fixtures. Longer set context remains in its source map; the bounded crop is an identified analysis window, not a substitute for whole-set context.

The optional worker returns a `pocket.job-handle/v1` and current revision. Use `job_status` for fresh state. To request cancellation, call `job_cancel` with that job ID and exact current revision. A stale revision conflicts. `cancel_requested` is pending until a safe boundary acknowledges cancellation; it is not immediate decoder termination. Cancelled/failed/interrupted jobs expose no committed hypothesis result. Do not automatically relaunch an interrupted request. Retained staging is diagnostic evidence.

All six calls use public providers and matching CLI/MCP tools. CLI spellings are `audio-hypotheses`, `audio-hypothesis-correct`, `audio-hypothesis-query`, `audio-hypothesis-submit`, `job-status` and `job-cancel`, each with `--spec <input.json>`. The [public contract](../../docs/midi.md#local-audio-hypotheses-corrections-and-optional-workers) documents bounded records, corrections, pagination and worker limits.
