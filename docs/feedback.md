# Reusing a listening correction

`record_feedback` stores a listener's words against one exact output hash, a local frame interval and a scope such as `bar_phase`, `timing` or `tonal_overlap`. `query_feedback` retrieves these records without broadening their meaning or resolving disagreements on the listener's behalf.

```python
from pocket_music.feedback import query_feedback

result = query_feedback(
    "/path/to/trial",
    variant_id="v01",
    start_frame=48000,
    end_frame=96000,
    scope="bar_phase",
    limit=20,
)
```

Coordinates are frames in the selected output file, not source-recording or arrangement time. Supply both interval endpoints or neither. Filtering uses interval overlap with an exclusive end; returned notes keep their full original interval and text. Two contradictory claims are returned as two claims. Bar-position feedback never becomes timing, tonal or full-mix approval.

The query verifies the sealed trial and feedback records and current selected output hashes. A changed output or tampered note fails explicitly. `output_sha256` can constrain the exact recording in addition to `variant_id`; an identity outside the trial is an error. Empty notes means no stored matching feedback, not a favorable musical verdict.

The result includes explicit pagination (`total`, `offset`, `limit`, `next_offset`), the applied filter, and `musical_verdict: null`. `limit` defaults to 20 and is capped at 100. The same public function is exposed as the MCP tool `query_feedback` and through a JSON argument file:

```sh
pocket lab feedback-list --spec /path/to/feedback-query.json
```

Keep real listener words and trial audio outside Git. Generated test claims exercise this contract without claiming a person auditioned the fixture.
