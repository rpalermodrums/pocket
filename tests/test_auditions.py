# SPDX-License-Identifier: AGPL-3.0-only
"""Generated render evidence only: tests do not claim rendering or human listening."""
import shutil
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from test_native_candidates import sealed_fixture, snapshot

from pocket_music.artifact_store import put_bytes, put_record, read_record
from pocket_music.assets import sha256_file
from pocket_music.auditions import (
    attach_candidate_render,
    audition_feedback,
    audition_plan,
    promote_candidate,
    validate_candidate_promotion,
)
from pocket_music.errors import PocketError


def plan_fixture(tmp_path):
    baseline, _, args, parent = sealed_fixture(tmp_path, prepare_id="baseline", seal_id="baseline-seal")
    candidate, _, _, _ = sealed_fixture(tmp_path, "with_material", "variant", "variant-seal")
    store = args["store_root"]
    plan = audition_plan(store, "audition", [candidate["artifacts"]["candidate"]],
                         baseline["artifacts"]["candidate"], [{"n": 4, "d": 1}, {"n": 8, "d": 1}],
                         "Synthetic fixture only", pre_roll_qn={"n": 4, "d": 1},
                         post_roll_qn={"n": 4, "d": 1}, tail_seconds=1, sample_rate=8000)
    return store, plan["artifacts"]["audition_plan"], candidate["artifacts"]["candidate"], parent


def attachment_args(tmp_path, *, signal=.01, extra_frames=0):
    store, plan_handle, candidate, parent = plan_fixture(tmp_path)
    plan = read_record(plan_handle, store)
    trial = read_record(candidate, store)
    render = tmp_path / "synthetic-render.wav"
    sf.write(render, np.full((plan["expected_frames"] + extra_frames, 2), signal, dtype="float64"),
             8000, subtype="FLOAT")
    return {"store_root": store, "request_id": "attach", "candidate": candidate,
            "render_plan": plan_handle, "path": str(render), "expected_sha256": sha256_file(render),
            "actual_settings": plan["settings"],
            "native_report": {"actor": "Synthetic test; no native export", "actor_kind": "agent",
                              "observed_at": "2026-09-16T00:00:00+00:00",
                              "candidate_sha256": trial["saved_als_sha256"], "export_completed": True,
                              "arrangement_only": True, "no_missing_media": True}}, parent


def promotion_args(tmp_path):
    args, parent = attachment_args(tmp_path)
    result = attach_candidate_render(**args)
    return {"store_root": args["store_root"], "request_id": "promote", "candidate": args["candidate"],
            "attachment": result["artifacts"]["attachment"], "output_dir": str(tmp_path / "kept"),
            "decision": {"action": "keep", "actor": "Synthetic technical reviewer", "actor_kind": "agent",
                         "reason": "Test preservation; no listening claim"}}, parent


def test_promotion_collects_explicit_related_artifacts_and_nested_evidence(tmp_path):
    args, _ = promotion_args(tmp_path)
    raw = put_bytes(b'opaque fixture preset', args['store_root'], 'fixture.adv', 'pocket.native-preset/v1')
    related = put_record({'schema': 'pocket.test-preset-evidence/v1', 'preset': raw}, args['store_root'])
    args['related_artifacts'] = [related]
    result = promote_candidate(**args)
    folder = Path(result['promotion_dir'])
    moved = tmp_path / 'moved-related'
    folder.rename(moved)
    checked = validate_candidate_promotion(str(moved), result['lineage_sha256'])
    assert checked['musical_verdict'] == 'agent_technical_keep'
    assert (moved / 'evidence' / raw['artifact_uri']).read_bytes() == b'opaque fixture preset'
    (moved / 'evidence' / raw['artifact_uri']).write_bytes(b'changed')
    with pytest.raises(PocketError):
        validate_candidate_promotion(str(moved), result['lineage_sha256'])


def test_related_artifact_failure_cannot_publish_or_pass_retry(tmp_path):
    args, _ = promotion_args(tmp_path)
    raw = put_bytes(b'MThd fixture', args['store_root'], 'material.mid', 'pocket.smf-derivative/v1')
    args['related_artifacts'] = [raw]
    promote_candidate(**args)
    (Path(args['store_root']) / raw['artifact_uri']).write_bytes(b'tampered source')
    with pytest.raises(PocketError, match='integrity'):
        promote_candidate(**args)


def test_plan_counts_context_tail_and_never_claims_listening(tmp_path):
    store, handle, _, _ = plan_fixture(tmp_path)
    plan = read_record(handle, store)
    assert plan["expected_frames"] == 56000  # 12 quarter notes / 2 + one second, at 8000 Hz.
    assert plan["settings"]["start_qn"] == {"n": 0, "d": 1}
    assert plan["settings"]["end_qn"] == {"n": 12, "d": 1}
    assert plan["listening"] == "not_reviewed"


def test_render_attachment_binds_exact_bytes_without_listening(tmp_path):
    args, parent = attachment_args(tmp_path)
    before = snapshot(parent.parent)
    result = attach_candidate_render(**args)
    assert result["status"] == "ok"
    assert result["coverage"]["human_listening"] == "not_reviewed"
    attachment = read_record(result["artifacts"]["attachment"], args["store_root"])
    assert attachment["musical_verdict"] is None
    assert attachment["audio"]["sha256"] == args["expected_sha256"]
    assert snapshot(parent.parent) == before
    assert attach_candidate_render(**args) == result


@pytest.mark.parametrize("signal,extra,disposition", [(0, 0, "unexpected_silence"),
    (1, 0, "sample_overload"), (.01, 3, "wrong_duration"), (float("nan"), 0, "nonfinite_audio")])
def test_failed_signal_preserved_but_not_promotable(tmp_path, signal, extra, disposition):
    args, _ = attachment_args(tmp_path, signal=signal, extra_frames=extra)
    result = attach_candidate_render(**args)
    assert result["status"] == "failed"
    assert result["coverage"]["promotable"] is False
    assert result["coverage"]["rendered_signal"] == disposition
    assert Path(args["store_root"], result["artifacts"]["audio"]["artifact_uri"]).exists()
    with pytest.raises(PocketError, match="Unusable"):
        promote_candidate(args["store_root"], "reject-bad-promotion", args["candidate"],
                          result["artifacts"]["attachment"],
                          {"action": "keep", "actor": "test", "actor_kind": "agent", "reason": "test"},
                          str(tmp_path / "must-not-publish"))
    assert not (tmp_path / "must-not-publish").exists()


def test_mismatched_actual_settings_and_incomplete_export_refused(tmp_path):
    args, _ = attachment_args(tmp_path)
    args["actual_settings"] = {**args["actual_settings"], "normalization": True}
    with pytest.raises(PocketError, match="settings"):
        attach_candidate_render(**args)
    args["request_id"] = "attach-incomplete"
    args["actual_settings"]["normalization"] = False
    args["native_report"]["export_completed"] = False
    with pytest.raises(PocketError, match="Completed"):
        attach_candidate_render(**args)


def test_feedback_exact_interval_and_independent_attribution(tmp_path):
    args, _ = attachment_args(tmp_path)
    attached = attach_candidate_render(**args)
    handle = attached["artifacts"]["attachment"]
    result = audition_feedback(args["store_root"], "feedback", handle, [0, 8000],
                               "Synthetic reviewer", "agent", "Technical test", "revise")
    record = read_record(result["artifacts"]["feedback"], args["store_root"])
    assert record["evidence_kind"] == "agent_report"
    assert record["decision"] == "revise"
    with pytest.raises(PocketError, match="outside"):
        audition_feedback(args["store_root"], "bad-feedback", handle, [0, 56001],
                           "Synthetic reviewer", "agent", "outside")


def test_promotion_preserves_sources_and_relocated_package_validates(tmp_path):
    args, parent = promotion_args(tmp_path)
    before = snapshot(parent.parent)
    result = promote_candidate(**args)
    assert result["coverage"]["decision"] == "agent_technical_keep"
    assert result["coverage"]["native_relocated_reopen"] == "not_verified"
    assert snapshot(parent.parent) == before
    moved = tmp_path / "relocated"
    shutil.move(args["output_dir"], moved)
    shutil.move(args["store_root"], tmp_path / "archived-store")
    shutil.move(parent.parent, tmp_path / "archived-source")
    checked = validate_candidate_promotion(str(moved), result["lineage_sha256"])
    assert checked["child_als_sha256"] == sha256_file(moved / "candidate.als")
    assert checked["native_relocated_reopen"] == "not_verified"


def test_destination_collision_and_fake_human_keep_refused(tmp_path):
    args, _ = promotion_args(tmp_path)
    Path(args["output_dir"]).mkdir()
    with pytest.raises(PocketError, match="exists"):
        promote_candidate(**args)
    Path(args["output_dir"]).rmdir()
    args["request_id"] = "fake-human"
    args["decision"]["actor_kind"] = "human"
    with pytest.raises(PocketError):
        promote_candidate(**args)
    assert not Path(args["output_dir"]).exists()


def test_changed_promoted_child_is_rejected(tmp_path):
    args, _ = promotion_args(tmp_path)
    result = promote_candidate(**args)
    Path(result["child_als"]).write_bytes(b"modified")
    with pytest.raises(PocketError, match="changed"):
        validate_candidate_promotion(args["output_dir"], result["lineage_sha256"])


def test_promotion_retry_reverifies_published_child(tmp_path):
    args, _ = promotion_args(tmp_path)
    result = promote_candidate(**args)
    Path(result["child_als"]).write_bytes(b"changed")
    with pytest.raises(PocketError, match="changed"):
        promote_candidate(**args)


def test_render_retry_rechecks_source_file(tmp_path):
    args, _ = attachment_args(tmp_path)
    attach_candidate_render(**args)
    Path(args["path"]).write_bytes(b"changed completed render")
    with pytest.raises(PocketError, match="changed"):
        attach_candidate_render(**args)


@pytest.mark.parametrize("consumer", ["promotion", "feedback"])
def test_external_attachment_cannot_forge_usable_audio_claim(tmp_path, consumer):
    args, _ = promotion_args(tmp_path)
    record = read_record(args["attachment"], args["store_root"])
    record["audio"] = put_bytes(b"not audio", args["store_root"], "fake.wav", "pocket.render-audio/v1")
    forged = put_record(record, args["store_root"])
    with pytest.raises(PocketError, match="decode"):
        if consumer == "promotion":
            promote_candidate(**{**args, "attachment": forged})
        else:
            audition_feedback(args["store_root"], "bad-feedback", forged, [0, 10],
                               "Synthetic", "agent", "A report cannot certify invented audio")
    assert not Path(args["output_dir"]).exists()


def test_external_plan_cannot_expand_duration_tolerance(tmp_path):
    args, _ = attachment_args(tmp_path)
    record = read_record(args["render_plan"], args["store_root"])
    record["frame_tolerance"] = 8000
    args["render_plan"] = put_record(record, args["store_root"])
    with pytest.raises(PocketError, match="fidelity"):
        attach_candidate_render(**args)
