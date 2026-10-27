# SPDX-License-Identifier: AGPL-3.0-only
import copy

import pytest

from pocket_music.artifact_store import read_record
from pocket_music.curves import curve_transform
from pocket_music.errors import PocketError


def target():
    return {"kind": "macro", "target_id": "instance:a/slot:1", "scope": "instance", "unit": "normalized",
            "value_min": 0, "value_max": 1, "quantized": False, "values": [], "ownership": "none",
            "value_mode": "absolute", "parameter_layout_sha256": "a" * 64}


def point(n, value, d=1, order=0):
    return {"time": {"n": n, "d": d}, "value": value, "order": order}


def create(tmp_path, **kwargs):
    args = {"store_root": str(tmp_path), "request_id": "create", "target": target(),
            "operations": [{"op": "create", "space": "phrase_qn", "interpolation": "linear",
                            "points": [point(0, 0.1), point(1, 0.3, 3), point(1, 0.7)]}]}
    args.update(kwargs)
    return curve_transform(**args)


def test_rational_time_exact_transform_immutable_source(tmp_path):
    result = create(tmp_path)
    source = result["artifacts"]["curve"]
    before = read_record(source, str(tmp_path))
    edited = curve_transform(store_root=str(tmp_path), request_id="shift", curve=source,
                             operations=[{"op": "shift", "amount": {"n": 1, "d": 7}},
                                         {"op": "scale_time", "factor": {"n": 3, "d": 2}}])
    record = read_record(edited["artifacts"]["curve"], str(tmp_path))
    assert record["points"][1]["time"] == {"n": 5, "d": 7}
    assert read_record(source, str(tmp_path)) == before
    assert not record["executable"]
    assert record["parent"] == source


def test_same_request_same_bytes_and_changed_request_refused(tmp_path):
    result = create(tmp_path)
    assert create(tmp_path) == result
    with pytest.raises(PocketError, match="idempotency"):
        create(tmp_path, operations=[{"op": "create", "space": "phrase_qn", "interpolation": "linear",
                                     "points": [point(0, 0.9)]}])


@pytest.mark.parametrize("points", [
    [point(0, 0.1), point(0, 0.2, order=1)],
    [point(1, 0.1), point(0, 0.2)],
    [point(0, float("nan"))],
    [point(2, 0.1, 4)],
    [point(0, True)],
])
def test_malformed_or_ambiguous_points_rejected(tmp_path, points):
    with pytest.raises(PocketError):
        create(tmp_path, operations=[{"op": "create", "space": "phrase_qn", "interpolation": "linear", "points": points}])


def test_explicit_steps_preserve_controller_order_and_reject_implicit_smoothing(tmp_path):
    points = [point(0, 0.1), point(1, 0.1, order=0), point(1, 0.9, order=1), point(2, 0.9)]
    result = create(tmp_path, operations=[{"op": "create", "space": "phrase_qn", "interpolation": "step", "points": points}])
    assert read_record(result["artifacts"]["curve"], str(tmp_path))["points"] == points
    with pytest.raises(PocketError, match="discontinuity"):
        curve_transform(store_root=str(tmp_path), request_id="smooth", curve=result["artifacts"]["curve"],
                        operations=[{"op": "smooth", "window": 3}])


def test_simplify_measures_error_in_target_unit(tmp_path):
    points = [point(0, 0), point(1, 0.5), point(2, 1)]
    result = create(tmp_path, operations=[{"op": "create", "space": "phrase_qn", "interpolation": "linear", "points": points},
                                         {"op": "simplify", "tolerance": 0.001}])
    record = read_record(result["artifacts"]["curve"], str(tmp_path))
    assert record["points"] == [points[0], points[-1]]
    assert result["approximation_max_value_error"] == 0


def test_no_silent_clamping_or_retargeting(tmp_path):
    result = create(tmp_path)
    with pytest.raises(PocketError, match="domain"):
        curve_transform(store_root=str(tmp_path), request_id="offset", curve=result["artifacts"]["curve"],
                        operations=[{"op": "offset", "amount": 0.5}])
    with pytest.raises(PocketError, match="target"):
        curve_transform(store_root=str(tmp_path), request_id="retarget", curve=result["artifacts"]["curve"], target=target(),
                        operations=[{"op": "offset", "amount": 0.1}])


def test_quantized_target_requires_steps_and_valid_values(tmp_path):
    enum = {**target(), "quantized": True, "values": [0, 0.5, 1]}
    with pytest.raises(PocketError, match="step"):
        create(tmp_path, target=enum, operations=[{"op": "create", "space": "phrase_qn", "interpolation": "linear",
                                                  "points": [point(0, 0), point(1, 0.5)]}])
    with pytest.raises(PocketError, match="quantized"):
        create(tmp_path, request_id="invalidenum", target=enum, operations=[{"op": "create", "space": "phrase_qn",
                                               "interpolation": "step", "points": [point(0, 0.3)]}])


def test_owner_conflict_requires_explicit_offline_override(tmp_path):
    owned = {**target(), "ownership": "host_automation"}
    with pytest.raises(PocketError, match="ownership"):
        create(tmp_path, target=owned)
    result = create(tmp_path, request_id="override", target=owned, conflict_policy="plan_override")
    assert result["executable"] is False
    assert result["warnings"]


def test_per_note_expression_retains_note_and_resize_policy(tmp_path):
    note_target = {key: value for key, value in target().items() if key != "parameter_layout_sha256"}
    note_target.update(kind="per_note_pressure", scope="note", note_id="uuid:note-a", resize_policy="stretch_with_gate")
    result = create(tmp_path, target=note_target, operations=[{"op": "create", "space": "note_relative_qn",
                                                            "interpolation": "linear", "points": [point(0, 0), point(1, 1)]}])
    record = read_record(result["artifacts"]["curve"], str(tmp_path))
    assert record["target"]["note_id"] == "uuid:note-a"
    assert record["target"]["resize_policy"] == "stretch_with_gate"


def test_splice_is_bounded_and_unknown_operation_fields_rejected(tmp_path):
    result = create(tmp_path)
    changed = curve_transform(store_root=str(tmp_path), request_id="splice", curve=result["artifacts"]["curve"],
                              operations=[{"op": "splice", "start": {"n": 1, "d": 3}, "end": {"n": 1, "d": 1},
                                           "points": [point(1, 0.2, 2), point(1, 0.4)]}])
    points = read_record(changed["artifacts"]["curve"], str(tmp_path))["points"]
    assert points == [point(0, 0.1), point(1, 0.2, 2), point(1, 0.4)]
    op = {"op": "offset", "amount": 0.1, "unrelated": "refuse"}
    with pytest.raises(PocketError, match="unknown"):
        curve_transform(store_root=str(tmp_path), request_id="unknown", curve=result["artifacts"]["curve"], operations=[op])


def test_curve_arguments_not_mutated(tmp_path):
    curve_target = target()
    before = copy.deepcopy(curve_target)
    create(tmp_path, target=curve_target)
    assert curve_target == before
