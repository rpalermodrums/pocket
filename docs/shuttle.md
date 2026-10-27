# Shuttle: improvise from a crate

A shuttle carries thread through a loom. Shuttle lets an agent choose records
as a set unfolds, inspect actual Serato state and execute one short move at a
time. It builds on record bags and Whisker, with no prescribed running order.

This first version is experimental. Its native profile is macOS, Serato DJ Pro
5.0.0, a connected DDJ-FLX4 and Extended view at 1470×956 logical pixels. A
successful build does not establish native acceptance on another setup.

## Prepare the connection

1. Build helpers with `pocket shuttle build-bridge --spec build.json --output NEW_PATH`.
   Use `{}` for `build.json`; the CLI takes the destination from `--output`,
   not an `output_dir` inside the spec. Swift and ffmpeg must already exist.
   Building does not open an app, start audio capture or change a mapping.
   Capability discovery checks the local macOS platform and toolchain without
   running either compiler or capture. An available build toolchain does not
   establish a connected controller, supported app view, or native acceptance.
2. Start `python -m pocket_music.shuttle_host --build-dir PATH --runtime-dir NEW_PATH`.
   The foreground process owns a private local socket, a separate virtual MIDI
   device and the Serato Virtual Audio capture. An existing runtime destination
   is refused. Shut it down with Ctrl-C when finished.
3. In Serato, enable **Make Audio Available to Other Applications**. Save any
   existing MIDI mapping, enable the separate **Pocket Shuttle** MIDI device,
   and load the supplied `Shuttle.xml` as a separate preset. Primary FLX4
   remapping is not required. Qualify the actual loaded mapping before a set.
4. Select the intended local crate in **Custom** order, clear search and focus
   its track list. The first version accepts 1–64 local records. Native library
   navigation temporarily brings Serato to the foreground.
5. Call `shuttle_prepare` with `crate_name`, a new `output_dir`, `runtime_dir`
   and optionally a read-only `library_path` and Whisker `intent`.
   For the CLI, use `shuttle prepare --spec prepare.json --output NEW_SESSION`
   and omit `output_dir` from `prepare.json`.

Preparation retains immutable recording snapshots (APFS clones where supported)
and a sealed record bag. Serato writes gain/cue metadata to its native files;
that must not silently mutate Pocket's retained source identity. Native loading
reverifies decoded PCM when the source version changes. Container hashes and
decoded-frame identities stay separate. No gain, resampling or audio edit is
applied to retained recordings.

Integer recordings retain their existing decoded identity format. FLOAT and
DOUBLE recordings use a tagged v2 representation that hashes their actual
floating-point samples and refuses nonfinite values. Reprepare a session whose
floating-point source was identified by the earlier integer decoder. The old
sealed identity cannot be silently upgraded.

## Play one decision at a time

The Python functions, CLI and MCP call the same providers:

| Provider | CLI action | Purpose |
|---|---|---|
| `shuttle_build_bridge` | `shuttle build-bridge` | Build opt-in native helpers |
| `shuttle_prepare` | `shuttle prepare` | Retain this crate and start its unordered session |
| `shuttle_observe` | `shuttle observe` | Get a fresh revision, deck state and master levels |
| `shuttle_options` | `shuttle options` | Ask Whisker for next-record options |
| `shuttle_act` | `shuttle act` | Execute one explicit move |
| `shuttle_request_status` | `shuttle request-status` | Inspect a request without redispatching |

CLI actions consume `--spec` JSON. `shuttle_act` needs `session_dir`, the current
`expected_revision`, a unique `request_id` and an `action` object. Examples:

```json
{"kind":"claim"}
{"kind":"load","deck":"right","track_id":"an-exact-prepared-track-id"}
{"kind":"gain","deck":"right","value":0}
{"kind":"play","deck":"right"}
{"kind":"gain","deck":"right","value":48,"duration_seconds":4}
{"kind":"loop","deck":"right","beats":8}
{"kind":"loop","deck":"right","beats":null}
{"kind":"pause","deck":"left"}
{"kind":"release"}
```

Observe after a move before choosing another. Library navigation uses native
keyboard events; deck controls use the dedicated MIDI mapping. Loading refuses a
playing deck. It checks the selected recording's PCM, the resulting deck display,
and a fresh matching full-path cache event from this Serato process's log. An
ambiguous or unsuccessful load starts no playback and is not retried silently.
Display artist/duration matches on initially loaded decks are labeled separately
from these source-checked loads. A cache request alone is not playback evidence.
Source verification includes replacement-sensitive file metadata and compares
the decoded identity to each prepared target. It is repeated after navigation
and before binding a load. This checks the selected file, not a decoded-sample
comparison with Serato's internal audio cache. Release, physical input, observed
process changes, and display mismatches invalidate remembered source bindings.

Gain values are MIDI 0–64, approximately −24 to 0 dB. **Zero is not a mute.**
Pause the outgoing deck after fading it if it should leave completely. Native
numeric gain readback is unavailable; commanded gain is labeled as commanded.
A gain ride is at most four seconds. Loop lengths are 8 or 16 beats, or explicit
off when feedback is known. Sync feedback does not prove beat or phrase alignment.
A changed loop state requires matching MIDI feedback timestamped after dispatch.
An already-matching loop returns `unchanged` with cached-feedback scope; it does
not claim fresh readback. Unknown outcomes never trigger another blind toggle.

FLX4 input releases ownership and cancels further queued movement. Controls are
not restored after intervention. Reclaiming is a new explicit action from a fresh
revision after the controller is still. Release also cancels an in-flight move;
it does not stop the music. This version's takeover detector covers FLX4 MIDI,
not every possible mouse gesture or keyboard shortcut in another application.
Ownership is checked at each dispatch submission as well as at action admission.
Revocation cancels undispatched work. A message already submitted to the native
helper may still arrive; a software guard cannot undo physical input. Any
topology change after the helper's initial inventory invalidates the runtime,
including changes while nobody owns it. Reconnecting requires a new runtime.

Stale/invalid audio and unsupported views block control. A solo transport start
with no detected master audio is unverified unless `allow_silence:true` was
explicit. Gain rides cancel on sustained unexpected near-silence; intentional
quiet passages can explicitly opt out. This is a signal check, not a music judge.
Observation requests are serialized without waiting behind another screenshot
request. A busy observer refuses rather than returning cached success. Native
capture timestamps must belong to the new request and lie within its bounded
observation interval. `native_observation_ns` is the capture start;
`native_observation_completed_ns` reports the later observer completion. The
helper uses CPU-only text recognition. These checks do not create a beat clock
or certify a source position when the text is unreadable.

A repeated request ID with identical inputs returns its retained outcome without
repeating a move. A changed request under the same ID is refused. A retained
receipt is historical, not a fresh state snapshot. After a lost response, use
request status and observe actual state; never retry with a new ID to find out
whether a physical action happened. Process restarts require a new runtime and
session; old process nonces are invalid.

## What this version establishes

Master audio is stereo 48 kHz, metered in 100 ms blocks. The reader verifies the
self-describing FLOAT32 WAV pipe before interpreting samples. A different rate,
channel count, or unsupported header refuses capture; no implicit resampling or
channel conversion is requested. Observation and MIDI feedback remain separate
evidence.

Optional `--capture-file NEW.wav` records the FLOAT32 master pipe frames for at
most 20 minutes. Observation exposes `capture.state`, `frames_written`,
`maximum_frames`, and format verification separately from the audio meter.
The lifecycle includes `starting`, `recording`, `exhausted`, `failed`, and
`finalized`. The exact final partial block is retained when it contains complete
stereo frames. Existing files are refused, including a destination created
during setup. Write or finalization failure is not a successful save.

Reaching the limit closes the bounded file and reports `exhausted`. Metering can
continue, but further musical control is blocked because the requested capture
is no longer recording. Do not choose this optional recorder for a longer take.
Use a separately verified primary recorder and omit this capture option. A
frame count or closed file alone does not prove device-clock continuity, an
unconverted original device format, or a full take without gaps.

This profile does not capture isolated headphone cue audio or prove physical
headphone output. No model, network request, inference or audio decoding runs in the
live suggestion loop. Decoding to verify a changed source is preparation for
loading the inactive deck.

There is no hard real-time or phrase scheduler, autonomous musical listening,
automatic source separation, physical knob movement, or general controller
support in this profile. Broader performance scheduling remains a separate
unqualified capability. Your listening and feedback decide whether the moves
make a worthwhile set.
