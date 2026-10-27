# Pocket additional permissions

> **Pending legal review.** These permissions are in effect for every copy of
> Pocket that includes this file. An attorney hasn't reviewed their wording yet,
> and a later version may clarify it. A copy of Pocket you've already received
> keeps the permissions it came with.

These are additional permissions under section 7 of the GNU Affero General
Public License, version 3 (the "License"), granted by Ryan Palermo, the
copyright holder of Pocket. They apply to Pocket's own material. They don't
change the license of the third-party material listed in [NOTICE](NOTICE).

As section 7 of the License allows, if you modify Pocket you may remove these
permissions from your copy. [Licensing](LICENSING.md) explains in plain language
what they mean for musicians; this file is the version that counts.

## Definitions

"Output" means files produced by running Pocket, such as audio, MIDI, analysis
results, reports, receipts, manifests, lineage records and Live Sets.

"Pocket Device Files" means the Max for Live device sources in
`src/pocket_music/devices/` and the files Pocket's device builders produce from
them, such as `Baste.amxd`, `Native MIDI.amxd` and `Native MIDI Writer.amxd`
together with their `.maxpat`, `.js` and `manifest.json` files.

"Musical Project" means a collection of material made for creating, performing,
practicing, recording or sharing music, such as an Ableton Live Set and its
project folder, or a trial, candidate or promoted project made with Pocket. A
software product is not a Musical Project.

## 1. Output

The License covers Pocket itself, not what you make with it. To the extent that
Output contains material from Pocket, such as fixed text, schema identifiers or
settings that Pocket writes into receipts, reports, MIDI files or Live Sets, you
may convey that Output under terms of your choice, without the conditions of the
License.

This permission doesn't extend to Pocket Device Files, which section 2 covers,
or to any other part of Pocket's source code.

## 2. Unmodified device files in a Musical Project

You may convey unmodified Pocket Device Files as part of a Musical Project under
terms of your choice, without the conditions of the License. This applies
whether the files were placed in the project by Pocket (for example by its
device builders, Stitch or Pipette), by Ableton Live (for example by Collect All
and Save, or by freezing a device), or by you.

This permission doesn't apply to modified Pocket Device Files, or to Pocket
Device Files conveyed on their own or as part of a software product. Those
remain under the License.

## 3. Ableton Live and Max

If you modify Pocket, or any covered work, by linking or combining it with
Ableton Live, Max, Max for Live or Node for Max (or a modified version of those
programs), including their libraries such as the Live API and `max-api`,
containing parts covered by the terms of their own licenses, the licensors of
Pocket grant you additional permission to convey the resulting work.

## Notes for legal review

This section is not part of the permissions.

- Section 3 adapts the Free Software Foundation's template for a GPLv3
  section 7 linking exception to name Pocket's host programs. Confirm it's
  needed and sufficient for devices that run inside a proprietary host.
- Sections 1 and 2 are new wording in the spirit of the Bison exception. Confirm
  that "Musical Project" and "unmodified" are tight enough that a competitor
  can't use section 2 to ship modified or standalone devices in a closed
  product, and loose enough that musicians sharing Live Sets never need to think
  about the License.
- Confirm the effect of publishing these permissions before review, and how a
  later revision applies to copies already distributed.
- Package metadata declares `AGPL-3.0-only AND Apache-2.0`, because Python
  packaging tools don't accept a custom SPDX exception identifier. Confirm that
  this is acceptable.
