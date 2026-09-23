# Two independent expression parts in a MIDI file

From the repository root, with Pocket and its optional `midi` extra installed:

```sh
python examples/midi-expression/example.py
```

The script creates a new temporary directory and prints its location with all artifact handles. It imports the independently authored `material.json`, publishes an expression plan, and exports it. A second export takes the external material and configuration directly; the script requires identical MIDI and sidecar handles. No generation, native session or workflow runner is needed.

This synthetic file example covers the encoding part of the "independent expressive movement" scenario in [what's supported](../../docs/midi-capabilities.md#five-worked-scenarios). Notes 60 and 67 start together and last eight quarter notes. Note 60 has pitch steps 0 → +30 → 0 cents at qn 0, 3 and 8, with pressure 0.15 → 0.40 → 0.15. Note 67 remains neutral. These are supplied steps, not a performed gesture or a smooth ramp. Both voices get explicit pitch/pressure/slide initialization. The declared lower zone uses manager channel 1 and members 2 and 3, with ±48-semitone member bend range. No receiver is configured.

The file retains release velocity 37. Each member stays reserved through a declared one-qn tail; reset occurs at qn 9 and the sidecar reports that one-qn extension. The exact +30-cent request encodes to unsigned bend 8243, yielding 3825/128 cents under the declared range, with error 15/128 cent. Pressure 0.15 and 0.40 encode to 19 and 51. Full error and allocation proofs remain in the plan artifact; the rich source curves remain unchanged.

The configuration is an attributed agent assumption for file verification. It establishes no installed instrument setting, audible independence, native save/reopen, recorded gesture, transport recovery, actual listening or musical approval. Keep the source and plan as masters. General lossless native SMF import remains experimentally unsupported; this example does not authorize that route.

The same functions are exposed by `pocket midi-expression-plan --spec <input.json>`, `pocket midi-export --spec <input.json>` and their matching MCP names. Use the full configuration fields in `configuration.json`; do not infer receiver settings from a plugin name.
