# Changelog

Notable changes to Pocket, newest first. Package versions and the versions of serialized artifacts and profiles are independent of each other. Pocket hasn't been published to a package index. 0.4.0 is tagged as the last version released under the MIT License; the other version headings mark development milestones, not publication dates.

## Unreleased

### Changed

- **Pocket is now licensed under the GNU Affero General Public License, version 3 only (AGPL-3.0-only)**, with [additional permissions](LICENSE-EXCEPTION.md) for output and for unmodified Pocket Max for Live devices shared inside musical projects. Using Pocket stays free, including commercially, and what you make with it is yours; a commercial license is available for closed products. [Licensing](LICENSING.md) explains the change in plain language. **0.4.0 is the last version released under the MIT License**, and copies of it and earlier versions keep that license. `examples/` and `skills/` stay MIT licensed.
- The package version is now 0.5.0.dev0, so no AGPL-licensed build reports 0.4.0.
- Package metadata now declares `License-Expression: AGPL-3.0-only AND Apache-2.0` (Pocket, plus the Apache-2.0 Basic Pitch decoder derivative) and ships `LICENSE`, `LICENSE-EXCEPTION.md`, `NOTICE` and the Basic Pitch license. Building requires setuptools 77 or later.

### Added

- `NOTICE`: which license applies to which files, the MIT notice for earlier versions, and third-party notices. It now includes Basic Pitch's upstream notice, which its Apache-2.0 license requires alongside the adapted decoder.
- SPDX license identifiers at the top of source files. A test keeps new files labeled and keeps byte-pinned files (the decoder derivative, the model runners and the Max for Live devices) unchanged, so recorded identities, qualifications and native acceptance stay valid.
- A draft [contributor license agreement](CLA.md), pending legal review and not yet in effect, and a CLA workflow that stays off until the maintainer enables it.
- The website publishes the plain-language licensing page, the notices, the additional permissions and the draft agreement, and shows plain-text license files verbatim.

## 0.4.0: last MIT-licensed version

The last version released under the MIT License: the source at commit `3ba9a4f`, tagged `v0.4.0`. It wasn't published to a package index. It includes the [Baste and Pipette](docs/releases/0.4.0.md) milestone (fresh read-only Live observation and preservation of explicitly kept saved trials; existing names and serialized identifiers remain compatible) and the changes below.

### Added

- Source-bound musical contexts, explicit occurrence mappings and typed interpretation bindings.
- Reversible source slips, timeline shifts and anchor rebindings with locks and preservation evidence.
- Exact practice renders, revision comparisons, scoped attributed feedback and an explicit linear join-envelope profile.
- Installed Python/CLI/MCP input contract export and opt-in machine-readable errors.
- Public documentation site source, generated provider reference, contribution/conduct/security policies and an allowlisted publishing build.
- `practice_preview`: a declared `pocket.practice-preview/v1` browser copy of an exact practice render under `browser-pcm16-original-rate/v1` (original rate, PCM16, nearest rounding with ties to even; no dither, gain, clamping or resampling). Unrepresentable samples and undeclared sample rates are refused. Restart a running MCP server to register the new tool.
- `practice_feedback` accepts an optional `preview` and then writes `pocket.practice-feedback/v2`, recording the exact preview and interval reviewed. Calls without `preview` still write unchanged v1 reports. `practice_feedback_query` accepts mixed v1/v2 reports and adds `report_schema` and `reviewed_audio` to v2 rows only; `practice_query` also reads previews and v2 reports.
- `pocket practice-review`: a loopback-only [practice review page](docs/practice-review.md) that plays one retained comparison through declared previews and saves explicitly attributed interval reports.
- New guides for newcomers: [key ideas](docs/concepts.md), with a glossary, and [use Pocket with an agent](docs/agents.md). Most guides now open with a plain-language summary.
- A redesigned website: a new home page with the tool family, light and dark color schemes, an on-page contents rail, previous and next links, copy buttons for code, search results with context, a 404 page and a link-preview image.
- `examples/linked_downbeat.py`: a synthetic [linked-downbeat handover](docs/musical-context.md#land-an-internal-downbeat-on-a-handover). A linked source slip moves a repeated passage's internal bar one onto its clip boundaries. Locks refuse any change to the clip placement or to an anchor's kind or position, and no context edit can change the tempo step. It writes a revision comparison for `pocket practice-review` and records no listening. It adds no provider, profile or schema.

### Changed

- The documentation was reviewed end to end for newcomers. The README, guide index, getting-started walkthrough, status page, capability matrix, release notes and agent skill were rewritten for clarity, and references to private working material were removed.
- Within one practice provider call (and `context_edit_query`), artifact bytes and hash checks already verified earlier in that call are reused, within per-call memory bounds, instead of being read and hashed again. Nothing verified is trusted across calls.

### Fixed

- Practice feedback queries report the processing profile of the audio actually reviewed.
- CI installs the optional MIDI dependency used by its test suite.
- The selection workspace refuses a non-ASCII `X-Pocket-CSRF` header with 403 instead of dropping the connection.

The music-tool foundation landed in [PR #2](https://github.com/rpalermodrums/pocket/pull/2). It preserves legacy defaults; no human musical approval or new native qualification is implied. The public-site and policy entries landed in [PR #3](https://github.com/rpalermodrums/pocket/pull/3). Browser previews, v2 reports and the practice review page come from [PR #4](https://github.com/rpalermodrums/pocket/pull/4); no human listening, device-output check or native qualification has been performed with them.

## 0.2 development milestone

[0.2 notes](docs/releases/0.2.0.md): canonical tool naming and the associated compatibility changes and limits.

## Maintaining this file

Add concise user-facing changes under Unreleased. Include migration details for changed schemas/profiles, errors or aliases. Before assigning a release heading/date, verify the version/tag and owner-approved release decision. Do not create a tag or publish a package as a side effect of updating notes.
