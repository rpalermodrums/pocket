# Track Map

Track Map describes a bounded piece of an audio source. It returns exact file identity and source coordinates, detected attacks, pulse-rate alternatives, local tempo evidence, local pitch-class features, and possible repeated textures. It does not edit audio, produce a warp map, identify a song's key, or certify musical beat one.

The distinction matters: two sources can both have a stable 120 BPM grid and still have an incorrect musical alignment. A strong accent can be a backbeat or a syncopation. Consistent results from one analysis method do not resolve that ambiguity.

## Python API

```python
from pocket_music.track_map import analyze_region

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

Invalid values, out-of-bounds crops, nonfinite samples, changed files, and unreadable media raise `PocketError`. A crop that is too short for rhythm inference still returns source and signal evidence. The analyzer never silently extends a request to obtain more context.

## Coordinates and identity

`region.start_frame` and `region.end_frame_exclusive` identify complete frames from the original source. Fractional-frame requests round inward. A one-ULP arithmetic allowance preserves decimal representations of exact sample boundaries. The source sample rate remains the coordinate reference even when spectral analysis uses a lower rate.

Every onset includes `source_frame`, `source_seconds`, and `region_seconds`. A frame coordinate is exact bookkeeping; the detected attack itself has finite resolution. The default spectral hop is 10 ms, followed by a bounded original-waveform envelope search. These are acoustic attack candidates, not guaranteed kick starts or musical downbeats. Attacks at crop boundaries can be missed.

Source identity is read once through the shared asset provider. Device, inode, size, and modification time are checked across identification and analysis. This catches ordinary concurrent changes; it is not an adversarial file-integrity protocol. `peak_dbfs` is a sample peak, not an oversampled true peak or a loudness measurement.

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
| `rhythm.drift` | Local BPM range and linear trend when at least three sufficiently supported windows use comparable counting. No warp is authorized, even when the clock appears stable. |
| `rhythm.bar_interpretations` | Every candidate bar rotation, with full-band and band-specific accent shares. `bar_phase_status` always remains `unresolved`. |
| `harmony.windows` | Four-second pitch-class energy profiles and prominent classes, or explicit abstention. Each window also carries flatness, entropy, and level evidence. |
| `repetitions.candidates` | Similar ordered four-second band-energy fingerprints. Similar timbre can produce matches; these are not confirmed repeated phrases. |
| `provenance`, `limitations` | Algorithm version, library versions, analysis parameters, absence of learned models/human feedback, and scope limits. |

## How to use the evidence

Inspect local windows before relying on a crop-wide grid. `local_grids_only` means some short windows support a pulse lattice while the entire crop does not. Large variation may represent real tempo drift, a changed rhythmic pattern, or an analysis failure. Half/double switches are explicitly separated from the drift calculation.

Retain all bar hypotheses for subsequent musical review. Accent shares alone cannot establish beat one, phrase length, an entry role, or the relationship between two records. A later tool can compare hypotheses and produce controlled listening alternatives; Track Map does not turn its scores into an automatic clip shift.

Harmonic evidence uses local spectral peaks near A440 equal-tempered pitch bins. It includes overtones and mixed instruments. Broadband noise, silence, or diffuse pitch-class energy cause abstention. A tonal mixture can also cause abstention, and a percussive resonance can look tonal. There is no global key, chord label, note transcription, pitch-correction recommendation, or assertion of a harmonic clash.

The processing averages channel power rather than summing channel samples, so anti-phase stereo does not disappear from the analysis. Onset bands are independently normalized within the crop; their values are not mix-level ratios or bass loudness measurements.

## Validation

Run generated-fixture checks with:

```sh
python -m pytest tests/test_track_map.py
```

The fixtures cover original-source offsets and file identity, strict crop bounds, stable click tempo with and without a hint, unresolved half/double and bar interpretations, accelerating clicks, silence, broadband-noise tonal abstention, a known local pitch class, anti-phase stereo, nonfinite input, and source-coordinate preservation for repeated textures.

Bounded development checks on three existing 30-second music passages recovered pulse rates near 120 BPM in two passages. Another favored approximately 246 BPM, retaining approximately 123 BPM as a half-rate counting alternative despite the supplied 123 BPM hint. Musical bar position remained unresolved throughout. Some local windows also favored a doubled pulse rate; that is exposed as ambiguity. These checks establish that the tool runs on real media, not that it understands the recordings or solves their previously disputed alignments. Source-specific reports and recordings remain outside this repository.
