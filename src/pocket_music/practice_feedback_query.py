"""Explicit standalone feedback retrieval; no native imports or inferred preferences."""
from __future__ import annotations

from typing import Literal

from .artifact_store import ArtifactHandle
from .errors import PocketError
from .feedback_selection import query_feedback_records
from .practice_audio import load_practice_audio, load_practice_feedback

COVERAGE = {"provider": "practice-feedback-query-v1", "execution": "retained_evidence_only",
            "provider_playback": False, "native_execution": False, "musical_preference_inference": False}


def practice_feedback_query(store_root: str, feedback: list[ArtifactHandle], render: ArtifactHandle | None = None,
                            actor: str | None = None, actor_kind: Literal["human", "agent"] | None = None,
                            decision: Literal["keep", "revise", "reject", "no_addition"] | None = None,
                            interval_frames: list[int] | None = None, limit: int = 16,
                            cursor: str | None = None, max_bytes: int = 16384) -> dict:
    """Retrieve original reports against an optional exact render and half-open interval."""
    if interval_frames is not None and render is None:
        raise PocketError("Interval filter requires an exact render handle")
    selected = load_practice_audio(render, store_root) if render is not None else None
    cache = {}
    def load(handle):
        record, audio = load_practice_feedback(handle, store_root, cache)
        return {"feedback": handle, **{k: record[k] for k in ("actor", "actor_kind", "note", "decision",
                "evidence_kind", "interval_frames", "render", "comparison", "render_sha256")},
                "sample_rate": audio["signal"]["sample_rate"]}
    return query_feedback_records(store_root=store_root, feedback=feedback, loader=load,
        schema="pocket.practice-feedback/v1", coverage=COVERAGE, actor=actor, actor_kind=actor_kind,
        decision=decision, render_sha256=selected["audio"]["sha256"] if selected else None,
        interval_frames=interval_frames, limit=limit, cursor=cursor, max_bytes=max_bytes,
        identity_extra=render, extra_filter=lambda row: render is None or row["render"] == render)
