# Contributing to Pocket

Thanks for helping. Pocket is a musician-centered toolkit made of small, composable operations. If you're new, read [key ideas](docs/concepts.md) first; it explains the vocabulary the rules below rely on. Start with a concrete musical or user problem. For a new public capability, processing profile or artifact schema, open a proposal before building a large feature. A focused bug fix can start with a reproducible case.

## Set up and check your change

Use Python 3.11 or later and Node.js for the synthetic Max/bridge tests:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[agent,dev,midi]'
python -m ruff check --select E9,F63,F7,F82 src tests examples
node --test tests/baste_reader.test.cjs tests/baste_bridge.test.cjs
python -m pytest -q -o tmp_path_retention_policy=failed
```

Run focused tests while developing, then the relevant full checks before submission. The MIDI extra is required for full test collection even when your change does not process MIDI. Optional acquisition tools and model environments are separate; skipped optional tests must remain visible in your report. Never relax native deadlines or evidence checks merely to get a passing run.

[Building the website](docs/site-maintenance.md) uses a separate, pinned documentation toolchain. It does not change Pocket's runtime dependencies. When you change behavior, update the guide that describes it in the same pull request, and follow the site's [voice and brand notes](docs/site-maintenance.md#voice-and-brand).

## Keep the musical contracts precise

- Identify recordings by their bytes. Keep source frames, timeline positions, pulse, bar one, phrase starts and repeated occurrences distinct.
- Keep measured evidence, algorithmic hypotheses, authored decisions and attributed listening separate. Preserve uncertainty and contradictory reports.
- Write new artifacts and variants. Never overwrite the user's media, project or earlier trial. Declare processing explicitly: no hidden normalization, fades, resampling, stretching or rounding.
- Use the same public provider implementation through Python, CLI and MCP. Update discovery, input contracts and usage docs together. Keep optional dependencies optional.
- Preserve serialized compatibility and request replay semantics. Version a changed artifact/profile contract; document migration and refusal behavior. Do not invent closed output schemas where the implementation returns dynamic receipts.
- Retain bounds and transitive integrity checks. Measure before optimizing verification; a cached assertion is not proof of unchanged bytes.

Add regression tests for actual failure modes. For audio transformations, use independently computed expected samples. For interface changes, test the installed package outside the checkout as well as direct imports. Technical checks do not establish musical quality.

## Keep private material private

Generate public test audio at runtime. Do not commit real recordings, full mixes, native projects, credentials, model weights, personal listener notes, machine paths or retained user stores. Use ignored `private/` for local plans and acceptance evidence. Sanitize issue reports and inspect staged filenames/content before committing. Do not force-add ignored files to make a test pass.

The MIT license covers this project's code. You are responsible for permission and compatible licensing for contributed code/assets. Preserve third-party notices. Public examples must use synthetic or explicitly cleared material.

## Native and online work

A fake-host test, saved-project inspection, native save/reopen, actual render and human listening are separate acceptance steps. State which you performed. Coordinate use of Live; use disposable projects and never assume the active user session is a test fixture. Do not silently install models, acquire music, access online accounts or broaden a native profile.

## Pull requests

Keep changes reviewable and explain the before/after behavior. Include compatibility impact, tests actually run, skipped/unperformed acceptance and known limits. For agent-assisted work, the submitter remains responsible for correctness and evidence; identify assistance when it helps explain how the work was validated. An automated review is not a human approval.

Use a branch and PR. Pocket currently has a single maintainer. Required CI is the normal merge gate, and an administrator bypass exists for exceptional maintainer decisions. Don't disable protection to get past a failure. The maintainer merges and chooses any bypass explicitly. There is no outside reviewer or response-time guarantee.

For agents, Git writes, publishing, releases and settings changes require task-specific authorization. Read [AGENTS.md](AGENTS.md) and preserve unrelated working changes.

## Reports and conduct

Use public issues for sanitized bugs and capability proposals. Follow the [code of conduct](CODE_OF_CONDUCT.md). Send vulnerabilities through [private security reporting](SECURITY.md), not a public issue. For sensitive non-security concerns, contact [Ryan](mailto:ryan@ryanpalermo.dev).
