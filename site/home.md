<div class="hero" markdown>
<div class="hero-copy" markdown>
<p class="eyebrow">The musical toolkit for agents</p>

# Composable tools for music

Pocket gives musicians and their AI agents a shared set of small, exact tools
for recordings, MIDI and arrangements. Each one does a single job, shows its
evidence and leaves your originals untouched, so you can compose them into
whatever your music needs.

[Run your first experiment](../docs/getting-started.md){: .primary-link }
[Learn the key ideas](../docs/concepts.md){: .secondary-link }

<p class="hero-meta">Python 3.11+ · Command line · MCP · Free and open source (AGPL) · Early development</p>
</div>
<div class="hero-art"><img src="assets/pip.svg" width="300" height="300" alt=""></div>
</div>

## Built to stay in the pocket

Musicians say a band is *in the pocket* when everyone is locked into the time
and every note sits where it should. Pocket brings that same care to the
details software usually glosses over.

<div class="principles" markdown>
<div class="principle" markdown>
**A recording is its bytes.**
Audio is identified by its exact content, not its title or filename, so one
master never quietly becomes another.
</div>
<div class="principle" markdown>
**Every clock has a name.**
Samples in a file, beats on a timeline, the pulse you tap and the bar one you
count are kept apart. Converting between them is always explicit.
</div>
<div class="principle" markdown>
**Evidence keeps its label.**
Measurements, a model's guesses, authored decisions and a person's listening
reports are separate records, and disagreements stay visible.
</div>
<div class="principle" markdown>
**Nothing is overwritten.**
Every edit is a new version with its lineage. Your recordings, projects and
earlier trials stay exactly as they were.
</div>
</div>

## Meet the toolkit

The names come from the pocket itself: it's sewn with thread and stitches, and
Pip, the field mouse who lives in it, peeks out and feels the way with whiskers.

<p class="group-label">Practice and listening · no DAW needed</p>

<div class="cards" markdown>
<div class="card" markdown>
[Context & practice](../docs/musical-context.md)
{: .card-title }

Mark passages and cues in a recording, render exact alternatives and compare them.
</div>
<div class="card" markdown>
[Practice review](../docs/practice-review.md)
{: .card-title }

Listen in your browser and save what you heard, pinned to the exact audio and moment.
</div>
<div class="card" markdown>
[Peek](../docs/peek.md)
{: .card-title }

Look inside a recording: where sounds start, which pulses fit, how the tempo drifts.
</div>
<div class="card" markdown>
[MIDI & sound](../docs/midi.md)
{: .card-title }

Create, edit, structure and export MIDI with explicit timing, locks and expression.
</div>
</div>

<p class="group-label">Ableton Live · optional adapters</p>

<div class="cards" markdown>
<div class="card" markdown>
[Thread](../docs/thread.md)
{: .card-title }

Follow the clips and sources through a saved set, down to the source sample.
</div>
<div class="card" markdown>
[Stitch](../docs/stitch.md)
{: .card-title }

Compare exact renders of a transition and pin notes to the span you heard.
</div>
<div class="card" markdown>
[Baste](../docs/baste.md)
{: .card-title }

Take a quick, read-only look at the session that's open right now.
</div>
<div class="card" markdown>
[Pipette](../docs/pipette.md)
{: .card-title }

Carry one kept trial into a new project without disturbing the original.
</div>
</div>

<p class="group-label">Records and sets</p>

<div class="cards" markdown>
<div class="card" markdown>
[Record bags](../docs/record-bag.md)
{: .card-title }

Gather a crate of records with honest, attributed notes about each one.
</div>
<div class="card" markdown>
[Weave](../docs/weave.md)
{: .card-title }

Sketch several possible orders for a set. Every route is a hypothesis.
</div>
<div class="card" markdown>
[Whisker](../docs/whisker.md)
{: .card-title }

Propose what could come next, in a session you and an agent share.
</div>
<div class="card" markdown>
[Workspace](../docs/workspace.md)
{: .card-title }

A small local page for bags, routes and next-record choices.
</div>
</div>

## One tool, three ways in

Every provider is available from Python, from the `pocket` command and to an AI
agent over [MCP](../docs/agents.md). All three call the same function, so the
answer never depends on the interface you use.

<div class="three-ways" markdown>
<div markdown>
**Python**

```python
from pocket_music import context_resolve

context_resolve(
    store_root=store, context=context,
    anchor_id="internal-cue",
    occurrence_id="repeat",
    target_clock_id="practice",
    target_space="arrangement_qn",
)
```
</div>
<div markdown>
**Command line**

```sh
pocket context-resolve \
  --spec resolve-spec.json
```

The spec file holds the same arguments as JSON.
</div>
<div markdown>
**Agent (MCP)**

```text
tool:  context_resolve
input: the same JSON arguments
```

Your agent calls it for you and shows you the receipt.
</div>
</div>

## Try it in a couple of minutes

```sh
git clone https://github.com/rpalermodrums/pocket.git && cd pocket
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e .
python examples/practice_context.py private/first-practice
pocket context-resolve --spec private/first-practice/resolve-spec.json
```

Pocket makes a short test recording, repeats two passages, renders an
alternative and tells you exactly where a cue lands on the second pass:
`{"n": 5, "d": 2}`, or 5/2 quarter notes, the "and" of beat three. You don't
need a DAW, a model or an account.

[Walk through your first experiment](../docs/getting-started.md) · [Connect an agent](../docs/agents.md) · [Browse the provider reference](../reference/index.md)

## Where it's heading

Pocket isn't exclusive to any one genre, instrument, or DAW. After
version 1, the aim is a responsive, living "band in a box": a practice partner
that follows the form, comps, trades and adjusts to you in real time. Today's
tools are the foundation for it: exact time, honest evidence and experiments you
can always undo.

[Status and direction](../docs/status.md) · [Changelog](../CHANGELOG.md) · [Contributing](../CONTRIBUTING.md) · [GitHub](https://github.com/rpalermodrums/pocket)
