"""A small JSON CLI over the same public functions used by the MCP server."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .assets import identify_audio
from .errors import PocketError


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


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="pocket", description="Map music, inspect sets and test transitions.")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    asset = commands.add_parser("identify", help="Identify an exact local recording")
    asset.add_argument("path")
    asset.add_argument("--output", help="Write a new JSON file (never overwrite)")
    track = commands.add_parser("track-map", help="Analyze a bounded source passage")
    track.add_argument("path")
    track.add_argument("--start", type=float, default=0.0, help="Original-source seconds")
    track.add_argument("--duration", type=float, default=30.0)
    track.add_argument("--start-frame", type=int, help="Exact source start; pair with --frames")
    track.add_argument("--frames", type=int, help="Exact source frame count; do not mix with seconds flags")
    track.add_argument("--bpm-hint", type=float)
    track.add_argument("--beats-per-bar", type=int, default=4)
    track.add_argument("--output")
    project = commands.add_parser("set-map", help="Inspect saved Ableton control and source intent")
    project.add_argument("path")
    project.add_argument("--hash-sources", action="store_true", help="Also hash active source files")
    project.add_argument("--full", action="store_true", help="Explicit full raw inventory instead of a handle")
    project.add_argument("--cache-dir", help="Directory for immutable handle snapshots")
    project.add_argument("--output")
    region = commands.add_parser("set-region", help="Query a saved handle at arrangement seconds or mm:ss")
    region.add_argument("handle", help="JSON file containing a summary or bare handle")
    region.add_argument("start", help="Seconds, mm:ss or hh:mm:ss")
    region.add_argument("--duration", type=float, default=32)
    region.add_argument("--max-clips", type=int, default=16)
    region.add_argument("--clip-offset", type=int, default=0)
    region.add_argument("--max-events-per-lane", type=int, default=12)
    region.add_argument("--max-bytes", type=int, default=16000)
    region.add_argument("--output")
    search = commands.add_parser("find-clips", help="Find clip names or IDs in a saved handle")
    search.add_argument("handle")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--offset", type=int, default=0)
    search.add_argument("--output")
    raw = commands.add_parser("map-export", help="Export the full raw inventory from a verified handle")
    raw.add_argument("handle")
    raw.add_argument("--output", required=True)
    position = commands.add_parser("source-position", help="Map an arrangement beat to original-source time")
    position.add_argument("map", help="Saved Set Map JSON")
    position.add_argument("clip_id")
    position.add_argument("beat", type=float)
    position.add_argument("--output")
    lab = commands.add_parser("lab", help="Prepare and review controlled transition experiments")
    actions = lab.add_subparsers(dest="action", required=True)
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
    return root


def _dispatch(args: argparse.Namespace) -> dict:
    if args.command == "identify":
        return identify_audio(args.path)
    if args.command == "track-map":
        from .track_map import analyze_region
        return analyze_region(args.path, args.start, args.duration, args.bpm_hint, args.beats_per_bar,
                              start_frame=args.start_frame, frames=args.frames)
    if args.command == "set-map":
        from .set_map import inspect_set
        from .set_queries import inspect_set_summary
        if args.full:
            return inspect_set(args.path, hash_sources=args.hash_sources)
        return inspect_set_summary(args.path, cache_dir=args.cache_dir, hash_sources=args.hash_sources)
    if args.command in {"set-region", "find-clips", "map-export"}:
        from .set_queries import export_set_map, find_clips, query_set_region
        stored = _read_object(args.handle)
        handle = stored.get("handle", stored)
        if args.command == "set-region":
            return query_set_region(handle, args.start, args.duration, max_clips=args.max_clips,
                                    clip_offset=args.clip_offset, max_events_per_lane=args.max_events_per_lane,
                                    max_bytes=args.max_bytes)
        if args.command == "find-clips":
            return find_clips(handle, args.query, limit=args.limit, offset=args.offset)
        return export_set_map(handle, args.output)
    if args.command == "source-position":
        from .set_map import source_position
        return source_position(_read_object(args.map), args.clip_id, args.beat)
    from . import transition_lab
    from .feedback import query_feedback
    spec = _read_object(args.spec)
    if args.action in {"create", "prepare-native"}:
        if "output_dir" in spec:
            raise PocketError("Use --output for the destination; omit output_dir from the specification")
        spec["output_dir"] = args.output
    action = {
        "create": transition_lab.create_trial,
        "prepare-native": transition_lab.prepare_native_trial,
        "feedback": transition_lab.record_feedback,
        "feedback-list": query_feedback,
        "attach-render": transition_lab.attach_completed_render,
        "validate-native": transition_lab.validate_native_trial,
    }[args.action]
    return action(**spec)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = _dispatch(args)
        _emit(result, getattr(args, "output", None) if args.command not in {"lab", "map-export"} else None)
        return 0
    except (PocketError, OSError, ValueError, TypeError) as exc:
        sys.stderr.write(json.dumps({"error": type(exc).__name__, "message": str(exc)}) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
