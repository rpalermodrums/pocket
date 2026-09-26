# SPDX-License-Identifier: AGPL-3.0-only
"""End-of-region trimming of Basic Pitch activations, recorded exactly.

Pocket's byte-pinned note projection keeps floor(resampled_frames * 86 / 22050)
retained frames, the basic-pitch 0.4.0 rule it was derived from. Upstream
spotify/basic-pitch commit e989e40 (2025-11-04, #179) changed that to
int(resampled_frames / 36164 * 142), because the old rule truncated final notes.
These tests pin down what the current profile does at the end of a region, so the
decision to keep that rule deliberately or adopt the fix under a new identity rests
on evidence. They don't endorse either rule.
"""
import math

import numpy as np
import pytest

from pocket_music.audio_note_projection import project_note_arrays


def upstream_retained_frames(resampled_frames):
    """spotify/basic-pitch after #179: expected windows times retained frames per window."""
    return int(resampled_frames / 36164 * (2 * 86 - 30))


def windows(resampled, start, end):
    count = len(range(0, resampled + 3840, 36164))
    result = {name: np.zeros((count, 172, bins), dtype=np.float32)
              for name, bins in [('note', 88), ('onset', 88), ('contour', 264)]}
    for i in range(start, end): result['note'][i // 142, 15 + i % 142, 48] = .75
    result['onset'][start // 142, 15 + start % 142, 48] = .5
    return result


def source(frames, start=701, rate=22050):
    return {'start_frame': start, 'end_frame_exclusive': start + frames, 'sample_rate': rate}


@pytest.mark.parametrize(('resampled', 'pocket', 'upstream'), [
    (44100, 172, 173), (220500, 860, 865), (441000, 1720, 1731),
])
def test_retained_frame_count_uses_the_pre_179_rule(resampled, pocket, upstream):
    result = project_note_arrays(windows(resampled, 20, 40), source(resampled), resampled)
    assert result['frame_count'] == pocket == math.floor(resampled * (86 / 22050))
    assert upstream_retained_frames(resampled) == upstream
    # The raw windows hold more retained frames than either rule keeps.
    assert len(result['window_starts']) * 142 > upstream


def test_a_note_ending_after_the_retained_frames_is_dropped():
    # Onset 8 frames before Pocket's last retained frame, sounding 24 frames in total.
    raw = windows(441000, 1712, 1736)
    retained = project_note_arrays(raw, source(441000), 441000)
    assert retained['frame_count'] == 1720
    # The activations beyond frame 1719 exist in the raw windows the model produced.
    assert raw['note'][1735 // 142, 15 + 1735 % 142, 48] == np.float32(.75)
    # Trimmed to 1720 frames, only 7 frames of the note remain, under the 12-frame
    # minimum, so the note is discarded rather than shortened.
    assert retained['events'] == [] and retained['ledger'] == [] and retained['excluded'] == []
    # The pre-#179 rule stops 11 frames earlier than the upstream fix would.
    assert upstream_retained_frames(441000) - retained['frame_count'] == 11


def test_the_same_note_twenty_frames_earlier_is_kept():
    control = project_note_arrays(windows(441000, 1692, 1716), source(441000), 441000)
    assert [(row['start_model_frame'], row['end_model_frame']) for row in control['ledger']] == [(1692, 1716)]
    assert len(control['events']) == 1
