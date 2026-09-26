# SPDX-License-Identifier: MIT
"""Generate authored signals and a comparison recipe. No downloaded recordings."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import soundfile as sf


def generate(destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=False)
    rate, seconds = 24000, 20
    signal = np.zeros(rate * seconds, dtype=np.float64)
    tick_length = int(rate * 0.03)
    time = np.arange(tick_length) / rate
    for beat in range(seconds * 2):
        frequency = 140 if beat % 4 == 0 else 700
        tick = np.cos(2 * np.pi * frequency * time) * np.exp(-time * 150)
        start = round((0.25 + beat * 0.5) * rate)
        signal[start:start + tick_length] += (0.5 if beat % 4 == 0 else 0.3) * tick
    source = destination / "pulse.wav"
    sf.write(source, np.column_stack([signal, signal]), rate, subtype="FLOAT")
    spec = {
        "title": "Synthetic pulse-position experiment",
        "start_frame": rate,
        "frames": 8 * rate,
        "variants": [
            {"label": "A — original position", "source_path": str(source)},
            {"label": "B — half-beat later source window", "source_path": str(source),
             "start_frame": rate + rate // 4, "frames": 8 * rate},
        ],
    }
    (destination / "comparison.json").write_text(json.dumps(spec, indent=2) + "\n")
    return {"directory": str(destination), "audio": str(source), "authored_bpm": 120,
            "authored_first_pulse_seconds": 0.25, "note": "Generated timing fixture, not musical evaluation"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(generate(args.destination.expanduser().resolve()), indent=2))
