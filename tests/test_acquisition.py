import functools
import http.server
import json
import shutil
import subprocess
import threading
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from pocket_music import acquisition as acq
from pocket_music.assets import sha256_file
from pocket_music.errors import PocketError


def planned(tmp_path, **kwargs):
    return acq.plan_acquisition("https://example.org/fixture.wav", str(tmp_path / "plan"),
                                version_note="Generated fixture; no recording edition inferred",
                                authorization_note="Generated test audio", expected_source_id="fixture", **kwargs)


def execute(tmp_path, handle):
    return acq.acquire_source(handle["plan_dir"], expected_plan_sha256=handle["sha256"],
                              output_dir=str(tmp_path / "result"))


class FixtureRunner:
    def __init__(self, tmp_path, *, silent=False):
        self.source = tmp_path / "source.wav"
        signal = np.zeros(16000) if silent else .12 * np.sin(2 * np.pi * 440 * np.arange(16000) / 16000)
        sf.write(self.source, signal, 16000, subtype="PCM_16")
        self.info = {"id": "fixture", "format_id": "wave", "ext": "wav", "duration": 1,
                     "title": "Generated test", "acodec": "pcm_s16le", "webpage_url": "https://example.org"}
        self.partial = self.bad_decode = self.mismatch = False
        self.calls = []

    def __call__(self, args, *, timeout=180):
        self.calls.append(args)
        if "--version" in args or "-version" in args:
            text = "fixture-version"
        elif "--dump-single-json" in args:
            text = json.dumps(self.info)
        elif "-show_streams" in args:
            text = json.dumps({"streams": [{"codec_type": "audio", "sample_rate": "16000", "channels": 1,
                                           "codec_name": "pcm_s16le", "duration": "1.0"}], "format": {}})
        elif "--write-info-json" in args:
            template = Path(args[args.index("-o") + 1])
            original = template.parent / ("original.wav.part" if self.partial else "original.wav")
            shutil.copyfile(self.source, original)
            info = {**self.info, "id": "wrong"} if self.mismatch else self.info
            (template.parent / "original.info.json").write_text(json.dumps(info))
            text = ""
        elif "-c:a" in args:
            signal, rate = sf.read(self.source, dtype="float32")
            sf.write(args[-1], signal, rate, subtype="FLOAT")
            return subprocess.CompletedProcess(args, 1 if self.bad_decode else 0, "", "broken" if self.bad_decode else "")
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, text, "")


def test_original_and_float_derivative_exact_no_hidden_processing(tmp_path, monkeypatch):
    runner = FixtureRunner(tmp_path)
    monkeypatch.setattr(acq, "_run", runner)
    handle = planned(tmp_path)
    result = execute(tmp_path, handle)
    assert result["status"] == "completed" and result["ready_for_analysis"]
    assert not result["ready_as_requested_recording"] and result["version_identity"] == "unverified"
    assert result["original"]["sha256"] == sha256_file(runner.source)
    data, rate = sf.read(result["decoded"]["local_path"], dtype="float32")
    expected, _ = sf.read(runner.source, dtype="float32")
    np.testing.assert_array_equal(data, expected)
    assert rate == 16000 and result["decoded"]["channels"] == 1
    command = next(c for c in runner.calls if "-c:a" in c)
    assert "-ar" not in command and "-ac" not in command and "-af" not in command
    with pytest.raises(PocketError, match="already exists"):
        execute(tmp_path, handle)


@pytest.mark.parametrize("mode", ["partial", "bad_decode", "mismatch"])
def test_partial_bad_decode_and_wrong_source_preserved_as_failed(tmp_path, monkeypatch, mode):
    runner = FixtureRunner(tmp_path)
    setattr(runner, mode, True)
    monkeypatch.setattr(acq, "_run", runner)
    result = execute(tmp_path, planned(tmp_path))
    assert result["status"] == "failed" and not result["ready_for_analysis"]
    assert Path(result["path"]).exists() and (tmp_path / "result" / ".staging").exists()


def test_silent_file_kept_but_not_ready(tmp_path, monkeypatch):
    runner = FixtureRunner(tmp_path, silent=True)
    monkeypatch.setattr(acq, "_run", runner)
    result = execute(tmp_path, planned(tmp_path))
    assert result["status"] == "completed" and not result["ready_for_analysis"]
    assert "digital_silence" in result["signal"]["reasons"]


def test_decoded_mutation_during_scan_is_not_published(tmp_path, monkeypatch):
    runner = FixtureRunner(tmp_path)
    monkeypatch.setattr(acq, "_run", runner)
    original_scan = acq._scan
    def changed(path):
        result = original_scan(path)
        sf.write(path, np.ones(16000) * .01, 16000, subtype="FLOAT")
        return result
    monkeypatch.setattr(acq, "_scan", changed)
    result = execute(tmp_path, planned(tmp_path))
    assert result["status"] == "failed" and "changed during signal" in result["reason"]


def test_stale_plan_before_any_execution(tmp_path, monkeypatch):
    handle = planned(tmp_path)
    monkeypatch.setattr(acq, "_run", lambda *a, **k: pytest.fail("Must not run"))
    with pytest.raises(PocketError, match="identity"):
        acq.acquire_source(handle["plan_dir"], expected_plan_sha256="0" * 64,
                           output_dir=str(tmp_path / "bad"))
    assert not (tmp_path / "bad").exists()


def test_explicit_conversion_is_checked_not_merely_labeled(tmp_path, monkeypatch):
    runner = FixtureRunner(tmp_path)
    monkeypatch.setattr(acq, "_run", runner)
    result = execute(tmp_path, planned(tmp_path, sample_rate=48000, channels=2))
    assert result["status"] == "failed" and "policy mismatch" in result["reason"]
    command = next(c for c in runner.calls if "-c:a" in c)
    assert command[command.index("-ar") + 1] == "48000"
    assert command[command.index("-ac") + 1] == "2"


def test_malformed_metadata_preserved_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(acq, "_run", lambda args, **kw: subprocess.CompletedProcess(args, 0, "not-json", ""))
    result = execute(tmp_path, planned(tmp_path))
    assert result["status"] == "failed" and not result["ready_for_analysis"]


@pytest.mark.parametrize("url", ["file:///secret", "https://name:password@example.org/x", "https://open.spotify.com/track/x"])
def test_unsupported_urls(tmp_path, url):
    with pytest.raises(PocketError):
        acq.plan_acquisition(url, str(tmp_path / "bad"), version_note="unknown", authorization_note="fixture")


def test_discovery_is_bounded_and_does_not_download(monkeypatch):
    calls = []
    def metadata(args, **kwargs):
        calls.append(args)
        return {"entries": [{"id": str(n), "url": f"https://example.org/{n}"} for n in range(6)]}
    monkeypatch.setattr(acq, "_json_command", metadata)
    result = acq.discover_sources("artist song", limit=2)
    assert len(result["candidates"]) == 2 and not result["downloaded"]
    assert "--skip-download" in calls[0] and calls[0][-1] == "ytsearch2:artist song"
    with pytest.raises(PocketError):
        acq.discover_sources("artist", limit=11)


def test_reviewed_format_retry_is_pinned_and_mismatch_fails(tmp_path, monkeypatch):
    runner = FixtureRunner(tmp_path)
    monkeypatch.setattr(acq, "_run", runner)
    result = execute(tmp_path, planned(tmp_path, format_id="wave"))
    assert result["status"] == "completed"
    commands = [c for c in runner.calls if "-f" in c]
    assert all(c[c.index("-f") + 1] == "wave" for c in commands)
    other = tmp_path / "other"
    other.mkdir()
    failed = execute(other, planned(other, format_id="different"))
    assert failed["status"] == "failed" and "explicitly selected" in failed["reason"]
    assert not (other / "result" / ".staging" / "original.wav").exists()


def test_zero_exit_with_decoder_error_still_fails(tmp_path, monkeypatch):
    runner = FixtureRunner(tmp_path)
    def warning(args, **kwargs):
        result = runner(args, **kwargs)
        if "-c:a" in args:
            return subprocess.CompletedProcess(args, 0, "", "Error parsing Opus packet header.")
        return result
    monkeypatch.setattr(acq, "_run", warning)
    result = execute(tmp_path, planned(tmp_path))
    assert result["status"] == "failed" and not result["ready_for_analysis"]


def test_format_inspection_is_bounded_and_sanitized(monkeypatch):
    monkeypatch.setattr(acq, "_json_command", lambda *args, **kwargs: {
        "id": "example", "formats": [
            {"format_id": "140", "acodec": "aac", "url": "https://cdn.invalid/?secret=abc"},
            {"format_id": "251", "acodec": "opus"}, {"format_id": "video", "acodec": "none"}]})
    result = acq.inspect_source_formats("https://example.org/audio", limit=1)
    assert len(result["formats"]) == 1 and result["total_audio_formats"] == 2 and result["truncated"]
    assert "secret" not in json.dumps(result) and not result["downloaded"]


@pytest.mark.parametrize("format_id", ["140/251", "bestaudio", "", "-f 140", None])
def test_format_id_requires_concrete_token(tmp_path, format_id):
    if format_id is None:
        # Omission selects the default.
        planned(tmp_path, format_id=format_id)
    else:
        with pytest.raises(PocketError):
            planned(tmp_path, format_id=format_id)


def test_real_ytdlp_generated_http_audio(tmp_path):
    """No public recording: exercise optional binaries against a local generated WAV."""
    binaries = [shutil.which(x) for x in ("yt-dlp", "ffmpeg", "ffprobe")]
    if not all(binaries):
        pytest.skip("Optional yt-dlp/ffmpeg/ffprobe are not installed")
    source = tmp_path / "served"
    source.mkdir()
    t = np.arange(32000) / 16000
    sf.write(source / "fixture.wav", .1 * np.sin(2 * np.pi * 431 * t), 16000, subtype="PCM_16")
    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    handler = functools.partial(QuietHandler, directory=str(source))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        handle = acq.plan_acquisition(f"http://127.0.0.1:{server.server_port}/fixture.wav",
                                      str(tmp_path / "plan"), version_note="Generated fixture",
                                      authorization_note="Test-generated original")
        result = acq.acquire_source(handle["plan_dir"], expected_plan_sha256=handle["sha256"],
                                    output_dir=str(tmp_path / "acquired"), yt_dlp_executable=binaries[0],
                                    ffmpeg_executable=binaries[1], ffprobe_executable=binaries[2])
        assert result["status"] == "completed", result
        assert result["original"]["sha256"] == sha256_file(source / "fixture.wav")
        assert result["decoded"]["frames"] == 32000
        assert result["decoded"]["sample_rate"] == 16000 and result["decoded"]["channels"] == 1
        original, _ = sf.read(source / "fixture.wav", dtype="float32")
        decoded, _ = sf.read(result["decoded"]["local_path"], dtype="float32")
        np.testing.assert_array_equal(original, decoded)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
