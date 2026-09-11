"""Bounded listening trials with exact lineage and a supervised native-trial adapter.

This module does not render an Ableton set, infer a downbeat, or declare that a
listener heard anything. Its outputs describe samples and explicit human input.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Literal

from typing_extensions import NotRequired, TypedDict

import numpy as np
import soundfile as sf

from .assets import identify_audio, sha256_file
from .errors import PocketError


class TrialVariant(TypedDict):
    """One exact source window; omitted fields use the shared explicit window."""

    source_path: str
    label: str
    expected_sha256: NotRequired[str]
    start_frame: NotRequired[int]
    frames: NotRequired[int]
    gain_db: NotRequired[float]


class NativeExportSettings(TypedDict):
    """Declared export settings, independently checked against the WAV header."""

    rendered_track: Literal["Main"]
    sample_rate: int
    channels: Literal[2]
    normalization: Literal[False]
    mono: Literal[False]
    loop_render: Literal[False]
    dither: Literal["none"]


FeedbackScope = Literal["timing", "bar_phase", "flow", "tonal_overlap", "level", "preference", "other"]

MAX_SECONDS = 300
MAX_VARIANTS = 8
SCOPES = frozenset({"timing", "bar_phase", "flow", "tonal_overlap", "level", "preference", "other"})
_AUDIO_PATH = "DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events/AudioClip"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _integer(value, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PocketError(f"{name} must be an integer >= {minimum}")
    return value


def _number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PocketError(f"{name} must be a finite number")
    return float(value)


def _stamp(path: Path) -> tuple:
    try:
        s = path.stat()
    except OSError as exc:
        raise PocketError(f"Cannot read {path.name}: {exc.strerror or exc}") from exc
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns


def _text(value, name: str, maximum: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise PocketError(f"{name} must contain 1–{maximum} characters")
    return value.strip()


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def _seal(folder: Path, name: str, value: dict) -> str:
    data = _json_bytes(value)
    digest = hashlib.sha256(data).hexdigest()
    with (folder / name).open("xb") as stream:
        stream.write(data)
    with (folder / (name + ".sha256")).open("x") as stream:
        stream.write(digest + "\n")
    return digest


def _load_sealed(folder: Path, name: str, schema: str) -> tuple[dict, str]:
    try:
        path = folder / name
        before = _stamp(path)
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if _stamp(path) != before or (folder / (name + ".sha256")).read_text().strip() != digest:
            raise PocketError("Manifest changed after creation; create a new trial")
        result = json.loads(data)
        if result.get("schema") != schema:
            raise PocketError("Unsupported trial manifest schema")
        return result, digest
    except (OSError, ValueError, TypeError) as exc:
        raise PocketError(f"Cannot verify trial manifest: {exc}") from exc


def _staging(destination: Path) -> Path:
    if destination.exists():
        raise PocketError("Output destination already exists; choose a new trial directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=".pocket-trial-", dir=destination.parent))


def _publish(staging: Path, destination: Path) -> None:
    # A new directory reservation prevents replacing an existing trial, including
    # an empty one. File creation is exclusive; partial failures are not published.
    try:
        destination.mkdir()
    except FileExistsError as exc:
        raise PocketError("Output destination appeared during creation") from exc
    for item in staging.iterdir():
        os.rename(item, destination / item.name)
    staging.rmdir()


def _safe_child(folder: Path, filename: str) -> Path:
    path = (folder / filename).resolve()
    if not path.is_relative_to(folder.resolve()) or path == folder.resolve():
        raise PocketError("Artifact path escapes its trial directory")
    return path


def _peak_summary(peak: float, overs: int, frames: int) -> dict:
    return {
        "sample_peak": peak,
        "sample_peak_dbfs": 20 * math.log10(peak) if peak else None,
        "samples_at_or_above_full_scale": overs,
        "decoded_frames": frames,
        "true_peak_measured": False,
    }


def create_trial(
    output_dir: str,
    variants: list[TrialVariant],
    *,
    start_frame: int | None = None,
    frames: int | None = None,
    title: str = "Transition trial",
    allow_duration_mismatch: bool = False,
) -> dict:
    """Create immutable, exact-window DOUBLE WAV variants from existing audio.

    Frame coordinates are source-file frames, end-exclusive. Per-variant frame
    bounds override the shared bounds and are explicitly recorded. No resampling,
    downmix, normalization, or fades are performed. Outputs over full scale remain
    floating point and are flagged, never silently limited.
    """
    destination = Path(output_dir).expanduser().resolve()
    title = _text(title, "title", 200)
    if not isinstance(allow_duration_mismatch, bool):
        raise PocketError("allow_duration_mismatch must be a boolean")
    if not isinstance(variants, list) or not 1 <= len(variants) <= MAX_VARIANTS:
        raise PocketError(f"Provide between 1 and {MAX_VARIANTS} variants")
    prepared = []
    for index, variant in enumerate(variants):
        if not isinstance(variant, dict) or "source_path" not in variant:
            raise PocketError("Each variant needs source_path and label")
        label = _text(variant.get("label"), "label", 120)
        source = Path(variant["source_path"]).expanduser().resolve()
        identity = identify_audio(source)
        if variant.get("expected_sha256") is not None and variant["expected_sha256"] != identity["sha256"]:
            raise PocketError(f"Stale source identity for {label}")
        begin = _integer(variant.get("start_frame", start_frame), "start_frame")
        count = _integer(variant.get("frames", frames), "frames", 1)
        if begin + count > identity["frames"]:
            raise PocketError(f"Excerpt extends beyond source for {label}")
        if count / identity["sample_rate"] > MAX_SECONDS:
            raise PocketError(f"Excerpt exceeds {MAX_SECONDS} seconds")
        if identity["channels"] not in (1, 2):
            raise PocketError("Only mono or stereo sources are supported; no automatic channel conversion")
        gain = _number(variant.get("gain_db", 0), "gain_db")
        if not -120 <= gain <= 24:
            raise PocketError("Explicit gain must be between -120 and +24 dB")
        prepared.append(
            {
                "id": f"v{index + 1:02d}",
                "label": label,
                "source": identity,
                "source_start_frame": begin,
                "frames": count,
                "gain_db": gain,
                "mapping": "separate_source_window"
                if "start_frame" in variant or "frames" in variant
                else "shared_frame_window",
            }
        )
    if len({(v["source"]["sample_rate"], v["source"]["channels"]) for v in prepared}) != 1:
        raise PocketError("All variants must share sample rate and channel count; no implicit conversion")
    if not allow_duration_mismatch and len({v["frames"] for v in prepared}) != 1:
        raise PocketError("Unequal durations require allow_duration_mismatch=True")
    stage = _staging(destination)
    try:
        for v in prepared:
            source = Path(v["source"]["local_path"])
            before = _stamp(source)
            # Revalidate identity if source changed between input validation and reading.
            if sha256_file(source) != v["source"]["sha256"]:
                raise PocketError("Source changed before extraction")
            filename = v["id"] + ".wav"
            peak, overs, count = 0.0, 0, 0
            payload = hashlib.sha256()
            with (
                sf.SoundFile(source) as src,
                sf.SoundFile(
                    stage / filename, "w", samplerate=src.samplerate, channels=src.channels, subtype="DOUBLE"
                ) as dst,
            ):
                src.seek(v["source_start_frame"])
                remaining = v["frames"]
                while remaining:
                    block = src.read(min(65536, remaining), dtype="float64", always_2d=True)
                    if len(block) == 0 or not np.isfinite(block).all():
                        raise PocketError("Source excerpt is incomplete or contains non-finite samples")
                    payload.update(np.asarray(block, dtype="<f8").tobytes())
                    if v["gain_db"] != 0:
                        block = block * 10 ** (v["gain_db"] / 20)
                    dst.write(block)
                    peak = max(peak, float(np.max(np.abs(block))))
                    overs += int(np.count_nonzero(np.abs(block) >= 1))
                    count += len(block)
                    remaining -= len(block)
            if _stamp(source) != before:
                raise PocketError("Source changed during extraction")
            output = identify_audio(stage / filename)
            output.pop("local_path")
            v.update(
                {
                    "output_file": filename,
                    "output": output,
                    "source_window_float64_sha256": payload.hexdigest(),
                    "zero_gain_decoded_samples_exact": v["gain_db"] == 0,
                    "signal": _peak_summary(peak, overs, count),
                }
            )
        manifest = {
            "schema": "pocket.transition-trial/v1",
            "created_at": _now(),
            "title": title,
            "variants": prepared,
            "allow_duration_mismatch": allow_duration_mismatch,
            "processing": {
                "fades": False,
                "normalization": False,
                "resampling": False,
                "channel_conversion": False,
                "output_subtype": "DOUBLE",
            },
            "evidence": "samples_and_declared_windows",
            "musical_verdict": None,
        }
        digest = _seal(stage, "trial.json", manifest)
        _publish(stage, destination)
        return {**manifest, "trial_dir": str(destination), "manifest_sha256": digest}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def record_feedback(
    trial_dir: str,
    variant_id: str,
    *,
    output_sha256: str,
    note: str,
    start_frame: int,
    end_frame: int,
    scope: FeedbackScope = "timing",
    listener: str | None = None,
) -> dict:
    """Append human feedback bound to one output hash and an exact local time span."""
    folder = Path(trial_dir).expanduser().resolve()
    manifest, digest = _load_sealed(folder, "trial.json", "pocket.transition-trial/v1")
    matches = [v for v in manifest["variants"] if v["id"] == variant_id]
    if len(matches) != 1:
        raise PocketError("Unknown or ambiguous variant_id")
    variant = matches[0]
    if scope not in SCOPES:
        raise PocketError(f"scope must be one of {', '.join(sorted(SCOPES))}")
    start = _integer(start_frame, "start_frame")
    end = _integer(end_frame, "end_frame", 1)
    if not start < end <= variant["frames"]:
        raise PocketError("Feedback interval must be inside the selected output, end-exclusive")
    actual = sha256_file(_safe_child(folder, variant["output_file"]))
    if actual != output_sha256 or actual != variant["output"]["sha256"]:
        raise PocketError("Output identity changed or feedback references a different output")
    feedback = {
        "schema": "pocket.listener-feedback/v1",
        "created_at": _now(),
        "trial_manifest_sha256": digest,
        "variant_id": variant_id,
        "output_sha256": actual,
        "start_frame": start,
        "end_frame": end,
        "sample_rate": variant["source"]["sample_rate"],
        "scope": scope,
        "note": _text(note, "note"),
        "listener": _text(listener, "listener", 120) if listener is not None else None,
        "evidence": "listener_supplied",
        "generalization": "selected_output_and_interval_only",
    }
    target = folder / "feedback"
    target.mkdir(exist_ok=True)
    filename = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ-") + uuid.uuid4().hex + ".json"
    feedback_hash = _seal(target, filename, feedback)
    return {**feedback, "feedback_file": str(target / filename), "feedback_sha256": feedback_hash}


def _read_als(path: Path) -> ET.Element:
    try:
        data = path.read_bytes()
        xml = gzip.decompress(data) if data.startswith(b"\x1f\x8b") else data
        if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
            raise PocketError("DTD/entity declarations are unsupported in Ableton XML")
        root = ET.fromstring(xml)
        if root.tag != "Ableton":
            raise PocketError("Expected Ableton XML root")
        return root
    except (OSError, ValueError, ET.ParseError, EOFError) as exc:
        raise PocketError(f"Cannot read Ableton set: {exc}") from exc


def _value(node, path: str) -> str:
    result = node.find(path)
    if result is None or "Value" not in result.attrib:
        raise PocketError(f"Unsupported Ableton schema: missing {path}")
    return result.get("Value")


def _clip(root, clip_id):
    match = re.fullmatch(r"track:(\d+)/clip:(\d+)", str(clip_id))
    if not match:
        raise PocketError("clip_id must be track:<native Id>/clip:<native Id>")
    found = [
        c
        for t in root.findall("LiveSet/Tracks/AudioTrack")
        if t.get("Id") == match[1]
        for c in t.findall(_AUDIO_PATH)
        if c.get("Id") == match[2]
    ]
    if len(found) != 1:
        raise PocketError("Audio Arrangement clip_id is missing or ambiguous; Session/MIDI are unsupported")
    return found[0]


def _constant_tempo(root, start: float, end: float) -> float:
    live = root.find("LiveSet")
    main = live.find("MainTrack")
    if main is None:
        main = live.find("MasterTrack")
    if main is None:
        raise PocketError("Missing Main track")
    tempo = main.find("DeviceChain/Mixer/Tempo")
    if tempo is None:
        raise PocketError("Missing Main tempo")
    manual = float(_value(tempo, "Manual"))
    target = tempo.find("AutomationTarget")
    envelopes = [
        e
        for e in main.findall("AutomationEnvelopes/Envelopes/AutomationEnvelope")
        if target is not None and _value(e, "EnvelopeTarget/PointeeId") == target.get("Id")
    ]
    if len(envelopes) > 1:
        raise PocketError("Duplicate tempo automation lanes")
    points = []
    if envelopes:
        for event in envelopes[0].findall("Automation/Events/*"):
            if event.tag != "FloatEvent" or list(event) or set(event.attrib) - {"Id", "Time", "Value"}:
                raise PocketError("Native trial adapter supports only plain linear tempo events")
            points.append((float(event.get("Time")), float(event.get("Value"))))
        if any(b[0] <= a[0] for a, b in pairwise(points)):
            raise PocketError("Tempo event times must strictly increase")
    if not all(math.isfinite(t) and math.isfinite(v) and v > 0 for t, v in points):
        raise PocketError("Invalid tempo points")
    if not math.isfinite(manual) or manual <= 0:
        raise PocketError("Invalid manual tempo")

    def at(time):
        if not points:
            return manual
        if time <= points[0][0]:
            return points[0][1]
        for (a, av), (b, bv) in pairwise(points):
            if time <= b:
                return av + (bv - av) * (time - a) / (b - a)
        return points[-1][1]

    values = [at(start), at(end)] + [v for t, v in points if start < t < end]
    if max(values) - min(values) > 1e-7:
        raise PocketError("Native trial export range must have constant tempo in this first adapter")
    return values[0]


def _check_relative_dependencies(root, set_path: Path) -> None:
    """Reject existing relative targets that could override verified absolute refs."""
    project_root = next(
        (
            parent
            for parent in (set_path.parent, *set_path.parent.parents)
            if (parent / "Ableton Project Info").is_dir()
        ),
        set_path.parent,
    )
    for ref in root.findall(".//SampleRef/FileRef") + root.findall(".//MxPatchRef/FileRef"):
        relative = ref.find("RelativePath")
        relative_type = ref.find("RelativePathType")
        if relative is None or not relative.get("Value"):
            continue
        if relative_type is None or relative_type.get("Value") not in {"1", "3"}:
            raise PocketError("Unsupported relative dependency reference type")
        absolute = Path(_value(ref, "Path")).expanduser()
        for base in {project_root, set_path.parent}:
            target = (base / relative.get("Value")).resolve()
            if target.is_file() and (not absolute.is_file() or not target.samefile(absolute)):
                raise PocketError("Conflicting project-relative and absolute dependency paths")


def _dependencies(root) -> list:
    # LastPresetRef is a historical preset pointer, not an active load dependency.
    # Active audio clips (including Session clips) and Max patches are verified.
    paths = set()
    for reference in root.findall(".//SampleRef/FileRef") + root.findall(".//MxPatchRef/FileRef"):
        path = Path(_value(reference, "Path")).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise PocketError("Native adapter requires existing absolute audio/Max dependency paths")
        paths.add(path.resolve())
    return [
        {"local_path": str(path), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}
        for path in sorted(paths)
    ]


def prepare_native_trial(
    source_als: str,
    output_dir: str,
    *,
    clip_id: str,
    shift_beats: float,
    export_start_beat: float,
    export_length_beats: float,
    expected_als_sha256: str,
    range_name: str = "Trial",
) -> dict:
    """Duplicate a set and translate one warped Audio Arrangement clip only.

    Host automation is fixed. Source/warp/clip-fade fields are unchanged; attached
    clip content travels with its clip. The named export range is an instruction,
    not an automated export or a change to the project's saved loop range.
    """
    source = Path(source_als).expanduser().resolve()
    before = _stamp(source)
    source_hash = sha256_file(source)
    if source_hash != expected_als_sha256:
        raise PocketError("Stale source ALS identity")
    root = _read_als(source)
    if root.find("LiveSet") is None or root.findall("LiveSet/Tracks/MidiTrack"):
        raise PocketError("First native adapter supports audio-only Ableton sets")
    if root.findall(".//PluginDevice"):
        raise PocketError("External plugin hosts are outside the native adapter's verified scope")
    selected = _clip(root, clip_id)
    if _value(selected, "IsWarped") != "true" or _value(selected, "Loop/LoopOn") != "false":
        raise PocketError("Selected clip must be warped and non-looping")
    if float(_value(selected, "Loop/StartRelative")) != 0:
        raise PocketError("Nonzero StartRelative is unsupported")
    if selected.findall("Envelopes/Envelopes/*") or selected.findall(".//ClipEnvelope"):
        raise PocketError("Clip-local automation is outside the controls-fixed adapter")
    start = float(_value(selected, "CurrentStart"))
    end = float(_value(selected, "CurrentEnd"))
    if not all(math.isfinite(x) for x in (start, end)) or end <= start:
        raise PocketError("Invalid clip geometry")
    if selected.get("Time") is None or abs(float(selected.get("Time")) - start) > 1e-8:
        raise PocketError("Clip Time and CurrentStart disagree")
    shift = _number(shift_beats, "shift_beats")
    if shift == 0 or abs(shift) > 16:
        raise PocketError("This adapter requires a nonzero shift of at most 16 beats")
    begin = _number(export_start_beat, "export_start_beat")
    length = _number(export_length_beats, "export_length_beats")
    if begin < 0 or length <= 0 or start + shift < 0:
        raise PocketError("Clip/export range crosses an unsupported negative project boundary")
    all_ends = [
        float(_value(c, "CurrentEnd")) for c in root.findall("LiveSet/Tracks/AudioTrack/" + _AUDIO_PATH)
    ]
    project_end = max(all_ends)
    if end + shift > project_end + 1e-8 or begin + length > project_end + 1e-8:
        raise PocketError("Trial cannot extend beyond the existing Arrangement audio boundary")
    if min(end, end + shift, begin + length) <= max(start, start + shift, begin):
        raise PocketError("Export range must overlap both original and shifted clip")
    bpm = _constant_tempo(root, begin, begin + length)
    if length * 60 / bpm > MAX_SECONDS:
        raise PocketError(f"Native trial export exceeds {MAX_SECONDS} seconds")
    destination = Path(output_dir).expanduser().resolve()
    _check_relative_dependencies(root, source)
    _check_relative_dependencies(root, destination / "candidate.als")
    dependencies = _dependencies(root)
    original = copy.deepcopy(root)
    changes = {
        "Time": {"before": selected.get("Time"), "after": format(start + shift, ".17g")},
        "CurrentStart": {"before": _value(selected, "CurrentStart"), "after": format(start + shift, ".17g")},
        "CurrentEnd": {"before": _value(selected, "CurrentEnd"), "after": format(end + shift, ".17g")},
    }
    selected.set("Time", changes["Time"]["after"])
    for name in ("CurrentStart", "CurrentEnd"):
        selected.find(name).set("Value", changes[name]["after"])
    stage = _staging(destination)
    try:
        candidate = stage / "candidate.als"
        candidate.write_bytes(
            gzip.compress(ET.tostring(root, encoding="utf-8", xml_declaration=True), mtime=0)
        )
        readback = _read_als(candidate)
        reverted = copy.deepcopy(readback)
        reverted_clip = _clip(reverted, clip_id)
        reverted_clip.set("Time", changes["Time"]["before"])
        for name in ("CurrentStart", "CurrentEnd"):
            reverted_clip.find(name).set("Value", changes[name]["before"])
        if ET.tostring(reverted) != ET.tostring(original):
            raise PocketError("Candidate readback contains changes outside the declared clip translation")
        if _stamp(source) != before or sha256_file(source) != source_hash:
            raise PocketError("Original ALS changed while preparing trial")
        manifest = {
            "schema": "pocket.native-trial/v1",
            "created_at": _now(),
            "source_als": {"local_path": str(source), "sha256": source_hash},
            "candidate_file": "candidate.als",
            "candidate_sha256": sha256_file(candidate),
            "clip_id": clip_id,
            "shift_beats": shift,
            "changes": changes,
            "controls_fixed": True,
            "fixed_controls_scope": "host automation and all unchanged XML fields",
            "clip_attached_content": "source, warp and clip-fade settings unchanged; travels with clip",
            "portability": False,
            "relative_reference_validation": "no_existing_conflict_at_original_or_candidate_location",
            "dependencies": dependencies,
            "export_range": {
                "name": _text(range_name, "range_name", 120),
                "start_beat": begin,
                "length_beats": length,
                "constant_bpm": bpm,
                "modeled_seconds": length * 60 / bpm,
            },
            "readback": {"generated_xml": "passed_only_declared_translation", "native_save": False},
            "render_status": "awaiting_supervised_native_export",
            "instruction": "Render the named range in Live. Keep candidate.als unchanged; Save As elsewhere "
            "if Live needs to save. Attach only the completed output with explicit settings.",
        }
        digest = _seal(stage, "native-trial.json", manifest)
        _publish(stage, destination)
        return {**manifest, "trial_dir": str(destination), "manifest_sha256": digest}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def attach_completed_render(
    trial_dir: str,
    rendered_audio: str,
    *,
    expected_candidate_sha256: str,
    rendered_start_beat: float,
    rendered_length_beats: float,
    expected_frames: int,
    settings: NativeExportSettings,
    export_completed: bool = False,
) -> dict:
    """Attach actual completed audio; export attribution remains user-supplied.

    Completion, range and settings are declarations, not proof that Live rendered
    the candidate. Audio/header/hash/readability checks are measured independently.
    """
    folder = Path(trial_dir).expanduser().resolve()
    manifest, digest = _load_sealed(folder, "native-trial.json", "pocket.native-trial/v1")
    if export_completed is not True:
        raise PocketError("Explicit completed-export confirmation is required")
    candidate = _safe_child(folder, manifest["candidate_file"])
    actual_candidate = sha256_file(candidate)
    if expected_candidate_sha256 != actual_candidate or actual_candidate != manifest["candidate_sha256"]:
        raise PocketError("Candidate changed after preparation; do not attach against stale intent")
    _check_relative_dependencies(_read_als(candidate), candidate)
    requested = manifest["export_range"]
    for actual, key in ((rendered_start_beat, "start_beat"), (rendered_length_beats, "length_beats")):
        if abs(_number(actual, key) - requested[key]) > 1e-8:
            raise PocketError("Declared rendered range differs from trial range")
    count = _integer(expected_frames, "expected_frames", 1)
    required = {
        "rendered_track": "Main",
        "normalization": False,
        "mono": False,
        "loop_render": False,
        "dither": "none",
    }
    if not isinstance(settings, dict) or any(settings.get(k) != v for k, v in required.items()):
        raise PocketError("Declare Main, normalization/mono/loop_render false and dither='none'")
    for key in ("normalization", "mono", "loop_render"):
        if settings[key] is not False:
            raise PocketError(f"{key} must be false")
    source = Path(rendered_audio).expanduser().resolve()
    before = _stamp(source)
    identity = identify_audio(source)
    if identity["format"] != "WAV" or identity["subtype"] not in ("FLOAT", "DOUBLE"):
        raise PocketError("Attach an undithered native floating-point WAV")
    if identity["channels"] != 2 or identity["channels"] != settings.get("channels"):
        raise PocketError("Native adapter expects explicitly declared stereo; no downmix")
    if identity["sample_rate"] != settings.get("sample_rate") or identity["frames"] != count:
        raise PocketError("Render header does not match declared sample rate/frame count")
    modeled_frames = requested["modeled_seconds"] * identity["sample_rate"]
    frame_delta = identity["frames"] - modeled_frames
    delta = frame_delta / identity["sample_rate"]
    if abs(frame_delta) > 2:
        raise PocketError("Render duration differs from modeled range by more than 2 frames")
    # Verify external dependencies are still the versions the candidate references.
    for dep in manifest["dependencies"]:
        if sha256_file(dep["local_path"]) != dep["sha256"]:
            raise PocketError("Native trial dependency changed; exported lineage is stale")
    peak, overs, decoded = 0.0, 0, 0
    with sf.SoundFile(source) as stream:
        for block in stream.blocks(blocksize=65536, dtype="float64", always_2d=True):
            if not np.isfinite(block).all():
                raise PocketError("Completed render contains non-finite audio")
            peak = max(peak, float(np.max(np.abs(block))))
            overs += int(np.count_nonzero(np.abs(block) >= 1))
            decoded += len(block)
    if decoded != count or _stamp(source) != before:
        raise PocketError("Render changed or full decoded frame count is incomplete")
    attachment_id = "render-" + uuid.uuid4().hex
    destination = folder / "renders" / attachment_id
    stage = _staging(destination)
    try:
        shutil.copyfile(source, stage / "render.wav")
        if sha256_file(stage / "render.wav") != identity["sha256"] or _stamp(source) != before:
            raise PocketError("Render changed during attachment")
        receipt = {
            "schema": "pocket.native-render-attachment/v1",
            "created_at": _now(),
            "trial_manifest_sha256": digest,
            "candidate_sha256": actual_candidate,
            "render_identity": identity,
            "render_file": "render.wav",
            "declared_export_range": {
                "start_beat": rendered_start_beat,
                "length_beats": rendered_length_beats,
            },
            "declared_settings": settings,
            "duration_delta_seconds": delta,
            "modeled_frames": modeled_frames,
            "frame_delta": frame_delta,
            "duration_tolerance_frames": 2,
            "signal": _peak_summary(peak, overs, decoded),
            "native_export_attribution": "user_supplied_not_observed_by_pocket",
            "native_save_verified": False,
            "completion": "explicit_confirmation_plus_stable_full_decode",
            "processing": "byte_copy_no_gain_no_fades",
            "musical_verdict": None,
        }
        receipt_hash = _seal(stage, "attachment.json", receipt)
        _publish(stage, destination)
        return {**receipt, "attachment_dir": str(destination), "receipt_sha256": receipt_hash}
    finally:
        if stage.exists():
            shutil.rmtree(stage)
