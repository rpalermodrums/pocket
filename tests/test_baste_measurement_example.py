# SPDX-License-Identifier: AGPL-3.0-only
"""The Baste cost example records numbers only and never fills in what a device didn't report."""
import importlib.util
import json
import subprocess
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "measure_baste_observations", Path(__file__).parents[1] / "examples/measure_baste_observations.py")
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)

PRIVATE = "Private vocal take"


def observation():
    parameter = {"name": PRIVATE, "value": 0.5, "display_value": 1000, "min": 0, "max": 1, "automation_state": 0}
    device = {"name": PRIVATE, "class_name": "Eq8", "enabled": True, "parameters": [parameter, parameter],
              "chains": [{"name": PRIVATE, "devices": [{"name": PRIVATE, "parameters": [parameter], "chains": []}]}]}
    track = {"name": PRIVATE, "devices": [device], "arrangement_clips": [{"name": PRIVATE}],
             "session_clips": [{"present": True, "name": PRIVATE}, {"present": False}]}
    empty = {"name": PRIVATE, "devices": [], "arrangement_clips": [], "session_clips": []}
    return {"tracks": [track, track], "return_tracks": [empty], "main_track": empty,
            "objects_read": 40, "property_reads": 400}


def test_series_records_counts_and_timings_but_no_names(tmp_path):
    log = tmp_path / "Log.txt"
    log.write_bytes(b"x" * 100)
    rss = iter(range(1000, 2000, 10))
    replies = iter([{"disposition": "ok", "observation": observation(), "read_elapsed_ms": 50,
                     "release_elapsed_ms": 2, "round_trip_ms": 60, "live_objects_created": 45,
                     "live_objects_released": 45, "release_error": None},
                    {"disposition": "path_invalid", "observation": None, "read_elapsed_ms": 5,
                     "live_objects_created": 4, "live_objects_released": 3,
                     "release_error": "1 of 4 Live objects were not released: id still reads \"7\""},
                    {"disposition": "ok", "observation": observation(), "read_elapsed_ms": 70,
                     "live_objects_created": 45, "live_objects_released": 45, "release_error": None}])
    typed = iter(["oops", "120", "", "90"])

    def grow(size):
        log.write_bytes(log.read_bytes() + b"y" * size)

    def observe(**_):
        grow(3)
        return next(replies)

    def ask(_):
        grow(7)
        return next(typed)

    record = example.measure(3, 10, observe=observe, rss=lambda: next(rss), log=log, ask=ask,
                             edit_after=[3, 0, 1])
    assert PRIVATE not in json.dumps(record) and str(tmp_path) not in json.dumps(record)
    assert record["set_size"] == {"tracks": 2, "return_tracks": 1, "devices": 4, "parameters": 6,
                                  "session_clips": 2, "arrangement_clips": 2, "objects_read": 40,
                                  "property_reads": 400}
    assert [edit["after_observations"] for edit in record["edits"]] == [0, 1, 3]
    assert [edit["live_log_growth_bytes"] for edit in record["edits"]] == [14, 7, 7]
    assert [row["release_failed"] for row in record["observations"]] == [False, True, False]
    # Log growth during the series leaves out the edits made inside it.
    assert record["summary"] == {"ok": 2, "failed": 1, "first_read_ms": 50, "last_read_ms": 70,
                                 "median_read_ms": 60.0, "live_objects_created": 94,
                                 "live_objects_unreleased": 1,
                                 "add_delete_ms": {"0": 120.0, "1": None, "3": 90.0},
                                 "live_rss_growth_kib": 60, "live_log_growth_outside_edits_bytes": 9}


def test_control_series_keeps_the_pauses_and_observes_nothing():
    def observe(**_):
        raise AssertionError("a control series must not observe")

    typed = iter(["100", "150"])
    record = example.measure(5, 10, observe=observe, rss=lambda: 1000, ask=lambda _: next(typed),
                             edit_after=[0, 5], control=True)
    assert record["control"] is True and record["observations"] == []
    assert record["summary"]["add_delete_ms"] == {"0": 100.0, "5": 150.0}
    assert record["summary"]["ok"] == 0 and record["summary"]["live_objects_created"] is None
    assert record["summary"]["live_rss_growth_kib"] == 0


def test_an_earlier_device_build_leaves_release_counts_unknown():
    reply = {"disposition": "ok", "observation": observation(), "read_elapsed_ms": 50}
    record = example.measure(2, 10, observe=lambda **_: reply, rss=lambda: None)
    assert [row["release_failed"] for row in record["observations"]] == [None, None]
    assert record["summary"]["live_objects_created"] is None
    assert record["summary"]["live_objects_unreleased"] is None
    assert record["summary"]["live_rss_growth_kib"] is None


def test_rss_comes_from_exactly_one_live_process(monkeypatch):
    def listing(stdout):
        return lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=stdout)
    live = "  812344 /Applications/Ableton Live 12 Suite.app/Contents/MacOS/Live\n   2048 /usr/sbin/cfprefsd\n"
    monkeypatch.setattr(example.subprocess, "run", listing(live))
    assert example.live_rss_kib() == 812344
    monkeypatch.setattr(example.subprocess, "run", listing(live + live))
    assert example.live_rss_kib() is None
    monkeypatch.setattr(example.subprocess, "run", listing("   2048 /usr/sbin/cfprefsd\n"))
    assert example.live_rss_kib() is None
