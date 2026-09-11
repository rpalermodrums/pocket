"""Optional, local stdio adapter. All tools call the public library functions."""

from __future__ import annotations


def build_server():
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.types import ToolAnnotations
    except ImportError as exc:
        raise SystemExit("Install Pocket's agent extra: python -m pip install -e '.[agent]'") from exc
    from .assets import identify_audio
    from .set_map import arrangement_position, inspect_set, source_position
    from .track_map import analyze_region
    from .transition_lab import (
        attach_completed_render,
        create_trial,
        prepare_native_trial,
        record_feedback,
    )

    server = FastMCP("Pocket", instructions=(
        "Analyze bounded passages and keep evidence separate from musical approval. "
        "Never infer bar one solely from tempo. Native export remains supervised. "
        "Trial functions write only new local outputs; preserve baseline recordings and projects."
    ))
    for function in (
        identify_audio, analyze_region, inspect_set, source_position, arrangement_position,
        create_trial, record_feedback, prepare_native_trial, attach_completed_render,
    ):
        read_only = function in {
            identify_audio, analyze_region, inspect_set, source_position, arrangement_position,
        }
        server.add_tool(function, annotations=ToolAnnotations(
            readOnlyHint=read_only, destructiveHint=False,
            idempotentHint=read_only, openWorldHint=False,
        ))
    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
