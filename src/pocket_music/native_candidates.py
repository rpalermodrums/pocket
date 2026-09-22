"""File-based, supervised MIDI candidate lifecycle, isolated from Stitch v2.

No function in this module controls Live. Native reports are attributed evidence,
not observations made by this provider. Unknown saved XML differences fail shut.
"""
from __future__ import annotations

import copy
import gzip
import json
import math
import os
import re
import shutil
import stat
import tempfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Literal

from .artifact_store import (
    ArtifactHandle,
    _verify_handles,
    canonical_bytes,
    digest,
    put_bytes,
    put_record,
    read_bytes,
    read_record,
    receipt,
    run_request,
)
from .assets import sha256_file
from .candidate_types import (
    CandidateContext,
    LayerSpec,
    NativeAbandonment,
    NativeReconciliation,
    NativeSaveReport,
)
from .errors import PocketError
from .native_normalization import BUILD, PROFILE, normalize_metadata, validate_environment
from .stitch import _check_relative_dependencies, _collect_dependencies, _constant_tempo, _read_als
from .thread import inspect_set


def _stamp(path: Path) -> list[int]:
    try:
        stat = path.stat()
    except OSError as error:
        raise PocketError(f"Missing candidate/source file: {path.name}") from error
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def _text(value, field, limit=240):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise PocketError(f"{field} must be nonempty text, at most {limit} characters")
    return value


def _time(value, label):
    if (not isinstance(value, dict) or set(value) != {"n", "d"}
            or any(isinstance(value.get(k), bool) or not isinstance(value.get(k), int) for k in ("n", "d"))
            or value["d"] <= 0 or math.gcd(value["n"], value["d"]) != 1):
        raise PocketError(f"{label} must be a reduced rational quarter-note value")
    return Fraction(value["n"], value["d"])


def _context(value):
    if not isinstance(value, dict) or set(value) != {
            "arrangement_start_qn", "length_qn", "tempo_bpm", "meter"}:
        raise PocketError("Native context requires arrangement_start_qn, length_qn, tempo_bpm and meter")
    if _time(value["arrangement_start_qn"], "arrangement_start_qn") < 0:
        raise PocketError("Native arrangement start must be nonnegative")
    if _time(value["length_qn"], "length_qn") <= 0:
        raise PocketError("Native context length must be positive")
    if value["tempo_bpm"] != 120 or value["meter"] != [4, 4]:
        raise PocketError("Initial native candidate profile requires confirmed 120 BPM and 4/4")
    return copy.deepcopy(value)


def _xml_value(node, path, default=None):
    child = node.find(path)
    return default if child is None else child.get("Value", default)


def _xml_semantics(node):
    # Whitespace and attribute serialization order have no XML semantic meaning.
    # Every element, attribute value, non-whitespace text and child order remains.
    return [node.tag, dict(sorted(node.attrib.items())), (node.text or "").strip(),
            (node.tail or "").strip(), [_xml_semantics(child) for child in node]]


def _parse_bytes(data):
    try:
        raw = gzip.decompress(data) if data.startswith(b"\x1f\x8b") else data
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise PocketError("DTD/entity declarations are unsupported")
        root = ET.fromstring(raw)
    except (ET.ParseError, OSError, EOFError) as error:
        raise PocketError(f"Malformed saved set: {error}") from error
    if root.tag != "Ableton" or root.find("LiveSet/Tracks") is None:
        raise PocketError("Expected saved Ableton LiveSet/Tracks")
    return root


def _safe(root, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise PocketError("Unsafe workspace/package path")
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise PocketError("Workspace/package path escapes its root")
    if any(p.is_symlink() for p in (path, *path.parents) if p != root.parent and p.is_relative_to(root)):
        raise PocketError("Workspace/package symlinks are unsupported")
    return path


def _folder(store_root, workspace_id):
    if not isinstance(workspace_id, str) or not re.fullmatch(r"workspace-[0-9a-f]{24}", workspace_id):
        raise PocketError("Invalid workspace ID")
    return _safe(Path(store_root).expanduser().resolve(), f"workspaces/{workspace_id}")


def _write_json(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        stream.write(canonical_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read_workspace_state(path):
    try:
        with path.open("rb") as stream:
            payload = stream.read(4 * 1024 * 1024 + 1)
        if len(payload) > 4 * 1024 * 1024:
            raise PocketError("Workspace state exceeds its metadata bound")
        state = json.loads(payload)
        required = {"schema", "workspace_id", "revision", "state", "preparation", "mutable_als", "history"}
        if (not isinstance(state, dict) or not required <= set(state) or type(state.get("revision")) is not int or
                state["revision"] < 0 or not isinstance(state.get("state"), str) or not state["state"] or
                not isinstance(state.get("history"), list) or any(
                not isinstance(entry, dict) for entry in state["history"])):
            raise PocketError("Invalid workspace state structure")
        canonical_bytes(state)
        return payload, state
    except PocketError:
        raise
    except (OSError, ValueError, RecursionError) as error:
        raise PocketError("Invalid workspace state") from error


def _workspace_owner(folder, descriptor=None):
    lock = _safe(folder, ".workspace-owner-lock")
    metadata = lock.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size != 0:
        raise PocketError("Stable workspace owner lock must be an empty regular file without aliases")
    if descriptor is not None:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise PocketError("Stable workspace owner lock was replaced")
    directory = _safe(folder, ".workspace-lock")
    if not directory.is_dir() or {p.name for p in directory.iterdir()} != {"owner.json"}:
        raise PocketError("Workspace is busy or interrupted; ownerless or unknown mutex cannot be recovered")
    marker = _safe(directory, "owner.json")
    marker_stat = marker.stat()
    if not stat.S_ISREG(marker_stat.st_mode) or marker_stat.st_nlink != 1 or marker_stat.st_size > 4096:
        raise PocketError("Workspace ownership marker must be a bounded regular file without aliases")
    try:
        with os.fdopen(os.open(marker, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
            opened_marker = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened_marker.st_mode) or opened_marker.st_nlink != 1 or
                    (opened_marker.st_dev, opened_marker.st_ino) != (marker_stat.st_dev, marker_stat.st_ino)):
                raise PocketError("Workspace ownership marker changed while opening")
            payload = stream.read(4097)
    except OSError as error:
        raise PocketError("Cannot safely read workspace ownership marker") from error
    current_marker = _safe(directory, "owner.json").stat()
    if ((current_marker.st_dev, current_marker.st_ino, current_marker.st_size, current_marker.st_mtime_ns, current_marker.st_nlink) !=
            (marker_stat.st_dev, marker_stat.st_ino, marker_stat.st_size, marker_stat.st_mtime_ns, 1)):
        raise PocketError("Workspace ownership marker changed during read")
    try:
        record = json.loads(payload)
    except (ValueError, UnicodeError) as error:
        raise PocketError("Invalid workspace ownership marker") from error
    required = {"schema", "workspace_id", "stable_device", "stable_inode", "acquisition_token"}
    if (len(payload) > 4096 or not isinstance(record, dict) or set(record) != required or
            record["schema"] != "pocket.workspace-lock-owner/v1" or record["workspace_id"] != folder.name or
            type(record["stable_device"]) is not int or type(record["stable_inode"]) is not int or
            (record["stable_device"], record["stable_inode"]) != (metadata.st_dev, metadata.st_ino) or
            not isinstance(record["acquisition_token"], str) or not re.fullmatch(r"[0-9a-f]{32}", record["acquisition_token"])):
        raise PocketError("Workspace ownership marker does not bind this stable lock inode")
    return record


@contextmanager
def _workspace(store_root, workspace_id, expected_revision, *, recover_native=False, recovery_terminal=None):
    try:
        import fcntl
    except ImportError as error:
        raise PocketError("Native workspace ownership requires a platform with POSIX file locks") from error
    folder = _folder(store_root, workspace_id)
    lock = folder / ".workspace-lock"
    stable = _safe(folder, ".workspace-owner-lock")
    try:
        descriptor = os.open(stable, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise PocketError("Workspace is missing, busy or interrupted; inspect before recovery") from error
    acquired = owned = False
    marker = None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size != 0:
            raise PocketError("Stable workspace owner lock must be an empty regular file without aliases")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as error:
            raise PocketError("Workspace is missing, busy or interrupted; another cooperating owner holds its lock") from error
        if (stable.stat().st_dev, stable.stat().st_ino) != (metadata.st_dev, metadata.st_ino):
            raise PocketError("Stable workspace owner lock was replaced")
        os.fsync(descriptor)
        if lock.exists():
            if not recover_native:
                raise PocketError("Workspace is missing, busy or interrupted; inspect before recovery")
            prior_owner = _workspace_owner(folder, descriptor)
            payload, previous = _read_workspace_state(folder / "workspace.json")
            if (type(expected_revision) is not int or previous["revision"] != expected_revision or
                    previous["schema"] != "pocket.workspace/v1" or previous["workspace_id"] != workspace_id):
                raise PocketError("Stale workspace revision or identity during lock recovery")
            if previous["state"] not in {"host_pending", "outcome_unknown"} and not (
                    previous["state"] == "awaiting_native" and recovery_terminal is not None and
                    previous.get("last_native_terminal") == recovery_terminal):
                raise PocketError("Physical lock recovery requires pending native work or its exact last terminal")
            recovery = put_record({"schema": "pocket.native-workspace-lock-recovery/v1", "workspace_id": workspace_id,
                                   "prior_owner": prior_owner, "prior_revision": previous["revision"],
                                   "prior_state_sha256": digest(previous),
                                   "ownership_evidence": "exclusive_stable_flock_acquired",
                                   "native_outcome": "not_inferred_from_process_lock"}, store_root)
            if _read_workspace_state(folder / "workspace.json")[0] != payload or _workspace_owner(folder, descriptor) != prior_owner:
                raise PocketError("Workspace changed during explicit lock recovery")
            revision = previous["revision"] + 1
            _write_json(folder / "workspace.json", {**previous, "revision": revision,
                "history": [*previous["history"], {"revision": revision, "state": "workspace_lock_recovered", "evidence": recovery}]})
            (lock / "owner.json").unlink()
            lock.rmdir()
            expected_revision = revision
        try:
            lock.mkdir()
        except FileExistsError as error:
            raise PocketError("Workspace mutex was acquired concurrently") from error
        marker = {"schema": "pocket.workspace-lock-owner/v1", "workspace_id": workspace_id,
                  "stable_device": metadata.st_dev, "stable_inode": metadata.st_ino,
                  "acquisition_token": os.urandom(16).hex()}
        _write_json(lock / "owner.json", marker)
        owned = True
        _, state = _read_workspace_state(folder / "workspace.json")
        if (isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
                or state.get("revision") != expected_revision):
            raise PocketError("Stale workspace revision")
        if state.get("schema") != "pocket.workspace/v1" or state.get("workspace_id") != workspace_id:
            raise PocketError("Workspace identity mismatch")
        yield folder, state
    finally:
        try:
            if owned:
                if _workspace_owner(folder, descriptor) != marker:
                    raise PocketError("Workspace ownership changed; retained mutex for inspection")
                (lock / "owner.json").unlink()
                lock.rmdir()
        finally:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


@contextmanager
def _workspace_snapshot(store_root, workspace_id, expected_revision):
    """Read an atomic state snapshot even when a crashed writer retained its lock."""
    folder = _folder(store_root, workspace_id)
    path = folder / "workspace.json"
    before, state = _read_workspace_state(path)
    if (isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or
            state.get("revision") != expected_revision):
        raise PocketError("Stale workspace revision")
    if state.get("schema") != "pocket.workspace/v1" or state.get("workspace_id") != workspace_id:
        raise PocketError("Workspace identity mismatch")
    yield folder, state
    if _read_workspace_state(path)[0] != before:
        raise PocketError("Workspace changed during inspection; read the current revision again")


def _verify_source(folder, preparation=None):
    try:
        environment = json.loads((folder / "environment.json").read_bytes())
    except (OSError, ValueError) as error:
        raise PocketError("Missing/corrupt workspace source environment") from error
    if preparation is not None and digest(environment) != preparation["source_environment_sha256"]:
        raise PocketError("Source environment differs from sealed preparation")
    for source in environment["sources"]:
        path = Path(source["path"])
        if _stamp(path) != source["stamp"] or sha256_file(path) != source["sha256"]:
            raise PocketError("Protected source changed since preparation")


def _verify_dependencies(folder, preparation, store_root):
    for dependency in preparation["dependencies"]:
        path = _safe(folder, dependency["relative_path"])
        if sha256_file(path) != dependency["artifact"]["sha256"]:
            raise PocketError("Workspace dependency changed or is missing")
        read_bytes(dependency["artifact"], store_root)


def _require_no_native_pending(state):
    if "native_pending" in state or state.get("state") in {"host_pending", "outcome_unknown"}:
        raise PocketError("Native operation is pending or uncertain; explicit terminal reconciliation is required")


def _sha(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise PocketError(f"{name} must be a lowercase SHA-256-sized identity")
    return value


def _native_nonce(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise PocketError("session_nonce must be the adapter's 32 lowercase hexadecimal characters")
    return value


def _load_native_pending(state, store_root):
    pending = read_record(state.get("native_pending"), store_root, "pocket.native-pending/v1")
    required = {"schema", "request_id", "request_key", "input_sha256", "session_nonce", "package_sha256",
                "operation", "workspace_id", "pending_revision", "expected_observation", "preparation",
                "saved_als", "saved_als_sha256", "saved_als_stamp"}
    if set(pending) != required:
        raise PocketError("Native pending record fields are invalid")
    if (pending["workspace_id"] != state["workspace_id"] or pending["preparation"] != state["preparation"] or
            type(pending["pending_revision"]) is not int or not 1 <= pending["pending_revision"] <= state["revision"] or
            pending["operation"] not in {"insert_empty", "set_velocity"}):
        raise PocketError("Native pending workspace/revision/operation identity mismatch")
    if not isinstance(pending["request_id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", pending["request_id"]):
        raise PocketError("Native pending request identity is invalid")
    for key in ("request_key", "input_sha256", "package_sha256", "saved_als_sha256"):
        _sha(pending[key], key)
    _native_nonce(pending["session_nonce"])
    stamp = pending["saved_als_stamp"]
    if not isinstance(stamp, list) or len(stamp) != 4 or any(type(n) is not int or n < 0 for n in stamp):
        raise PocketError("Native pending file stamp is invalid")
    read_record(pending["expected_observation"], store_root, "pocket.native-midi-observation/v1")
    _verify_handles(pending, store_root)
    return pending


def _native_terminal(handle, store_root):
    """Require the adapter's full journal/readback validator before any release.

    This lazy dependency is relevant only to native operation reconciliation.
    Standalone MIDI and existing supervised candidate operations do not load it.
    """
    record = read_record(handle, store_root, "pocket.native-midi-terminal/v1")
    _verify_handles(record, store_root)
    required = {"schema", "request_id", "request_key", "input_sha256", "session_nonce", "package_sha256",
                "operation", "workspace_id", "pending_revision", "expected_observation", "before_observation",
                "after_observation", "bridge_journal", "saved_als_sha256", "outcome", "dispatch_count"}
    if (set(record) != required or type(record["dispatch_count"]) is not int or
            type(record["pending_revision"]) is not int or record["pending_revision"] < 1):
        raise PocketError("Native terminal evidence fields are invalid")
    if record["outcome"] == "refused_before_dispatch":
        if record["dispatch_count"] != 0 or record["after_observation"] is not None:
            raise PocketError("Native refusal terminal has dispatch/readback contradictions")
    elif record["outcome"] == "verified_readback":
        if record["dispatch_count"] != 1 or record["after_observation"] is None:
            raise PocketError("Native write terminal requires one dispatch and after observation")
    else:
        raise PocketError("Unknown native outcomes cannot release a workspace")
    for key in ("expected_observation", "before_observation", "after_observation"):
        if record[key] is not None:
            read_record(record[key], store_root, "pocket.native-midi-observation/v1")
    read_bytes(record["bridge_journal"], store_root)
    try:
        from .native_midi import validate_native_terminal
    except ImportError as error:
        raise PocketError("Native terminal validator is unavailable; workspace remains quarantined") from error
    if validate_native_terminal(handle, store_root) != record:
        raise PocketError("Native terminal validator did not verify the exact supplied evidence")
    return record


def _match_native_terminal(record, pending):
    for key in ("request_id", "request_key", "input_sha256", "session_nonce", "package_sha256", "operation",
                "workspace_id", "pending_revision", "expected_observation", "saved_als_sha256"):
        if record[key] != pending[key]:
            raise PocketError(f"Native terminal differs from pending {key}")


class NativeWorkspace:
    """Internal shared transaction context; public native capabilities own dispatch."""

    def __init__(self, folder, state, store_root, path, expected_sha256, expected_stamp):
        self.folder = folder
        self.state = copy.deepcopy(state)
        self.store_root = store_root
        self.path = path
        self.expected_sha256 = expected_sha256
        self.expected_stamp = expected_stamp
        self.started = False
        self.native_dependencies = []
        self.lock_owner = _workspace_owner(folder) if (folder / ".workspace-lock").exists() else None

    def _check(self):
        if self.lock_owner is not None and _workspace_owner(self.folder) != self.lock_owner:
            raise PocketError("Native workspace lock ownership changed")
        _, current = _read_workspace_state(self.folder / "workspace.json")
        if current != self.state:
            raise PocketError("Native workspace changed while its shared lock was held")
        _safe(self.folder, str(self.path.relative_to(self.folder)))
        if not self.path.is_file() or self.path.stat().st_nlink != 1:
            raise PocketError("Native mutable set must be a regular copy without shared hard links")
        preparation = read_record(self.state["preparation"], self.store_root, "pocket.candidate-preparation/v1")
        _verify_handles(preparation, self.store_root)
        _verify_source(self.folder, preparation)
        _verify_dependencies(self.folder, preparation, self.store_root)
        if _stamp(self.path) != self.expected_stamp or sha256_file(self.path) != self.expected_sha256:
            raise PocketError("Saved native workspace changed during the pending operation")
        for dependency in self.native_dependencies:
            path = Path(dependency["path"])
            if any(node.is_symlink() for node in (path, *path.parents)):
                raise PocketError("Native writer package path changed to a symlink")
            if _stamp(path) != dependency["stamp"] or sha256_file(path) != dependency["sha256"]:
                raise PocketError("Native writer package changed during the pending operation")

    def validate_observation(self, observation_handle, writer_device):
        """Check the declared source and exact loaded writer before any dispatch.

        Current native guards, note-edit scope and freshness are the adapter's
        additional responsibility. This checks saved bytes and package files.
        """
        from .native_midi import load_native_midi_observation, load_native_writer_device

        self._check()
        if self.started:
            raise PocketError("Validate saved native source before beginning a write")
        _verify_handles(observation_handle, self.store_root)
        _verify_handles(writer_device, self.store_root)
        observation = load_native_midi_observation(observation_handle, self.store_root)
        package = load_native_writer_device(writer_device, self.store_root)
        binding = observation["saved_binding"]
        if (binding is None or binding["path"] != str(self.path) or binding["sha256"] != self.expected_sha256 or
                package["package_sha256"] != observation["package_sha256"]):
            raise PocketError("Native writer observation does not bind this saved workspace and package")
        if observation["target"]["device_track_runtime_id"] != observation["target"]["track_runtime_id"]:
            raise PocketError("Native writer device is not on the explicitly owned target track")
        preparation = read_record(self.state["preparation"], self.store_root, "pocket.candidate-preparation/v1")
        parent = _parse_bytes(read_bytes(preparation["prepared_als"], self.store_root))
        saved = _parse_bytes(read_bytes(binding["saved_als"], self.store_root))
        source_ids = {track.get("Id") for track in parent.findall("LiveSet/Tracks/*")}
        added = [track for track in saved.findall("LiveSet/Tracks/*") if track.get("Id") not in source_ids]
        if len(added) != 1 or added[0].tag != "MidiTrack" or added[0].get("Id") != binding["track_id"]:
            raise PocketError("Native observation does not identify the one declared owned MIDI track")
        track = added[0]
        if _xml_value(track, "Name/EffectiveName") != preparation["layer"]["track_name"]:
            raise PocketError("Native writer track name differs from declared layer")
        devices = track.findall("DeviceChain/DeviceChain/Devices/*")
        if [device.tag for device in devices] != ["Operator", "MxDeviceAudioEffect"]:
            raise PocketError("Native writer requires exactly Operator then its declared Max device")
        clips = track.findall(".//MidiClip")
        if len(clips) != 1 or clips[0].get("Id") != binding["clip_id"]:
            raise PocketError("Native writer requires the single declared clip")
        if _xml_value(clips[0], "GrooveSettings/GrooveId", "-1") != "-1":
            raise PocketError("Native writer clip must have no groove")
        refs = track.findall(".//MxPatchRef/FileRef")
        if (len(refs) != 1 or devices[1].find(".//MxPatchRef/FileRef") is not refs[0] or
                track.findall(".//SampleRef/FileRef") or track.findall(".//PluginDevice")):
            raise PocketError("Native writer cannot introduce unrelated active dependencies")
        _check_relative_dependencies(saved, self.path)
        absolute = _xml_value(refs[0], "Path", "")
        package_path = Path(absolute)
        if (not package_path.is_absolute() or str(package_path) != os.path.normpath(absolute) or
                package_path.name != "Native MIDI Writer.amxd" or
                any(node.is_symlink() for node in (package_path, *package_path.parents))):
            raise PocketError("Native writer requires an exact absolute nonsymlink package reference")
        dependencies = []
        for name, artifact in package["files"].items():
            path = package_path.parent / name
            if any(node.is_symlink() for node in (path, *path.parents)):
                raise PocketError("Native writer package must not contain symlinks")
            payload = read_bytes(artifact, self.store_root)
            stamp = _stamp(path)
            if (not path.is_file() or path.stat().st_nlink != 1 or stamp[2] != len(payload) or
                    path.read_bytes() != payload or _stamp(path) != stamp):
                raise PocketError("Loaded native writer package differs from its retained exact bytes")
            dependencies.append({"path": str(path), "sha256": artifact["sha256"], "stamp": stamp})
        environment = _normalization_environment(self.folder, self.path, preparation, self.store_root)
        protected = copy.deepcopy(saved)
        protected.find("LiveSet/Tracks").remove(protected.find("LiveSet/Tracks/MidiTrack"))
        protected, runtime = _runtime_identity(parent, protected, preparation, self.store_root, environment,
                                               observation_handle, binding["track_id"], binding["clip_id"])
        protected, metadata = normalize_metadata(parent, protected, environment, complete_saved=saved)
        if _xml_semantics(parent) != _xml_semantics(protected):
            raise PocketError("Native writer observation changed protected source")
        self.native_dependencies = dependencies
        self._check()
        return {"protected_source_xml": "matched_after_checked_metadata", "runtime_identity": runtime,
                "metadata": metadata, "writer_device": writer_device, "package_sha256": package["package_sha256"],
                "native_dispatch": False, "native_note_content": "adapter_must_validate"}

    def _persist(self, updated):
        if _read_workspace_state(self.folder / "workspace.json")[1] != self.state:
            raise PocketError("Native workspace state changed concurrently; refusing to overwrite it")
        _write_json(self.folder / "workspace.json", updated)
        self.state = updated

    def begin(self, *, request_id, request_key, operation, input_sha256, session_nonce,
              package_sha256, expected_observation):
        """Persist quarantine before a native dispatch can be acknowledged."""
        _require_no_native_pending(self.state)
        self._check()
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", request_id):
            raise PocketError("Native request_id must be a safe identifier")
        if operation not in {"insert_empty", "set_velocity"}:
            raise PocketError("Native operation is outside the bounded write profile")
        previous = [entry for entry in self.state["history"] if entry.get("state") == "host_pending"]
        if len(previous) >= 1000 or any(entry.get("request_id") == request_id for entry in previous):
            raise PocketError("Native request was already registered or workspace operation bound exceeded")
        for entry in previous:
            prior = _load_native_pending({**self.state, "native_pending": entry.get("pending")}, self.store_root)
            if prior["request_key"] == request_key:
                raise PocketError("Native request_key was already registered in this workspace")
        for key, value in (("request_key", request_key), ("input_sha256", input_sha256),
                           ("package_sha256", package_sha256)):
            _sha(value, key)
        _native_nonce(session_nonce)
        read_record(expected_observation, self.store_root, "pocket.native-midi-observation/v1")
        _verify_handles(expected_observation, self.store_root)
        revision = self.state["revision"] + 1
        pending = {"schema": "pocket.native-pending/v1", "request_id": request_id, "request_key": request_key,
                   "input_sha256": input_sha256, "session_nonce": session_nonce, "package_sha256": package_sha256,
                   "operation": operation, "workspace_id": self.state["workspace_id"], "pending_revision": revision,
                   "expected_observation": expected_observation, "preparation": self.state["preparation"],
                   "saved_als": str(self.path.relative_to(self.folder)), "saved_als_sha256": self.expected_sha256,
                   "saved_als_stamp": self.expected_stamp}
        handle = put_record(pending, self.store_root)
        updated = {**self.state, "state": "host_pending", "revision": revision, "native_pending": handle,
                   "history": [*self.state["history"], {"state": "host_pending", "revision": revision,
                                                       "request_id": request_id, "pending": handle}]}
        self._persist(updated)
        self.started = True
        return {"pending": handle, "pending_revision": revision}

    def finish(self, terminal, *, attribution=None, reconciliation_request_id=None):
        """Release only matching, adapter-verified terminal evidence; never dispatch."""
        self._check()
        if self.state["state"] not in {"host_pending", "outcome_unknown"}:
            raise PocketError("Workspace has no reconcilable pending native operation")
        pending = _load_native_pending(self.state, self.store_root)
        record = _native_terminal(terminal, self.store_root)
        _match_native_terminal(record, pending)
        self._check()
        revision = self.state["revision"] + 1
        entry = {"state": "awaiting_native", "revision": revision, "request_id": pending["request_id"],
                 "terminal": terminal, "outcome": record["outcome"]}
        if attribution is not None:
            entry.update(reconciliation=attribution, reconciliation_request_id=reconciliation_request_id)
        updated = {**self.state, "state": "awaiting_native", "revision": revision,
                   "last_native_terminal": terminal, "history": [*self.state["history"], entry]}
        updated.pop("native_pending")
        updated.pop("native_pending_error", None)
        self._persist(updated)
        return {"workspace_id": self.state["workspace_id"], "state": "awaiting_native", "revision": revision}

    def _retain_unknown(self, reason):
        if not self.started or "native_pending" not in self.state or self.state["state"] == "outcome_unknown":
            return
        revision = self.state["revision"] + 1
        self._persist({**self.state, "state": "outcome_unknown", "revision": revision,
                       "native_pending_error": str(reason)[:2000],
                       "history": [*self.state["history"], {"state": "outcome_unknown", "revision": revision,
                                                           "pending": self.state["native_pending"]}]})


@contextmanager
def native_workspace(store_root, workspace_id, expected_revision, saved_als, expected_sha256):
    """Hold the candidate's existing physical lock throughout native prepare/dispatch.

    An exception or normal exit without finish retains durable quarantine.
    Merely entering the context never starts a native operation.
    """
    with _workspace(store_root, workspace_id, expected_revision) as (folder, state):
        _require_no_native_pending(state)
        if state["state"] != "awaiting_native":
            raise PocketError("Native writes require an awaiting_native workspace")
        preparation = read_record(state["preparation"], store_root, "pocket.candidate-preparation/v1")
        if preparation["mode"] != "with_material":
            raise PocketError("Native writes require an explicitly owned with_material layer")
        path = Path(saved_als).expanduser().absolute()
        if not path.is_relative_to(folder.resolve()):
            raise PocketError("Native saved set must be inside its isolated workspace")
        path = _safe(folder, str(path.relative_to(folder)))
        session = NativeWorkspace(folder, state, store_root, path, _sha(expected_sha256, "expected_sha256"), _stamp(path))
        session._check()
        error = "Native context exited without verified terminal evidence"
        try:
            yield session
        except BaseException as caught:
            error = str(caught)
            raise
        finally:
            session._retain_unknown(error)


def candidate_native_reconcile(store_root: str, workspace_id: str, expected_revision: int,
                               terminal: ArtifactHandle, attribution: NativeReconciliation,
                               request_id: str) -> dict:
    """Explicitly reconcile retained native quarantine; never retry a host write."""
    try:
        from .native_midi import release_native_host_lease
    except ImportError as error:
        raise PocketError("Native host lease validator is unavailable; workspace remains quarantined") from error
    if not isinstance(attribution, dict) or set(attribution) != {"actor", "actor_kind", "observed_at", "reason"}:
        raise PocketError("Native reconciliation requires exact attributed actor/time/reason fields")
    _text(attribution["actor"], "actor")
    _text(attribution["reason"], "reason", 2000)
    if attribution["actor_kind"] not in {"human", "agent"}:
        raise PocketError("Native reconciliation actor_kind must be human or agent")
    try:
        if datetime.fromisoformat(attribution["observed_at"]).utcoffset() is None:
            raise ValueError()
    except (ValueError, TypeError):
        raise PocketError("Native reconciliation observed_at requires timezone") from None
    inputs = {"workspace_id": workspace_id, "expected_revision": expected_revision,
              "terminal": terminal, "attribution": attribution}

    def work():
        with _workspace(store_root, workspace_id, expected_revision, recover_native=True,
                        recovery_terminal=terminal) as (folder, state):
            pending_handle = state.get("native_pending")
            completed = pending_handle is None
            if completed:
                if state["state"] != "awaiting_native" or state.get("last_native_terminal") != terminal:
                    raise PocketError("Native reconciliation requires pending work or its exact last completed terminal")
                record = _native_terminal(terminal, store_root)
                matches = [entry["pending"] for entry in state["history"] if entry.get("state") == "host_pending" and
                           entry.get("request_id") == record["request_id"]]
                if len(matches) != 1:
                    raise PocketError("Completed native terminal has ambiguous pending history")
                pending_handle = matches[0]
            pending = _load_native_pending({**state, "native_pending": pending_handle}, store_root)
            path = _safe(folder, pending["saved_als"])
            session = NativeWorkspace(folder, state, store_root, path, pending["saved_als_sha256"], pending["saved_als_stamp"])
            if completed:
                session._check()
                _match_native_terminal(record, pending)
                workspace = {key: state[key] for key in ("workspace_id", "revision", "state")}
            else:
                workspace = session.finish(terminal, attribution=attribution, reconciliation_request_id=request_id)
            return receipt(request_id, artifacts={"native_terminal": terminal, "native_pending": pending_handle},
                           workspace=workspace,
                           coverage={"native_redispatched": False, "native_saved": False,
                                     "human_listening": "not_established", "terminal_previously_persisted": completed})

    result = run_request(store_root, request_id, "candidate_native_reconcile", inputs, work)
    record = _native_terminal(terminal, store_root)
    folder = _folder(store_root, workspace_id)
    _, state = _read_workspace_state(folder / "workspace.json")
    preparation = read_record(state["preparation"], store_root, "pocket.candidate-preparation/v1")
    _verify_source(folder, preparation)
    _verify_dependencies(folder, preparation, store_root)
    # The original immutable pending record supplies the exact saved path/stamp,
    # including on a completed replay after its mutable pointer was cleared.
    matches = [entry["pending"] for entry in state["history"] if entry.get("state") == "host_pending" and
               entry.get("request_id") == record["request_id"]]
    if matches != [result["artifacts"]["native_pending"]]:
        raise PocketError("Reconciled native pending history is missing or ambiguous")
    pending = _load_native_pending({**state, "native_pending": matches[0]}, store_root)
    _match_native_terminal(record, pending)
    path = _safe(folder, pending["saved_als"])
    if _stamp(path) != pending["saved_als_stamp"] or sha256_file(path) != pending["saved_als_sha256"]:
        raise PocketError("Native saved workspace changed since reconciliation")
    release_native_host_lease(terminal, store_root)
    return result


def _abandonment_attribution(attribution):
    declarations = {"all_old_live_max_instances_stopped", "old_writer_unloaded",
                    "old_candidate_closed_without_saving", "no_native_dispatch_in_flight"}
    if (not isinstance(attribution, dict) or
            set(attribution) != {"actor", "actor_kind", "observed_at", "reason"} | declarations):
        raise PocketError("Native abandonment requires exact attributed closure and supervision declarations")
    if any(attribution[key] is not True for key in declarations):
        raise PocketError("All old native sessions must be explicitly stopped, unloaded and closed without saving")
    _text(attribution["actor"], "actor")
    _text(attribution["reason"], "reason", 2000)
    if attribution["actor_kind"] not in {"human", "agent"}:
        raise PocketError("Abandonment actor_kind must be human or agent")
    try:
        if datetime.fromisoformat(attribution["observed_at"]).utcoffset() is None:
            raise ValueError()
    except (ValueError, TypeError):
        raise PocketError("Abandonment observed_at requires timezone") from None


def _abandonment_sources(session, pending, fresh_observation, *, require_recent=False):
    from .native_midi import load_native_midi_observation

    session._check()
    old = load_native_midi_observation(pending["expected_observation"], session.store_root)
    fresh = load_native_midi_observation(fresh_observation, session.store_root)
    _verify_handles(fresh_observation, session.store_root)
    binding = old["saved_binding"]
    if binding is None or binding["path"] != str(session.path) or binding["sha256"] != pending["saved_als_sha256"]:
        raise PocketError("Pending native observation differs from the unchanged old saved workspace")
    new = fresh["saved_binding"]
    if (new is None or fresh["session_nonce"] == pending["session_nonce"] or
            Path(new["path"]).is_relative_to(session.folder) or
            session.folder.is_relative_to(Path(new["path"]).parent)):
        raise PocketError("Abandonment requires another saved project and a different observed native session")
    stopped = fresh["guards"]["song"]["is_playing"]
    if stopped.get("status") != "available" or type(stopped.get("value")) is not int or stopped["value"] != 0:
        raise PocketError("Fresh native project must be observed stopped")
    if datetime.fromisoformat(fresh["read_started_at"]) <= datetime.fromisoformat(old["read_ended_at"]):
        raise PocketError("Abandonment observation must follow the old native observation")
    if require_recent:
        age = (datetime.now().astimezone() - datetime.fromisoformat(fresh["read_ended_at"])).total_seconds()
        if not -5 <= age <= 300:
            raise PocketError("Abandonment requires a native observation from the last five minutes")
    fresh_path = Path(new["path"])
    if any(path.is_symlink() for path in (fresh_path, *fresh_path.parents)):
        raise PocketError("Fresh saved project must not use symlink aliases")
    fresh_stamp = _stamp(fresh_path)
    if sha256_file(fresh_path) != new["sha256"] or _stamp(fresh_path) != fresh_stamp:
        raise PocketError("Fresh abandonment project differs from its retained saved bytes")
    preparation = read_record(session.state["preparation"], session.store_root, "pocket.candidate-preparation/v1")
    parent = _parse_bytes(read_bytes(preparation["prepared_als"], session.store_root))
    saved = _parse_bytes(read_bytes(binding["saved_als"], session.store_root))
    protected = copy.deepcopy(saved)
    tracks = protected.findall("LiveSet/Tracks/MidiTrack")
    if len(tracks) != 1 or tracks[0].get("Id") != binding["track_id"]:
        raise PocketError("Abandonment source has ambiguous owned MIDI layer")
    protected.find("LiveSet/Tracks").remove(tracks[0])
    environment = _normalization_environment(session.folder, session.path, preparation, session.store_root)
    protected, runtime = _runtime_identity(parent, protected, preparation, session.store_root, environment,
                                           pending["expected_observation"], binding["track_id"], binding["clip_id"])
    protected, metadata = normalize_metadata(parent, protected, environment, complete_saved=saved)
    if _xml_semantics(parent) != _xml_semantics(protected):
        raise PocketError("Abandonment cannot hide changed protected source")
    session._check()
    return {"protected_source_xml": "matched_after_checked_metadata", "runtime_identity": runtime,
            "metadata": metadata, "original_source_files": "unchanged_hashes_and_stamps",
            "collected_dependency_bytes": "unchanged",
            "old_saved_candidate": "unchanged_pending_hash_and_stamp", "native_outcome": "unknown",
            "closure": "attributed_supervision_not_process_death_proof"}


def candidate_native_abandon(store_root: str, workspace_id: str, expected_revision: int,
                             pending: ArtifactHandle, fresh_observation: ArtifactHandle,
                             attribution: NativeAbandonment, request_id: str,
                             bridge_dir: str | None = None) -> dict:
    """Permanently abandon supervised unknown native work; never undo or resume it.

    An unavailable old bridge is supporting evidence only. An ownerless physical
    workspace lock after a hard provider crash remains a refusal, never stolen.
    """
    try:
        from .native_midi import observe_native_abandonment, release_native_abandoned_lease
    except ImportError as error:
        raise PocketError("Supervised native abandonment evidence/release is unavailable") from error
    _abandonment_attribution(attribution)
    inputs = {"workspace_id": workspace_id, "expected_revision": expected_revision, "pending": pending,
              "fresh_observation": fresh_observation, "attribution": attribution, "bridge_dir": bridge_dir}

    def work():
        with _workspace(store_root, workspace_id, expected_revision, recover_native=True) as (folder, state):
            if state["state"] not in {"host_pending", "outcome_unknown"} or state.get("native_pending") != pending:
                raise PocketError("Abandonment requires the exact currently quarantined native pending identity")
            record = _load_native_pending(state, store_root)
            path = _safe(folder, record["saved_als"])
            session = NativeWorkspace(folder, state, store_root, path, record["saved_als_sha256"], record["saved_als_stamp"])
            sources = _abandonment_sources(session, record, fresh_observation, require_recent=True)
            recovery = observe_native_abandonment(pending, fresh_observation, store_root, bridge_dir)
            read_record(recovery, store_root, "pocket.native-abandonment-observation/v1")
            _verify_handles(recovery, store_root)
            session._check()
            revision = state["revision"] + 1
            artifact = put_record({"schema": "pocket.native-abandonment/v1", "workspace_id": workspace_id,
                                   "pending_revision": record["pending_revision"], "abandoned_revision": revision,
                                   "pending": pending, "fresh_observation": fresh_observation,
                                   "recovery_observation": recovery, "attribution": attribution,
                                   "outcome": "unknown", "policy": "permanently_unsealable_no_resume",
                                   "source_preservation": sources}, store_root)
            session._persist({**state, "state": "abandoned_native_unknown", "revision": revision,
                              "native_abandonment": artifact,
                              "history": [*state["history"], {"state": "abandoned_native_unknown", "revision": revision,
                                                              "request_id": request_id, "abandonment": artifact}]})
            return receipt(request_id, artifacts={"abandonment": artifact, "pending": pending},
                           workspace={"workspace_id": workspace_id, "revision": revision, "state": "abandoned_native_unknown"},
                           coverage={"native_outcome": "unknown", "native_rollback": False, "resumable": False,
                                     "sealable": False, "recovery": "explicit_supervised_abandonment",
                                     "human_listening": "not_established"})

    result = run_request(store_root, request_id, "candidate_native_abandon", inputs, work)
    folder = _folder(store_root, workspace_id)
    _, state = _read_workspace_state(folder / "workspace.json")
    artifact = result["artifacts"]["abandonment"]
    if (state["state"] != "abandoned_native_unknown" or state.get("native_abandonment") != artifact or
            state.get("native_pending") != pending or state["revision"] != result["workspace"]["revision"]):
        raise PocketError("Abandoned native workspace identity changed")
    record = _load_native_pending(state, store_root)
    path = _safe(folder, record["saved_als"])
    session = NativeWorkspace(folder, state, store_root, path, record["saved_als_sha256"], record["saved_als_stamp"])
    checked = _abandonment_sources(session, record, fresh_observation)
    abandonment = read_record(artifact, store_root, "pocket.native-abandonment/v1")
    if abandonment["source_preservation"] != checked:
        raise PocketError("Abandonment source preservation evidence changed")
    _verify_handles(artifact, store_root)
    release_native_abandoned_lease(artifact, store_root)
    return result


def _validate_material(handle, store_root):
    # Import lazily: candidate code cannot become a prerequisite of pure MIDI work.
    from .material import load_material
    record = load_material(handle, store_root)
    return _validate_material_profile(record)


def _validate_material_profile(record, *, check_wire_projection=True):
    if (len(record["clips"]) != 1 or record["curves"] or record["clips"][0]["loop"] or
            record["coverage"].get("editing_allowed") is False):
        raise PocketError("Initial native profile requires one unlooped clip and unambiguous conventional notes")
    if record["tempo_map_ref"] is not None or record["meter_map_ref"] is not None:
        raise PocketError("Native material tempo/meter maps require a separately qualified mapping")
    if record["events"] and check_wire_projection:
        _validate_native_note_events(record)
    for note in record["notes"]:
        if note.get("expression_refs") or note.get("mute") or note.get("pitch", {}).get("cents_offset", 0):
            raise PocketError("Native expression, muted notes and microtuning are not qualified")
        if note["channel"] != 1:
            raise PocketError("Initial stock native profile requires MIDI channel 1")
    return record


def _material_amendment(preparation, final_material, store_root):
    """Recompute a narrow amendment; caller-supplied claims are never trusted.

    Historical wire events remain source evidence. Only an already-qualified
    parent's one attack velocity may diverge from their original projection.
    """
    from .material import load_material

    if preparation["mode"] != "with_material" or preparation.get("material") is None:
        raise PocketError("final_material requires an existing with_material preparation")
    original = _validate_material(preparation["material"], store_root)
    final = load_material(read_record(final_material, store_root, "pocket.material/v1"), store_root)
    if final["material_id"] != original["material_id"] or final["parent_revision"] != original["revision_sha256"]:
        raise PocketError("Final material must be a direct child of the prepared material revision")
    mutable_metadata = {"revision_sha256", "parent_revision", "notes", "provenance"}
    if ({k: v for k, v in original.items() if k not in mutable_metadata} !=
            {k: v for k, v in final.items() if k not in mutable_metadata}):
        raise PocketError("Final material changed locked structure, source evidence, maps or coverage")
    if not isinstance(original["provenance"], dict) or not isinstance(final["provenance"], dict):
        raise PocketError("Final material requires object provenance")
    if ({k: v for k, v in original["provenance"].items() if k != "last_edit"} !=
            {k: v for k, v in final["provenance"].items() if k != "last_edit"}):
        raise PocketError("Final material changed locked provenance outside last_edit")
    if ([n["id"] for n in original["notes"]] != [n["id"] for n in final["notes"]]):
        raise PocketError("Final material must preserve every ordered note identity")
    changed = []
    for before, after in zip(original["notes"], final["notes"], strict=True):
        if any(set(note["velocity"]) != {"value", "domain"} or
               note["velocity"]["domain"] != "midi1_7bit" for note in (before, after)):
            raise PocketError("Final material attack velocity requires exact value/domain fields")
        if before == after:
            continue
        if ({**before, "velocity": after["velocity"]} != after or
                before["velocity"]["value"] == after["velocity"]["value"]):
            raise PocketError("Final material may change only one note's attack velocity; all other fields are locked")
        changed.append((before, after))
    if len(changed) != 1:
        raise PocketError("Final material must change exactly one note's attack velocity")
    # Base wire projection was checked above and ordered evidence was compared
    # exactly. This exception is confined to the proven one-velocity amendment.
    _validate_material_profile(final, check_wire_projection=False)
    before, after = changed[0]
    report = {"schema": "pocket.candidate-material-amendment/v1",
              "policy": "single_note_velocity/v1", "parent": preparation["material"], "child": final_material,
              "base_revision": original["revision_sha256"], "final_revision": final["revision_sha256"],
              "changed_note_id": before["id"], "velocity_before": before["velocity"],
              "velocity_after": after["velocity"],
              "invariants": {"ordered_note_ids": "exact", "outside_selected_velocity": "exact",
                             "structure_sources_events_curves_maps_coverage": "exact",
                             "original_material": "retained", "native_note_id_retention": "not_established",
                             "human_listening": "not_established"}}
    return final, report


def _validate_native_note_events(record):
    """Qualify exact note-only wire projections without making Mido mandatory.

    The wire remains in the material as source evidence. No controller, opaque
    metadata, ambiguous lifecycle or edited/raw mismatch is silently discarded.
    """
    events = sorted(record["events"], key=lambda event: event["order"])
    if (record["coverage"].get("editing_allowed") is not True or
            record["coverage"].get("issues") or
            record["coverage"].get("source_only_note_event_ids") or
            any(e["message_type"] not in {"note_on", "note_off", "end_of_track"} for e in events) or
            sum(e["message_type"] == "end_of_track" for e in events) != 1 or
            events[-1]["message_type"] != "end_of_track"):
        raise PocketError("Native wire projection supports only complete, exact note events and terminal end_of_track")
    times = [Fraction(e["time"]["n"], e["time"]["d"]) for e in events]
    if times != sorted(times) or times[-1] != _time(record["clips"][0]["length_qn"], "clip length"):
        raise PocketError("Native wire ordering or end_of_track differs from clip timing")
    by_id = {event["id"]: event for event in events}
    bound = []
    for note in record["notes"]:
        binding = note["source_binding"] or {}
        on, off = by_id.get(binding.get("on_event_id")), by_id.get(binding.get("off_event_id"))
        if on is None or off is None:
            raise PocketError("Native wire notes require exact on/off source bindings")
        if off["message_type"] == "note_on":
            raise PocketError("Native supervised import of note-on-zero release is known unsupported; Live resets release velocity")
        onset = Fraction(note["onset"]["n"], note["onset"]["d"])
        end = onset + Fraction(note["duration_qn"]["n"], note["duration_qn"]["d"])
        channel, pitch = note["channel"] - 1, note["pitch"]["midi_note"]
        if (binding.get("kind") != "smf" or on["order"] >= off["order"] or
                isinstance(binding.get("track_index"), bool) or not isinstance(binding.get("track_index"), int) or
                binding["track_index"] < 0 or
                on.get("track_index") != binding["track_index"] or off.get("track_index") != binding["track_index"] or
                on["message_type"] != "note_on" or on["bytes"] != [0x90 + channel, pitch, note["velocity"]["value"]] or
                off["message_type"] not in {"note_on", "note_off"} or
                off["bytes"] != [(0x90 if off["message_type"] == "note_on" else 0x80) + channel,
                                 pitch, note["release_velocity"]["value"]] or
                (off["message_type"] == "note_on" and note["release_velocity"]["value"] != 0) or
                Fraction(on["time"]["n"], on["time"]["d"]) != onset or
                Fraction(off["time"]["n"], off["time"]["d"]) != end):
            raise PocketError("Native wire events differ from the canonical note projection")
        bound.extend([on["id"], off["id"]])
    if len(bound) != len(set(bound)) or set(bound) != {e["id"] for e in events if e["message_type"] != "end_of_track"}:
        raise PocketError("Native wire note lifecycles must be a complete bijection")
    active = set()
    for event in events[:-1]:
        channel_pitch = tuple(event["bytes"][:2])
        key = (channel_pitch[0] & 15, channel_pitch[1])
        if event["message_type"] == "note_on" and event["bytes"][2] > 0:
            if key in active:
                raise PocketError("Native same-pitch wire overlaps are not qualified")
            active.add(key)
        elif key not in active:
            raise PocketError("Native wire release has no active note")
        else:
            active.remove(key)
    if active:
        raise PocketError("Native wire contains unterminated notes")


def candidate_prepare(source_als: str, expected_source_sha256: str, store_root: str,
                      request_id: str, mode: Literal["clone_only", "with_material"] = "clone_only",
                      material: ArtifactHandle | None = None, context: CandidateContext | None = None,
                      layer: LayerSpec | None = None) -> dict:
    """Copy a saved audio-only project; returns an isolated supervised workspace."""
    inputs = {"source_als": str(Path(source_als).expanduser().resolve()),
              "expected_source_sha256": expected_source_sha256, "mode": mode,
              "material": material, "context": context, "layer": layer}

    def work():
        source = Path(inputs["source_als"])
        original_stamp = _stamp(source)
        if sha256_file(source) != expected_source_sha256:
            raise PocketError("Stale source ALS hash")
        root = _read_als(source)
        if root.find("LiveSet/Tracks") is None:
            raise PocketError("Expected LiveSet/Tracks")
        if root.findall(".//MidiTrack") or root.findall(".//PluginDevice"):
            raise PocketError("Initial candidate profile requires an audio-only parent without external plugins")
        if mode not in {"clone_only", "with_material"}:
            raise PocketError("Unknown candidate preparation mode")
        checked_context = _context(context) if context is not None else None
        if checked_context is not None and _constant_tempo(root, 0, float(
                _time(checked_context["arrangement_start_qn"], "start") +
                _time(checked_context["length_qn"], "length"))) != 120:
            raise PocketError("Saved source tempo does not match the declared constant 120 BPM context")
        if mode == "with_material":
            if material is None or checked_context is None or not isinstance(layer, dict):
                raise PocketError("with_material requires material, context and layer")
            if set(layer) != {"track_name", "instrument_device"} or layer["instrument_device"] != "Operator":
                raise PocketError("Initial layer profile requires one named Operator instrument")
            _text(layer["track_name"], "track_name")
            actual_material = _validate_material(material, store_root)
            if _time(actual_material["clips"][0]["length_qn"], "material length") != _time(
                    checked_context["length_qn"], "context length"):
                raise PocketError("Material clip length differs from native context")
        elif material is not None or layer is not None:
            raise PocketError("clone_only does not add material or a layer")
        mapping = inspect_set(source, hash_sources=True)
        if any(d["runtime_dependency"] is None or
               (d["runtime_dependency"] and d["status"] != "present") for d in mapping["dependencies"]):
            raise PocketError("Unclassified or missing dependencies block initial native preparation")
        _check_relative_dependencies(root, source)
        workspace_id = "workspace-" + digest({"request_id": request_id, "source": expected_source_sha256})[:24]
        destination = _folder(store_root, workspace_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise PocketError("Workspace destination already exists")
        stage = Path(tempfile.mkdtemp(prefix=".prepare-", dir=destination.parent))
        try:
            dependencies, changes = _collect_dependencies(root, stage, destination)
            copied = gzip.compress(ET.tostring(root, encoding="utf-8", xml_declaration=True), mtime=0)
            (stage / "candidate.als").write_bytes(copied)
            source_handle = put_bytes(source.read_bytes(), store_root, "source.als", "pocket.saved-set/v1")
            prepared_handle = put_bytes(copied, store_root, "prepared.als", "pocket.saved-set/v1")
            dependency_records = [{"relative_path": d["relative_path"], "kind": d["kind"],
                                   "artifact": put_bytes(_safe(stage, d["relative_path"]).read_bytes(),
                                                         store_root, "dependency.bin", "pocket.binary-asset/v1")}
                                  for d in dependencies]
            context_handle = (put_record({"schema": "pocket.context/v1", **checked_context}, store_root)
                              if checked_context is not None else None)
            source_entries = [{"path": str(source), "sha256": expected_source_sha256, "stamp": original_stamp}]
            source_entries.extend({"path": d["source_path"], "sha256": d["sha256"],
                                   "stamp": _stamp(Path(d["source_path"]))} for d in dependencies)
            environment = {"schema": "pocket.local-environment/v1", "sources": source_entries}
            preparation = {"schema": "pocket.candidate-preparation/v1", "workspace_id": workspace_id,
                           "parent": source_handle, "prepared_als": prepared_handle, "mode": mode,
                           "material": material, "context": context_handle, "layer": layer,
                           "dependencies": dependency_records,
                           "source_environment_sha256": digest(environment),
                           "allowed_changes": [] if mode == "clone_only" else ["add_one_named_operator_midi_track"],
                           "reference_rebindings": [{"kind": c["kind"], "index": c["index"],
                                                      "relative_path": c["relative_path"]} for c in changes],
                           "normalization_profile": "none", "native_execution": "supervised_only"}
            preparation_handle = put_record(preparation, store_root)
            _write_json(stage / "environment.json", environment)
            state = {"schema": "pocket.workspace/v1", "workspace_id": workspace_id, "revision": 1,
                     "state": "awaiting_native", "preparation": preparation_handle,
                     "mutable_als": "candidate.als", "history": [{"revision": 0, "state": "prepared"},
                                                                  {"revision": 1, "state": "awaiting_native"}]}
            _write_json(stage / "workspace.json", state)
            _verify_source(stage, preparation)
            _verify_dependencies(stage, preparation, store_root)
            stage.rename(destination)
            return receipt(request_id, artifacts={"preparation": preparation_handle, "parent": source_handle},
                           workspace={"workspace_id": workspace_id, "revision": 1, "state": "awaiting_native",
                                      "mutable_als_path": str(destination / "candidate.als")},
                           coverage={"native_execution": False, "source_bytes_preserved": True,
                                     "mutable_files_are_copies": True},
                           next_actions=["Import declared material in this isolated copy, save and reopen; then seal"])
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    result = run_request(store_root, request_id, "candidate_prepare", inputs, work)
    folder = _folder(store_root, result["workspace"]["workspace_id"])
    preparation = read_record(result["artifacts"]["preparation"], store_root, "pocket.candidate-preparation/v1")
    _verify_source(folder, preparation)
    _verify_dependencies(folder, preparation, store_root)
    if not _safe(folder, "candidate.als").is_file():
        raise PocketError("Prepared workspace is missing its mutable candidate")
    return result


def _workspace_history(state, limit, cursor):
    if type(limit) is not int or not 0 <= limit <= 20:
        raise PocketError("history_limit must be an integer from 0 to 20")
    # Reject deep metadata before pretty JSON/deepcopy can amplify a small
    # retained state into an allocation proportional to nesting depth squared.
    pending = [(state, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 128:
            raise PocketError("Workspace metadata exceeds the supported depth of 128")
        if isinstance(value, dict):
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
    commitment = digest(state)
    offset = 0
    if cursor is not None:
        if not isinstance(cursor, str) or not re.fullmatch(r"history-v1\.[0-9a-f]{64}\.[0-9]{1,7}", cursor):
            raise PocketError("Invalid workspace history cursor")
        if not limit or cursor.split(".")[1] != commitment:
            raise PocketError("History cursor requires a page limit and the same workspace revision and state")
        offset = int(cursor.rsplit(".", 1)[1])
        if offset >= len(state["history"]):
            raise PocketError("Workspace history cursor is outside its history")
    rows = []
    for entry in state["history"][offset:offset + limit]:
        # The CLI's pretty ASCII JSON is larger than the compact UTF-8 MCP
        # representation; bound the larger public encoding without truncating.
        if len(json.dumps([*rows, entry], indent=2, allow_nan=False).encode("utf-8")) + 1 > 12 * 1024:
            if not rows:
                raise PocketError("One workspace history entry exceeds the 12 KiB page bound; retained state is unchanged")
            break
        rows.append(copy.deepcopy(entry))
    end = offset + len(rows)
    return {"schema": "pocket.workspace-history-page/v1", "total": len(state["history"]),
            "rows": rows, "next_cursor": f"history-v1.{commitment}.{end}"
            if limit and end < len(state["history"]) else None}


def candidate_inspect(store_root: str, workspace_id: str, expected_revision: int,
                      history_limit: int = 0, history_cursor: str | None = None) -> dict:
    """Validate current state; request bounded, revision-bound history pages explicitly."""
    with _workspace_snapshot(store_root, workspace_id, expected_revision) as (folder, state):
        history = _workspace_history(state, history_limit, history_cursor)
        preparation = read_record(state["preparation"], store_root, "pocket.candidate-preparation/v1")
        _verify_source(folder, preparation)
        _verify_dependencies(folder, preparation, store_root)
        if "native_pending" in state:
            _load_native_pending(state, store_root)
        if state["state"] == "sealed":
            trial = load_candidate_record(state["candidate"], store_root)
            if sha256_file(_safe(folder, state["mutable_als"])) != trial["saved_als_sha256"]:
                raise PocketError("Sealed workspace was edited; start a new workspace")
        summary = {key: copy.deepcopy(state[key]) for key in (
            "schema", "workspace_id", "revision", "state", "preparation", "mutable_als", "candidate",
            "native_pending", "last_native_terminal", "native_abandonment", "native_pending_error") if key in state}
        _verify_handles([summary, history], store_root)
        result = receipt(workspace=summary, history=history, coverage={"source_bytes_preserved": True,
                         "workspace_lock_present": (folder / ".workspace-lock").exists(),
                         "native_loading": "not_observed_by_provider"})
        if len(json.dumps(result, indent=2, allow_nan=False).encode("utf-8")) + 1 > 16 * 1024:
            raise PocketError("Workspace inspection exceeds the 16 KiB response bound; request a smaller history page")
        return result


def candidate_cancel(store_root: str, workspace_id: str, expected_revision: int, request_id: str) -> dict:
    """Mark an unsealed supervised workspace cancelled, preserving every file."""
    inputs = {"workspace_id": workspace_id, "expected_revision": expected_revision}

    def work():
        with _workspace(store_root, workspace_id, expected_revision) as (folder, state):
            _require_no_native_pending(state)
            if state["state"] != "awaiting_native":
                raise PocketError("Only an awaiting_native workspace may be cancelled")
            revision = state["revision"] + 1
            updated = {**state, "state": "cancelled", "revision": revision,
                       "history": [*state["history"], {"revision": revision, "state": "cancelled",
                                                       "request_id": request_id}]}
            _write_json(folder / "workspace.json", updated)
            return receipt(request_id, status="cancelled", artifacts={"preparation": state["preparation"]},
                           workspace={"workspace_id": workspace_id, "revision": revision, "state": "cancelled"},
                           coverage={"native_call_interrupted": False, "files_retained": True,
                                     "promotable": False})

    return run_request(store_root, request_id, "candidate_cancel", inputs, work)


def _native_report(report, expected_hash):
    required = {"actor", "actor_kind", "observed_at", "host_version", "saved_als_sha256",
                "saved", "reopened", "missing_media", "device_missing"}
    if not isinstance(report, dict) or not required <= set(report) or set(report) - required - {
            "instrument_recipe_verified", "evidence"}:
        raise PocketError("Native report has missing or unsupported fields")
    _text(report["actor"], "actor")
    _text(report["host_version"], "host_version")
    if report["actor_kind"] not in {"human", "agent"}:
        raise PocketError("Native report actor_kind must be human or agent")
    try:
        if datetime.fromisoformat(report["observed_at"]).utcoffset() is None:
            raise ValueError()
    except (ValueError, TypeError):
        raise PocketError("Native observed_at requires ISO8601 with timezone") from None
    for key in ("saved", "reopened", "missing_media", "device_missing"):
        if not isinstance(report[key], bool):
            raise PocketError(f"Native {key} must be a boolean")
    if report["saved_als_sha256"] != expected_hash:
        raise PocketError("Native save report has stale candidate hash")
    if not report["saved"] or not report["reopened"] or report["missing_media"] or report["device_missing"]:
        raise PocketError("Native saved/reopened with no missing media/device is required")
    return {**report, "evidence_kind": "attributed_native_save_reopen_report"}


def _notes(root):
    notes = []
    for keys in root.findall(".//KeyTrack"):
        pitch = int(_xml_value(keys, "MidiKey", "-1"))
        for event in keys.findall("Notes/MidiNoteEvent"):
            if event.get("IsEnabled", "true").lower() not in {"true", "1"}:
                raise PocketError("Muted native note outside initial profile")
            if event.get("Probability", "1") not in {"1", "1.0"}:
                raise PocketError("Probabilistic native note outside initial profile")
            if Fraction(event.get("VelocityDeviation", "0")) != 0:
                raise PocketError("Native velocity deviation outside initial profile")
            notes.append((pitch, Fraction(event.get("Time", "0")), Fraction(event.get("Duration", "0")),
                          Fraction(event.get("Velocity", "0")), Fraction(event.get("OffVelocity", "64"))))
    return sorted(notes)


def _normalization_environment(folder, path, preparation, store_root):
    local = json.loads((folder / "environment.json").read_bytes())
    _verify_source(folder, preparation)
    assets = []
    for dependency in preparation["dependencies"]:
        source = _safe(folder, dependency["relative_path"])
        content = read_bytes(dependency["artifact"], store_root)
        stamp = source.stat()
        if sha256_file(source) != dependency["artifact"]["sha256"] or len(content) != stamp.st_size:
            raise PocketError("Normalization asset evidence is stale")
        assets.append({"relative_path": dependency["relative_path"], "sha256": dependency["artifact"]["sha256"],
                       "size_bytes": stamp.st_size, "mtime_seconds": int(stamp.st_mtime)})
    return {"schema": "pocket.native-normalization-environment/v1", "profile": PROFILE,
            "source_directory": str(Path(local["sources"][0]["path"]).parent),
            "saved_directory": str(path.parent), "collected_assets": assets,
            "source_environment": local}


def _validate_normalization_evidence(preparation, store_root, environment):
    if environment is None:
        raise PocketError("Unknown protected source XML difference; native normalization is not qualified")
    assets = validate_environment(environment)
    source_environment = environment.get("source_environment")
    if (not isinstance(source_environment, dict) or
            digest(source_environment) != preparation.get("source_environment_sha256")):
        raise PocketError("Normalization source environment differs from sealed preparation")
    if (source_environment.get("schema") != "pocket.local-environment/v1" or
            not isinstance(source_environment.get("sources"), list) or not source_environment["sources"]):
        raise PocketError("Normalization requires the original source environment")
    origin = source_environment["sources"][0]
    if (origin.get("sha256") != preparation["parent"]["sha256"] or
            str(Path(origin["path"]).parent) != environment["source_directory"]):
        raise PocketError("Normalization source directory does not match preparation origin")
    if set(assets) != {d["relative_path"] for d in preparation["dependencies"]}:
        raise PocketError("Normalization asset ledger differs from collected dependencies")
    for dependency in preparation["dependencies"]:
        observed = assets[dependency["relative_path"]]
        if (observed["sha256"] != dependency["artifact"]["sha256"] or
                observed["size_bytes"] != len(read_bytes(dependency["artifact"], store_root))):
            raise PocketError("Normalization asset bytes do not match preparation")


def _runtime_identity(parent, preserved, preparation, store_root, environment, handle, track_id, clip_id):
    """Bind one retained reader observation to the original workspace and source."""
    from .native_midi import load_native_midi_observation
    from .native_runtime_identity import normalize_runtime_identity_values

    _validate_normalization_evidence(preparation, store_root, environment)
    _verify_handles(handle, store_root)
    observation = load_native_midi_observation(handle, store_root)
    binding = observation["saved_binding"]
    if not isinstance(binding, dict):
        raise PocketError("Runtime identity qualification requires retained saved-set binding")
    directory = Path(binding["path"]).parent
    if str(directory) != environment["saved_directory"] or directory.name != preparation["workspace_id"]:
        raise PocketError("Runtime identity observation is outside the original exact candidate workspace")
    if binding["track_id"] != track_id or binding["clip_id"] != clip_id:
        raise PocketError("Runtime identity observation does not bind the final owned track and clip")
    if binding["saved_als"]["artifact_schema"] != "pocket.live-set-bytes/v1":
        raise PocketError("Runtime identity observation requires a saved-set artifact")
    observed_saved = _parse_bytes(read_bytes(binding["saved_als"], store_root))
    observed_source = copy.deepcopy(observed_saved)
    added = observed_source.findall("LiveSet/Tracks/MidiTrack")
    if len(added) != 1 or added[0].get("Id") != track_id:
        raise PocketError("Runtime identity observation has ambiguous owned layer")
    observed_source.find("LiveSet/Tracks").remove(added[0])
    observed_source, observed_runtime = normalize_runtime_identity_values(
        parent, observed_source, observed_saved, observation)
    observed_source, observed_metadata = normalize_metadata(
        parent, observed_source, environment, complete_saved=observed_saved)
    if _xml_semantics(parent) != _xml_semantics(observed_source):
        raise PocketError("Runtime observation changed protected source XML")
    normalized, report = normalize_runtime_identity_values(parent, preserved, observed_saved, observation)
    return normalized, {**report, "observation_source_preservation": {
        "protected_source_xml": "matched_after_checked_metadata", "runtime_identity": observed_runtime,
        "metadata": observed_metadata, "source_environment": "bound_to_preparation",
        "owned_note_content": "not_compared_to_final_material"}}


def _preservation(prepared, saved, preparation, store_root, normalization_environment=None, final_material=None,
                  runtime_identity_observation=None):
    parent = _parse_bytes(prepared)
    child = _parse_bytes(saved)
    preserved = copy.deepcopy(child)
    expected_note_count = 0
    owned_track_id = owned_clip_id = None
    if preparation["mode"] == "with_material":
        original_ids = {t.get("Id") for t in parent.findall("LiveSet/Tracks/*")}
        added = [t for t in preserved.findall("LiveSet/Tracks/*") if t.get("Id") not in original_ids]
        if len(added) != 1 or added[0].tag != "MidiTrack":
            raise PocketError("Expected exactly one newly added MIDI track")
        track = added[0]
        layer = preparation["layer"]
        if _xml_value(track, "Name/EffectiveName", _xml_value(track, "Name/UserName", "")) != layer["track_name"]:
            raise PocketError("Added MIDI track name does not match declared layer")
        devices = track.findall("DeviceChain/DeviceChain/Devices/*")
        if len(devices) != 1 or devices[0].tag != "Operator" or track.findall(".//PluginDevice"):
            raise PocketError("Added layer must contain the declared stock Operator, without plugins")
        if track.findall(".//SampleRef/FileRef") or track.findall(".//MxPatchRef/FileRef"):
            raise PocketError("Added stock layer cannot introduce uncollected active dependencies")
        clips = track.findall(".//MidiClip")
        if len(clips) != 1:
            raise PocketError("Expected exactly one native MIDI clip")
        clip = clips[0]
        owned_track_id, owned_clip_id = track.get("Id"), clip.get("Id")
        context = read_record(preparation["context"], store_root, "pocket.context/v1")
        start = _time(context["arrangement_start_qn"], "start")
        length = _time(context["length_qn"], "length")
        if (Fraction(_xml_value(clip, "CurrentStart", clip.get("Time", "-1"))) != start or
                Fraction(_xml_value(clip, "CurrentEnd", "-1")) != start + length):
            raise PocketError("Native clip placement differs from context")
        if _xml_value(clip, "Loop/LoopOn", "false") not in {"false", "0"}:
            raise PocketError("Looped native clips are outside initial profile")
        if _xml_value(clip, "GrooveSettings/GrooveId", "-1") != "-1":
            raise PocketError("Native clip grooves are outside initial profile, including zero global amount")
        for marker in ("Loop/StartRelative", "Loop/LoopStart"):
            if Fraction(_xml_value(clip, marker, "0")) != 0:
                raise PocketError("Native clip playback origin differs from material origin")
        material = (_validate_material(preparation["material"], store_root) if final_material is None else
                    _material_amendment(preparation, final_material, store_root)[0])
        expected = sorted((n["pitch"]["midi_note"],
                           Fraction(n["onset"]["n"], n["onset"]["d"]),
                           Fraction(n["duration_qn"]["n"], n["duration_qn"]["d"]),
                           Fraction(n["velocity"]["value"]), Fraction(n["release_velocity"]["value"]))
                          for n in material["notes"])
        if _notes(clip) != expected:
            raise PocketError("Saved native notes differ from exact material; no tolerance has been qualified")
        expected_note_count = len(expected)
        preserved.find("LiveSet/Tracks").remove(track)
    normalization = None
    runtime_identity = None
    if runtime_identity_observation is not None:
        if preparation["mode"] != "with_material" or owned_track_id is None or owned_clip_id is None:
            raise PocketError("Runtime identity qualification requires the declared owned MIDI layer")
        preserved, runtime_identity = _runtime_identity(
            parent, preserved, preparation, store_root, normalization_environment, runtime_identity_observation,
            owned_track_id, owned_clip_id)
    if runtime_identity is not None or _xml_semantics(parent) != _xml_semantics(preserved):
        _validate_normalization_evidence(preparation, store_root, normalization_environment)
        preserved, normalization = normalize_metadata(parent, preserved, normalization_environment, complete_saved=child)
    if _xml_semantics(parent) != _xml_semantics(preserved):
        raise PocketError("Unknown protected source XML difference after normalization")
    report = {"protected_source_xml": "exact_semantic_match" if normalization is None else "matched_after_checked_metadata",
            "normalization_profile": "none" if normalization is None else PROFILE,
            "native_notes_matched": expected_note_count, "added_tracks": int(preparation["mode"] == "with_material"),
            "note_tolerance_qn": {"n": 0, "d": 1}, "instrument_sound": "attributed_report_only"}
    if normalization is not None:
        report["normalization"] = normalization
    if runtime_identity is not None:
        report["runtime_identity"] = runtime_identity
    return report


def _preservation_summary(preservation):
    """Keep detailed XML evidence in the trial instead of expanding tool receipts."""
    runtime = preservation.get("runtime_identity", {})
    observed = runtime.get("observation_source_preservation", {})
    return {**{key: copy.deepcopy(preservation[key]) for key in (
        "protected_source_xml", "normalization_profile", "native_notes_matched", "added_tracks",
        "note_tolerance_qn", "instrument_sound")},
        "metadata_fields_normalized": preservation.get("normalization", {}).get("changed_fields", 0),
        "runtime_identity_fields_normalized": runtime.get("changed_fields", 0),
        "observed_source_metadata_fields_normalized": observed.get("metadata", {}).get("changed_fields", 0)}


def candidate_seal(store_root: str, workspace_id: str, expected_revision: int,
                   saved_als: str, expected_sha256: str, native_report: NativeSaveReport,
                   request_id: str, instrument_state: ArtifactHandle | None = None,
                   final_material: ArtifactHandle | None = None,
                   runtime_identity_observation: ArtifactHandle | None = None) -> dict:
    """Seal only exact, preserved, saved/reopened candidates; reports stay attributed."""
    inputs = {"workspace_id": workspace_id, "expected_revision": expected_revision,
              "saved_als": saved_als, "expected_sha256": expected_sha256,
              "native_report": native_report, "instrument_state": instrument_state}
    # Preserve the exact existing idempotency identity when this additive input
    # is omitted (or explicitly null).
    if final_material is not None:
        inputs["final_material"] = final_material
    if runtime_identity_observation is not None:
        inputs["runtime_identity_observation"] = runtime_identity_observation

    def work():
        with _workspace(store_root, workspace_id, expected_revision) as (folder, state):
            _require_no_native_pending(state)
            if state["state"] != "awaiting_native":
                raise PocketError("Workspace must be awaiting_native; sealed/failed/cancelled work is immutable")
            path = Path(saved_als).expanduser().resolve()
            if not path.is_relative_to(folder.resolve()):
                raise PocketError("Saved candidate must be inside its isolated workspace")
            path = _safe(folder, str(path.relative_to(folder)))
            stamp = _stamp(path)
            if sha256_file(path) != expected_sha256:
                raise PocketError("Stale saved candidate hash")
            report = _native_report(native_report, expected_sha256)
            for evidence in native_report.get("evidence", []):
                read_bytes(evidence, store_root)
            preparation = read_record(state["preparation"], store_root, "pocket.candidate-preparation/v1")
            amendment = (_material_amendment(preparation, final_material, store_root)[1]
                         if final_material is not None else None)
            _verify_source(folder, preparation)
            _verify_dependencies(folder, preparation, store_root)
            if preparation["mode"] == "with_material" and (
                    instrument_state is None or native_report.get("instrument_recipe_verified") is not True):
                raise PocketError("Stock instrument state artifact and attributed recipe verification are required")
            if instrument_state is not None:
                read_record(instrument_state, store_root, "pocket.instrument-state/v1")
            payload = path.read_bytes()
            environment = (_normalization_environment(folder, path, preparation, store_root)
                           if dict(_parse_bytes(payload).attrib) == BUILD else None)
            preservation = _preservation(read_bytes(preparation["prepared_als"], store_root), payload,
                                         preparation, store_root, environment, final_material, runtime_identity_observation)
            candidate_als = put_bytes(payload, store_root, "candidate.als", "pocket.saved-set/v1")
            trial = {"schema": "pocket.native-trial/v3", "parent": preparation["parent"],
                     "preparation": state["preparation"], "material": final_material or preparation["material"],
                     "context": preparation["context"], "instrument_state": instrument_state,
                     "candidate_als": candidate_als, "saved_als_sha256": expected_sha256,
                     "dependencies": preparation["dependencies"], "preservation": preservation,
                     "native_evidence": report, "native_verification": "attributed_save_reopen",
                     "provider_native_observation": False, "portability": "editable_same_environment",
                     "relocated_native_reopen": "not_verified", "listening": "not_reviewed", "decision": None}
            if preservation["normalization_profile"] != "none":
                trial["normalization_environment"] = put_record(environment, store_root)
            if amendment is not None:
                trial["material_amendment"] = put_record(amendment, store_root)
            if runtime_identity_observation is not None:
                trial["runtime_identity_observation"] = runtime_identity_observation
            trial_handle = put_record(trial, store_root)
            _verify_source(folder, preparation)
            _verify_dependencies(folder, preparation, store_root)
            if _stamp(path) != stamp or sha256_file(path) != expected_sha256:
                raise PocketError("Candidate changed during sealing")
            history = [*state["history"]]
            revision = state["revision"]
            for lifecycle in ("saved_unverified", "verified", "sealed"):
                revision += 1
                history.append({"revision": revision, "state": lifecycle, "request_id": request_id})
            _write_json(folder / "workspace.json", {**state, "revision": revision, "state": "sealed",
                                                     "history": history, "candidate": trial_handle,
                                                     "mutable_als": str(path.relative_to(folder))})
            return receipt(request_id, artifacts={"candidate": trial_handle, "candidate_als": candidate_als},
                           workspace={"workspace_id": workspace_id, "revision": revision, "state": "sealed"},
                           coverage={"artifact_integrity": "verified", "native_verification": "attributed_save_reopen",
                                     "rendered_audio": "not_attached", "human_listening": "not_reviewed"},
                           change_summary=_preservation_summary(preservation))

    result = run_request(store_root, request_id, "candidate_seal", inputs, work)
    folder = _folder(store_root, workspace_id)
    saved = Path(saved_als).expanduser().resolve()
    if not saved.is_relative_to(folder.resolve()):
        raise PocketError("Saved candidate must remain inside its isolated workspace")
    saved = _safe(folder, str(saved.relative_to(folder)))
    if sha256_file(saved) != expected_sha256:
        raise PocketError("Saved candidate changed since the sealed request")
    trial = load_candidate_record(result["artifacts"]["candidate"], store_root)
    # Historical completed journals may contain the full proof. Project their
    # return without rewriting the retained journal, trial, or request identity.
    return {**result, "change_summary": _preservation_summary(trial["preservation"])}


def load_candidate_record(candidate: ArtifactHandle, store_root: str) -> dict:
    """Shared core: verify and load the complete immutable v3 trial and evidence."""
    trial = read_record(candidate, store_root, "pocket.native-trial/v3")
    _verify_handles(trial, store_root)
    qualifications = {"native_verification": "attributed_save_reopen", "provider_native_observation": False,
                      "portability": "editable_same_environment", "relocated_native_reopen": "not_verified",
                      "listening": "not_reviewed", "decision": None}
    for field, expected in qualifications.items():
        if field not in trial or type(trial[field]) is not type(expected) or trial[field] != expected:
            raise PocketError("Candidate native, listening, or decision qualifications differ from its supported profile")
    roles = {"parent": "pocket.saved-set/v1", "preparation": "pocket.candidate-preparation/v1",
             "material": "pocket.material/v1", "context": "pocket.context/v1",
             "instrument_state": "pocket.instrument-state/v1", "candidate_als": "pocket.saved-set/v1"}
    for key, family in roles.items():
        handle = trial.get(key)
        if handle is not None or key in {"parent", "preparation", "candidate_als"}:
            if not isinstance(handle, dict) or handle.get("artifact_schema") != family:
                raise PocketError(f"Candidate {key} requires {family}")
            read_bytes(handle, store_root)
    preparation = read_record(trial["preparation"], store_root, "pocket.candidate-preparation/v1")
    prepared_als = preparation.get("prepared_als")
    if not isinstance(prepared_als, dict) or prepared_als.get("artifact_schema") != "pocket.saved-set/v1":
        raise PocketError("Candidate prepared_als requires pocket.saved-set/v1")
    workspace_folder = _folder(store_root, preparation["workspace_id"])
    if workspace_folder.exists():
        _verify_source(workspace_folder, preparation)
        workspace_state = json.loads((workspace_folder / "workspace.json").read_bytes())
        if workspace_state["state"] == "sealed":
            if workspace_state.get("candidate") != candidate:
                raise PocketError("Sealed workspace candidate identity changed")
            current = _safe(workspace_folder, workspace_state["mutable_als"])
            if sha256_file(current) != trial["saved_als_sha256"]:
                raise PocketError("Sealed mutable candidate changed; start a new workspace")
    for key in ("parent", "context"):
        if trial.get(key) != preparation.get(key):
            raise PocketError("Candidate lineage does not match preparation")
    final_material = None
    if "material_amendment" in trial:
        amendment = read_record(trial["material_amendment"], store_root, "pocket.candidate-material-amendment/v1")
        final_material = trial["material"]
        if amendment != _material_amendment(preparation, final_material, store_root)[1]:
            raise PocketError("Candidate material amendment report mismatch")
    elif trial.get("material") != preparation.get("material"):
        raise PocketError("Candidate lineage does not match preparation")
    if not isinstance(trial.get("dependencies"), list) or trial["dependencies"] != preparation.get("dependencies"):
        raise PocketError("Candidate dependencies do not match preparation")
    for dependency in trial["dependencies"]:
        handle = dependency.get("artifact") if isinstance(dependency, dict) else None
        if not isinstance(handle, dict) or handle.get("artifact_schema") != "pocket.binary-asset/v1":
            raise PocketError("Candidate dependency requires pocket.binary-asset/v1")
        read_bytes(handle, store_root)
    if trial["saved_als_sha256"] != trial["candidate_als"]["sha256"]:
        raise PocketError("Candidate saved hash mismatch")
    _native_report({k: v for k, v in trial["native_evidence"].items() if k != "evidence_kind"},
                   trial["saved_als_sha256"])
    environment = (read_record(trial["normalization_environment"], store_root,
                               "pocket.native-normalization-environment/v1")
                   if trial.get("normalization_environment") is not None else None)
    checked = _preservation(read_bytes(preparation["prepared_als"], store_root),
                            read_bytes(trial["candidate_als"], store_root), preparation, store_root, environment,
                            final_material, trial.get("runtime_identity_observation"))
    if checked != trial["preservation"]:
        raise PocketError("Candidate preservation report mismatch")
    return trial


def validate_candidate(candidate: ArtifactHandle, store_root: str) -> dict:
    """Revalidate complete candidate evidence and return a concise public receipt."""
    trial = load_candidate_record(candidate, store_root)
    return receipt(artifacts={"candidate": candidate, "candidate_als": trial["candidate_als"]},
                   change_summary=_preservation_summary(trial["preservation"]),
                   coverage={"artifact_integrity": "verified", "native_verification": trial["native_verification"],
                             "provider_native_observation": trial["provider_native_observation"],
                             "rendered_audio": "not_evaluated_by_candidate_validation",
                             "human_listening": trial["listening"], "musical_decision": trial["decision"],
                             "portability": trial["portability"],
                             "relocated_native_reopen": trial["relocated_native_reopen"]})
