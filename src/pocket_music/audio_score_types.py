# SPDX-License-Identifier: AGPL-3.0-only
"""Authored audio passage membership; coordinates remain in musical context."""
from typing import Literal

from typing_extensions import TypedDict

from .context_types import Attribution


class AudioPassageMember(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    occurrence_id: str
    role: str


class AudioPassage(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    passage_id: str
    label: str
    members: list[AudioPassageMember]
    attribution: Attribution


class AudioPassageRelation(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    relation_id: str
    from_passage: str
    to_passage: str
    kind: Literal["sequence", "repeat", "variation", "withhold", "return", "call_response",
                  "motif_reference", "derivation"]
    attribution: Attribution


class AudioScoreDefinition(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    score_id: str
    title: str
    attribution: Attribution
    passages: list[AudioPassage]
    relations: list[AudioPassageRelation]
