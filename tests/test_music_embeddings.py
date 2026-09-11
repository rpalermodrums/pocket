import copy
import hashlib
import json

import numpy as np
import pytest
import soundfile as sf

from pocket_music import music_embeddings as embeddings
from pocket_music.assets import identify_audio
from pocket_music.errors import PocketError


def receipt(digest="a" * 64, axis=0):
    vector = np.zeros(512)
    vector[axis] = 1
    return {"schema": "pocket.music-embedding/v1", "model_id": embeddings.MODEL_ID,
            "model_revision": embeddings.MODEL_REVISION, "checkpoint_sha256": embeddings.WEIGHT_SHA256,
            "dimension": 512, "modality": "audio", "vector": vector.tolist(),
            "asset": {"sha256": digest}, "source_region": {"source_origin": "independently_acquired",
            "source_start_frame": 7, "source_frames": 1000}}


def test_exact_source_frames_resampling_and_padding(tmp_path):
    sr = 44117
    t = np.arange(sr * 2) / sr
    samples = np.column_stack([.1 * np.sin(t * 2 * np.pi * 317), .07 * np.sin(t * 2 * np.pi * 517)])
    path = tmp_path / "generated.wav"
    sf.write(path, samples, sr, subtype="FLOAT")
    identity = identify_audio(path)
    asset, model, recipe = embeddings._read_audio_region(path, 333, 44001, identity["sha256"], "user_recording")
    assert asset["sha256"] == identity["sha256"]
    assert recipe["source_start_frame"] == 333
    assert recipe["source_frames"] == 44001
    assert recipe["source_end_frame_exclusive"] == 44334
    assert len(model) == 480000
    raw, _ = sf.read(path, start=333, frames=44001, dtype="float32", always_2d=True)
    assert recipe["source_region_pcm_sha256"] == hashlib.sha256(raw.astype('<f4').tobytes()).hexdigest()
    assert recipe["zero_padding_frames"] > 0
    assert np.count_nonzero(model[recipe["resampled_frames"]:]) == 0


@pytest.mark.parametrize("source_origin", ["spotify_api", "spotify_preview", "spotify_ui", None])
def test_spotify_and_unknown_origin_rejected_before_read(source_origin):
    with pytest.raises(PocketError, match="eligible"):
        embeddings._read_audio_region("does-not-exist", 0, 1000, None, source_origin)


def test_silence_nonfinite_bounds_and_wrong_identity(tmp_path):
    path = tmp_path / "silence.wav"
    sf.write(path, np.zeros(48000), 48000, subtype="FLOAT")
    with pytest.raises(PocketError, match="abstaining"):
        embeddings._read_audio_region(path, 0, 48000, None, "user_recording")
    with pytest.raises(PocketError, match="bounds"):
        embeddings._read_audio_region(path, 1, 48000, None, "user_recording")
    with pytest.raises(PocketError, match="identity"):
        embeddings._read_audio_region(path, 0, 1000, "f" * 64, "user_recording")
    with pytest.raises(PocketError, match="integer"):
        embeddings._read_audio_region(path, True, 1000, None, "user_recording")
    sf.write(path, np.full(48000, float('nan')), 48000, subtype="FLOAT")
    with pytest.raises(PocketError, match="nonfinite"):
        embeddings._read_audio_region(path, 0, 48000, None, "user_recording")


def test_explicit_ten_second_max_no_hidden_random_crop(tmp_path):
    path = tmp_path / "long.wav"
    sf.write(path, np.ones(480001) * .1, 48000, subtype="FLOAT")
    with pytest.raises(PocketError, match="random crops"):
        embeddings._read_audio_region(path, 0, 480001, None, "independently_acquired")


def test_optional_model_preflight_missing_is_explicit(tmp_path):
    report = embeddings.model_preflight(tmp_path)
    assert not report["files_ready"]
    assert report["download_performed"] is False
    with pytest.raises(PocketError, match="checkpoint"):
        embeddings.LocalClapAdapter(tmp_path)


def test_preflight_rejects_same_length_config_tamper(tmp_path, monkeypatch):
    monkeypatch.setattr(embeddings, "MODEL_FILES", {"config.json": 2})
    monkeypatch.setattr(embeddings, "MODEL_FILE_SHA256", {"config.json": hashlib.sha256(b'{}').hexdigest()})
    (tmp_path / "config.json").write_bytes(b'[]')
    assert embeddings.model_preflight(tmp_path)["mismatched_files"] == ["config.json:sha256"]


def test_sealed_index_identity_and_no_overwrite(tmp_path):
    a, b = receipt(), receipt("b" * 64, 1)
    handle = embeddings.build_embedding_index([a, b], tmp_path / "index.json")
    index = embeddings.load_embedding_index(handle)
    tracks = [{"track_id": "good", "audio": {"status": "identified", "identity": {"sha256": "a" * 64}}},
              {"track_id": "stale", "audio": {"status": "identified", "identity": {"sha256": "c" * 64}}},
              {"track_id": "catalog", "title": "a"}]
    bound = embeddings.apply_embedding_index(tracks, index)
    assert "embedding" in bound[0] and all("embedding" not in t for t in bound[1:])
    assert all("embedding" not in t for t in tracks)
    assert embeddings.matching_cosine(bound[0]["embedding"], bound[0]["embedding"], "a" * 64, "a" * 64) == 1
    assert embeddings.matching_cosine(bound[0]["embedding"], bound[0]["embedding"], "c" * 64, "a" * 64) is None
    with pytest.raises(PocketError, match="create embedding"):
        embeddings.build_embedding_index([a], tmp_path / "index.json")
    (tmp_path / "index.json").write_text('{}')
    with pytest.raises(PocketError, match="hash"):
        embeddings.load_embedding_index(handle)


def test_reject_mixed_model_and_invalid_vectors(tmp_path):
    for change in ({"model_revision": "wrong"}, {"vector": [float('nan')] * 512},
                   {"vector": [0] * 512}, {"dimension": 128}):
        row = {**receipt(), **change}
        with pytest.raises(PocketError):
            embeddings.build_embedding_index([row], tmp_path / "index.json")


def test_query_ranking_is_offline_and_origin_restricted(tmp_path):
    handle = embeddings.build_embedding_index([receipt(), receipt("b" * 64, 1)], tmp_path / "index.json")
    query = {**receipt(), "modality": "text", "text_origin": "user_authored"}
    options = embeddings.rank_embedding_query(query, handle)
    assert options[0]["audio_sha256"] == "a" * 64
    assert options[0]["cosine"] == 1
    query["text_origin"] = "spotify_api"
    with pytest.raises(PocketError, match="provenance"):
        embeddings.rank_embedding_query(query, handle)


def test_session_prepares_sealed_index_and_stale_audio_falls_back(tmp_path, monkeypatch):
    from pocket_music import on_deck
    handle = embeddings.build_embedding_index([receipt(), receipt("b" * 64, 1)], tmp_path / "index.json")
    tracks = [{"track_id": name, "audio": {"status": "identified", "identity": {"sha256": digest}}}
              for name, digest in (("a", "a" * 64), ("b", "b" * 64))]
    monkeypatch.setattr(on_deck, "_load_bag", lambda _: {"tracks": copy.deepcopy(tracks)})
    session = on_deck.prepare_session({}, tmp_path / "session", current_track_id="a", embedding_index=handle)
    result = on_deck.session_options(session["session_dir"])
    assert result["options"][0]["evidence"]["semantic_cosine"] == 0
    index_path = tmp_path / "session/embeddings.json"
    index_path.write_text(json.dumps({}))
    with pytest.raises(PocketError, match="index"):
        on_deck.session_options(session["session_dir"])
