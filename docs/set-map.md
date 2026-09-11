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
