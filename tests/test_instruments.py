# SPDX-License-Identifier: AGPL-3.0-only
import copy
import hashlib
import plistlib

import pytest

from pocket_music.artifact_store import put_record, read_record
from pocket_music.errors import PocketError
from pocket_music.instruments import (
    instrument_inspect,
    instrument_parameters,
    preset_catalog,
    serum_inspect,
    serum_plan,
    serum_presets,
    sound_plan,
)


def observation():
    return {
        "identity": {"manufacturer": "Test", "product": "serum2", "build": "test-fixture",
                     "format": "VST3", "class_id": "fixture-only", "instance_id": "one", "host_build": "fake",
                     "os_arch": "test", "binding": "test saved-state bytes"},
        "topology_token": "filter-a", "attribution": "automated fixture; not native evidence",
        "observed_at": "2026-09-16T00:00:00Z", "source_kind": "manual",
        "parameters": [{"parameter_id": "slot:0", "name": "Cutoff", "identity_quality": "host_slot_bound",
                        "native_min": 0.0, "native_max": 1.0, "value": 0.5, "unit": "normalized",
                        "quantized": False, "values": [], "writable": True, "automation_owner": "none",
                        "semantic_key": None, "mapping_evidence": None}],
    }


def state(tmp_path, value=None, request_id="inspect"):
    return instrument_inspect(store_root=str(tmp_path), request_id=request_id, scope="supplied_observation",
                              observation=observation() if value is None else value)


def plan_args(tmp_path, inspected):
    return {"store_root": str(tmp_path), "request_id": "plan", "brief": "test one bounded movement",
            "recipe": "parameter_alternatives", "state": inspected["artifacts"]["state"],
            "expected_layout_sha256": inspected["parameter_layout_sha256"], "expected_topology_token": "filter-a",
            "alternatives": [{"label": "A", "hypothesis": "Listener may prefer this",
                              "changes": [{"parameter_id": "slot:0", "value": 0.25}]}]}


def test_inventory_is_read_only_separates_fx_and_never_certifies(tmp_path):
    root = tmp_path / "plugins"
    for name in ["Serum 2", "Serum 2 FX", "Other"]:
        folder = root / (name + ".vst3") / "Contents"
        folder.mkdir(parents=True)
        (folder / "Info.plist").write_bytes(plistlib.dumps({"CFBundleName": name, "CFBundleShortVersionString": "test"}))
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("Info.plist")}
    inspected = instrument_inspect(store_root=str(tmp_path / "store"), request_id="inventory", plugin_roots=[str(root)], limit=1)
    assert inspected["total"] == 3
    assert inspected["next_offset"] == 1
    records = read_record(inspected["artifacts"]["inventory"], str(tmp_path / "store"))["items"]
    assert {r["product"] for r in records} == {"serum2", "serum_fx2", "unknown"}
    assert all(not action["available"] for r in records for action in r["capabilities"].values())
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}


def test_absence_is_scoped_and_serum_alias_uses_same_provider(tmp_path):
    args = {"store_root": str(tmp_path / "store"), "request_id": "inventory", "plugin_roots": [str(tmp_path / "absent")],
            "product": "serum2"}
    direct = instrument_inspect(**args)
    assert serum_inspect(**args) == direct
    assert direct["items"] == []
    assert direct["coverage"]["scope"] == "approved roots only"
    assert direct["coverage"]["native_verified"] is False


def test_supplied_observation_is_not_native_certification(tmp_path):
    result = state(tmp_path)
    queried = instrument_parameters(store_root=str(tmp_path), state=result["artifacts"]["state"])
    assert queried["items"][0]["name"] == "Cutoff"
    assert not queried["native_verified"] and not queried["complete_patch"]
    assert not queried["executable"]


@pytest.mark.parametrize("mutation", [
    lambda x: x["parameters"].append(copy.deepcopy(x["parameters"][0])),
    lambda x: x["parameters"][0].update(value=float("nan")),
    lambda x: x["parameters"][0].update(value=True),
    lambda x: x["parameters"][0].update(semantic_key="filter.cutoff"),
    lambda x: x.update(complete_patch=True),
    lambda x: x["identity"].update(format="assumed"),
])
def test_reject_malformed_or_overclaimed_observation(tmp_path, mutation):
    value = observation()
    mutation(value)
    with pytest.raises(PocketError):
        state(tmp_path, value)


def test_stock_sound_plan_without_midi_daw_or_serum(tmp_path):
    args = {"store_root": str(tmp_path), "request_id": "stock", "brief": "test starting sound"}
    result = sound_plan(**args)
    assert sound_plan(**args) == result
    record = read_record(result["artifacts"]["sound_plan"], str(tmp_path))
    assert record["recipe_settings"]["amp_envelope_ms"] == {"attack": 5, "decay": 180, "release": 60}
    assert record["recipe_settings"]["voices"] == 4
    assert record["performance"] is None
    assert record["executable"] is False
    assert record["musical_decision"] is None


def test_parameter_plan_preserves_exact_inputs_and_alias_equivalence(tmp_path):
    inspected = state(tmp_path)
    args = plan_args(tmp_path, inspected)
    direct = sound_plan(**args)
    args.pop("recipe")
    assert serum_plan(**args) == direct
    record = read_record(direct["artifacts"]["sound_plan"], str(tmp_path))
    assert record["realized_alternatives"][0]["changes"][0]["before"] == 0.5
    assert record["realized_alternatives"][0]["changes"][0]["unit"] == "normalized"
    assert record["baseline"] == "unchanged"


@pytest.mark.parametrize("change,match", [
    ({"expected_topology_token": "changed"}, "topology"),
    ({"expected_layout_sha256": "0" * 64}, "layout"),
    ({"locks": ["slot:0"]}, "lock"),
    ({"locks": ["hidden_matrix"]}, "unknown"),
    ({"alternatives": [{"label": "A", "hypothesis": "x", "changes": [{"parameter_id": "slot:0", "value": 2}]}]}, "domain"),
])
def test_plan_rejects_stale_bindings_locks_and_out_of_range(tmp_path, change, match):
    args = plan_args(tmp_path, state(tmp_path))
    args.update(change)
    with pytest.raises(PocketError, match=match):
        sound_plan(**args)


@pytest.mark.parametrize("owner", ["host_automation", "macro", "remote", "unknown"])
def test_ownership_refuses_static_plan_write(tmp_path, owner):
    value = observation()
    value["parameters"][0]["automation_owner"] = owner
    with pytest.raises(PocketError, match="ownership"):
        sound_plan(**plan_args(tmp_path, state(tmp_path, value)))


def test_catalog_hashes_opaque_bytes_and_is_idempotent(tmp_path):
    presets = tmp_path / "presets"
    presets.mkdir()
    a = presets / "Same.SerumPreset"
    b = presets / "Old.fxp"
    a.write_bytes(b"opaque fixture, not a real preset")
    b.write_bytes(b"other opaque fixture")
    before = a.stat().st_mtime_ns
    args = {"store_root": str(tmp_path / "store"), "request_id": "catalog", "operation": "scan", "roots": [str(presets)],
            "product": "serum2"}
    result = serum_presets(**args)
    assert result == preset_catalog(**args)
    assert len(result["items"]) == 2
    assert not result["items"][0]["format_verified"]
    assert a.stat().st_mtime_ns == before
    query = serum_presets(store_root=str(tmp_path / "store"), operation="query",
                          catalog=result["artifacts"]["catalog"], query="same")
    assert query["items"][0]["sha256"] == hashlib.sha256(a.read_bytes()).hexdigest()
    a.write_bytes(b"new version")
    assert serum_presets(**args) == result  # an immutable scan receipt, not a fresh scan
    newer = serum_presets(**{**args, "request_id": "catalog-new"})
    assert newer["artifacts"]["catalog"]["sha256"] != result["artifacts"]["catalog"]["sha256"]


def test_source_symlink_not_scanned(tmp_path):
    presets = tmp_path / "presets"
    presets.mkdir()
    outside = tmp_path / "outside.SerumPreset"
    outside.write_bytes(b"private")
    (presets / "link.SerumPreset").symlink_to(outside)
    result = serum_presets(store_root=str(tmp_path / "store"), request_id="scan", operation="scan", roots=[str(presets)])
    assert result["items"] == []


def test_tampered_layout_record_is_rejected(tmp_path):
    inspected = state(tmp_path)
    record = read_record(inspected["artifacts"]["state"], str(tmp_path))
    record["observation"]["parameters"][0]["name"] = "Another parameter"
    forged = put_record(record, str(tmp_path))
    with pytest.raises(PocketError, match="fingerprint"):
        instrument_parameters(store_root=str(tmp_path), state=forged)
