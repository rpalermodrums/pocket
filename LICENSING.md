# Licensing

Pocket is free and open source. Anyone can use it at no cost, including for paid
gigs, releases, teaching and commercial work, and the music you make with it is
yours. Pocket's code is licensed under the
[GNU Affero General Public License, version 3](LICENSE) (AGPL-3.0-only), a
license approved by the Open Source Initiative. If you want to build a closed
product on Pocket, a commercial license is available.

This page explains what that means in everyday terms. It isn't legal advice.
The [license text](LICENSE) and the
[additional permissions](LICENSE-EXCEPTION.md) are what count.

## Using Pocket is free

You don't need to ask anyone or pay anything to:

- use Pocket for anything, including paid gigs, commercial releases, client
  sessions, teaching and research;
- run it on as many of your machines as you like, on your own or with an agent;
- change it for your own use and keep those changes private;
- share it, changed or not, as long as you pass on the same freedoms (see
  [below](#when-the-license-asks-something-of-you)).

Nothing in the license depends on whether you earn money from your music.

## Your music is yours

The license covers Pocket itself, not what you make with it. Recordings,
renders, MIDI files, arrangements, Live Sets, analysis results, reports and
listening notes you make with Pocket are yours (or belong to whoever already
owns them), and you can share or sell them on any terms.

A few of Pocket's Max for Live devices, such as [Baste](docs/baste.md) and the
Native MIDI devices, can end up inside your project folders. Pocket's device
builders write them there, and [Stitch](docs/stitch.md) and
[Pipette](docs/pipette.md) carry them into trials and promoted projects. An
[additional permission](LICENSE-EXCEPTION.md#2-unmodified-device-files-in-a-musical-project)
lets you share a project that contains unmodified Pocket devices with no license
conditions at all. If you change a device and share your changed version, the
license applies to it as it does to the rest of Pocket.

Pocket's license gives no rights to recordings, models or other material that
you or others source independently. Those keep their own terms.

## When the license asks something of you

The license only asks something of you when you pass Pocket itself on to other
people:

- **If you share Pocket**, changed or not, include the license and make the
  source code of what you share available, including any changes you made.
- **If you change Pocket and let other people use your changed version over a
  network**, for example as a hosted service or a shared server, offer those
  people the source code of your changed version.

Using Pocket on your own machines, or through an agent that runs it locally,
asks nothing of you.

## Building a product on Pocket

If you distribute software that includes or builds on Pocket's code, or offer a
changed Pocket as a service, the license requires your product's source code to
be available under the same license. That's how Pocket stays open: improvements
come back to everyone.

If you'd rather keep your product closed, you can get a commercial license
instead. It lets you build on Pocket without the source-sharing conditions, and
suits companies making plugins, hardware or hosted services. If you're not sure
whether you need one, ask.

Commercial licensing: TODO: commercial licensing contact

## Why Pocket is licensed this way

Pocket is dual licensed: the same code is available under two licenses. The AGPL
is for everyone. The commercial license is for people who build closed
products. Commercial licenses help pay for Pocket's maintenance, so the core can
stay free for the musicians it's made for. Pocket's core will always be
available under an open source license.

Contributors sign a [contributor license agreement](CLA.md) so that the project
can offer both licenses. It grants a license and doesn't transfer anyone's
copyright, and it promises that every contribution stays available under an open
source license. [Contributing](CONTRIBUTING.md#contributor-license-agreement)
explains the process.

## Other parts of this repository

- The example scripts in `examples/` and the agent skill in `skills/` are MIT
  licensed, so you can copy them into your own scripts and agent setups on any
  terms.
- Pocket's note decoder adapts part of Basic Pitch, and Basic Pitch's Apache-2.0
  license travels with it.
- The website's font is under the SIL Open Font License.

[NOTICE](NOTICE) lists exactly which license applies to which files.

## Earlier versions

Pocket 0.4.0 and earlier were released under the MIT License. If you have a copy
of one of those versions, you can keep using it under the MIT License. Later
versions are licensed as described on this page.
