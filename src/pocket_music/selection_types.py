"""Shared selection vocabulary; annotation is not measured musical truth."""

from __future__ import annotations

from typing import Literal, NotRequired
from typing_extensions import TypedDict


class BagHandle(TypedDict):
    schema: Literal["pocket.record-bag-handle/v1"]
    path: str
    sha256: str


class MusicalProfile(TypedDict, total=False):
    tags: list[str]
    roles: list[str]
    energy: float
    bpm: float
    bpm_candidates: list[float]
    key: str
    vocal_density: float
    notes: str
    provenance: Literal["user", "agent_hypothesis", "measured"]


class SourceRegion(TypedDict):
    start_frame: int
    frames: int
    role: str
    note: NotRequired[str]


class BagTrackInput(TypedDict):
    track_id: str
    title: str
    artists: list[str]
    spotify_uri: NotRequired[str]
    album: NotRequired[str]
    duration_seconds: NotRequired[float]
    explicit: NotRequired[bool]
    available: NotRequired[bool]
    catalog_source: NotRequired[Literal["spotify_ui", "spotify_api", "user_list", "local_manifest"]]
    local_path: NotRequired[str]
    expected_audio_sha256: NotRequired[str]
    version_note: NotRequired[str]
    profile: NotRequired[MusicalProfile]
    regions: NotRequired[list[SourceRegion]]


class SelectionIntent(TypedDict, total=False):
    setting: Literal["warm_up", "peak_time", "after_hours", "open"]
    direction: Literal["hold", "lift", "left_turn", "explore"]
    target_energy: float
    tags: list[str]
    creativity: float
    max_stretch_percent: float
    require_local_audio: bool
    avoid_track_ids: list[str]
    avoid_pairs: list[list[str]]


class SetBrief(TypedDict, total=False):
    title: str
    setting: Literal["warm_up", "peak_time", "after_hours", "open"]
    target_minutes: float
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
