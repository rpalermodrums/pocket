# SPDX-License-Identifier: AGPL-3.0-only
"""Literal output-frame join envelopes; no inferred seam or musical alignment."""
from typing import Literal

from typing_extensions import TypedDict


class JoinEnvelope(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    boundary_frame: int
    fade_out_frames: int
    fade_in_frames: int
    curve: Literal["linear"]
