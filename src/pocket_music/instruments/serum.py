# SPDX-License-Identifier: AGPL-3.0-only
"""Serum-specialist planning over public general providers; no native adapter yet."""
from __future__ import annotations

from typing import Literal

from ..artifact_store import ArtifactHandle, read_record
from ..errors import PocketError
from ..instrument_types import InstrumentObservation, SoundAlternative
from .core import instrument_inspect, preset_catalog, sound_plan


def serum_inspect(*, store_root: str, request_id: str,
                  scope: Literal["installed", "supplied_observation"] = "installed",
                  product: Literal["serum1", "serum2", "serum_fx1", "serum_fx2"] = "serum2",
                  plugin_roots: list[str] | None = None, observation: InstrumentObservation | None = None,
                  limit: int = 50, offset: int = 0) -> dict:
    if product not in {"serum1", "serum2", "serum_fx1", "serum_fx2"}:
        raise PocketError("Expected a distinct Serum instrument or FX identity")
    return instrument_inspect(store_root=store_root, request_id=request_id, scope=scope, product=product,
                              plugin_roots=plugin_roots, observation=observation, limit=limit, offset=offset)


def serum_presets(*, store_root: str, operation: Literal["scan", "query"],
                  roots: list[str] | None = None, catalog: ArtifactHandle | None = None,
                  product: Literal["serum1", "serum2"] = "serum2", query: str = "",
                  limit: int = 50, offset: int = 0, request_id: str | None = None) -> dict:
    if product not in {"serum1", "serum2"}:
        raise PocketError("Serum FX preset compatibility has not been qualified")
    return preset_catalog(store_root=store_root, operation=operation, roots=roots, catalog=catalog,
                          product=product, query=query, limit=limit, offset=offset, request_id=request_id)


def serum_plan(*, store_root: str, request_id: str, brief: str, state: ArtifactHandle,
               alternatives: list[SoundAlternative], expected_layout_sha256: str,
               expected_topology_token: str, locks: list[str] | None = None,
               context: ArtifactHandle | None = None, performance: ArtifactHandle | None = None,
               max_alternatives: int = 3) -> dict:
    record = read_record(state, store_root, "pocket.instrument-state/v1")
    identity = record.get("observation", {}).get("identity", {})
    if identity.get("product") not in {"serum1", "serum2", "serum_fx1", "serum_fx2"}:
        raise PocketError("State is not an identified Serum instrument or FX profile")
    return sound_plan(store_root=store_root, request_id=request_id, brief=brief, recipe="parameter_alternatives",
                      state=state, alternatives=alternatives, expected_layout_sha256=expected_layout_sha256,
                      expected_topology_token=expected_topology_token, locks=locks, context=context,
                      performance=performance, max_alternatives=max_alternatives)
