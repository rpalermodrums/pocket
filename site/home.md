# Music tools you can put to work

Pocket gives a musician and an agent small, composable tools: inspect a passage, place an internal cue, try an explicit edit and compare the exact result. Practice, composition, performance and DJ preparation all belong here.

[Try your first experiment](../docs/getting-started.md){: .primary-link }
[Explore the tools](../docs/README.md){: .secondary-link }

## Keep the music. Try the change.

Start with a generated recording, repeat a passage and retain a baseline and an alternative. It runs locally, without a DAW, model download or account.

```sh
python -m pip install -e .
python examples/practice_context.py private/first-practice
```

Run these from a checkout with a virtual environment. The [first experiment](../docs/getting-started.md) walks through setup and output. The example uses synthetic tones; it is a demonstration of exact comparison, not a claim about musical taste.

## Find a useful starting point

**A passage or practice loop.** Connect source frames, repeated occurrences and internal cues. Make a literal edit, compare variants and keep a report about the exact interval heard. [Musical context and practice](../docs/musical-context.md).

**Notes and musical material.** Inspect, generate and edit MIDI with explicit constraints. File export and native instruments have their own optional requirements. [MIDI and sound tools](../docs/midi.md).

**Records and arrangements.** Plan a route, choose what comes next, inspect saved Ableton sets or qualify a small transition experiment. [Selection](../docs/selection-interfaces.md) and [saved-set tools](../docs/thread.md).

## Small tools, shared by people and agents

Python, CLI and MCP call the same composable providers. Exact identities and explicit coordinates keep the agent's proposal connected to the material. Measurements, interpretations and a musician's listening report remain separate.

[Use Pocket with an agent](../skills/pocket/SKILL.md) or open the [installed contract guide](../docs/contracts.md). The generated reference in this site describes actual interfaces, including their limits.

## A bigger musical goal

Pocket is public, open source and early in development. It is not limited to electronic music, a genre or a DAW. After v1, the aim includes a responsive, living “band in a box” for jazz practice: following, comping, trading and learning to play together.

That future needs its own real-time and musical qualification. Today's tools give us a way to test smaller questions honestly. [See current status and direction](../docs/status.md).
