# Spotify bridge

> **In brief.** An optional bridge for moving *lists* of records to and from
> Spotify. You can import a playlist's catalog details into a record bag, or
> create a new private playlist from a plan you've reviewed. It never streams
> or analyzes Spotify audio, and Spotify content is never sent to a model.
>
> **You need** nothing extra to import. Creating a playlist needs a Spotify
> access token in the process environment (`POCKET_SPOTIFY_ACCESS_TOKEN`).

This optional adapter transfers a deterministic catalog/order. It does not stream, analyze Spotify audio, infer musical features, authenticate automatically or send Spotify content to a model. The user interface and API call the same functions in `pocket_music.spotify_bridge`.

## Import

`import_spotify_items(items, output_dir, *, title='Spotify selection', catalog_source='spotify_ui')`

`SpotifyCatalogItem` requires `uri`; optional fields are `title`, `artists`, `album`, `duration_seconds`, `explicit`, `is_local`, `available`, `version_note`, `isrc` and zero-based `position`. Supply at most 500 rows. A declared position must equal its input index. The sealed `import.json` preserves every row and its unresolved reasons; rows with valid track URIs/title/artists also become record-bag-compatible `tracks`. Unavailable rows remain `available:false`. Local/unsupported URIs and missing catalog identity remain unresolved; no title-based replacement occurs. Unknown explicit status remains absent. Repeated URI occurrences preserve their order and receive distinct row IDs.

An import is `catalog_only`, not recording-equivalence evidence. The returned `model_input_allowed:false` is a usage boundary: models must receive independently sourced local audio and user-authored musical annotations, not this Spotify catalog. Human UI/API exports are different catalog provenance. Plain user lists belong to the record-bag provider.

## New private playlists

`plan_spotify_playlist(uris, output_dir, *, name, description='')` seals 1–500 exact `spotify:track:<ID>` URIs, preserving duplicates. The plan always requests a new private, non-collaborative playlist and has a unique operation marker. It cannot target an existing playlist. The result supplies `plan_dir`, `path`, `sha256`, count and a manual checklist.

`execute_spotify_playlist(plan_dir, *, expected_plan_sha256, token_env='POCKET_SPOTIFY_ACCESS_TOKEN')` uses only an environment access token. A token itself must never be a CLI/MCP argument, response, manifest or log. Configure authorization separately; no client secret, cookies or PKCE workflow is implemented in this module. Missing token returns `manual_action_required`. No optional SDK is required: requests use the standard library.

The executor uses current-user `POST /me/playlists`, `POST /playlists/{id}/items` and complete paginated readback. Request `playlist-modify-private` and `playlist-read-private`. Writes are journaled and fsynced before being sent; an exclusive operation lock prevents two executors from duplicating the same plan. Reads verify ownership, privacy, operation marker and exact URI order. Names alone are not unique IDs.

A lost create response is reconciled through the unique marker in the current user's playlists, bounded to 1,000 playlists. A lost add is checked against the expected prefix/count; a clearly committed batch can resume. Absent/ambiguous uncertain writes, external ordering/removal, malformed responses or inaccessible data return `needs_reconciliation` without blindly replaying the write. No automatic rollback/delete/replace occurs. The executor does not retry internally; `Retry-After` and quota status are returned for a later deliberate retry/reconciliation. A definitely rejected create may therefore require a fresh plan after correcting authorization. Receipts are immutable, while `journal.jsonl` is explicitly append-only operational state; do not edit it.

`verify_spotify_playlist_ui(plan_dir, *, expected_plan_sha256, playlist_url, observed_uris, observed_private, observer)` records a human/UI-observed ordered URI list and privacy flag, bound to the plan hash and playlist URL. It reports observed/expected counts. `ready` requires exact order and observed private status. This is `operator_reported_ui`, never independent API verification; the observer is responsible for the actual observation. A playlist rehearsal is not a DJ render or musical verdict.

## Current access and limits

Checked September 2026: development apps need a Premium owner and five-user allowlist. July raised the Client ID limit to 25; development quotas are shared per developer account. Recommendations/Audio Features/Audio Analysis are unavailable for new use cases. Existing integrations can have different migration status. Treat actual denied access as a capability result, not a reason to request broader scopes automatically.

Use PKCE with an explicit loopback IP if a later optional authorization module is added; `localhost` is not permitted. Keep authentication outside agent-readable tool arguments. A person can create the fresh private playlist and use the UI receipt today.

Primary documentation: [playlist creation](https://developer.spotify.com/documentation/web-api/reference/create-playlist), [items](https://developer.spotify.com/documentation/web-api/reference/add-items-to-playlist), [quota modes](https://developer.spotify.com/documentation/web-api/concepts/quota-modes), [July changes](https://developer.spotify.com/documentation/web-api/references/changes/july-2026), [redirect rules](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri), [Developer Policy](https://developer.spotify.com/policy).
