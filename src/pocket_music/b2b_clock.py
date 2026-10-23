# SPDX-License-Identifier: AGPL-3.0-only
"""The b2b clocks. Every b2b decision reads time through them; only the host also reads real time,
to bound how long it waits for an act to finish.

Leases and deadlines use a deadline clock that keeps counting while the machine sleeps, so a
grant can't survive a suspend. A second uptime clock pauses during sleep; the broker compares
the two between its entries to detect a suspend (``suspend_gap_ns``). ``time.monotonic()`` is
not used: on macOS it pauses during sleep. Wall time is display metadata and never decides
anything.
"""
from __future__ import annotations

import sys
import threading
import time
from datetime import UTC, datetime
from typing import Protocol

from .errors import PocketError

# (deadline clock, uptime clock) by platform. The deadline clock counts suspended time.
_CLOCK_NAMES = {"darwin": ("CLOCK_MONOTONIC", "CLOCK_UPTIME_RAW"),
                "linux": ("CLOCK_BOOTTIME", "CLOCK_MONOTONIC")}
_UNSUPPORTED_HINT = ("Run the b2b host on macOS or Linux; capabilities_list reports the b2b providers as "
                     "unsupported on this platform.")


class Clock(Protocol):
    def now_ns(self) -> int: ...      # deadline domain: counts suspended time, never decreases
    def uptime_ns(self) -> int: ...   # excludes suspended time; used only to detect a suspend
    def wall_utc(self) -> str: ...    # display only (ISO 8601); never used for a decision


def platform_clock_names(platform: str | None = None) -> tuple[str, str]:
    """Return the (deadline, uptime) clock names for a platform, or refuse an unsupported one."""
    platform = sys.platform if platform is None else platform
    key = "linux" if platform.startswith("linux") else platform
    if key not in _CLOCK_NAMES:
        raise PocketError("b2b needs macOS or Linux", code="unsupported_profile", hint=_UNSUPPORTED_HINT)
    return _CLOCK_NAMES[key]


def suspend_gap_ns(previous: tuple[int, int], current: tuple[int, int]) -> int:
    """Deadline time that passed without uptime between two (now_ns, uptime_ns) readings.

    A positive gap is time the machine spent suspended, plus reading skew between the two
    clocks, which the broker's threshold absorbs. The gap is never negative.
    """
    return max(0, (current[0] - previous[0]) - (current[1] - previous[1]))


class SystemClock:
    """The platform clock pair, each guarded so a reading never runs backwards."""

    def __init__(self, platform: str | None = None):
        deadline_name, uptime_name = platform_clock_names(platform)
        ids = tuple(getattr(time, name, None) for name in (deadline_name, uptime_name))
        if None in ids:
            raise PocketError(f"This Python has no {deadline_name}/{uptime_name} clock pair",
                              code="unsupported_profile", hint=_UNSUPPORTED_HINT)
        self._ids = ids
        self.names = {"deadline": deadline_name, "uptime": uptime_name}
        self._lock = threading.Lock()
        self._last = [0, 0]

    def _read(self, index: int) -> int:
        raw = time.clock_gettime_ns(self._ids[index])
        with self._lock:
            self._last[index] = max(self._last[index], raw)
            return self._last[index]

    def now_ns(self) -> int:
        return self._read(0)

    def uptime_ns(self) -> int:
        return self._read(1)

    def wall_utc(self) -> str:
        return datetime.now(UTC).isoformat()


class TestClock:
    """A deterministic clock for tests. Nothing advances unless a test advances it."""

    __test__ = False  # not a pytest test class
    names = {"deadline": "test", "uptime": "test"}  # noqa: RUF012 - read-only class constant

    def __init__(self, start_ns: int = 1_000_000_000_000):
        if type(start_ns) is not int or start_ns < 0:
            raise ValueError("A test clock starts at a nonnegative integer time")
        self._now = start_ns
        self._uptime = start_ns
        self._lock = threading.Lock()

    def advance(self, ns: int) -> None:
        if type(ns) is not int or ns < 0:
            raise ValueError("A test clock only moves forward")
        with self._lock:
            self._now += ns
            self._uptime += ns

    def advance_ms(self, ms: int) -> None:
        if type(ms) is not int:
            raise ValueError("Advance a test clock by whole milliseconds")
        self.advance(ms * 1_000_000)

    def suspend(self, ns: int) -> None:
        """Model a machine sleep: deadline time moves on, uptime doesn't."""
        if type(ns) is not int or ns < 0:
            raise ValueError("A suspend lasts a nonnegative time")
        with self._lock:
            self._now += ns

    def set_raw(self, ns: int) -> None:
        """Offer a raw reading; like the system clock's guard, an earlier reading is ignored."""
        with self._lock:
            self._now = max(self._now, ns)
            self._uptime = max(self._uptime, ns)

    def now_ns(self) -> int:
        with self._lock:
            return self._now

    def uptime_ns(self) -> int:
        with self._lock:
            return self._uptime

    def wall_utc(self) -> str:
        return "2026-01-01T00:00:00+00:00"
