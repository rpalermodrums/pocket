# Selection interfaces

> **In brief.** Pocket's record-selection tools work together. A
> [record bag](record-bag.md) holds your crate. [Weave](weave.md) proposes
> routes through it, [Whisker](whisker.md) proposes what comes next during a
> set, and the [workspace](workspace.md) puts both in a browser page. This guide
> shows the matching command-line and MCP calls for each step. The tools propose;
> you decide.

Weave and Whisker share the same sealed record bags across Python, the
command line, the local [workspace](workspace.md) and MCP. They propose routes
and next records; they neither mix audio nor certify a handoff. Unknown properties
remain unknown. A catalog URI is distinct from exact local recording identity.

## Human workspace

```sh
pocket workspace --workspace-dir /path/to/private-session --bag-handle /path/to/bag-result.json
```

The handle file can contain a bare bag handle or the result of creating a bag.
The server binds to loopback, prints its local URL and stays open until Ctrl-C.
Port 0 chooses a free port; `--port 8765` requests a fixed one. This blocking server
is not an MCP tool. Keep workspace/catalog artifacts outside public repositories.

## JSON command line

Every new operation uses `--spec FILE`, a JSON object with the same argument names
as its public Python function. A **destination-writing** operation requires
`--output`: a new artifact directory, except `embeddings build`, which creates a
new index JSON file. Omit `output_dir`/`output_path` from that specification; the
CLI supplies it. The resulting handle or receipt is printed as JSON to stdout.

For operations without a new provider destination, optional `--output FILE`
saves the response JSON; omit it for stdout. Existing response files are rejected
before a state-changing call. Session updates and playlist executions still write
their provider's revision/receipt history. No action silently overwrites an earlier
plan or bag. Provider errors become a JSON error on stderr with exit code 2.

| Command | Public function | `--output` |
|---|---|---|
| `bag create` | `create_record_bag` | New bag directory |
| `bag query` | `query_record_bag` | Optional response JSON |
| `bag revise` | `revise_record_bag` | New bag revision directory |
| `weave plan` | `plan_set_routes` | New plan directory |
| `weave feedback` | `record_plan_feedback` | New feedback revision directory |
| `weave replan` | `replan_set` | New plan directory |
| `whisker prepare` | `prepare_session` | New session directory |
| `whisker snapshot` | `session_snapshot` | Optional response JSON |
| `whisker options` | `session_options` | Optional response JSON |
| `whisker update` | `update_session` | Optional response JSON |
| `spotify import` | `import_spotify_items` | New import directory |
| `spotify plan` | `plan_spotify_playlist` | New playlist plan directory |
| `spotify execute` | `execute_spotify_playlist` | Optional response JSON |
| `spotify verify-ui` | `verify_spotify_playlist_ui` | Optional response JSON |
| `acquire discover` | `discover_sources` | Optional response JSON |
| `acquire formats` | `inspect_source_formats` | Optional response JSON |
| `acquire plan` | `plan_acquisition` | New selected-source plan directory |
| `acquire run` | `acquire_source` | New acquisition directory |
| `embeddings preflight` | `model_preflight` | Optional response JSON |
| `embeddings build` | `build_embedding_index` | New index JSON file |
| `embeddings query` | `rank_embedding_query` | Optional response JSON |

For example, save this as a new `bag-spec.json`:

```json
{
  "title": "Opening records",
  "tracks": [
    {"track_id": "record-a", "title": "A record", "artists": ["An artist"],
     "profile": {"roles": ["opening"], "provenance": "user"}},
    {"track_id": "record-b", "title": "Another record", "artists": ["Another artist"]}
  ]
}
```

Then create the bag:

```sh
pocket bag create --spec bag-spec.json --output new-bag-directory
```

Use the returned `handle` object as `bag_handle` in a Weave specification:

```json
{
  "bag_handle": {"schema": "pocket.record-bag-handle/v1", "path": "/absolute/path/to/record-bag.json", "sha256": "COPY_THE_RETURNED_SHA"},
  "brief": {"setting": "warm_up", "track_count": 2},
  "seed": 12,
  "route_count": 2
}
```

```sh
pocket weave plan --spec workshop-spec.json --output new-plan-directory
```

Use actual returned handles; the illustrative path and SHA are not valid inputs.
`warm_up`, `peak_time` and `after_hours` are the initial exploratory settings.
Anchors retain relative order, not fixed opening/closing positions. Estimated
performance duration remains separate from full-record duration. See
[Weave](weave.md) for its exact feedback and duration semantics.

## MCP and public Python

The Weave route tools are MCP `weave`, `weave_feedback` and `weave_replan`. Shared-session tools are `whisker_prepare`, `whisker_snapshot`, `whisker` (options) and `whisker_update`. Their Python function names in the command table remain unchanged. The [name changes](compatibility.md) page maps every renamed tool to its current name.

`pocket-mcp` exposes canonical tools alongside compatibility aliases. The 21 selection provider tools return one compact JSON
text record without a duplicated structured copy. Inputs expose nested bag/plan
handles, attributed profiles, brief constraints, integer revision/frame fields,
choice enums and cached embedding receipt fields. Standard option limits apply:
bag pages default to 20, Whisker to 6, Weave to 3 distinct routes. Explicit
larger requests still obey provider bounds; limited route diversity is reported.
Full catalogs and embedding indexes are not automatically echoed by discovery.

Python imports stay lazy:

```python
from pocket_music import create_record_bag, plan_set_routes, prepare_session, session_options
```

These resolve to the actual provider functions; importing Pocket neither loads
model weights nor starts a server. Existing module imports and the three original
tools remain supported. `inspect_set` is still the full Python library function;
the MCP tool `thread` returns the summary-first interface. See [name changes](compatibility.md) for the old tool aliases that still work.

Optional unknown numeric profile values may be null; omitting an unknown field
is always the most portable choice. In particular, omit unavailable/explicit flags
when unknown rather than sending null to the Spotify bridge. Do not replace an
unknown energy or BPM with a default measured value. Session updates require the
current revision **and** SHA; stale writes fail and require reloading the snapshot.

## External actions and optional models

Spotify import accepts at most 500 observed rows per batch. Preserve exact order
and URIs; import results distinguish unresolved entries and supply bag-compatible
tracks. A reviewed export plan creates only a new private playlist. Execution
reads the token from `POCKET_SPOTIFY_ACCESS_TOKEN` or a named environment variable;
**never put credentials in specs, MCP arguments or receipts**. The tool does not
authenticate automatically. UI verification records operator-reported observations,
not independent API evidence. Read [Spotify bridge](spotify-bridge.md) before use.

Source discovery and format inspection are network metadata reads. Inspect format
IDs before explicitly choosing an alternative codec with `plan_acquisition`
`format_id`; there is no silent codec fallback. Acquisition follows an explicitly
selected, sealed URL/version plan; it preserves original compressed bytes and
records decoding. Neither title matching nor a WAV extension proves recording
identity or lossless origin. See [acquisition](acquisition.md).

Embedding preflight only checks a prepared cache. Build/query consume already
computed receipts and run no model inference. Independent local audio and
user-authored text are eligible; Spotify streams, previews and imported metadata
are not model inputs. Optional inference uses the isolated worker described in
[music embeddings](music-embeddings.md). Whisker remains usable offline without it.
