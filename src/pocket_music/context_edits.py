"""Immutable literal context revisions with replayable preservation evidence."""
from __future__ import annotations

import copy

from .artifact_store import (
    ArtifactHandle,
    _verify_handles,
    canonical_bytes,
    digest,
    put_record,
    read_record,
    run_request,
)
from .context_edit_types import ContextEdit, ContextLock
from .context_types import Attribution
from .coordinates import bounded_list, fields, fraction, integer, rational_json, text
from .errors import PocketError
from .interpretations import _anchor, load_interpretation
from .musical_context import COVERAGE_V2, SCHEMA_V2, _validate, attribution, context_receipt, load_context

SCHEMA = "pocket.context-edit/v1"
COVERAGE = {"profile": "literal-context-edits/v1", "source_bytes_changed": False,
            "native_execution": False, "audio_rendered": False, "human_listening": "not_performed"}


def _objects(definition, section):
    key = "occurrence_id" if section == "occurrences" else "anchor_id"
    return {row[key]: row for row in definition[section]}


def _locks(locks, before, after):
    bounded_list(locks, "locks", maximum=128)
    identities = set()
    for lock in locks:
        fields(lock, {"section", "object_id", "fields"})
        if lock["section"] not in ("occurrences", "anchors"):
            raise PocketError("Unknown context lock section")
        text(lock["object_id"], "locked object identity", 120)
        first, second = _objects(before, lock["section"]), _objects(after, lock["section"])
        if lock["object_id"] not in first or lock["object_id"] not in second:
            raise PocketError("Context lock refers to an unknown object")
        bounded_list(lock["fields"], "locked fields", 1, 8)
        for field in lock["fields"]:
            text(field, "locked field", 120)
            identity = (lock["section"], lock["object_id"], field)
            if identity in identities:
                raise PocketError("Duplicate context field lock")
            identities.add(identity)
            original, result = first[lock["object_id"]], second[lock["object_id"]]
            if field not in original:
                raise PocketError("Unknown locked context field")
            if canonical_bytes(original[field]) != canonical_bytes(result[field]):
                raise PocketError("Context edit changes a locked field", code="locked_field")


def _changes(before, after):
    rows = []
    preserved = copy.deepcopy(before)
    for section in ("occurrences", "anchors"):
        key = "occurrence_id" if section == "occurrences" else "anchor_id"
        for index, (old, new) in enumerate(zip(before[section], after[section], strict=True)):
            if old[key] != new[key]:
                raise PocketError("Context edit changed object identity or ordering")
            for field in old:
                if canonical_bytes(old[field]) != canonical_bytes(new[field]):
                    rows.append({"path": f"/definition/{section}/{index}/{field}", "object_id": old[key],
                                 "before": old[field], "after": new[field]})
                    preserved[section][index][field] = {"changed_field": True}
    return rows, digest(preserved)


def _apply(parent, parent_handle, operations, locks, author, store_root):
    attribution(author)
    bounded_list(operations, "context edit operations", 1, 32)
    before = parent["definition"]
    definition = copy.deepcopy(before)
    bindings = copy.deepcopy(parent.get("bindings", []))
    touched = set()
    for op in operations:
        if not isinstance(op, dict) or op.get("kind") not in ("occurrence_slip_source", "occurrence_shift_timeline", "anchor_rebind"):
            raise PocketError("Unknown context edit operation")
        kind = op["kind"]
        if kind == "anchor_rebind":
            fields(op, {"kind", "anchor_id", "interpretation", "binding_id"})
            text(op["anchor_id"], "anchor_id", 120)
            anchors = _objects(definition, "anchors")
            if op["anchor_id"] not in anchors:
                raise PocketError("Anchor rebind requires an existing exact anchor identity")
            key = ("anchors", op["anchor_id"])
            if key in touched:
                raise PocketError("An anchor may be rebound only once per edit")
            touched.add(key)
            interpretation, _ = load_interpretation(op["interpretation"], store_root)
            if interpretation["context"] != parent_handle:
                raise PocketError("Anchor rebind requires an interpretation of the exact edit parent")
            binding = {"kind": "anchor", "binding_id": op["binding_id"], "anchor_id": op["anchor_id"],
                       "label": anchors[op["anchor_id"]]["label"]}
            result = _anchor(binding, interpretation, author)
            anchors[op["anchor_id"]].update(result)
            # The new exact claim replaces this anchor's prior binding, never unrelated selections.
            bindings = [b for b in bindings if b["binding"].get("anchor_id") != op["anchor_id"]]
            bindings.append({"interpretation": op["interpretation"], "binding": binding, "attribution": copy.deepcopy(author)})
            continue
        field = "source_span_frames" if kind == "occurrence_slip_source" else "timeline_span_qn"
        delta_key = "delta_frames" if kind == "occurrence_slip_source" else "delta_qn"
        fields(op, {"kind", "occurrence_ids", delta_key})
        bounded_list(op["occurrence_ids"], "explicit linked occurrence IDs", 1, 32)
        for identifier in op["occurrence_ids"]:
            text(identifier, "occurrence_id", 120)
        if len(set(op["occurrence_ids"])) != len(op["occurrence_ids"]):
            raise PocketError("Duplicate linked occurrence identity")
        delta = integer(op[delta_key], "source slip") if kind == "occurrence_slip_source" else fraction(op[delta_key])
        if not delta:
            raise PocketError("Context edit delta must be nonzero")
        objects = _objects(definition, "occurrences")
        for identifier in op["occurrence_ids"]:
            if identifier not in objects:
                raise PocketError("Unknown occurrence in context edit")
            key = (identifier, field)
            if key in touched:
                raise PocketError("Each occurrence field may change only once per edit")
            touched.add(key)
            old = objects[identifier][field]
            objects[identifier][field] = ([integer(v + delta, "shifted source frame", 0) for v in old]
                if kind == "occurrence_slip_source" else [rational_json(fraction(v) + delta) for v in old])
    _locks(locks, before, definition)
    _validate(definition, store_root)
    child = {"schema": parent["schema"], "definition": definition, "parent": parent_handle,
             "coverage": copy.deepcopy(parent["coverage"])}
    if bindings or parent["schema"] == SCHEMA_V2:
        child.update(schema=SCHEMA_V2, coverage=copy.deepcopy(COVERAGE_V2), bindings=bindings)
    changes, preserved = _changes(before, definition)
    if child.get("bindings", []) != parent.get("bindings", []):
        changes.append({"path": "/bindings", "object_id": None, "before": parent.get("bindings", []),
                        "after": child.get("bindings", [])})
    if not changes:
        raise PocketError("Context edit makes no change")
    return child, changes, preserved


def _renderability(child, operations, store_root):
    from .practice_audio import _sequence
    groups = [op["occurrence_ids"] for op in operations if "occurrence_ids" in op]
    results = []
    for ids in groups:
        try:
            _sequence(child, ids, store_root)
            results.append({"occurrence_ids": ids, "exact_pcm_compatible": True, "reason": None})
        except PocketError as error:
            results.append({"occurrence_ids": ids, "exact_pcm_compatible": False, "reason": str(error)})
    return results


def load_context_edit(handle, store_root):
    _verify_handles(handle, store_root)
    record = read_record(handle, store_root, SCHEMA)
    fields(record, {"schema", "parent", "child", "operations", "locks", "attribution", "changes",
                    "preserved_definition_sha256", "renderability", "coverage"})
    parent, _ = load_context(record["parent"], store_root)
    child, _ = load_context(record["child"], store_root)
    expected, changes, preserved = _apply(parent, record["parent"], record["operations"], record["locks"],
                                         record["attribution"], store_root)
    if (canonical_bytes(child) != canonical_bytes(expected) or record["changes"] != changes
            or record["preserved_definition_sha256"] != preserved or record["coverage"] != COVERAGE
            or record["renderability"] != _renderability(record["child"], record["operations"], store_root)):
        raise PocketError("Context edit preservation evidence mismatch", code="evidence_mismatch")
    return record


def context_edit(store_root: str, request_id: str, context: ArtifactHandle, operations: list[ContextEdit],
                 locks: list[ContextLock], attribution: Attribution) -> dict:
    """Apply only named literal changes, retaining the exact parent and replayable proof."""
    inputs = {"context": context, "operations": operations, "locks": locks, "attribution": attribution}
    def work():
        parent, _ = load_context(context, store_root)
        definition, changes, preserved = _apply(parent, context, operations, locks, attribution, store_root)
        child = put_record(definition, store_root)
        load_context(child, store_root)
        renderability = _renderability(child, operations, store_root)
        record = {"schema": SCHEMA, "parent": context, "child": child, "operations": copy.deepcopy(operations),
                  "locks": copy.deepcopy(locks), "attribution": copy.deepcopy(attribution), "changes": changes,
                  "preserved_definition_sha256": preserved, "renderability": renderability, "coverage": COVERAGE}
        edit = put_record(record, store_root)
        return context_receipt(request_id=request_id, artifacts={"context": child, "edit": edit}, coverage=COVERAGE,
                               change_summary={"changed_paths": [row["path"] for row in changes]},
                               renderability=renderability)
    result = run_request(store_root, request_id, "context_edit", inputs, work)
    load_context_edit(result["artifacts"]["edit"], store_root)
    return result


def context_edit_query(store_root: str, edit: ArtifactHandle) -> dict:
    """Recompute preservation and supported render-profile checks from retained parents."""
    return context_receipt(artifacts={"edit": edit}, summary=load_context_edit(edit, store_root), coverage=COVERAGE)
