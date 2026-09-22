# Optional local learned-pulse example

This opt-in example uses an existing, explicitly selected BeatThis CPU environment and local checkpoint. Pocket does not install packages or download weights. The base audio and MIDI tools do not need this environment.

Run from an installed Pocket environment. Supply the absolute interpreter/checkpoint paths and their independently checked SHA256 values:

```sh
python examples/audio-pulse/example.py --executable "$MODEL_PYTHON" --executable-sha256 "$MODEL_PYTHON_SHA256" --weights "$MODEL_WEIGHTS" --weights-sha256 "$MODEL_WEIGHTS_SHA256"
```

The example creates a new disposable directory and a synthetic four-second tone, runs the explicit seven-case qualification, then analyzes the tone directly and through an owned worker. It asserts identical evidence and unchanged source bytes, and prints bounded retained annotations. Each model child has a 60-second limit; the example's worker wait is separately bounded. If the wait ends, inspect the retained job instead of assuming cancellation or resubmitting.

The first profile is `beat_this_cpu_v1/synthetic_cpu_v1`: CPU float32, one thread, minimal postprocessor, arithmetic-mean downmix, soxr HQ and seed zero. Audio must be mono/stereo PCM16 WAV at 8000, 44100 or 48000 Hz, with a 2–20-second crop. The entire original source currently has a 256 MiB limit. An inspected model handle or inline declaration can be supplied to the independent public analysis call; no workflow history is required.

False boundary pulses have occurred on synthetic tones. Raw arrays, fractional coordinates, original downbeat peak support, excluded endpoints, repeat checks and source/model/runtime identities remain in immutable artifacts. Qualification establishes execution and provenance within this profile; it does not establish beat accuracy, transcription, human listening or musical usefulness. The shared query and correction tools work from retained evidence without the optional runtime.
