"""Pipette promotes explicit kept, saved Stitch candidates into collected children.

Checksums establish artifact identity, not authenticity or a musical verdict.
Live loading and export reports retain their original, independently labeled scope.
"""

from __future__ import annotations

import copy
import gzip
import os
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Literal

from typing_extensions import TypedDict

from .assets import identify_audio, sha256_file
from .errors import PocketError
from .stitch import (
    _load_sealed,
    _now,
    _read_als,
    _safe_child,
    _seal,
    _staging,
    _stamp,
    _text,
    validate_native_trial,
)
from .thread_queries import find_clips, inspect_set_summary


class PromotionDecision(TypedDict):
    action: Literal["keep"]
    actor: str
    actor_kind: Literal["human", "agent"]
    reason: str


def _hash(value, label):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise PocketError(f"{label} must be a lowercase SHA-256")
    return value


def _relative(folder: Path, name: str) -> Path:
    if not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts:
        raise PocketError("Artifact must use a contained relative path")
    return _safe_child(folder, name)


def _publish_complete(stage: Path, destination: Path) -> None:
    """Reserve a new destination exclusively and roll back our own partial publish."""
    try:
        destination.mkdir()
    except FileExistsError as exc:
        raise PocketError("Output destination appeared during promotion") from exc
    try:
        # Publish the openable child last, after the seal and all dependencies.
        for item in sorted(stage.iterdir(), key=lambda p: p.name == "promoted.als"):
            os.rename(item, destination / item.name)
        stage.rmdir()
    except BaseException:
        shutil.rmtree(destination)
        raise


def _selected_evidence(folder, attachment, trial_sha, candidate_sha, attachment_sha):
    manifest, digest = _load_sealed(folder, "native-trial.json", "pocket.native-trial/v2")
    if digest != trial_sha or manifest["candidate_sha256"] != candidate_sha:
        raise PocketError("Stale-trial: expected trial or candidate identity changed")
    candidate = _relative(folder, manifest["candidate_file"])
    if sha256_file(candidate) != candidate_sha:
        raise PocketError("Stale-trial: candidate bytes changed after preparation")
    readiness = validate_native_trial(str(folder), expected_candidate_sha256=candidate_sha)
    receipt, receipt_hash = _load_sealed(attachment, "attachment.json", "pocket.native-render-attachment/v1")
    if receipt_hash != attachment_sha:
        raise PocketError("Stale-trial: selected render attachment identity changed")
    if receipt["trial_manifest_sha256"] != digest or receipt["candidate_sha256"] != candidate_sha:
        raise PocketError("Selected render attachment belongs to a different trial/candidate")
    expected_range = {k: manifest["export_range"][k] for k in ("start_beat", "length_beats")}
    if receipt["declared_export_range"] != expected_range:
        raise PocketError("Selected render export range differs from the trial")
    signal = receipt["signal"]
    disposition = signal.get("disposition")
    expectation = manifest["signal_expectation"]
    matched = ((expectation == "music" and disposition == "usable_signal") or
               (expectation == "intentional_silence" and disposition == "intentional_silence"
                and bool(manifest["expectation_note"])))
    if (signal.get("usable_for_expectation") is not True or not matched or
            signal.get("expectation") != expectation or signal.get("expectation_note") != manifest["expectation_note"]):
        raise PocketError(f"Render not promotable: disposition={disposition!r}, expectation={expectation!r}")
    if receipt["artifact"].get("status") != "verified" or receipt["artifact"].get("byte_copy_verified") is not True:
        raise PocketError("Selected render attachment is not a verified artifact")
    render = _relative(attachment, receipt["render_file"])
    actual = identify_audio(render)
    fields = ("sha256", "frames", "sample_rate", "channels", "format", "subtype")
    if any(actual[k] != receipt["render_identity"].get(k) for k in fields):
        raise PocketError("Stale-trial: attached render bytes or header changed")
    return manifest, receipt, readiness, candidate, render


def _rebind_child(candidate, stage, destination, manifest):
    """Rewrite only collected absolute hints, proving all other XML is unchanged."""
    original = _read_als(candidate)
    child = copy.deepcopy(original)
    changes = []
    for entry in manifest["reference_changes"]:
        ref = child.findall(".//" + entry["kind"] + "/FileRef")[entry["index"]]
        old_path = ref.find("Path").get("Value")
        new_path = str(_relative(destination, entry["relative_path"]))
        ref.find("Path").set("Value", new_path)
        changes.append({"kind": entry["kind"], "index": entry["index"],
                        "before": old_path, "after": new_path})
    path = stage / "promoted.als"
    path.write_bytes(gzip.compress(ET.tostring(child, encoding="utf-8", xml_declaration=True), mtime=0))
    reverted = _read_als(path)
    for entry in changes:
        ref = reverted.findall(".//" + entry["kind"] + "/FileRef")[entry["index"]]
        if ref.find("Path").get("Value") != entry["after"]:
            raise PocketError("Promoted dependency hint differs from declared rewrite")
        ref.find("Path").set("Value", entry["before"])
    if ET.tostring(reverted) != ET.tostring(original):
        raise PocketError("Promoted child changed outside collected dependency hints")
    return sha256_file(path), changes


def _check_thread_files(summary):
    statuses = summary["coverage"]["runtime_filesystem_references"]
    if any(count for status, count in statuses.items() if status != "present"):
        raise PocketError(f"Thread cannot resolve promoted active media unambiguously: {statuses}")


def promote_trial(
    trial_dir: str,
    output_dir: str,
    *,
    attachment_relative_path: str,
    expected_trial_manifest_sha256: str,
    expected_candidate_sha256: str,
    expected_attachment_sha256: str,
    decision: PromotionDecision,
) -> dict:
    """Promote one explicit keep decision into a new collected, sealed saved project.

    Requires the unchanged saved parent, trial, selected render and collected files.
    Rebinds only collected dependency hints, proving other saved XML unchanged.
    Does not Save As in Live, merge unsaved changes, interpret a Baste observation
    as durable, or replace the parent.
    """
    trial_sha = _hash(expected_trial_manifest_sha256, "expected_trial_manifest_sha256")
    candidate_sha = _hash(expected_candidate_sha256, "expected_candidate_sha256")
    attachment_sha = _hash(expected_attachment_sha256, "expected_attachment_sha256")
    if (not isinstance(decision, dict) or set(decision) != {"action", "actor", "actor_kind", "reason"}
            or decision.get("action") != "keep" or decision.get("actor_kind") not in {"human", "agent"}):
        raise PocketError("Promotion requires an explicit keep decision with actor, actor_kind and reason")
    attribution = {**decision, "actor": _text(decision["actor"], "actor", 120),
                   "reason": _text(decision["reason"], "reason"), "evidence": "attributed_decision_context"}
    folder = Path(trial_dir).expanduser().resolve()
    attachment = _relative(folder, attachment_relative_path)
    if attachment.parent != folder / "renders":
        raise PocketError("Select an attachment directly inside this trial's renders directory")
    destination = Path(output_dir).expanduser().resolve()
    if destination.is_relative_to(folder):
        raise PocketError("Promotion destination must be outside the preserved trial")
    try:
        manifest, receipt, readiness, candidate, render = _selected_evidence(
            folder, attachment, trial_sha, candidate_sha, attachment_sha)
        parent = Path(manifest["source_als"]["local_path"]).expanduser().resolve()
        parent_hash = manifest["source_als"]["sha256"]
        parent_before = _stamp(parent)
        if sha256_file(parent) != parent_hash:
            raise PocketError("Stale-trial: saved parent changed; preserve/reconcile its original version first")
        sources = [(candidate, "evidence/approved-candidate.als", candidate_sha)]
        for dep in manifest["dependencies"]:
            name = dep["relative_path"]
            if Path(name).parts[0] not in {"Samples", "Devices"}:
                raise PocketError("Unsupported collected dependency destination")
            sources.append((_relative(folder, name), name, dep["sha256"]))
        for src, name in ((folder / "native-trial.json", "evidence/native-trial.json"),
                          (folder / "native-trial.json.sha256", "evidence/native-trial.json.sha256"),
                          (attachment / "attachment.json", "evidence/attachment.json"),
                          (attachment / "attachment.json.sha256", "evidence/attachment.json.sha256"),
                          (render, "evidence/render.wav")):
            sources.append((src, name, sha256_file(src)))
        before = {src: _stamp(src) for src, _, _ in sources}
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, OSError) as exc:
        raise PocketError(f"Cannot verify promotion evidence: {exc}") from exc
    stage = _staging(destination)
    published = False
    try:
        (stage / "Ableton Project Info").mkdir()
        artifacts = []
        for src, name, digest in sources:
            target = _relative(stage, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with src.open("rb") as source, target.open("xb") as out:
                shutil.copyfileobj(source, out)
            if sha256_file(target) != digest:
                raise PocketError("Evidence changed during promotion copy")
            artifacts.append({"relative_path": name, "sha256": digest, "size_bytes": target.stat().st_size})
        child_sha, reference_changes = _rebind_child(candidate, stage, destination, manifest)
        artifacts.append({"relative_path": "promoted.als", "sha256": child_sha,
                          "size_bytes": (stage / "promoted.als").stat().st_size})
        # Exercise the existing saved-map and handle validation before sealing.
        summary = inspect_set_summary(stage / "promoted.als", cache_dir=stage / ".thread-check")
        _check_thread_files(summary)
        find_clips(summary["handle"], limit=1)
        shutil.rmtree(stage / ".thread-check")
        # Reverify full source evidence after copying, not just its filenames.
        _selected_evidence(folder, attachment, trial_sha, candidate_sha, attachment_sha)
        if (any(_stamp(src) != before[src] or sha256_file(src) != digest for src, _, digest in sources)
                or _stamp(parent) != parent_before or sha256_file(parent) != parent_hash):
            raise PocketError("Stale-trial: input changed during promotion")
        lineage = {
            "schema": "pocket.promotion/v1", "created_at": _now(),
            "parent_als_sha256": parent_hash, "trial_manifest_sha256": trial_sha,
            "candidate_als_sha256": candidate_sha,
            "child_als_sha256": child_sha, "child_file": "promoted.als",
            "reference_hint_changes": reference_changes,
            "saved_XML_except_declared_reference_hints_identical": True,
            "selected_attachment_sha256": attachment_sha,
            "render_audio_sha256": receipt["render_identity"]["sha256"],
            "export_range": receipt["declared_export_range"], "decision": attribution,
            "source_trial_dir": str(folder), "source_parent_als": str(parent),
            "source_attachment_relative_path": attachment_relative_path,
            "artifacts": artifacts, "signal": receipt["signal"],
            "native_readiness": receipt["native_readiness"],
            "native_loading_of_promoted_child": "unverified",
            "saved_readback": {"provider": "Thread", "set_sha256": summary["set"]["sha256"],
                               "handle_query_verified": True},
            "processing": "collected_dependency_hints_rebound_only; media_and_evidence_byte_copied",
            "parent_preservation": "bytes_and_mtime_verified_unchanged",
            "portability_scope": readiness["native_readiness"]["scope"],
            "limitations": ["Sequential filesystem checks are not protection against a malicious concurrent writer",
                            "Native operator reports retain their original scope; child loading is a separate check",
                            "Promotion records an attributed keep decision, not measured musical quality"],
        }
        digest = _seal(stage, "lineage.json", lineage)
        _publish_complete(stage, destination)
        published = True
        result = validate_promotion(str(destination), expected_lineage_sha256=digest)
        return {**result, "lineage": lineage}
    except BaseException:
        if published:
            shutil.rmtree(destination)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def validate_promotion(promotion_dir: str, *, expected_lineage_sha256: str) -> dict:
    """Verify a promoted project's seal, copied evidence and normal Thread handle.

    Relocation does not require the old trial/parent paths. No native loading or
    listening verdict is inferred, and no Live session is opened or changed.
    """
    _hash(expected_lineage_sha256, "expected_lineage_sha256")
    folder = Path(promotion_dir).expanduser().resolve()
    try:
        lineage, digest = _load_sealed(folder, "lineage.json", "pocket.promotion/v1")
        if digest != expected_lineage_sha256:
            raise PocketError("Stale promotion lineage identity")
        if not (folder / "Ableton Project Info").is_dir():
            raise PocketError("Missing promoted project marker")
        for item in lineage["artifacts"]:
            file = _relative(folder, item["relative_path"])
            if file.stat().st_size != item["size_bytes"] or sha256_file(file) != item["sha256"]:
                raise PocketError("Promoted artifact changed: " + item["relative_path"])
        child = _relative(folder, lineage["child_file"])
        if sha256_file(child) != lineage["child_als_sha256"]:
            raise PocketError("Promoted child changed")
        summary = inspect_set_summary(child)
        _check_thread_files(summary)
        find_clips(summary["handle"], limit=1)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, OSError) as exc:
        raise PocketError(f"Cannot verify promoted project: {exc}") from exc
    return {"schema": "pocket.promotion-validation/v1", "promotion_dir": str(folder),
            "child_als": str(child), "child_als_sha256": lineage["child_als_sha256"],
            "lineage_file": str(folder / "lineage.json"), "lineage_sha256": digest,
            "artifacts_verified": len(lineage["artifacts"]), "thread": summary,
            "file_readiness": "sealed_collected_child_verified",
            "native_loading": "unverified", "musical_verdict": None}
