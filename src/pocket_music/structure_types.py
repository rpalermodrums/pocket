# SPDX-License-Identifier: AGPL-3.0-only
"""Strict inputs for the file-only attributed phrase and motif ledger."""
from __future__ import annotations

from typing import Literal, NotRequired

from typing_extensions import TypedDict

from .artifact_store import ArtifactHandle
from .material_types import MaterialRecord, Rational


class StructureAttribution(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    actor: str
    actor_kind: Literal['human', 'agent']
    statement: str
    uncertainty: list[str]
    evidence: list[ArtifactHandle]


class MaterialBinding(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    key: str
    material: ArtifactHandle | MaterialRecord


class PhraseSpan(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    start: Rational
    end: Rational


class PhraseNode(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    node_id: str
    kind: Literal['phrase', 'motif_reference']
    label: str
    material_key: str
    material_revision: str
    clip_id: str
    space: Literal['clip_qn']
    span_qn: PhraseSpan
    note_ids: list[str]
    attribution: StructureAttribution


class PhraseRelation(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    relation_id: str
    from_node: str
    to_node: str
    kind: Literal['sequence', 'repeat', 'variation', 'withhold', 'return', 'call_response', 'motif_reference', 'derivation']
    identity_claims: list[Literal['exact_events', 'rhythm', 'pitch_intervals', 'accents', 'perceptual']]
    attribution: StructureAttribution


class StructureDefinition(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012
    label: str
    materials: list[MaterialBinding]
    nodes: list[PhraseNode]
    relations: list[PhraseRelation]
    attribution: StructureAttribution
    cycle_policy: Literal['reject']
    parent_structure: NotRequired[ArtifactHandle | None]
