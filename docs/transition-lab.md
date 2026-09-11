# Transition Lab

Transition Lab turns a specific musical question into a small comparison with traceable audio. Start with completed renders, choose the passage, and attach a listening note to the exact version and time span reviewed. It does not decide which version sounds better.

The CLI and MCP call the same four Python providers in `pocket_music.transition_lab`:

| Provider | Result |
|---|---|
| `create_trial` | Exact excerpts, a sealed trial manifest and per-variant source/output hashes |
| `record_feedback` | A new timestamped note bound to one variant, output hash and local frame interval |
| `prepare_native_trial` | A separate Ableton candidate with one media-only clip translation |
| `attach_completed_render` | A verified copy of a completed, explicitly declared native export |

## Compare existing renders

Save a specification outside the repository. Paths below are placeholders for your own completed files; frame numbers assume 48 kHz:

```json
{
  "title": "Opening handoff: same-clock comparison",
  "start_frame": 4608000,
  "frames": 1536000,
  "variants": [
    {"label": "Before", "source_path": "mix-before.wav"},
    {"label": "Candidate", "source_path": "mix-candidate.wav", "gain_db": 0}
  ]
}
```

```sh
pocket lab create --spec trial-spec.json --output /path/to/local-trials/opening-01
```

Both variants take source frames 4,608,000 through 6,143,999: 96–128 seconds. Bounds are end-exclusive. Optional `expected_sha256` on each variant rejects an outdated source. A whole-file hash identifies the parent recording; `source_window_float64_sha256` identifies the selected decoded window before gain.

Defaults matter:

- No fades, normalization, resampling, downmix or crop expansion.
- Gain defaults to exactly 0 dB. Any different gain is explicit and recorded.
- DOUBLE WAV preserves decoded samples exactly at zero gain, including the first and last samples. It does not promise identical source container bytes or a particular playback application's support for 64-bit float WAV.
- Sample rates and channel counts must match across variants. Mono and stereo are supported. Each excerpt is at most five minutes, with at most eight variants.
- Lengths must match unless `allow_duration_mismatch: true` is explicit. Set `start_frame` and/or `frames` inside a variant to compare separately mapped source passages. These overrides are recorded; the tool does not infer musical equivalence.
- Explicit gain can produce floating-point values above full scale. Such values are preserved and counted, never silently limited. The receipt includes sample peaks and finite-sample checks; true-peak/loudness metering and mastering are outside this tool.

Same clock, same source phrase and same perceived level are different comparisons. Choose and label the one the question needs. Separately mapped passages can be useful when an arrangement revision moves a handoff; an equal clock window can include different material.

The trial directory is new and cannot replace a previous trial. `trial.json` and its SHA-256 sidecar bind the generated variants. Later feedback hashes and parses the same manifest bytes, checks file stability, and verifies the selected audio again. These local hashes detect stale or accidentally edited files; they are not signatures against deliberate alteration of both a file and its checksum.

## Record a listening observation

After reviewing an output, pass its current `output.sha256` and an interval in **that excerpt's local frames**, not parent-source frames. For example, a local interval from 48,000 to 96,000 means seconds 1–2 of a 48 kHz excerpt.

```python
from pocket_music.transition_lab import record_feedback

record_feedback(
    trial_dir,
    "v02",
    output_sha256=output_hash,
    note=listener_observation,
    start_frame=48000,
    end_frame=96000,
    scope="timing",
)
```

Supported scopes are `timing`, `bar_phase`, `flow`, `tonal_overlap`, `level`, `preference` and `other`. The note and optional listener name are explicitly supplied information. The tool adds a UTC timestamp and unique immutable record under `feedback/`; it does not claim that an agent listened or generalize the observation to another render, source, passage or project. CLI equivalent: `pocket lab feedback --spec feedback-spec.json`.

## Prepare one supervised native experiment

This first adapter answers a narrow question: what happens if one piece of audio moves relative to the existing controls? It is not a general arrangement editor.

```python
from pocket_music.transition_lab import prepare_native_trial

candidate = prepare_native_trial(
    source_als,
    new_trial_dir,
    clip_id="track:1001/clip:0",
    shift_beats=1,
    export_start_beat=192,
    export_length_beats=64,
    expected_als_sha256=source_hash,
    range_name="Opening media-only shift",
)
```

Use the canonical ID from Set Map. An ambiguous track/clip pair is rejected. The adapter translates only that AudioClip's `Time`, `CurrentStart` and `CurrentEnd`, then reads the candidate back and proves that reversing those three edits restores all original XML fields. Host automation remains fixed. Source bounds, negative pickups, warp anchors, gains, pitch, tempo and clip-fade settings are unchanged. Clip-attached content and its unchanged fades move with the clip.

The supported scope is deliberately small: audio-only hosts, a non-looping warped Arrangement AudioClip with `StartRelative=0`, no clip automation, no external `PluginDevice`, a nonzero shift of at most 16 beats, and a constant-tempo named export interval of at most five minutes. The candidate and export range cannot extend beyond the existing Arrangement audio boundary. Unknown tempo curves and child curve nodes are rejected. This does not infer bar one or move the associated track automation for you.

The new `candidate.als` retains verified absolute audio and Max patch references, so `portability` is false. It does not duplicate an entire record collection. Existing conflicting project-relative references at either the original or candidate location are rejected; attachment checks the candidate location again. Historical preset pointers are not treated as active load dependencies. Stock device behavior, Max internals and dependencies hidden inside devices remain outside this adapter's validation.

Open the candidate in Live manually and render the **named range in the manifest**. Pocket does not change the saved loop range, operate Live, select export settings, or observe an export. Keep the generated candidate unchanged; use Save As elsewhere if Live needs to save. A saved/edited candidate fails the original-hash precondition and requires a new reviewed trial.

## Attach the completed native output

`pocket lab attach-render --spec completed-export.json` accepts the same arguments as:

```python
from pocket_music.transition_lab import attach_completed_render

attachment = attach_completed_render(
    trial_dir,
    completed_float_wav,
    expected_candidate_sha256=candidate_hash,
    rendered_start_beat=192,
    rendered_length_beats=64,
    expected_frames=1536000,
    settings={
        "rendered_track": "Main",
        "sample_rate": 48000,
        "channels": 2,
        "normalization": False,
        "mono": False,
        "loop_render": False,
        "dither": "none",
    },
    export_completed=True,
)
```

The explicit completion/range/settings statements are **user-supplied export evidence**. They are not proof that Live rendered this candidate. Pocket independently verifies the unchanged candidate and dependencies, the actual floating-point stereo WAV header, complete finite decoded frame count, stable audio hash, and byte-identical attachment copy. Duration must be within two frames of the constant-tempo modeled interval, with the exact discrepancy recorded. A larger difference is rejected for review, not excused as ordinary native rounding.

The attachment is a new immutable subdirectory under `renders/`. No gain or fades are applied. Create a normal trial from its `render.wav` and the comparable baseline render to gather scoped feedback. Full-set rendering, arbitrary native normalization/readback, automatic source mapping across tempo changes, true-peak mastering and promotion of a winner remain later work.

## Generated-data demonstration

Run the following Python after choosing a new output directory **outside the repository**. It creates its own four-second test signal; no recording is bundled:

```python
from pathlib import Path
import numpy as np
import soundfile as sf
from pocket_music.transition_lab import create_trial

folder = Path(output_directory)
folder.mkdir(parents=True, exist_ok=False)
samples = 0.1 + 0.05 * np.sin(np.arange(192000) * 0.025)
source = folder / "generated.wav"
sf.write(source, samples, 48000, subtype="DOUBLE")
result = create_trial(
    folder / "trial",
    [
        {"label": "Unchanged", "source_path": str(source)},
        {"label": "Explicit -3 dB", "source_path": str(source), "gain_db": -3},
    ],
    start_frame=0,
    frames=len(samples),
)
restored = sf.read(folder / "trial" / "v01.wav", dtype="float64")[0]
assert np.array_equal(restored, samples)
```

The generated-fixture tests additionally cover stale identities, bounds, destination collisions, exact no-fade samples, separately mapped windows, rate/channel rejection, scoped feedback, changed dependencies, actual Ableton clip-envelope layout, unsupported tempo curves, XML declarations and the two-frame native-attachment limit. Real local comparison receipts and listener notes belong outside Git.
