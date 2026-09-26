# SPDX-License-Identifier: AGPL-3.0-only
"""File-only instrument discovery and explicit, non-executable sound plans.

No function in this module connects to a DAW, loads a plugin, or changes presets.
Supplied observations are useful planning inputs but never native certification.
"""
from __future__ import annotations

import copy
import hashlib
import os
import platform
import plistlib
import re
from pathlib import Path
from typing import Literal

from ..artifact_store import ArtifactHandle, digest, put_record, read_bytes, read_record, receipt, run_request
from ..errors import PocketError
from ..instrument_types import InstrumentObservation, SoundAlternative
from ._validation import boolean, choice, domain, fields, integer, page, text

PRODUCTS = {"serum1", "serum2", "serum_fx1", "serum_fx2", "operator", "unknown"}
FORMATS = {".vst3": "VST3", ".component": "AU", ".vst": "VST2", ".aaxplugin": "AAX"}
OWNERS = {"none", "host_automation", "macro", "remote", "unknown"}
ACTIONS = ("parameter_read", "parameter_write", "preset_load", "preset_save", "state_restore",
           "mpe_receive", "fx_automation_expose", "modulation_inspect", "asset_collect")
STOCK_RECIPE = {
    "recipe_id": "operator_sine_pluck/v1", "product": "operator", "format": "native",
    "oscillator_a": {"enabled": True, "waveform": "sine", "key_tracking": True},
    "other_oscillators": "disabled; no FM contribution", "filter": "disabled", "lfo": "disabled",
    "pitch_modulation": "disabled", "amp_envelope_ms": {"attack": 5, "decay": 180, "release": 60},
    "sustain": "silence", "envelope_loop": False, "voices": 4, "effects": [],
    "native_readback_required": True,
    "source": "https://www.ableton.com/en/manual/live-instrument-reference/#operator",
}


def _product(name: str) -> str:
    normalized = name.lower().replace(" ", "").replace("-", "").replace("_", "")
    if "serum" not in normalized:
        return "unknown"
    generation = "2" if "serum2" in normalized or "serumfx2" in normalized else "1"
    return "serum_fx" + generation if "fx" in normalized else "serum" + generation


def _roots(roots, defaults):
    roots = defaults if roots is None else roots
    if not isinstance(roots, list) or not 1 <= len(roots) <= 16:
        raise PocketError("Provide 1–16 approved search roots")
    result = []
    for root in roots:
        path = Path(text(root, "search root", 4096)).expanduser().absolute()
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise PocketError("Search roots must not use symlinks")
        if path not in result:
            result.append(path)
    return result


def _walk(root, *, bundles=False, max_entries=20000):
    if not root.exists():
        return []
    if not root.is_dir():
        raise PocketError("Search root must be a directory")
    found, count = [], 0

    def fail(error):
        raise PocketError(f"Search incomplete: {error}") from error

    for folder, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        count += len(dirs) + len(files)
        if count > max_entries:
            raise PocketError("Search exceeds 20000 entries; use narrower approved roots")
        paths = [Path(folder) / name for name in sorted(dirs + files)]
        found.extend(path for path in paths if not path.is_symlink()
                     and (path.suffix.lower() in FORMATS if bundles else path.is_file()))
        dirs[:] = sorted(name for name in dirs if not (Path(folder) / name).is_symlink()
                         and (Path(folder) / name).suffix.lower() not in FORMATS)
    return found


def _capabilities(product):
    return {action: {"status": "documented_unprobed" if product in PRODUCTS - {"unknown"} else "unknown",
                     "available": False, "probe_receipt": None,
                     "reason": "No qualified native route is registered"} for action in ACTIONS}


def _validate_observation(value, store_root):
    fields(value, ("identity", "topology_token", "parameters", "attribution", "observed_at", "source_kind"),
           ("opaque_state", "dependencies"), "instrument observation")
    identity = value["identity"]
    fields(identity, ("manufacturer", "product", "build", "format", "class_id", "instance_id", "host_build",
                      "os_arch", "binding"), label="instrument identity")
    for key, item in identity.items():
        text(item, key)
    choice(identity["format"], {*FORMATS.values(), "native", "unknown"}, "plugin format")
    for key in ("topology_token", "attribution", "observed_at"):
        text(value[key], key)
    choice(value["source_kind"], {"saved", "live", "manual"}, "observation source")
    parameters = value["parameters"]
    if not isinstance(parameters, list) or len(parameters) > 4096:
        raise PocketError("Expected at most 4096 parameter descriptors")
    seen = set()
    for descriptor in parameters:
        fields(descriptor, ("parameter_id", "name", "identity_quality", "native_min", "native_max", "value",
                            "unit", "quantized", "values", "writable", "automation_owner", "semantic_key",
                            "mapping_evidence"), ("display_value",), "parameter descriptor")
        for key in ("parameter_id", "name", "unit"):
            text(descriptor[key], key)
        if descriptor["parameter_id"] in seen:
            raise PocketError("Ambiguous duplicate parameter identity")
        seen.add(descriptor["parameter_id"])
        choice(descriptor["identity_quality"], {"native_id", "host_slot_bound"}, "identity quality")
        choice(descriptor["automation_owner"], OWNERS, "automation owner")
        boolean(descriptor["writable"], "writable")
        domain(descriptor["value"], descriptor["native_min"], descriptor["native_max"], descriptor["quantized"],
               descriptor["values"], "parameter value")
        for key in ("semantic_key", "mapping_evidence", "display_value"):
            if descriptor.get(key) is not None:
                text(descriptor[key], key)
        if descriptor["semantic_key"] and not descriptor["mapping_evidence"]:
            raise PocketError("Semantic keys require attributed mapping evidence")
    if value.get("opaque_state") is not None:
        read_bytes(value["opaque_state"], store_root)
    if not isinstance(value.get("dependencies", []), list) or len(value.get("dependencies", [])) > 1000:
        raise PocketError("dependencies must contain at most 1000 artifact handles")
    for handle in value.get("dependencies", []):
        read_bytes(handle, store_root)


def _layout(observation):
    # Values/display text may vary without changing the layout; topology never may.
    descriptors = [{key: item for key, item in parameter.items() if key not in {"value", "display_value"}}
                   for parameter in observation["parameters"]]
    return digest({"identity": observation["identity"], "topology_token": observation["topology_token"],
                   "parameters": descriptors})


def _state(handle, store_root):
    state = read_record(handle, store_root, "pocket.instrument-state/v1")
    fields(state, ("schema", "observation", "parameter_layout_sha256", "native_verified", "complete_patch",
                   "capabilities", "coverage"), label="instrument state")
    _validate_observation(state.get("observation"), store_root)
    if state.get("parameter_layout_sha256") != _layout(state["observation"]):
        raise PocketError("Parameter layout fingerprint mismatch")
    if state.get("native_verified") is not False or state.get("complete_patch") is not False:
        raise PocketError("This provider accepts only explicitly unverified, incomplete supplied observations")
    if state.get("capabilities") != _capabilities(state["observation"]["identity"]["product"]):
        raise PocketError("Supplied instrument state cannot enable unqualified native capabilities")
    if state.get("coverage") != _coverage(state["observation"]):
        raise PocketError("Supplied instrument state cannot claim native coverage")
    return state


def _coverage(observation):
    return {"exposed_parameters": "supplied_unverified", "opaque_state": "supplied" if
            observation.get("opaque_state") else "absent", "internal_modulation": "unknown",
            "asset_completeness": "unknown", "native_restore": "unverified"}


def instrument_inspect(*, store_root: str, request_id: str,
                       scope: Literal["installed", "supplied_observation"] = "installed",
                       plugin_roots: list[str] | None = None, product: str = "all",
                       observation: InstrumentObservation | None = None,
                       limit: int = 50, offset: int = 0) -> dict:
    """Inventory bundles or validate an attributed observation, without native actions."""
    page(limit, offset)
    choice(scope, {"installed", "supplied_observation"}, "inspection scope")
    if product != "all":
        choice(product, PRODUCTS, "product filter")
    inputs = {"scope": scope, "plugin_roots": plugin_roots, "product": product, "observation": observation,
              "limit": limit, "offset": offset}

    def work():
        if scope == "supplied_observation":
            if plugin_roots is not None:
                raise PocketError("plugin_roots are only valid for installed inventory")
            _validate_observation(observation, store_root)
            if product != "all" and observation["identity"]["product"] != product:
                raise PocketError("Observation product does not match requested product")
            state = {"schema": "pocket.instrument-state/v1", "observation": copy.deepcopy(observation),
                     "parameter_layout_sha256": _layout(observation), "native_verified": False,
                     "complete_patch": False, "capabilities": _capabilities(observation["identity"]["product"]),
                     "coverage": _coverage(observation)}
            handle = put_record(state, store_root)
            return receipt(request_id, artifacts={"state": handle}, coverage=state["coverage"],
                           native_verified=False, complete_patch=False, executable=False,
                           parameter_layout_sha256=state["parameter_layout_sha256"],
                           warnings=["Supplied observations are not fresh native reads or capability certification"])
        if observation is not None:
            raise PocketError("observation is only valid for supplied_observation scope")
        roots = _roots(plugin_roots, ["/Library/Audio/Plug-Ins", "~/Library/Audio/Plug-Ins",
                                      "/Library/Application Support/Avid/Audio/Plug-Ins"])
        items, errors = [], []
        for root_index, root in enumerate(roots):
            for bundle in _walk(root, bundles=True):
                plist = bundle / "Contents/Info.plist"
                try:
                    if any(part.is_symlink() for part in (plist, *plist.parents)):
                        raise PocketError("Bundle metadata must not follow symlinks")
                    with plist.open("rb") as stream:
                        payload = stream.read(1024 * 1024 + 1)
                    if len(payload) > 1024 * 1024:
                        raise PocketError("Bundle metadata exceeds 1 MiB")
                    metadata = plistlib.loads(payload)
                except (OSError, ValueError, plistlib.InvalidFileException) as error:
                    errors.append({"root_index": root_index, "bundle": bundle.name, "error": str(error)[:200]})
                    continue
                name = str(metadata.get("CFBundleName", bundle.stem))
                found_product = _product(name + " " + bundle.stem)
                if product != "all" and found_product != product:
                    continue
                items.append({"product": found_product, "display_name": name,
                              "build": str(metadata.get("CFBundleShortVersionString", "unknown")),
                              "bundle_id": str(metadata.get("CFBundleIdentifier", "unknown")),
                              "format": FORMATS[bundle.suffix.lower()], "metadata_sha256": hashlib.sha256(payload).hexdigest(),
                              "root_index": root_index, "relative_path": str(bundle.relative_to(root)),
                              "identity_evidence": "bundle_metadata_only", "loaded_instance": "unknown",
                              "license_readiness": "unknown", "capabilities": _capabilities(found_product)})
        environment = put_record({"schema": "pocket.environment/v1", "os_arch": platform.platform(),
                                  "search_roots": [{"path": str(root), "exists": root.exists()} for root in roots]}, store_root)
        inventory = put_record({"schema": "pocket.instrument-inventory/v1", "items": items,
                                "environment": environment, "errors": errors, "complete_in_roots": not errors}, store_root)
        return receipt(request_id, artifacts={"inventory": inventory, "environment": environment},
                       items=items[offset:offset + limit], total=len(items),
                       next_offset=offset + limit if offset + limit < len(items) else None,
                       coverage={"scope": "approved roots only", "loaded_plugin": "unknown", "native_verified": False},
                       warnings=["No plugin was instantiated; custom locations outside roots remain unknown"],
                       errors=errors[:100], executable=False)

    return run_request(store_root, request_id, "instrument_inspect", inputs, work)


def instrument_parameters(*, store_root: str, state: ArtifactHandle, limit: int = 50, offset: int = 0) -> dict:
    """Query an explicit stored observation; this is not a fresh live read."""
    page(limit, offset)
    record = _state(state, store_root)
    parameters = record["observation"]["parameters"]
    return receipt(artifacts={"state": state}, items=parameters[offset:offset + limit], total=len(parameters),
                   next_offset=offset + limit if offset + limit < len(parameters) else None,
                   parameter_layout_sha256=record["parameter_layout_sha256"],
                   identity=record["observation"]["identity"], topology_token=record["observation"]["topology_token"],
                   coverage=record["coverage"], native_verified=False, complete_patch=False, executable=False)


def preset_catalog(*, store_root: str, operation: Literal["scan", "query"], roots: list[str] | None = None,
                   catalog: ArtifactHandle | None = None, product: str = "all", query: str = "",
                   limit: int = 50, offset: int = 0, request_id: str | None = None) -> dict:
    """Index opaque local presets or query an immutable catalog; never load or alter them."""
    page(limit, offset)
    choice(operation, {"scan", "query"}, "catalog operation")
    choice(product, {"all", *PRODUCTS}, "product filter")
    if not isinstance(query, str) or len(query) > 512:
        raise PocketError("query must be text of at most 512 characters")

    def result(handle, record):
        matches = [entry for entry in record["entries"] if query.casefold() in entry["filename"].casefold()
                   and (product == "all" or product in entry["compatible_products_documented"])]
        return receipt(request_id, artifacts={"catalog": handle}, items=matches[offset:offset + limit],
                       total=len(matches), next_offset=offset + limit if offset + limit < len(matches) else None,
                       coverage={"preset_bytes": "opaque", "dependencies": "unknown", "loadability": "unverified"},
                       warnings=["Extensions and filenames are discovery hints, not validated preset formats"], executable=False)

    if operation == "query":
        if roots is not None or catalog is None:
            raise PocketError("query requires catalog and no roots")
        record = read_record(catalog, store_root, "pocket.preset-catalog/v1")
        _validate_catalog(record, store_root)
        return result(catalog, record)
    if catalog is not None:
        raise PocketError("scan accepts roots, not a prior catalog")
    approved_roots = _roots(roots, ["/Library/Audio/Presets/Xfer Records", "~/Library/Audio/Presets/Xfer Records"])

    def work():
        entries = []
        for root_index, root in enumerate(approved_roots):
            for path in _walk(root):
                extension = path.suffix.lower()
                if extension not in {".fxp", ".serumpreset", ".adv", ".adg"}:
                    continue
                before = path.stat()
                if before.st_size > 128 * 1024 * 1024 or len(entries) >= 5000:
                    raise PocketError("Catalog bound exceeded; use narrower roots or smaller preset files")
                sha = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        sha.update(chunk)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                    raise PocketError("Preset changed during catalog scan")
                compatible = ["serum2"] if extension == ".serumpreset" else []
                if extension == ".fxp":
                    compatible = ["serum1", "serum2"]
                entries.append({"filename": path.name, "relative_path": str(path.relative_to(root)),
                                "root_index": root_index, "sha256": sha.hexdigest(), "size_bytes": after.st_size,
                                "format_hint": extension, "format_verified": False,
                                "compatible_products_documented": compatible,
                                "compatibility_condition": "Only if this is a valid Serum preset of the hinted generation",
                                "asset_coverage": "unknown", "preview": None, "annotations": []})
        environment = put_record({"schema": "pocket.environment/v1",
                                  "search_roots": [{"path": str(root), "exists": root.exists()} for root in approved_roots]}, store_root)
        record = {"schema": "pocket.preset-catalog/v1", "entries": entries, "environment": environment,
                  "byte_policy": "read_only_source; no private format parsing"}
        return result(put_record(record, store_root), record)

    return run_request(store_root, request_id, "preset_catalog", {"roots": [str(root) for root in approved_roots],
                       "product": product, "query": query, "limit": limit, "offset": offset}, work)


def _validate_catalog(record, store_root):
    fields(record, ("schema", "entries", "environment", "byte_policy"), label="preset catalog")
    environment = read_record(record["environment"], store_root, "pocket.environment/v1")
    roots = environment.get("search_roots")
    if not isinstance(roots, list) or not 1 <= len(roots) <= 16:
        raise PocketError("Catalog environment requires 1–16 search roots")
    entries = record["entries"]
    if not isinstance(entries, list) or len(entries) > 5000:
        raise PocketError("Catalog requires at most 5000 entries")
    for entry in entries:
        fields(entry, ("filename", "relative_path", "root_index", "sha256", "size_bytes", "format_hint",
                       "format_verified", "compatible_products_documented", "compatibility_condition",
                       "asset_coverage", "preview", "annotations"), label="catalog entry")
        filename = text(entry["filename"], "preset filename", 4096)
        relative = text(entry["relative_path"], "preset relative path", 4096)
        if Path(relative).is_absolute() or ".." in Path(relative).parts or Path(relative).name != filename:
            raise PocketError("Catalog preset path must be relative and match its filename")
        integer(entry["root_index"], "root index", 0, len(roots) - 1)
        integer(entry["size_bytes"], "preset size", 0, 128 * 1024 * 1024)
        if not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            raise PocketError("Catalog preset requires a byte SHA-256")
        choice(entry["format_hint"], {".fxp", ".serumpreset", ".adv", ".adg"}, "preset extension hint")
        compatible = entry["compatible_products_documented"]
        if not isinstance(compatible, list) or any(p not in PRODUCTS for p in compatible):
            raise PocketError("Invalid documented preset compatibility")
        if entry["format_verified"] is not False or entry["asset_coverage"] != "unknown" or entry["preview"] is not None:
            raise PocketError("Opaque catalogs cannot claim verified format, assets or audition previews")
        if entry["annotations"] != []:
            raise PocketError("Attributed preset annotation import is not implemented")
        text(entry["compatibility_condition"], "compatibility condition")


def sound_plan(*, store_root: str, request_id: str, brief: str,
               recipe: Literal["operator_sine_pluck", "parameter_alternatives"] = "operator_sine_pluck",
               state: ArtifactHandle | None = None, alternatives: list[SoundAlternative] | None = None,
               expected_layout_sha256: str | None = None, expected_topology_token: str | None = None,
               locks: list[str] | None = None, context: ArtifactHandle | None = None,
               performance: ArtifactHandle | None = None, max_alternatives: int = 3) -> dict:
    """Plan stock sound setup or explicitly bound scalar alternatives, without applying them."""
    text(brief, "brief", 4000)
    choice(recipe, {"operator_sine_pluck", "parameter_alternatives"}, "sound recipe")
    integer(max_alternatives, "max_alternatives", 1, 8)
    locks = [] if locks is None else locks
    if not isinstance(locks, list) or len(locks) > 4096 or any(not isinstance(x, str) for x in locks):
        raise PocketError("locks must be parameter IDs")
    if len(locks) != len(set(locks)):
        raise PocketError("Duplicate locks")
    inputs = {"brief": brief, "recipe": recipe, "state": state, "alternatives": alternatives,
              "expected_layout_sha256": expected_layout_sha256, "expected_topology_token": expected_topology_token,
              "locks": locks, "context": context, "performance": performance, "max_alternatives": max_alternatives}

    def work():
        if context is not None:
            read_record(context, store_root, "pocket.context/v1")
        if performance is not None:
            from ..material import load_material
            load_material(performance, store_root)
        changes = []
        if recipe == "operator_sine_pluck":
            if state is not None or alternatives is not None or locks or expected_layout_sha256 or expected_topology_token:
                raise PocketError("Stock setup recipe accepts a brief and optional context/performance only")
            setup = STOCK_RECIPE
        else:
            if state is None:
                raise PocketError("Parameter alternatives require an explicit instrument-state handle")
            record = _state(state, store_root)
            observed = record["observation"]
            if expected_layout_sha256 != record["parameter_layout_sha256"]:
                raise PocketError("Stale or missing expected parameter layout")
            if expected_topology_token != observed["topology_token"]:
                raise PocketError("Stale or missing expected topology")
            descriptors = {p["parameter_id"]: p for p in observed["parameters"]}
            if set(locks) - descriptors.keys():
                raise PocketError("Cannot promise locks on unknown or unobserved properties")
            if not isinstance(alternatives, list) or not 1 <= len(alternatives) <= max_alternatives:
                raise PocketError("Provide 1..max_alternatives explicit alternatives")
            labels = set()
            for alternative in alternatives:
                fields(alternative, ("label", "hypothesis", "changes"), label="sound alternative")
                text(alternative["label"], "alternative label")
                text(alternative["hypothesis"], "hypothesis", 4000)
                if alternative["label"] in labels:
                    raise PocketError("Alternative labels must be unique")
                labels.add(alternative["label"])
                edits = alternative["changes"]
                if not isinstance(edits, list) or not 1 <= len(edits) <= 2:
                    raise PocketError("Each sound alternative changes one or two parameters")
                seen, realized = set(), []
                for edit in edits:
                    fields(edit, ("parameter_id", "value"), label="parameter change")
                    key = text(edit["parameter_id"], "parameter ID")
                    if key not in descriptors or key in seen:
                        raise PocketError("Unknown or repeated parameter ID")
                    seen.add(key)
                    descriptor = descriptors[key]
                    if key in locks:
                        raise PocketError("Parameter change conflicts with a lock")
                    if not descriptor["writable"] or descriptor["automation_owner"] != "none":
                        raise PocketError("Parameter writability or automation ownership conflicts with proposed change")
                    domain(edit["value"], descriptor["native_min"], descriptor["native_max"],
                           descriptor["quantized"], descriptor["values"], "planned value")
                    realized.append({"parameter_id": key, "before": descriptor["value"], "after": edit["value"],
                                     "unit": descriptor["unit"], "value_domain": "native",
                                     "semantic_key": descriptor["semantic_key"],
                                     "mapping_evidence": descriptor["mapping_evidence"]})
                changes.append({**alternative, "changes": realized})
            setup = None
        plan = {"schema": "pocket.sound-plan/v1", **inputs, "recipe_settings": setup,
                "realized_alternatives": changes, "baseline": "unchanged", "executable": False,
                "native_verified": False, "locks_enforced": "plan_only",
                "required_routes": ["native stock setup and state capture"] if setup else ["guarded parameter_write"],
                "verification_steps": ["fresh instance, topology and parameter ownership readback",
                                       "isolated workspace checkpoint", "apply bounded change and verify readback",
                                       "save, reopen and verify source preservation", "render; separately request human listening"],
                "musical_decision": None}
        return receipt(request_id, artifacts={"sound_plan": put_record(plan, store_root)}, executable=False,
                       change_summary={"alternatives": len(changes) if changes else 1, "baseline": "unchanged"},
                       coverage={"implementation": "offline planning", "native_routes": "unavailable",
                                 "human_listening": "not_performed"},
                       next_actions=["Qualify the required native route in an isolated fixture before application"])

    return run_request(store_root, request_id, "sound_plan", inputs, work)
