# Security policy

## Report privately

Use [GitHub's private vulnerability reporting](https://github.com/rpalermodrums/pocket/security/advisories/new) for a suspected Pocket vulnerability. If that route is unavailable, email [ryan@ryanpalermo.dev](mailto:ryan@ryanpalermo.dev).

Do not open a public issue containing exploit details, credentials, private recordings, full stores or personal paths. Include the affected commit/version, platform, a minimal synthetic reproduction, expected/actual behavior and likely impact. Share only what is needed to investigate; never send live tokens.

Ryan Palermo handles reports on a best-effort basis. There is no guaranteed response time, bug bounty or maintained backport schedule. Please coordinate disclosure so a fix and useful guidance can be prepared.

## Supported development state

Pocket is in early development. Report against current main where possible and identify the exact revision. Versioned notes are not a promise that older releases receive security updates. Package/schema versions and native qualification profiles are separate.

## Boundaries

Pocket's local process can read and write files available to the account running it. Use trusted agent configuration and deliberately selected data. Loopback browser interfaces are local tools, not authenticated remote services; do not expose them to a network. Optional online/model/native adapters need explicit setup and have separate failure modes.

Evidence verification checks retained identities within declared bounds. It is not a sandbox for arbitrary code or a guarantee of musical correctness. Repository scanning is another layer of review, not proof that all vulnerabilities have been found.
