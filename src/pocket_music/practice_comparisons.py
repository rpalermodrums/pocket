"""Cross-revision comparisons with explicit edit ancestry and full occurrence pairs."""
from __future__ import annotations

import copy
from typing import Literal

from .artifact_store import (
    ArtifactHandle,
    _verify_handles,
    canonical_bytes,
    per_call_verification,
    put_record,
    read_record,
    run_request,
)
from .context_edits import load_context_edit
from .coordinates import bounded_list, fields, integer, text
from .errors import PocketError
from .musical_context import context_receipt
from .practice_comparison_types import RevisionCorrespondence

SCHEMA = "pocket.practice-revision-comparison/v1"
COVERAGE = {"profile": "explicit-edit-correspondence/v1", "provider_playback": False,
            "human_listening": "not_performed", "automatic_alignment": False}


def _pairs(baseline, variant, pairs, duration_policy):
    bounded_list(pairs, "occurrence correspondence", 1, 32)
    first = {m["occurrence_id"]: m for m in baseline["mappings"]}
    second = {m["occurrence_id"]: m for m in variant["mappings"]}
    seen_first, seen_second = set(), set()
    for pair in pairs:
        fields(pair, {"baseline_occurrence_id", "variant_occurrence_id", "baseline_interval_frames", "variant_interval_frames"})
        a, b = pair["baseline_occurrence_id"], pair["variant_occurrence_id"]
        text(a, "baseline occurrence", 120)
        text(b, "variant occurrence", 120)
        if a not in first or b not in second or a in seen_first or b in seen_second:
            raise PocketError("Correspondence requires unique, known output occurrences")
        seen_first.add(a)
        seen_second.add(b)
        for key, mapping in (("baseline_interval_frames", first[a]), ("variant_interval_frames", second[b])):
            bounded_list(pair[key], "comparison output interval", 2, 2)
            for frame in pair[key]:
                integer(frame, "comparison output frame", 0)
            if pair[key] != mapping["output_span_frames"]:
                raise PocketError("This correspondence profile requires exact full occurrence output intervals")
        if duration_policy == "equal" and (pair["baseline_interval_frames"][1] - pair["baseline_interval_frames"][0]
                != pair["variant_interval_frames"][1] - pair["variant_interval_frames"][0]):
            raise PocketError("Unequal occurrence durations require allow_mismatch policy")
    if seen_first != set(first) or seen_second != set(second):
        raise PocketError("Correspondence must cover every compared output occurrence exactly once")


def _validate(inputs, store_root):
    from .practice_audio import load_practice_render
    text(inputs["question"], "comparison question", 2000)
    bounded_list(inputs["variants"], "variants", 1, 8)
    bounded_list(inputs["edit_receipts"], "edit receipts", 1, 16)
    bounded_list(inputs["correspondence"], "variant correspondence", 1, 8)
    if inputs["duration_policy"] not in ("equal", "allow_mismatch"):
        raise PocketError("Unknown revision comparison duration policy")
    handles = [inputs["baseline"], *inputs["variants"]]
    if len({canonical_bytes(h) for h in handles}) != len(handles):
        raise PocketError("Comparison requires distinct exact render identities")
    renders = [load_practice_render(h, store_root) for h in handles]
    if len({(r["signal"]["sample_rate"], r["signal"]["channels"]) for r in renders}) != 1:
        raise PocketError("Comparison rate and channel layouts must match")
    if inputs["duration_policy"] == "equal" and len({r["signal"]["frames"] for r in renders}) != 1:
        raise PocketError("Unequal output durations require allow_mismatch policy")
    edits = {}
    for handle in inputs["edit_receipts"]:
        edit = load_context_edit(handle, store_root)
        key = canonical_bytes(edit["child"])
        if key in edits:
            raise PocketError("Duplicate or conflicting edit child identity")
        edits[key] = edit
    root = canonical_bytes(renders[0]["context"])
    used = set()
    for render in renders[1:]:
        current = canonical_bytes(render["context"])
        if current == root:
            raise PocketError("Use practice_compare for one exact context revision")
        for _ in range(16):
            if current == root:
                break
            if current not in edits:
                raise PocketError("Variant lacks verified edit lineage to the exact baseline context", code="evidence_mismatch")
            used.add(current)
            current = canonical_bytes(edits[current]["parent"])
        if current != root:
            raise PocketError("Comparison edit ancestry exceeds 16 revisions")
    if used != set(edits):
        raise PocketError("Comparison contains unrelated edit receipts")
    correspondence = {}
    for item in inputs["correspondence"]:
        fields(item, {"variant", "pairs"})
        key = canonical_bytes(item["variant"])
        if key in correspondence:
            raise PocketError("Duplicate variant correspondence")
        correspondence[key] = item["pairs"]
    if set(correspondence) != {canonical_bytes(v) for v in inputs["variants"]}:
        raise PocketError("Correspondence must name each exact variant render")
    for handle, render in zip(inputs["variants"], renders[1:], strict=True):
        _pairs(renders[0], render, correspondence[canonical_bytes(handle)], inputs["duration_policy"])
    return renders


@per_call_verification
def practice_compare_revisions(store_root: str, request_id: str, baseline: ArtifactHandle,
                               variants: list[ArtifactHandle], edit_receipts: list[ArtifactHandle],
                               correspondence: list[RevisionCorrespondence], question: str,
                               duration_policy: Literal["equal", "allow_mismatch"] = "equal") -> dict:
    """Compare immutable edits with their unchanged baseline; never infer correspondence or a winner."""
    inputs = {"baseline": baseline, "variants": variants, "edit_receipts": edit_receipts,
              "correspondence": correspondence, "question": question, "duration_policy": duration_policy}
    def work():
        renders = _validate(inputs, store_root)
        ready = all(r["signal"]["usable_for_expectation"] for r in renders)
        record = {"schema": SCHEMA, **copy.deepcopy(inputs), "signal_ready": ready,
                  "listening": "not_reviewed", "musical_verdict": None, "coverage": COVERAGE}
        handle = put_record(record, store_root)
        return context_receipt(request_id=request_id, artifacts={"comparison": handle},
                               coverage={**COVERAGE, "signal_ready": ready},
                               warnings=[] if ready else ["One or more renders require signal review"])
    result = run_request(store_root, request_id, "practice_compare_revisions", inputs, work)
    load_revision_comparison(result["artifacts"]["comparison"], store_root)
    return result


def load_revision_comparison(handle, store_root):
    _verify_handles(handle, store_root)
    record = read_record(handle, store_root, SCHEMA)
    keys = {"baseline", "variants", "edit_receipts", "correspondence", "question", "duration_policy"}
    fields(record, {"schema", "signal_ready", "listening", "musical_verdict", "coverage"} | keys)
    renders = _validate({k: record[k] for k in keys}, store_root)
    if (record["signal_ready"] is not all(r["signal"]["usable_for_expectation"] for r in renders)
            or record["listening"] != "not_reviewed" or record["musical_verdict"] is not None
            or record["coverage"] != COVERAGE):
        raise PocketError("Revision comparison evidence mismatch", code="evidence_mismatch")
    return record, renders
