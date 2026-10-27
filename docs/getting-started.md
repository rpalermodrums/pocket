# Your first experiment

In a few minutes you'll install Pocket, have it make a short test recording, and
watch it do three things: repeat two passages, render an alternative, and find
exactly where a cue lands in time. You don't need a DAW, a model, an account or
any music of your own.

This experiment shows how the tools fit together. The tones are test signals, so
it says nothing about whether any music sounds good.

## What you need

- Python 3.11 or later
- Git, to clone the repository

## 1. Install

```sh
git clone https://github.com/rpalermodrums/pocket.git
cd pocket
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

On Windows, activate the environment with `.venv\Scripts\activate` instead.
Check the install with `pocket --version`.

## 2. Make a practice comparison

```sh
python examples/practice_context.py private/first-practice
```

The `private/` folder is ignored by Git, so anything you make there stays on
your machine. The destination must be new. To run the experiment again, choose
another name, such as `private/first-practice-2`.

The command prints four lines:

```json
{
  "results": ".../private/first-practice/results.json",
  "baseline_audio": ".../store/artifacts/83af…/practice.wav",
  "alternative_audio": ".../store/artifacts/a08d…/practice.wav",
  "listening": "not_performed"
}
```

### What just happened

1. **A test recording.** Pocket wrote three seconds of a steady A (220 Hz).
   For one second in the middle, an E a fifth above joins in.
2. **An exact capture.** It copied a passage of that recording into a new
   *store* (`private/first-practice/store`) and remembered where the passage
   came from, down to the sample.
3. **A timeline and a cue.** It declared a 120 BPM practice timeline and named
   an internal cue, `internal-cue`, at sample 4000 of the recording.
4. **Two renders.** The *baseline* plays the plain passage twice. The
   *alternative* plays the passage with the added fifth twice. Both are exact:
   no fades, no resampling, nothing hidden.
5. **A comparison.** It bundled the two renders with a question ("Which passage
   should the practice loop use?") and verified everything it had written.

`results.json` holds every receipt. The last line, `listening: not_performed`,
is Pocket being honest: nobody has listened yet, so it makes no claim about how
the renders sound.

## 3. Ask where the cue lands

The first command also saved the arguments for a follow-up question: *during
the repeat, where does the cue fall on the practice timeline?*

```sh
pocket context-resolve --spec private/first-practice/resolve-spec.json
```

The end of the receipt answers it:

```json
"input":  { "clock_id": "recording", "space": "source_frame",   "value": 4000 },
"output": { "clock_id": "practice",  "space": "arrangement_qn", "value": { "n": 5, "d": 2 } }
```

Sample 4000 of the recording lands at **5/2 quarter notes**, the "and" of beat
three, on the second pass. Pocket writes positions as exact fractions, so
nothing is rounded along the way.

Why ask about *the repeat*? The same stretch of recording plays twice, so the
cue happens twice. Pocket calls each placement an **occurrence**. If you ask
about the cue without naming one, Pocket refuses and tells you the question is
ambiguous. It won't guess the first one. [Key ideas](concepts.md#time-has-more-than-one-clock)
explains this and the other distinctions Pocket keeps.

The CLI command calls the same function you would call from Python or through
an agent:

```python
from pocket_music import context_resolve
```

## 4. Listen, if you like

You can play the two WAV files in any audio player. To try Pocket's own
listening page, which saves a report pinned to the exact audio and moment you
heard, follow the [practice review guide](practice-review.md#start-it). It has a
ready-made command for this experiment. The page runs on macOS and Linux.

## Where to go next

- [Key ideas](concepts.md): stores, receipts, clocks and the kinds of evidence,
  in plain terms.
- [Use Pocket with an agent](agents.md): connect an AI assistant over MCP.
- [Musical context and practice](musical-context.md): everything this experiment
  used, in depth.
- [Peek](peek.md): inspect timing and tonal evidence in a real recording.
- [MIDI and sound tools](midi.md): create, edit and export MIDI material.
- [All guides](README.md).

When you move on to your own music, keep it under `private/`, keep the original
safe, and start with one small question. Record a listening report only after a
person has actually listened to the exact audio and interval. A technical check
is useful evidence, but it's a different kind of evidence.

## If a command refuses

Pocket refuses rather than guessing. The message tells you why.

| Message mentions | Why | Fix |
|---|---|---|
| destination exists | Pocket won't overwrite an earlier experiment | Choose a new folder name |
| MIDI support is missing | MIDI file import and export are optional | `python -m pip install -e '.[midi]'` |
| rate, gaps or stretching | A renderer only does what its profile declares | Keep the inputs within that profile. Don't normalize or move material to force it |
| ambiguous | A cue sits inside a passage that plays more than once | Name the occurrence you mean |

[Contracts and errors](contracts.md) covers error codes and safe retries. Audio
format support comes from the installed libsndfile. WAV and FLAC decode exactly,
so prefer them for comparisons. Some optional workers and the Live adapters need
macOS or Linux, or a particular host. Each guide says so up front.
