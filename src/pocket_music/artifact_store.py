"""Explicit, relocatable immutable artifacts and local request journals.

Domain providers validate content. This layer validates byte identity, containment,
and publication/retry boundaries; it never dispatches native operations.
"""
from __future__ import annotations

import contextlib
import contextvars
import copy
import functools
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from typing_extensions import TypedDict

from .errors import PocketError


class ArtifactHandle(TypedDict):
    __pydantic_config__ = {"extra": "forbid", "strict": True}  # noqa: RUF012 - TypedDict schema metadata
    schema: Literal["pocket.artifact-handle/v1"]
    artifact_uri: str
    sha256: str
    artifact_schema: str


# Budgets for one call's verified snapshot. Past them, reads are simply verified again.
SNAPSHOT_BYTES = 256 * 1024 * 1024
MEMO_ENTRIES = 4096
_SCOPE = contextvars.ContextVar("pocket_verification_scope", default=None)


class _Snapshot:
    __slots__ = ("loads", "open", "owner", "payloads", "roots", "size")

    def __init__(self):
        self.payloads, self.size, self.loads, self.roots = {}, 0, {}, {}
        self.open, self.owner = True, threading.get_ident()


def _active():
    """The caller's open snapshot. A copied context or another thread never reuses one."""
    scope = _SCOPE.get()
    if scope is None or not scope.open or scope.owner != threading.get_ident():
        return None
    return scope


@contextlib.contextmanager
def verification_scope():
    """Bound one public call to an immutable snapshot of what it verified.

    Inside the outermost scope, an identical handle in the same store returns the
    bytes already hash-verified during this call, and a memoized domain load
    returns a fresh copy of its already-validated result. Only the thread that
    opened the scope uses it, and it is closed and emptied when the scope exits,
    even if a context copied inside it survives: the next call re-reads and
    re-verifies everything. Graph-walk accounting, bounds and errors are unchanged.
    """
    if _active() is not None:
        yield
        return
    snapshot = _Snapshot()
    token = _SCOPE.set(snapshot)
    try:
        yield
    finally:
        snapshot.open = False
        snapshot.payloads.clear()
        snapshot.loads.clear()
        snapshot.roots.clear()
        _SCOPE.reset(token)


def _scoped_root(scope, store_root):
    """Resolve a store root once per scope, so every spelling of one store shares one identity."""
    if scope is None or not isinstance(store_root, (str, Path)):
        return _root(store_root)
    spelled = os.fspath(store_root)
    key = ("", spelled) if os.path.isabs(spelled) else (os.getcwd(), spelled)
    if key not in scope.roots:
        scope.roots[key] = _root(store_root)
    return scope.roots[key]


def per_call_verification(function):
    """Run a public provider inside its own verification scope."""
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        with verification_scope():
            return function(*args, **kwargs)
    return wrapper


def _is_handle(value):
    return (isinstance(value, dict) and value.get("schema") == "pocket.artifact-handle/v1"
            and set(value) == {"schema", "artifact_uri", "sha256", "artifact_schema"})


def call_memo(kind):
    """Memoize a pure (handle, store_root) validator for the current call only."""
    def decorate(function):
        @functools.wraps(function)
        def wrapper(handle, store_root, *args, **kwargs):
            scope = _active()
            if scope is None or args or kwargs or MEMO_ENTRIES <= 0 or not _is_handle(handle):
                return function(handle, store_root, *args, **kwargs)
            try:
                key = (kind, str(_scoped_root(scope, store_root)), canonical_bytes(handle))
            except (PocketError, OSError, RuntimeError, ValueError, TypeError):
                # The validator itself raises exactly the error it always raised.
                return function(handle, store_root)
            if key not in scope.loads:
                result = function(handle, store_root)
                if len(scope.loads) >= MEMO_ENTRIES:
                    return result
                scope.loads[key] = result
            # Callers own their copy; the validated original is never mutated.
            return copy.deepcopy(scope.loads[key])
        return wrapper
    return decorate


def canonical_bytes(value) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, OverflowError, RecursionError) as error:
        raise PocketError(f"Not finite JSON data: {error}") from error


def digest(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _root(store_root: str | Path) -> Path:
    if not isinstance(store_root, (str, Path)) or not str(store_root):
        raise PocketError("store_root must be an explicit local directory")
    return Path(store_root).expanduser().resolve()


def _contained(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise PocketError("Artifact URI must be relative to store_root")
    if any(part in ("..", ".") for part in relative.split("/")) or "\\" in relative:
        raise PocketError("Unsafe artifact URI")
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise PocketError("Artifact escapes store_root")
    # Reject aliases even if the link remains within the store.
    if any(p.is_symlink() for p in (path, *list(path.parents)[:len(Path(relative).parts)])):
        raise PocketError("Artifact paths must not use symlinks")
    return path


def read_bytes(handle: ArtifactHandle, store_root: str | Path) -> bytes:
    if (not isinstance(handle, dict) or handle.get("schema") != "pocket.artifact-handle/v1" or
            set(handle) != {"schema", "artifact_uri", "sha256", "artifact_schema"}):
        raise PocketError("Expected pocket.artifact-handle/v1")
    sha = handle.get("sha256")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise PocketError("Invalid artifact SHA-256")
    if not isinstance(handle.get("artifact_schema"), str):
        raise PocketError("Missing artifact schema")
    uri = handle.get("artifact_uri")
    # Validate the literal URI before Path can normalize repeated separators or
    # other aliases. Handles must retain the bounded form emitted by put_bytes.
    if not isinstance(uri, str) or not re.fullmatch(
            rf"artifacts/{sha}/[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}", uri):
        raise PocketError("Artifact URI must be a canonical content address")
    scope = _active()
    root = _scoped_root(scope, store_root)
    key = (str(root), uri)
    if scope is not None and key in scope.payloads:
        # Already contained, read and hash-verified in this call; serve that snapshot.
        return scope.payloads[key]
    path = _contained(root, uri)
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise PocketError(f"Cannot read artifact: {error}") from error
    if hashlib.sha256(payload).hexdigest() != sha:
        raise PocketError("Artifact integrity mismatch")
    if scope is not None and scope.size + len(payload) <= SNAPSHOT_BYTES:
        scope.payloads[key] = payload
        scope.size += len(payload)
    return payload


def read_record(handle: ArtifactHandle, store_root: str | Path, expected_schema: str | None = None) -> dict:
    try:
        record = json.loads(read_bytes(handle, store_root))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise PocketError(f"Invalid artifact JSON: {error}") from error
    canonical_bytes(record)
    if not isinstance(record, dict) or record.get("schema") != handle["artifact_schema"]:
        raise PocketError("Artifact schema mismatch")
    if expected_schema is not None and record.get("schema") != expected_schema:
        raise PocketError(f"Expected {expected_schema}")
    return record


def put_bytes(payload: bytes, store_root: str | Path, filename: str, artifact_schema: str) -> ArtifactHandle:
    if not isinstance(payload, bytes) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", filename):
        raise PocketError("Expected bytes and a safe artifact filename")
    if not isinstance(artifact_schema, str) or not artifact_schema:
        raise PocketError("artifact_schema is required")
    root = _root(store_root)
    sha = hashlib.sha256(payload).hexdigest()
    uri = f"artifacts/{sha}/{filename}"
    destination = _contained(root, uri)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive file creation ensures existing artifacts are never overwritten.
    temporary = destination.parent / (".publish-" + os.urandom(12).hex())
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Link only immutable temporary bytes, then unlink staging. Mutable
            # projects/presets are always copied by their domain provider.
            os.link(temporary, destination)
        except FileExistsError:
            if destination.read_bytes() != payload:
                raise PocketError("Content address collision or modified artifact")
    finally:
        temporary.unlink(missing_ok=True)
    return {"schema": "pocket.artifact-handle/v1", "artifact_uri": uri,
            "sha256": sha, "artifact_schema": artifact_schema}


def put_record(record: dict, store_root: str | Path) -> ArtifactHandle:
    if not isinstance(record, dict) or not isinstance(record.get("schema"), str):
        raise PocketError("Artifact record requires a schema")
    return put_bytes(canonical_bytes(record), store_root, "record.json", record["schema"])


def receipt(request_id=None, status="ok", artifacts=None, change_summary=None,
            coverage=None, warnings=None, uncertainty=None, next_actions=None, **extra) -> dict:
    if status not in {"ok", "needs_input", "unsupported", "conflict", "cancelled", "partial", "failed",
                      "outcome_unknown"}:
        raise PocketError("Unknown receipt status")
    result = {"schema": "pocket.operation-receipt/v1", "request_id": request_id, "status": status,
              "artifacts": artifacts if artifacts is not None else {},
              "change_summary": change_summary if change_summary is not None else {},
              "coverage": coverage if coverage is not None else {},
              "warnings": warnings if warnings is not None else [],
              "uncertainty": uncertainty if uncertainty is not None else [],
              "provenance": {"provider": "pocket", "contract": "midi-v1", "canonicalizer": "json-v1"},
              "next_actions": next_actions if next_actions is not None else [], **extra}
    canonical_bytes(result)
    return result


def _verify_handles(value, root):
    # Receipts often contain a plan whose evidence is another record. Verify
    # that graph on replay too; an intact outer JSON is not intact provenance.
    pending = [(value, 0)]
    seen = set()
    visited = metadata_bytes = 0
    while pending:
        current, depth = pending.pop()
        visited += 1
        if depth > 128 or visited > 1000000 or len(seen) > 10000:
            raise PocketError("Artifact evidence graph exceeds verification bounds")
        if isinstance(current, dict):
            if current.get("schema") == "pocket.artifact-handle/v1":
                identity = canonical_bytes(current)
                if identity in seen:
                    continue
                payload = read_bytes(current, root)
                seen.add(identity)
                # put_record reserves this filename. Raw MIDI, audio, presets
                # and source bytes remain opaque; domain readers validate them.
                if Path(current["artifact_uri"]).name == "record.json":
                    metadata_bytes += len(payload)
                    if metadata_bytes > 64 * 1024 * 1024:
                        raise PocketError("Artifact metadata exceeds verification bounds")
                    try:
                        record = json.loads(payload)
                    except (ValueError, UnicodeError, RecursionError) as error:
                        raise PocketError("Invalid referenced artifact JSON") from error
                    if not isinstance(record, dict) or record.get("schema") != current["artifact_schema"]:
                        raise PocketError("Referenced artifact schema mismatch")
                    canonical_bytes(record)
                    pending.append((record, depth + 1))
            else:
                pending.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            pending.extend((child, depth + 1) for child in current)


def _atomic_json(path: Path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(canonical_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run_request(store_root: str | Path, request_id: str, operation: str, inputs: dict,
                work: Callable[[], dict]) -> dict:
    """Execute one file operation; interrupted/failed requests require inspection.

    The exclusive request lock is deliberately never stolen. It is not a DAW
    lease. A crash leaves a retained journal and refuses automatic redispatch.
    """
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", request_id):
        raise PocketError("request_id must be 1–96 safe identifier characters")
    root = _root(store_root)
    folder = _contained(root, f"requests/{request_id}")
    folder.mkdir(parents=True, exist_ok=True)
    identity = digest({"operation": operation, "inputs": inputs})
    lock = folder / "lock"
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise PocketError("Request is active or interrupted; inspect its journal before retry", code="request_not_complete") from error
    journal_path = folder / "journal.json"
    try:
        if journal_path.exists():
            try:
                journal = json.loads(journal_path.read_bytes())
            except (ValueError, OSError) as error:
                raise PocketError("Request journal is corrupt; inspect before recovery") from error
            if journal.get("input_sha256") != identity:
                raise PocketError("idempotency_conflict: request ID has different inputs", code="idempotency_conflict")
            if journal.get("state") != "complete":
                raise PocketError("Request did not complete; inspect retained artifacts and use a new request ID", code="request_not_complete")
            result = journal["receipt"]
            if digest(result) != journal.get("receipt_sha256"):
                raise PocketError("Request receipt integrity mismatch", code="evidence_mismatch")
            _verify_handles(inputs, root)
            _verify_handles(result, root)
            return result
        _verify_handles(inputs, root)
        journal = {"schema": "pocket.request-journal/v1", "request_id": request_id,
                   "operation": operation, "input_sha256": identity, "state": "started"}
        _atomic_json(journal_path, journal)
        try:
            result = work()
            canonical_bytes(result)
            _verify_handles(result, root)
            _atomic_json(journal_path, {**journal, "state": "complete", "receipt": result,
                                        "receipt_sha256": digest(result)})
            return result
        except BaseException as error:
            _atomic_json(journal_path, {**journal, "state": "failed", "error": str(error)[:2000]})
            raise
    finally:
        shutil.rmtree(lock)


def request_status(store_root: str, request_id: str) -> dict:
    """Inspect a retained request without redispatching or stealing its lock.

    Completed artifact integrity is rechecked. External native/file state still
    requires the corresponding candidate or provider validator before resuming.
    """
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", request_id):
        raise PocketError("request_id must be 1–96 safe identifier characters")
    root = _root(store_root)
    folder = _contained(root, f"requests/{request_id}")
    path = _contained(root, f"requests/{request_id}/journal.json")
    locked = (folder / 'lock').exists()
    if not path.exists():
        return receipt(request_id, status='outcome_unknown' if locked else 'needs_input',
                       journal_state='lock_without_journal' if locked else 'not_found',
                       coverage={'redispatched': False, 'lock_present': locked},
                       next_actions=['Inspect the owning process before any retry'] if locked else
                                    ['No recorded request exists in this store'])
    try:
        with path.open('rb') as stream:
            payload = stream.read(16 * 1024 * 1024 + 1)
        if len(payload) > 16 * 1024 * 1024:
            raise PocketError('Request journal exceeds inspection bounds')
        journal = json.loads(payload)
    except (OSError, ValueError, RecursionError) as error:
        raise PocketError('Request journal is unreadable or corrupt') from error
    canonical_bytes(journal)
    if not isinstance(journal, dict) or journal.get('schema') != 'pocket.request-journal/v1' or journal.get('request_id') != request_id:
        raise PocketError('Request journal identity mismatch')
    state = journal.get('state')
    if not isinstance(state, str) or state not in {'started', 'failed', 'complete'}:
        raise PocketError('Unknown request journal state')
    base_fields = {'schema', 'request_id', 'operation', 'input_sha256', 'state'}
    extra_fields = {'complete': {'receipt', 'receipt_sha256'}, 'failed': {'error'}, 'started': set()}[state]
    if set(journal) != base_fields | extra_fields:
        raise PocketError('Unexpected request journal fields')
    if not isinstance(journal.get('operation'), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', journal['operation']):
        raise PocketError('Invalid request journal operation')
    if not isinstance(journal.get('input_sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', journal['input_sha256']):
        raise PocketError('Invalid request journal input identity')
    if state == 'failed' and (not isinstance(journal['error'], str) or len(journal['error']) > 2000):
        raise PocketError('Invalid bounded request error')
    result_status = None
    if state == 'complete':
        result = journal.get('receipt')
        if not isinstance(result, dict) or digest(result) != journal.get('receipt_sha256'):
            raise PocketError('Request receipt integrity mismatch', code='evidence_mismatch')
        _verify_handles(result, root)
        result_status = result.get('status', 'outcome_unknown')
        if not isinstance(result_status, str) or result_status not in {
                'ok', 'needs_input', 'unsupported', 'conflict', 'cancelled', 'partial',
                'failed', 'outcome_unknown'}:
            raise PocketError('Unknown recorded operation status')
    return receipt(request_id, status='outcome_unknown' if locked or state == 'started' else
                   ('failed' if state == 'failed' else result_status), journal_state=state,
                   receipt_status=result_status,
                   operation=journal.get('operation'), journal_sha256=digest(journal),
                   receipt_sha256=journal.get('receipt_sha256'),
                   error=str(journal.get('error', ''))[:2000] or None,
                   coverage={'redispatched': False, 'lock_present': locked,
                             'artifact_integrity': 'verified' if state == 'complete' else 'not_complete',
                             'external_state': 'requires_provider_revalidation'},
                   next_actions=['Revalidate external state through the original provider before resuming'] if state == 'complete' else
                                ['Inspect retained files and owning process; do not steal the lock or replay partial work'])
