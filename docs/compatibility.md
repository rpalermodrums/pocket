# Name compatibility

**Peek**, **Thread** and **Stitch** are the current names for Track Map, Set Map and Transition Lab. This is a naming change, not an analysis, editing or saved-format migration. **Pipette** is reserved for a future tool; it has no command, provider or MCP endpoint.

## Current interfaces

Use the current names for new calls. The CLI still accepts these exact older aliases, with the same flags and subcommands:

| Current CLI | Accepted compatibility alias |
|---|---|
| `pocket peek` | `pocket track-map` |
| `pocket thread` | `pocket set-map` |
| `pocket thread-region` | `pocket set-region` |
| `pocket thread-find-clips` | `pocket find-clips` |
| `pocket thread-export` | `pocket map-export` |
| `pocket thread-source-position` | `pocket source-position` |
| `pocket stitch` | `pocket lab` |

MCP discovery exposes **35 primary tools and 13 compatibility aliases: 48 names total**. Thirteen primary names changed; the other 22, including `identify_audio` and all selection tools, are unchanged.

| Current MCP name | Accepted compatibility alias | Python function |
|---|---|---|
| `peek` | `analyze_region` | `peek.analyze_region` |
| `thread` | `inspect_set` | `thread_queries.inspect_set_summary` |
| `thread_region` | `query_set_region` | `thread_queries.query_set_region` |
| `thread_find_clips` | `find_clips` | `thread_queries.find_clips` |
| `thread_export` | `export_set_map` | `thread_queries.export_thread` |
| `thread_source_position` | `source_position` | `thread.source_position` |
| `thread_arrangement_position` | `arrangement_position` | `thread.arrangement_position` |
| `stitch` | `create_trial` | `stitch.create_trial` |
| `stitch_prepare_native` | `prepare_native_trial` | `stitch.prepare_native_trial` |
| `stitch_feedback` | `record_feedback` | `stitch.record_feedback` |
| `stitch_feedback_list` | `query_feedback` | `feedback.query_feedback` |
| `stitch_attach_render` | `attach_completed_render` | `stitch.attach_completed_render` |
| `stitch_validate_native` | `validate_native_trial` | `stitch.validate_native_trial` |

Python module paths above are relative to `pocket_music`. Prefer `pocket_music.peek`, `pocket_music.thread`, `pocket_music.thread_queries` and `pocket_music.stitch`. The former modules `track_map`, `set_map`, `set_queries` and `transition_lab` remain import-compatible aliases.

Generic Python function names such as `analyze_region`, `inspect_set`, `inspect_set_summary` and `query_set_region` stay unchanged. `export_thread` is the current export function; `export_set_map` remains an alias. Python `inspect_set` returns the full saved map. MCP `thread` and CLI `thread` return a bounded summary; CLI `thread --full` explicitly requests the full inventory.

## Saved evidence stays valid

Existing schema identifiers, serialized field keys, CLI flags, cache locations and output artifacts retain their names and meanings. For example, Peek still emits `pocket.track-map/v1`, Thread still emits `pocket.set-map/v1`, `analyze_region_frame_args` and `set_map` keep their exact spelling, and the default saved-map cache remains `pocket/set-maps`. Trial filenames and prior reports are not renamed or rewritten. Do not replace these identifiers merely because a tool has a new public name.

The former documentation paths remain short compatibility link pages. Read the canonical [Peek](peek.md), [Thread](thread.md) and [Stitch](stitch.md) guides for current examples. Historical release notes retain their original naming under an explicit historical notice.
