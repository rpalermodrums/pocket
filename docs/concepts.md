# Key ideas

Pocket is careful about a handful of things that most music software leaves
unsaid: which recording you mean, which clock a position belongs to, and who
said what about the result. Once these ideas click, every other guide reads more
easily. This page explains them in plain terms and ends with a glossary.

## Pocket in one paragraph

Pocket is a library of small, single-purpose operations for musical work. Among
other things they can capture a passage of a recording, place a cue on a
timeline, render alternatives, edit MIDI, read a saved Ableton Live set and
record what a listener heard. Each operation is called a **provider**, and you
can call every provider three ways. Providers that make something, such as a
capture, a render or a report, write it as a new file. Read-only providers,
such as queries, `capabilities_list` and [Peek](peek.md), write nothing. Many
providers, including all of the practice tools, return a **receipt** describing
exactly what they did and did not do. Some older tools, Peek among them, return
their own documented report instead.

## One implementation, three ways in

| Interface | What it looks like | Good for |
|---|---|---|
| Python | `from pocket_music import context_resolve` | Scripts, notebooks and your own tools |
| Command line | `pocket context-resolve --spec arguments.json` | The terminal and shell scripts |
| MCP | the `context_resolve` tool | An AI agent working alongside you |

All three call the same Python function, so a result never depends on which door
you came in through. CLI command names use hyphens, and MCP tool names use
underscores. The CLI and MCP take the same JSON argument object. `capabilities_list`
(or `pocket capabilities-list`) reports what your installation can do right now.

**MCP**, the Model Context Protocol, is an open standard that lets AI assistants
call local tools. `pocket-mcp` runs on your own machine and talks to your agent
over standard input and output. Nothing is hosted, and no audio is uploaded. See
[Use Pocket with an agent](agents.md).

## Stores, artifacts and handles

Pocket writes what it makes into a folder you choose, called a **store**
(`store_root`). Inside it:

- An **artifact** is one saved thing: a musical context, a render, a comparison
  or a listening report. It lives under `artifacts/<sha256>/`, named by the hash
  of its own bytes, and it never changes after it is written.
- A **handle** is a small JSON pointer to one artifact. It holds the artifact's
  location in the store, its SHA-256 hash and its schema. You pass handles from
  one call to the next.

```json
{
  "schema": "pocket.artifact-handle/v1",
  "artifact_uri": "artifacts/4f78c0e9…/record.json",
  "sha256": "4f78c0e9…",
  "artifact_schema": "pocket.musical-context/v1"
}
```

- A **receipt** is what many calls return, whether or not they wrote
  anything. It holds a status, handles to anything
  new, `coverage` (what was and was not attempted), warnings and stated
  uncertainty. For example, `"human_listening": "not_performed"` means exactly
  that.

Every read checks the bytes against their hash. Moving a whole store to another
folder or machine keeps every reference valid.

## Nothing is overwritten

Pocket never edits your recordings, projects or earlier results in place.

- An edit creates a **new** artifact that remembers its parent (its
  **lineage**). The original stays exactly as it was.
- Commands that write folders require a **new** destination and refuse an
  existing one, so an earlier experiment is never clobbered.
- Operations that write into a store take a **request ID**. Repeat a call with
  the same ID and the same inputs, and you get the same verified receipt back
  (a **replay**), with nothing done twice. Reuse the ID with different inputs,
  and the call fails with `idempotency_conflict`. An interrupted request is left
  in place for you to inspect with `request_status`. It is never retried
  silently.

## A recording is its bytes

Pocket identifies audio by the SHA-256 hash of the file's exact bytes, not by
its title or filename. Renaming a file doesn't change its identity. Two masters
that share a title are two different recordings. A catalog entry, such as a
Spotify track URI, describes a release, not a particular audio file on your disk.

## Time has more than one clock

Musicians juggle several kinds of time at once, and Pocket names each of them
separately.

- **Source frames** are sample positions in the original recording file. At
  48 kHz, one second is 48,000 frames. Pocket counts from the start of the whole
  file, even when you captured only a short passage of it.
- A **timeline** measures arrangement position in quarter notes (**qn**) on a
  declared **tempo map**. At 120 BPM, one quarter note lasts half a second.
- An **occurrence** is one placement of a source passage on a timeline. A
  passage that plays twice has two occurrences, each with its own ID. That's why
  Pocket asks *which* occurrence you mean when you place a cue inside a repeated
  passage.
- An **anchor** (a cue) is a named point you author, such as "where the horn
  comes in". Each anchor has a kind, and the kinds stay distinct: an **onset**
  (where a sound starts), a **pulse** (the steady beat you would tap), **bar
  one** (the downbeat that starts a bar) and a **phrase start** (where a musical
  sentence begins). A loud accent might be a backbeat, and a phrase can start on
  a pickup. Pocket never assumes that any two of these coincide.

Positions are exact. Pocket writes fractions as `{"n": 5, "d": 2}`, meaning 5/2
or two and a half quarter notes, and never rounds them to decimals behind your
back. Frame intervals are *end-exclusive*: `[1000, 2000)` covers frames 1000
through 1999.

The [first experiment](getting-started.md) puts all of this in one place: a cue
at source frame 4000 lands at 5/2 quarter notes during a passage's second pass.

## Four kinds of evidence

| Kind | Example | Where it comes from |
|---|---|---|
| **Measurement** | A file hash, a sample peak, the attacks Pocket detected | Pocket, computed the same way every time |
| **Hypothesis** | Candidate pulse rates, a model's guess at the notes | An analyzer or model. It can be wrong, so its alternatives are kept |
| **Authored decision** | "This is bar one." "Keep variant B." | A person or an agent, named in the record |
| **Listening report** | "The fill lands late in bars 3–4." | Someone who listened to the exact audio and interval |

Every claim carries an **attribution**: the `actor`, their `actor_kind` (`human`
or `agent`), the `statement` and any `uncertainty`. These are stated claims, not
verified identities. Pocket always labels an agent's report as an agent report,
and it never presents one as human listening.

A successful render, a clean signal check or a confident score is never a
musical verdict. Pocket doesn't pick winners or average opinions, and when two
reports disagree, you see both.

## Profiles, limits and refusals

A **profile** is a named, versioned rulebook for one kind of processing, such
as `browser-pcm16-original-rate/v1`. It states exactly what an operation does
and, just as importantly, what it will not do.

When an input falls outside a profile, Pocket **refuses** and explains why.
It will not approximate. There is no silent resampling, normalization, fading,
time-stretching or rounding to make something "work". Every provider also has
explicit size limits, so a request can't grow without bound.

Schema names such as `pocket.practice-render/v1` identify a record's family and
version. When a format changes, it gets a new version, and old records keep
their original meaning.

A **qualified** profile has been tested against a specific real environment,
such as one browser or one Live version. That is a narrower promise than
*implemented*, and the guides say which one applies.

## Working with Ableton Live

Pocket's core never needs a DAW (digital audio workstation). A set of optional
adapters works with **Ableton Live 12**:

- [Thread](thread.md) reads saved `.als` set files.
- [Stitch](stitch.md) builds comparison copies of a transition.
- [Baste](baste.md) observes the open session through a Max for Live device.
- [Pipette](pipette.md) carries a kept trial into a new project.

**Native** means inside Live itself, as opposed to working with its files.
Native steps are **supervised**: a person opens, saves and renders in Live, and
Pocket checks the resulting files and records what was observed, with
attribution. A saved set describes intent, and a rendered file establishes what
audio came out. Neither one tells you how the audio sounds.

## Choosing records

- A **record bag** is a sealed crate of records with optional notes about each
  one, such as energy or vocal density, attributed to whoever wrote them.
- A **brief** states the setting, duration and intent for a set.
- A **route** is one proposed order through a bag for a brief. [Weave](weave.md)
  proposes several.
- A **session** is the shared, revision-checked state that [Whisker](whisker.md)
  uses to propose what could come next.

## The tool family

A pocket is sewn with thread and stitches, and Pip, the field mouse who lives in
this one, peeks out and feels the way with whiskers. The names follow from that.

| Tool | In a phrase |
|---|---|
| [Peek](peek.md) | Look inside a recording without deciding what it means |
| [Thread](thread.md) | Follow the clips and sources through a saved Live set |
| [Stitch](stitch.md) | Join a transition several ways and compare the seams |
| [Baste](baste.md) | A quick, temporary look at the open session, with nothing sewn in |
| [Pipette](pipette.md) | Transfer exactly the one kept trial, and nothing else |
| [Weave](weave.md) | Interlace a crate of records into possible sets |
| [Whisker](whisker.md) | Sense what could come next |

The standalone practice, review and MIDI tools are named for what they do. See
[all guides](README.md).

## Glossary

| Term | Meaning |
|---|---|
| A/B, baseline, variant | The unchanged version you compare against, and the alternatives compared with it |
| Anchor | A named, authored point in a recording, such as a cue |
| Artifact | One immutable record or file in a store, named by its hash |
| Attribution | Who made a claim, whether they are a person or an agent, and how sure they were |
| Bar one | The downbeat that begins a bar. It is distinct from the pulse and the phrase start |
| Capture | Copying an exact passage of a recording into a store, together with its original position |
| Comparison | A baseline, its variants and the question you want answered |
| Context | Pocket's record of which recordings, clocks, occurrences and anchors belong together |
| Coverage | The part of a receipt that says what was and was not attempted |
| DAW | Digital audio workstation, such as Ableton Live |
| Frame | One sample position, across all channels, in an audio file |
| Handle | A small JSON pointer to one artifact: its location, hash and schema |
| Join envelope | A declared linear fade at the joint between two rendered passages |
| MCP | Model Context Protocol, the standard Pocket uses to offer its tools to AI agents |
| Material | Pocket's own versioned format for MIDI notes and clips |
| No addition | The always-available choice to leave the music as it is |
| Occurrence | One placement of a source passage on a timeline |
| Operator | The person running Live during a supervised step. This is not Ableton's Operator instrument, which some recipes also use |
| Preview | A declared, browser-playable copy of a render |
| Profile | A named, versioned rulebook for one kind of processing |
| Provider | One Pocket operation, available through Python, the CLI and MCP |
| Pulse | The steady beat you would tap along to |
| qn | Quarter notes, the unit of timeline position |
| Receipt | What many calls return: status, any new handles, coverage, warnings and uncertainty |
| Record bag | A sealed crate of records used for planning sets |
| Region | A captured passage of a recording, kept with its original frame bounds |
| Render | Audio Pocket produced by playing chosen occurrences in order |
| Request ID | Your label for a write, which makes a retry safe |
| Route | One proposed order through a record bag |
| Schema | A record's family and version, such as `pocket.practice-render/v1` |
| SMF | Standard MIDI File |
| Store | The folder where Pocket keeps artifacts and request journals |
| Timeline | Arrangement time in quarter notes on a declared tempo map |
| Trial | A Stitch experiment: exact excerpts, their sources and any listening notes |
