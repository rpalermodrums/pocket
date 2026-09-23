# Changelog

Changes are recorded from repository history. Package versions and serialized artifact/profile versions are independent. There were no GitHub release tags at this documentation pass; the older version notes below describe development milestones, not verified publication dates.

## Unreleased

### Added

- Source-bound musical contexts, explicit occurrence mappings and typed interpretation bindings.
- Reversible source slips, timeline shifts and anchor rebindings with locks and preservation evidence.
- Exact practice renders, revision comparisons, scoped attributed feedback and an explicit linear join-envelope profile.
- Installed Python/CLI/MCP input contract export and opt-in machine-readable errors.
- Public documentation site source, generated provider reference, contribution/conduct/security policies and an allowlisted publishing build.

### Fixed

- Practice feedback queries report the processing profile of the audio actually reviewed.
- CI installs the optional MIDI dependency used by its test suite.

The music-tool foundation landed in [PR #2](https://github.com/rpalermodrums/pocket/pull/2). It preserves legacy defaults; no human musical approval or new native qualification is implied. The public-site and policy entries describe the changes accompanying this changelog, pending merge/publication.

## 0.4 development milestone

[Baste and Pipette](docs/releases/0.4.0.md): fresh read-only Live observation and preservation of explicitly kept saved trials. Existing names and serialized identifiers remain compatible.

## 0.2 development milestone

[0.2 notes](docs/releases/0.2.0.md): canonical tool naming and the associated compatibility changes and limits.

## Maintaining this file

Add concise user-facing changes under Unreleased. Include migration details for changed schemas/profiles, errors or aliases. Before assigning a release heading/date, verify the version/tag and owner-approved release decision. Do not create a tag or publish a package as a side effect of updating notes.
