# Pocket workspace

A small, local browser interface for Set Workshop and On Deck. Browse a record bag, make three proposed routes, save route or directed-pair feedback, and keep a shared next-record session. Nothing here plays audio, operates decks, authenticates Spotify, downloads recordings or establishes a musical verdict.

Run `pocket workspace --workspace-dir /path/to/new-workspace`. The command prints a JSON object containing the local URL, then serves until Ctrl-C. `--port 0` chooses an available port. An optional `--bag-handle /path/to/handle.json` initializes a verified existing bag; importing another bag later preserves the old artifacts. The Python entry point is:

```python
from pocket_music.workspace import start_workspace

start_workspace(workspace_dir, bag_handle=None, host="127.0.0.1", port=0)
```

The server deliberately accepts only `127.0.0.1`. It is a blocking local UI, not a remote service or a long-running MCP request. No JavaScript library, external font, CDN or network service is used.

## Bring records, then try a direction

Import a JSON array of catalog tracks, or an object with `title` and `tracks`. The format is the shared `BagTrackInput` catalog subset:

```json
{
  "title": "Practice bag",
  "tracks": [
    {"track_id": "first", "title": "A fictional record", "artists": ["Example artist"]},
    {"track_id": "second", "title": "Another fictional record", "artists": ["Example artist"]}
  ]
}
```

Search title, artist or attributed tags. Tempo, energy and edition remain unknown unless supplied through the underlying bag contract. The optional generated practice bag contains fictional records and no audio. Browser imports reject local file references: prepare a bag through the regular provider and pass its handle when verified local audio is required. JSON uploads are limited to 2 MB.

Workshop saves immutable plans through the same provider as the CLI. Choose a setting, target duration, record count and reproducible variation. Route feedback and pair feedback remain bound to their exact plan, bag and brief. “Try again with feedback” produces another plan; it does not overwrite the prior one. Duration is an estimate, and transition treatments remain proposals.

On Deck starts from a chosen record, or an empty current selection. Choose next, skip, or change intent. Choosing updates session state only. History, reasons and unknowns stay available without crowding the main options. Warm Workshop and lowlight On Deck share one bag.

## Shared state and local protection

The displayed session directory can also be used by the session CLI/MCP providers. An existing matching-bag session can be attached using its path relative to the workspace, including one prepared externally inside that directory. Refresh reads the current session. Every session mutation carries the exact revision and hash observed by the browser; a concurrent CLI change rejects a stale browser action rather than overwriting it.

`workspace.json` is an atomic mutable pointer to the current sealed bag/plan and shared session. The server serializes workspace writes and checks a workspace revision on each change; a lifetime file lock prevents two workspace servers using the same directory. Provider plans, bags and session histories retain their own integrity checks. Keep the whole directory as the human workspace; bag handles may still point to external local files and are not a portable-media package.

All routes and static assets are explicitly allowlisted. The server checks the exact local Host, rejects foreign origins, requires same-origin JSON plus a random session CSRF token for writes, and sends a restrictive content security policy. Untrusted catalog text is inserted with DOM `textContent`; there is no arbitrary file or command endpoint. Shared sessions must resolve inside the workspace, including through symlinks. This protects the local browser boundary; it is not a sandbox against another process with access to the user's filesystem.

## Initial limits and verification

This first interface exposes compact route controls and four next-track intents. More detailed constraints, embeddings and analysis stay in their existing providers. It has no drag ordering, waveform player, audio editor or remote sharing. A stale-state error asks for Refresh; it does not silently retry a decision against new state.

Generated-fixture HTTP tests exercise import/search, route generation and scoped feedback, replan, session choose/skip/intent, external-session CAS, initial handles, preserved old plans, path rejection, bounded requests, Host/Origin/CSRF protection and static content policy. All browser workflows use the actual shared providers; no fake route or session backend is shipped.
