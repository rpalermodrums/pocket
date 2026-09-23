# Your first experiment

Start with a small generated recording. Pocket will repeat two passages, keep the baseline, make an alternative and resolve an internal cue. This establishes how the tools fit together; it does not judge whether music sounds good.

## Install from source

You need Python 3.11 or later. Clone [the repository](https://github.com/rpalermodrums/pocket), open its directory and run:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

On Windows, activate with `.venv\Scripts\activate` instead. Core file tools do not need Ableton. Some worker/native profiles require POSIX or a qualified host; see the particular guide before using them. Audio codec support follows the installed libsndfile build; decoded WAV/FLAC are preferred for exact comparisons.

## Create and query a comparison

```sh
python examples/practice_context.py private/first-practice
pocket context-resolve --spec private/first-practice/resolve-spec.json
```

The destination must not already exist. Use a new name to repeat the example.

The first command prints `results`, `baseline_audio`, `alternative_audio` and `listening: not_performed`. The WAVs contain synthetic tones. `results.json` retains the exact context, resolved anchor, two renders and verified comparison. The second command resolves the same cue through the CLI; its receipt matches the public Python provider.

A repeated passage needs an occurrence ID: the same source frame can appear at several places in an arrangement. An internal cue is not necessarily the start of a clip. Those distinctions matter before any musical edit.

## Work with an agent

Install the optional interface:

```sh
python -m pip install -e '.[agent]'
pocket-mcp
```

The server speaks MCP over standard input/output; it is not a web page. Configure your MCP-capable agent to launch the executable in your virtual environment. Use an absolute path appropriate to your own checkout:

```json
{
  "mcpServers": {
    "pocket": {
      "command": "/path/to/pocket/.venv/bin/pocket-mcp"
    }
  }
}
```

Read [the Pocket skill](../skills/pocket/SKILL.md) and [installed contracts](contracts.md). The process runs with your account's file access. Online acquisition/catalog calls and model setup are separate explicit actions.

## Choose the next tool

- [Musical context and practice](musical-context.md): source clocks, interpretations, literal edits, comparison and feedback.
- [Practice review](practice-review.md): hear a retained comparison through declared browser previews and save attributed interval reports.
- [Peek](peek.md): inspect a passage's timing and tonal evidence without declaring musical beat one.
- [MIDI](midi.md): material creation, editing and optional MIDI file output.
- [Record selection](selection-interfaces.md): routes and next-record choices.

When you try your own music, keep it under ignored `private/`, preserve the original and choose one small question. Record human listening only after a person actually listens to the exact audio and interval. A technical report is useful, but it is a different kind of evidence.

## If a command refuses

An existing destination means an earlier experiment is being preserved: choose another folder. Missing optional MIDI file support needs `python -m pip install -e '.[midi]'`. A renderer refusing mismatched rate, gaps or time stretching is respecting its profile; do not normalize or move material silently to force success. [Errors and recovery](contracts.md) explain exact identities and retry behavior.
