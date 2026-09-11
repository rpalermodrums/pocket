# Shared record bags

A record bag is a sealed, immutable catalogue used by Set Workshop and On Deck.
Titles, Spotify URIs and musical annotations remain catalogue evidence. Only a
verified local `audio.identity` identifies recording bytes.

```python
from pocket_music.record_bag import create_record_bag, query_record_bag

bag = create_record_bag([
    {"track_id": "record-a", "title": "A record", "artists": ["An artist"],
     "catalog_source": "user_list",
     "profile": {"roles": ["opening"], "provenance": "user"}},
], "new-bag-directory", "Friday records")
page = query_record_bag(bag["handle"], limit=20)
```

Public functions are `create_record_bag(tracks, output_dir, title)`,
`load_record_bag(handle)`, `query_record_bag(handle, query="", *, limit=20,
offset=0)` and `revise_record_bag(handle, tracks, output_dir, title=None)`.
The `BagHandle` contains schema `pocket.record-bag-handle/v1`, manifest path and
SHA-256. Both the handle digest and adjacent seal must match on load. Output
directories must be new. Revisions retain the parent handle and never rewrite an
earlier catalogue. Input order and stable track IDs are preserved; duplicate IDs
are rejected. Search matches IDs, titles, artists and tags, with explicit paging.

Inputs use `selection_types.BagTrackInput`. Optional unknown fields may be absent
or null. A nonempty profile must explicitly attribute values to `user`,
`agent_hypothesis` or `measured`. Energy and vocal density use 0–1, and BPM uses
20–400, with at most eight BPM candidates. These are supplied values and
attributions; the bag never invents a measured key, energy, tempo or listening
verdict. Spotify-derived catalogue
metadata remains identified by `catalog_source`; model input provenance is a
separate contract. Unavailable entries can remain in the bag with `available:
false`, allowing later tools to exclude them without deleting their identity.

If `local_path` is supplied, creation verifies complete byte identity and the
decoded header. The caller's path and profile remain unchanged. A derived `audio`
field holds `status: identified`, the AssetRef in `identity`, an absolute lexical
`reference_path`, and `file_version` (device, inode, size, mtime and ctime).
`expected_audio_sha256`, when supplied, must match. Source regions require local
identified audio and exact in-bounds integer frames. No audio is copied or altered.

Loading checks current local version stamps and reference retargeting. A missing,
replaced or modified local recording invalidates use of that bag until an explicit
new revision is created; it is not silently replaced by a similarly titled file.
A catalogue-only bag remains usable offline without media. The stamp check is a
local version check, not a defense against privileged metadata manipulation.

Bag files contain local paths and potentially private library annotations. Keep
them outside public Git repositories. The public tests generate their own media
and catalogues at runtime.
