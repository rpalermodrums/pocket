"""Generated notes exercise retrieval identity, overlap and conflicting claims."""

from pathlib import Path

import pytest

from pocket_music.errors import PocketError
from pocket_music.feedback import query_feedback
from test_transition_lab import feedback, make_trial


def test_retrieval_preserves_conflicting_scoped_claims_and_exact_intervals(tmp_path):
    trial, _, _ = make_trial(tmp_path)
    feedback(trial, note="Fixture claim A", start_frame=10, end_frame=40, scope="bar_phase")
    feedback(trial, note="Fixture contradictory claim B", start_frame=20, end_frame=60, scope="bar_phase")
    feedback(trial, note="Fixture tone claim", scope="tonal_overlap")
    result = query_feedback(trial["trial_dir"], variant_id="v01", start_frame=39, end_frame=41,
                            scope="bar_phase")
    assert [n["note"] for n in result["notes"]] == ["Fixture claim A", "Fixture contradictory claim B"]
    assert [(n["start_frame"], n["end_frame"]) for n in result["notes"]] == [(10, 40), (20, 60)]
    assert result["musical_verdict"] is None
    assert len(query_feedback(trial["trial_dir"], start_frame=40, end_frame=41,
                              scope="bar_phase")["notes"]) == 1


def test_empty_and_paginated_queries(tmp_path):
    trial, _, _ = make_trial(tmp_path)
    assert query_feedback(trial["trial_dir"])["notes"] == []
    for index in range(3):
        feedback(trial, note=f"Fixture {index}")
    page = query_feedback(trial["trial_dir"], limit=2)
    assert page["pagination"] == {"total": 3, "offset": 0, "limit": 2, "next_offset": 2}
    assert query_feedback(trial["trial_dir"], offset=2)["notes"][0]["note"] == "Fixture 2"


@pytest.mark.parametrize("options", [
    {"variant_id": "unknown"}, {"output_sha256": "0" * 64}, {"start_frame": 0},
    {"start_frame": 10, "end_frame": 10}, {"start_frame": -1, "end_frame": 20},
    {"start_frame": 0, "end_frame": 3000}, {"limit": True}, {"offset": -1},
    {"limit": 101}, {"scope": "all"},
])
def test_invalid_identity_or_bounds_fail(tmp_path, options):
    trial, _, _ = make_trial(tmp_path)
    with pytest.raises(PocketError):
        query_feedback(trial["trial_dir"], **options)


def test_stale_audio_and_sealed_note_tampering_fail(tmp_path):
    trial, _, _ = make_trial(tmp_path)
    written = feedback(trial)
    path = Path(written["feedback_file"])
    original = path.read_bytes()
    path.write_bytes(original + b" ")
    with pytest.raises(PocketError, match="Manifest changed"):
        query_feedback(trial["trial_dir"])
    path.write_bytes(original)
    (Path(trial["trial_dir"]) / "v01.wav").write_bytes(b"changed")
    with pytest.raises(PocketError, match="identity changed"):
        query_feedback(trial["trial_dir"])


def test_cli_retrieval_uses_same_provider(tmp_path):
    import json
    from test_interfaces import _cli
    trial, _, _ = make_trial(tmp_path)
    feedback(trial)
    spec = tmp_path / "query.json"
    arguments = {"trial_dir": trial["trial_dir"], "variant_id": "v01"}
    spec.write_text(json.dumps(arguments))
    result = _cli("lab", "feedback-list", "--spec", spec)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == query_feedback(**arguments)
