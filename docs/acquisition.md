# Optional recording acquisition

`pocket_music.acquisition` discovers source candidates and acquires one explicitly chosen HTTP(S) recording URL. It does not download Spotify streams, select the first search result, infer an edition from a title, install tools, read browser cookies or normalize audio. Original recordings and private receipts belong outside Git.

For authorized recording acquisition, the default is **yt-dlp with the best available audio** (`bestaudio/best`). This is already the provider's selection policy. Keep the downloaded original codec; create any WAV or MP3 listening exports separately. Selecting the best encoding does not establish that a candidate is the requested performance, mix or edition.

## Providers

- `discover_sources(query, *, limit=5, executable='yt-dlp')` performs a flat metadata-only YouTube search for at most ten candidates. Returns selected metadata and `version_identity:unverified`; nothing is downloaded or automatically chosen.
- `inspect_source_formats(source_url, *, executable='yt-dlp', limit=32)` returns bounded, sanitized audio format metadata without downloading. Signed media URLs are omitted. This supports a reviewed alternate encoding when an earlier decode fails.
- `plan_acquisition(source_url, output_dir, *, version_note, authorization_note, expected_source_id=None, sample_rate=None, channels=None, format_id=None)` seals the chosen URL, evidence notes and decode policy. An optional concrete `format_id` pins an inspected encoding; selection expressions and automatic fallback are not accepted. Version/authorization notes are supplied assertions, not independently verified licenses or edition proof. A Spotify URL, local file URL or embedded URL credentials are rejected. Default rate/channels are `None`, preserving source rate/channels. Explicit rate may be 8–192 kHz and channels 1 or 2. The source bound is 30 minutes/512 MiB with at most eight channels.
- `acquire_source(plan_dir, *, expected_plan_sha256, output_dir, yt_dlp_executable='yt-dlp', ffmpeg_executable='ffmpeg', ffprobe_executable='ffprobe')` requires a new output directory. It inspects metadata first, pins the selected accessible `bestaudio/best` format, then downloads with bounded retries, no user config/plugins, no overwrite and abort-on-missing-fragment. Expected source ID and actual format must match across inspection/download.

The receipt reports yt-dlp/ffmpeg/ffprobe versions, selected sanitized source metadata, original byte hash/probe, decoded byte hash/header, full finite/signal scan and exact container-versus-decoded duration discrepancy. Full extractor dumps are not published as canonical metadata because they can include signed URLs and request details. Sanitized diagnostics omit URLs. Failed staging can retain partial files for inspection; it is not a completed asset.

The original container/codec bytes are retained. Decode uses strict error handling and `pcm_f32le`, with no gain, fades or normalization. Resampling/channel conversion happens only when explicitly requested. A float WAV does not improve a compressed recording. `bestaudio` is the extractor's available-format ranking, not a measured fidelity score; codec bitrate alone is not quality evidence.

For a single unambiguous audio stream probed as Opus in Matroska/WebM, decode explicitly uses input `-fflags +noparse+nofillin`: the container already supplies complete packets, and FFmpeg 8.0.1's additional Opus parser reports an error on an empty EOF flush. Other codec/container combinations keep the default strict configuration. New receipts retain `decode_configuration` and the exact `decode_command`; earlier receipts remain unchanged. Decoder errors and any nonempty error diagnostic still fail, including invalid Opus packets and truncated containers. No warnings are filtered and no failed receipt is retroactively promoted. These paired flags are documented in the [FFmpeg format options](https://ffmpeg.org/ffmpeg-formats.html#Format-Options).

For example, if an Opus source emits a packet error even with process exit code zero, it remains failed. Inspect the formats, explicitly select another available encoding in a new plan, and retry into a new directory. The retry does not erase the original failure or certify the alternate's listening fidelity.

## Readiness and failure

The immutable receipt distinguishes:

- `status:completed`: a retained original and complete decoded artifact passed clock/header checks.
- `ready_for_analysis`: finite, nonempty signal, no digital silence, no RMS at/below −60 dBFS, no samples at/above full scale and no download warning requiring review.
- `ready_as_requested_recording:false`: edition equivalence always remains unverified by this adapter, even if a supplied expected source ID matches. Clean/explicit/remix confirmation needs separate evidence/listener review.

Sample peak is measured; true peak and musical audition are not. Container duration may include packet padding; discrepancy is recorded and >100 ms is held. This tolerance is a container-completeness check, not permission to alter source timing or a DJ alignment tolerance. Full decode errors, partial downloads, changed original bytes, malformed metadata or source mismatch preserve an immutable failed receipt plus `.staging`, never a fake successful asset. Existing output directories are rejected; retry into a new output directory, preserving earlier evidence.

The two optional command dependencies are yt-dlp and FFmpeg (ffmpeg/ffprobe); Pocket core does not require or auto-install them. Current YouTube support may additionally require yt-dlp EJS and a supported JavaScript runtime. Pin a tested tool environment explicitly and record its version. No model weights or remote audio upload are part of this path.

## Testing

Tests generate media at runtime. Injected runner cases exercise partial download, wrong source, malformed metadata, decode failure, silent output, stale plan and conversion mismatch. A real optional integration test serves a generated mono 16 kHz WAV over localhost, runs yt-dlp + ffmpeg/ffprobe, then verifies original hash, exact float samples, complete frames and unchanged rate/channel count. It skips if binaries are unavailable and never fetches a public recording. No provider test authenticates or writes Spotify.

Primary documentation: [yt-dlp format selection](https://github.com/yt-dlp/yt-dlp#format-selection), [options and embedding](https://github.com/yt-dlp/yt-dlp#usage-and-options), [dependencies](https://github.com/yt-dlp/yt-dlp#dependencies), [EJS](https://github.com/yt-dlp/yt-dlp/wiki/EJS).
