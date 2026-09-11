"""Optional, local stdio adapter. All tools call the public library functions."""

from __future__ import annotations

from functools import wraps
import inspect
import json
from typing import get_type_hints


def _compact_response(function):
    """Serialize the same provider record without expanding bounded JSON to prose.

    Resolved signatures preserve input schemas, including nested handle types.
    The CLI/library return exactly the same parsed record.
    """
    @wraps(function)
    def compact(*args, **kwargs):
        return json.dumps(function(*args, **kwargs), ensure_ascii=False,
                          allow_nan=False, separators=(",", ":"))

    hints = get_type_hints(function, include_extras=True)
    signature = inspect.signature(function)
    compact.__signature__ = signature.replace(
        parameters=[p.replace(annotation=hints.get(p.name, p.annotation))
                    for p in signature.parameters.values()], return_annotation=str,
    )
    compact.__annotations__ = {**hints, "return": str}
    return compact


def build_server():
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.types import ToolAnnotations
    except ImportError as exc:
        raise SystemExit("Install Pocket's agent extra: python -m pip install -e '.[agent]'") from exc
    from .assets import identify_audio
    from .feedback import query_feedback
    from .set_map import arrangement_position, source_position
    from .set_queries import export_set_map, find_clips, inspect_set_summary, query_set_region
    from .track_map import analyze_region
    from .transition_lab import (
        attach_completed_render,
        create_trial,
        prepare_native_trial,
        record_feedback,
        validate_native_trial,
    )

    server = FastMCP("Pocket", instructions=(
        "Analyze bounded passages and keep evidence separate from musical approval. "
        "Never infer bar one solely from tempo. Native export remains supervised. "
        "Trial functions write only new local outputs; preserve baseline recordings and projects."
    ))
    for function in (
        identify_audio, analyze_region, inspect_set_summary, source_position, arrangement_position,
        find_clips, query_set_region, export_set_map,
        create_trial, record_feedback, query_feedback, prepare_native_trial,
        validate_native_trial, attach_completed_render,
    ):
        read_only = function in {
            identify_audio, analyze_region, source_position, arrangement_position, query_feedback,
            validate_native_trial, find_clips, query_set_region,
        }
        transport = _compact_response(function) if function in {
            inspect_set_summary, find_clips, query_set_region, export_set_map,
        } else function
        server.add_tool(transport, name="inspect_set" if function is inspect_set_summary else None,
                       structured_output=False if transport is not function else None,
                       annotations=ToolAnnotations(
            readOnlyHint=read_only, destructiveHint=False,
            idempotentHint=read_only, openWorldHint=False,
        ))
    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
