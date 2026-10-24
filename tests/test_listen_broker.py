# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import importlib.util
import random
import threading
import time
from collections import Counter, deque

import pytest
from test_listen_runtime import ALTERNATING_TONES_AND_GAPS, MS, RUNTIME, build_core, script, stereo_tone

from pocket_music.artifact_store import canonical_bytes
from pocket_music.error_contracts import JSON_SCHEMA_V3, error_envelope
from pocket_music.errors import PocketError, valid_hint
from pocket_music.listen import events_receipt_status, resolve_routes
from pocket_music.listen_runtime import (
    EVENT_MAX_BYTES,
    POLL_SCAN_LIMIT,
    EventBroker,
    EventFactory,
    FakeClock,
    RuntimeContext,
    SystemClock,
    WindowClocks,
    metered_routes,
)

ROUTES = metered_routes(resolve_routes([{"route_id": "master", "role": "master", "channels": [0, 1]},
                                        {"route_id": "aux", "role": "unlabeled", "channels": [2]}], 4)[0])
FACTORY = EventFactory(RuntimeContext("session-test", RUNTIME, "synthetic_script", 48000, ROUTES, 4800, -0.1,
                                      -60.0, 2000, 5, 500, "dev-0123456789abcdef", "synthetic_clock_ns"))
_ROUTE = {r.route_id: r for r in ROUTES}
_PLACEHOLDER = {"sequence": 2 ** 53, "published_monotonic_ns": 2 ** 63 - 1}
POLL_HANG_GUARD_S = 30
POLL_WAIT_PAST_HANG_GUARD_MS = 60_000


def meter(route="master", *, received_ns=0, pad=0):
    r = _ROUTE[route]
    n = len(r.channels)
    evidence = {"kind": "measurement", "type": "signal_level", "profile": "sample-peak-rms/v1",
                "channels": list(r.channels), "windows": 1, "window_frames": 4800, "frames_measured": 4800,
                "complete": True, "truncated_by": None, "sample_peak_dbfs": [-6.0] * n,
                "rms_dbfs": [-9.0] * n, "digital_zero": [False] * n, "nonfinite_count": [0] * n,
                "clipped_count": [0] * n, "clip_threshold_dbfs": -0.1, "finite": True,
                "true_peak": "not_measured", "loudness": "not_measured",
                **({"test_padding": "x" * pad} if pad else {})}
    return FACTORY.measurement(r, ordinal=1, interval=(0, 4800), clocks=WindowClocks(0, None, 0, 0),
                               received_ns=received_ns, evidence=evidence)


def health(kind="capture_stale", *, received_ns=0, routes=None, pad=0, detail=None):
    detail = {**(detail or {}), **({"test_padding": "x" * pad} if pad else {})}
    return FACTORY.health(kind, received_ns=received_ns, ordinal=1, affected_routes=routes, detail=detail)


def broker(clock=None, *, normal_events=8192, normal_bytes=4194304, health_events=1024, health_bytes=524288,
           record=None):
    return EventBroker(RUNTIME, clock or FakeClock(), normal_events=normal_events, normal_bytes=normal_bytes,
                       health_events=health_events, health_bytes=health_bytes,
                       on_publish=None if record is None else record)


def cursor(sequence, epoch=RUNTIME):
    return {"runtime_epoch": epoch, "sequence": sequence}


def seqs(page):
    return [e["sequence"] for e in page["events"]]


def near_cap_health():
    low, high = 0, EVENT_MAX_BYTES
    while low < high:
        middle = (low + high + 1) // 2
        draft = health(pad=middle)
        fits = "test_padding" in draft["evidence"]["detail"] and len(
            canonical_bytes({**draft, **_PLACEHOLDER})) <= EVENT_MAX_BYTES
        low, high = (middle, high) if fits else (low, middle - 1)
    return health(pad=low)


def test_sequences_are_contiguous_and_never_reused():
    clock = FakeClock()
    b = broker(clock, normal_events=4, normal_bytes=65536, health_events=2, health_bytes=32768)
    published = []
    for i in range(50):
        clock.advance(MS)
        published.append(b.publish(meter() if i % 3 else health()))
    assert published == list(range(1, 51)) and b.head == 50
    with pytest.raises(PocketError):
        b.publish({**meter(), "received_monotonic_ns": float("nan")})
    oversized = health()
    with pytest.raises(ValueError, match="at most"):
        b.publish({**oversized,
                   "evidence": {**oversized["evidence"], "detail": {"pad": "x" * EVENT_MAX_BYTES}}})
    with pytest.raises(ValueError, match="normal or health"):
        b.publish({**meter(), "delivery_class": "priority"})
    assert b.publish(health()) == 51
    page = b.poll(cursor(48))
    assert seqs(page) == [49, 50, 51]
    assert page["events"][-1]["published_monotonic_ns"] == clock.monotonic_ns()
    assert all(e["runtime_epoch"] == RUNTIME and e["schema"] == "pocket.listen-event/v1"
               for e in page["events"])


def test_poll_bounds_events_bytes_wait_and_cursor_are_enforced_with_hints():
    r = build_core(script(stereo_tone(9600)), window_frames=480, coalesce=1)
    r.drive()
    head = r.core.observe()["cursor"]
    bounds = [{"max_events": 0}, {"max_events": 257}, {"max_events": True}, {"max_bytes": 16383},
              {"max_bytes": 262145}, {"wait_ms": -1}, {"wait_ms": 1001}, {"max_evidence_age_ms": 0},
              {"max_evidence_age_ms": 3600001}, {"on_expired": "skip"}]
    bad_cursors = [None, {"runtime_epoch": RUNTIME}, {"runtime_epoch": RUNTIME, "sequence": -1},
                   {"runtime_epoch": RUNTIME, "sequence": True}, {"runtime_epoch": 7, "sequence": 0},
                   {"runtime_epoch": RUNTIME, "sequence": 0, "stream_epoch": "x"}]
    raised = []
    for case in bounds:
        with pytest.raises(PocketError) as error:
            r.core.poll(head, **case)
        raised.append(error.value)
    for bad in bad_cursors:
        with pytest.raises(PocketError, match="cursor") as error:
            r.core.poll(bad)
        raised.append(error.value)
    validator = None
    if importlib.util.find_spec("jsonschema") is not None:
        from jsonschema import Draft202012Validator
        validator = Draft202012Validator(JSON_SCHEMA_V3)
    for error in raised:
        assert error.code == "invalid_request" and valid_hint(error.hint)
        if validator is not None:
            validator.validate(error_envelope(error, version="v3"))
    page = r.core.poll(cursor(0), max_events=3)
    assert seqs(page) == [1, 2, 3] and page["truncated_by"] == "max_events" and page["more_available"]
    assert page["next_after"] == cursor(3)
    page = r.core.poll(cursor(0), max_events=256, max_bytes=16384)
    assert page["truncated_by"] == "max_bytes"
    assert sum(len(canonical_bytes(e)) for e in page["events"]) <= 16384
    assert page["next_after"]["sequence"] == page["events"][-1]["sequence"]


def test_first_event_always_fits_so_a_page_always_advances():
    draft = near_cap_health()
    b = broker()
    for _ in range(9):
        b.publish(draft)
    sizes = [len(canonical_bytes(e)) for e in b.poll(cursor(0), max_bytes=262144)["events"]]
    assert min(sizes) > EVENT_MAX_BYTES - 64 and max(sizes) <= EVENT_MAX_BYTES
    position, pages = 0, 0
    while position < b.head:
        page = b.poll(cursor(position), max_events=256, max_bytes=16384)
        assert 1 <= len(page["events"]) <= 2 and page["next_after"]["sequence"] > position
        position, pages = page["next_after"]["sequence"], pages + 1
    assert pages >= 5


def test_same_cursor_redelivers_byte_identical_events():
    b = broker()
    for i in range(20):
        b.publish(meter("aux", pad=i) if i % 2 else health(pad=i))
    first = b.poll(cursor(5), max_events=10)
    second = b.poll(cursor(5), max_events=10)
    assert [canonical_bytes(e) for e in first["events"]] == [canonical_bytes(e) for e in second["events"]]
    assert seqs(first) == list(range(6, 16)) and first["next_after"] == second["next_after"] == cursor(15)
    first["events"][0]["evidence"]["type"] = "edited"
    assert b.poll(cursor(5), max_events=1)["events"][0]["evidence"]["type"] != "edited"


def test_normal_eviction_refuses_with_cursor_expired_by_default():
    b = broker(normal_events=4, normal_bytes=65536)
    b.publish(health())
    for _ in range(10):
        b.publish(meter())
    page = b.poll(cursor(1))
    assert page["events_status"] == "cursor_expired" and "events" not in page and "next_after" not in page
    assert page["expired"] == {"delivery_class": "normal", "evicted_through": 7}
    assert page["earliest_retained"] == {"health": 1, "normal": 8} and page["head_sequence"] == 11
    assert page["resume"] == {"call": "listen_observe", "use": "cursor",
                              "or": {"on_expired": "resume_from_earliest"}}
    assert events_receipt_status(page) == "conflict"
    page = b.poll(cursor(7))
    assert seqs(page) == [8, 9, 10, 11] and page["missing"] == [] and events_receipt_status(page) == "ok"


def test_resume_from_earliest_lists_the_exact_missing_range_with_status_partial_and_keeps_health():
    b = broker(normal_events=4, normal_bytes=65536)
    for kind in ["m", "h", "m", "m", "h"] + ["m"] * 6:
        b.publish(meter() if kind == "m" else health())
    page = b.poll(cursor(0), on_expired="resume_from_earliest")
    assert page["missing"] == [{"from_sequence": 1, "through_sequence": 7, "delivery_class": "normal",
                                "cause": "retention_evicted", "health_in_range": "delivered"}]
    assert seqs(page) == [2, 5, 8, 9, 10, 11] and events_receipt_status(page) == "partial"
    position, delivered, missing = 0, [], []
    while position < b.head:
        page = b.poll(cursor(position), max_events=1, on_expired="resume_from_earliest")
        delivered += seqs(page)
        missing += [s for m in page["missing"] for s in range(m["from_sequence"], m["through_sequence"] + 1)]
        assert all(position < s <= page["next_after"]["sequence"] for s in seqs(page))
        position = page["next_after"]["sequence"]
    assert sorted(delivered + [s for s in missing if s not in delivered]) == list(range(1, 12))
    assert set(missing) - set(delivered) == {1, 3, 4, 6, 7}


@pytest.mark.parametrize("mode", ["refuse", "resume_from_earliest"])
def test_a_health_only_poll_never_expires_on_meters_it_never_subscribed_to(mode):
    b = broker(normal_events=256, normal_bytes=65536, health_events=64)
    b.publish(health("runtime_started"))
    b.publish(health("frames_missing"))
    for _ in range(400):
        b.publish(meter())
    evicted = b.counters()["normal"]["evicted_through"]
    assert evicted > 2
    page = b.poll(cursor(1), kinds=("health",), on_expired=mode)
    assert page["events_status"] == "page" and seqs(page) == [2] and page["missing"] == []
    assert page["filtered"] == {"kind": 400, "routes": 0, "stale_measurements": 0}
    assert events_receipt_status(page) == "ok" and page["next_after"]["sequence"] == b.head
    short = b.poll(cursor(0), kinds=("health",), max_events=1, on_expired=mode)
    assert seqs(short) == [1] and short["next_after"]["sequence"] == 1 and short["filtered"]["kind"] == 0
    assert b.poll(cursor(1), on_expired=mode)["events_status"] == ("cursor_expired" if mode == "refuse"
                                                                   else "page")


def test_health_eviction_requires_a_snapshot_regardless_of_on_expired():
    b = broker(health_events=2, health_bytes=32768)
    for _ in range(5):
        b.publish(health())
    b.publish(meter())
    for mode in ("refuse", "resume_from_earliest"):
        page = b.poll(cursor(2), on_expired=mode)
        assert page["events_status"] == "snapshot_required" and "events" not in page
        assert page["health_lost"] == {"through_sequence": 3}
        assert page["resume"] == {"call": "listen_observe", "use": "cursor"}
        assert page["earliest_retained"] == {"health": 4, "normal": 6}
        assert events_receipt_status(page) == "conflict"
    assert seqs(b.poll(cursor(3))) == [4, 5, 6]


def test_meter_flood_cannot_evict_reserved_health():
    b = broker(normal_events=256, normal_bytes=65536, health_events=64, health_bytes=32768)
    b.publish(health("capture_stale"))
    for _ in range(10000):
        b.publish(meter())
    counters = b.counters()
    assert counters["health"]["events"] == 1 and counters["health_lost_through"] == 0
    assert counters["evicted"]["health"] == 0 and counters["evicted"]["normal"] > 9000
    assert counters["normal"]["bytes"] <= 65536
    page = b.poll(cursor(0), on_expired="resume_from_earliest")
    assert seqs(page)[0] == 1 and page["events"][0]["evidence"]["type"] == "capture_stale"
    assert page["missing"][0]["through_sequence"] == counters["normal"]["evicted_through"]


def test_a_full_facts_queue_expires_no_cursor_and_reports_lost_measurement_accounting():
    r = build_core(ALTERNATING_TONES_AND_GAPS, facts_capacity=4)
    early = r.core.observe()["cursor"]
    assert early["sequence"] == 2
    r.drive(measure_every=0)
    head = r.core.broker.head
    assert r.core.broker.counters()["health_lost_through"] == 0 and not r.events("health_facts_dropped")
    for old in range(head):
        page = r.core.poll(cursor(old), max_events=256, max_bytes=262144)
        assert page["events_status"] == "page" and page["missing"] == [], old
        assert page["next_after"]["sequence"] == head and not page["more_available"], old
    assert len(r.events("frames_missing")) == 19
    fresh = r.core.observe()["observation"]
    assert fresh["worker"]["facts_dropped"] == r.core.feed.facts.dropped_total == 17
    assert "facts_dropped" not in fresh["broker"]
    codes = [n["code"] for n in fresh["notices"]]
    assert "measurement_facts_dropped" in codes and "health_facts_dropped" not in codes


def test_age_filter_counts_stale_measurements_and_never_hides_health():
    clock = FakeClock()
    b = broker(clock)
    old = clock.monotonic_ns()
    b.publish(meter(received_ns=old))
    b.publish(health(received_ns=old))
    b.publish(meter("aux", received_ns=old))
    clock.advance(2000 * MS)
    b.publish(meter(received_ns=clock.monotonic_ns()))
    page = b.poll(cursor(0), max_age_ms=1000)
    assert seqs(page) == [2, 4] and page["filtered"] == {"kind": 0, "routes": 0, "stale_measurements": 2}
    assert page["next_after"] == cursor(4) and events_receipt_status(page) == "ok"
    assert page["now_monotonic_ns"] == clock.monotonic_ns()
    clock.advance(60_000 * MS)
    page = b.poll(cursor(0), max_age_ms=1)
    assert seqs(page) == [2] and page["filtered"]["stale_measurements"] == 3


def test_kinds_must_include_health():
    r = build_core(script(stereo_tone(9600)), coalesce=1)
    r.drive()
    for kinds in (["measurement"], [], ["health", "health"], ["health", "calibration"], "health"):
        with pytest.raises(PocketError) as error:
            r.core.poll(cursor(0), kinds=kinds)
        assert error.value.code == "invalid_request" and "can't be filtered out" in error.value.hint
    page = r.core.poll(cursor(0), kinds=["health"], max_events=256)
    assert page["events"] and all(e["delivery_class"] == "health" for e in page["events"])
    assert page["filtered"]["kind"] == len(r.events("signal_level")) > 0
    assert page["next_after"]["sequence"] == r.core.broker.head


def test_route_filter_counts_skipped_meters_and_keeps_route_specific_health():
    b = broker()
    b.publish(meter("master"))
    b.publish(meter("aux"))
    b.publish(health("nonfinite_input", routes=("aux",)))
    b.publish(meter("aux"))
    b.publish(health())
    page = b.poll(cursor(0), routes=("master",))
    assert seqs(page) == [1, 3, 5] and page["filtered"] == {"kind": 0, "routes": 2, "stale_measurements": 0}
    assert page["events"][1]["evidence"]["affected_routes"] == ["aux"]
    flood = broker(normal_bytes=33554432)
    for _ in range(POLL_SCAN_LIMIT + 100):
        flood.publish(meter("aux"))
    page = flood.poll(cursor(0), routes=("master",))
    assert page["events"] == [] and page["truncated_by"] == "scan_limit" and page["more_available"]
    assert page["filtered"]["routes"] == POLL_SCAN_LIMIT and page["next_after"] == cursor(POLL_SCAN_LIMIT)
    page = flood.poll(page["next_after"], routes=("master",))
    assert page["filtered"]["routes"] == 100 and not page["truncated"] and not page["more_available"]
    r = build_core(script(stereo_tone(4800)))
    with pytest.raises(PocketError) as error:
        r.core.poll(cursor(0), routes=["drums"])
    assert error.value.hint == "listen_observe lists route_ids."


def test_cursor_from_another_runtime_epoch_is_an_epoch_mismatch():
    b = broker()
    b.publish(health())
    for sequence in (0, 1, 999):
        page = b.poll(cursor(sequence, "rt-ffffffffffffffff"), wait_ms=1000)
        assert page["events_status"] == "runtime_epoch_mismatch" and page["current_runtime_epoch"] == RUNTIME
        assert "events" not in page and events_receipt_status(page) == "conflict" and page["waited_ms"] == 0


def test_cursor_ahead_of_the_runtime_is_refused_not_clamped():
    b = broker()
    b.publish(health())
    with pytest.raises(PocketError) as error:
        b.poll(cursor(2))
    assert error.value.code == "invalid_request" and "never clamps" in error.value.hint
    assert seqs(b.poll(cursor(0))) == [1]


def test_long_poll_returns_on_publish_or_within_the_wait_bound():
    b = broker(SystemClock())
    b.publish(health())
    started = time.monotonic()
    page = b.poll(cursor(1), wait_ms=200)
    elapsed = time.monotonic() - started
    assert page["events"] == [] and events_receipt_status(page) == "ok" and not page["more_available"]
    assert elapsed >= 0.19 and page["waited_ms"] >= 190 and elapsed < POLL_HANG_GUARD_S
    timer = threading.Timer(0.03, lambda: b.publish(meter()))
    timer.start()
    started = time.monotonic()
    page = b.poll(cursor(1), wait_ms=POLL_WAIT_PAST_HANG_GUARD_MS)
    elapsed = time.monotonic() - started
    timer.join()
    assert seqs(page) == [2] and elapsed < POLL_HANG_GUARD_S and page["waited_ms"] < POLL_HANG_GUARD_S * 1000
    started = time.monotonic()
    assert seqs(b.poll(cursor(0), wait_ms=POLL_WAIT_PAST_HANG_GUARD_MS)) == [1, 2] and \
        time.monotonic() - started < POLL_HANG_GUARD_S
    closer = threading.Timer(0.03, b.close)
    closer.start()
    started = time.monotonic()
    assert b.poll(cursor(2), wait_ms=POLL_WAIT_PAST_HANG_GUARD_MS)["events"] == [] and \
        time.monotonic() - started < POLL_HANG_GUARD_S
    closer.join()


class _RetentionModel:
    def __init__(self, budgets):
        self.budgets = budgets
        self.retained = {"normal": deque(), "health": deque()}
        self.bytes = {"normal": 0, "health": 0}
        self.evicted_through = {"normal": 0, "health": 0}
        self.lost_through = 0
        self.info = {}
        self.evicted = set()

    def __call__(self, event):
        sequence, delivery = event["sequence"], event["delivery_class"]
        data = canonical_bytes(event)
        self.info[sequence] = (delivery, event["route_id"], event["received_monotonic_ns"], data)
        queue = self.retained[delivery]
        queue.append((sequence, len(data)))
        self.bytes[delivery] += len(data)
        count, limit = self.budgets[delivery]
        while len(queue) > count or self.bytes[delivery] > limit:
            gone, size = queue.popleft()
            self.bytes[delivery] -= size
            self.evicted.add(gone)
            self.evicted_through[delivery] = gone
            if delivery == "health":
                self.lost_through = max(self.lost_through, gone)

    @property
    def head(self):
        return max(self.info, default=0)

    def skip(self, sequence, args, now):
        delivery, route, received, _ = self.info[sequence]
        if delivery == "health":
            return None
        if args["kinds"] == ("health",):
            return "kind"
        if args["routes"] is not None and route not in args["routes"]:
            return "routes"
        if args["max_age_ms"] is not None and now - received > args["max_age_ms"] * MS:
            return "stale_measurements"
        return None


def _check_page(model, page, c, args, seed, round_):
    where = (seed, round_, c, args)
    if c < model.lost_through:
        assert page["events_status"] == "snapshot_required" and "events" not in page, where
        return
    health_only = args["kinds"] == ("health",)
    expired = c < model.evicted_through["normal"] and not health_only
    if expired and args["on_expired"] == "refuse":
        assert page["events_status"] == "cursor_expired" and "events" not in page, where
        return
    assert page["events_status"] == "page", where
    head, after = model.head, page["next_after"]["sequence"]
    assert (c < after <= head) if head > c else after == c, where
    delivered = seqs(page)
    assert delivered == sorted(set(delivered)) and all(c < s <= after for s in delivered), where
    for event in page["events"]:
        assert canonical_bytes(event) == model.info[event["sequence"]][3], where
    predicted = Counter()
    for s in range(c + 1, after + 1):
        if s in model.evicted:
            assert model.info[s][0] == "normal" and (expired or health_only), where
            if health_only:
                predicted["kind"] += 1
                continue
            assert any(m["from_sequence"] <= s <= m["through_sequence"] for m in page["missing"]), where
            continue
        reason = model.skip(s, args, page["now_monotonic_ns"])
        assert (s in delivered) is (reason is None), (where, s, reason)
        if reason:
            predicted[reason] += 1
    assert page["filtered"] == {k: predicted[k] for k in ("kind", "routes", "stale_measurements")}, where
    through = min(model.evicted_through["normal"], after)
    expected = [{"from_sequence": c + 1, "through_sequence": through, "delivery_class": "normal",
                 "cause": "retention_evicted", "health_in_range": "delivered"}]
    assert page["missing"] == (expected if expired and through > c else []), where
    assert len(delivered) <= args["max_events"], where
    assert sum(len(model.info[s][3]) for s in delivered) <= args["max_bytes"], where
    assert page["more_available"] == (head > after), where
    if page["truncated"]:
        following = after + 1
        assert following not in model.evicted, where
        assert model.skip(following, args, page["now_monotonic_ns"]) is None, where
        size = len(model.info[following][3])
        used = sum(len(model.info[s][3]) for s in delivered)
        assert len(delivered) == args["max_events"] or used + size > args["max_bytes"], where
    if events_receipt_status(page) == "ok":
        assert all(health_only and model.info[s][0] == "normal"
                   for s in range(c + 1, after + 1) if s in model.evicted), where
        if not page["events"] and not sum(predicted.values()):
            assert head == c, where


def test_every_sequence_after_a_cursor_is_delivered_filtered_missing_or_expired():
    for seed in range(10):
        rng = random.Random(seed)
        clock = FakeClock()
        budgets = {"normal": (12, 24000), "health": (8, 20000)}
        model = _RetentionModel(budgets)
        b = broker(clock, normal_events=12, normal_bytes=24000, health_events=8, health_bytes=20000,
                   record=model)
        consumer = 0
        for round_ in range(500):
            for _ in range(rng.randint(0, 4)):
                received = clock.monotonic_ns() - rng.randint(0, 3000) * MS
                pad = rng.choice([0, 0, rng.randint(0, 5000)])
                if rng.random() < 0.25:
                    routes = rng.choice([None, ("aux",), ("master",)])
                    b.publish(health(received_ns=received, routes=routes, pad=pad))
                else:
                    b.publish(meter(rng.choice(["master", "aux"]), received_ns=received, pad=pad))
            clock.advance(rng.randint(0, 500) * MS)
            head = model.head
            c = consumer if rng.random() < 0.5 else rng.randint(max(0, head - 40), head)
            args = {"routes": rng.choice([None, ("master",), ("aux",), ("master", "aux")]),
                    "kinds": rng.choice([None, ("health",), ("measurement", "health")]),
                    "max_events": rng.randint(1, 8), "max_bytes": rng.choice([8192, 12000, 16384, 40000]),
                    "max_age_ms": rng.choice([None, 100, 800, 2000]),
                    "on_expired": rng.choice(["refuse", "resume_from_earliest"])}
            page = b.poll(cursor(c), routes=args["routes"], kinds=args["kinds"],
                          max_events=args["max_events"], max_bytes=args["max_bytes"],
                          max_age_ms=args["max_age_ms"], on_expired=args["on_expired"])
            _check_page(model, page, c, args, seed, round_)
            if c == consumer:
                consumer = page["next_after"]["sequence"] if page["events_status"] == "page" else head
        counters = b.counters()
        assert counters["health_lost_through"] == model.lost_through
        assert counters["normal"]["evicted_through"] == model.evicted_through["normal"]
        assert counters["evicted"]["normal"] > 0 and counters["evicted"]["health"] > 0


def test_one_minute_blocked_consumer_gets_explicit_gaps_while_capture_stays_running():
    r = build_core(script(stereo_tone(61 * 48000), block=4800), window_frames=480, coalesce=1,
                   normal_events=256, normal_bytes=65536, health_events=64, health_bytes=32768)
    start = r.core.observe()["cursor"]
    r.drive(reads=2, timeout_ns=100 * MS)
    first = r.core.poll(start, max_events=256, max_bytes=262144)
    assert first["events_status"] == "page" and first["missing"] == [] and not first["more_available"]
    blocked = first["next_after"]
    r.drive(reads=600, timeout_ns=100 * MS)
    observation = r.core.observe()["observation"]
    assert observation["capture"]["state"] == "running"
    assert observation["capture"]["frames_received"] == 602 * 4800
    assert not r.events("capture_failed") and not r.events("capture_stale")
    assert observation["broker"]["normal"]["evicted_through"] > blocked["sequence"]
    assert observation["broker"]["health_lost_through"] == 0
    refused = r.core.poll(blocked)
    assert refused["events_status"] == "cursor_expired" and events_receipt_status(refused) == "conflict"
    assert refused["health_summary"]["capture_state"] == "running"
    resumed = r.core.poll(blocked, on_expired="resume_from_earliest", max_events=256, max_bytes=262144)
    assert events_receipt_status(resumed) == "partial"
    (missing,) = resumed["missing"]
    assert missing["from_sequence"] == blocked["sequence"] + 1
    assert missing["through_sequence"] == observation["broker"]["normal"]["evicted_through"]
    assert resumed["events"][0]["sequence"] == missing["through_sequence"] + 1
    assert len(r.events("signal_level")) > 256 * 10


def test_ok_page_without_events_only_when_nothing_was_published_or_lost():
    b = broker(normal_events=4, normal_bytes=65536)
    page = b.poll(cursor(0))
    assert page["events"] == [] and page["missing"] == [] and events_receipt_status(page) == "ok"
    assert page["next_after"] == cursor(0) and not page["more_available"] and not page["truncated"]
    for _ in range(6):
        b.publish(meter("master"))
    assert b.poll(cursor(0))["events_status"] == "cursor_expired"
    resumed = b.poll(cursor(0), on_expired="resume_from_earliest")
    assert events_receipt_status(resumed) == "partial" and seqs(resumed) == [3, 4, 5, 6]
    filtered = b.poll(cursor(2), routes=("aux",))
    assert filtered["events"] == [] and filtered["filtered"]["routes"] == 4
    assert filtered["next_after"] == cursor(6)
    assert events_receipt_status(filtered) == "ok"
    caught_up = b.poll(cursor(6))
    assert caught_up["events"] == []
    assert caught_up["filtered"] == {"kind": 0, "routes": 0, "stale_measurements": 0}
    assert events_receipt_status(caught_up) == "ok" and caught_up["head_sequence"] == 6
