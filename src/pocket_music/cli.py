"""A small JSON CLI over the same public functions used by the MCP server."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path

from . import __version__
from .assets import identify_audio
from .errors import PocketError

# (module, public function, provider destination argument or None).
# With a destination, --output creates that artifact; otherwise it saves the
# response JSON. The parsed record printed to stdout is the library result.
_SPEC_OPERATIONS = {
    "bag": {
        "create": ("record_bag", "create_record_bag", "output_dir"),
        "query": ("record_bag", "query_record_bag", None),
        "revise": ("record_bag", "revise_record_bag", "output_dir"),
    },
    "workshop": {
        "plan": ("set_workshop", "plan_set_routes", "output_dir"),
        "feedback": ("set_workshop", "record_plan_feedback", "output_dir"),
        "replan": ("set_workshop", "replan_set", "output_dir"),
    },
    "on-deck": {
        "prepare": ("on_deck", "prepare_session", "output_dir"),
        "snapshot": ("on_deck", "session_snapshot", None),
        "options": ("on_deck", "session_options", None),
        "update": ("on_deck", "update_session", None),
    },
    "spotify": {
        "import": ("spotify_bridge", "import_spotify_items", "output_dir"),
        "plan": ("spotify_bridge", "plan_spotify_playlist", "output_dir"),
        "execute": ("spotify_bridge", "execute_spotify_playlist", None),
        "verify-ui": ("spotify_bridge", "verify_spotify_playlist_ui", None),
    },
    "acquire": {
        "discover": ("acquisition", "discover_sources", None),
        "formats": ("acquisition", "inspect_source_formats", None),
        "plan": ("acquisition", "plan_acquisition", "output_dir"),
        "run": ("acquisition", "acquire_source", "output_dir"),
    },
    "embeddings": {
        "preflight": ("music_embeddings", "model_preflight", None),
        "build": ("music_embeddings", "build_embedding_index", "output_path"),
        "query": ("music_embeddings", "rank_embedding_query", None),
    },
}


def _read_object(path: str) -> dict:
    with Path(path).expanduser().open() as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise PocketError("The specification must be a JSON object")
    return value


def _emit(value: dict, output: str | None = None) -> None:
    payload = json.dumps(value, indent=2, allow_nan=False) + "\n"
    if output:
        destination = Path(output).expanduser()
        with destination.open("x") as stream:
            stream.write(payload)
    else:
        sys.stdout.write(payload)


def _initial_command(commands, name, compatibility_name, help_text):
    command = commands.add_parser(
        name, aliases=[compatibility_name],
        help=f"{help_text} (compatibility alias: {compatibility_name})",
        description=f"{help_text}. {compatibility_name} remains a compatibility alias.",
    )
    # argparse retains the literal alias by default. Normalize before both
    # provider dispatch and response/artifact --output routing.
    command.set_defaults(command=name)
    return command


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="pocket", description="Inspect sources and sets, and test transitions with Peek, Thread and Stitch.")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    asset = commands.add_parser("identify", help="Identify an exact local recording")
    asset.add_argument("path")
    asset.add_argument("--output", help="Write a new JSON file (never overwrite)")
    track = _initial_command(commands, "peek", "track-map", "Analyze a bounded source passage")
    track.add_argument("path")
    track.add_argument("--start", type=float, default=0.0, help="Original-source seconds")
    track.add_argument("--duration", type=float, default=30.0)
    track.add_argument("--start-frame", type=int, help="Exact source start; pair with --frames")
    track.add_argument("--frames", type=int, help="Exact source frame count; do not mix with seconds flags")
    track.add_argument("--bpm-hint", type=float)
    track.add_argument("--beats-per-bar", type=int, default=4)
    track.add_argument("--output")
    project = _initial_command(commands, "thread", "set-map", "Inspect saved Ableton control and source intent")
    project.add_argument("path")
    project.add_argument("--hash-sources", action="store_true", help="Also hash active source files")
    project.add_argument("--full", action="store_true", help="Explicit full raw inventory instead of a handle")
    project.add_argument("--cache-dir", help="Directory for immutable handle snapshots")
    project.add_argument("--output")
    region = _initial_command(commands, "thread-region", "set-region", "Query a saved handle at arrangement seconds or mm:ss")
    region.add_argument("handle", help="JSON file containing a summary or bare handle")
    region.add_argument("start", help="Seconds, mm:ss or hh:mm:ss")
    region.add_argument("--duration", type=float, default=32)
    region.add_argument("--max-clips", type=int, default=16)
    region.add_argument("--clip-offset", type=int, default=0)
    region.add_argument("--max-events-per-lane", type=int, default=12)
    region.add_argument("--max-bytes", type=int, default=16000)
    region.add_argument("--output")
    search = _initial_command(commands, "thread-find-clips", "find-clips", "Find clip names or IDs in a saved handle")
    search.add_argument("handle")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--offset", type=int, default=0)
    search.add_argument("--output")
    raw = _initial_command(commands, "thread-export", "map-export", "Export the full raw inventory from a verified handle")
    raw.add_argument("handle")
    raw.add_argument("--output", required=True)
    position = _initial_command(commands, "thread-source-position", "source-position", "Map an arrangement beat to original-source time")
    position.add_argument("map", help="Saved Thread JSON (existing set-map schema)")
    position.add_argument("clip_id")
    position.add_argument("beat", type=float)
    position.add_argument("--output")
    stitch_parser = _initial_command(commands, "stitch", "lab", "Prepare and review controlled transition experiments")
    actions = stitch_parser.add_subparsers(dest="action", required=True)
    for name, help_text in [
        ("create", "Create exact comparison excerpts from a variant specification"),
        ("prepare-native", "Prepare a new media-only shifted Ableton candidate"),
    ]:
        command = actions.add_parser(name, help=help_text)
        command.add_argument("--spec", required=True, help="JSON arguments for this operation")
        command.add_argument("--output", required=True, help="New trial directory")
    for name, help_text in [
        ("feedback", "Attach a scoped listener note to the exact heard variant"),
        ("feedback-list", "Retrieve exact scoped listener claims without widening their meaning"),
        ("attach-render", "Check and attach a declared completed native export"),
        ("validate-native", "Verify a collected candidate after relocation; does not observe Live"),
    ]:
        command = actions.add_parser(name, help=help_text)
        command.add_argument("--spec", required=True, help="JSON arguments for this operation")
    for group, operations in _SPEC_OPERATIONS.items():
        command = commands.add_parser(group, help={
            "bag": "Create or navigate a sealed record catalogue",
            "workshop": "Explore constrained routes and scoped feedback",
            "on-deck": "Prepare a session and record manual next-track choices",
            "spotify": "Import a catalogue or execute a reviewed fresh-playlist plan",
            "acquire": "Discover candidates or acquire an explicitly selected recording",
            "embeddings": "Inspect an optional model cache or use prepared local receipts",
        }[group])
        actions = command.add_subparsers(dest="action", required=True)
        for name, (_, function, destination) in operations.items():
            action = actions.add_parser(name, help=function.replace("_", " "))
            action.add_argument("--spec", required=True, help="JSON object of public function arguments")
            output_help = ("New index JSON path (never overwrite)" if destination == "output_path" else
                           "New artifact directory (never overwrite)") if destination else "New response JSON file; otherwise stdout"
            action.add_argument("--output", required=destination is not None, help=output_help)
    workspace = commands.add_parser("workspace", help="Serve the local human workspace; Ctrl-C stops it")
    workspace.add_argument("--workspace-dir", required=True)
    workspace.add_argument("--bag-handle", help="JSON file containing a bag handle or create result")
    workspace.add_argument("--port", type=int, default=0, help="Loopback port; 0 chooses a free port")
    return root


def _dispatch(args: argparse.Namespace):
    if args.command in _SPEC_OPERATIONS:
        module, function, destination = _SPEC_OPERATIONS[args.command][args.action]
        spec = _read_object(args.spec)
        if destination:
            if destination in spec:
                raise PocketError(f"Use --output for the destination; omit {destination} from the specification")
            spec[destination] = args.output
        provider = getattr(importlib.import_module(f".{module}", __package__), function)
        return provider(**spec)
    if args.command == "workspace":
        from .workspace import start_workspace
        stored = _read_object(args.bag_handle) if args.bag_handle else None
        handle = stored.get("handle", stored) if stored else None
        start_workspace(args.workspace_dir, bag_handle=handle, port=args.port)
        return None
    if args.command == "identify":
        return identify_audio(args.path)
    if args.command == "peek":
        from .peek import analyze_region
        return analyze_region(args.path, args.start, args.duration, args.bpm_hint, args.beats_per_bar,
                              start_frame=args.start_frame, frames=args.frames)
    if args.command == "thread":
        from .thread import inspect_set
        from .thread_queries import inspect_set_summary
        if args.full:
            return inspect_set(args.path, hash_sources=args.hash_sources)
        return inspect_set_summary(args.path, cache_dir=args.cache_dir, hash_sources=args.hash_sources)
    if args.command in {"thread-region", "thread-find-clips", "thread-export"}:
        from .thread_queries import export_thread, find_clips, query_set_region
        stored = _read_object(args.handle)
        handle = stored.get("handle", stored)
        if args.command == "thread-region":
            return query_set_region(handle, args.start, args.duration, max_clips=args.max_clips,
                                    clip_offset=args.clip_offset, max_events_per_lane=args.max_events_per_lane,
                                    max_bytes=args.max_bytes)
        if args.command == "thread-find-clips":
            return find_clips(handle, args.query, limit=args.limit, offset=args.offset)
        return export_thread(handle, args.output)
    if args.command == "thread-source-position":
        from .thread import source_position
        return source_position(_read_object(args.map), args.clip_id, args.beat)
    from . import stitch
    from .feedback import query_feedback
    spec = _read_object(args.spec)
    if args.action in {"create", "prepare-native"}:
        if "output_dir" in spec:
            raise PocketError("Use --output for the destination; omit output_dir from the specification")
        spec["output_dir"] = args.output
    action = {
        "create": stitch.create_trial,
        "prepare-native": stitch.prepare_native_trial,
        "feedback": stitch.record_feedback,
        "feedback-list": query_feedback,
        "attach-render": stitch.attach_completed_render,
        "validate-native": stitch.validate_native_trial,
    }[args.action]
    return action(**spec)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        artifact_output = (args.command in {"stitch", "thread-export", "workspace"} or
                           (args.command in _SPEC_OPERATIONS and
                            _SPEC_OPERATIONS[args.command][args.action][2] is not None))
        response_path = None if artifact_output else getattr(args, "output", None)
        # Detect an existing response destination before a state-changing call.
        if response_path:
            destination = Path(response_path).expanduser()
            if os.path.lexists(destination):
                raise PocketError("Response output already exists; choose a new JSON path")
            if not destination.parent.is_dir():
                raise PocketError("Response output parent must already be a directory")
        result = _dispatch(args)
        if result is not None:
            _emit(result, response_path)
        return 0
    except (PocketError, OSError, ValueError, TypeError) as exc:
        sys.stderr.write(json.dumps({"error": type(exc).__name__, "message": str(exc)}) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
