# Practice review

A small local page for listening to one retained comparison and saving your own
attributed report about the exact audio and interval you heard. It sits on top of
the same public providers as Python, CLI and MCP: `practice_preview`,
`practice_feedback` and `practice_feedback_query`. It is not a general web service,
file browser or editor.

## Start it

You need an existing store and a comparison made with `practice_compare`,
`practice_compare_revisions` or `practice_compare_processed`. Save the comparison
receipt (or its bare handle) as JSON, then run:

```sh
pocket practice-review --store-root /path/to/store \
  --comparison-file /path/to/comparison-receipt.json \
  --session-dir /path/to/new-review-session --port 0
```

The command prints one JSON line with the local URL and serves until Ctrl-C.
On Ctrl-C it waits for a preview or report that is still being written before
exiting. `--port 0` chooses a free port. Add `--reports-file reports.json` (a JSON list of
feedback handles or receipts) to show existing reports, for example an agent's
technical report, beside new ones; every one must belong to the selected comparison.
The server locks its session directory with POSIX `fcntl` locking, so it does not
run on Windows.

The [first experiment](getting-started.md) keeps its comparison receipt under
`comparison` in `results.json`. To review its synthetic tones:

```sh
python -c "import json, sys; json.dump(json.load(open(sys.argv[1]))['comparison'], open(sys.argv[2], 'x'))" \
  private/first-practice/results.json private/first-practice/comparison.json
pocket practice-review --store-root private/first-practice/store \
  --comparison-file private/first-practice/comparison.json \
  --session-dir private/first-practice/review --port 0
```

A report about those tones exercises the page; it is not a musical listening test.

The Python entry point is:

```python
from pocket_music.practice_review import start_practice_review

start_practice_review(store_root, comparison_file, session_dir, reports_file=None, port=0)
```

The session directory holds only `review-session.json`, a small index of the
preview and report handles made through this page, plus a lock so two servers
cannot share it. Reopening the same session directory shows those previews and
reports again after full verification, on every request. If the index lists a report
twice, a report about another comparison, or anything that is not a report, the page
refuses to load rather than count it. The artifacts themselves live in the store.

## Listen and report

1. The page states the comparison's question, the baseline and each variant, how
   each was processed (exact source samples or a declared join envelope), its
   signal checks and a short audio identity. Provenance opens on request.
2. Choose an item and prepare its browser preview. This calls `practice_preview`
   with the `browser-pcm16-original-rate/v1` profile: an original-rate PCM16 copy
   with nearest rounding and no dither, gain or clamping. The page says whether any
   sample was rounded. An unrepresentable sample or an unqualified sample rate is
   refused with an explanation, every time you try; the page never resamples or
   normalizes to make audio playable. A refused or interrupted preview request is
   kept in the store for inspection and a later attempt uses a new request ID.
3. Play it with the browser's controls. Nothing plays automatically. Mark the
   interval you actually heard with **Start at playhead** / **End at playhead**
   (`[` and `]`), or type seconds. The page always shows the integer frames that
   will be saved. **Play this interval** (`P`) replays that span and stops within
   a display frame (about 20 ms) of its end; pausing, seeking or ordinary playback
   cancels it.
4. Enter your name and what you heard, optionally choose an existing decision
   (keep, revise, reject, no addition), and tick **I listened to this interval of
   this preview**. Saving calls `practice_feedback` with the preview and creates an
   immutable `pocket.practice-feedback/v2` report with `actor_kind: "human"`. The
   receipt appears only after the stored report has been read back and verified.
   **Cancel draft** saves nothing.

Playback, seeking or a finished timer never creates a report. A draft belongs to
one item and one preview: switching items with unsaved text asks you to keep or
discard it, and a changed preview makes the draft unsaveable. While a save is in
flight the form, cancel and item choices are locked. A repeated save of the same
unchanged draft returns the same report rather than a duplicate, including after
a dropped connection: the page then says the report may already exist and keeps
your draft.

Every saved report is listed as written, including contradictory decisions and
agent reports, which are labelled separately. The header counts human listening
reports and agent reports separately, and only a person's report says what was
"heard". Nothing is averaged into a preference, and a report is not a musical
verdict for any other interval or item.

### Switching between items

Switching keeps the playhead position only when the comparison itself proves
that the same output frame is the same musical position:

- a processed comparison (join envelopes of one exact baseline), whose members
  share identical timing and frame counts; or
- a revision comparison whose explicit occurrence correspondence pairs every
  occurrence at identical output frames.

Otherwise, including ordinary same-context comparisons, each item keeps its own
position and the page says so instead of implying an aligned A/B.

## What this establishes

The page shows what Pocket verified: the render, the preview bytes and the saved
report. It does not establish what your device output: the browser or operating
system may resample or process audio. Chromium is the only browser qualified for
the preview format so far. A report is the listener's attributed statement about an
interval, not proof that anyone listened, and not approval of other material.

## Local protection

The server binds only `127.0.0.1`, checks the exact Host, rejects foreign origins
and cross-site requests, requires a same-origin JSON request with a per-process
token for every write, limits request bodies to 64 KiB, and sends a restrictive
content security policy with `no-store` caching. Error messages never include
store or session paths. Only the selected comparison's
items are reachable through server-issued item IDs; there is no path parameter or
store listing. Audio responses support one byte range. Each request verifies what
it reads; nothing verified in one request is trusted in the next. This protects
the local browser boundary; it is not a sandbox against other local processes.

## Limits

One comparison per session (a baseline plus up to eight variants), up to 128 reports
per session, and one preview preparation at a time. Reports are listed 16 per page.
The page has no waveform editor, synchronized playback for unaligned comparisons,
remote sharing or account system.
