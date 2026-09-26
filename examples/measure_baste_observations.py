# SPDX-License-Identifier: MIT
"""Measure what a series of Baste observations costs the running Live process.

Run it on the computer running Live, with one Baste device loaded in an isolated
test set and no other work using that Live instance:

    python examples/measure_baste_observations.py /path/to/new/private-directory \\
        --observations 30 --device-label after --live-version 12.x.y \\
        --live-log /path/to/Live/Log.txt --edit-checkpoints

It samples Live's resident memory (RSS) before the series and after every
observation, and the size of Live's log when you name it. With
--edit-checkpoints it pauses before and after the series so you can add and
delete one track in Live and type how long that took; the log growth across
each edit is recorded beside the time.

Only numbers are written: dispositions, timings, LiveAPI object counts and the
set's size. Track, clip, device and parameter names and values are not, and
neither is the log's location. results.json goes into the new directory. The
results describe Live's responsiveness with the device build you loaded. They
are not a musical result.
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
from pathlib import Path

import pocket_music
from pocket_music import observe_live


def live_rss_kib() -> int | None:
    """Resident memory of the one running Live process, or None unless exactly one is found."""
    try:
        listing = subprocess.run(["ps", "-A", "-o", "rss=,comm="], capture_output=True, text=True,
                                 check=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    sizes = []
    for line in listing.splitlines():
        rss, _, command = line.strip().partition(" ")
        name = Path(command.strip()).name
        if rss.isdigit() and (name == "Live" or name.startswith("Ableton Live")):
            sizes.append(int(rss))
    return sizes[0] if len(sizes) == 1 else None


def log_bytes(path: Path | None) -> int | None:
    try:
        return path.stat().st_size if path else None
    except OSError:
        return None


def set_size(observation: dict) -> dict:
    """How big the observed set is, as counts only."""
    size = {"tracks": len(observation["tracks"]), "return_tracks": len(observation["return_tracks"]),
            "devices": 0, "parameters": 0, "session_clips": 0, "arrangement_clips": 0,
            "objects_read": observation["objects_read"], "property_reads": observation["property_reads"]}

    def count(devices):
        for device in devices:
            size["devices"] += 1
            size["parameters"] += len(device["parameters"])
            for chain in device["chains"]:
                count(chain["devices"])

    for track in [*observation["tracks"], *observation["return_tracks"], observation["main_track"]]:
        size["session_clips"] += sum(1 for slot in track["session_clips"] if slot["present"])
        size["arrangement_clips"] += len(track["arrangement_clips"])
        count(track["devices"])
    return size


def edit_checkpoint(when: str, log: Path | None, rss, ask) -> dict:
    before = log_bytes(log)
    while True:
        typed = ask(f"[{when}] In Live, add one track and delete it, timing both with a stopwatch. "
                    "Type the time in milliseconds (blank to skip) and press Enter: ").strip()
        try:
            milliseconds = float(typed) if typed else None
            break
        except ValueError:
            print("Type a number of milliseconds, or leave it blank.", file=sys.stderr)
    after = log_bytes(log)
    return {"when": when, "add_delete_ms": milliseconds, "live_rss_kib": rss(),
            "live_log_growth_bytes": None if before is None or after is None else after - before}


def measure(observations: int, timeout_seconds: float, *, observe=observe_live, rss=live_rss_kib,
            log: Path | None = None, ask=None) -> dict:
    record = {"schema": "pocket.example.baste-live-cost/v1",
              "environment": {"pocket": pocket_music.__version__, "platform": platform.platform(),
                              "python": platform.python_version()},
              "observations_requested": observations, "set_size": None, "edits": [], "observations": []}
    if ask:
        record["edits"].append(edit_checkpoint("before_series", log, rss, ask))
    record.update(live_rss_kib_before_series=rss(), live_log_bytes_before_series=log_bytes(log))
    for number in range(1, observations + 1):
        result = observe(timeout_seconds=timeout_seconds)
        observation = result.get("observation")
        if observation and record["set_size"] is None:
            record["set_size"] = set_size(observation)
        reported = result.get("live_objects_created") is not None  # Earlier device builds don't report it.
        row = {"n": number, "disposition": result["disposition"], "round_trip_ms": result.get("round_trip_ms"),
               "read_elapsed_ms": result.get("read_elapsed_ms"),
               "release_elapsed_ms": result.get("release_elapsed_ms"),
               "live_objects_created": result.get("live_objects_created"),
               "live_objects_released": result.get("live_objects_released"),
               "release_failed": result.get("release_error") is not None if reported else None,
               "live_rss_kib": rss()}
        record["observations"].append(row)
        print(json.dumps(row), flush=True)
    record["live_log_bytes_after_series"] = log_bytes(log)
    if ask:
        record["edits"].append(edit_checkpoint("after_series", log, rss, ask))
    record["summary"] = summary(record)
    return record


def summary(record: dict) -> dict:
    rows = record["observations"]
    reads = [row["read_elapsed_ms"] for row in rows if row["disposition"] == "ok"]
    counted = all(row["live_objects_created"] is not None for row in rows)
    first, last = record["live_rss_kib_before_series"], rows[-1]["live_rss_kib"] if rows else None
    before, after = record["live_log_bytes_before_series"], record["live_log_bytes_after_series"]
    return {"ok": len(reads), "failed": len(rows) - len(reads),
            "first_read_ms": reads[0] if reads else None, "last_read_ms": reads[-1] if reads else None,
            "median_read_ms": statistics.median(reads) if reads else None,
            "live_objects_created": sum(row["live_objects_created"] for row in rows) if counted else None,
            "live_objects_unreleased": sum(row["live_objects_created"] - row["live_objects_released"]
                                           for row in rows) if counted else None,
            "live_rss_growth_kib": None if first is None or last is None else last - first,
            "live_log_growth_bytes": None if before is None or after is None else after - before}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--observations", type=int, default=30)
    parser.add_argument("--timeout-seconds", type=float, default=35)
    parser.add_argument("--device-label", default="", help="Which device build is loaded, such as before or after")
    parser.add_argument("--live-version", default="", help="Live's version, from About Live")
    parser.add_argument("--live-log", type=Path, help="Live's Log.txt; only its size is recorded")
    parser.add_argument("--edit-checkpoints", action="store_true",
                        help="Pause before and after the series to time a manual track add and delete")
    args = parser.parse_args()
    destination = args.destination.expanduser().resolve()
    if destination.exists():
        raise SystemExit("Choose a destination directory that doesn't exist yet")
    if not 1 <= args.observations <= 1000:
        raise SystemExit("--observations must be between 1 and 1000")
    record = measure(args.observations, args.timeout_seconds, log=args.live_log,
                     ask=input if args.edit_checkpoints else None)
    record.update(device_label=args.device_label, live_version=args.live_version)
    destination.mkdir(parents=True)
    (destination / "results.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"results": str(destination / "results.json"), **record["summary"]}))


if __name__ == "__main__":
    main()
