# SPDX-License-Identifier: AGPL-3.0-only
"""Generated audio and ALS fixtures only; native acceptance is recorded separately."""
import hashlib
import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from test_stitch import attach, audio, prepare

from pocket_music import pipette
from pocket_music.assets import sha256_file
from pocket_music.errors import PocketError
from pocket_music.stitch import _read_als, prepare_native_trial
from pocket_music.thread_queries import find_clips


def promotion_args(tmp_path, *, silence=False, signal=None):
    options = {"signal_expectation": "intentional_silence", "expectation_note": "Generated silence test"} if silence else {}
    trial, parent, _ = prepare(tmp_path, **options)
    rendered, _ = audio(tmp_path, "rendered.wav")
    if silence or signal is not None:
        sf.write(rendered, np.full((32000, 2), 0 if silence else signal, dtype="float64"), 8000, subtype="DOUBLE")
    receipt = attach(trial, rendered)
    return {"trial_dir": trial["trial_dir"], "output_dir": str(tmp_path / "promoted"),
            "attachment_relative_path": str(Path(receipt["attachment_dir"]).relative_to(trial["trial_dir"])),
            "expected_trial_manifest_sha256": trial["manifest_sha256"],
            "expected_candidate_sha256": trial["candidate_sha256"],
            "expected_attachment_sha256": receipt["receipt_sha256"],
            "decision": {"action": "keep", "actor": "Generated test", "actor_kind": "agent",
                         "reason": "Exercise promotion mechanics, no listening claim"}}, parent


def snapshot(folder):
    return {str(p.relative_to(folder)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in Path(folder).rglob("*") if p.is_file()}


def reseal(path, modify):
    obj = json.loads(path.read_text())
    modify(obj)
    raw = (json.dumps(obj, sort_keys=True, indent=2) + "\n").encode()
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    path.with_name(path.name + ".sha256").write_text(digest + "\n")
    return digest


def test_promotion_preserves_parent_trial_and_selected_audio_exactly(tmp_path):
    args, parent = promotion_args(tmp_path)
    preserved = snapshot(Path(args["trial_dir"]))
    parent_before = (parent.read_bytes(), parent.stat().st_mtime_ns)
    result = pipette.promote_trial(**args)
    assert snapshot(Path(args["trial_dir"])) == preserved
    assert (parent.read_bytes(), parent.stat().st_mtime_ns) == parent_before
    approved = Path(args["output_dir"]) / "evidence/approved-candidate.als"
    assert approved.read_bytes() == (Path(args["trial_dir"]) / "candidate.als").read_bytes()
    lineage = result["lineage"]
    assert lineage["parent_als_sha256"] == sha256_file(parent)
    assert lineage["trial_manifest_sha256"] == args["expected_trial_manifest_sha256"]
    assert lineage["candidate_als_sha256"] == args["expected_candidate_sha256"]
    assert lineage["child_als_sha256"] == sha256_file(Path(result["child_als"]))
    reverted = _read_als(Path(result["child_als"]))
    for change in lineage["reference_hint_changes"]:
        ref = reverted.findall(".//" + change["kind"] + "/FileRef")[change["index"]]
        ref.find("Path").set("Value", change["before"])
    assert ET.tostring(reverted) == ET.tostring(_read_als(approved))
    assert lineage["decision"]["evidence"] == "attributed_decision_context"
    assert result["native_loading"] == "unverified" and result["musical_verdict"] is None
    assert result["thread"]["handle"]["set_sha256"] == result["child_als_sha256"]
    assert result["thread"]["coverage"]["runtime_filesystem_references"] == {"present": 2}
    assert find_clips(result["thread"]["handle"])["total_matches"] == 2


def test_relocated_child_independent_of_trial_and_parent_paths(tmp_path):
    args, _ = promotion_args(tmp_path)
    result = pipette.promote_trial(**args)
    moved = tmp_path / "relocated"
    shutil.move(args["output_dir"], moved)
    shutil.move(args["trial_dir"], tmp_path / "archived-trial")
    checked = pipette.validate_promotion(str(moved), expected_lineage_sha256=result["lineage_sha256"])
    assert checked["child_als_sha256"] == result["child_als_sha256"]
    assert find_clips(checked["thread"]["handle"])["total_matches"] == 2


@pytest.mark.parametrize("kind", ["candidate", "parent", "render", "dependency", "manifest", "attachment"])
def test_stale_evidence_cannot_publish(tmp_path, kind):
    args, parent = promotion_args(tmp_path)
    trial = Path(args["trial_dir"])
    attachment = trial / args["attachment_relative_path"]
    manifest = json.loads((trial / "native-trial.json").read_text())
    paths = {"candidate": trial / "candidate.als", "parent": parent, "render": attachment / "render.wav",
             "dependency": trial / manifest["dependencies"][0]["relative_path"],
             "manifest": trial / "native-trial.json", "attachment": attachment / "attachment.json"}
    with paths[kind].open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(PocketError):
        pipette.promote_trial(**args)
    assert not Path(args["output_dir"]).exists()


@pytest.mark.parametrize("field", ["expected_candidate_sha256", "expected_trial_manifest_sha256", "expected_attachment_sha256"])
def test_wrong_expected_hash(tmp_path, field):
    args, _ = promotion_args(tmp_path)
    args[field] = "0" * 64
    with pytest.raises(PocketError, match="Stale-trial"):
        pipette.promote_trial(**args)
    assert not Path(args["output_dir"]).exists()


@pytest.mark.parametrize("signal,disposition", [(0, "unexpected_silence"), (1e-10, "unexpected_near_silence"),
                                             (1.2, "sample_overload"), (float("nan"), "nonfinite_audio")])
def test_bad_signal_cannot_promote(tmp_path, signal, disposition):
    args, _ = promotion_args(tmp_path, signal=signal)
    with pytest.raises(PocketError, match=disposition):
        pipette.promote_trial(**args)
    assert not Path(args["output_dir"]).exists()


def test_matched_intentional_silence_can_promote(tmp_path):
    args, _ = promotion_args(tmp_path, silence=True)
    result = pipette.promote_trial(**args)
    assert result["lineage"]["signal"]["disposition"] == "intentional_silence"


@pytest.mark.parametrize("field,value", [("action", "reject"), ("actor", ""), ("reason", "  "), ("actor_kind", "system")])
def test_explicit_attribution_required(tmp_path, field, value):
    args, _ = promotion_args(tmp_path)
    args["decision"][field] = value
    with pytest.raises(PocketError):
        pipette.promote_trial(**args)
    assert not Path(args["output_dir"]).exists()


def test_range_candidate_and_trial_bindings_are_checked_even_with_a_valid_seal(tmp_path):
    args, _ = promotion_args(tmp_path)
    path = Path(args["trial_dir"]) / args["attachment_relative_path"] / "attachment.json"
    args["expected_attachment_sha256"] = reseal(path, lambda x: x["declared_export_range"].update(start_beat=1))
    with pytest.raises(PocketError, match="range"):
        pipette.promote_trial(**args)


def test_two_children_and_no_overwrite(tmp_path):
    args, parent = promotion_args(tmp_path)
    first = pipette.promote_trial(**args)
    before = snapshot(Path(first["promotion_dir"]))
    second_trial = prepare_native_trial(str(parent), str(tmp_path / "native-second"),
        clip_id="track:100/clip:0", shift_beats=2, export_start_beat=0, export_length_beats=8,
        expected_als_sha256=sha256_file(parent))
    receipt = attach(second_trial, tmp_path / "rendered.wav")
    next_args = {**args, "trial_dir": second_trial["trial_dir"], "output_dir": str(tmp_path / "second-child"),
                 "expected_trial_manifest_sha256": second_trial["manifest_sha256"],
                 "expected_candidate_sha256": second_trial["candidate_sha256"],
                 "expected_attachment_sha256": receipt["receipt_sha256"],
                 "attachment_relative_path": str(Path(receipt["attachment_dir"]).relative_to(second_trial["trial_dir"]))}
    second = pipette.promote_trial(**next_args)
    assert first["lineage"]["parent_als_sha256"] == second["lineage"]["parent_als_sha256"]
    assert first["child_als_sha256"] != second["child_als_sha256"]
    assert snapshot(Path(first["promotion_dir"])) == before
    with pytest.raises(PocketError, match="already exists"):
        pipette.promote_trial(**args)
    assert snapshot(Path(first["promotion_dir"])) == before


@pytest.mark.parametrize("path", ["../escape", "/tmp/escape", "renders/../../escape", "candidate.als"])
def test_attachment_path_must_be_contained(tmp_path, path):
    args, _ = promotion_args(tmp_path)
    args["attachment_relative_path"] = path
    with pytest.raises(PocketError):
        pipette.promote_trial(**args)


def test_publish_failure_leaves_no_child(tmp_path, monkeypatch):
    args, _ = promotion_args(tmp_path)
    def fail_rename(*_):
        raise OSError("injected publish failure")
    monkeypatch.setattr(pipette.os, "rename", fail_rename)
    with pytest.raises(OSError, match="injected"):
        pipette.promote_trial(**args)
    assert not Path(args["output_dir"]).exists()
    assert not list(tmp_path.glob(".pocket-trial-*"))


def test_modified_child_rejected_by_pipette_and_normal_thread(tmp_path):
    args, _ = promotion_args(tmp_path)
    result = pipette.promote_trial(**args)
    with Path(result["child_als"]).open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(PocketError, match="changed"):
        pipette.validate_promotion(args["output_dir"], expected_lineage_sha256=result["lineage_sha256"])
    with pytest.raises(PocketError, match="Stale"):
        find_clips(result["thread"]["handle"])


def test_child_is_published_after_its_seal_and_dependencies(tmp_path, monkeypatch):
    args, _ = promotion_args(tmp_path)
    original = pipette.os.rename
    names = []
    def rename(source, destination):
        names.append(source.name)
        if source.name == "promoted.als":
            assert (destination.parent / "lineage.json").is_file()
            assert (destination.parent / "lineage.json.sha256").is_file()
            assert (destination.parent / "evidence/render.wav").is_file()
        return original(source, destination)
    monkeypatch.setattr(pipette.os, "rename", rename)
    pipette.promote_trial(**args)
    assert names[-1] == "promoted.als"


def test_partial_publish_failure_rolls_back_its_own_files(tmp_path, monkeypatch):
    args, _ = promotion_args(tmp_path)
    original = pipette.os.rename
    calls = []
    def rename(source, destination):
        calls.append(source.name)
        if len(calls) == 3:
            raise OSError("injected after publishing files")
        return original(source, destination)
    monkeypatch.setattr(pipette.os, "rename", rename)
    with pytest.raises(OSError, match="injected"):
        pipette.promote_trial(**args)
    assert not Path(args["output_dir"]).exists()


def test_input_changed_during_copy_prevents_publication(tmp_path, monkeypatch):
    args, parent = promotion_args(tmp_path)
    original = pipette.shutil.copyfileobj
    def copy_file(source, destination):
        original(source, destination)
        with parent.open("ab") as out:
            out.write(b"concurrent parent edit")
    monkeypatch.setattr(pipette.shutil, "copyfileobj", copy_file)
    with pytest.raises(PocketError, match="input changed"):
        pipette.promote_trial(**args)
    assert not Path(args["output_dir"]).exists()


def test_malformed_sealed_signal_is_a_provider_error(tmp_path):
    args, _ = promotion_args(tmp_path)
    receipt = Path(args["trial_dir"]) / args["attachment_relative_path"] / "attachment.json"
    args["expected_attachment_sha256"] = reseal(receipt, lambda x: x.update(signal=[]))
    with pytest.raises(PocketError, match="Cannot verify"):
        pipette.promote_trial(**args)
    assert not Path(args["output_dir"]).exists()
