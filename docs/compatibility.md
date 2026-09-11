# Name compatibility

**Peek**, **Thread** and **Stitch** are the current names for Track Map, Set Map and Transition Lab. **Weave** and **Whisker** are the current names for Set Workshop and On Deck. These are naming changes, not analysis, editing or saved-format migrations. **Baste** and **Pipette** are new 0.4 tools, with no legacy aliases.

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
| `pocket weave` | `pocket workshop` |
| `pocket whisker` | `pocket on-deck` |

MCP discovery exposes **39 primary tools and 20 compatibility aliases: 59 names total**. Twenty primary names changed across the original five branded tools. The other 15 original names, including `identify_audio`, record-bag operations and the Spotify, acquisition and embedding adapters, are unchanged. Baste/Pipette add four names:

| MCP | CLI | Python provider |
|---|---|---|
| `baste` | `pocket baste` | `baste.observe_live` |
| `baste_build_device` | `pocket baste-device` | `baste.build_baste_device` |
| `pipette` | `pocket pipette promote` | `pipette.promote_trial` |
| `pipette_validate` | `pocket pipette validate` | `pipette.validate_promotion` |

These functions are also lazy root exports from `pocket_music`. New observation,
device and promotion records have their own v1 schemas; they do not rename or
rewrite existing Thread, Stitch or selection records.

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
| `weave` | `plan_set_routes` | `weave.plan_set_routes` |
| `weave_feedback` | `record_plan_feedback` | `weave.record_plan_feedback` |
| `weave_replan` | `replan_set` | `weave.replan_set` |
| `whisker` | `session_options` | `whisker.session_options` |
| `whisker_prepare` | `prepare_session` | `whisker.prepare_session` |
| `whisker_snapshot` | `session_snapshot` | `whisker.session_snapshot` |
| `whisker_update` | `update_session` | `whisker.update_session` |

Python module paths above are relative to `pocket_music`. Prefer `pocket_music.peek`, `pocket_music.thread`, `pocket_music.thread_queries`, `pocket_music.stitch`, `pocket_music.weave` and `pocket_music.whisker`. The former modules `track_map`, `set_map`, `set_queries`, `transition_lab`, `set_workshop` and `on_deck` remain same-object import-compatible aliases.

Generic Python function names such as `analyze_region`, `inspect_set`, `inspect_set_summary` and `query_set_region` stay unchanged. `export_thread` is the current export function; `export_set_map` remains an alias. Python `inspect_set` returns the full saved map. MCP `thread` and CLI `thread` return a bounded summary; CLI `thread --full` explicitly requests the full inventory.

Selection functions such as `plan_set_routes`, `record_plan_feedback`, `replan_set`, `prepare_session`, `session_snapshot`, `session_options` and `update_session` also retain their Python names. `whisker` is the MCP tool for session options; `pocket whisker` is the CLI group with its existing `prepare`, `snapshot`, `options` and `update` subcommands.

## Saved evidence stays valid

Existing schema identifiers, serialized field keys, CLI flags, cache locations and output artifacts retain their names and meanings. For example, Peek still emits `pocket.track-map/v1`, Thread still emits `pocket.set-map/v1`, `analyze_region_frame_args` and `set_map` keep their exact spelling, and the default saved-map cache remains `pocket/set-maps`. Trial filenames and prior reports are not renamed or rewritten. Do not replace these identifiers merely because a tool has a new public name.

Workspace HTTP endpoints, existing DOM selectors, sealed bag/plan/session keys, workspace and session filenames, hashes and revision semantics are unchanged by the Weave/Whisker branding. Existing sessions do not need migration.

Provider version constants `WORKSHOP_VERSION` and `ON_DECK_VERSION`, the `workshop_version` field and the serialized `on_deck.rank_next_tracks` provider label also remain unchanged. These identify implementation/evidence contracts, not the current display names.

The former documentation paths remain short compatibility link pages. Read the canonical [Peek](peek.md), [Thread](thread.md), [Stitch](stitch.md), [Weave](weave.md) and [Whisker](whisker.md) guides for current examples. Historical release notes retain their original naming under an explicit historical notice.
