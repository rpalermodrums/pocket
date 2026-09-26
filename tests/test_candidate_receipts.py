# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded seal responses retain complete immutable source evidence elsewhere."""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest
from test_candidate_runtime_identity import runtime_fixture
from test_native_candidates import prepare, seal_args
from test_native_midi import bridge as bridge  # noqa: PLC0414 - expose pytest fixture

from pocket_music.artifact_store import digest, put_record, read_bytes, read_record
from pocket_music.errors import PocketError
from pocket_music.native_candidates import (
    _preservation_summary,
    candidate_seal,
    load_candidate_record,
    validate_candidate,
)


def test_preservation_summary_is_bounded_independent_of_xml_detail():
    fields = [{"path": "Synthetic/Metadata/" + "x" * 300, "before": "0", "after": "1"}
              for _ in range(111)]
    preservation = {
        "protected_source_xml": "matched_after_checked_metadata", "normalization_profile": "fixture-only",
        "native_notes_matched": 3, "added_tracks": 1, "note_tolerance_qn": {"n": 0, "d": 1},
        "instrument_sound": "attributed_report_only",
        "normalization": {"changed_fields": 111, "changes": fields},
        "runtime_identity": {"changed_fields": 7, "changes": fields[:7],
                             "observation_source_preservation": {
                                 "metadata": {"changed_fields": 112, "changes": [*fields, fields[0]]}}},
    }
    before = copy.deepcopy(preservation)
    summary = _preservation_summary(preservation)
    assert summary["metadata_fields_normalized"] == 111
    assert summary["runtime_identity_fields_normalized"] == 7
    assert summary["observed_source_metadata_fields_normalized"] == 112
    assert summary["note_tolerance_qn"] == {"n": 0, "d": 1}
    assert summary["instrument_sound"] == "attributed_report_only"
    assert len(json.dumps(summary).encode()) < 1024
    assert preservation == before
    summary["note_tolerance_qn"]["n"] = 1
    assert preservation == before


def test_new_seal_and_journal_are_concise_without_replacing_trial(tmp_path):
    prepared, inputs, _ = prepare(tmp_path)
    args = seal_args(prepared, inputs["store_root"])
    result = candidate_seal(**args)
    trial = load_candidate_record(result["artifacts"]["candidate"], inputs["store_root"])
    journal = json.loads((Path(inputs["store_root"]) / "requests/seal/journal.json").read_bytes())
    assert journal["receipt"] == result
    assert len(json.dumps(result).encode()) < 3072
    assert result["change_summary"]["metadata_fields_normalized"] == 0
    assert result["change_summary"]["runtime_identity_fields_normalized"] == 0
    assert "metadata_fields_normalized" not in trial["preservation"]
    assert trial["listening"] == "not_reviewed"
    assert trial["decision"] is None


def test_complete_runtime_proof_stays_in_trial_with_bounded_receipt(tmp_path, bridge):
    fixture = runtime_fixture(tmp_path, bridge)
    result = candidate_seal(**fixture["args"])
    trial = load_candidate_record(result["artifacts"]["candidate"], fixture["store"])
    assert len(json.dumps(result).encode()) < 3072
    assert result["change_summary"]["runtime_identity_fields_normalized"] == 7
    assert len(trial["preservation"]["runtime_identity"]["changes"]) == 7
    assert trial["preservation"]["runtime_identity"]["observation_source_preservation"]["metadata"]
    assert candidate_seal(**fixture["args"]) == result
    public = validate_candidate(result["artifacts"]["candidate"], fixture["store"])
    assert len(json.dumps(public).encode()) < 3072
    assert public["artifacts"] == result["artifacts"]
    assert public["change_summary"] == result["change_summary"]
    assert public["coverage"] == {
        "artifact_integrity": "verified", "native_verification": "attributed_save_reopen",
        "provider_native_observation": False, "rendered_audio": "not_evaluated_by_candidate_validation",
        "human_listening": "not_reviewed", "musical_decision": None,
        "portability": "editable_same_environment", "relocated_native_reopen": "not_verified"}


def test_historical_expanded_replay_keeps_journal_trial_and_qualifications(tmp_path, bridge):
    fixture = runtime_fixture(tmp_path, bridge)
    result = candidate_seal(**fixture["args"])
    trial = load_candidate_record(result["artifacts"]["candidate"], fixture["store"])
    path = Path(fixture["store"]) / "requests/seal-runtime/journal.json"
    journal = json.loads(path.read_bytes())
    journal["receipt"]["change_summary"] = trial["preservation"]
    journal["receipt"]["warnings"] = ["Synthetic historical warning; retain it"]
    journal["receipt"]["uncertainty"] = ["Synthetic historical uncertainty; retain it"]
    journal["receipt_sha256"] = digest(journal["receipt"])
    path.write_text(json.dumps(journal) + "\n")
    historical = path.read_bytes()
    trial_bytes = read_bytes(result["artifacts"]["candidate"], fixture["store"])
    replay = candidate_seal(**fixture["args"])
    assert len(json.dumps(replay).encode()) < 3072
    assert replay["change_summary"] == result["change_summary"]
    for key in journal["receipt"]:
        if key != "change_summary":
            assert replay[key] == journal["receipt"][key]
    assert path.read_bytes() == historical
    assert read_bytes(result["artifacts"]["candidate"], fixture["store"]) == trial_bytes
    assert load_candidate_record(result["artifacts"]["candidate"], fixture["store"]) == trial
    assert candidate_seal(**fixture["args"]) == replay


@pytest.mark.parametrize(("field", "value"), [
    ("listening", "approved"), ("decision", {"action": "keep"}),
    ("provider_native_observation", 0), ("native_verification", "provider_certified"),
    ("relocated_native_reopen", "verified"), ("portability", "all_environments"),
])
def test_public_validation_rejects_forged_qualification_claims(tmp_path, field, value):
    prepared, inputs, _ = prepare(tmp_path)
    result = candidate_seal(**seal_args(prepared, inputs["store_root"]))
    trial = load_candidate_record(result["artifacts"]["candidate"], inputs["store_root"])
    trial[field] = value
    forged = put_record(trial, inputs["store_root"])
    with pytest.raises(PocketError, match="qualifications"):
        validate_candidate(forged, inputs["store_root"])


@pytest.mark.parametrize("role", ["candidate_als", "parent", "prepared_als", "dependency",
                                 "material", "context", "instrument_state"])
def test_fixed_candidate_artifact_roles_refuse_opaque_family_substitution(tmp_path, role):
    prepared, inputs, _ = prepare(tmp_path)
    result = candidate_seal(**seal_args(prepared, inputs["store_root"]))
    trial = load_candidate_record(result["artifacts"]["candidate"], inputs["store_root"])
    preparation = read_record(trial["preparation"], inputs["store_root"])
    destination = tmp_path / "relocated"
    shutil.copytree(Path(inputs["store_root"]) / "artifacts", destination / "artifacts")
    false_handle = {**trial["candidate_als"], "artifact_schema": "qa.false-family/v1"}
    if role == "prepared_als":
        preparation[role] = false_handle
    elif role == "dependency":
        preparation["dependencies"][0]["artifact"]["artifact_schema"] = "qa.false-family/v1"
        trial["dependencies"] = copy.deepcopy(preparation["dependencies"])
    else:
        trial[role] = false_handle
        if role in {"parent", "material", "context"}:
            preparation[role] = false_handle
    trial["preparation"] = put_record(preparation, destination)
    forged = put_record(trial, destination)
    with pytest.raises(PocketError, match="requires pocket"):
        validate_candidate(forged, str(destination))
