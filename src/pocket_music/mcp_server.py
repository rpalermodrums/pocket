"""Optional, local stdio adapter. All tools call the public library functions."""

from __future__ import annotations

import inspect
import json
from functools import wraps
from typing import Literal, NotRequired, get_type_hints

from typing_extensions import TypedDict

from .selection_types import BagHandle, SelectionIntent


def _compact_response(function, input_types=None):
    """Serialize the same provider record without expanding bounded JSON to prose.

    Resolved signatures preserve input schemas, including nested handle types.
    The CLI/library return exactly the same parsed record.
    """
    @wraps(function)
    def compact(*args, **kwargs):
        return json.dumps(function(*args, **kwargs), ensure_ascii=False,
                          allow_nan=False, separators=(",", ":"))

    hints = {**get_type_hints(function, include_extras=True), **(input_types or {})}
    signature = inspect.signature(function)
    compact.__signature__ = signature.replace(
        parameters=[p.replace(annotation=hints.get(p.name, p.annotation))
                    for p in signature.parameters.values()], return_annotation=str,
    )
    compact.__annotations__ = {**hints, "return": str}
    return compact


class EmbeddingIndexHandle(TypedDict):
    schema: Literal["pocket.embedding-index-handle/v1"]
    path: str
    sha256: str


class EmbeddingAsset(TypedDict):
    # Preserve the complete provider receipt, including header/provenance fields.
    __pydantic_config__ = {"extra": "allow"}  # noqa: RUF012 - TypedDict schema metadata
    sha256: str


class EmbeddingSourceRegion(TypedDict):
    __pydantic_config__ = {"extra": "allow"}  # noqa: RUF012 - TypedDict schema metadata
    source_origin: Literal["independently_acquired", "user_recording"]
    source_start_frame: int
    source_frames: int


class EmbeddingReceipt(TypedDict):
    """Already-computed receipt; submitting one does not run model inference."""
    __pydantic_config__ = {"extra": "allow"}  # noqa: RUF012 - TypedDict schema metadata
    schema: Literal["pocket.music-embedding/v1"]
    model_id: str
    model_revision: str
    checkpoint_sha256: str
    dimension: int
    vector: list[float]
    modality: Literal["audio", "text"]
    asset: NotRequired[EmbeddingAsset]
    source_region: NotRequired[EmbeddingSourceRegion]
    text_origin: NotRequired[Literal["user_authored"]]


# Untyped provider internals still receive a discoverable, strict transport
# contract. Keep the public argument names; provider validation is authoritative.
_SELECTION_INPUTS = {
    "prepare_session": {"bag_handle": BagHandle, "output_dir": str,
                        "current_track_id": str | None, "intent": SelectionIntent | None,
                        "embedding_index": EmbeddingIndexHandle | None},
    "session_snapshot": {"session_dir": str},
    "session_options": {"session_dir": str, "limit": int, "expected_revision": int | None,
                        "expected_sha256": str | None},
    "update_session": {"session_dir": str, "expected_revision": int, "expected_sha256": str,
                       "action": Literal["choose", "skip", "intent"], "track_id": str | None,
                       "intent": SelectionIntent | None, "note": str | None},
    "model_preflight": {"model_dir": str},
    "build_embedding_index": {"receipts": list[EmbeddingReceipt], "output_path": str},
    "rank_embedding_query": {"query_receipt": EmbeddingReceipt, "index_handle": EmbeddingIndexHandle,
                             "limit": int},
}


_TOOL_DESCRIPTIONS = {
    "query_record_bag": "Search a sealed record bag by ID/title/artist/tag; default20 records, explicit paging. Validates local source stamps.",
    "replan_set": "Create new seeded alternatives from a saved plan. Feedback stays bound to the same bag and brief; originals remain unchanged.",
    "prepare_session": "Create an offline Whisker session from a sealed bag. Optional cached embedding index is copied; no inference or deck control.",
    "session_options": "Read up to limit next-track proposals (default6, maximum128) from the current sealed session. Excludes current/played/skipped records; no inference.",
    "session_snapshot": "Read the current session revision, SHA and exact decision history. Use revision+SHA for subsequent updates.",
    "update_session": "Record an explicit manual choose/skip/intent action. Requires current revision+SHA; stale writes fail. Never controls a deck.",
    "model_preflight": "Check the prepared optional model cache against pinned hashes. Does not download, import torch or claim successful inference.",
    "build_embedding_index": "Build a new sealed local index from already-computed eligible audio receipts. Preserves provenance; no model inference.",
    "rank_embedding_query": "Retrieve bounded semantic matches from a prepared index and user-text receipt; no inference or musical approval.",
}

_SELECTION_NAMES = {
    "plan_set_routes": "weave",
    "record_plan_feedback": "weave_feedback",
    "replan_set": "weave_replan",
    "session_options": "whisker",
    "prepare_session": "whisker_prepare",
    "session_snapshot": "whisker_snapshot",
    "update_session": "whisker_update",
}


def build_server(error_format=None):
    import os
    if error_format is None:
        error_format = os.environ.get("POCKET_ERROR_FORMAT", "legacy")
    if error_format not in ("legacy", "v2"):
        raise ValueError("POCKET_ERROR_FORMAT must be legacy or v2")
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.types import ToolAnnotations
    except ImportError as exc:
        raise SystemExit("Install Pocket's agent extra: python -m pip install -e '.[agent]'") from exc
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp.types import TextContent

    # Validate new public contracts before SDK convenience coercion/pre-parsing.
    # Legacy tools retain their established transport behavior.
    from pydantic import TypeAdapter

    from .acquisition import acquire_source, discover_sources, inspect_source_formats, plan_acquisition
    from .assets import identify_audio
    from .baste import build_baste_device, observe_live
    from .error_contracts import error_envelope
    from .errors import PocketError
    from .feedback import query_feedback
    from .music_embeddings import build_embedding_index, model_preflight, rank_embedding_query
    from .peek import analyze_region
    from .pipette import promote_trial, validate_promotion
    from .record_bag import create_record_bag, query_record_bag, revise_record_bag
    from .spotify_bridge import (
        execute_spotify_playlist,
        import_spotify_items,
        plan_spotify_playlist,
        verify_spotify_playlist_ui,
    )
    from .stitch import (
        attach_completed_render,
        create_trial,
        prepare_native_trial,
        record_feedback,
        validate_native_trial,
    )
    from .thread import arrangement_position, source_position
    from .thread_queries import export_thread, find_clips, inspect_set_summary, query_set_region
    from .weave import plan_set_routes, record_plan_feedback, replan_set
    from .whisker import prepare_session, session_options, session_snapshot, update_session

    class PocketServer(FastMCP):
        strict_providers: dict

        async def call_tool(self, name, arguments):
            function = self.strict_providers.get(name)
            if function is not None:
                try:
                    signature = inspect.signature(function)
                    bound = signature.bind(**arguments)
                    hints = get_type_hints(function, include_extras=True)
                    for key, value in bound.arguments.items():
                        TypeAdapter(hints[key]).validate_python(value, strict=True)
                except (TypeError, ValueError) as error:
                    message = json.dumps(error_envelope(error, code='invalid_arguments')) if error_format == 'v2' else f'Invalid {name} arguments: {error}'
                    raise ToolError(message) from error
                # Validation must not rewrite the original JSON before hashing:
                # the SDK's convenience parser coerces integers to floats even
                # after strict validation, changing content-addressed identities.
                try:
                    return [TextContent(type='text', text=_compact_response(function)(**arguments))]
                except PocketError as error:
                    message = json.dumps(error_envelope(error)) if error_format == "v2" else str(error)
                    raise ToolError(message) from error
                except (OSError, TypeError, ValueError) as error:
                    if error_format == "v2":
                        raise ToolError(json.dumps(error_envelope(error))) from error
                    raise
            return await super().call_tool(name, arguments)

        async def list_tools(self):
            result = await super().list_tools()
            for tool in result:
                if tool.name in self.strict_providers:
                    tool.inputSchema['additionalProperties'] = False
            return result

    server = PocketServer("Pocket", instructions=(
        "Use Peek for bounded source evidence, Thread for saved arrangement/source intent, "
        "Stitch for controlled transition trials, Weave for set routes and Whisker for next-record options. "
        "Baste reads fresh live state without saving; runtime IDs are not durable handles. "
        "Pipette promotes an explicit kept saved trial into a new project with sealed lineage. "
        "Prefer these primary names; older tool names "
        "remain compatibility aliases. Keep evidence separate from musical approval. "
        "Never infer bar one solely from tempo. Native export remains supervised. "
        "Trial functions write only new local outputs; preserve baseline recordings and projects. "
        "Record bags distinguish catalog metadata, attributed hypotheses and exact local audio identity. "
        "Selection routes and next-track choices are proposals, not auditions or deck control. "
        "Use query_record_bag for bounded discovery. Use whisker_prepare once, then whisker for options "
        "and whisker_update for explicit "
        "revision-bound updates. Spotify execution creates a fresh private playlist only from a reviewed "
        "plan; credentials must remain in the process environment, never tool arguments. "
        "Acquisition requires a selected source plan; model retrieval uses prepared independent local "
        "audio/user-text receipts, never Spotify content. No inference runs in the live suggestion path."
    ))
    server.add_tool(identify_audio, annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
    ))
    for name, function, read_only in (
        ("baste", observe_live, True), ("baste_build_device", build_baste_device, False),
        ("pipette", promote_trial, False), ("pipette_validate", validate_promotion, True),
    ):
        server.add_tool(_compact_response(function), name=name,
                        description=f"{name}: {inspect.getdoc(function)}", structured_output=False,
                        annotations=ToolAnnotations(readOnlyHint=read_only, destructiveHint=False,
                                                    idempotentHint=function is validate_promotion,
                                                    openWorldHint=False))
    initial_tools = (
        ("peek", "analyze_region", analyze_region),
        ("thread", "inspect_set", inspect_set_summary),
        ("thread_region", "query_set_region", query_set_region),
        ("thread_find_clips", "find_clips", find_clips),
        ("thread_export", "export_set_map", export_thread),
        ("thread_source_position", "source_position", source_position),
        ("thread_arrangement_position", "arrangement_position", arrangement_position),
        ("stitch", "create_trial", create_trial),
        ("stitch_prepare_native", "prepare_native_trial", prepare_native_trial),
        ("stitch_feedback", "record_feedback", record_feedback),
        ("stitch_feedback_list", "query_feedback", query_feedback),
        ("stitch_attach_render", "attach_completed_render", attach_completed_render),
        ("stitch_validate_native", "validate_native_trial", validate_native_trial),
    )
    registrations = []
    for primary, compatibility, function in initial_tools:
        read_only = function in {
            analyze_region, source_position, arrangement_position, query_feedback,
            validate_native_trial, find_clips, query_set_region,
        }
        transport = _compact_response(function) if function in {
            inspect_set_summary, find_clips, query_set_region, export_thread,
        } else function
        annotations = ToolAnnotations(
            readOnlyHint=read_only, destructiveHint=False,
            idempotentHint=read_only, openWorldHint=False,
        )
        structured = False if transport is not function else None
        description = inspect.getdoc(function) or "Read the corresponding provider's saved evidence."
        server.add_tool(transport, name=primary, description=f"{primary}: {description}",
                        structured_output=structured, annotations=annotations)
        registrations.append((primary, compatibility, transport, structured, annotations, description))
    readonly = {query_record_bag, session_options, session_snapshot, model_preflight, rank_embedding_query,
                discover_sources, inspect_source_formats}
    external = {discover_sources, inspect_source_formats, acquire_source, execute_spotify_playlist}
    for function in (
        create_record_bag, query_record_bag, revise_record_bag,
        plan_set_routes, record_plan_feedback, replan_set,
        prepare_session, session_snapshot, session_options, update_session,
        import_spotify_items, plan_spotify_playlist, execute_spotify_playlist, verify_spotify_playlist_ui,
        discover_sources, inspect_source_formats, plan_acquisition, acquire_source,
        model_preflight, build_embedding_index, rank_embedding_query,
    ):
        compatibility = function.__name__
        primary = _SELECTION_NAMES.get(compatibility, compatibility)
        transport = _compact_response(function, _SELECTION_INPUTS.get(compatibility))
        description = _TOOL_DESCRIPTIONS.get(compatibility)
        annotations = ToolAnnotations(
            readOnlyHint=function in readonly, destructiveHint=False,
            idempotentHint=function in readonly, openWorldHint=function in external,
        )
        if primary != compatibility:
            description = description or inspect.getdoc(function) or "Use the corresponding public provider."
            registrations.append((primary, compatibility, transport, False, annotations, description))
            description = f"{primary}: {description}"
        server.add_tool(transport, name=primary, description=description,
                        structured_output=False, annotations=annotations)
    # Reuse the exact callable, signature and transport for old names. Register
    # aliases after all primaries so discovery leads with the current vocabulary.
    for primary, compatibility, transport, structured, annotations, description in registrations:
        server.add_tool(transport, name=compatibility,
                        description=f"Compatibility alias for {primary}. {description}",
                        structured_output=structured, annotations=annotations)
    from importlib import import_module

    from .capabilities import PUBLIC_CAPABILITIES, capabilities_list
    server.strict_providers = {'capabilities_list': capabilities_list}
    server.add_tool(_compact_response(capabilities_list), name='capabilities_list', structured_output=False,
                    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                                idempotentHint=True, openWorldHint=False))
    for name, module, _, _, read_only, description in PUBLIC_CAPABILITIES:
        function = getattr(import_module(f'.{module}', __package__), name)
        server.strict_providers[name] = function
        server.add_tool(_compact_response(function), name=name, description=description,
                        structured_output=False,
                        annotations=ToolAnnotations(readOnlyHint=read_only, destructiveHint=False,
                                                    idempotentHint=True, openWorldHint=False))
    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
