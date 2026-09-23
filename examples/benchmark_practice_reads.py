"""Measure verified practice reads on generated fixtures; changes no algorithm.

Run: python examples/benchmark_practice_reads.py /path/to/new/private-directory
Options: --runs N (default 20), --warmup N (default 2), --fixtures small,three,duration,ancestry

The destination must not exist. Synthetic stores are created inside it, then each
read-heavy provider call is timed through its public function. Separate passes
record elapsed time, tracemalloc peak and artifact-read/verification counts, so
instrumentation never inflates the timing pass. The operating-system page cache is
left warm (fixtures were just written); Pocket itself caches nothing between calls.
Results are written to results.json and summary.md. They are technical evidence,
not a musical or listening result.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import math
import os
import platform
import resource
import statistics
import subprocess
import sys
import time
import tracemalloc
from fractions import Fraction
from pathlib import Path

import numpy as np
import scipy
import soundfile as sf

import pocket_music
from pocket_music import (
    artifact_store,
    audio_region_capture,
    context_bind_interpretation,
    context_create,
    context_edit,
    context_edit_query,
    context_query,
    interpretation_create,
    interpretation_query,
    musical_time,
    practice_compare,
    practice_compare_processed,
    practice_compare_revisions,
    practice_envelope,
    practice_feedback,
    practice_feedback_query,
    practice_preview,
    practice_query,
    practice_render,
)
from pocket_music.assets import sha256_file

PROFILE = "browser-pcm16-original-rate/v1"
AUTHOR = {"actor": "benchmark fixture", "actor_kind": "agent", "statement": "Generated technical fixture",
          "uncertainty": ["No musical or listening claim"]}


def q(value):
    value = Fraction(value)
    return {"n": value.numerator, "d": value.denominator}


def source_wav(path, seconds, rate, channels, seed):
    rng = np.random.default_rng(seed)
    frames = seconds * rate
    t = np.arange(frames) / rate
    columns = [0.2 * np.sin(2 * np.pi * (110 * (c + 1)) * t) + 0.02 * rng.standard_normal(frames)
               for c in range(channels)]
    sf.write(path, np.stack(columns, axis=1), rate, subtype="PCM_16")
    return path


def capture(store, name, path, start, frames):
    return audio_region_capture(store_root=store, request_id=name, source={
        "path": str(path), "expected_sha256": sha256_file(path), "start_frame": start, "frames": frames,
        "source_origin": "independently_acquired"})["artifacts"]["region"]


def clock(store, name, end_qn):
    return musical_time("create", store, request_id=name, definition={
        "source_context": {"schema": "pocket.time-context/v1", "context_id": name, "attribution": "Synthetic"},
        "domain_qn": {"start": q(0), "end": q(end_qn)},
        "tempo": [{"at_qn": q(0), "bpm": q(120), "interpolation": "step"}],
        "host_origin": {"arrangement_qn": q(0), "host_seconds": q(0)}})["artifacts"]["time_map"]


def context_with(store, name, sources, occurrences, end_qn, anchors=()):
    return context_create(store, name, {
        "context_id": name, "title": name, "attribution": AUTHOR,
        "sources": [{"clock_id": clock_id, "region": region} for clock_id, region in sources],
        "timelines": [{"clock_id": "practice", "time_map": clock(store, name + "-clock", end_qn)}],
        "occurrences": occurrences, "anchors": list(anchors), "materials": []})["artifacts"]["context"]


def occurrence(identifier, clock_id, span, start_qn, rate):
    length = Fraction(2 * (span[1] - span[0]), rate)
    return {"occurrence_id": identifier, "source_clock_id": clock_id, "timeline_clock_id": "practice",
            "source_span_frames": list(span), "timeline_span_qn": [q(start_qn), q(start_qn + length)]}


def feedback(store, name, comparison, render, interval, kind="agent"):
    return practice_feedback(store, name, comparison, render, interval, "benchmark fixture", kind,
                             "Generated technical fixture; no listening performed")["artifacts"]["feedback"]


def envelope(store, name, render, boundary, frames):
    return practice_envelope(store, name, render, [{"boundary_frame": boundary, "fade_out_frames": frames,
                             "fade_in_frames": frames, "curve": "linear"}], AUTHOR)["artifacts"]["render"]


def small_fixture(root):
    """One 8 kHz mono passage repeated, the example-sized practice path."""
    store = str(root / "store")
    path = source_wav(root / "source.wav", 3, 8000, 1, 1)
    region = capture(store, "capture", path, 700, 20000)
    occurrences = [occurrence(n, "recording", s, i * 2, 8000) for i, (n, s) in
                   enumerate([("first", (1700, 9700)), ("again", (1700, 9700)), ("alternative", (11000, 19000))])]
    anchor = {"anchor_id": "internal", "kind": "bar_one", "label": "Authored", "attribution": AUTHOR,
              "position": {"clock_id": "recording", "space": "source_frame", "value": 3700}}
    context = context_with(store, "context", [("recording", region)], occurrences, 8, [anchor])
    raw = practice_render(store, "raw", context, ["first", "again"])["artifacts"]["render"]
    alt = practice_render(store, "alt", context, ["alternative"])["artifacts"]["render"]
    comparison = practice_compare(store, "compare", raw, [alt], "Benchmark", True)["artifacts"]["comparison"]
    edit = context_edit(store, "edit", context, [{"kind": "occurrence_slip_source", "occurrence_ids": ["first", "again"],
                        "delta_frames": 400}], [], AUTHOR)
    child = practice_render(store, "child", edit["artifacts"]["context"], ["first", "again"])["artifacts"]["render"]
    revision = practice_compare_revisions(store, "revision", raw, [child], [edit["artifacts"]["edit"]], [{
        "variant": child, "pairs": [{"baseline_occurrence_id": n, "variant_occurrence_id": n,
                                     "baseline_interval_frames": [i * 8000, (i + 1) * 8000],
                                     "variant_interval_frames": [i * 8000, (i + 1) * 8000]}
                                    for i, n in enumerate(["first", "again"])]}],
        "Benchmark")["artifacts"]["comparison"]
    joined = envelope(store, "env", raw, 8000, 40)
    processed = practice_compare_processed(store, "processed", raw, [joined], "Benchmark")["artifacts"]["comparison"]
    preview_raw = practice_preview(store, "preview-raw", raw, PROFILE)["artifacts"]["preview"]
    preview_env = practice_preview(store, "preview-env", joined, PROFILE)["artifacts"]["preview"]
    interpretation = interpretation_create(store, "interp", context, "recording", {
        "kind": "onset", "status": "authored", "source_frame_q": q(3000)}, AUTHOR)["artifacts"]["interpretation"]
    bound = context_bind_interpretation(store, "bind", context, interpretation, {
        "kind": "anchor", "binding_id": "b", "anchor_id": "authored-onset", "label": "Onset"},
        AUTHOR)["artifacts"]["context"]
    reports = [feedback(store, f"fb-{i}", processed, r, [0, 4000]) for i, r in enumerate([raw, joined])]
    state = {"i": 0}

    def new_report():
        state["i"] += 1
        return feedback(store, f"bench-new-{state['i']}", processed, joined, [0, 4000])

    operations = {
        "context_query.summary": lambda: context_query(store, context),
        "context_query.bindings": lambda: context_query(store, bound, "bindings"),
        "interpretation_query": lambda: interpretation_query(store, interpretation),
        "context_edit_query": lambda: context_edit_query(store, edit["artifacts"]["edit"]),
        "practice_query.render": lambda: practice_query(store, raw),
        "practice_query.envelope": lambda: practice_query(store, joined),
        "practice_query.comparison": lambda: practice_query(store, comparison),
        "practice_query.revision_comparison": lambda: practice_query(store, revision),
        "practice_query.processed_comparison": lambda: practice_query(store, processed),
        "practice_query.preview_raw": lambda: practice_query(store, preview_raw),
        "practice_query.preview_envelope": lambda: practice_query(store, preview_env),
        "practice_query.feedback": lambda: practice_query(store, reports[0]),
        "practice_feedback_query.all": lambda: practice_feedback_query(store, reports),
        "practice_preview.replay": lambda: practice_preview(store, "preview-env", joined, PROFILE),
        "practice_feedback.create": new_report,
    }
    dimensions = {"sample_rate": 8000, "channels": 1, "render_frames": 16000, "renders": 4, "previews": 2,
                  "reports": len(reports), "edit_depth": 1}
    return store, operations, dimensions, [context, comparison, revision, processed, *reports]


def three_fixture(root):
    """Three 44.1 kHz stereo passages: 8 s repeated twice, 5/15 ms joins, previews and reports."""
    store = str(root / "store")
    path = source_wav(root / "source.wav", 70, 44100, 2, 3)
    rate, passage = 44100, 8 * 44100
    comparisons, previews, reports, renders = [], [], [], []
    for index in range(3):
        start = index * 22 * rate
        region = capture(store, f"capture-{index}", path, start, 20 * rate)
        span = (start + rate, start + rate + passage)
        context = context_with(store, f"context-{index}", [("recording", region)],
                               [occurrence("a", "recording", span, 0, rate), occurrence("b", "recording", span, 16, rate)],
                               32)
        raw = practice_render(store, f"raw-{index}", context, ["a", "b"])["artifacts"]["render"]
        variants = [envelope(store, f"env-{index}-{ms}", raw, passage, rate * ms // 1000) for ms in (5, 15)]
        comparison = practice_compare_processed(store, f"cmp-{index}", raw, variants,
                                                "Benchmark")["artifacts"]["comparison"]
        comparisons.append(comparison)
        renders.extend([raw, *variants])
        for member in (raw, *variants):
            previews.append(practice_preview(store, f"pv-{index}-{len(previews)}", member, PROFILE)["artifacts"]["preview"])
            reports.extend(feedback(store, f"fb-{index}-{len(reports)}-{k}", comparison, member,
                                    [passage - rate + k, passage + rate]) for k in range(2))
    operations = {
        "practice_query.processed_comparison": lambda: practice_query(store, comparisons[0]),
        "practice_query.render_16s_stereo": lambda: practice_query(store, renders[0]),
        "practice_query.envelope_16s_stereo": lambda: practice_query(store, renders[1]),
        "practice_query.preview_16s_stereo": lambda: practice_query(store, previews[1]),
        "practice_feedback_query.18_reports": lambda: practice_feedback_query(store, reports, limit=128,
                                                                               max_bytes=65536),
        "practice_preview.replay_16s_stereo": lambda: practice_preview(store, "pv-0-1", renders[1], PROFILE),
        "review_screen.load_one_passage": lambda: ([practice_query(store, comparisons[0])]
                                                   + [practice_query(store, p) for p in previews[:3]]
                                                   + [practice_feedback_query(store, reports[:6])]),
    }
    dimensions = {"sample_rate": rate, "channels": 2, "render_frames": 2 * passage, "passages": 3,
                  "renders": len(renders), "previews": len(previews), "reports": len(reports)}
    return store, operations, dimensions, [*comparisons, *previews, *reports]


def duration_fixture(root):
    """Near the 120 s / 64 MiB render bound: 48 kHz mono, six 20 s occurrences."""
    store = str(root / "store")
    rate = 48000
    path = source_wav(root / "source.wav", 21, rate, 1, 5)
    region = capture(store, "capture", path, 0, 20 * rate)
    occurrences = [occurrence(f"o{i}", "recording", (0, 20 * rate), 40 * i, rate) for i in range(6)]
    context = context_with(store, "context", [("recording", region)], occurrences, 240)
    raw = practice_render(store, "raw", context, [f"o{i}" for i in range(6)])["artifacts"]["render"]
    joined = envelope(store, "env", raw, 20 * rate, 240)
    preview = practice_preview(store, "preview", raw, PROFILE)["artifacts"]["preview"]
    operations = {
        "practice_query.render_120s": lambda: practice_query(store, raw),
        "practice_query.envelope_120s": lambda: practice_query(store, joined),
        "practice_query.preview_120s": lambda: practice_query(store, preview),
        "practice_preview.replay_120s": lambda: practice_preview(store, "preview", raw, PROFILE),
    }
    dimensions = {"sample_rate": rate, "channels": 1, "render_frames": 120 * rate, "render_double_bytes":
                  120 * rate * 8, "previews": 1}
    return store, operations, dimensions, [raw, joined, preview]


def ancestry_fixture(root):
    """Maximum edit ancestry (16) and eight variants, with 128 reports on one comparison."""
    store = str(root / "store")
    path = source_wav(root / "source.wav", 3, 8000, 1, 7)
    region = capture(store, "capture", path, 700, 20000)
    occurrences = [occurrence(n, "recording", (1700, 9700), i * 2, 8000) for i, n in enumerate(["first", "again"])]
    context = context_with(store, "context", [("recording", region)], occurrences, 8)
    baseline = practice_render(store, "baseline", context, ["first", "again"])["artifacts"]["render"]
    receipts, variants, current = [], [], context
    for depth in range(16):
        result = context_edit(store, f"edit-{depth}", current, [{"kind": "occurrence_slip_source",
                              "occurrence_ids": ["first", "again"], "delta_frames": 100}], [], AUTHOR)
        receipts.append(result["artifacts"]["edit"])
        current = result["artifacts"]["context"]
        if depth >= 8:  # variants at depths 9–16; the deepest uses every receipt
            variants.append(practice_render(store, f"variant-{depth}", current, ["first", "again"])["artifacts"]["render"])
    pairs = [{"baseline_occurrence_id": n, "variant_occurrence_id": n, "baseline_interval_frames": [i * 8000, (i + 1) * 8000],
              "variant_interval_frames": [i * 8000, (i + 1) * 8000]} for i, n in enumerate(["first", "again"])]
    comparison = practice_compare_revisions(store, "revision", baseline, variants, receipts,
                                            [{"variant": v, "pairs": pairs} for v in variants],
                                            "Benchmark")["artifacts"]["comparison"]
    members = [baseline, *variants]
    reports = [feedback(store, f"fb-{i}", comparison, members[i % len(members)], [0, 800 + i]) for i in range(128)]
    operations = {
        "context_edit_query.depth16": lambda: context_edit_query(store, receipts[-1]),
        "practice_query.revision_comparison_8x16": lambda: practice_query(store, comparison),
        "practice_query.feedback_on_8x16": lambda: practice_query(store, reports[0]),
        "practice_feedback_query.128_reports": lambda: practice_feedback_query(store, reports, limit=128,
                                                                                max_bytes=65536),
    }
    dimensions = {"sample_rate": 8000, "channels": 1, "render_frames": 16000, "edit_depth": 16,
                  "variants": len(variants), "edit_receipts": len(receipts), "reports": len(reports)}
    return store, operations, dimensions, [comparison, *reports]


FIXTURES = {"small": small_fixture, "three": three_fixture, "duration": duration_fixture,
            "ancestry": ancestry_fixture}
LOADERS = {"practice_audio": ("load_practice_render", "load_comparison", "load_practice_feedback"),
           "practice_envelopes": ("load_practice_envelope", "load_processed_comparison"),
           "practice_comparisons": ("load_revision_comparison",),
           "practice_previews": ("load_practice_preview",),
           "musical_context": ("load_context",),
           "context_edits": ("load_context_edit",)}


class Counter:
    """Wrap artifact reads and domain loaders everywhere they were imported by name."""

    def __init__(self):
        self.reads = self.bytes = self.walks = 0
        self.unique = set()
        self.loaders = {}
        self.patched = []

    def install(self):
        original_read = artifact_store.read_bytes
        original_walk = artifact_store._verify_handles

        @functools.wraps(original_read)
        def read(handle, store_root):
            payload = original_read(handle, store_root)
            self.reads += 1
            self.bytes += len(payload)
            self.unique.add(handle["sha256"])
            return payload

        @functools.wraps(original_walk)
        def walk(value, root):
            self.walks += 1
            return original_walk(value, root)

        replacements = {original_read: read, original_walk: walk}
        for module_name, names in LOADERS.items():
            module = sys.modules[f"pocket_music.{module_name}"]
            for name in names:
                original = getattr(module, name)
                replacements[original] = self._loader(name, original)
        for module in [m for n, m in list(sys.modules.items()) if n.startswith("pocket_music") and m]:
            for attribute, value in list(vars(module).items()):
                if callable(value) and value in replacements:
                    self.patched.append((module, attribute, value))
                    setattr(module, attribute, replacements[value])

    def _loader(self, name, original):
        @functools.wraps(original)
        def counted(*args, **kwargs):
            self.loaders[name] = self.loaders.get(name, 0) + 1
            return original(*args, **kwargs)
        return counted

    def uninstall(self):
        for module, attribute, value in reversed(self.patched):
            setattr(module, attribute, value)
        self.patched.clear()

    def reset(self):
        self.reads = self.bytes = self.walks = 0
        self.unique = set()
        self.loaders = {}

    def snapshot(self):
        return {"artifact_reads": self.reads, "unique_artifacts": len(self.unique), "bytes_read": self.bytes,
                "graph_walks": self.walks, "loader_calls": dict(sorted(self.loaders.items()))}


def nearest_rank(values, percentile):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile / 100 * len(ordered)) - 1)]


def measure(operation, runs, warmup, counter):
    for _ in range(warmup):
        operation()
    elapsed = []
    for _ in range(runs):
        start = time.perf_counter()
        operation()
        elapsed.append(time.perf_counter() - start)
    tracemalloc.start()
    operation()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    counter.install()
    counter.reset()
    try:
        operation()
    finally:
        counter.uninstall()
    return {"runs": runs, "warmup": warmup, "median_s": statistics.median(elapsed),
            "p95_s": nearest_rank(elapsed, 95), "min_s": min(elapsed), "max_s": max(elapsed),
            "tracemalloc_peak_bytes": peak, "counts": counter.snapshot()}


def environment():
    def run(*command):
        try:
            return subprocess.run(command, capture_output=True, text=True, check=True, timeout=10,
                                  cwd=Path(__file__).resolve().parent).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
    cpu = None
    if Path("/proc/cpuinfo").exists():
        cpu = next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                    if line.startswith("model name")), None)
    return {"platform": platform.platform(), "machine": platform.machine(), "cpu": cpu or platform.processor(),
            "logical_cpus": os.cpu_count(), "python": sys.version.split()[0], "pocket_music": pocket_music.__version__,
            "numpy": np.__version__, "scipy": scipy.__version__, "soundfile": sf.__version__,
            "libsndfile": sf.__libsndfile_version__, "commit": run("git", "rev-parse", "HEAD"),
            "worktree_dirty": bool(run("git", "status", "--porcelain")) if run("git", "rev-parse", "HEAD") else None,
            "memory_bytes": os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") if hasattr(os, "sysconf") else None}


def summary_markdown(result):
    lines = ["# Practice read benchmark", "",
             (f"Commit `{result['environment']['commit']}` · Python {result['environment']['python']} · "
              f"{result['environment']['cpu']} ({result['environment']['logical_cpus']} logical CPUs)"), "",
             (f"Runs {result['policy']['runs']} after {result['policy']['warmup']} warm-up; p95 is nearest-rank. "
              "OS page cache warm; no application cache."), "",
             "| Fixture | Operation | Median ms | p95 ms | Peak MiB | Reads | Unique | MiB read | Walks | Loader calls |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    for row in result["operations"]:
        counts = row["counts"]
        loaders = ", ".join(f"{k.removeprefix('load_')}={v}" for k, v in counts["loader_calls"].items())
        lines.append(f"| {row['fixture']} | {row['operation']} | {row['median_s'] * 1000:.1f} | "
                     f"{row['p95_s'] * 1000:.1f} | {row['tracemalloc_peak_bytes'] / 2**20:.1f} | "
                     f"{counts['artifact_reads']} | {counts['unique_artifacts']} | {counts['bytes_read'] / 2**20:.1f} | "
                     f"{counts['graph_walks']} | {loaders} |")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--fixtures", default="small,three,duration,ancestry")
    args = parser.parse_args()
    destination = args.destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=False)
    names = args.fixtures.split(",")
    if any(name not in FIXTURES for name in names) or args.runs < 1 or args.warmup < 0:
        raise SystemExit("Unknown fixture or invalid run counts")
    result = {"schema": "pocket.benchmark-practice-reads/v1", "environment": environment(),
              "policy": {"runs": args.runs, "warmup": args.warmup, "percentile": "nearest-rank",
                         "os_page_cache": "warm; fixtures were just written and not dropped",
                         "application_cache": "none; each public call revalidates retained evidence",
                         "instrumentation": "separate tracemalloc and read-count passes after timing"},
              "fixtures": {}, "operations": []}
    counter = Counter()
    for name in names:
        root = destination / name
        root.mkdir()
        started = time.perf_counter()
        store, operations, dimensions, identities = FIXTURES[name](root)
        result["fixtures"][name] = {"dimensions": dimensions, "build_s": time.perf_counter() - started,
                                    "identity_sha256": hashlib.sha256(
                                        artifact_store.canonical_bytes(identities)).hexdigest(),
                                    "store_bytes": sum(p.stat().st_size for p in Path(store).rglob("*") if p.is_file())}
        for operation, call in operations.items():
            row = {"fixture": name, "operation": operation, **measure(call, args.runs, args.warmup, counter)}
            result["operations"].append(row)
            print(json.dumps({"fixture": name, "operation": operation, "median_ms": round(row["median_s"] * 1000, 1),
                              "p95_ms": round(row["p95_s"] * 1000, 1)}), flush=True)
    result["process_max_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    (destination / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    (destination / "summary.md").write_text(summary_markdown(result))
    print(json.dumps({"results": str(destination / "results.json"), "summary": str(destination / "summary.md")}))


if __name__ == "__main__":
    main()
