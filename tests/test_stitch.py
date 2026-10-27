# SPDX-License-Identifier: AGPL-3.0-only
"""Generated fixtures only: no recordings, personal feedback or local media paths."""

import copy
import gzip
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from pocket_music.assets import sha256_file
from pocket_music.errors import PocketError
from pocket_music.stitch import (
    attach_completed_render,
    create_trial,
    prepare_native_trial,
    record_feedback,
    validate_native_trial,
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


def prepare(tmp_path, **options):
    path, root = als_fixture(tmp_path)
    result = prepare_native_trial(
        path,
        tmp_path / "native",
        clip_id="track:100/clip:0",
        shift_beats=1,
        export_start_beat=0,
        export_length_beats=8,
        expected_als_sha256=sha256_file(path),
        **options,
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
    for kind in ("SampleRef", "MxPatchRef"):
        for saved_parent, original_parent in zip(
            saved.findall(".//" + kind), root.findall(".//" + kind), strict=True
        ):
            saved_parent[:] = [copy.deepcopy(child) for child in original_parent]
    assert ET.tostring(root) == ET.tostring(saved)
    assert result["controls_fixed"] is True and result["portability"] is True
    assert result["native_readiness"]["native_loading"] == "unverified"
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
    source = Path(result["trial_dir"]) / result["dependencies"][0]["relative_path"]
    original_bytes = source.read_bytes()
    source.write_bytes(original_bytes[:-1] + bytes([original_bytes[-1] ^ 1]))
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
    from pocket_music import stitch as lab

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


def test_collection_repairs_missing_relative_and_ignores_unrelated_outer_project(tmp_path):
    path, root = als_fixture(tmp_path)
    for ref in root.findall(".//SampleRef/FileRef"):
        ET.SubElement(ref, "RelativePathType", Value="3")
        ET.SubElement(ref, "RelativePath", Value="Samples/Imported/same-name.wav")
    path.write_bytes(gzip.compress(ET.tostring(root)))
    other = tmp_path / "Other Project"
    (other / "Ableton Project Info").mkdir(parents=True)
    media = other / "Samples/Imported"
    media.mkdir(parents=True)
    audio(media, "same-name.wav")
    result = prepare_native_trial(
        path,
        other / "trial",
        clip_id="track:100/clip:0",
        shift_beats=1,
        export_start_beat=0,
        export_length_beats=8,
        expected_als_sha256=sha256_file(path),
    )
    folder = Path(result["trial_dir"])
    assert (folder / "Ableton Project Info").is_dir()
    saved = ET.fromstring(gzip.decompress((folder / "candidate.als").read_bytes()))
    for ref in saved.findall(".//SampleRef/FileRef"):
        relative = ref.find("RelativePath").get("Value")
        assert relative != "Samples/Imported/same-name.wav"
        assert (folder / relative).samefile(Path(ref.find("Path").get("Value")))
        assert sha256_file(folder / relative) == sha256_file(tmp_path / "source.wav")
    ready = validate_native_trial(folder, expected_candidate_sha256=result["candidate_sha256"])
    assert ready["native_readiness"]["copied_files_verified"] == 1
    assert ready["ready_to_compare"] is False


def test_collection_rejects_actual_source_relative_conflict(tmp_path):
    path, root = als_fixture(tmp_path)
    for ref in root.findall(".//SampleRef/FileRef"):
        ET.SubElement(ref, "RelativePathType", Value="3")
        ET.SubElement(ref, "RelativePath", Value="Samples/Imported/same-name.wav")
    path.write_bytes(gzip.compress(ET.tostring(root)))
    media = tmp_path / "Samples/Imported"
    media.mkdir(parents=True)
    conflicting, _ = audio(media, "same-name.wav")
    assert conflicting != tmp_path / "source.wav"
    with pytest.raises(PocketError, match="Conflicting project-relative"):
        prepare_native_trial(
            path,
            tmp_path / "bad",
            clip_id="track:100/clip:0",
            shift_beats=1,
            export_start_beat=0,
            export_length_beats=8,
            expected_als_sha256=sha256_file(path),
        )
    assert not (tmp_path / "bad").exists()


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

    from pocket_music.stitch import NativeExportSettings, TrialVariant

    for provider in (
        create_trial,
        record_feedback,
        prepare_native_trial,
        attach_completed_render,
        validate_native_trial,
    ):
        hints = get_type_hints(provider)
        assert set(inspect.signature(provider).parameters) <= hints.keys()
    assert get_args(get_type_hints(create_trial)["variants"]) == (TrialVariant,)
    assert is_typeddict(TrialVariant) and is_typeddict(NativeExportSettings)
    assert get_type_hints(TrialVariant)["frames"] is int
    assert get_type_hints(NativeExportSettings)["sample_rate"] is int
    assert get_type_hints(attach_completed_render)["expected_frames"] is int
    assert get_type_hints(attach_completed_render)["export_completed"] is bool


def observed(result, **overrides):
    return {
        "observer": "generated-fixture-operator",
        "observed_at": "2026-01-01T00:00:00+00:00",
        "candidate_sha256": result["candidate_sha256"],
        "no_missing_media": True,
        "export_completed": True,
        **overrides,
    }


def test_collected_trial_survives_relocation_and_original_removal(tmp_path):
    result, source_als, _ = prepare(tmp_path)
    old_folder = Path(result["trial_dir"])
    moved = tmp_path / "Moved Project"
    shutil.move(old_folder, moved)
    source_als.unlink()
    (tmp_path / "source.wav").unlink()
    result["trial_dir"] = str(moved)
    validation = validate_native_trial(moved, expected_candidate_sha256=result["candidate_sha256"])
    assert validation["native_readiness"]["stale_absolute_hints_after_relocation"] == 2
    assert validation["native_readiness"]["native_loading"] == "unverified"
    rendered, _ = audio(tmp_path, "rendered.wav")
    receipt = attach(result, rendered, native_observation=observed(result))
    assert receipt["ready_to_compare"] is True
    assert (
        receipt["native_readiness"]["observation_provenance"]
        == "operator_reported_not_independently_observed"
    )
    assert receipt["native_save_verified"] is False


def test_collection_includes_adjacent_maxpat_and_does_not_claim_transitive_scope(tmp_path):
    path, root = als_fixture(tmp_path)
    patch = tmp_path / "Utility.amxd"
    companion = tmp_path / "Utility.maxpat"
    patch.write_bytes(b"generated Max fixture")
    companion.write_text('{"patcher": {}}')
    ref = ET.SubElement(ET.SubElement(root.find("LiveSet/MainTrack"), "MxPatchRef"), "FileRef")
    ET.SubElement(ref, "Path", Value=str(patch))
    path.write_bytes(gzip.compress(ET.tostring(root)))
    result = prepare_native_trial(
        path,
        tmp_path / "trial",
        clip_id="track:100/clip:0",
        shift_beats=1,
        export_start_beat=0,
        export_length_beats=8,
        expected_als_sha256=sha256_file(path),
    )
    assert len(result["dependencies"]) == 3
    scope = result["native_readiness"]["scope"]
    assert "adjacent_same_stem_MAXPAT" in scope
    assert "Opaque Max" in result["native_readiness"]["limitations"][0]
    copied = next(d for d in result["dependencies"] if d["kind"] == "adjacent_same_stem_MAXPAT")
    assert sha256_file(Path(result["trial_dir"]) / copied["relative_path"]) == sha256_file(companion)


@pytest.mark.parametrize(
    "amplitude,disposition",
    [
        (0, "unexpected_silence"),
        (0.000001, "unexpected_near_silence"),
        (1.2, "sample_overload"),
        (float("nan"), "nonfinite_audio"),
    ],
)
def test_failed_signal_is_preserved_but_never_ready(tmp_path, amplitude, disposition):
    result, _, _ = prepare(tmp_path)
    rendered = tmp_path / "rendered.wav"
    sf.write(rendered, np.full((32000, 2), amplitude, dtype="float64"), 8000, subtype="DOUBLE")
    receipt = attach(result, rendered, native_observation=observed(result))
    assert receipt["artifact"]["status"] == "verified"
    assert receipt["signal"]["disposition"] == disposition
    assert receipt["signal"]["reasons"]
    assert receipt["ready_to_compare"] is False
    assert sha256_file(Path(receipt["attachment_dir"]) / "render.wav") == sha256_file(rendered)


def test_nonzero_without_native_observation_stays_unverified(tmp_path):
    result, _, _ = prepare(tmp_path)
    rendered, _ = audio(tmp_path, "rendered.wav")
    receipt = attach(result, rendered)
    assert receipt["signal"]["usable_for_expectation"] is True
    assert receipt["native_readiness"]["operator_observation"] is None
    assert receipt["ready_to_compare"] is False


@pytest.mark.parametrize("flag", ["no_missing_media", "export_completed"])
def test_negative_native_observation_is_preserved_without_readiness(tmp_path, flag):
    result, _, _ = prepare(tmp_path)
    rendered, _ = audio(tmp_path, "rendered.wav")
    receipt = attach(result, rendered, native_observation=observed(result, **{flag: False}))
    assert receipt["ready_to_compare"] is False
    assert receipt["native_readiness"]["operator_observation"][flag] is False
    assert receipt["native_readiness"]["native_loading"] == (
        "operator_reported_missing_media"
        if flag == "no_missing_media"
        else "operator_reported_no_missing_media"
    )
    assert receipt["native_readiness"]["native_export"] == (
        "operator_reported_incomplete" if flag == "export_completed" else "operator_reported_completed"
    )


@pytest.mark.parametrize(
    "override",
    [
        {"candidate_sha256": "0" * 64},
        {"no_missing_media": "true"},
        {"observed_at": "2026-01-01T00:00:00"},
        {"observer": ""},
    ],
)
def test_bad_observation_rejected_before_attachment(tmp_path, override):
    result, _, _ = prepare(tmp_path)
    rendered, _ = audio(tmp_path, "rendered.wav")
    with pytest.raises(PocketError):
        attach(result, rendered, native_observation=observed(result, **override))
    assert not (Path(result["trial_dir"]) / "renders").exists()


def test_intentional_silence_requires_preparation_note_and_is_separate(tmp_path):
    with pytest.raises(PocketError, match="expectation_note"):
        prepare(tmp_path, signal_expectation="intentional_silence")
    assert not (tmp_path / "native").exists()
    result, _, _ = prepare(
        tmp_path, signal_expectation="intentional_silence", expectation_note="Declared silent fixture"
    )
    rendered = tmp_path / "silence.wav"
    sf.write(rendered, np.zeros((32000, 2)), 8000, subtype="FLOAT")
    receipt = attach(result, rendered, native_observation=observed(result))
    assert receipt["ready_to_compare"] is True
    assert receipt["signal"]["disposition"] == "intentional_silence"
    assert receipt["signal"]["expectation_note"] == "Declared silent fixture"
    audible, _ = audio(tmp_path, "audible.wav")
    unexpected = attach(result, audible, native_observation=observed(result))
    assert unexpected["signal"]["disposition"] == "unexpected_audio"
    assert unexpected["ready_to_compare"] is False


def test_collection_failure_leaves_no_published_trial(tmp_path, monkeypatch):
    import pocket_music.stitch as lab

    path, _ = als_fixture(tmp_path)
    copy_file = lab.shutil.copyfile

    def corrupt_copy(source, target):
        copy_file(source, target)
        Path(target).write_bytes(b"corrupt")

    monkeypatch.setattr(lab.shutil, "copyfile", corrupt_copy)
    with pytest.raises(PocketError, match="changed during collection"):
        prepare_native_trial(
            path,
            tmp_path / "trial",
            clip_id="track:100/clip:0",
            shift_beats=1,
            export_start_beat=0,
            export_length_beats=8,
            expected_als_sha256=sha256_file(path),
        )
    assert not (tmp_path / "trial").exists()
    assert not list(tmp_path.glob(".trial.*"))


def test_candidate_mutation_during_attachment_is_not_published(tmp_path, monkeypatch):
    from pocket_music import stitch as lab

    result, _, _ = prepare(tmp_path)
    rendered, _ = audio(tmp_path, "rendered.wav")
    copy_file = lab.shutil.copyfile

    def copy_and_change(source, target):
        copy_file(source, target)
        (Path(result["trial_dir"]) / "candidate.als").write_bytes(b"changed while attaching")

    monkeypatch.setattr(lab.shutil, "copyfile", copy_and_change)
    with pytest.raises(PocketError, match="changed during attachment"):
        attach(result, rendered, native_observation=observed(result))
    assert not list((Path(result["trial_dir"]) / "renders").glob("render-*"))


def test_native_range_label_survives_clip_field_readback(tmp_path):
    result, _, _ = prepare(tmp_path, range_name="Named opening diagnostic")
    assert result["export_range"]["name"] == "Named opening diagnostic"
