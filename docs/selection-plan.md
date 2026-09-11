# Weave and Whisker

This cycle adds two tools to Pocket and tests them with three separate briefs: warm-up, peak time and after-hours. A planned route is a musical hypothesis. A live suggestion should preserve the DJ's freedom and be cheap enough to consult during a set.

## Implementation order and stopping point

1. **Shared record bag and Weave.** Import a user song list or a deterministic Spotify snapshot; preserve recording/version identity and missing information. Create several reproducible routes with anchors, exclusions, duration estimates and explicit transition ideas. Save alternative trials and scoped pair feedback so rejected transitions can change a later attempt without banning a recording globally.
2. **Whisker.** Prepare features before performance. Given the current track, played history and intent, return a compact, diverse shortlist for holding, lifting or changing direction, with reasons, limitations and proposed treatment. Support manual choices, skips and append-only session history. No audio routing or automatic deck control is part of this cycle.
3. **Practical adapters and human controls.** Expose both through the library, CLI, MCP and a small local browser workspace. Support a fresh Spotify rehearsal playlist with order verification, plus reviewed yt-dlp source acquisition retaining the original codec and exact identity. Evaluate one optional local music-embedding model on independently sourced recordings. Network calls and model inference stay outside the live decision path.

The stopping point is a working offline-capable planning/live toolchain, real Spotify-library-derived direction proposals, an actual playlist-transfer rehearsal, a tested acquisition path and honest model evaluation. It is not three newly rendered Ableton sets or an autonomous DJ. Test-set directions are proposed for the next musical production pass.

## Shared contracts

`selection_types.py` defines the input vocabulary. Record-bag entries have stable `track_id`, title/artists and optional Spotify URI; a title is not a recording identity. Optional local media is identified and versioned separately. `profile` contains explicitly attributed musical annotations. `catalog_source` distinguishes UI/API catalogs from user input and independent local manifests. No Spotify content is passed into the optional model; model inference uses independent local audio and user-authored intent. Spotify is a deterministic catalog/playlist bridge, not a streaming mixer or an ML feature source.

The Weave owner implements `record_bag.py`: `create_record_bag(tracks, output_dir, title)`, `load_record_bag(handle)` and immutable revision helpers. A handle has schema, path and SHA-256. `load_record_bag` verifies the sealed manifest and returns its dictionary with a `tracks` list. Other providers never edit that list or the underlying manifest in place.

The Whisker owner implements a pure `rank_next_tracks(tracks, current_track_id, *, played_ids, intent, limit)` helper consumed by both tools. Returned options include track identity, a role/lane, separate reasons and unknowns, estimated tempo treatment when applicable, and a proposed transition—not a listening verdict. Weave may add sequence-level constraints and exploration; Whisker may add state/history. Optional embeddings remain replaceable and carry model revision, audio hash and source interval.

Playlist export preserves exact Spotify track URIs and rejects unresolvable entries. New playlist writes require a concrete export plan; interrupted or uncertain writes are reconciled before retrying. Audio search returns candidates; acquisition requires an explicitly selected source URL and version evidence. Best available compressed audio is never relabeled lossless because it was decoded to WAV.

## Parallel ownership

| Owner | Files and responsibility |
|---|---|
| Weave | `record_bag.py`, `weave.py`, their tests and provider docs; consumes the shared types and pure ranking helper |
| Whisker / models | `whisker.py`, `music_embeddings.py`, optional model worker, tests and provider/model docs; owns the pure ranking helper |
| Spotify / acquisition | `spotify_bridge.py`, `acquisition.py`, tests and adapter docs; no UI control or credentials in fixtures |
| Integrator | Shared types, CLI/MCP, local workspace UI/server, package metadata, integration tests, release/evaluation docs, Spotify UI exploration and production checks |

Each implementation branch has its own worktree. Contributors commit their own reviewed files locally; the integrator alone merges exact commits and pushes tested milestones to private `main`. No broad staging, force pushes, shared-branch edits or recording/model/private-library fixtures in Git. Existing sets, earlier trials and the initial three tools remain preserved.

## Acceptance

- Unknown tempo/key/energy remains unknown. Catalog metadata does not imply a measured property. Models supply retrieval evidence, never musical approval or an automatic beat-one edit.
- The same brief/seed is reproducible; a different seed/intent can generate materially different routes. Anchors, exclusions and forbidden exact pairs survive replanning. Full-track duration and estimated performance duration remain distinct.
- Trial feedback is bound to its bag/route or exact transition; rejection does not silently become a global preference. Changes create a new revision.
- Whisker returns distinct viable options with reasons and omissions, excludes the currently playing and already played tracks by default, and continues to work offline. A 1,000-track generated bag has bounded response size and sub-250 ms steady-state decision latency on the development machine; startup/preparation costs are reported separately.
- The local human workflow is legible at a glance, supports manual override and shows the same records as CLI/MCP. Browser writes are local and limited to the chosen workspace; render untrusted titles/notes as text.
- Spotify import/export preserves order/version identifiers and reports inaccessible/local/unavailable items. A fresh test playlist is read back and its exact order verified through the supported connection/UI.
- The acquisition adapter has bounded discovery, no shell interpolation, no overwrite, codec/source provenance, stable hashes and a truthful lossless/decoded distinction. A controlled fixture and a selected real source exercise the complete path.
- Model evaluation records source/weight identities, runtime and limits; fallback is explicit. No remote audio upload, training or inference inside the live loop.
- Generated tests, real local acceptance, installation and actual interface checks pass before delivery. The three proposed test directions cite observed liked tracks and keep unauditioned transitions labeled as proposals.
