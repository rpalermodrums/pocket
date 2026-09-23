# Foundation check on a recorded performance

Checkpoint: 23 September 2026. The user selected
[Wayne Shorter — Fee-Fi-Fo-Fum](https://www.youtube.com/watch?v=5bE0CZi-O8c)
for testing. The extractor identifies it as “Remastered1998/Rudy Van Gelder
Edition,” published by Wayne Shorter – Topic; that metadata is not independent
edition verification.

## Acquisition and source evidence

Pocket's `plan_acquisition` and `acquire_source` used `bestaudio/best` and pinned
the selected format before downloading. Format 251 was available: Opus in WebM,
48 kHz stereo. The original was retained and strictly decoded to FLOAT32 WAV:
16,995,200 frames, or 354.066667 seconds. No rate conversion, channel conversion,
normalization, gain or fades were applied.

The installed yt-dlp 2025.10.22 failed with HTTP 403. Its failed receipt was
preserved. An isolated task runtime with yt-dlp 2026.08.19 and its default EJS
dependencies succeeded, using the already installed Deno runtime. The shared
yt-dlp installation and Pocket's environment were unchanged. Runtime preparation
followed the [upstream installation](https://github.com/yt-dlp/yt-dlp/wiki/Installation)
and [EJS requirements](https://github.com/yt-dlp/yt-dlp/wiki/EJS).

The full recording contains **166 channel samples at or above full scale**, across
165 frames; measured sample peak is approximately **+0.829 dBFS**. The full decode
completed, but `ready_for_analysis` remains false with
`samples_at_or_above_full_scale`. This is a signal warning, not proof of audible
distortion or a reason to alter the source silently. True peak was not measured.

Original media, decoded audio, complete receipts, analysis and listening files
remain in the private acceptance directory outside Git. This document contains
only a reproducible method and aggregate technical findings, not recordings or
personal listener notes.

## Explicit passage tests

Each capture retains twenty seconds. Each A/B case renders an eight-second source
window twice. A starts 0.5 seconds into the capture; B moves the source window
50 ms later. These are **boundary probes**, not phrase-aligned musical loop claims.
All six outputs are 768,000 frames at 48 kHz stereo (16 seconds).

| Capture in recording | Peek pulse candidates (approx. BPM) | A/B signal result | Exact decoded source samples |
|---|---|---|---|
| 0:08–0:28 | None; preserve abstention | Both usable signal | Verified |
| 1:24–1:44 | 104.82, 34.94 | Both usable signal | Verified |
| 0:30–0:50 | 105.25, 52.61, 100.58, 26.29, 81.05 | Both `sample_overload`; six overload samples per repeated output | Verified |

The analysis used a four-beat probe setting. It did not identify time signature,
bar one, phrase boundaries or instrument-specific beat events. The context used
an explicitly nominal clock: a rounded leading hypothesis where available, or
a labeled 120 BPM **technical clock** when pulse analysis abstained. These clocks
label coordinates while preserving original playback rate; they do not quantize
the recording or assert its true tempo.

An observed attack inside each passage was selected as an `onset` anchor, not
renamed beat one. Each choice retains its analysis in the acceptance package.
The current context schema stores the chosen annotation ID in attributed text;
typed, source-checked interpretation references are the first next-phase task.

## What passed

- Public capture, analysis, context, resolve, render, compare, feedback and query
  providers composed successfully on all three passages.
- Repeated source anchors require an occurrence ID. Selected forward/inverse
  conversions returned the exact original source frame.
- Identical requests replayed verified receipts; changed input under the same
  request ID failed with `idempotency_conflict`.
- Independently decoded output samples matched the requested source windows
  exactly, including the overloaded FLOAT32 samples. No hidden limiter or gain
  change occurred. Original file identity remained unchanged.
- The affected comparison correctly kept `signal_ready=false`; the two clean
  comparisons returned true. Signal readiness never became a musical verdict.
- Real-recording CLI and MCP results matched direct Python providers. Querying
  a relocated copy of the artifact store returned the same verified result.
- Feedback records were explicitly agent technical reports. No human listening,
  keep decision or learned preference was invented.

The existing regression subset passed 118 tests before the foundation commits.
After adding an independently generated FLOAT32 overload regression, the final
context/practice/interface subset passed 27 tests. Lint passed for the added
exercise and regression. The earlier full-suite checkpoint remains documented
in [foundation status](foundation-status.md).

## Reproduce without committing media

After an authorized acquisition, supply the independently verified decoded WAV
and a **new** destination outside the repository:

```sh
python examples/exercise_practice_recording.py \
  /path/to/decoded.wav /path/to/new/private-practice-run \
  --start-seconds 8 84 30
```

The recipe writes full results and retained artifacts, and prints per-passage
status. Its source arguments are ordinary local recordings; it contains no
third-party recording, embedded download, private path or model requirement.
The captures, loop lengths and 50 ms variant are explicitly described in the
recipe; choose another experiment rather than interpreting these as musical cues.

## Browser playback boundary

Chrome rejected the retained DOUBLE WAV outputs as unsupported. Separate local
FLOAT32 review copies were created and independently decoded: all six preserve
every sample exactly, including overloads, because these source samples were
already FLOAT32. The retained DOUBLE evidence files were unchanged. This is an
explicit container export for this experiment, not a general promise of lossless
64-bit-to-32-bit conversion or a musical approval. A future browser-facing render
profile needs declared format support and its own fidelity checks.

## Remaining musical check

Listen to the unchanged passage and candidate repetitions at the same monitoring
level. Identify the actual phrase start/internal cue, and judge whether the join
is useful for practice. Record only the exact interval and aspect heard. Keep the
overloaded case separate for signal review; a completed render is not its approval.

Technical foundations passed on this reference. Acoustic musical acceptance,
native-host qualification, tempo-following and real-time accompaniment remain
unestablished. See the [concrete next-phase plan](next-phase-plan.md).
