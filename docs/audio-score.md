# Audio passage scores

An audio passage score groups the [occurrences](concepts.md) in a musical context
into named passages. You can describe which occurrence supplies a foundation,
which carries a recurring figure, and how one passage relates to another. The
roles and relationships are your annotations. Pocket does not infer or play them.

Start with a context made by `context_create`. Its retained source regions,
source frames, timeline clocks and occurrence placements remain authoritative.
The score references occurrence IDs; it does not introduce another clock or copy
their coordinates. See [musical context and practice](musical-context.md) for a
generated example of creating that context.

## Create a score

The Python, CLI and MCP interfaces call the same provider. This example assumes
`context` is an actual context handle and the named occurrences exist in it:

```python
from pocket_music import audio_score_create, audio_score_query

author = {
    "actor": "performer",
    "actor_kind": "human",
    "statement": "Proposed passage roles; not yet auditioned",
    "uncertainty": [],
}
created = audio_score_create(
    store_root, "opening-score", context,
    {
        "score_id": "opening",
        "title": "Opening passage",
        "attribution": author,
        "passages": [
            {
                "passage_id": "figure-and-return",
                "label": "Linked figure appearances",
                "members": [
                    {"occurrence_id": "figure-first", "role": "foreground"},
                    {"occurrence_id": "figure-return", "role": "callback"},
                ],
                "attribution": author,
            },
            {
                "passage_id": "shared-middle",
                "label": "Shared middle",
                "members": [
                    {"occurrence_id": "figure-first", "role": "foreground"},
                    {"occurrence_id": "backing", "role": "foundation"},
                ],
                "attribution": author,
            },
        ],
        "relations": [
            {
                "relation_id": "middle-to-return",
                "from_passage": "shared-middle",
                "to_passage": "figure-and-return",
                "kind": "return",
                "attribution": author,
            },
        ],
    },
)
score = created["artifacts"]["score"]
members = audio_score_query(
    store_root, score, section="members", passage_ids=["figure-and-return"]
)
```

An occurrence can belong to several passages, as `figure-first` does here.
Membership within one passage must be unique. A role is bounded authored text,
not a measured or isolated component. Calling an occurrence a foundation does not
remove other sounds from its source.

Relations can be `sequence`, `repeat`, `variation`, `withhold`, `return`,
`call_response`, `motif_reference`, or `derivation`. This profile requires an
acyclic relation graph. Relations are nonexecuting annotations: a `repeat` does
not create another occurrence or launch a loop. Represent each actual appearance
with its own context occurrence.

## Revise after an explicit context edit

Use the existing `context_resolve` and `context_edit` tools to make a coordinate
change. An explicit linked edit lists the occurrences it affects. The score does
not silently expand that selection or move other passages.

```python
from pocket_music import audio_score_revise

# edited is the actual response from context_edit on this score's exact context.
revised = audio_score_revise(
    store_root, "rebind-opening-score", score, edited["artifacts"]["edit"]
)
```

The edit must prove the exact parent context and its child. The revised score
retains the complete passage definition and references that child, its parent
score, and the edit receipt. It does not rewrite the original score, context or
recording. A different context with the same display name is insufficient.

Queries revalidate retained contexts, membership, and revision proofs, including
after the artifact store is moved. A newly hashed record with unrequested role
changes, an unrelated context, or a forged edit proof is rejected. The checks
establish identity and declared relationships, not the authenticity of a person's
annotation or its musical quality.

## Interfaces and bounds

- `audio_score_create(store_root, request_id, context, definition)` creates an
  immutable `pocket.audio-score/v1` artifact.
- `audio_score_query(store_root, score, section="summary", passage_ids=None,
  offset=0, limit=32)` reads `summary`, `passages`, `relations`, or `members`.
  Pages contain at most 64 rows. A relation filter includes either endpoint.
- `audio_score_revise(store_root, request_id, score, context_edit)` rebinds an
  unchanged definition through a verified context edit.

CLI names use hyphens, for example `pocket audio-score-query --spec query.json`.
MCP uses the Python provider names. Requests that create artifacts use the normal
request journal: the same ID and exact inputs replay the retained result; changed
inputs under that ID are refused. Retained dependencies are verified on replay.

A score has at most 256 passages, 32 members per passage, 512 relations, and
32 parent revisions. Unknown IDs, duplicate members, unsupported fields and
cycles are refused. Existing context bounds still apply. Not every context
occurrence needs a score membership.

This profile does not render audio, add gain or effects, certify beat one,
establish listening approval, mutate a host, or establish performance readiness.
An overlap can be described without claiming that it sounds good or that the
current practice renderer can render it. Processing and native performance need
their own explicit profiles and evidence.
