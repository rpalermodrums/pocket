# SPDX-License-Identifier: AGPL-3.0-only
"""Retrieve listener claims without widening their identity, interval or scope."""

from __future__ import annotations

from pathlib import Path

from .assets import sha256_file
from .errors import PocketError
from .stitch import FeedbackScope, SCOPES, _integer, _load_sealed, _safe_child


def query_feedback(
    trial_dir: str,
    *,
    variant_id: str | None = None,
    output_sha256: str | None = None,
    start_frame: int | None = None,
    end_frame: int | None = None,
    scope: FeedbackScope | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """Return sealed listener notes overlapping an exact output-local frame range.

    All notes retain their original interval and wording, including contradictory
    claims. A range filter selects overlap; it does not rewrite note applicability.
    This is evidence retrieval, never an algorithmic or new listening verdict.
    """
    folder = Path(trial_dir).expanduser().resolve()
    manifest, digest = _load_sealed(folder, "trial.json", "pocket.transition-trial/v1")
    count = _integer(limit, "limit", 1)
    skip = _integer(offset, "offset")
    if count > 100:
        raise PocketError("limit must be at most 100")
    if scope is not None and scope not in SCOPES:
        raise PocketError("Unknown feedback scope")
    if (start_frame is None) != (end_frame is None):
        raise PocketError("Supply both start_frame and end_frame, or neither")
    if start_frame is not None:
        _integer(start_frame, "start_frame")
        _integer(end_frame, "end_frame", 1)
        if end_frame <= start_frame:
            raise PocketError("Feedback query interval must be increasing and end-exclusive")

    variants = {v["id"]: v for v in manifest["variants"]}
    if len(variants) != len(manifest["variants"]):
        raise PocketError("Ambiguous trial variant identity")
    selected = [v for v in variants.values()
                if (variant_id is None or v["id"] == variant_id)
                and (output_sha256 is None or v["output"]["sha256"] == output_sha256)]
    if not selected:
        raise PocketError("Variant/output identity does not belong to this trial")
    for variant in selected:
        if sha256_file(_safe_child(folder, variant["output_file"])) != variant["output"]["sha256"]:
            raise PocketError("Output identity changed; stored feedback is stale")
        if end_frame is not None and end_frame > variant["frames"]:
            raise PocketError("Feedback query extends beyond a selected output")
    selected_ids = {v["id"] for v in selected}
    notes = []
    for path in sorted((folder / "feedback").glob("*.json")):
        _safe_child(folder, str(path.relative_to(folder)))
        note, note_hash = _load_sealed(path.parent, path.name, "pocket.listener-feedback/v1")
        variant = variants.get(note.get("variant_id"))
        if (note.get("trial_manifest_sha256") != digest or variant is None
                or note.get("output_sha256") != variant["output"]["sha256"]):
            raise PocketError("Feedback record does not belong to this exact trial/output")
        begin = _integer(note.get("start_frame"), "stored start_frame")
        end = _integer(note.get("end_frame"), "stored end_frame", 1)
        if not begin < end <= variant["frames"] or note.get("scope") not in SCOPES:
            raise PocketError("Invalid stored feedback scope or interval")
        if note["variant_id"] not in selected_ids or (scope is not None and note["scope"] != scope):
            continue
        if start_frame is not None and not (end > start_frame and begin < end_frame):
            continue
        notes.append({**note, "feedback_file": str(path), "feedback_sha256": note_hash})
    return {
        "schema": "pocket.feedback-query/v1",
        "trial_manifest_sha256": digest,
        "coordinates": "selected_output_local_frames_end_exclusive",
        "filter": {"variant_id": variant_id, "output_sha256": output_sha256,
                   "start_frame": start_frame, "end_frame": end_frame, "scope": scope},
        "notes": notes[skip:skip + count],
        "pagination": {"total": len(notes), "offset": skip, "limit": count,
                       "next_offset": skip + count if skip + count < len(notes) else None},
        "evidence": "stored_listener_supplied_claims",
        "generalization": "each_original_output_interval_and_scope_only",
        "musical_verdict": None,
    }
