# SPDX-License-Identifier: AGPL-3.0-only
"""Immutable audio passage scores over existing source and timeline occurrences."""
from __future__ import annotations

import copy
from typing import Literal

from .artifact_store import (
    ArtifactHandle,
    _verify_handles,
    call_memo,
    canonical_bytes,
    per_call_verification,
    put_record,
    read_record,
    run_request,
)
from .audio_score_types import AudioScoreDefinition
from .context_edits import load_context_edit
from .coordinates import bounded_list, fields, integer, text
from .errors import PocketError
from .musical_context import attribution, context_receipt, load_context

SCHEMA = "pocket.audio-score/v1"
PROFILE = "authored-audio-passages/v1"
COVERAGE = {"profile": PROFILE, "coordinates": "existing_context_occurrences",
            "roles": "authored_not_inferred", "relations": "nonexecuting_acyclic_annotations",
            "audio_rendered": False, "native_execution": False,
            "human_listening": "not_performed", "performance_readiness": "not_established"}
RELATIONS = {"sequence", "repeat", "variation", "withhold", "return", "call_response",
             "motif_reference", "derivation"}
MAX_PASSAGES = 256
MAX_MEMBERS = 32
MAX_RELATIONS = 512
MAX_PARENTS = 32


def _unique(rows, key):
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or key not in row:
            raise PocketError(f"Expected an explicit {key}")
        text(row[key], key, 120)
        if row[key] in seen:
            raise PocketError(f"Duplicate {key}")
        seen.add(row[key])
    return seen


def _acyclic(passage_ids, relations):
    # These are annotations, never instructions to repeat or launch audio.
    edges = {identifier: set() for identifier in passage_ids}
    incoming = {identifier: 0 for identifier in passage_ids}
    for relation in relations:
        start, end = relation["from_passage"], relation["to_passage"]
        if end not in edges[start]:
            edges[start].add(end)
            incoming[end] += 1
    ready = [identifier for identifier, count in incoming.items() if count == 0]
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for target in edges[current]:
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
    if visited != len(passage_ids):
        raise PocketError("Audio passage relations must be acyclic; they do not execute loops")


def _validate(definition, context, store_root):
    fields(definition, {"score_id", "title", "attribution", "passages", "relations"})
    text(definition["score_id"], "score_id", 120)
    text(definition["title"], "score title", 240)
    attribution(definition["attribution"])
    bounded_list(definition["passages"], "audio passages", 1, MAX_PASSAGES)
    bounded_list(definition["relations"], "passage relations", 0, MAX_RELATIONS)
    passage_ids = _unique(definition["passages"], "passage_id")
    _unique(definition["relations"], "relation_id")
    context_record, _ = load_context(context, store_root)
    occurrence_ids = {row["occurrence_id"] for row in context_record["definition"]["occurrences"]}
    for passage in definition["passages"]:
        fields(passage, {"passage_id", "label", "members", "attribution"})
        text(passage["label"], "passage label", 240)
        attribution(passage["attribution"])
        bounded_list(passage["members"], "passage members", 1, MAX_MEMBERS)
        members = _unique(passage["members"], "occurrence_id")
        if not members <= occurrence_ids:
            raise PocketError("Audio passage refers to an unknown context occurrence")
        for member in passage["members"]:
            fields(member, {"occurrence_id", "role"})
            text(member["role"], "authored occurrence role", 240)
    for relation in definition["relations"]:
        fields(relation, {"relation_id", "from_passage", "to_passage", "kind", "attribution"})
        for key in ("from_passage", "to_passage"):
            text(relation[key], key, 120)
            if relation[key] not in passage_ids:
                raise PocketError("Relation refers to an unknown audio passage")
        if not isinstance(relation["kind"], str) or relation["kind"] not in RELATIONS:
            raise PocketError("Unknown audio passage relation kind")
        attribution(relation["attribution"])
    _acyclic(passage_ids, definition["relations"])
    return context_record


@call_memo("audio_score")
def load_audio_score(handle, store_root):
    """Revalidate context, memberships and every exact edit in bounded ancestry."""
    _verify_handles(handle, store_root)
    chain, current_handle, seen = [], handle, set()
    while current_handle is not None:
        identity = canonical_bytes(current_handle)
        if identity in seen or len(chain) > MAX_PARENTS:
            raise PocketError("Audio score ancestry exceeds 32 parents or contains a cycle")
        seen.add(identity)
        record = read_record(current_handle, store_root, SCHEMA)
        fields(record, {"schema", "context", "definition", "parent", "context_edit", "coverage"})
        if canonical_bytes(record["coverage"]) != canonical_bytes(COVERAGE):
            raise PocketError("Audio score coverage mismatch", code="evidence_mismatch")
        _validate(record["definition"], record["context"], store_root)
        chain.append(record)
        current_handle = record["parent"]
    for index, record in enumerate(chain):
        if record["parent"] is None:
            if record["context_edit"] is not None:
                raise PocketError("A root audio score cannot claim a revision edit", code="evidence_mismatch")
            continue
        if record["context_edit"] is None:
            raise PocketError("An audio score revision requires its exact context edit", code="evidence_mismatch")
        parent = chain[index + 1]
        edit = load_context_edit(record["context_edit"], store_root)
        if (edit["parent"] != parent["context"] or edit["child"] != record["context"]
                or canonical_bytes(record["definition"]) != canonical_bytes(parent["definition"])):
            raise PocketError("Audio score revision context or definition differs from its proof", code="evidence_mismatch")
    return chain[0]


@per_call_verification
def audio_score_create(store_root: str, request_id: str, context: ArtifactHandle,
                       definition: AudioScoreDefinition) -> dict:
    """Group explicit audio occurrences and authored roles without changing clocks or audio."""
    inputs = {"context": context, "definition": definition}
    def work():
        _validate(definition, context, store_root)
        handle = put_record({"schema": SCHEMA, **copy.deepcopy(inputs), "parent": None,
                             "context_edit": None, "coverage": copy.deepcopy(COVERAGE)}, store_root)
        load_audio_score(handle, store_root)
        return context_receipt(request_id=request_id, artifacts={"score": handle}, coverage=copy.deepcopy(COVERAGE),
                               change_summary={"passages": len(definition["passages"]),
                                               "relations": len(definition["relations"]),
                                               "memberships": sum(len(p["members"]) for p in definition["passages"])})
    result = run_request(store_root, request_id, "audio_score_create", inputs, work)
    load_audio_score(result["artifacts"]["score"], store_root)
    return result


@per_call_verification
def audio_score_query(store_root: str, score: ArtifactHandle,
                      section: Literal["summary", "passages", "relations", "members"] = "summary",
                      passage_ids: list[str] | None = None, offset: int = 0, limit: int = 32) -> dict:
    """Read bounded score membership; role and relationship claims remain attributed."""
    integer(offset, "score query offset", 0)
    integer(limit, "score query limit", 1, 64)
    if section not in ("summary", "passages", "relations", "members"):
        raise PocketError("Unknown audio score query section")
    record = load_audio_score(score, store_root)
    definition = record["definition"]
    selected = None
    if passage_ids is not None:
        bounded_list(passage_ids, "passage filter", 1, MAX_PASSAGES)
        for identifier in passage_ids:
            text(identifier, "passage filter ID", 120)
        selected = set(passage_ids)
        known = {p["passage_id"] for p in definition["passages"]}
        if len(selected) != len(passage_ids) or not selected <= known:
            raise PocketError("Unknown or duplicate passage filter ID")
    common = {"artifacts": {"score": score}, "coverage": copy.deepcopy(COVERAGE)}
    if section == "summary":
        if offset or passage_ids is not None:
            raise PocketError("Audio score summary accepts no offset or passage filter")
        return context_receipt(**common, summary={"score_id": definition["score_id"], "title": definition["title"],
            "context": record["context"], "parent": record["parent"], "context_edit": record["context_edit"],
            "attribution": definition["attribution"], "counts": {"passages": len(definition["passages"]),
            "relations": len(definition["relations"]),
            "memberships": sum(len(p["members"]) for p in definition["passages"])}})
    passages = [p for p in definition["passages"] if selected is None or p["passage_id"] in selected]
    if section == "passages":
        rows = passages
    elif section == "members":
        rows = [{"passage_id": p["passage_id"], **member} for p in passages for member in p["members"]]
    else:
        rows = [r for r in definition["relations"] if selected is None
                or r["from_passage"] in selected or r["to_passage"] in selected]
    page = rows[offset:offset + limit]
    return context_receipt(**common, section=section, rows=page, total=len(rows), offset=offset,
                           next_offset=offset + len(page) if offset + len(page) < len(rows) else None,
                           **({"filter_scope": "either_endpoint"} if section == "relations" else {}))


@per_call_verification
def audio_score_revise(store_root: str, request_id: str, score: ArtifactHandle,
                       context_edit: ArtifactHandle) -> dict:
    """Rebind unchanged passage membership to the exact child of a verified context edit."""
    inputs = {"score": score, "context_edit": context_edit}
    def work():
        parent = load_audio_score(score, store_root)
        edit = load_context_edit(context_edit, store_root)
        if edit["parent"] != parent["context"]:
            raise PocketError("Context edit parent is not the audio score's exact context", code="source_mismatch")
        _validate(parent["definition"], edit["child"], store_root)
        handle = put_record({"schema": SCHEMA, "context": edit["child"],
                             "definition": copy.deepcopy(parent["definition"]), "parent": score,
                             "context_edit": context_edit, "coverage": copy.deepcopy(COVERAGE)}, store_root)
        load_audio_score(handle, store_root)
        return context_receipt(request_id=request_id, artifacts={"score": handle, "context": edit["child"],
                               "edit": context_edit}, coverage=copy.deepcopy(COVERAGE),
                               change_summary={"definition_changed": False, "context_rebound": True})
    result = run_request(store_root, request_id, "audio_score_revise", inputs, work)
    load_audio_score(result["artifacts"]["score"], store_root)
    return result
