# Listen: watch the audio that's actually there

> **In brief.** Listen copies live audio from the inputs you declare and reports
> what it can measure: levels, clipped samples, near-silence and whether audio
> is still arriving. Every level event carries its exact position in the
> stream and when it was received, and every `frames_missing` event its exact
> start and length. Listen
> never changes a device, a route or a deck, saves no audio, and isn't in the
> path to your speakers or headphones.
>
> **You need** macOS or Linux. The synthetic adapter needs no hardware. Reading
> a real input uses an experimental macOS helper that isn't qualified yet; it
> needs macOS 15 or later and the Swift compiler.

Listen is experimental. It runs one small local runtime process per session,
plus a copy of the helper when it reads a real input. Only `listen_start`
starts the runtime, and it stops when you stop it, at its time limit
(`limits.max_runtime_seconds`, 4 hours by default), if it fails, or if its
process is signalled to end. You read
what it saw through bounded snapshots and pages of events.

## What Listen does and won't do

Listen reports [measured evidence](concepts.md#four-kinds-of-evidence) about
samples. It doesn't make musical judgements.

- Near-silence means the samples stayed below a level you chose for a duration
  you chose. It doesn't mean a deck stopped, a dropout happened or a track
  ended.
- Listen doesn't observe physical headphones and doesn't listen the way a
  person does. Every receipt and event says so in its coverage block.
- Its events grant nothing. An agent or another tool can read them, but Listen
  itself controls nothing: it changes no device, routing or transport.
- No audio file is written. Recent audio sits in a bounded in-memory window
  that disappears when the runtime stops.

## Audio routes

An [audio route](concepts.md#glossary) is a set of input channels with a role:

| Role | What Listen does with it |
|---|---|
| `master` | Meters it. The label comes from your setup, so Listen reports it as `declared_unverified`. |
| `cue` | Records whether the channels exist, and nothing else. In this version cue isn't monitored: no routing test has shown that those channels carry cue audio, so Listen never meters them, keeps them or reports their levels. |
| `unlabeled` | Meters it, with no claim about what the channels carry (`no_semantic_claim`). If the input doesn't have those channels, the route reads `unavailable` with reason `channels_not_exposed`, and the other routes keep capturing. |

To see levels on channels you'd call cue, declare them as an `unlabeled` route
instead. Listen then reports their levels without calling them cue.

A few rules keep routes unambiguous:

- Channels count from 0. Channel 0 is the device's first input; Audio MIDI
  Setup shows it as 1.
- One channel belongs to one route, so a channel never carries two labels.
- You can declare up to four routes and four channels in total, with one or
  two channels per route: at most one master, one cue and two unlabeled
  routes, and at least one master or unlabeled route.
- A route ID with a word that starts with `cue` or `master`, such as
  `cue-left`, `cue1` or `master2`, must have that role.
- Inputs are chosen by uid. Names are only for display, since two inputs can
  share a name; `listen_devices` flags shared names.

## Try it without hardware

The synthetic adapter plays a script you write. It is deterministic, and
everything it produces is labeled `synthetic_script`. These steps use the
command line; [from Python and MCP](#from-python-and-mcp) shows the same calls
there.

List the script's inputs. Nothing is opened:

```sh
pocket listen devices --spec devices.json
```

```json
{"adapter": {"kind": "synthetic", "script": {"block_frames": 480,
  "devices": [{"uid": "synthetic:duo-a", "name": "Practice Duo", "input_channels": 4, "sample_rate": 48000}],
  "segments": [{"kind": "tone", "frames": 480000,
                "tones": [{"channel": 0, "hz": 480.0, "dbfs": -12.0},
                          {"channel": 1, "hz": 480.0, "dbfs": -12.0}]}]}}}
```

This script is one four-channel input that plays ten seconds of a −12 dBFS
tone on channels 0 and 1 and then ends. Seal a session for it into a new
directory. Preparing runs nothing and opens nothing:

```sh
pocket listen prepare --spec prepare.json --output NEW_SESSION
```

```json
{"config": {
  "adapter": {"kind": "synthetic", "script": {"block_frames": 480,
    "devices": [{"uid": "synthetic:duo-a", "name": "Practice Duo", "input_channels": 4, "sample_rate": 48000}],
    "segments": [{"kind": "tone", "frames": 480000,
                  "tones": [{"channel": 0, "hz": 480.0, "dbfs": -12.0},
                            {"channel": 1, "hz": 480.0, "dbfs": -12.0}]}]}},
  "device": {"uid": "synthetic:duo-a", "expected_sample_rate": 48000},
  "routes": [{"route_id": "master", "role": "master", "channels": [0, 1]},
             {"route_id": "cue", "role": "cue", "channels": [2, 3]}],
  "measurement": {"clip_threshold_dbfs": -0.1,
                  "near_silence": {"level_dbfs": -60.0, "min_duration_ms": 2000}}}}
```

Listen creates `output_dir` as a new directory that only you can open, and
refuses one on a volume that ignores ownership. A name that already exists,
even as a symlink, is refused rather than followed, so a session never reuses
or overwrites anything.

The clip threshold and the near-silence level and duration have no defaults:
you say what counts. The receipt echoes the resolved `device`, `measurement`,
`buffering`, `health`, `delivery` and `limits` settings under
`session.settings`, with the session's `config_sha256`. The sealed session
record, `artifacts.session`, holds the whole resolved config, including the
adapter's defaults, such as a synthetic script's `host_drift_ppm` of 0. The
receipt also declares the session's sizes, which are configured, not
measured. `memory` gives the byte size of each audio buffer, including
measurement's scratch window, and of event retention, with their total;
queues bounded by a count are listed by count under `not_in_total`. `disk`
gives the budgets of what one runtime writes in the session, its journal and
stderr, as `per_runtime_total_declared_bytes`, and every start that launches a
runtime adds a new runtime directory; a conflict or a replayed request adds
none. The small sealed records each runtime also writes, such as its
launch record and terminal, have no declared size and are named under
`not_in_total`.

Start the runtime with a new request ID and that SHA:

```sh
pocket listen start --spec start.json
```

```json
{"session_dir": "NEW_SESSION", "request_id": "start-1", "expected_config_sha256": "…from prepare…"}
```

The receipt says `"start_outcome": "capturing"`, names the runtime in
`runtime.runtime_epoch` and gives a [cursor](concepts.md#glossary),
`{"runtime_epoch": "rt-…", "sequence": 0}`. Look at what's arriving:

```sh
pocket listen observe --spec observe.json      # {"session_dir": "NEW_SESSION"}
```

The observation's `summary` is one line, such as `master: receiving, peak
-12.0 dBFS · cue: not monitored (no routing test) · stream 1, 0 missing frames
· last block 4 ms ago`, and the receipt carries a fresh cursor. The last part
is the age of the newest block, or `no block yet`. On the command line the
keys come in sorted order, so `summary` is near the end, and `·` prints as
`\u00b7`. Read events after a cursor:

```sh
pocket listen events --spec events.json
```

```json
{"session_dir": "NEW_SESSION", "after": {"runtime_epoch": "rt-…", "sequence": 0}, "wait_ms": 500}
```

The first page starts with `runtime_started`, `capture_opened` and
`stream_started`, then level events for `master`. Each event has a `sequence`;
pass the page's `next_after` as the next `after`. When the script ends, capture
ends with `end_of_stream` and the runtime keeps answering until you stop it.
Stop the runtime you named:

```sh
pocket listen stop --spec stop.json
```

```json
{"session_dir": "NEW_SESSION", "request_id": "stop-1", "expected_runtime_epoch": "rt-…"}
```

The stop seals a terminal record in the session and returns its handle under
`artifacts.terminal`. Every receipt and event carries a coverage block. Some
receipts add a field, such as `capture`, but these are always there:

```json
{"audio_source": "synthetic_script", "physical_headphones": "not_observed", "headphone_cue": "not_observed",
 "human_listening": "not_performed", "route_semantics": "not_verified", "control_issued": false,
 "device_settings_changed": false, "audio_written_to_disk": false, "native_qualification": "not_established"}
```

`route_semantics` is always `not_verified`: no routing test has checked what
any route carries. Each route's `semantic_status` says what its role claims.

`audio_source` is `synthetic_script` on every record of a synthetic session.
With the macOS helper it is `device_input` once a runtime has received a block
from the input: on its observations, events, event pages and terminal from
then on, and on the receipts built from them. It is `none` on records that
describe no audio: a refused or conflicting start, a start that failed before
capture or whose input sent nothing, a runtime's records from before its first
block (`capture.frames_received` is 0), a stop with no terminal, a read or a
stop with nothing running, and `listen_request_status`.

### Script a fault

A script is a list of segments played in order, so you can rehearse what Listen
reports when something goes wrong. This one plays two seconds of tone, loses
512 frames, goes silent for three seconds and then loses the device:

```json
"segments": [{"kind": "tone", "frames": 96000, "tones": [{"channel": 0, "hz": 480.0, "dbfs": -12.0}]},
             {"kind": "gap", "frames": 512, "via": "device_gap"},
             {"kind": "silence", "frames": 144000},
             {"kind": "device_lost"}]
```

Its events include an exact `frames_missing` of 512 frames at stream frame
96000, a `near_silence` that starts after two seconds of complete quiet windows
and is interrupted when the device goes, and `stream_ended` and
`capture_ended` with cause `device_lost`.

| Segment `kind` | Fields | What it simulates |
|---|---|---|
| `tone` | `frames`, `tones` (`channel`, `hz`, `dbfs`) | Sine tones |
| `ramp` | `frames`, `channels`, `period_frames`, `dbfs` | A sample-exact repeating ramp |
| `silence` | `frames` | Exact zeros |
| `impulse` | `frames`, `impulses` (`channel`, `offset_frames`, `dbfs` up to +12) | Single-sample peaks, for clipping |
| `nonfinite` | `frames`, `channels`, `offsets`, `value` (`nan`, `inf` or `-inf`) | Samples that aren't numbers |
| `gap` | `frames`, `via` (`device_gap`, `helper_ring_overflow` or `processor_overload`) | Frames that never arrive |
| `stall` | `ms` (50 to 30000), `counter` (`advances` or `pauses`) | No audio for a while |
| `counter_reset` | `sample_time` (optional; 0 by default) | The device's sample counter starting again from a lower value |
| `counter_backward` | `delta_frames` (negative) | The device's sample counter jumping back |
| `clocks` | `sample_time_valid`, `host_time_valid` | Host time appearing or disappearing; with `sample_time_valid: false`, the next block ends the stream and capture with `device_sample_time_unavailable` |
| `rate_change` | `sample_rate` | A sample-rate change, which ends the stream |
| `channel_reorder` | `order` | A channel-layout change, which ends the stream |
| `device_lost`, `hogged` | none | The device going away, or another app taking it for exclusive use |

A segment that ends the stream comes last. A script lists 1 to 8 `devices`,
each with a `uid` starting `synthetic:`, a display `name`, 1 to 16
`input_channels` and a `sample_rate`. Give two of them the same name to see
shared names flagged. `block_frames` is 32 to 8192, a script holds up to 256
segments and four hours of frames at the lowest `sample_rate` it lists, and an
optional `host_drift_ppm`, 0 by default, makes the device's clock run fast or
slow.

A `counter_reset` needs frames before it. Its `sample_time` must be below the
scripted counter, which gap frames and stalls that advance it also move, and
below one second of frames at the script's lowest `sample_rate`. It reads as
a new stream only when it lands below where the delivered frames end: landing
there exactly continues the stream, and landing past it reads as a
`frames_missing`.

## From Python and MCP

The same providers are Python functions and MCP tools with the same names and
arguments. With `pocket listen prepare`, `output_dir` comes from `--output`.
The flat command `pocket listen-prepare` takes `output_dir` in the `--spec`
file like any other argument, and its `--output` names a new file for the
response JSON. In Python and MCP it is an ordinary argument.

```python
from pocket_music.listen import listen_events, listen_observe, listen_prepare, listen_start, listen_stop

prepared = listen_prepare("NEW_SESSION", config)        # config is prepare.json's "config"
session_dir = prepared["session"]["session_dir"]
started = listen_start(session_dir, "start-1", prepared["session"]["config_sha256"])
page = listen_events(session_dir, started["cursor"], wait_ms=500)
now = listen_observe(session_dir)
listen_stop(session_dir, "stop-1", started["runtime"]["runtime_epoch"])
```

In MCP, `listen_prepare` takes `{"output_dir": "NEW_SESSION", "config": {…}}`
and `listen_events` takes `{"session_dir": "NEW_SESSION", "after": {…}}`. MCP
checks argument types strictly, so an unknown key, or `true` where a number
belongs, is refused before Listen runs. `listen_observe`, `listen_events` and
`listen_request_status` are marked read-only. `listen_devices` isn't, because
with the CoreAudio adapter it runs a copy of the helper, but it's safe to
repeat. `listen_start` and `listen_stop` are safe to retry with the same
request ID. After installing a new version, restart a running MCP server to
register the tools.

## What each result tells you

Every provider returns a [receipt](concepts.md#stores-artifacts-and-handles).
Its `status` says how the call went. `listen_start`, `listen_stop` and
`listen_events` also name an outcome in `start_outcome`, `stop_outcome` and
`events_status`, and the rows below name the other fields that qualify a
result, such as `capture_snapshot` or `measurement_drain`.

| Result | What it establishes | What it doesn't establish |
|---|---|---|
| `listen_devices`, `ok` | The inputs an adapter lists, by uid, with their channels, rates and formats | That any input is carrying audio |
| `listen_devices`, `failed` (CoreAudio) | The macOS helper's device listing didn't complete; `probe.reason` says how (`nonzero_exit`, `invalid_output`, `output_too_large` or `timeout`) and `probe.stderr_tail` what it printed. No device or stream was opened | Which inputs exist |
| `listen_prepare`, `ok` | A sealed session: one uid, its audio routes and every setting | That the input is connected or runs at that rate; start checks |
| `listen_start`, `start_outcome: "capturing"` (`ok`, or `partial` when `capture_snapshot` is `unavailable`) | Frames arrived from the input with that uid, which reported the expected nominal rate when it opened | That the `master` channels carry your master mix, or the rate audio arrives at, which isn't measured |
| `listen_start`, `"started_no_audio_yet"` (`partial`) | The runtime is running and the input's IO started, but no frame had arrived when the receipt was written. `capture.state` is `stale` when capture has already gone stale, and the `capture_no_frames` notice says so | Why no audio arrived; check the source app and the device |
| `listen_start`, `"capture_ended_during_start"` (`partial`) | Capture ended or failed while the start waited, and `capture.ended_cause` says why, such as `device_lost`. It won't resume: stop this runtime, then start again | — |
| `listen_start`, `"stopped_during_start"` (`partial`) | A stop began while the start waited, so the runtime isn't capturing, even if frames had arrived. `terminal` summarizes its sealed record; while the runtime is still stopping, or its terminal doesn't verify, `stopped_runtime.terminal` is `null`; `listen_request_status` shows when it is sealed, and `listen_observe` then summarizes it | — |
| `listen_start`, `"capture_refused"` (`unsupported`, or `needs_input` when microphone access is the only reason and macOS reports it denied or gives no state) | Capture never started; `capture_refusal.reasons` says why, such as `sample_rate_mismatch`, `microphone_not_authorized` or `stale_after_below_block_period`, and `refused_at` says whether the device was refused when it opened, at route checks or when IO was about to start. A terminal record is sealed, and every route in it is `unavailable` | — |
| `listen_start`, `"already_running"` (`conflict`) | Another runtime holds this session; nothing new started | — |
| `listen_start`, `"start_unconfirmed"` (`outcome_unknown`) | The runtime holds the session's [lease](concepts.md#glossary), but it didn't answer within the start wait; `reason` says whether its connection record exists yet. It may still be starting: `listen_observe` shows whether it runs, and `listen_request_status` shows this request, which won't run again. While it holds the lease, until its process exits, a new start with a new `request_id` is refused as `already_running` | Whether capture started |
| `listen_start`, `capture_snapshot: "unavailable"` (`partial`) | The runtime answered, but its snapshot didn't arrive, so the receipt carries no routes or device | Anything about the capture; call `listen_observe` |
| `listen_observe`, `ok` | A fresh snapshot of capture, clocks, routes and delivery, with a cursor | That a level is current: check its `age_ms` and `fresh`, which count from the newest block in its interval |
| `listen_events`, `ok` | Nothing after your cursor was lost | That the audio is fine; read the health events and `health_summary` |
| `listen_events`, `events_status: "runtime_not_running"` (`needs_input`) | The cursor's runtime has stopped. `missing` names the sequences it published after your cursor, which can't be read any more. `cursor_runtime` says what Listen found for that runtime: an empty `missing` means nothing was lost only when its `terminal` is `sealed`, and the next step says so when it isn't | What those events said. A sealed terminal summarizes them. When the runtime sealed none, the range ends at the journal's last line and says `through_is_lower_bound: true`: later events, such as levels, may have been published and lost too. When its terminal doesn't verify (`integrity_mismatch`) or Listen doesn't know the epoch (`unknown`), `missing` is empty. The journal keeps their health events only when the range says `health_in_range: "runtime_journal"`; `runtime_journal_incomplete` means the journal lost lines: `cursor_runtime.journal` shows how, or the terminal's `broker.publish_hook_errors` counts events whose journal hook failed |
| `listen_stop`, `stop_outcome: "stopped"` (`ok`) | The named runtime stopped and its terminal record is sealed. `lease_released` is true when the session is free for a new start; if another runtime took it first, `superseded_by_runtime_epoch` names that runtime | — |
| `listen_stop`, `stop_outcome: "already_stopped"` (`ok`, or `partial` with a caveat below, such as a runtime that failed) | Another stop, such as the runtime's own limit or a failure, had already sealed the named runtime's terminal, however long ago; `stopped_by` says which | — |
| `listen_stop`, `stop_outcome: "not_running"` (`needs_input`) | Nothing named was running to stop: the epoch isn't one of this session's runtimes, the runtime was refused before capture ran, or it exited without a terminal record. That includes a stop that got no reply when the runtime then exited without one; `reason` then says so | Whether this stop reached the runtime. `runtime_known` says whether the epoch is one of this session's runtimes; `previous_runtime` is that runtime's record when it is, and the session's newest runtime's when it isn't |
| `listen_stop`, `stop_outcome: "stop_unconfirmed"` (`outcome_unknown`) | The stop got no reply from the runtime, and the runtime still held the lease 5 seconds later, so Listen can't say whether the stop reached it. `listen_observe` shows whether it still runs; `listen_stop` with a new `request_id` is safe, since it stops only the named runtime epoch | Whether the runtime stopped |
| `listen_stop`, `terminal_sealed: false` (`partial`) | The runtime stopped, but its terminal record wasn't sealed or doesn't verify; `reason` says which | — |
| `listen_stop`, `measurement_drain: "incomplete"` (`partial`) | The runtime stopped and its terminal is sealed, but measurement didn't reach every block within the stop bound. Health doesn't wait for measurement, so every gap and stream change capture found was published before `runtime_stopped` | Levels for `terminal.worker.unmeasured_at_stop_frames` frames (and any `lag_frames`); each route's totals cover only its `frames_measured` |
| `listen_stop`, a measurement overrun (`partial`) | The runtime stopped and its terminal is sealed, but measurement fell behind during the run: it skipped frames as a `measurement_overrun`, then caught up | Levels for those frames (`terminal.worker.unmeasured_frames` beyond the frames after a failure or at stop); each route's totals cover only its `frames_measured` |
| `listen_stop`, frames measurement lost track of (`partial`) | The runtime stopped and its terminal is sealed, but measurement lost track of `terminal.worker.unaccounted_frames` frames after its fact queue was full (`frames_unaccounted`) | Whether those frames arrived: a `frames_missing` event covers any that didn't, and any that did have no levels |
| `listen_stop`, `measurement_state: "failed"` (`partial`) | The runtime stopped and its terminal is sealed, but level measurement failed during it | Levels for `terminal.worker.unmeasured_after_failure_frames` frames; each route's totals cover only its `frames_measured` |
| `listen_stop`, `adapter_close` other than `clean` (`partial`) | The runtime stopped and its terminal is sealed, but the input adapter timed out, raised, exited with an error or had to be terminated or killed while closing. `terminal.stop.adapter_exit` gives what Listen learned: the helper's exit code and reason, or the `error` closing raised; after a timeout it learned nothing more | That the macOS helper stopped reading the input |
| `listen_stop`, held-back helper audio (`partial`) | The runtime stopped and its terminal is sealed, but the macOS helper exited holding audio it had captured and never sent: after a device change it sends none of what its ring still holds, which can be up to about 3 seconds when the runtime was behind reading it (the ring is the smallest power of two of frames that holds 2 seconds: 131072 frames at 44.1 and 48 kHz, 262144 at 88.2 and 96 kHz). `terminal.stop.adapter_exit.counters` gives `undelivered_blocks` and `undelivered_frames` | That audio: no event or frame total covers it, so capture's frames end before it |
| `listen_stop`, helper drops after the last block (`partial`) | The runtime stopped and its terminal is sealed, but the macOS helper counted frames it dropped after the last block capture took. `terminal.stop.helper_drops_after_last_block_frames` gives the count | Where they fell: no later block reached capture, so no `frames_missing` event covers them |
| `listen_stop`, a journal caveat (`partial`) | The runtime stopped and its terminal is sealed, but its journal stopped writing, dropped lines or hadn't finished writing when the stop closed it, or some events never reached it because their journal hook failed; `terminal.journal` gives `state`, `dropped_lines` and `lines_unwritten_at_close`, and `terminal.broker.publish_hook_errors` counts those events, health or near-silence | That every health event is on disk |
| `listen_start`, `"host_failed"` (`failed`) | The runtime exited during startup or failed while the start waited. For a startup failure, `startup_failure` gives its stage and error. When capture was refused but its terminal record couldn't be sealed, such as on a full disk, the stage is `refused` and `startup_failure.refusal` gives the reasons. Its `record` is `unverified` when the runtime couldn't finish sealing its startup-failure record, such as on a full disk, and `missing` when it wrote none; then `stage` is `null`. A runtime that exited before writing one, such as when it couldn't start Python or was killed, leaves `exit_code` and `stderr_tail`. A runtime that failed after its capture began writes no startup-failure record but seals a terminal: `startup_failure.error` comes from it, and `terminal` summarizes it | — |
| `listen_request_status` | What the request journal and runtime directories record | The runtime's current state; it never contacts the runtime |

When no runtime is running, `listen_observe` and `listen_events` return
`needs_input` with the last runtime's summary, including its sealed terminal's
handle as `terminal_handle`. When the runtime holds the
session but doesn't answer, they return `outcome_unknown`; Listen never signals
a process to find out.

## What the measurements mean

Levels use the `sample-peak-rms/v1` and `near-silence/v1`
[profiles](concepts.md#profiles-limits-and-refusals). They are computed per
channel over contiguous received frames, and an interval never spans a gap:
the device's sample counter places every frame (see
[Clocks, epochs and gaps](#clocks-epochs-and-gaps)).

| Measurement | What it supports | What it doesn't establish |
|---|---|---|
| Sample peak (dBFS) | The largest sample magnitude in the interval | The true peak between samples |
| RMS (dBFS) | The average power of the received samples | Perceived loudness |
| Clipped samples | How many samples reached your clip threshold | That anyone heard distortion |
| Non-finite samples | Samples that aren't numbers (NaN or infinity); that channel's levels stay empty and a health event says why | Which app produced them |
| Near-silence | Every complete window stayed below your level for at least your duration | A stopped deck, a dropout or an intended ending |
| Stale capture | At least `stale_after_ms` has passed since capture started, before the first block, or since the newest block capture has taken was received. A block received before that decision but taken by capture after it ends the episode at once, and its `capture_resumed` says `detail.received_before_stale_decision: true`. `stale_after_ms` must be more than twice the input's block period, or blocks arriving normally would read as stalls | That playback stopped, or that no audio reached the runtime |

A channel of exact zeros reports `null` dBFS with `digital_zero: true`, never a
floor value. True peak and loudness aren't measured, and records say
`"not_measured"`.

If level measurement stops after an internal error, capture health is still
reported, but each metered route turns `unavailable` with health
`measurement_failed` and near-silence `unknown`, because no level describes the
audio that keeps arriving. Every received frame the failure leaves without a
level is counted in `worker.unmeasured_after_failure_frames`, and `worker.error`
says what failed. That count serves every route, so with more than one
metered route it can also hold frames that some routes measured just before
the failure, up to the audio of one level event.

Measurement reads a bounded in-memory ring of recent audio and a bounded list
of block records. If it falls so far behind that capture overwrites audio or
records it hasn't reached, Listen skips ahead to half that buffer behind
capture and reports the frames it skipped as one `measurement_overrun`, so a
slow measurement catches up instead of reporting each lost block on its own.
While measurement trails capture by more than `stale_after_ms` of audio, each
route's near-silence reads `unknown`: the last window measured no longer
describes the audio arriving now. A route's `nonfinite` or `ok` health also
comes from the last window measured, so then the summary says how far behind
capture that audio is, and for a `nonfinite` route so does the
`nonfinite_input` notice.

## Reading events

`listen_start` and `listen_observe` both give you a cursor. Every receipt from
`listen_events` says plainly whether anything was lost:

- **`ok`**: nothing after your cursor was lost. A page with no events and zero
  `filtered` counts means nothing new was published during the wait. `filtered`
  says how many level or near-silence events your `routes`, `kinds` or
  `max_evidence_age_ms` skipped, and while `more_available` is true, keep
  paging with `next_after`.
- **`cursor_expired`** (status `conflict`): level or near-silence events after
  your cursor were dropped before you read them, so the page carries no
  events. Repeat with `"on_expired": "resume_from_earliest"` to page on: that
  page delivers the health events still after your cursor, up to its
  `max_events` and `max_bytes`, and its status is `partial`. Its `missing` is
  one range, from your cursor to where the dropped events end, with
  `health_in_range: "delivered"`: the health events in it are on the page, and
  the rest of the range, other than events your filters skip, was lost. Keep
  passing `resume_from_earliest` while `next_after` is still inside that
  range, since each page from there finds dropped events again. A
  fresh cursor from `listen_observe` starts at the newest event, so it skips
  any health events in between. A poll with `"kinds": ["health"]` never gets
  `cursor_expired`: it asked for no level events, so the ones dropped are
  counted in `filtered.kind` like the ones it skips.
- **`snapshot_required`** (status `conflict`): health events after your cursor
  were evicted before you read them. Call `listen_observe` and use its cursor.
- **`runtime_epoch_mismatch`** (status `conflict`): a runtime is running, and
  the cursor names a different runtime epoch. When that epoch is an earlier
  runtime of this session, `cursor_runtime` and `missing` say, as with
  `runtime_not_running`, what it published after your cursor before it
  stopped. When it isn't, such as a cursor from another session,
  `cursor_runtime.terminal` is `unknown`, and the next step says Listen can't
  rule out loss.
- **`runtime_not_running`** (status `needs_input`): the cursor's runtime has
  stopped. `missing` lists the sequences it published after your cursor, which
  can't be read any more. When that runtime sealed its terminal, the range ends
  at its last event and the terminal summarizes them. When it sealed none, such
  as after a crash, the range ends at its journal's last line with
  `through_is_lower_bound: true`, since the journal holds no levels; later
  events may be lost too, and only the journal remains. Then an empty
  `missing` doesn't mean nothing was lost, and the next step says so.
  `health_in_range` is `runtime_journal` when that runtime's journal kept
  every line, so its health events are on disk, and
  `runtime_journal_incomplete` when it didn't. `cursor_runtime.journal` shows
  a journal that stopped writing, dropped lines or didn't finish; when events
  never reached the journal because their journal hook failed, that runtime's
  sealed terminal counts them in `broker.publish_hook_errors`, and the next
  step points there. The journal never holds its own `journal_failed` or
  `journal_exhausted` events, or anything after them.

Health events, such as stale capture, gaps and stream changes, are never
filtered by kind, route or age, so `kinds` must include `"health"`. If any are
evicted, the poll returns `snapshot_required` instead of a page, so a page
never hides missing health.

Capture publishes its health events as it finds them, whatever measurement is
doing. A gap or a stream change is in the events while measurement is behind
or after it has failed, and always before any level for the audio after it.
Levels come later, so a level for audio before a gap can follow that gap's
`frames_missing`. Capture's health events keep the order it found them in: a
stale episode's `capture_stale` and `capture_resumed` always alternate, and every
`stream_epoch` an event names was announced by an earlier `stream_started`.

`helper_unresponsive` says the runtime has read no heartbeat from the macOS
helper for 2 seconds, and `helper_responsive` says one arrived again. The
runtime reads heartbeats in order with the audio, so if it falls behind
reading the helper's audio, a heartbeat waits too. The event's
`detail.reader_backpressured` is `true` when the runtime's own reader had to
wait for room in its queue at any point since it read that last heartbeat, so
at least part of the delay is the runtime's, not the helper's.
`capture.helper_unresponsive` in `listen_observe` shows which holds now, or
`unknown` when the heartbeat isn't being checked: while the adapter's
statistics can't be read, once capture has ended, or for an adapter without a
heartbeat, such as the synthetic one.

`frames_unaccounted` covers frames measurement never reached after it lost
track of a capture fact about them (its fact queue was full;
`worker.facts_dropped` counts the facts), including whole streams it never saw.
Capture keeps the end frame of each stream whose closing fact measurement lost,
until measurement reaches that stream, so these intervals are exact. It holds
at most 4096 such ends at once; when more are waiting, the oldest are
dropped, and each of those streams gets one `frames_unaccounted` with
`extent: "unknown"` and no interval. Measurement can't say whether those
frames arrived, but the health events can: a `frames_missing` event covers
them if they didn't. They're never counted as Listen falling behind: the
exact ones add to `worker.unaccounted_frames`, not to its overrun counts, and
a level they cut short says `truncated_by: "frames_unaccounted"`.

Level events are coalesced per route before they're published, and you can
filter them by route or by age with `max_evidence_age_ms`. Ages come from
`received_monotonic_ns` and the page's `now_monotonic_ns`, and a level event
says how long it stays current in `valid_until_monotonic_ns`. A level event's
age and its `valid_until_monotonic_ns` count from the newest block in its
interval. Its `interval` gives the stream frames it covers, at most
`stale_after_ms` of audio, but not when they were received: a stall whose
counter pauses, or blocks that arrive late, can leave one level spanning
audio received long before its newest block. `device_clock.host_time_ns` is
the host time of the block that holds its first frame. An age counts from when
the runtime received the block, not from when the device captured it: audio
that waited before capture took it is older than its age says.
`capture.adapter_stats` gives `backlog_blocks_lower_bound`, a lower bound on
the blocks the macOS helper had written that capture hadn't taken yet,
wherever they wait: in the helper, on the way to the runtime, or read but not
yet taken. Of those, the audio the runtime has read but capture hasn't taken
is `queued_audio_frames` there, held in `queued_items` protocol items (blocks
and events).

Without `max_evidence_age_ms`, the same cursor returns the same events while
they're retained, so keep track of `(runtime_epoch, sequence)` if you read a
page twice. With it, a later read of the same cursor skips the levels that
have aged past it since, and counts them in `filtered.stale_measurements`.

## Clocks, epochs and gaps

Listen keeps [time's separate clocks](concepts.md#time-has-more-than-one-clock)
apart. Each [epoch](concepts.md#glossary) starts a fresh count:

- Each `listen_start` begins a new **runtime epoch**, and event sequences start
  again at 1.
- Within a runtime, a **stream epoch** covers one unbroken run of the device's
  sample counter. Positions are stream frames, counted from 0. A counter reset
  or backward jump starts a new stream epoch, and positions from an earlier one
  don't carry over. Host time coming and going doesn't start one. The
  `device_clock` fields, `capture.last_block.device_host_time_ns` and
  `capture.clocks.device_host_time` follow it, and a block without host time
  clears an overload report, so a gap after it has cause `device_gap` rather
  than `processor_overload`. A counter that jumps forward shows a gap of
  exactly that many frames. Listen calls a backward jump that lands from 0 to
  just below one second's worth of frames a `counter_reset`, and any other,
  including one that lands below 0, a `counter_backward`.
- Each level and near-silence event reports the device's sample time and host
  time beside its interval, in `device_clock`. Health events, such as
  `frames_missing`, carry an interval but no `device_clock`.
  `device_clock.sample_time_start` is the device sample time of the interval's
  first frame. `host_time_ns` is the host time of the block that holds that
  frame, which starts at stream frame `host_time_frame` with device sample time
  `host_time_sample_time`. Listen never subtracts one clock from another, so
  drift and latency are reported as not estimated and not measured.

Every block must carry the device's sample time, so the counter places every
frame and every `frames_missing` event is exact: a start frame and a frame
count. When blocks stop arriving, Listen reports `capture_stale` and then
`capture_resumed`, and a gap only if the counter moved on meanwhile.

The macOS helper counts the frames it drops: blocks it had no room for, and
IO cycles longer than the 8192 frames it accepts, which this version doesn't
refuse in advance. It reports how many, not where, so Listen places them by
the counter:

- A gap of exactly as many frames as the helper dropped has cause
  `helper_ring_overflow`.
- A gap that lost more frames than the helper dropped has cause `device_gap`
  or `processor_overload`, and its `adapter_dropped_frames` gives the
  helper's share.
- When the helper dropped more frames than the counter skipped, the counter
  moved back inside them, so a new stream epoch starts, as for any backward
  jump.
- Frames it dropped just before a stream began, at the start of capture or
  across a counter reset or backward jump, belong to no stream; the new
  stream's `stream_started` counts them in `adapter_dropped_frames`. Frames it
  dropped just before a block without a usable sample time are counted in the
  `adapter_dropped_frames` of the `stream_ended` that block causes.
- Frames it dropped after the last block capture took have no later block to
  report them; the `listen_stop` receipt does.

Some changes end the stream instead: a block without a usable sample time
(`device_sample_time_unavailable`: none, flagged invalid, not finite, not a
whole frame or beyond a signed 64-bit frame count), a sample-rate, format or
channel-layout change, the device going away, another app taking the device
for exclusive use, the macOS helper failing to read one of those properties
while capturing (`device_property_unreadable`; it never guesses a value
then), or the end of a
synthetic script. After a device change the macOS
helper sends none of the audio its ring still holds, since some of it may
already be in a new format; the `listen_stop` receipt counts it. Listen doesn't
reconnect or convert on its own, and the runtime keeps holding the session
until you stop it: stop it with `listen_stop`, then start a new runtime; if the
rate changed, prepare a new session first.

## Stopping and recovering

- `listen_stop` needs the `runtime_epoch` you're stopping and stops only that
  runtime. While another runtime is running, naming a different one is refused
  with `stale_revision`.
- One runtime runs per session directory. A running runtime holds the
  session's **lease**, a lock on the session that ends when its process exits.
  While it's held, a second start returns a `conflict` receipt
  (`already_running`) and starts nothing.
- [Request IDs](concepts.md#nothing-is-overwritten) make retries safe: the same
  ID replays the recorded receipt, marked `"replayed": true`, beside a fresh
  `current_runtime`, even after a newer runtime started. That includes a
  conflict or an unknown outcome, so a new attempt needs a new request ID. A
  replay is history; call `listen_observe` for the current state.
- If a reply is lost, `listen_request_status` shows which runtime the request
  started or which terminal it sealed, without contacting the runtime or
  repeating anything. It looks through the session's newest 64 runtimes
  (`runtimes_scanned`, with `runtimes_omitted` for older ones), so for a
  request older than those, `request_runtime` and `stop_terminal` are `null`.
- A runtime stops on its own at `limits.max_runtime_seconds`, 4 hours by
  default.
- At most eight calls to a runtime run at once, and a ninth is told the
  runtime is busy. A stop never waits for one of those places, so readers
  can't keep `listen_stop` from reaching the runtime.
- A stop's waits share a 5-second bound before its terminal is sealed, so a
  stuck adapter and a slow measurement can't each add their own wait.
  `terminal.stop` records `elapsed_ms` beside `bound_ms`, with `bound_exceeded`
  if it ran over anyway.
- With the macOS helper, capture's stream ends at the last block it took
  before the stop. `terminal.stop` counts what came after: in `adapter_exit`,
  the audio frames still queued (`audio_frames_left_unread`) and those that
  arrived while the helper closed (`audio_frames_discarded_while_closing`),
  which a stop can leave without a caveat, and in
  `helper_drops_after_last_block_frames` the frames the helper dropped after
  that block, which make the stop `partial`.
- If a runtime's process dies, its directory stays as history. The next start
  checks its journal, records what it found and never reuses it. The journal
  marks where its full queue dropped lines (`dropped_lines_marked` in that
  check) and where it stopped writing because its budget ran out
  (`stopped_writing`). After a failed write it marks the place only if the
  file accepts that line; a write error that persists, such as a full disk,
  leaves no mark, and the check then reads like a journal that ended cleanly,
  though a sealed terminal's `journal.state` says `failed`. A
  write error that left a partial line shows in that check as a torn final
  line. Lines still waiting to be written when the process died leave no
  trace.
- A terminal record gives each route's `availability_at_start` beside its
  `availability`, which is `unavailable` for every route once the runtime has
  stopped. Its `reason` tells them apart: `capture_stopped` when capture ran
  until the stop, `capture_ended` or `capture_failed` when it ended first,
  `measurement_worker_failed` when level measurement had failed, and the
  route's own reason, such as `routing_test_not_available`, for a route that
  was never metered.
- `capture_opened` and the terminal's `capture.provenance` say which software
  produced the capture: `pocket_version` and, with the macOS helper, the
  `macos_version` its helper reported and the build's `helper_sha256` and
  `build_json_sha256`. The helper doesn't read a driver version, so
  `driver_version` is `not_reported`. For the synthetic adapter these fields
  are `not_applicable`.
- A runtime that never opened its journal, because startup ended first, is
  reported with `journal.present: false`; its `chain_ok: false` then means
  there is no chain, not a broken one.

## The macOS helper (experimental)

The CoreAudio adapter reads one input by uid through a small helper that Pocket
compiles on your Mac.

- Build it with `pocket listen build-helper --spec build.json`, where
  `build.json` is `{}`. It needs macOS 15 or later with the Swift compiler
  (`swiftc`) from Xcode or the Command Line Tools. Building only compiles; it
  opens no device.
- Pocket compiles each build into a new folder of its own, in `~/.pocket/listen`
  or in the folder `POCKET_LISTEN_DIR` names; a relative folder counts from the
  directory Pocket runs in. The receipt's `build.build_id` names the build, and
  `build.build_dir` says where it is.
- Use the build by its `build_id`. Listen runs only builds recorded under its
  folder's private key, unchanged and private to you. A build edited, or copied
  or moved without its record and that key, is refused, and you build again.
  The key binds no location: a whole Listen folder copied or moved with its
  key, and named by `POCKET_LISTEN_DIR`, keeps its builds usable.
- Keep that folder on a volume that enforces ownership. On a volume that
  ignores ownership, as macOS can set for an external drive (Get Info: Ignore
  ownership on this volume), every account counts as the owner of every file,
  so Listen refuses its folder, builds and key there.
- Listen never runs the helper from its build folder. Each time, it copies the
  verified helper into a new folder in `/private/tmp` that only you can open,
  checks the copy and runs that, so a change to the build afterwards can't
  change what runs. `/private/tmp` must be root's with the sticky bit set, as
  macOS makes it. `listen_devices` removes its copy when the listing ends, and a
  runtime removes its copy when it stops. A runtime that was killed leaves its
  `pocket-listen-helper-…` folder behind; Listen never uses it again, and you
  can delete it.
- `pocket listen devices` with `{"adapter": {"kind": "coreaudio", "build_id":
  "BUILD_ID"}}` runs the helper's read-only listing mode. It lists inputs by
  uid with their channels, rates and formats, and opens no stream.
- To use an input, prepare a session with `{"kind": "coreaudio", "build_id":
  "BUILD_ID"}` as its adapter and the input's uid from that list. The rest of
  the config is the same as for the synthetic adapter.
- Microphone access must already be granted to the app that runs Pocket, such
  as your terminal. Listen checks it before capturing and won't start without
  it, because macOS can otherwise deliver silence. Listen never asks for
  access itself. If macOS reports access denied, turn it on for that app in
  System Settings > Privacy & Security > Microphone. macOS lists an app there
  only after the app has asked for access, so an app that never asked has
  nothing to turn on: run Pocket from an app that already has microphone
  access. `listen_devices` reports the state macOS gives and the step that
  fits it.
- The helper checks the input once more just before capture begins: its
  liveness, rate, exclusive use, channel layout and stream formats. If one of
  them changed since `listen_start` opened it, the start is refused:
  `device_not_alive`, `device_hogged` or `sample_rate_mismatch` when the
  input went away, was taken or changed rate, and
  `device_changed_before_start` for a channel-layout or format change, or for
  one of those properties the helper couldn't read then, with the change
  named in `capture_refusal.detail.exit_event_type`. Other properties, such as
  the block size, aren't checked again.
- `listen_devices` gives each input a `profile_fit` and its
  `supported_channels`. An input with some streams that aren't 32-bit float,
  but whose streams all carry 32-bit samples, is `partial:not_float32_streams`,
  and routes can use the channels listed in `supported_channels`. The helper
  reads every stream of an input in each IO cycle, so when any stream's
  samples aren't 32 bits wide, the input is `unsupported:not_float32` with no
  `supported_channels`, and a start on it is refused with
  `format_not_float32`. An input whose exclusive-use state the helper couldn't
  read has `hog_mode_pid: null` and is `unsupported:hog_mode_unreadable`. A
  device whose input stream configuration it couldn't read isn't listed, and
  `devices_unreadable` counts those devices, with a notice when there are any.
  When the helper opens an input, a liveness, sample-rate or stream
  configuration read that fails still reads as not alive, 0 Hz or no input
  channels, so the start is refused for that reason; this version doesn't
  name such a failed read at open.
- Listen is designed to read alongside other apps: it never takes a device for
  exclusive use or changes any setting. Sharing an input with another app that
  reads it hasn't been checked natively yet. It accepts 32-bit float input at
  44.1, 48, 88.2 or 96 kHz.
- For a supervised run in a terminal, `python -m pocket_music.listen_host
  --session-dir NEW_SESSION --foreground --expected-config-sha256 SHA` runs the
  same runtime in the foreground until you press Ctrl-C.

Native capture isn't qualified yet, and `capabilities_list` reports these tools
with `native_verified: false`.

## Limits

These are configuration limits, not measured performance.

| Setting | Range | Default |
|---|---|---|
| `measurement.clip_threshold_dbfs` | -20 to 0 dBFS | none: required |
| `measurement.near_silence.level_dbfs` | -120 to -20 dBFS | none: required |
| `measurement.near_silence.min_duration_ms` | 100 to 600000 | none: required |
| `measurement.window_frames` | 32 to the sample rate, at most a quarter of the ring, and with `meter_coalesce_windows` at most `stale_after_ms` of audio | a tenth of the sample rate |
| `buffering.ring_seconds` | 1 to 60 | 10 |
| `health.stale_after_ms` | 100 to 5000, at least the audio one level event covers (`window_frames` times `meter_coalesce_windows`), and more than twice the input's block period when the input reports one; an input that reports no block size isn't checked | 500 |
| `delivery.meter_coalesce_windows` | 1 to 50, and with `window_frames` at least 10 ms and at most `stale_after_ms` of audio per level event | 5 |
| `delivery.normal_retention_events` / `_bytes` | 256 to 65536 / 65536 to 33554432 | 8192 / 4194304 |
| `delivery.health_retention_events` / `_bytes` | 64 to 8192 / 32768 to 4194304 | 1024 / 524288 |
| `limits.max_runtime_seconds` | 10 to 43200 | 14400 |
| `device.expected_sample_rate` | 44100, 48000, 88200 or 96000 | none: required |
| `listen_events` `max_events` | 1 to 256 | 64 |
| `listen_events` `max_bytes` | 16384 to 262144 | 65536 |
| `listen_events` `wait_ms` | 0 to 1000 | 0 |

`max_bytes` bounds the summed canonical bytes of a page's events. The page's
own fields come on top, and a page can hold fewer bytes or no events at all.

## Not in this version

A routing test that would establish cue, recording, and capturing or
analyzing a recent window of audio are follow-ups. `capabilities_list` shows
them as unavailable: `listen_calibrate`, `listen_capture_window`,
`listen_analyze`, `listen_record_start` and `listen_record_stop`. Listen also
doesn't capture inputs without a device sample counter, reconnect to a device
that went away, push events to subscribers or estimate drift and latency
between clocks. Nor does it have a way to request microphone access for an app
that has never asked; that onboarding is a follow-up too.

## Interfaces

Python, the command line and MCP call the same functions. Each CLI action is
also available as a flat command, such as `pocket listen-start`; a flat
`listen-prepare` takes `output_dir` in its spec (see
[From Python and MCP](#from-python-and-mcp)).

| Provider | CLI action | Purpose |
|---|---|---|
| `listen_build_helper` | `pocket listen build-helper` | Build the optional macOS helper |
| `listen_devices` | `pocket listen devices` | List inputs by uid; opens no stream |
| `listen_prepare` | `pocket listen prepare` | Seal one input, its audio routes and settings |
| `listen_start` | `pocket listen start` | Start the session's runtime |
| `listen_observe` | `pocket listen observe` | A fresh snapshot with a cursor |
| `listen_events` | `pocket listen events` | Bounded events after a cursor |
| `listen_stop` | `pocket listen stop` | Stop the named runtime and seal its terminal record |
| `listen_request_status` | `pocket listen request-status` | Inspect a start or stop request without repeating it |
