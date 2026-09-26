# SPDX-License-Identifier: AGPL-3.0-only
"""End-of-region trimming of Basic Pitch activations, recorded exactly.

Basic Pitch 0.4.0 kept floor(resampled_frames * 86 / 22050) retained frames. Upstream
spotify/basic-pitch commit e989e40 (2025-11-04, #179) changed that to
int(resampled_frames / 36164 * 142), because the old rule truncated final notes.
Pocket's byte-pinned v1 projection keeps the old rule, so records made with it still
verify. The v2 projection adopts the fix. These tests pin down what each does at the
end of a region.
"""
import math

import numpy as np
import pytest

from pocket_music import audio_note_projection as v1
from pocket_music import audio_note_projection_v2 as v2
from pocket_music.audio_note_projection import project_note_arrays
from pocket_music.errors import PocketError


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


@pytest.mark.parametrize(('resampled', 'upstream'), [(44100, 173), (220500, 865), (441000, 1731)])
def test_v2_keeps_the_upstream_retained_frame_count(resampled, upstream):
    result = v2.project_note_arrays(windows(resampled, 20, 40), source(resampled), resampled)
    assert result['frame_count'] == upstream == upstream_retained_frames(resampled)
    assert result['decoder'] == 'basic_pitch_0_4_0_false_false_v2'


def test_v2_count_is_exact_and_covered_for_every_supported_length():
    counts = []
    for resampled in range(44100, 441001):
        count = v2.retained_frame_count(resampled)
        # Upstream's float expression never rounds differently from exact arithmetic here.
        assert count == resampled * 142 // 36164
        assert count <= len(range(0, resampled + 3840, 36164)) * 142
        counts.append(count)
    assert (min(counts), max(counts)) == (173, v2.MAX_FRAMES)
    for outside in (44099, 441001, 44100.0, True):
        with pytest.raises(PocketError):
            v2.retained_frame_count(outside)


def test_v2_keeps_the_note_v1_drops():
    raw = windows(441000, 1712, 1736)
    assert v1.project_note_arrays(raw, source(441000), 441000)['events'] == []
    kept = v2.project_note_arrays(raw, source(441000), 441000)
    # The decoder ends a note on the last retained frame, as upstream does.
    assert [(row['start_model_frame'], row['end_model_frame']) for row in kept['ledger']] == [(1712, 1730)]
    assert kept['excluded'] == []
    event, = kept['events']
    assert source(441000)['start_frame'] <= event['start_frame'] < event['end_frame_exclusive'] <= source(441000)['end_frame_exclusive']


def test_v2_last_frame_lands_within_two_hops_of_the_region_end():
    # Upstream #179's own test: the last frame's time matches the audio's length within two hops.
    for resampled in [*range(44100, 441001, 997), 441000]:
        assert abs(v2.vendor_frame_times(v2.retained_frame_count(resampled))[-1] - resampled / 22050) <= 2 / 86
    assert 20 - v1.vendor_frame_times(1720)[-1] > 2 / 86


def test_v2_decodes_and_times_like_v1_where_both_apply():
    rng = np.random.default_rng(179)
    frames = rng.random((1720, 88), dtype=np.float32)
    onsets = (rng.random((1720, 88), dtype=np.float32) * (rng.random((1720, 88)) < .01)).astype(np.float32)
    decoded = v2.decode_note_frames(frames, onsets)
    assert decoded and decoded == v1.decode_note_frames(frames, onsets)
    assert np.array_equal(v2.vendor_frame_times(1720), v1.vendor_frame_times(1720))
    control = windows(441000, 1692, 1716)
    first, second = (module.project_note_arrays(control, source(441000), 441000) for module in (v1, v2))
    assert first['ledger'] == second['ledger'] and first['events'] == second['events']
    assert (first['decoder'], first['frame_count'], second['frame_count']) == ('basic_pitch_0_4_0_false_false_v1', 1720, 1731)
