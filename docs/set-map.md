# Set Map

Set Map reads saved Ableton Live 12 intent. It does not open Live, alter a set,
execute an instrument, or establish what a listener hears.

```python
from pocket_music.set_map import inspect_set, source_position, arrangement_position

set_map = inspect_set("mix.als")
clip = set_map["clips"][0]
source = source_position(set_map, clip["id"], clip["start_beat"])
inverse = arrangement_position(set_map, clip["id"], source["source_seconds"])
```

`inspect_set(path, *, hash_sources=False)` returns a JSON-serializable
`pocket.set-map/v1` document. It includes track and clip instances, source file
references and available headers, original warp markers, loop fields, gain,
pitch, fade settings, MIDI note structures, devices, routing, locators, transport,
tempo and parameter-linked automation. Source hashing is opt-in; the set itself
is always hashed. Set-file identity is checked for changes during inspection;
source headers and opt-in hashes are checked together for stability. Runtime
results include local paths and belong outside public source repositories.

Canonical clip IDs have the form `track:10/clip:0`. Repeated local clip IDs gain
an instance suffix, such as `track:10/clip:0@1`. IDs are scoped to the inspected
file, not a persistent cross-edit identity. Lookup also accepts
`{"track_id": "10", "clip_id": "0"}` when exactly one instance matches;
ambiguity raises `PocketError`.

## Source coordinates

For supported non-looping warped audio, at Arrangement beat **q** the local source
pulse is `LoopStart + q - CurrentStart`. The complete piecewise-linear warp map
then maps that pulse to original source seconds. Endpoint slopes may extrapolate;
decoded source bounds are reported independently rather than silently clamping a
small native rounding discrepancy. Negative LoopStart values preserve pickups.
A quarter-note lattice origin is **not independent evidence of musical bar one**.

For supported Warp-off audio, LoopStart is in **seconds**. The source advances by
elapsed host time from CurrentStart to q. Nonconstant tempo is integrated as
`60 / BPM(beat)`; linear BPM ramps in beat coordinates are not linear time ramps.
Dormant warp markers on these clips remain in the inventory but do not drive
source mapping. Saved LoopEnd and the arrangement-implied source end are separate
observations: a tiny mismatch alone does not prove truncated render samples.

`source_position(set_map, clip_id, arrangement_beat)` and
`arrangement_position(set_map, clip_id, source_seconds)` return `status: mapped`
with coordinate evidence, or `status: unknown` with a reason. Positions outside
the selected clip and nonfinite requests raise `PocketError`. The physical end
is accepted for inspection and inversion. Session/MIDI clips, freeze caches, enabled loops,
nonzero or absent StartRelative, active grooves, tempo-leader interactions and
unwarped transposition are not mapped. Curved/unknown tempo event encodings are
retained but rejected for host-time integration. Warped source coordinates can
still be known when elapsed host seconds are unknown. Freeze-cache clip instances
are retained separately and are not counted as additional Arrangement clips.

All event attributes and negative initial events are retained in their saved
order. Duplicate tempo times, ambiguous targets and an unverified initial-sentinel
ramp are rejected rather than silently flattened or assigned a default tempo.
This is a saved coordinate model, not a sample-exact simulation of warp DSP,
groove execution, plugin latency or native rendering precision.

## Dependencies and devices

Audio SampleRefs and Max patch files are potential runtime loads. Missing or
conflicting references are flagged separately from dormant preset/source-context
provenance. A missing historical stock preset path is not mislabeled as missing
active audio. Unknown reference kinds stay unclassified. Relative project paths
and saved absolute paths are shown; conflicting existing targets remain
ambiguous. Muting a track does not prove its referenced media is unnecessary.

Stock device scalar settings and parameter targets are inventoried, including EQ
band configuration. Max and unsupported plugin/device DSP receive explicit
coverage warnings. Nested devices are enumerated, but rack signal selection,
sidechains, external plugin binaries, Max dependency caches, modulation and all
audible consequences are not resolved. Device coverage never means its sound was
rendered or approved.

Generated tests cover piecewise warp inversion, pickups, changing-tempo natural
playback, negative automation, multiple clips, duplicate local IDs, dependency
classification, MIDI state and explicit unsupported cases. No user recordings or
machine-specific fixture paths are shipped.

## Bounded queries and persistent handles

The full `inspect_set` library call above remains compatible. For normal agent
work, use `pocket_music.set_queries`:

```python
from pocket_music.set_queries import (
    inspect_set_summary, find_clips, query_set_region, export_set_map,
)

summary = inspect_set_summary("mix.als", cache_dir="private-map-cache")
handle = summary["handle"]
clips = find_clips(handle, "opening", limit=10)
region = query_set_region(handle, "2:22", 32)
# Optional: a new full inventory artifact, never echoed through the handle.
receipt = export_set_map(handle, "full-inventory.json")
```

Public signatures:

- `inspect_set_summary(path, *, cache_dir=None, hash_sources=False)`
- `find_clips(handle, query="", *, limit=20, offset=0)`
- `query_set_region(handle, start_seconds, duration_seconds=32, *, max_clips=16,
  clip_offset=0, max_events_per_lane=12, max_bytes=16000)`
- `export_set_map(handle, output_path)`

`SetHandle` is a discoverable `TypedDict`: schema `pocket.set-handle/v1`, absolute
`cache_path`, `cache_sha256` and `set_sha256`. It is a small JSON value that works
in a new process; no global interpreter registry or full-map echo is required.
The cache is a new gzip JSON snapshot, bound by its complete hash. By default it
lives in `$XDG_CACHE_HOME/pocket/set-maps` (or `~/.cache/pocket/set-maps`). These
artifacts contain local paths and must stay outside public repositories. Each
inspection writes a new file. Export also refuses to overwrite existing files.

Every operation verifies the cache and saved ALS hashes. Active and unclassified
references are checked against device/inode/size/mtime/**ctime**, lexical symlink
and target versions, including absent paths and project-marker existence. A
changed, replaced, disappeared or newly appearing candidate invalidates the
handle; callers must inspect again. Restoring a file's mtime does not evade its
ctime/replacement check. Source hashing is optional: without it, a reference is
bound to an observed local filesystem version, **not claimed as a recording-byte
asset identity**. With it, the initial full source hashes are retained. No claim
is made against privileged manipulation of filesystem metadata or mutation after
an operation returns.

The summary separates audio mapping support, MIDI placement inventory,
automation target resolution, active filesystem reference status, unreadable
headers, missing relative candidates, dormant provenance, and unevaluated DSP.
An existing absolute reference does not prove that Live loads it when another
saved relative location is absent. Native loaded-media status remains
`not_verified`; successful queries never constitute a native trial receipt.

Region starts accept nonnegative seconds, `mm:ss[.fraction]`, or `hh:mm:ss`.
Duration is positive and at most 600 seconds. The half-open region intersects
physical Arrangement audio/MIDI clip instances; it does not assume these are
sequential records or audible layers. Source intervals are returned for supported
audio. MIDI includes placement, loop fields and note count; full note/expression
structures remain in the raw snapshot. Unsupported tempo prevents seconds-based
selection and yields an explicit unknown result. Loops, grooves, unsupported
pickup semantics and unwarped transposition retain their existing mapping limits.

Controls cover the selected tracks and Main: manual mixer values, resolved
parameter names, bracketing/inside envelope knots, exact linear numeric window
start/end/min/max when supported, relevant EQ band configuration and Utility
manual settings. Negative initial knots are retained when they bracket a query.
Nonlinear/ambiguous event shapes are not numerically interpreted. Device and
routing state does not simulate group/return processing, modulation or DSP.

Truncation is explicit. Clip discovery pages by `offset`; region clips page by
`clip_offset`. Event lists retain boundary brackets and disclose the relevant
count and omissions. The complete linear window extrema are computed before
truncation. The compact UTF-8 JSON byte budget can omit whole control tracks or
clips, with their counts/IDs disclosed. No query returns a nonadvancing cursor:
if the budget cannot hold even one overlapping clip, it asks for a larger budget
or a raw export. Increase limits deliberately for complete event traces. The
16 KB default leaves room for ordinary transport overhead; callers must still
respect the truncation flags. It does not guarantee a fixed MCP wire size for
arbitrary projects, paths or serialization.

## Complete source-frame intervals

`pocket_music.source_frames.source_frame_interval(start_seconds, end_seconds,
 sample_rate, source_frames)` returns raw coordinates plus inward complete frames:
`[ceil(start * rate), floor(end * rate))`. It does **not** expand a source crop to
cover partial edge samples. One-ULP arithmetic handling matches Track Map.

The named policy
`inward_complete_frames_with_0.1_sample_file_boundary_snap/v1` allows only a raw
coordinate just outside source zero or EOF, by at most **0.1 sample**, to snap to
that file boundary. Every adjustment and its size are recorded. Interior
fractional positions do not gain this tolerance. Genuine negative/out-of-file
intervals return `out_of_bounds`; intervals containing no complete frames return
`empty`; unavailable headers or unsupported source mappings remain `unknown` in
region queries. Raw mapping results from `source_position` remain unchanged.

For a valid interval, use integer `analyze_region_frame_args` (`start_frame`,
`frames`) as the preferred tool-to-tool handoff. The result also includes
`analyze_region_args` in seconds only when a checked inward rounding roundtrip
preserves the same frames; otherwise that compatibility value is null. The
source path is the sibling clip `source.path`. This frame contract describes
original recording samples, not sample-exact rendered warp DSP or plugin latency.

Generated query tests cover cross-process handles, invalidation after same-size
source writes with restored mtime, file replacement, symlink retargeting, newly
appearing media, cache corruption, changing-tempo natural regions, warped
pickups, MIDI/layers, ambiguous targets, event/byte truncation and complete-frame
boundaries. Production acceptance artifacts remain private and outside Git.
