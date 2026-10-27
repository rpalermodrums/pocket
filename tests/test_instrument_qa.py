# SPDX-License-Identifier: AGPL-3.0-only
"""Independent adversarial QA: no actual plugin, DAW or private preset files."""
import copy
import plistlib

import pytest

from pocket_music.artifact_store import put_record, read_record
from pocket_music.curves import curve_transform
from pocket_music.errors import PocketError
from pocket_music.instruments import instrument_inspect, instrument_parameters, preset_catalog, sound_plan


def observed():
    descriptor = {"parameter_id": "native:filter-a", "name": "Cutoff", "identity_quality": "native_id",
                  "native_min": 20, "native_max": 20000, "value": 1000, "unit": "Hz", "quantized": False,
                  "values": [], "writable": True, "automation_owner": "none", "semantic_key": None,
                  "mapping_evidence": None}
    return {"identity": {"manufacturer": "QA", "product": "serum2", "build": "qa-only", "format": "VST3",
                         "class_id": "qa-class", "instance_id": "qa-one", "host_build": "qa-host", "os_arch": "qa",
                         "binding": "qa-saved-instance"}, "topology_token": "layout-A",
            "parameters": [descriptor, {**descriptor, "parameter_id": "native:filter-b", "value": 2000}],
            "attribution": "Independent synthetic QA; not native evidence",
            "observed_at": "2026-09-16T00:00:00+00:00", "source_kind": "manual"}


def inspected(tmp_path, observation=None, request="inspect"):
    return instrument_inspect(store_root=str(tmp_path), request_id=request, scope="supplied_observation",
                              observation=observation or observed())


def cc_target():
    return {"kind": "cc", "target_id": "cc:11", "scope": "channel", "channel": 1, "unit": "midi1_7bit",
            "value_min": 0, "value_max": 127, "quantized": True, "values": [], "ownership": "none",
            "value_mode": "absolute"}


def test_duplicate_parameter_names_keep_distinct_identity_and_reordering_invalidates_layout(tmp_path):
    first = inspected(tmp_path)
    observation = observed()
    observation["parameters"].reverse()
    second = inspected(tmp_path, observation, "reordered")
    parameters = instrument_parameters(store_root=str(tmp_path), state=first["artifacts"]["state"])
    assert [p["name"] for p in parameters["items"]] == ["Cutoff", "Cutoff"]
    assert len({p["parameter_id"] for p in parameters["items"]}) == 2
    assert first["parameter_layout_sha256"] != second["parameter_layout_sha256"]
    with pytest.raises(PocketError, match="layout"):
        sound_plan(store_root=str(tmp_path), request_id="stale", brief="stale layout test",
                   recipe="parameter_alternatives", state=second["artifacts"]["state"],
                   expected_layout_sha256=first["parameter_layout_sha256"], expected_topology_token="layout-A",
                   alternatives=[{"label": "A", "hypothesis": "none", "changes": [
                       {"parameter_id": "native:filter-a", "value": 900}]}])


def test_sound_plan_preserves_native_units_and_leaves_other_same_name_parameter_alone(tmp_path):
    first = inspected(tmp_path)
    before = read_record(first["artifacts"]["state"], str(tmp_path))
    result = sound_plan(store_root=str(tmp_path), request_id="units", brief="test exact Hz",
                        recipe="parameter_alternatives", state=first["artifacts"]["state"],
                        expected_layout_sha256=first["parameter_layout_sha256"], expected_topology_token="layout-A",
                        locks=["native:filter-b"], alternatives=[{"label": "A", "hypothesis": "none", "changes": [
                            {"parameter_id": "native:filter-a", "value": 900}]}])
    plan = read_record(result["artifacts"]["sound_plan"], str(tmp_path))
    assert plan["realized_alternatives"][0]["changes"] == [{"parameter_id": "native:filter-a", "before": 1000,
        "after": 900, "unit": "Hz", "value_domain": "native", "semantic_key": None, "mapping_evidence": None}]
    assert read_record(first["artifacts"]["state"], str(tmp_path)) == before
    assert plan["executable"] is False


def test_equal_tick_controller_steps_survive_shift_without_channel_change(tmp_path):
    points = [{"time": {"n": 1, "d": 3}, "value": 25, "order": 0},
              {"time": {"n": 1, "d": 3}, "value": 90, "order": 1}]
    original = copy.deepcopy(points)
    result = curve_transform(store_root=str(tmp_path), request_id="cc", target=cc_target(), operations=[
        {"op": "create", "space": "clip_qn", "interpolation": "step", "points": points},
        {"op": "shift", "amount": {"n": 1, "d": 6}}])
    curve = read_record(result["artifacts"]["curve"], str(tmp_path))
    assert [p["time"] for p in curve["points"]] == [{"n": 1, "d": 2}, {"n": 1, "d": 2}]
    assert [p["value"] for p in curve["points"]] == [25, 90]
    assert [p["order"] for p in curve["points"]] == [0, 1]
    assert curve["target"]["channel"] == 1 and curve["executable"] is False
    assert points == original


@pytest.mark.parametrize("provider", ["sound", "curve"])
def test_context_handle_requires_context_schema(tmp_path, provider):
    wrong = put_record({"schema": "pocket.unrelated-qa/v1", "test": "not a musical context"}, str(tmp_path))
    with pytest.raises(PocketError, match="context"):
        if provider == "sound":
            sound_plan(store_root=str(tmp_path), request_id="wrong-context", brief="bad context", context=wrong)
        else:
            curve_transform(store_root=str(tmp_path), request_id="wrong-context", target=cc_target(), context=wrong,
                            operations=[{"op": "create", "space": "clip_qn", "interpolation": "step",
                                         "points": [{"time": {"n": 0, "d": 1}, "value": 64, "order": 0}]}])


def test_performance_handle_requires_valid_material(tmp_path):
    wrong = put_record({"schema": "pocket.unrelated-qa/v1", "test": "not notes"}, str(tmp_path))
    with pytest.raises(PocketError, match="material|performance"):
        sound_plan(store_root=str(tmp_path), request_id="wrong-performance", brief="bad performance", performance=wrong)


def test_bundle_metadata_symlink_cannot_read_outside_approved_root(tmp_path):
    root = tmp_path / "plugins"
    contents = root / "Serum 2.vst3/Contents"
    contents.mkdir(parents=True)
    private = tmp_path / "outside.plist"
    private.write_bytes(plistlib.dumps({"CFBundleName": "Serum 2", "CFBundleShortVersionString": "outside-root"}))
    (contents / "Info.plist").symlink_to(private)
    try:
        result = instrument_inspect(store_root=str(tmp_path / "store"), request_id="inventory",
                                    plugin_roots=[str(root)])
    except PocketError:
        return
    assert result["items"] == [], "Approved-root inventory must not read symlinked external metadata"


def test_malformed_external_catalog_is_not_accepted_as_valid_index(tmp_path):
    malformed = put_record({"schema": "pocket.preset-catalog/v1", "entries": [
        {"filename": "NotActuallyHashed.SerumPreset", "compatible_products_documented": ["serum2"]}]}, str(tmp_path))
    with pytest.raises(PocketError):
        preset_catalog(store_root=str(tmp_path), operation="query", catalog=malformed, product="serum2")


def test_external_instrument_state_cannot_claim_native_coverage(tmp_path):
    first = inspected(tmp_path)
    record = read_record(first["artifacts"]["state"], str(tmp_path))
    record["coverage"]["native_restore"] = "verified"
    record["capabilities"]["parameter_write"].update(available=True, status="available")
    forged = put_record(record, str(tmp_path))
    with pytest.raises(PocketError):
        instrument_parameters(store_root=str(tmp_path), state=forged)
