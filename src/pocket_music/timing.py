# SPDX-License-Identifier: AGPL-3.0-only
"""Coordinate math, not an Ableton audio renderer.

Float-event tempo values interpolate linearly in *beats*. Their elapsed seconds
therefore require integration of 60 / BPM, not averaging endpoint tempos.
"""
from __future__ import annotations

from bisect import bisect_right
import math

from .errors import PocketError


def finite(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise PocketError(f"{label} must be numeric, not a boolean")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise PocketError(f"{label} must be numeric") from error
    if not math.isfinite(number):
        raise PocketError(f"{label} must be finite")
    return number


class TempoMap:
    """A strictly ordered positive, piecewise-linear beat/BPM sequence.

    Outside its anchors, the nearest BPM is held. Equal-beat events are rejected
    rather than silently choosing one. Tiny but nonzero beat differences remain.
    """

    def __init__(self, points: list[dict]):
        if not points:
            raise PocketError("Tempo map has no points")
        self.points = tuple((finite(p['beat'], 'tempo beat'),
                             finite(p['bpm'], 'tempo BPM')) for p in points)
        if any(bpm <= 0 for _, bpm in self.points):
            raise PocketError("Tempo BPM must be positive")
        if any(a[0] >= b[0] for a, b in zip(self.points, self.points[1:])):
            raise PocketError("Tempo beats must be strictly increasing")
        self.beats = tuple(p[0] for p in self.points)

    def bpm_at(self, beat: float) -> float:
        beat = finite(beat, 'arrangement beat')
        index = bisect_right(self.beats, beat) - 1
        if index < 0:
            return self.points[0][1]
        if index == len(self.points) - 1:
            return self.points[-1][1]
        left, right = self.points[index:index + 2]
        return left[1] + (right[1] - left[1]) * (beat - left[0]) / (right[0] - left[0])

    def between(self, start: float, end: float) -> float:
        start, end = finite(start, 'start beat'), finite(end, 'end beat')
        if end < start:
            return -self.between(end, start)
        cuts = [start, *(b for b in self.beats if start < b < end), end]
        total = 0.0
        for left, right in zip(cuts, cuts[1:]):
            width = right - left
            if width == 0:
                continue
            initial, final = self.bpm_at(left), self.bpm_at(right)
            change = final - initial
            if abs(change) <= initial * 1e-12:
                total += width * 60 / initial
            else:
                total += 60 * width * math.log1p(change / initial) / change
        return total

    def beat_to_seconds(self, beat: float) -> float:
        return self.between(0.0, beat)

    def seconds_to_beat(self, seconds: float) -> float:
        seconds = finite(seconds, 'arrangement seconds')
        if seconds == 0:
            return 0.0
        bound = abs(seconds) * max(p[1] for p in self.points) / 60 + 1
        low, high = (-bound, 0.0) if seconds < 0 else (0.0, bound)
        for _ in range(80):
            mid = (low + high) / 2
            if self.beat_to_seconds(mid) < seconds:
                low = mid
            else:
                high = mid
        return (low + high) / 2


def warp_coordinate(markers: list[dict], value: float, *, inverse: bool = False) -> float:
    """Piecewise-linear source seconds↔local pulse, endpoint slopes extrapolated.

    Marker Id is deliberately ignored: IDs may be reused or renumbered by Live.
    Both numeric axes must increase strictly; reversed maps are unsupported.
    """
    if len(markers) < 2:
        raise PocketError("At least two warp markers are required")
    pairs = [(finite(m['source_seconds'], 'warp source seconds'),
              finite(m['beat'], 'warp beat')) for m in markers]
    if any(a[0] >= b[0] or a[1] >= b[1] for a, b in zip(pairs, pairs[1:])):
        raise PocketError("Warp marker seconds and beats must increase strictly")
    coordinates = [(b, s) for s, b in pairs] if inverse else pairs
    x = finite(value, 'warp coordinate')
    index = max(0, min(len(coordinates) - 2,
                       bisect_right([p[0] for p in coordinates], x) - 1))
    left, right = coordinates[index:index + 2]
    return left[1] + (x - left[0]) * (right[1] - left[1]) / (right[0] - left[0])
