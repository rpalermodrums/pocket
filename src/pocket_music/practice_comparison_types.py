# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit correspondence between selected full output occurrences."""
from typing_extensions import TypedDict

from .artifact_store import ArtifactHandle


class OccurrencePair(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    baseline_occurrence_id: str
    variant_occurrence_id: str
    baseline_interval_frames: list[int]
    variant_interval_frames: list[int]


class RevisionCorrespondence(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    variant: ArtifactHandle
    pairs: list[OccurrencePair]
