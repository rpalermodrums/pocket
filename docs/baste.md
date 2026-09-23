# Baste: observe the open Live session

> **In brief.** In sewing, basting is a quick, temporary stitch that holds
> things in place so you can take a look. Baste does the same for the Live set
> that's open right now, including unsaved changes. A small Max for Live device
> reports tracks, clips, devices and parameters as they are at this moment.
> Baste can't change anything in Live. Each look is a snapshot and never becomes
> a saved reference.
>
> **You need** macOS with Ableton Live 12 Suite (which includes Max for Live).
> For a saved `.als` file, use [Thread](thread.md) instead.

Baste reads the currently open, possibly unsaved Live session through a real Max
for Live device. Thread remains the reader for a saved `.als`. Neither observation
is a substitute for the other, and neither establishes what the audio sounds like.

## Install the editable device

The first supported host is macOS with Ableton Live 12 Suite / Max for Live.
Python uses the local `ps` command for process presence; Windows process discovery
is not implemented. Live's Max installation supplies Node for Max and `max-api`.
No separate npm package, remote service, model or Remote Script is required.

```sh
pocket baste-device --output /path/to/new/Baste-device
```

Load the generated `Baste.amxd` on an audio-capable track. Keep it beside
`Baste.maxpat`, `baste_reader.js`, `baste_device.js` and `baste_bridge.js`.
The patch passes stereo input directly to output. Building it does not load it
or change Live. Loading a device is a deliberate session edit by the operator;
subsequent observations have no Live mutation capability.

Load **one** Baste device per Live process. Wait for Live to finish loading the set.
Do not move its scripts independently or assume Collect All and Save discovers
opaque JavaScript dependencies. The source patch and scripts ship in the Python
wheel, so the device is reproducible and editable.

```sh
pocket baste
pocket baste --output /path/to/new-observation.json
# Optional shorter deadline for a small set:
pocket baste --timeout-seconds 10
```

The Python equivalent is `pocket_music.observe_live(timeout_seconds=35)`.
MCP exposes `baste` with the same arguments and `baste_build_device(output_dir)`.
The response is a single compact JSON record. There is no saved handle, path
evaluation input, arbitrary LiveAPI command, subscription or replay endpoint.

## Read the result

`pocket.live-observation/v1` includes a fresh `request_id`, UTC request/receipt
times, device read start/end times, elapsed milliseconds, a disposition and an
observation. Device read timing is available only when a device reply arrived.

An `ok` observation includes regular tracks, return tracks and Main. Tracks carry
Session slots (including empty slots), Arrangement clips, devices, nested rack
chains and exposed device parameters. Parameters retain `value`, numeric
`display_value`, `min`, `max` and `automation_state`.
`display_value` is Live's displayed numeric representation, not a formatted unit
label. Rack devices remain attached to their chain; device enabled state is Live's
`is_active` value. Opaque plugin state and hidden DSP internals are outside scope.

Arrangement start/end/length are in beats. Session clips have no Arrangement
start, so those fields are null. Markers retain `clip_beats` for warped audio/MIDI
and `source_seconds` for unwarped audio. This does not establish a recording hash,
source crop mapping, beat one or phrase start. Clip/runtime IDs and paths describe
only this read. Do not carry them into a later edit as stable identities.

| Disposition | Meaning / next step |
|---|---|
| `ok` | Complete supported read; inspect its timing and scope. |
| `ableton_not_running` | The OS process check found no Live process. |
| `device_not_loaded` | Live is running, but no ready reachable device was found. |
| `path_invalid` | A path disappeared or topology changed during the read; retry after edits settle. |
| `unsupported_property` | Live did not return a required supported value; inspect the named property. |
| `read_limit` | Object/read/time budget exceeded; reduce session complexity. |
| `observation_timeout` | The requested deadline elapsed; no prior or partial observation is reused. |
| `ambiguous_devices` | More than one ready connection exists; keep one device active. |
| `bridge_error` | Malformed discovery/response, transport error, or a busy device. |

Failure responses carry `observation: null`, rather than an empty successful
session. The reader limits work to 30,000 objects, 300,000 counted reads and 30
seconds between budget checks. Python accepts a 0.1–60 second transport timeout
(default 35 seconds). A single host call cannot be preempted; these are resource
bounds, not a real-time scheduling guarantee. Reading a large set takes longer.
When a read reaches one of these bounds, Baste reports a failure rather than
returning part of a session.

## Read-only boundary and transport

The Max reader constructs fresh LiveAPI objects on the low-priority thread after
`live.thisdevice` signals readiness. Its capability facade permits identity,
`get` and `getcount` reads; it cannot call, set or navigate a Live object. It
re-resolves owners and child-ID lists/counts before success to detect topology
changes without constructing a second proxy for every parameter. Parameter
proxies use IDs read in this request to avoid repeated deep path resolution.
No objects or
observations persist between calls. Values can change during traversal: the result
is explicitly sequential, not atomic, durable, or a revision token.

Node for Max only carries requests and dictionaries. A private wire envelope
carries JSON in bounded Unicode-safe string chunks: Max's dictionary atom
conversion must not change public boolean fields into numbers. It listens on an ephemeral
IPv4 loopback port with a random bearer token. A mode-0600 discovery file in
`~/.pocket/baste` stores connection details, never session data. `POCKET_BASTE_DIR`
changes that directory for both processes; `--bridge-dir` changes client discovery
only. The device must inherit a custom environment setting when Live starts.
Keep discovery files private. Browser-origin requests and unknown routes are
rejected. Dead descriptors are ignored; live ambiguous/malformed descriptors fail
explicitly. The bridge admits one pending request, validates nonces and releases
reply dictionaries. A client timeout keeps the read slot busy until Max actually
finishes; repeated short deadlines cannot queue overlapping host reads. A host
call that never returns requires reloading the device. The bridge does not write
a saved set or issue Live commands.

Implementation follows Cycling '74's [LiveAPI reference](https://docs.cycling74.com/apiref/js/liveapi),
[DeviceParameter reference](https://docs.cycling74.com/apiref/lom/deviceparameter/),
[Track reference](https://docs.cycling74.com/apiref/lom/track/) and
[Node for Max API](https://docs.cycling74.com/apiref/nodeformax).

## Verify changes

```sh
node --test tests/baste_reader.test.cjs
python -m pytest -q tests/test_baste.py tests/test_baste_pipette_interfaces.py
```

Generated tests exercise the unchanged reader with a capability-trapping fake,
both clip views, racks, value/display differences, missing objects, topology edits,
resource bounds, dictionary lifetime and the actual HTTP transport. They do not
prove native behavior. Native acceptance uses an isolated project, GUI comparison,
a change since Save, repeated observations and saved-byte/mtime preservation.

See [Thread](thread.md) for reading the saved file and [Pipette](pipette.md)
for the separate saved-candidate promotion workflow.
