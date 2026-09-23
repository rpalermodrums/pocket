# Maintaining the public site

The website renders tracked guides through MkDocs with Pocket's existing artwork. `site/public-docs.json` is the publication allowlist. Only listed tracked files, checked theme assets and generated installed reference are staged; private plans, recordings and stores are never source inputs. Do not use repository-root upload patterns.

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

Run `python -m pytest -q tests/test_public_site.py` for publication boundary regressions. The docs workflow also builds the site, checks internal links and verifies the first experiment. Browser review should cover home, a long guide, reference search, keyboard navigation and narrow screens.

## Update content

Edit existing Markdown once; do not create another editable copy for the website. Add public pages/assets explicitly to the allowlist and navigation. Links to repository code/examples resolve to the source revision used in the build. Never publish an ignored planning package to fill a missing guide.

Keep text direct and musician-first. Preserve the Pocket/Pip identity and the README removals from PR #1. The site uses the artwork's blue/paper/sleeve palette, readable text and restrained navigation. Existing capabilities need concrete examples; the living practice partner remains post-v1. No invented endorsements or musical approval.

## Publish and roll back

The `Publish website` workflow is manually dispatched from main after this PR is merged. It builds from that trusted main revision, checks the matching successful test run, uploads only the allowlisted `pocket/` output, then deploys through the `github-pages` environment. PR builds have read-only permissions and cannot deploy. Pages configuration is a repository setting, separate from a code merge.

The expected public address is `https://rpalermodrums.github.io/pocket/`. Treat that as a deployment target until a successful deployment is verified; a local preview is not a live website. After publishing, verify anonymous access, links, search and the manifest revision before setting the repository homepage.

To roll back, select an earlier reviewed main commit using the workflow's revision input. The workflow accepts only ancestors of current main with a successful Tests run and rebuilds that revision's allowlisted content. Review the resulting artifact; never upload an arbitrary local directory. Record the deployment/commit and keep the previous accepted manifest.

No site deployment publishes a package or changes repository visibility. Releases/tags require their own owner decision.
