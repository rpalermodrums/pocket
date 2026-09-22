# Optional local note hypotheses

This example uses an **existing** explicitly selected ONNX CPU environment and the supported Basic Pitch model. It installs nothing, downloads nothing, and never opens Live or writes MIDI. Run it from an environment with Pocket installed. Base audio/MIDI work does not require the optional model environment.

Supply absolute paths and independently checked lowercase SHA256 values. Keep the virtual-environment launcher path: resolving its symlink to the base interpreter can select a different environment.

```sh
python examples/audio-notes/example.py run \
  --executable "$MODEL_PYTHON" \
  --executable-sha256 "$MODEL_PYTHON_SHA256" \
  --weights "$MODEL_ONNX" \
  --weights-sha256 "$MODEL_ONNX_SHA256"
```

Here `MODEL_PYTHON` is your existing optional environment's Python executable and `MODEL_ONNX` is your existing `nmp.onnx`. The supported model SHA256 is `2c3c1d144bfa61ad236e92e169c13535c880469a12a047d4e73451f2c059a0ec`; a different digest is refused. The current adapter requires ONNX Runtime 1.26.0 and soxr 1.1.0, CPU only, one thread and sequential execution. The executable hash is machine-specific and must be checked locally. The example does not create or repair that environment.

The script creates a new temporary output directory that it keeps for inspection. It generates a two-second mono PCM16 WAV at 22050Hz with an explicitly gated 440Hz tone, calls `audio_note_model_inspect` with `synthetic_onnx_cpu_v1`, then passes the inspected model handle to the independent `audio_note_hypotheses` primitive. It uses the shared public query/correction APIs to retain an attributed synthetic alternative alongside the raw result. The original audio and every model window remain unchanged. The correction demonstrates supplied synthetic intent; it is not an audition or evidence of model accuracy.

Add `--worker` to the same command to submit the identical input through `audio_note_submit` and require exact direct/worker hypothesis-handle equality. The additional execution is opt-in. Every model child has a 60-second limit. The example's worker wait is separately limited to 120seconds; reaching that wait limit does not establish cancellation. Failures retain the directory and print a bounded diagnostic. Inspect existing evidence rather than automatically retrying or resubmitting.

The successful JSON output is bounded to 64KiB and includes `directory`, `manifest`, `source_unchanged`, optional `worker`, the public inspection/direct/correction receipts, and bounded original/corrected views. `example.json` retains the artifact handles and synthetic gate description; full raw arrays and proofs stay in `store/`.

## Read retained evidence without the optional environment

Use the manifest path printed by the first run:

```sh
python examples/audio-notes/example.py query \
  --manifest "$EXAMPLE_MANIFEST" --view history
```

This mode calls only artifact-based public queries. It does not inspect the original source path, launch the optional interpreter, or run the model. It still requires the base Pocket environment. After copying the complete `store/` artifact graph and `example.json` with ordinary independent copies, supply the relocated store explicitly:

```sh
python examples/audio-notes/example.py query \
  --manifest "$COPIED_MANIFEST" --store "$COPIED_STORE" --view annotations
```

The public `audio_hypothesis_correct` call shown in the script is also artifact-only. Its exact `expected_revision` protects against stale corrections; authored alternatives retain attribution, uncertainty, superseded rows and original evidence. No prior conversation or hidden workflow state is required.

## What the evidence means

The **public inspection qualification contains only two two-second mono 22050Hz silence/tone cases**, with full raw repeats checked for finite values and exact equality. It does not imply that a broader format, duration or musical-accuracy suite ran. Separate private prerequisite experiments informed the adapter; actual public-path format/rate/long-source gates remain separate evidence and must be completed before relying on that broader execution scope. A successful example establishes only the calls and synthetic input it actually executed.

The declared source profile accepts 2–20-second mono/stereo PCM16 or IEEE FLOAT32 WAV crops at 22050/44100/48000Hz, with an original-source limit of 256MiB. Arithmetic-mean downmix can cancel antiphase channels. No normalization or clipping is applied. Base artifact replay verifies source decoding, downmix and retained resampled bytes, and independently recomputes the entire note ledger; it does not rerun optional soxr resampling.

New analysis records retain the base NumPy/SciPy versions and versioned decoder/vendor source identities separately from the optional model runtime profile. Earlier v1 records without that field remain readable with decoder provenance explicitly unknown. Queries recompute the complete ledger in the current base environment; they do not claim cross-version numerical equivalence.

The decoder explicitly uses `infer_onsets=False`, `melodia_trick=False`, thresholds 0.5/0.3, energy tolerance 11 and a note length strictly greater than 11 model frames. Its 142-frame retained-window seams differ from the vendor clock's 172-frame empirical correction. Vendor timestamp estimates, float-origin rational coordinates, outward-rounded source envelopes and excluded boundary events are retained separately; they are not exact acoustic onset measurements.

Pitch bins do not establish performed tuning, voices, instrument identity or harmony. Model amplitude is uncalibrated energy, not confidence or MIDI velocity. Retained raw contour activations are not qualified pitch-bend/MPE expression. Nothing here establishes human listening, musical approval, native receiver behavior or performance readiness. Keeping the original music unchanged remains a valid outcome.
