"""Generated fixtures only: no recordings, personal feedback or local media paths."""

import copy
import gzip
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from pocket_music.assets import sha256_file
from pocket_music.errors import PocketError
from pocket_music.transition_lab import (
    attach_completed_render,
    create_trial,
    prepare_native_trial,
    record_feedback,
)


def audio(tmp_path, name="source.wav", *, rate=8000, channels=2, frames=32000):
    n = np.arange(frames)
    # Nonzero first/last samples expose unintended fades; asymmetric channels
    # expose implicit downmix and channel order changes.
    data = np.column_stack([0.17 + 0.04 * np.sin(n * (0.013 + j * 0.021)) for j in range(channels)])
    path = tmp_path / name
    sf.write(path, data, rate, subtype="DOUBLE")
    return path, data


def make_trial(tmp_path, **kwargs):
    source, data = audio(tmp_path)
    result = create_trial(
        tmp_path / "trial",
        [
            {"label": "Original", "source_path": str(source)},
            {"label": "Lower", "source_path": str(source), "gain_db": -3},
        ],
        start_frame=31,
        frames=2000,
        **kwargs,
    )
    return result, source, data


def test_exact_samples_no_fades_and_explicit_gain(tmp_path):
    result, source, data = make_trial(tmp_path)
    first = sf.read(Path(result["trial_dir"]) / "v01.wav", always_2d=True)[0]
    second = sf.read(Path(result["trial_dir"]) / "v02.wav", always_2d=True)[0]
    assert np.array_equal(first, data[31:2031])
    assert np.array_equal(second, data[31:2031] * 10 ** (-3 / 20))
    assert first[0, 0] != 0 and first[-1, 0] != 0
    assert result["variants"][0]["source"]["sha256"] == sha256_file(source)
    assert result["processing"]["fades"] is False
    assert result["variants"][0]["zero_gain_decoded_samples_exact"] is True
    assert result["variants"][1]["zero_gain_decoded_samples_exact"] is False


def test_separately_mapped_source_windows(tmp_path):
    source, data = audio(tmp_path)
    result = create_trial(
        tmp_path / "mapped",
        [
            {"label": "One", "source_path": str(source), "start_frame": 3},
            {"label": "Two", "source_path": str(source), "start_frame": 100},
        ],
        frames=101,
    )
    assert np.array_equal(sf.read(Path(result["trial_dir"]) / "v02.wav")[0], data[100:201])
    assert result["variants"][1]["mapping"] == "separate_source_window"


@pytest.mark.parametrize("start,count", [(-1, 10), (0, 0), (31990, 20), (True, 10), (0.5, 10), (0, None)])
def test_bounds_fail_without_creating_output(tmp_path, start, count):
    source, _ = audio(tmp_path)
    with pytest.raises(PocketError):
        create_trial(
            tmp_path / "bad", [{"label": "X", "source_path": str(source)}], start_frame=start, frames=count
        )
    assert not (tmp_path / "bad").exists()


def test_stale_identity_and_overwrite(tmp_path):
    result, source, _ = make_trial(tmp_path)
    previous = sha256_file(Path(result["trial_dir"]) / "trial.json")
    with pytest.raises(PocketError, match="already exists"):
        create_trial(
            result["trial_dir"], [{"label": "X", "source_path": str(source)}], start_frame=0, frames=10
        )
    assert sha256_file(Path(result["trial_dir"]) / "trial.json") == previous
    with pytest.raises(PocketError, match="Stale"):
        create_trial(
            tmp_path / "stale",
            [{"label": "X", "source_path": str(source), "expected_sha256": "0" * 64}],
            start_frame=0,
            frames=10,
        )


@pytest.mark.parametrize("rate,channels", [(16000, 2), (8000, 1), (8000, 3)])
def test_no_implicit_rate_or_channel_conversion(tmp_path, rate, channels):
    a, _ = audio(tmp_path, "a.wav")
    b, _ = audio(tmp_path, "b.wav", rate=rate, channels=channels)
    with pytest.raises(PocketError):
        create_trial(
            tmp_path / "trial",
            [{"label": "A", "source_path": str(a)}, {"label": "B", "source_path": str(b)}],
            start_frame=0,
            frames=100,
        )


def test_unequal_lengths_require_explicit_opt_in(tmp_path):
    a, _ = audio(tmp_path)
    variants = [
        {"label": "A", "source_path": str(a), "frames": 10},
        {"label": "B", "source_path": str(a), "frames": 11},
    ]
    with pytest.raises(PocketError, match="Unequal"):
        create_trial(tmp_path / "bad", variants, start_frame=0)
    result = create_trial(tmp_path / "good", variants, start_frame=0, allow_duration_mismatch=True)
    assert result["allow_duration_mismatch"] is True


def test_nonfinite_rejected_and_overload_not_limited(tmp_path):
    a, data = audio(tmp_path)
    data[1, 0] = np.nan
    sf.write(a, data, 8000, subtype="DOUBLE")
    with pytest.raises(PocketError, match="non-finite"):
        create_trial(tmp_path / "bad", [{"label": "X", "source_path": str(a)}], start_frame=0, frames=100)
    assert not (tmp_path / "bad").exists()
    b, clean = audio(tmp_path, "clean.wav")
    result = create_trial(
        tmp_path / "loud",
        [{"label": "Explicit gain", "source_path": str(b), "gain_db": 24}],
        start_frame=0,
        frames=100,
    )
    assert result["variants"][0]["signal"]["sample_peak"] > 1
    assert np.array_equal(sf.read(tmp_path / "loud/v01.wav")[0], clean[:100] * 10 ** (24 / 20))


def feedback(result, **overrides):
    args = {
        "output_sha256": result["variants"][0]["output"]["sha256"],
        "note": "Fixture timing observation",
        "start_frame": 0,
        "end_frame": 100,
        "scope": "timing",
    }
    args.update(overrides)
    return record_feedback(result["trial_dir"], "v01", **args)


def test_feedback_is_scoped_immutable_human_evidence(tmp_path):
    result, _, _ = make_trial(tmp_path)
    first = feedback(result)
    second = feedback(result, scope="bar_phase")
    assert first["feedback_file"] != second["feedback_file"]
    assert first["evidence"] == "listener_supplied"
    assert first["trial_manifest_sha256"] == result["manifest_sha256"]
    assert sha256_file(Path(result["trial_dir"]) / "trial.json") == result["manifest_sha256"]


@pytest.mark.parametrize(
    "override",
    [
        {"scope": "all music"},
        {"end_frame": 2001},
        {"start_frame": 101},
        {"output_sha256": "0" * 64},
        {"note": ""},
    ],
)
def test_feedback_scope_and_stale_hash_rejected(tmp_path, override):
    result, _, _ = make_trial(tmp_path)
    with pytest.raises(PocketError):
        feedback(result, **override)


def test_manifest_and_output_tampering_rejected(tmp_path):
    result, _, _ = make_trial(tmp_path)
    manifest = Path(result["trial_dir"]) / "trial.json"
    original = manifest.read_bytes()
    manifest.write_bytes(original + b" ")
    with pytest.raises(PocketError, match="Manifest changed"):
        feedback(result)
    manifest.write_bytes(original)
    (Path(result["trial_dir"]) / "v01.wav").write_bytes(b"different audio")
    with pytest.raises(PocketError, match="Output identity"):
        feedback(result)


def als_fixture(tmp_path):
    source, _ = audio(tmp_path)
    root = ET.Element("Ableton", Creator="Ableton Live fixture")
    live = ET.SubElement(root, "LiveSet")
    tracks = ET.SubElement(live, "Tracks")
    for track_id, start, end in ((100, 0, 8), (101, 8, 24)):
        track = ET.SubElement(tracks, "AudioTrack", Id=str(track_id))
        element = track
        for name in _path_parts():
            element = ET.SubElement(element, name)
        clip = ET.SubElement(element, "AudioClip", Id="0", Time=str(start))
        for name, value in (("CurrentStart", start), ("CurrentEnd", end), ("IsWarped", "true")):
            ET.SubElement(clip, name, Value=str(value))
        loop = ET.SubElement(clip, "Loop")
        for name, value in (
            ("LoopOn", "false"),
            ("LoopStart", "-0.125"),
            ("LoopEnd", 8),
            ("StartRelative", 0),
        ):
            ET.SubElement(loop, name, Value=str(value))
        markers = ET.SubElement(clip, "WarpMarkers")
        ET.SubElement(markers, "WarpMarker", SecTime="0", BeatTime="-0.125")
        ET.SubElement(markers, "WarpMarker", SecTime="4.0625", BeatTime="8")
        ref = ET.SubElement(ET.SubElement(clip, "SampleRef"), "FileRef")
        ET.SubElement(ref, "Path", Value=str(source))
        ET.SubElement(clip, "Fades", FadeInLength="0.004")
    main = ET.SubElement(live, "MainTrack")
    tempo = ET.SubElement(ET.SubElement(ET.SubElement(main, "DeviceChain"), "Mixer"), "Tempo")
    ET.SubElement(tempo, "Manual", Value="120")
    ET.SubElement(tempo, "AutomationTarget", Id="8")
    path = tmp_path / "original.als"
    path.write_bytes(gzip.compress(ET.tostring(root)))
    return path, root


def _path_parts():
    return ["DeviceChain", "MainSequencer", "Sample", "ArrangerAutomation", "Events"]


def prepare(tmp_path):
    path, root = als_fixture(tmp_path)
    result = prepare_native_trial(
        path,
        tmp_path / "native",
        clip_id="track:100/clip:0",
        shift_beats=1,
        export_start_beat=0,
        export_length_beats=8,
        expected_als_sha256=sha256_file(path),
    )
    return result, path, root


def test_native_only_translates_selected_media_preserves_controls_and_original(tmp_path):
    result, original, root = prepare(tmp_path)
    assert result["source_als"]["sha256"] == sha256_file(original)
    saved = ET.fromstring(gzip.decompress((Path(result["trial_dir"]) / "candidate.als").read_bytes()))
    old = root.find(".//AudioClip")
    new = saved.find(".//AudioClip")
    assert new.get("Time") == "1" and new.find("CurrentEnd").get("Value") == "9"
    for key in ("Time",):
        new.set(key, old.get(key))
    for key in ("CurrentStart", "CurrentEnd"):
        new.find(key).set("Value", old.find(key).get("Value"))
    assert ET.tostring(root) == ET.tostring(saved)
    assert result["controls_fixed"] is True and result["portability"] is False
    assert result["readback"]["native_save"] is False
    assert result["dependencies"][0]["sha256"] == sha256_file(tmp_path / "source.wav")


@pytest.mark.parametrize("mutation", ["midi", "duplicate", "loop", "relative", "unwarped", "tempo_curve"])
def test_native_rejects_unsupported_and_ambiguous_geometry(tmp_path, mutation):
    path, root = als_fixture(tmp_path)
    clip = root.find(".//AudioClip")
    if mutation == "midi":
        ET.SubElement(root.find("LiveSet/Tracks"), "MidiTrack", Id="105")
    elif mutation == "duplicate":
        root.find(".//AudioTrack/DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events").append(
            copy.deepcopy(clip)
        )
    elif mutation == "loop":
        clip.find("Loop/LoopOn").set("Value", "true")
    elif mutation == "relative":
        clip.find("Loop/StartRelative").set("Value", "1")
    elif mutation == "unwarped":
        clip.find("IsWarped").set("Value", "false")
    else:
        main = root.find("LiveSet/MainTrack")
        env = ET.SubElement(
            ET.SubElement(ET.SubElement(main, "AutomationEnvelopes"), "Envelopes"), "AutomationEnvelope"
        )
        ET.SubElement(ET.SubElement(env, "EnvelopeTarget"), "PointeeId", Value="8")
        events = ET.SubElement(ET.SubElement(env, "Automation"), "Events")
        ET.SubElement(events, "FloatEvent", Time="0", Value="120", CurveControl1X="0.2")
    path.write_bytes(gzip.compress(ET.tostring(root)))
    with pytest.raises(PocketError):
        prepare_native_trial(
            path,
            tmp_path / "native",
            clip_id="track:100/clip:0",
            shift_beats=1,
            export_start_beat=0,
            export_length_beats=8,
            expected_als_sha256=sha256_file(path),
        )


def attach(result, rendered, **overrides):
    args = {
        "expected_candidate_sha256": result["candidate_sha256"],
        "rendered_start_beat": 0,
        "rendered_length_beats": 8,
        "expected_frames": 32000,
        "export_completed": True,
        "settings": {
            "rendered_track": "Main",
            "normalization": False,
            "mono": False,
            "loop_render": False,
            "dither": "none",
            "sample_rate": 8000,
            "channels": 2,
        },
    }
    args.update(overrides)
    return attach_completed_render(result["trial_dir"], rendered, **args)


def test_attach_completed_actual_render_but_no_fake_native_claim(tmp_path):
    result, _, _ = prepare(tmp_path)
    rendered, _ = audio(tmp_path, "rendered.wav")
    record = attach(result, rendered)
    assert record["signal"]["decoded_frames"] == 32000
    assert record["native_export_attribution"] == "user_supplied_not_observed_by_pocket"
    assert record["native_save_verified"] is False
    assert sha256_file(Path(record["attachment_dir"]) / "render.wav") == sha256_file(rendered)
    assert sha256_file(Path(result["trial_dir"]) / "native-trial.json") == result["manifest_sha256"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"export_completed": False},
        {"expected_frames": 31999},
        {"rendered_start_beat": 1},
        {"expected_candidate_sha256": "0" * 64},
    ],
)
def test_attach_requires_completion_identity_and_exact_declared_range(tmp_path, overrides):
    result, _, _ = prepare(tmp_path)
    rendered, _ = audio(tmp_path, "rendered.wav")
    with pytest.raises(PocketError):
        attach(result, rendered, **overrides)


def test_attach_rejects_changed_dependencies_and_candidate(tmp_path):
    result, _, _ = prepare(tmp_path)
    rendered, _ = audio(tmp_path, "rendered.wav")
    source = tmp_path / "source.wav"
    source.write_bytes(b"changed dependency")
    with pytest.raises(PocketError, match="dependency changed"):
        attach(result, rendered)
    candidate = Path(result["trial_dir"]) / "candidate.als"
    candidate.write_bytes(b"new native serialization")
    with pytest.raises(PocketError, match="Candidate changed"):
        attach(result, rendered)


def test_native_rejects_actual_clip_automation_envelope(tmp_path):
    path, root = als_fixture(tmp_path)
    clip = root.find(".//AudioClip")
    ET.SubElement(ET.SubElement(ET.SubElement(clip, "Envelopes"), "Envelopes"), "AutomationEnvelope", Id="0")
    path.write_bytes(gzip.compress(ET.tostring(root)))
    with pytest.raises(PocketError, match="Clip-local automation"):
        prepare_native_trial(
            path,
            tmp_path / "native",
            clip_id="track:100/clip:0",
            shift_beats=1,
            export_start_beat=0,
            export_length_beats=8,
            expected_als_sha256=sha256_file(path),
        )


def test_native_rejects_tempo_curve_child(tmp_path):
    path, root = als_fixture(tmp_path)
    main = root.find("LiveSet/MainTrack")
    env = ET.SubElement(
        ET.SubElement(ET.SubElement(main, "AutomationEnvelopes"), "Envelopes"), "AutomationEnvelope"
    )
    ET.SubElement(ET.SubElement(env, "EnvelopeTarget"), "PointeeId", Value="8")
    events = ET.SubElement(ET.SubElement(env, "Automation"), "Events")
    event = ET.SubElement(events, "FloatEvent", Time="0", Value="120")
    ET.SubElement(event, "CurveControls", Value="0.2")
    path.write_bytes(gzip.compress(ET.tostring(root)))
    with pytest.raises(PocketError, match="plain linear tempo"):
        prepare_native_trial(
            path,
            tmp_path / "native",
            clip_id="track:100/clip:0",
            shift_beats=1,
            export_start_beat=0,
            export_length_beats=8,
            expected_als_sha256=sha256_file(path),
        )


@pytest.mark.parametrize(
    "xml", [b'<!DOCTYPE Ableton [<!ENTITY x "fixture">]><Ableton/>', b"<NotAbleton><LiveSet/></NotAbleton>"]
)
def test_native_rejects_dtd_entities_and_wrong_root(tmp_path, xml):
    path = tmp_path / "bad.als"
    path.write_bytes(gzip.compress(xml))
    with pytest.raises(PocketError):
        prepare_native_trial(
            path,
            tmp_path / "native",
            clip_id="track:100/clip:0",
            shift_beats=1,
            export_start_beat=0,
            export_length_beats=8,
            expected_als_sha256=sha256_file(path),
        )


@pytest.mark.parametrize("extra_frames,accepted", [(2, True), (3, False), (268, False)])
def test_native_attach_has_two_frame_limit_not_a_millisecond_timing_excuse(tmp_path, extra_frames, accepted):
    result, _, _ = prepare(tmp_path)
    rendered, _ = audio(tmp_path, "rendered.wav", frames=32000 + extra_frames)
    if accepted:
        record = attach(result, rendered, expected_frames=32000 + extra_frames)
        assert record["frame_delta"] == extra_frames
        assert record["duration_tolerance_frames"] == 2
    else:
        with pytest.raises(PocketError, match="2 frames"):
            attach(result, rendered, expected_frames=32000 + extra_frames)


def test_source_mutation_between_validation_and_extraction_is_rejected(tmp_path, monkeypatch):
    from pocket_music import transition_lab as lab

    source, data = audio(tmp_path)
    identify = lab.identify_audio

    def identify_then_change(path):
        identity = identify(path)
        changed = data.copy()
        changed[0] += 0.01
        sf.write(source, changed, 8000, subtype="DOUBLE")
        return identity

    monkeypatch.setattr(lab, "identify_audio", identify_then_change)
    with pytest.raises(PocketError, match="changed before extraction"):
        create_trial(
            tmp_path / "trial", [{"label": "X", "source_path": str(source)}], start_frame=0, frames=100
        )
    assert not (tmp_path / "trial").exists()


def test_native_candidate_rejects_conflicting_project_relative_media(tmp_path):
    path, root = als_fixture(tmp_path)
    for ref in root.findall(".//SampleRef/FileRef"):
        ET.SubElement(ref, "RelativePathType", Value="3")
        ET.SubElement(ref, "RelativePath", Value="Samples/Imported/same-name.wav")
    path.write_bytes(gzip.compress(ET.tostring(root)))
    other_project = tmp_path / "Other Project"
    (other_project / "Ableton Project Info").mkdir(parents=True)
    media = other_project / "Samples/Imported"
    media.mkdir(parents=True)
    audio(media, "same-name.wav")
    with pytest.raises(PocketError, match="Conflicting project-relative"):
        prepare_native_trial(
            path,
            other_project / "trial",
            clip_id="track:100/clip:0",
            shift_beats=1,
            export_start_beat=0,
            export_length_beats=8,
            expected_als_sha256=sha256_file(path),
        )
    assert not (other_project / "trial").exists()


def test_native_attachment_rechecks_new_relative_conflict(tmp_path):
    path, root = als_fixture(tmp_path)
    for ref in root.findall(".//SampleRef/FileRef"):
        ET.SubElement(ref, "RelativePathType", Value="3")
        ET.SubElement(ref, "RelativePath", Value="Samples/Imported/same-name.wav")
    path.write_bytes(gzip.compress(ET.tostring(root)))
    project = tmp_path / "Other Project"
    (project / "Ableton Project Info").mkdir(parents=True)
    result = prepare_native_trial(
        path,
        project / "trial",
        clip_id="track:100/clip:0",
        shift_beats=1,
        export_start_beat=0,
        export_length_beats=8,
        expected_als_sha256=sha256_file(path),
    )
    media = project / "Samples/Imported"
    media.mkdir(parents=True)
    audio(media, "same-name.wav")
    rendered, _ = audio(tmp_path, "rendered.wav")
    with pytest.raises(PocketError, match="Conflicting project-relative"):
        attach(result, rendered)


def test_feedback_rejects_manifest_mutation_between_read_and_use(tmp_path, monkeypatch):
    import json

    result, _, _ = make_trial(tmp_path)
    manifest_path = Path(result["trial_dir"]) / "trial.json"
    original_read = Path.read_bytes

    def read_then_replace(path):
        data = original_read(path)
        if path == manifest_path:
            changed = json.loads(data)
            changed["variants"][0]["frames"] = 9999
            path.write_text(json.dumps(changed))
        return data

    monkeypatch.setattr(Path, "read_bytes", read_then_replace)
    with pytest.raises(PocketError, match="Manifest changed"):
        feedback(result, end_frame=4000)
    assert not (Path(result["trial_dir"]) / "feedback").exists()


def test_public_inputs_have_discoverable_nested_types():
    import inspect
    from typing import get_args, get_type_hints

    from typing_extensions import is_typeddict

    from pocket_music.transition_lab import NativeExportSettings, TrialVariant

    for provider in (create_trial, record_feedback, prepare_native_trial, attach_completed_render):
        hints = get_type_hints(provider)
        assert set(inspect.signature(provider).parameters) <= hints.keys()
    assert get_args(get_type_hints(create_trial)["variants"]) == (TrialVariant,)
    assert is_typeddict(TrialVariant) and is_typeddict(NativeExportSettings)
    assert get_type_hints(TrialVariant)["frames"] is int
    assert get_type_hints(NativeExportSettings)["sample_rate"] is int
    assert get_type_hints(attach_completed_render)["expected_frames"] is int
    assert get_type_hints(attach_completed_render)["export_completed"] is bool
