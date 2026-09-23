# Status and direction

Pocket is open source (MIT), public and in early development. The package
version is 0.4.0. The [changelog](../CHANGELOG.md) separates development
milestones from published releases. Pocket hasn't been published to a package
index yet, so install it from a checkout, as described in
[your first experiment](getting-started.md). Its distribution name is
`pocket-music`. The package called `pocket` on PyPI is an unrelated project.

## Available now

**Practice and listening, with no DAW.** The standalone path connects exact
recording passages, named source and timeline clocks, repeated occurrences,
internal cues, attributed interpretations, literal edits, exact audio
comparisons and scoped listening reports. Declared join envelopes add a narrow,
explicit fade at passage joints. See [musical context and practice](musical-context.md).

**A listening page.** `pocket practice-review` serves a local page that plays a
comparison through declared browser previews and saves attributed reports naming
the exact preview and interval heard. Verified preview bytes are the player's
input. They don't prove what a device played. So far, Chromium is the only
browser qualified for the preview format. See [practice review](practice-review.md).

**Recordings, MIDI and sets.** [Peek](peek.md) inspects recordings. The
[MIDI and sound tools](midi.md) create, edit and export material. The
[selection tools](selection-interfaces.md) plan and play sets of records, and
they include a local workspace page.

**Ableton Live, narrowly.** [Thread](thread.md), [Stitch](stitch.md),
[Baste](baste.md) and [Pipette](pipette.md) work with Ableton Live 12. Each
native workflow has its own setup and a deliberately narrow, tested profile. A
tool's name in the list is not a promise that your installation is qualified.
Check `capabilities_list` and [what's supported](midi-capabilities.md).

## Next

The next step is for a musician to use the [practice review page](practice-review.md)
on a small real comparison and record attributed reports. Nobody has done this
yet. The automated tests, including optional Chromium browser checks, cover the
previews, saved reports and page behavior. They say nothing about musical
usefulness or what a speaker plays.

The installed provider reference and `capabilities_list` always take precedence
over roadmap language.

## Where it's heading

Pocket isn't exclusive to any one genre, instrument, or DAW. It's a toolkit for
music in general, spanning practice, composition and performance.

After version 1, the aim is a responsive, living "band in a box": a practice
partner that follows the form, comps, trades and adjusts to the musician in real
time. Jazz is an important proving ground.

That work needs real-time design, recovery when things go wrong, genuine musical
interaction and careful listening evaluation. Offline evidence, synthetic tests
and saved projects can't establish those abilities. There's no announced date,
no promise of universal DAW support and no plan for automatic musical judgment.

## Evidence you can inspect

Public tests generate their own audio. Provider contracts come from the
installed package and live MCP discovery. Real recordings and listening notes
stay on the machines of the people who made them. A native observation, a
rendered file, a signal check and a human listening report each answer a
different question, and none of them stands in for another.
