# Status and direction

Pocket is public, MIT-licensed and in early development. The package currently identifies as 0.4.0; [change history](../CHANGELOG.md) distinguishes development notes from published releases.

## Available now

The standalone context/practice path connects exact recording regions, explicit source and timeline clocks, repeated occurrences, internal cues, attributed interpretations, literal edits, exact audio comparisons and scoped reports. Explicit join envelopes have their own bounded processing profile.

A retained comparison can be reviewed in the loopback-only [practice review page](practice-review.md) (`pocket practice-review`). It plays declared PCM16 browser previews (`practice_preview`) and saves attributed reports that name the exact preview and interval reviewed (`pocket.practice-feedback/v2`). Verified preview bytes are the player's input, not proof of device output; only Chromium has been qualified for the preview format.

MIDI tools, recording analysis, record selection and a local selection workspace also exist. Saved/native Ableton workflows have separate setup and qualification requirements. Inspect current `capabilities_list` results and the [capability matrix](midi-capabilities.md); a named native tool is not a promise that your installation is qualified.

## Next practical step

Have a musician use the [practice review page](practice-review.md) on a small retained comparison and record attributed interval reports. No human listening has been performed with it yet: automated tests, including optional Chromium browser checks, cover the verified previews, saved reports and page behavior, not musical usefulness or what a device outputs. The installed guide and registry always take precedence over roadmap language.

## After v1

The broader goal is an agentic toolkit for music across acoustic/electronic instruments and genres. A responsive, living “band in a box” for jazz practice is one important direction: following, comping, trading and adjusting to the musician.

That work needs latency/recovery design, musical interaction and listening evaluation. Offline evidence, a synthetic test or a saved DAW project does not establish those abilities. There is no announced ship date, universal DAW support or automatic musical judgment.

## Evidence you can inspect

Public tests generate their own material. Provider input contracts come from the installed package and actual MCP discovery. Real recordings and personal acceptance notes stay private. A native observation, rendered audio, signal check and human listening report each answer a different question; none silently substitutes for another.
