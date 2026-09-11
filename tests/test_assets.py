import hashlib

import numpy as np
import pytest
import soundfile as sf

from pocket_music.assets import identify_audio, sha256_file
from pocket_music.errors import PocketError


def test_exact_recordings_do_not_collapse_by_title(tmp_path):
    a, b = tmp_path / "clean" / "song.wav", tmp_path / "explicit" / "song.wav"
    a.parent.mkdir()
    b.parent.mkdir()
    sf.write(a, np.zeros((100, 2)), 48000, subtype="FLOAT")
    sf.write(b, np.ones((100, 2)) * 0.1, 48000, subtype="FLOAT")
    left, right = identify_audio(a), identify_audio(b)
    assert left["filename"] == right["filename"]
    assert left["asset_id"] != right["asset_id"]
    assert left["frames"] == 100 and left["channels"] == 2
    assert left["sha256"] == hashlib.sha256(a.read_bytes()).hexdigest()


def test_identity_survives_file_rename(tmp_path):
    path = tmp_path / "a.wav"
    sf.write(path, np.zeros(100), 8000)
    expected = identify_audio(path)["asset_id"]
    renamed = path.rename(tmp_path / "b.wav")
    assert identify_audio(renamed)["asset_id"] == expected


def test_missing_and_non_audio_fail_explicitly(tmp_path):
    with pytest.raises(PocketError):
        sha256_file(tmp_path / "missing")
    invalid = tmp_path / "broken.wav"
    invalid.write_text("not audio")
    with pytest.raises(PocketError):
        identify_audio(invalid)
