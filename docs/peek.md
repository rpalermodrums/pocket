# Peek

> **In brief.** Peek looks inside a stretch of a recording and reports what it
> can measure: where sounds start, which pulse rates fit, how the local tempo
> drifts, which pitch classes ring out and which textures seem to repeat. It
> never edits audio, and it never decides where beat one is. That's your call.
>
> **Reach for it when** you want evidence about a passage's timing or tonal
> content before making a musical decision. **You need** a WAV or FLAC file
> and the stretch you care about.

Peek describes a bounded piece of an audio source. It returns exact file identity and source coordinates, detected attacks, pulse-rate alternatives, local tempo evidence, local pitch-class features, and possible repeated textures. It does not edit audio, produce a warp map, identify a song's key, or certify musical beat one.

The distinction matters: two sources can both have a stable 120 BPM grid and still have an incorrect musical alignment. A strong accent can be a backbeat or a syncopation. Consistent results from one analysis method do not resolve that ambiguity.

MCP `peek` and CLI `pocket peek` call the Python provider below with the same analysis contract.

## Python API

```python
from pocket_music.peek import analyze_region

report = analyze_region(
    "source.wav",
    start_seconds=40,
    duration_seconds=30,
    bpm_hint=120,
    beats_per_bar=4,
)
```

The function returns a JSON-serializable dictionary with schema `pocket.track-map/v1`. It writes no files, downloads no models, and uses only NumPy, SciPy, and soundfile. Runtime reports can include the source's local path; keep reports and recordings out of the repository.

| Input | Meaning |
| --- | --- |
| `path` | Existing audio readable by soundfile. Identity uses the complete file's SHA-256 through `assets.identify_audio`, rather than a title or a crop hash. |
| `start_seconds` | Finite original-source time, at or after zero; default `0`. |
| `duration_seconds` | Finite positive duration, at most 600 seconds; default `30`. The requested end must be within the source. |
| `bpm_hint` | Optional counting preference, 20–400 BPM. It can select between measured alternatives; a hint alone cannot supply periodicity evidence. |
| `beats_per_bar` | Integer 2–12; default `4`. This defines competing bar hypotheses, not a detected time signature. |
| `start_frame`, `frames` | Optional keyword-only pair of strict integers. Both are required together; booleans are rejected. These bypass seconds rounding and require the default `start_seconds=0`, `duration_seconds=30`. Bounds and the 600-second maximum still apply. |

Invalid values, out-of-bounds crops, nonfinite samples, changed files, and unreadable media raise `PocketError`. A crop that is too short for rhythm inference still returns source and signal evidence. The analyzer never silently extends a request to obtain more context.

## Coordinates and identity

`region.start_frame` and `region.end_frame_exclusive` identify complete frames from the original source. Fractional-frame requests round inward. A one-ULP arithmetic allowance preserves decimal representations of exact sample boundaries. The source sample rate remains the coordinate reference even when spectral analysis uses a lower rate.

Every onset includes `source_frame`, `source_seconds`, and `region_seconds`. A frame coordinate is exact bookkeeping; the detected attack itself has finite resolution. The default spectral hop is 10 ms, followed by a bounded original-waveform envelope search. These are acoustic attack candidates, not guaranteed kick starts or musical downbeats. Attacks at crop boundaries can be missed.

Source identity is read once through the shared asset provider. Device, inode, size, and modification time are checked across identification and analysis. This catches ordinary concurrent changes; it is not an adversarial file-integrity protocol. `peak_dbfs` is a sample peak, not an oversampled true peak or a loudness measurement.

For a source interval supplied by Thread, retain its integer frames directly:

```python
report = analyze_region("source.wav", start_frame=11515, frames=44107, bpm_hint=120)
```

`region.addressing` is `source_frames` and `rounding` is `none_explicit_source_frames` in this case. The seconds fields describe those exact frames; no conversion to seconds and back selects the audio. With seconds inputs, `region.addressing` is `source_seconds` and the existing inward rounding applies.

## Report fields

| Field | Evidence and interpretation |
| --- | --- |
| `asset`, `region` | Complete source identity and exact analyzed bounds. |
| `signal` | Channel-energy RMS, sample peak, near-silence flag, and number of samples at or above unity. A zero-energy dB value is `null`, never JSON infinity. |
| `onsets.events` | Positive spectral-flux attacks, original-waveform timing refinement, and separately normalized low/mid/high spectral-flux strengths. |
| `rhythm.selected_pulse_grid` | A fitted acoustic lattice, with origin, rate, inlier fraction, occupancy, and residuals. Its origin has no asserted musical role. It may be `null`. |
| `rhythm.tempo_candidates` | Competing measured fits ranked by onset regularity. Scores are algorithmic support, not calibrated probabilities. |
| `rhythm.counting_alternatives` | Half-rate, selected-rate, and double-rate interpretations. These remain possible counting conventions, not three independently verified tempos. |
| `rhythm.local_windows` | Eight-second fits at four-second steps, with original-source bounds and origins. Short crops use their available duration. |
| `rhythm.local_counting_ambiguities` | Windows whose dominant rate resembles half/double the reference. They are kept explicit instead of being silently folded or called tempo changes. |
| `rhythm.drift` | Local BPM range and linear trend when at least three sufficiently supported windows use comparable counting. Its explicit scope is pulse rate only: a stable BPM does not establish phase or count continuity. |
| `rhythm.acoustic_phase_candidates` | Persistent circular phase modes from independent low/body/high attack peaks at the selected rate. Each carries a band, source origin and nearest source frame, inlier/occupancy evidence and local support. They can be fractions of a pulse apart; these are not bar rotations. |
| `rhythm.crop_stability` | Two independently refitted overlapping inward crops, with exact source bounds, rates and phase differences at their common midpoint. Distinguishes phase sensitivity, counting sensitivity, similar phase at tested midpoints and insufficient evidence. It does not test boundary drift. |
| `rhythm.phase_count_continuity` | Band-resolved sliding-window phase modes, sustained displacement intervals, scattered evidence and explicit uncertainty. Stable attack layers can coexist with competing syncopations. Integer musical pulse count remains unverified. |
| `rhythm.bar_interpretations` | Every bar rotation **of the selected grid**, with full-band and band-specific accent shares. This does not enumerate all acoustic-phase/bar combinations. `bar_phase_status` always remains `unresolved`. |
| `harmony.windows` | Four-second pitch-class energy profiles and prominent classes, or explicit abstention. Each window also carries flatness, entropy, and level evidence. |
| `repetitions.candidates` | Similar ordered four-second band-energy fingerprints. Similar timbre can produce matches; these are not confirmed repeated phrases. |
| `provenance`, `limitations` | Algorithm version, library versions, analysis parameters, absence of learned models/human feedback, and scope limits. |

## How to use the evidence

Inspect local windows before relying on a crop-wide grid. `local_grids_only` means some short windows support a pulse lattice while the entire crop does not. Large variation may represent real tempo drift, a changed rhythmic pattern, or an analysis failure. Half/double switches are explicitly separated from the drift calculation.

Always inspect `phase_count_continuity` and `crop_stability` alongside `drift`. A short acceleration or a displaced rhythmic layer can leave a nearly identical average BPM before and after the event. The analyzer does not silently use that average to assert a count through the event.

The phase pass detects attacks independently in 35–180 Hz, 180–1500 Hz and 1500–5500 Hz bands on a nominal 2 ms spectral clock. Circular phase modes need at least five attacks and measurable concentration/coverage. Eight-pulse local windows, bounded to 4–8 seconds, advance by a quarter-window. A phase-change interval requires three supported windows on each flank, concentrated flanks and at least 0.18 pulse displacement. Full parameters are in the report. Overlapping detections retain the union of their evidence windows; those bounds are not exact musical event boundaries. A source-frame address is bookkeeping for a predicted phase or window, not sample-exact proof of an attack.

These tests can distinguish:

- **`locally_stable_acoustic_phase`:** at least two attack bands have sustained local phase support. This is positive acoustic evidence, limited to the analyzed crop and counting rate.
- **`competing_acoustic_phases`:** persistent attack layers are separated by at least 0.18 pulse. A half-pulse-separated body/high pattern can make a single strongest-grid choice sensitive to the crop.
- **`phase_or_count_continuity_unresolved`:** a band has displaced sustained flanks. This could be a timing change, syncopation, changed instrumentation or detector error. The tool exposes the affected band/window and modulo-phase displacement; it does not claim an exact corrected count or a new warp anchor.
- **`insufficient_evidence`:** no supported whole-crop rate, insufficient duration, or too little sustained band agreement. Natural/free timing is not forced onto a grid merely because a hint was provided.

The crop check re-runs onset detection and fitting after moving both boundaries inward by 125 ms and 375 ms. Reusing the original onset list would miss STFT-origin sensitivity. It reads no audio outside the original request and reuses the complete-file identity. Crops shorter than eight seconds abstain. `similar_phase_at_tested_midpoints` means only that the fitted phases agree at the shared source midpoints. `comparison_scope` explicitly states that boundary drift is not tested: for example, 120 and 121 BPM clocks can agree at the midpoint of a 60-second interval yet disagree by half a pulse at its edges. The result does not imply stability across either crop, solve half/double counting, or establish a downbeat. An onset-band clock may be stable at a subdivision rate while the half-rate interpretation has competing phases.

All support scores and thresholds are heuristic, not calibrated probabilities. The attack bands are different observations from one deterministic method, not independent learned models. A sustained instrument shift can trigger a warning without the musical clock changing, while an integer number of missing pulses can be invisible to modulo-phase analysis. `integer_pulse_count` remains `null` and `automatic_edit_authorized` remains `false` even for stable controls.

Retain all bar hypotheses for subsequent musical review. Accent shares alone cannot establish beat one, phrase length, an entry role, or the relationship between two records. A later tool can compare hypotheses and produce controlled listening alternatives; Peek does not turn its scores into an automatic clip shift.

Harmonic evidence uses local spectral peaks near A440 equal-tempered pitch bins. It includes overtones and mixed instruments. Broadband noise, silence, or diffuse pitch-class energy cause abstention. A tonal mixture can also cause abstention, and a percussive resonance can look tonal. There is no global key, chord label, note transcription, pitch-correction recommendation, or assertion of a harmonic clash.

An exact crop can end a fraction of one downsampled analysis frame after a four-second window boundary. That tiny trailing source interval is retained as `insufficient_tonal_evidence`, with reason `fewer_than_one_complete_analysis_frame` and null level/pitch features. It is not analyzed using adjacent padded spectral data, and the requested source-frame bounds remain unchanged.

The processing averages channel power rather than summing channel samples, so anti-phase stereo does not disappear from the analysis. Onset bands are independently normalized within the crop; their values are not mix-level ratios or bass loudness measurements.

## Validation

Run generated-fixture checks with:

```sh
python -m pytest tests/test_track_map.py tests/test_rhythm_continuity.py
```

The generated fixtures cover original-source offsets and file identity, strict seconds/frame bounds, stable click tempo with and without a hint, unresolved half/double and bar interpretations, accelerating clicks, silence, broadband-noise tonal abstention, a known local pitch class, anti-phase stereo, nonfinite input, and source-coordinate preservation for repeated textures. Additional fixtures exercise persistent half-pulse-separated attack layers under shifted crops, a brief phase displacement against a steady periodic bed, stable multiband controls, and an irregular sparse passage. Exact frame addressing is tested at a sample rate whose frame boundaries are fractional seconds.

The same checks were also run on real recordings, including a crop-sensitive percussion passage, a brief count event, steady openings, freely timed playing and passages the analyzer had never seen. On those recordings Peek exposes competing half-pulse layers and sensitivity to the crop. A passage can still select about 246 BPM despite a 123 BPM hint, but the report now shows a localized band-phase or count uncertainty rather than letting stable rate evidence stand alone. A tight crop around the event abstains. These are better diagnostics. They are not solved downbeats or corrected pulse counts. No recordings or models are committed to the repository.
