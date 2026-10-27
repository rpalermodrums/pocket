# Maintaining the website

The website at [rpalermodrums.github.io/pocket](https://rpalermodrums.github.io/pocket/) is built with MkDocs from the same Markdown files you read on GitHub, using Pocket's own theme in `site/theme/`. `site/public-docs.json` is the publication allowlist. Only the tracked files it lists, the theme files and the generated provider reference are published. Ignored folders, recordings and stores can never become site content. Don't use repository-root upload patterns.

## Build and inspect

From the checkout:

```sh
python -m pip install -e '.[agent,dev,midi]'
python -m pip install -r requirements-docs.txt
python site/build.py --output private/site-build/review
python -m http.server 8765 --bind 127.0.0.1 --directory private/site-build/review
```

Open `http://127.0.0.1:8765/pocket/`. Choose a new output directory for each build. The builder refuses an existing destination, untracked or symlinked public sources and links to unlisted local material. Stage newly added public files before building; staging is not permission to commit private content.

The output includes a hashed publication manifest. The generated provider/reference JSON comes from `examples/export_contracts.py` and the installed package; a source-revision mismatch is refused. It includes actual Python/CLI/MCP inputs and explicitly does not claim a general HTTP API or closed output schemas. Legacy MCP names remain distinguishable from registered composable providers.

Run `python -m pytest -q tests/test_public_site.py` for publication boundary regressions. The docs workflow also builds the site, checks internal links and verifies the first experiment. Browser review should cover the home page, a long guide, search, the provider reference, keyboard navigation, light and dark color schemes, narrow screens and the 404 page.

## Update content

Edit each guide once, in place. Don't create a second copy for the website. Add new public pages and assets to the allowlist and the navigation explicitly. A plain-text source published as a page, such as `LICENSE` or `NOTICE`, is shown verbatim in a text block under its `title`, so license texts are never reflowed as Markdown. Links to repository code and examples resolve to the source revision used in the build. If a guide is missing, write it. Never publish ignored material to fill the gap.

The home page is `site/home.md`. It uses `md_in_html` blocks for the hero, principle and tool cards, and ordinary Markdown links so the builder can check them. Most guides open with a short `> **In brief.**` blockquote, which the theme styles as a summary callout. Keep the opening plain enough for someone new to Pocket, and link unfamiliar terms to [key ideas](concepts.md).

### Voice and brand

- **Plain, exact and warm.** Say what a tool does and what it won't do, in that order. Prefer everyday musical language ("the second time through", "bar one") and introduce technical terms once.
- **Pip and the pocket.** Pocket's identity is Pip, the field mouse in a terracotta record sleeve, and the tool names that come from a sewn pocket (Thread, Stitch, Weave, Baste, Pipette) and from Pip (Peek, Whisker). Use this lightly: one line of color per page at most.
- **Honest about evidence.** Never imply that a check, render or score is a musical verdict. Don't invent endorsements, listening results or approval. The living practice partner is a post-v1 aim, not a current feature.
- **Pocket is its own thing.** Name other software only when Pocket works with it, plans to, or openly takes inspiration from it.
- **Palette.** The theme's colors come from the artwork: ink blue `#254f70`, paper `#f5f1e8` and sleeve terracotta `#c7795c`, with a dark scheme derived from them. Text is set in the bundled Atkinson Hyperlegible Next.

The link-preview image, `assets/social-card.png`, is rendered from `site/social-card.html`. After changing the source, open it in Chromium at a 1200×630 viewport and save a screenshot over the PNG. Playwright's `page.screenshot()` does this directly.

## Publish and roll back

The `Publish website` workflow runs automatically when the `Tests` push run for a main commit succeeds. It publishes that exact commit unless main had already moved past it when its tests finished; a superseded commit is skipped, and the newer commit publishes when its own tests pass. A commit that lands while a publish is underway publishes after it, so the site ends on the newest main commit with passing tests. The workflow can also be dispatched manually from main to re-publish or roll back. Either way it builds from a trusted main revision, checks the matching successful test run, uploads only the allowlisted `pocket/` output, then deploys through the `github-pages` environment. Pull request runs, including those from forks, never trigger a deployment, and PR builds have read-only permissions. Pages configuration is a repository setting, separate from a code merge.

The site is published at `https://rpalermodrums.github.io/pocket/`. After a deployment, check anonymous access, links, search and the revision in `publication-manifest.json`. A local preview is not the live website.

To roll back, select an earlier successfully published main commit that contains the site builder and workflows using the workflow's revision input. The workflow accepts only ancestors of current main with a successful Tests run and rebuilds that revision's allowlisted content. Review the resulting artifact; never upload an arbitrary local directory. Record the deployment/commit and keep the previous accepted manifest. A rollback lasts only until the next main commit passes its tests and publishes automatically, so revert or fix the problem on main as well.

No site deployment publishes a package or changes repository visibility. Releases and tags are separate, deliberate maintainer decisions.
