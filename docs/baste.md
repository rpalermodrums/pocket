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

Every response that reached the reader also says how it cleaned up after
itself: `live_objects_created`, `live_objects_released`, `release_elapsed_ms`
and `release_error`. [Keep Live responsive](#keep-live-responsive-across-observations)
explains them.

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
Once the result is complete, the reader releases every object it built by
clearing its path, as described below. No objects or observations persist
between calls. Values can change during traversal: the result
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

## Keep Live responsive across observations

Each observation builds a LiveAPI object for everything it reads: every track,
clip slot, clip, device, chain and parameter, plus one for each owner it
re-checks at the end. A set with a few dozen tracks can mean thousands of
objects in one read.

Live keeps a listener armed on every collection along a LiveAPI object's path
until that path is cleared. Freeing the object in Max (`freepeer()`) or leaving
it to garbage collection doesn't clear it. Each armed listener is told about
every later change to the set's structure, so objects left behind make ordinary
edits in Live slower. [Producer Pal](https://github.com/adamjmurray/producer-pal),
another Max for Live project, measured this on Live 12.4. With 7,190 objects
held, adding and deleting a track went from 120 ms to 630 ms. After 500
unreleased reads, a single track delete wrote 26 MB to Live's log. Those
figures come from Max's `v8` object. Baste runs in the older `js` object, so
they're the reason for this behavior, not a measurement of Baste.

So once a read's result is complete, including the final identity re-check,
Baste releases every object it built. It assigns each one an empty path, reads
the path back and counts the objects that cleared. Failed reads release their
objects too. The record reports:

| Field | Meaning |
|---|---|
| `live_objects_created` | LiveAPI objects this read built. |
| `live_objects_released` | Objects whose path read back empty after release. |
| `release_elapsed_ms` | Time spent releasing, after `read_elapsed_ms` ends. |
| `release_error` | The first release that failed, or `null`. |

A shortfall doesn't change the disposition or the observation, because the read
was already complete. It means some listeners may still be armed. Producer Pal
found that only reloading the device cleared them: remove the Baste device and
add it again before you rely on Live's responsiveness.

Clearing a path is the only write Baste makes to a LiveAPI object, and the value
is always the empty string. It points that object at nothing and leaves the set
unchanged. The reader's facade still offers only identity, `get` and `getcount`.

Two costs remain. Producer Pal measured that each construction registers a
context in Live's Max integration that only a device reload clears, and that
Live's memory grows by about 3.2 KB for every object resolved, whatever happens
to it afterwards. They avoid the first by retargeting a pool of released
objects. Baste doesn't pool: a pool would keep objects between reads and depend
on retargeting behavior nobody has checked in the `js` object. Pooling is a
follow-up if native measurement shows reads slowing down across a series. Only
resolving fewer objects would reduce the memory growth.

### Measure it in Live

Generated tests show that the reader releases everything it builds. They can't
show what that does for Live. No native measurement of this release has been
recorded yet. To take one:

1. Agree a time with anyone else using the Live instance. Open an isolated test
   set that's big enough for a read to build a few thousand objects (an
   observation's `objects_read` shows how many), and save it.
2. Build two devices into new directories with `pocket baste-device --output`:
   one from a checkout of the commit before this release was added, and one
   with it.
3. Load one device, then run a series of observations with the
   [measurement example](../examples/measure_baste_observations.py):

   ```sh
   python examples/measure_baste_observations.py /path/to/new/private-directory \
       --observations 30 --device-label before --live-version 12.x.y \
       --live-log /path/to/Live/Log.txt --edit-checkpoints
   ```

   Before and after the series it asks you to add one track in Live and delete
   it. Time that a few times with a stopwatch, type the median and press Enter.
   It records Live's memory after each observation and how much Live's log grew
   across each edit. On macOS, `Log.txt` is in the folder for your Live version
   under `~/Library/Preferences/Ableton`. The example writes numbers only, never
   names, values or the log's location.
4. Restart Live, load the other device and repeat.

Add the results to this section as a table: Live's version, the set's size,
and for each device the add-and-delete time and log growth before and after the
series, Live's memory before and after, the first and last read times, and
`live_objects_unreleased`. Leave out names and local paths. If reads get slower
across the series while every object is released, construction cost rather than
listeners is the likely cause.

## Verify changes

```sh
node --test tests/baste_reader.test.cjs
python -m pytest -q tests/test_baste.py tests/test_baste_pipette_interfaces.py
```

Generated tests exercise the unchanged reader with a capability-trapping fake,
both clip views, racks, value/display differences, missing objects, topology edits,
resource bounds, dictionary lifetime, the release of every LiveAPI object after
each read (failed reads included) and the actual HTTP transport. They do not
prove native behavior. Native acceptance uses an isolated project, GUI comparison,
a change since Save, repeated observations, saved-byte/mtime preservation and the
measurement above. A change to the reader needs native acceptance again.

See [Thread](thread.md) for reading the saved file and [Pipette](pipette.md)
for the separate saved-candidate promotion workflow.
