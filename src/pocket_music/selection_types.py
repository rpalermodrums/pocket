# SPDX-License-Identifier: AGPL-3.0-only
"""Shared selection vocabulary; annotation is not measured musical truth."""

from __future__ import annotations

from typing import Literal, NotRequired

from typing_extensions import TypedDict


class BagHandle(TypedDict):
    schema: Literal["pocket.record-bag-handle/v1"]
    path: str
    sha256: str


class MusicalProfile(TypedDict, total=False):
    __pydantic_config__ = {"extra": "forbid"}  # noqa: RUF012 - Reject misspelled constraints at transport boundary
    tags: list[str] | None
    roles: list[str] | None
    energy: float | None
    bpm: float | None
    bpm_candidates: list[float] | None
    key: str | None
    vocal_density: float | None
    notes: str | None
    provenance: Literal["user", "agent_hypothesis", "measured"]


class SourceRegion(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}  # noqa: RUF012 - Reject misspelled constraints at transport boundary
    start_frame: int
    frames: int
    role: str
    note: NotRequired[str]


class BagTrackInput(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}  # noqa: RUF012 - Reject misspelled constraints at transport boundary
    track_id: str
    title: str
    artists: list[str]
    spotify_uri: NotRequired[str | None]
    album: NotRequired[str | None]
    duration_seconds: NotRequired[float | None]
    explicit: NotRequired[bool]
    available: NotRequired[bool]
    catalog_source: NotRequired[Literal["spotify_ui", "spotify_api", "user_list", "local_manifest"]]
    local_path: NotRequired[str | None]
    expected_audio_sha256: NotRequired[str | None]
    version_note: NotRequired[str | None]
    profile: NotRequired[MusicalProfile | None]
    regions: NotRequired[list[SourceRegion] | None]


class SelectionIntent(TypedDict, total=False):
    __pydantic_config__ = {"extra": "forbid"}  # noqa: RUF012 - Reject misspelled constraints at transport boundary
    setting: Literal["warm_up", "peak_time", "after_hours", "open"]
    direction: Literal["hold", "lift", "left_turn", "explore"]
    target_energy: float | None
    tags: list[str]
    creativity: float | None
    max_stretch_percent: float | None
    require_local_audio: bool
    avoid_track_ids: list[str]
    avoid_pairs: list[list[str]]


class SetBrief(TypedDict, total=False):
    __pydantic_config__ = {"extra": "forbid"}  # noqa: RUF012 - Reject misspelled constraints at transport boundary
    title: str
    setting: Literal["warm_up", "peak_time", "after_hours", "open"]
    target_minutes: float | None
    track_count: int
    anchor_track_ids: list[str]
    excluded_track_ids: list[str]
    avoid_pairs: list[list[str]]
    intent: SelectionIntent
    performance_fraction: float
    overlap_seconds: float


class PlanHandle(TypedDict):
    schema: Literal["pocket.set-plan-handle/v1"]
    path: str
    sha256: str
