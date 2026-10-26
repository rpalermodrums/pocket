# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit original-rate amplitude recipes for bounded audio-score auditions."""
from typing import Literal

from typing_extensions import TypedDict

from .music_types import Rational


class AudioScoreAmplitudePoint(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    at_frame: int
    gain: Rational


class AudioScoreOccurrenceControl(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    occurrence_id: str
    gain: Rational
    points: list[AudioScoreAmplitudePoint]


class AudioScoreRecipe(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    profile: Literal["same-rate-overlay-linear/v1"]
    controls: list[AudioScoreOccurrenceControl]
