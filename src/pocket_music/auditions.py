# SPDX-License-Identifier: AGPL-3.0-only
"""Bound v3 render plans, audio evidence, attribution and collected promotion.

These providers neither render nor listen. A decoded WAV establishes signal
evidence, and an attributed report establishes only what its actor reported.
"""
from __future__ import annotations

import gzip
import io
import json
import math
import shutil
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from fractions import Fraction
from pathlib import Path

from .artifact_store import (
    ArtifactHandle,
    _verify_handles,
    canonical_bytes,
    put_bytes,
    put_record,
    read_bytes,
    read_record,
    receipt,
    run_request,
)
from .assets import sha256_file
from .audio_evidence import measure_audio, validate_feedback_report
from .candidate_types import AttributedDecision, CandidateTime, RenderReport, RenderSettings
from .errors import PocketError
from .native_candidates import (
    _parse_bytes,
    _safe,
    _stamp,
    _text,
    _time,
    _xml_semantics,
    _xml_value,
    load_candidate_record,
)
from .thread_queries import inspect_set_summary


def _integer(value, name, low=0, high=2**63 - 1):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise PocketError(f"{name} must be an integer from {low} through {high}")
    return value


def _number(value, name, low=0, high=3600):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise PocketError(f"{name} must be finite and between {low} and {high}")
    return value


def _fraction(value):
    return {"n": value.numerator, "d": value.denominator}


def audition_plan(store_root: str, request_id: str, candidates: list[ArtifactHandle],
                  baseline: ArtifactHandle, span_qn: list[CandidateTime],
                  question: str, pre_roll_qn: CandidateTime | None = None,
                  post_roll_qn: CandidateTime | None = None, tail_seconds: float = 4,
                  sample_rate: int = 48000, channels: int = 2,
                  signal_expectation: str = "audible") -> dict:
    """Plan a bounded constant-tempo comparison, including the unchanged baseline."""
    inputs = {"candidates": candidates, "baseline": baseline, "span_qn": span_qn, "question": question,
              "pre_roll_qn": pre_roll_qn, "post_roll_qn": post_roll_qn, "tail_seconds": tail_seconds,
              "sample_rate": sample_rate, "channels": channels, "signal_expectation": signal_expectation}

    def work():
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 8:
            raise PocketError("Audition requires one through eight candidates")
        if not isinstance(span_qn, list) or len(span_qn) != 2:
            raise PocketError("span_qn must contain start and end rational quarter notes")
        start, end = (_time(v, "span_qn") for v in span_qn)
        pre = _time(pre_roll_qn or {"n": 0, "d": 1}, "pre_roll_qn")
        post = _time(post_roll_qn or {"n": 0, "d": 1}, "post_roll_qn")
        if pre < 0 or post < 0 or start - pre < 0 or end <= start:
            raise PocketError("Invalid audition interval or roll bounds")
        _number(tail_seconds, "tail_seconds", high=120)
        _integer(sample_rate, "sample_rate", 8000, 192000)
        _integer(channels, "channels", 1, 2)
        _text(question, "question", 2000)
        if signal_expectation not in {"audible", "intentional_silence"}:
            raise PocketError("Unknown signal expectation")
        trials = [load_candidate_record(handle, store_root) for handle in [baseline, *candidates]]
        baseline_preparation = read_record(trials[0]["preparation"], store_root, "pocket.candidate-preparation/v1")
        if baseline_preparation["mode"] != "clone_only":
            raise PocketError("Audition baseline must be an unchanged clone_only candidate")
        if any(t["parent"] != trials[0]["parent"] for t in trials):
            raise PocketError("Comparison candidates must share the exact source parent")
        for trial in trials:
            if trial["context"] is None:
                raise PocketError("Audition requires a bound constant-tempo context on every candidate")
            context = read_record(trial["context"], store_root, "pocket.context/v1")
            if context.get("tempo_bpm") != 120 or context.get("meter") != [4, 4]:
                raise PocketError("Only the qualified constant 120 BPM, 4/4 render plan is supported")
        duration = (end - start + pre + post) / 2 + Fraction(str(tail_seconds))
        frames = duration * sample_rate
        if frames.denominator != 1 or duration > 3600:
            raise PocketError("Render duration must resolve to whole frames and at most one hour")
        plan = {"schema": "pocket.audition-plan/v1", "baseline": baseline, "candidates": candidates,
                "question": question, "span_qn": span_qn, "pre_roll_qn": _fraction(pre),
                "post_roll_qn": _fraction(post), "expected_frames": int(frames),
                "settings": {"sample_rate": sample_rate, "channels": channels,
                             "start_qn": _fraction(start - pre), "end_qn": _fraction(end + post),
                             "tempo_bpm": 120, "tail_seconds": tail_seconds, "normalization": False},
                "frame_tolerance": 2, "signal_expectation": signal_expectation,
                "execution": "supervised_native_export", "listening": "not_reviewed"}
        handle = put_record(plan, store_root)
        return receipt(request_id, artifacts={"audition_plan": handle},
                       change_summary={"candidate_count": len(candidates), "baseline_included": True,
                                       "expected_frames": int(frames)},
                       coverage={"rendered": False, "listened": False})

    return run_request(store_root, request_id, "audition_plan", inputs, work)


def _report(report, candidate_hash):
    required = {"actor", "actor_kind", "observed_at", "candidate_sha256", "export_completed",
                "arrangement_only", "no_missing_media"}
    if not isinstance(report, dict) or set(report) != required:
        raise PocketError("Render report requires exact actor, timestamp, candidate and export fields")
    _text(report["actor"], "actor")
    if report["actor_kind"] not in {"human", "agent"}:
        raise PocketError("Invalid render actor kind")
    try:
        if datetime.fromisoformat(report["observed_at"]).utcoffset() is None:
            raise ValueError()
    except (TypeError, ValueError):
        raise PocketError("Render observed_at requires ISO8601 with timezone") from None
    if report["candidate_sha256"] != candidate_hash:
        raise PocketError("Render report has stale candidate binding")
    if any(report[key] is not True for key in ("export_completed", "arrangement_only", "no_missing_media")):
        raise PocketError("Completed Arrangement export without missing media must be explicitly reported")
    return {**report, "evidence_kind": "attributed_native_export_report"}


def _measure_audio(source, plan):
    # Compatibility entry point; native format/plan qualification stays here.
    return measure_audio(source, plan, require_float=True)


def _validate_plan(plan, store_root):
    required = {"schema", "baseline", "candidates", "question", "span_qn", "pre_roll_qn", "post_roll_qn",
                "expected_frames", "settings", "frame_tolerance", "signal_expectation", "execution", "listening"}
    if set(plan) != required or plan["schema"] != "pocket.audition-plan/v1":
        raise PocketError("Invalid audition plan contract")
    settings = plan["settings"]
    if not isinstance(settings, dict) or set(settings) != {
            "sample_rate", "channels", "start_qn", "end_qn", "tempo_bpm", "tail_seconds", "normalization"}:
        raise PocketError("Invalid render settings contract")
    _integer(settings["sample_rate"], "sample_rate", 8000, 192000)
    _integer(settings["channels"], "channels", 1, 2)
    _number(settings["tail_seconds"], "tail_seconds", high=120)
    if settings["normalization"] is not False or settings["tempo_bpm"] != 120:
        raise PocketError("Unsupported render normalization or tempo")
    if not isinstance(plan["span_qn"], list) or len(plan["span_qn"]) != 2:
        raise PocketError("Invalid plan span")
    start, end = (_time(v, "span_qn") for v in plan["span_qn"])
    pre, post = _time(plan["pre_roll_qn"], "pre_roll"), _time(plan["post_roll_qn"], "post_roll")
    if (pre < 0 or post < 0 or end <= start or start - pre < 0 or
            _time(settings["start_qn"], "start") != start - pre or
            _time(settings["end_qn"], "end") != end + post):
        raise PocketError("Render interval differs from musical plan")
    seconds = (end - start + pre + post) / 2 + Fraction(str(settings["tail_seconds"]))
    if seconds > 3600 or seconds * settings["sample_rate"] != plan["expected_frames"]:
        raise PocketError("Render expected frames differ from declared time mapping")
    _integer(plan["expected_frames"], "expected_frames", 1)
    if plan["frame_tolerance"] != 2 or plan["signal_expectation"] not in {"audible", "intentional_silence"}:
        raise PocketError("Unsupported render fidelity policy")
    if not isinstance(plan["candidates"], list) or not 1 <= len(plan["candidates"]) <= 8:
        raise PocketError("Invalid candidate comparison count")
    trials = [load_candidate_record(handle, store_root) for handle in [plan["baseline"], *plan["candidates"]]]
    preparation = read_record(trials[0]["preparation"], store_root, "pocket.candidate-preparation/v1")
    if preparation["mode"] != "clone_only" or any(t["parent"] != trials[0]["parent"] for t in trials):
        raise PocketError("Comparison requires unchanged baseline and matching parent")
    for trial in trials:
        context = read_record(trial["context"], store_root, "pocket.context/v1")
        if context.get("tempo_bpm") != 120 or context.get("meter") != [4, 4]:
            raise PocketError("Comparison context is outside qualified tempo/meter profile")
    return plan


def _validate_attachment(attachment, store_root):
    record = read_record(attachment, store_root, "pocket.render-attachment/v3")
    required = {"schema", "candidate", "candidate_sha256", "render_plan", "audio", "actual_settings",
                "native_evidence", "signal", "listening", "musical_verdict"}
    if set(record) != required:
        raise PocketError("Invalid render attachment contract")
    trial = load_candidate_record(record["candidate"], store_root)
    plan = _validate_plan(read_record(record["render_plan"], store_root, "pocket.audition-plan/v1"), store_root)
    if record["candidate"] not in [plan["baseline"], *plan["candidates"]]:
        raise PocketError("Render candidate is absent from its audition plan")
    if record["candidate_sha256"] != trial["saved_als_sha256"] or record["actual_settings"] != plan["settings"]:
        raise PocketError("Render binding or actual settings mismatch")
    report = {k: v for k, v in record["native_evidence"].items() if k != "evidence_kind"}
    if _report(report, trial["saved_als_sha256"]) != record["native_evidence"]:
        raise PocketError("Render report attribution mismatch")
    measured = _measure_audio(io.BytesIO(read_bytes(record["audio"], store_root)), plan)
    if measured != record["signal"]:
        raise PocketError("Render signal evidence does not match decoded audio")
    if record["listening"] != "not_reviewed" or record["musical_verdict"] is not None:
        raise PocketError("Render attachment cannot certify listening or a musical verdict")
    return record


def attach_candidate_render(store_root: str, request_id: str, candidate: ArtifactHandle,
                            render_plan: ArtifactHandle, path: str, expected_sha256: str,
                            actual_settings: RenderSettings, native_report: RenderReport) -> dict:
    """Decode a complete render and bind its independent result to the exact candidate."""
    inputs = {"candidate": candidate, "render_plan": render_plan, "path": path,
              "expected_sha256": expected_sha256, "actual_settings": actual_settings,
              "native_report": native_report}

    def work():
        trial = load_candidate_record(candidate, store_root)
        plan = _validate_plan(read_record(render_plan, store_root, "pocket.audition-plan/v1"), store_root)
        if candidate not in [plan["baseline"], *plan["candidates"]]:
            raise PocketError("Candidate is absent from render plan")
        if actual_settings != plan["settings"]:
            raise PocketError("Actual export settings differ from exact audition plan")
        if actual_settings.get("normalization") is not False:
            raise PocketError("Actual normalization must be the boolean false")
        report = _report(native_report, trial["saved_als_sha256"])
        source = Path(path).expanduser().resolve()
        stamp = _stamp(source)
        if sha256_file(source) != expected_sha256:
            raise PocketError("Stale render hash")
        signal = _measure_audio(source, plan)
        disposition, usable = signal["disposition"], signal["usable_for_expectation"]
        audio_handle = put_bytes(source.read_bytes(), store_root, "render.wav", "pocket.render-audio/v1")
        if _stamp(source) != stamp or sha256_file(source) != expected_sha256:
            raise PocketError("Render changed during attachment")
        load_candidate_record(candidate, store_root)
        attachment = {"schema": "pocket.render-attachment/v3", "candidate": candidate,
                      "candidate_sha256": trial["saved_als_sha256"], "render_plan": render_plan,
                      "audio": audio_handle, "actual_settings": actual_settings, "native_evidence": report,
                      "signal": signal,
                      "listening": "not_reviewed", "musical_verdict": None}
        handle = put_record(attachment, store_root)
        return receipt(request_id, status="ok" if usable else "failed",
                       artifacts={"attachment": handle, "audio": audio_handle},
                       coverage={"artifact_integrity": "verified", "rendered_signal": disposition,
                                 "human_listening": "not_reviewed", "promotable": usable},
                       change_summary=attachment["signal"],
                       warnings=[] if usable else ["Retained failed render is excluded from promotion"])

    result = run_request(store_root, request_id, "attach_candidate_render", inputs, work)
    if sha256_file(Path(path).expanduser().resolve()) != expected_sha256:
        raise PocketError("Render source changed since attachment request")
    _validate_attachment(result["artifacts"]["attachment"], store_root)
    return result


def audition_feedback(store_root: str, request_id: str, attachment: ArtifactHandle,
                       interval_frames: list[int], actor: str, actor_kind: str,
                       note: str, decision: str | None = None) -> dict:
    """Record an attributed report; the tool itself does not establish listening."""
    inputs = {"attachment": attachment, "interval_frames": interval_frames, "actor": actor,
              "actor_kind": actor_kind, "note": note, "decision": decision}

    def work():
        evidence = _validate_attachment(attachment, store_root)
        read_bytes(evidence["audio"], store_root)
        load_candidate_record(evidence["candidate"], store_root)
        validate_feedback_report(interval_frames, evidence["signal"]["frames"], actor, actor_kind, note, decision)
        record = {"schema": "pocket.audition-feedback/v1", **inputs,
                  "evidence_kind": "attributed_human_listening" if actor_kind == "human" else "agent_report",
                  "render_sha256": evidence["audio"]["sha256"], "candidate": evidence["candidate"]}
        handle = put_record(record, store_root)
        return receipt(request_id, artifacts={"feedback": handle},
                       coverage={"listening": record["evidence_kind"], "provider_playback": False})

    return run_request(store_root, request_id, "audition_feedback", inputs, work)


def promote_candidate(store_root: str, request_id: str, candidate: ArtifactHandle,
                      attachment: ArtifactHandle, decision: AttributedDecision,
                      output_dir: str, portability_target: str = "editable_same_environment",
                      related_artifacts: list[ArtifactHandle] | None = None) -> dict:
    """Create a new collected v2 promotion, preserving the approved v3 candidate."""
    inputs = {"candidate": candidate, "attachment": attachment, "decision": decision,
              "output_dir": output_dir, "portability_target": portability_target}
    # Omitted additions retain the established request identity and v2 lineage.
    if related_artifacts is not None:
        if not isinstance(related_artifacts, list) or len(related_artifacts) > 32:
            raise PocketError("related_artifacts must contain at most 32 artifact handles")
        for related in related_artifacts:
            read_bytes(related, store_root)
        inputs["related_artifacts"] = related_artifacts

    def work():
        trial = load_candidate_record(candidate, store_root)
        render = _validate_attachment(attachment, store_root)
        read_bytes(render["audio"], store_root)
        if render["candidate"] != candidate or render["candidate_sha256"] != trial["saved_als_sha256"]:
            raise PocketError("Promotion render belongs to a different candidate")
        if render["signal"]["usable_for_expectation"] is not True or render["signal"]["disposition"] not in {
                "usable_signal", "intentional_silence"}:
            raise PocketError("Unusable render cannot support promotion")
        if (not isinstance(decision, dict) or not {"action", "actor", "actor_kind", "reason"} <= set(decision)
                or set(decision) - {"action", "actor", "actor_kind", "reason", "feedback"}):
            raise PocketError("Promotion requires an attributed decision")
        if decision["action"] != "keep" or decision["actor_kind"] not in {"human", "agent"}:
            raise PocketError("Promotion requires explicit human or agent keep")
        _text(decision["actor"], "actor")
        _text(decision["reason"], "reason", 4000)
        if decision["actor_kind"] == "human":
            feedback = read_record(decision.get("feedback"), store_root, "pocket.audition-feedback/v1")
            if (feedback["actor_kind"] != "human" or feedback["actor"] != decision["actor"] or
                    feedback["attachment"] != attachment or feedback["decision"] != "keep"):
                raise PocketError("Human musical keep requires matching attributed listening evidence")
        if portability_target != "editable_same_environment":
            raise PocketError("Only editable_same_environment is qualified; relocation requires separate native review")
        destination = Path(output_dir).expanduser().resolve()
        if destination.exists():
            raise PocketError("Promotion destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".promote-", dir=destination.parent))
        try:
            (stage / "Ableton Project Info").mkdir()
            original = read_bytes(trial["candidate_als"], store_root)
            root = _parse_bytes(original)
            reference_changes = []
            known_paths = {d["relative_path"] for d in trial["dependencies"]}
            for kind in ("SampleRef", "MxPatchRef"):
                for index, ref in enumerate(root.findall(".//" + kind + "/FileRef")):
                    relative = _xml_value(ref, "RelativePath")
                    if relative not in known_paths or _xml_value(ref, "RelativePathType") != "3":
                        raise PocketError("Candidate has an uncollected or unsupported active reference")
                    path_node = ref.find("Path")
                    if path_node is None:
                        raise PocketError("Candidate active reference lacks a saved path hint")
                    reference_changes.append({"kind": kind, "index": index, "before": path_node.get("Value"),
                                              "after": str(destination / relative)})
                    path_node.set("Value", str(destination / relative))
            rewritten = gzip.compress(ET.tostring(root, encoding="utf-8", xml_declaration=True), mtime=0)
            (stage / "candidate.als").write_bytes(rewritten)
            for dep in trial["dependencies"]:
                target = _safe(stage, dep["relative_path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(read_bytes(dep["artifact"], store_root))
            # Copy complete referenced immutable evidence into a portable local store.
            copied = set()

            def collect(value):
                if isinstance(value, dict) and value.get("schema") == "pocket.artifact-handle/v1":
                    uri = value["artifact_uri"]
                    if uri in copied:
                        return
                    copied.add(uri)
                    raw = read_bytes(value, store_root)
                    target = _safe(stage, "evidence/" + uri)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
                    if target.name == "record.json":
                        collect(json.loads(raw))
                elif isinstance(value, dict):
                    for v in value.values():
                        collect(v)
                elif isinstance(value, list):
                    for v in value:
                        collect(v)

            collect({"candidate": candidate, "attachment": attachment, "decision": decision,
                     "related_artifacts": related_artifacts})
            manifest = [{"path": str(p.relative_to(stage)), "sha256": sha256_file(p)}
                        for p in sorted(stage.rglob("*")) if p.is_file()]
            lineage = {"schema": "pocket.promotion/v2", "candidate": candidate, "attachment": attachment,
                       "decision": decision, "portability_target": portability_target,
                       "child_als": "candidate.als", "child_als_sha256": sha256_file(stage / "candidate.als"),
                       "approved_candidate_sha256": trial["saved_als_sha256"], "files": manifest,
                       "reference_hint_changes": reference_changes,
                       "native_relocated_reopen": "not_verified",
                       "musical_verdict": "attributed_human_keep" if decision["actor_kind"] == "human" else "agent_technical_keep"}
            if related_artifacts is not None:
                lineage["related_artifacts"] = related_artifacts
            (stage / "lineage.json").write_bytes(canonical_bytes(lineage))
            lineage_hash = sha256_file(stage / "lineage.json")
            (stage / "lineage.json.sha256").write_text(lineage_hash + "\n")
            load_candidate_record(candidate, store_root)
            stage.rename(destination)
            try:
                checked = validate_candidate_promotion(str(destination), lineage_hash)
            except BaseException:
                shutil.rmtree(destination)
                raise
            lineage_handle = put_record(lineage, store_root)
            return receipt(request_id, artifacts={"lineage": lineage_handle},
                           promotion_dir=str(destination), lineage_sha256=lineage_hash,
                           child_als=str(destination / "candidate.als"), thread=checked["thread"],
                           coverage={"native_relocated_reopen": "not_verified",
                                     "decision": lineage["musical_verdict"]})
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    result = run_request(store_root, request_id, "promote_candidate", inputs, work)
    validate_candidate_promotion(result["promotion_dir"], result["lineage_sha256"])
    return result


def validate_candidate_promotion(promotion_dir: str, expected_lineage_sha256: str) -> dict:
    """Verify moved package bytes and ordinary Thread, without claiming native reopen."""
    folder = Path(promotion_dir).expanduser().resolve()
    path = _safe(folder, "lineage.json")
    if sha256_file(path) != expected_lineage_sha256:
        raise PocketError("Promotion lineage changed")
    lineage = json.loads(path.read_bytes())
    if lineage.get("schema") != "pocket.promotion/v2":
        raise PocketError("Expected pocket.promotion/v2")
    related = lineage.get("related_artifacts", [])
    if not isinstance(related, list) or len(related) > 32:
        raise PocketError("Invalid related promotion artifacts")
    for handle in related:
        read_bytes(handle, str(folder / "evidence"))
    _verify_handles(related, folder / "evidence")
    for entry in lineage["files"]:
        if sha256_file(_safe(folder, entry["path"])) != entry["sha256"]:
            raise PocketError("Promotion file is missing or changed")
    child = _safe(folder, lineage["child_als"])
    if sha256_file(child) != lineage["child_als_sha256"]:
        raise PocketError("Promoted child changed")
    trial = load_candidate_record(lineage["candidate"], str(folder / "evidence"))
    attachment = _validate_attachment(lineage["attachment"], str(folder / "evidence"))
    if attachment["candidate"] != lineage["candidate"]:
        raise PocketError("Promotion attachment candidate mismatch")
    approved = _parse_bytes(read_bytes(trial["candidate_als"], str(folder / "evidence")))
    rewritten = _parse_bytes(child.read_bytes())
    for change in lineage["reference_hint_changes"]:
        ref = rewritten.findall(".//" + change["kind"] + "/FileRef")[change["index"]]
        node = ref.find("Path")
        if node is None or node.get("Value") != change["after"]:
            raise PocketError("Promoted reference hints do not match lineage")
        node.set("Value", change["before"])
    if _xml_semantics(approved) != _xml_semantics(rewritten):
        raise PocketError("Promotion changed protected candidate XML")
    thread = inspect_set_summary(child, cache_dir=folder / ".thread-cache", hash_sources=True)
    return {"schema": "pocket.promotion-validation/v2", "lineage_sha256": expected_lineage_sha256,
            "child_als_sha256": lineage["child_als_sha256"], "thread": thread,
            "native_relocated_reopen": "not_verified", "musical_verdict": lineage["musical_verdict"]}
