# Shuttle: improvise from a crate

A shuttle carries thread through a loom. Shuttle lets an agent choose records
as a set unfolds, inspect actual Serato state and execute one short move at a
time. It builds on record bags and Whisker, with no prescribed running order.

This first version is experimental. Its native profile is macOS, Serato DJ Pro
5.0.0, a connected DDJ-FLX4 and Extended view at 1470×956 logical pixels. A
successful build does not establish native acceptance on another setup.

## Prepare the connection

1. Build helpers with `pocket shuttle build-bridge --spec build.json`, where
   the spec contains a new `output_dir`. Swift and ffmpeg must already exist.
   Building does not open an app, start audio capture or change a mapping.
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

Preparation retains immutable recording snapshots (APFS clones where supported)
and a sealed record bag. Serato writes gain/cue metadata to its native files;
that must not silently mutate Pocket's retained source identity. Native loading
reverifies decoded PCM when the source version changes. Container hashes and
decoded-frame identities stay separate. No gain, resampling or audio edit is
applied to retained recordings.

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

Gain values are MIDI 0–64, approximately −24 to 0 dB. **Zero is not a mute.**
Pause the outgoing deck after fading it if it should leave completely. Native
numeric gain readback is unavailable; commanded gain is labeled as commanded.
A gain ride is at most four seconds. Loop lengths are 8 or 16 beats, or explicit
off when feedback is known. Sync feedback does not prove beat or phrase alignment.

FLX4 input releases ownership and cancels further queued movement. Controls are
not restored after intervention. Reclaiming is a new explicit action from a fresh
revision after the controller is still. Release also cancels an in-flight move;
it does not stop the music. This version's takeover detector covers FLX4 MIDI,
not every possible mouse gesture or keyboard shortcut in another application.

Stale/invalid audio and unsupported views block control. A solo transport start
with no detected master audio is unverified unless `allow_silence:true` was
explicit. Gain rides cancel on sustained unexpected near-silence; intentional
quiet passages can explicitly opt out. This is a signal check, not a music judge.

A repeated request ID with identical inputs returns its retained outcome without
repeating a move. A changed request under the same ID is refused. A retained
receipt is historical, not a fresh state snapshot. After a lost response, use
request status and observe actual state; never retry with a new ID to find out
whether a physical action happened. Process restarts require a new runtime and
session; old process nonces are invalid.

## What this version establishes

Master audio is stereo 48 kHz, metered in 100 ms blocks. Observation and MIDI
feedback remain separate evidence. Optional `--capture-file NEW.wav` records
actual FLOAT32 master input, preserving its samples, for at most 20 minutes.
It does not capture isolated headphone cue audio or prove physical headphone
output. No model, network request, inference or audio decoding runs in the
live suggestion loop. Decoding to verify a changed source is preparation for
loading the inactive deck.

There is no hard real-time or phrase scheduler, autonomous musical listening,
automatic source separation, physical knob movement, or general controller
support in this profile. Broader performance scheduling remains a separate
unqualified capability. Your listening and feedback decide whether the moves
make a worthwhile set.
