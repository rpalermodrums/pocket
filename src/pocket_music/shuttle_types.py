# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded intents for the experimental Shuttle native profile."""

from typing import Literal
from typing_extensions import NotRequired, TypedDict


class ShuttleAction(TypedDict):
    kind: Literal["claim", "release", "load", "play", "pause", "gain", "loop", "sync"]
    deck: NotRequired[Literal["left", "right"]]
    track_id: NotRequired[str]
    value: NotRequired[int]
    duration_seconds: NotRequired[float]
    beats: NotRequired[Literal[8, 16] | None]
    allow_silence: NotRequired[bool]
