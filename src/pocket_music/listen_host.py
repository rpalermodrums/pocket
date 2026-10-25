# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Literal

from .artifact_store import canonical_bytes, digest, put_record, read_record
from .error_contracts import CODES
from .errors import PocketError, contained, valid_hint
from .listen_native import (
    ADAPTER_PROFILES,
    REGISTRY_ENV,
    START_REFUSALS,
    SYNTHETIC_PROFILE,
    is_private,
    registry_dir,
    verify_build,
)

SESSION_POINTER = "listen-session.json"
SESSION_POINTER_SCHEMA = "pocket.listen-session-pointer/v1"
SESSION_SCHEMA = "pocket.listen-session/v1"
LAUNCH_SCHEMA = "pocket.listen-launch/v1"
CONNECTION_SCHEMA = "pocket.listen-connection/v1"
STARTUP_FAILURE_SCHEMA = "pocket.listen-startup-failure/v1"
TERMINAL_SCHEMA = "pocket.listen-terminal/v1"
TERMINAL_POINTER_SCHEMA = "pocket.listen-terminal-pointer/v1"
JOURNAL_SCHEMA = "pocket.listen-journal-entry/v1"

LEASE_NAME = "runtime.lock"
RUNTIMES = "runtimes"
LAUNCH_NAME = "launch-request.json"
CONNECTION_NAME = "connection.json"
JOURNAL_NAME = "journal.ndjson"
STDERR_NAME = "host.stderr"
STARTUP_FAILURE_NAME = "startup-failure.json"
TERMINAL_POINTER_NAME = "terminal-handle.json"
SOCKET_NAME = "host.sock"

MAX_ORDINAL = 9999
JOURNAL_BUDGET_BYTES = 16 * 1024 * 1024
JOURNAL_LINE_BYTES = 9 * 1024
JOURNAL_QUEUE_LINES = 4096
JOURNAL_FSYNC_S = 5.0
JOURNAL_MARKER_RESERVE_BYTES = 512
REQUEST_BYTES = 64 * 1024
RESPONSE_BYTES = 512 * 1024
MAX_CONNECTIONS = 8
STOP_BOUND_MS = 5000
SEAL_RESERVE_MS = 500
STDERR_CAP_BYTES = 1024 * 1024
START_WAIT_S = 10.0
CAPTURE_READ_NS = 50_000_000
LOOP_WAIT_NS = 50_000_000

EXIT_STOPPED, EXIT_FAILED, EXIT_SESSION, EXIT_REFUSED, EXIT_CONFLICT = 0, 1, 2, 3, 4
OPS = ("ping", "observe", "events", "stop")
REFUSAL_REASONS = (*START_REFUSALS, "master_channels_not_exposed", "no_metered_channels_exposed",
                   "helper_build_changed", "helper_protocol_error", "helper_start_timeout", "helper_exited",
                   "stale_after_below_block_period", "device_changed_before_start")
_EVENT_ARGS = {"after", "routes", "kinds", "max_events", "max_bytes", "wait_ms", "max_evidence_age_ms", "on_expired"}
_ZERO_SHA = "0" * 64
_RUNTIME_DIR = re.compile(r"(\d{4})-(rt-[0-9a-f]{16})")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
_SOL_LOCAL = 0      # SOL_LOCAL from <sys/un.h>; Python exposes LOCAL_PEERCRED but no SOL_LOCAL constant

_SESSION_HINT = "Pass the session directory listen_prepare created, unchanged; keep the directory together."
_PRIVATE_HINT = ("Listen's runtime directory must stay private to you (0700). Stop this runtime and start a new "
                 "one; don't change its permissions.")
_STALE_EPOCH_HINT = "Call listen_observe for the current runtime_epoch; listen_stop never stops a different runtime."
_TERMINAL_HINT = ("A sealed terminal record changed, so Listen doesn't trust it. Keep the session directory "
                  "unchanged; start a new runtime for fresh evidence.")
_REQUEST_HINT = "Send one JSON line {op, nonce, args} of at most 64 KiB, with the nonce from this runtime's connection.json."
_NONCE_HINT = "Read connection.json in this runtime's directory again; each runtime has its own nonce."
_BUSY_HINT = "Retry after current calls finish; at most 8 run at once."
_CONTROL_HINT = "Listen exposes no control operations; use ping, observe, events or stop."
_POSIX_HINT = "Listen's runtime runs on macOS or Linux; the synthetic adapter needs no other setup there."


def _fcntl():
    if os.name != "posix":
        raise PocketError("Listen's runtime needs a POSIX system", code="unsupported_profile", hint=_POSIX_HINT)
    import fcntl
    return fcntl


def _utc():
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _mismatch(message, hint=_SESSION_HINT):
    return PocketError(message, code="evidence_mismatch", hint=hint)


def _create_private(path):
    return os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600), "wb",
                     buffering=0)


def _seal(path, value):
    data = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    with _create_private(path) as stream:
        stream.write(data)
    with _create_private(str(path) + ".sha256") as stream:
        stream.write((hashlib.sha256(data).hexdigest() + "\n").encode())


def _read_sealed(path, schema, limit=1024 * 1024):
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(limit + 1)
        seal = Path(str(path) + ".sha256").read_text().strip()
        value = json.loads(data)
    except (OSError, ValueError) as error:
        raise _mismatch(f"Can't read the sealed {Path(path).name}") from error
    if len(data) > limit or hashlib.sha256(data).hexdigest() != seal or not isinstance(value, dict) \
            or value.get("schema") != schema:
        raise _mismatch(f"The sealed {Path(path).name} changed")
    return value


def _write_private_json(path, value):
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def session_root(session_dir):
    if not isinstance(session_dir, (str, Path)) or not str(session_dir):
        raise _mismatch("session_dir must name a directory listen_prepare created")
    return Path(session_dir).expanduser().resolve()


def load_session(session_dir):
    root = session_root(session_dir)
    try:
        pointer = json.loads((root / SESSION_POINTER).read_bytes())
        if not isinstance(pointer, dict) or set(pointer) != {"schema", "session"} \
                or pointer["schema"] != SESSION_POINTER_SCHEMA:
            raise PocketError("not a Listen session pointer")
        record = read_record(pointer["session"], root, SESSION_SCHEMA)
    except (OSError, PocketError, ValueError) as error:
        raise _mismatch(f"Can't verify the Listen session in {root}") from error
    config = record.get("config")
    adapter = config.get("adapter") if isinstance(config, dict) else None
    kind = adapter.get("kind") if isinstance(adapter, dict) else None
    profile = record.get("adapter_profile")
    build = record.get("build")
    if (not isinstance(config, dict) or record.get("config_sha256") != digest(config)
            or not isinstance(record.get("session_id"), str)
            or not isinstance(kind, str) or (kind, profile) not in ADAPTER_PROFILES.items()
            or (kind == "coreaudio") != isinstance(build, dict)):
        raise _mismatch("The Listen session record isn't consistent")
    return root, pointer["session"], record


def _open_lease(root):
    try:
        return os.open(root / LEASE_NAME, os.O_RDWR | os.O_NOFOLLOW)
    except OSError as error:
        raise _mismatch(f"The session's {LEASE_NAME} is missing or unreadable") from error


def acquire_lease(session_dir, *, retry_s=0.1):
    fcntl = _fcntl()
    fd = _open_lease(session_root(session_dir))
    deadline = time.monotonic() + retry_s
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return None
                time.sleep(0.01)
    except BaseException:
        os.close(fd)
        raise


def lease_held(session_dir):
    fcntl = _fcntl()
    fd = _open_lease(session_root(session_dir))
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def runtime_dirs(session_dir):
    base = session_root(session_dir) / RUNTIMES
    try:
        names = os.listdir(base)
    except FileNotFoundError:
        return []
    matches = ((_RUNTIME_DIR.fullmatch(name), name) for name in names)
    return sorted((int(m[1]), m[2], base / name) for m, name in matches if m)


def newest_runtime(session_dir):
    rows = runtime_dirs(session_dir)
    return rows[-1] if rows else None


def allocate_runtime(session_dir):
    root = session_root(session_dir)
    newest = newest_runtime(root)
    ordinal = 1 if newest is None else newest[0] + 1
    if ordinal > MAX_ORDINAL:
        raise PocketError(f"This session has used all {MAX_ORDINAL} runtime ordinals",
                          hint="Prepare a new session directory.")
    epoch = "rt-" + secrets.token_hex(8)
    base = root / RUNTIMES
    base.mkdir(mode=0o700, exist_ok=True)
    _check_private(base, "dir")
    path = base / f"{ordinal:04d}-{epoch}"
    path.mkdir(mode=0o700, exist_ok=False)
    os.chmod(path, 0o700)
    return ordinal, epoch, path


JOURNAL_CHECK_KEYS = ("present", "verified_lines", "last_sequence", "chain_ok", "torn_tail_bytes",
                      "dropped_lines_marked", "stopped_writing")


def verify_journal(path, max_lines=200000):
    result = {"present": False, "verified_lines": 0, "last_sequence": 0, "chain_ok": False,
              "torn_tail_bytes": 0, "head_sha256": None, "dropped_lines_marked": 0, "stopped_writing": None}
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(JOURNAL_BUDGET_BYTES + JOURNAL_LINE_BYTES + 1)
    except FileNotFoundError:
        return result
    except OSError:
        return {**result, "present": True}
    *lines, tail = data.split(b"\n")
    verified, last_sequence, head, ok = 0, 0, _ZERO_SHA, len(data) <= JOURNAL_BUDGET_BYTES + JOURNAL_LINE_BYTES
    marked, stopped = 0, None
    for raw in lines[:max_lines] if ok else ():
        try:
            entry = json.loads(raw)
            well_formed = (isinstance(entry, dict) and canonical_bytes(entry) == raw
                           and set(entry) == {"schema", "line", "prev_sha256", "monotonic_ns", "entry"}
                           and entry["schema"] == JOURNAL_SCHEMA and entry["line"] == verified + 1
                           and entry["prev_sha256"] == head and isinstance(entry["entry"], dict))
        except (ValueError, PocketError):
            well_formed = False
        if not well_formed:
            ok = False
            break
        event = entry["entry"].get("event")
        if isinstance(event, dict) and type(event.get("sequence")) is int:
            last_sequence = max(last_sequence, event["sequence"])
        written = entry["entry"]
        if written.get("journal") == "lines_dropped" and type(written.get("dropped_lines")) is int:
            marked += written["dropped_lines"]
        if written.get("journal") == "stopped_writing" and written.get("state") in ("exhausted", "failed"):
            stopped = written["state"]
        verified, head = verified + 1, hashlib.sha256(raw).hexdigest()
    ok = ok and len(lines) <= max_lines
    return {"present": True, "verified_lines": verified, "last_sequence": last_sequence, "chain_ok": ok,
            "torn_tail_bytes": len(tail), "head_sha256": head if verified else None,
            "dropped_lines_marked": marked, "stopped_writing": stopped}


def previous_runtime(session_dir):
    newest = newest_runtime(session_dir)
    if newest is None:
        return None
    _, epoch, path = newest
    journal = verify_journal(path / JOURNAL_NAME)
    try:
        terminal = "sealed" if read_terminal(session_dir, path) is not None else "missing"
    except PocketError:
        terminal = "integrity_mismatch"
    return {"runtime_epoch": epoch, "terminal": terminal, "journal": {k: journal[k] for k in JOURNAL_CHECK_KEYS}}


def prepare_launch(session_dir, *, request_id, launch):
    if launch not in ("provider", "foreground"):
        raise ValueError("launch is provider or foreground")
    root, _, session = load_session(session_dir)
    previous = previous_runtime(root)
    ordinal, epoch, path = allocate_runtime(root)
    record = {"schema": LAUNCH_SCHEMA, "session_id": session["session_id"], "config_sha256": session["config_sha256"],
              "runtime_epoch": epoch, "ordinal": ordinal, "request_id": request_id, "launch": launch,
              "previous_runtime": previous, "wall_utc": _utc()}
    _seal(path / LAUNCH_NAME, record)
    return path, record


def read_launch(runtime_dir):
    return _read_sealed(Path(runtime_dir) / LAUNCH_NAME, LAUNCH_SCHEMA)


def host_argv(session_dir, runtime_dir, lease_fd):
    return [sys.executable, "-P", "-m", "pocket_music.listen_host", "--session-dir",
            str(session_root(session_dir)), "--runtime-dir", str(runtime_dir), "--lease-fd", str(lease_fd)]


def _site_directories():
    import site
    found = [*site.getsitepackages(), site.getusersitepackages()]
    return {str(Path(folder).resolve()) for folder in found if folder}


def host_env(environ=None):
    """An installed package's folder is site-packages itself, which the daemon's interpreter
    (this one) already reads after the standard library; on PYTHONPATH it would come before it."""
    base = os.environ if environ is None else environ
    package = str(Path(__file__).resolve().parents[1])
    paths = [*([] if package in _site_directories() else [package]), *filter(None, [base.get("PYTHONPATH")])]
    rest = {k: v for k, v in base.items() if k != "PYTHONPATH"}
    return {**rest, **({"PYTHONPATH": os.pathsep.join(paths)} if paths else {}),
            REGISTRY_ENV: str(registry_dir(base))}


def spawn_host(session_dir, runtime_dir, lease_fd):
    runtime = Path(runtime_dir)
    with _create_private(runtime / STDERR_NAME) as stderr:
        process = subprocess.Popen(host_argv(session_dir, runtime, lease_fd),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr,
                                   pass_fds=(lease_fd,), start_new_session=True, cwd=runtime, env=host_env())
    threading.Thread(target=process.wait, daemon=True).start()
    return process


class RuntimeUnreachable(Exception):
    reason: Literal["lease_held_no_connection", "socket_unreachable", "invalid_response"]

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _LineTooLong(ValueError):
    pass


def _read_line(sock, limit):
    data = bytearray()
    while True:
        chunk = sock.recv(min(65536, limit + 1 - len(data)))
        if not chunk:
            return None
        newline = chunk.find(b"\n")
        data += chunk if newline < 0 else chunk[:newline]
        if len(data) > limit:
            raise _LineTooLong(f"a line holds at most {limit} bytes")
        if newline >= 0:
            return bytes(data)


def _check_private(path, kind):
    info = os.lstat(path)
    shape = {"dir": stat.S_ISDIR, "file": stat.S_ISREG, "socket": stat.S_ISSOCK}[kind]
    if not is_private(info, shape, os.fspath(path)):
        raise PocketError(f"Listen's runtime {kind} {Path(path).name} isn't private to you",
                          code="evidence_mismatch", hint=_PRIVATE_HINT)


def read_connection(runtime_dir):
    runtime = Path(runtime_dir)
    try:
        _check_private(runtime, "dir")
    except FileNotFoundError as error:
        raise _mismatch("The runtime directory is missing", _PRIVATE_HINT) from error
    path = runtime / CONNECTION_NAME
    try:
        _check_private(path, "file")
        with path.open("rb") as stream:
            data = stream.read(4097)
    except FileNotFoundError as error:
        raise RuntimeUnreachable("lease_held_no_connection") from error
    try:
        value = json.loads(data)
    except ValueError as error:
        raise _mismatch("connection.json isn't a Listen connection record", _PRIVATE_HINT) from error
    if (len(data) > 4096 or not isinstance(value, dict)
            or set(value) != {"schema", "socket", "nonce", "pid", "runtime_epoch", "session_id"}
            or value["schema"] != CONNECTION_SCHEMA or not isinstance(value["socket"], str)
            or type(value["pid"]) is not int or not isinstance(value["runtime_epoch"], str)
            or not isinstance(value["nonce"], str) or not re.fullmatch(r"[0-9a-f]{48}", value["nonce"])):
        raise _mismatch("connection.json isn't a Listen connection record", _PRIVATE_HINT)
    socket_path = Path(value["socket"])
    if socket_path.name != SOCKET_NAME or not socket_path.parent.name.startswith("pocket-listen-"):
        raise _mismatch("connection.json names a socket Listen didn't create", _PRIVATE_HINT)
    try:
        _check_private(socket_path.parent, "dir")
        _check_private(socket_path, "socket")
    except FileNotFoundError as error:
        raise RuntimeUnreachable("socket_unreachable") from error
    return value


def call(runtime_dir, op, args=None, *, timeout_s=5.0):
    descriptor = read_connection(runtime_dir)
    payload = canonical_bytes({"op": op, "nonce": descriptor["nonce"], "args": args or {}}) + b"\n"
    if len(payload) > REQUEST_BYTES:
        raise PocketError("The Listen runtime request is too large", hint=_REQUEST_HINT)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout_s)
            connection.connect(descriptor["socket"])
            connection.sendall(payload)
            raw = _read_line(connection, RESPONSE_BYTES)
    except (OSError, _LineTooLong) as error:
        raise RuntimeUnreachable("socket_unreachable") from error
    try:
        result = json.loads(raw) if raw is not None else None
    except ValueError:
        result = None
    if not isinstance(result, dict):
        raise RuntimeUnreachable("invalid_response")
    if result.get("status") == "error":
        code, hint = result.get("code"), result.get("hint")
        raise PocketError(str(result.get("error") or "The Listen runtime refused the request")[:2000],
                          code=code if code in CODES else "invalid_request", hint=hint if valid_hint(hint) else None)
    return result


def read_terminal(session_dir, runtime_dir):
    runtime = Path(runtime_dir)
    path = runtime / TERMINAL_POINTER_NAME
    try:
        with path.open("rb") as stream:
            data = stream.read(4097)
    except FileNotFoundError:
        return None
    try:
        pointer = json.loads(data)
        if not isinstance(pointer, dict) or set(pointer) != {"schema", "terminal"} \
                or pointer["schema"] != TERMINAL_POINTER_SCHEMA:
            raise PocketError("not a Listen terminal pointer")
        record = read_record(pointer["terminal"], session_root(session_dir), TERMINAL_SCHEMA)
    except (OSError, ValueError) as error:
        raise _mismatch("The runtime's terminal record doesn't verify", _TERMINAL_HINT) from error
    match = _RUNTIME_DIR.fullmatch(runtime.name)
    if match is None or record.get("runtime_epoch") != match[2]:
        raise _mismatch("The terminal record belongs to another runtime", _TERMINAL_HINT)
    return pointer["terminal"], record


def read_startup_failure(runtime_dir):
    path = Path(runtime_dir) / STARTUP_FAILURE_NAME
    return _read_sealed(path, STARTUP_FAILURE_SCHEMA, 64 * 1024) if path.exists() else None


def _startup_failure_outcome(runtime_dir):
    try:
        record = read_startup_failure(runtime_dir)
    except PocketError:
        return None, "unverified"
    return record, "missing" if record is None else "sealed"


def stderr_tail(runtime_dir, limit=2048):
    try:
        with (Path(runtime_dir) / STDERR_NAME).open("rb") as stream:
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - limit))
            return stream.read(limit).decode("utf-8", "replace") or None
    except OSError:
        return None


def current_runtime(session_dir):
    newest = newest_runtime(session_dir)
    epoch, path = (None, None) if newest is None else (newest[1], newest[2])
    if not lease_held(session_dir):
        return {"state": "never_started" if newest is None else "not_running", "runtime_epoch": epoch,
                "runtime_dir": None if path is None else str(path)}
    running = path is not None and (path / CONNECTION_NAME).is_file()
    return {"state": "running" if running else "unreachable", "runtime_epoch": epoch,
            "runtime_dir": None if path is None else str(path)}


def wait_for_runtime(session_dir, runtime_dir, process, *, timeout_s=START_WAIT_S, poll_s=0.05):
    deadline = time.monotonic() + timeout_s
    ping = None
    while True:
        code = process.poll()
        if code is not None:
            terminal = read_terminal(session_dir, runtime_dir)
            state = None if terminal is None else terminal[1].get("state")
            outcomes = {"refused": "capture_refused", "stopped": "stopped_during_start"}
            outcome = outcomes.get(state, "host_failed")
            failure, failure_record = _startup_failure_outcome(runtime_dir)
            return {"outcome": outcome, "exit_code": code, "ping": ping,
                    "terminal": terminal, "startup_failure": failure, "startup_failure_record": failure_record,
                    "stderr_tail": stderr_tail(runtime_dir)}
        if (Path(runtime_dir) / CONNECTION_NAME).exists():
            try:
                ping = call(runtime_dir, "ping", timeout_s=2.0)
            except (RuntimeUnreachable, PocketError):
                ping = None
            stopping = ping is not None and ping.get("runtime_state") != "running"
            ended = ping is not None and ping["capture_state"] in ("ended", "failed")
            if ping is not None and not stopping and (ping["frames_received"] > 0 or ended):
                return {"outcome": "capture_ended_during_start" if ended else "capturing",
                        "exit_code": None, "ping": ping, "terminal": None, "startup_failure": None,
                        "startup_failure_record": None, "stderr_tail": None}
        if time.monotonic() >= deadline:
            if ping is not None and ping.get("runtime_state") != "running":
                try:
                    terminal = read_terminal(session_dir, runtime_dir)
                except PocketError:
                    terminal = None
                return {"outcome": "stopped_during_start", "exit_code": None, "ping": ping,
                        "terminal": terminal, "startup_failure": None, "startup_failure_record": None,
                        "stderr_tail": None}
            return {"outcome": "started_no_audio_yet" if ping is not None else "start_unconfirmed",
                    "exit_code": None, "ping": ping, "terminal": None, "startup_failure": None,
                    "startup_failure_record": None, "stderr_tail": None}
        time.sleep(poll_s)


def peer_uid(sock):
    try:
        if sys.platform == "darwin":
            raw = sock.getsockopt(_SOL_LOCAL, socket.LOCAL_PEERCRED, 76)
            # struct xucred: cr_version (u32, XUCRED_VERSION 0) then cr_uid (u32).
            if len(raw) >= 8 and struct.unpack_from("=I", raw, 0)[0] == 0:
                return struct.unpack_from("=I", raw, 4)[0]
            return None
        if hasattr(socket, "SO_PEERCRED"):
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            return struct.unpack("3i", raw)[1]
    except (OSError, struct.error, AttributeError, TypeError):
        return None
    return None


def _peer_check_available():
    return "enforced" if sys.platform == "darwin" or hasattr(socket, "SO_PEERCRED") else "unavailable"


def _error_reply(error):
    reply = {"status": "error", "error": str(error)[:2000],
             "code": error.code if isinstance(error, PocketError) and error.code in CODES else "invalid_request"}
    hint = getattr(error, "hint", None)
    if valid_hint(hint):
        reply["hint"] = hint
    return reply


def _internal_error_reply(error):
    traceback.print_exception(error, file=sys.stderr)
    return _error_reply(PocketError("Listen runtime internal error; the runtime is still serving",
                                    hint="Call listen_observe again; host.stderr in the runtime directory "
                                         "has the details."))


@dataclass(frozen=True, slots=True)
class _StartupFault:
    stage: str
    error: Exception


def _start_thread(name, body, *, on_error):
    def run():
        try:
            body()
        except BaseException as error:  # noqa: BLE001
            on_error(error)
    thread = threading.Thread(target=run, name=f"listen-{name}", daemon=True)
    thread.start()
    return thread


class RuntimeJournal:
    def __init__(self, path, clock, *, budget_bytes=JOURNAL_BUDGET_BYTES, queue_lines=JOURNAL_QUEUE_LINES,
                 opener=None, on_degraded=None):
        self._stream = (opener or _create_private)(path)
        self._clock = clock
        self._budget = budget_bytes
        self._queue = queue.Queue(maxsize=queue_lines)
        self._on_degraded = on_degraded
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closing = threading.Event()
        self._head = _ZERO_SHA
        self._written_lines = self._written_bytes = 0
        self._state = "writing"
        self._error = None
        self._lines = self._bytes = self._dropped = 0
        self._unmarked = 0
        self._head_public = None
        self._last_sync = time.monotonic()
        self._stream_closed = False

    def offer(self, entry):
        with self._lock:
            if self._state != "writing" or self._closing.is_set():
                self._dropped += 1
                return False
            try:
                self._queue.put_nowait((self._clock.monotonic_ns(), entry, self._unmarked))
                self._unmarked = 0
                return True
            except queue.Full:
                self._dropped += 1
                self._unmarked += 1
                return False

    def offer_event(self, event):
        kind = event["evidence"].get("type")
        if event.get("delivery_class") == "health":
            if kind not in ("journal_failed", "journal_exhausted"):
                self.offer({"event": event})
        elif kind == "near_silence":
            self.offer({"event": event})

    def write_pending(self):
        written = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return written
            with self._write_lock:
                written += self._append(*item)

    def start(self):
        return _start_thread("journal", self._write_until_closed, on_error=self._writer_failed)

    def _write_until_closed(self):
        while not (self._closing.is_set() and self._queue.empty()):
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                item = None
            with self._write_lock:
                if item is not None:
                    self._append(*item)
                if time.monotonic() - self._last_sync >= JOURNAL_FSYNC_S:
                    self._sync()

    def _writer_failed(self, error):
        if self.state == "writing":
            self._degrade("failed", error, dropped=1)
        self._drop_queued()

    def _drop_queued(self):
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return
            with self._lock:
                self._dropped += 1

    def close(self, thread=None, timeout_s=0.5):
        with self._lock:
            self._closing.set()
        if thread is None:
            self.write_pending()
        else:
            thread.join(timeout_s)
        drained = thread is None or not thread.is_alive()
        unwritten = 0 if drained else self._queue.qsize() + (1 if self._write_lock.locked() else 0)
        if drained:
            with self._write_lock:
                if not self._stream_closed:
                    with self._lock:
                        pending, self._unmarked = self._unmarked, 0
                    if pending and self.state == "writing":
                        self._write_line(self._clock.monotonic_ns(), _dropped_marker(pending), offered=False)
                    self._sync()
                    try:
                        self._stream.close()
                    except OSError:
                        pass
                    self._stream_closed = True
        return {**self.status(), "head_sha256": self.head_sha256, "closed": drained,
                "lines_unwritten_at_close": unwritten}

    @property
    def head_sha256(self):
        with self._lock:
            return self._head_public if self._lines else None

    @property
    def state(self):
        with self._lock:
            return self._state

    def status(self):
        with self._lock:
            return {"state": self._state, "lines": self._lines, "bytes": self._bytes,
                    "dropped_lines": self._dropped, "budget_bytes": self._budget}

    def _append(self, monotonic_ns, entry, dropped_before=0):
        marked = dropped_before and self.state == "writing"
        marker = self._write_line(monotonic_ns, _dropped_marker(dropped_before), offered=False) if marked else 0
        return marker + self._write_line(monotonic_ns, entry)

    def _write_line(self, monotonic_ns, entry, *, offered=True):
        lost = 1 if offered else 0
        with self._lock:
            writing = self._state == "writing"
            if not writing:
                self._dropped += lost
        if not writing:
            return 0
        line = self._encode(monotonic_ns, entry)
        if line is None:
            with self._lock:
                self._dropped += lost
            return 0
        if self._written_bytes + len(line) + 1 > self._budget - JOURNAL_MARKER_RESERVE_BYTES:
            self._degrade("exhausted", monotonic_ns=monotonic_ns, dropped=lost)
            return 0
        failure = self._put(line)
        if failure is not None:
            error, partial = failure
            self._degrade("failed", error, monotonic_ns=None if partial else monotonic_ns, dropped=lost)
            return 0
        return 1

    def _encode(self, monotonic_ns, entry):
        try:
            line = canonical_bytes({"schema": JOURNAL_SCHEMA, "line": self._written_lines + 1,
                                    "prev_sha256": self._head, "monotonic_ns": monotonic_ns, "entry": entry})
        except PocketError:
            return None
        return line if len(line) <= JOURNAL_LINE_BYTES else None

    def _put(self, line):
        data, written = memoryview(line + b"\n"), 0
        try:
            while data:
                count = self._stream.write(data)
                if not count:
                    raise OSError("journal write made no progress")
                data, written = data[count:], written + count
        except OSError as error:
            return error, written > 0
        self._head = hashlib.sha256(line).hexdigest()
        self._written_lines += 1
        self._written_bytes += len(line) + 1
        with self._lock:
            self._lines, self._bytes, self._head_public = self._written_lines, self._written_bytes, self._head
        return None

    def _sync(self):
        self._last_sync = time.monotonic()
        if self._stream_closed or self.state == "failed":
            return
        try:
            os.fsync(self._stream.fileno())
        except (OSError, ValueError) as error:
            self._degrade("failed", error, monotonic_ns=self._clock.monotonic_ns())

    def _degrade(self, state, error=None, *, monotonic_ns=None, dropped=0):
        with self._lock:
            first = self._state == "writing"
            if first:
                self._state = state
                self._error = None if error is None else str(error)[:200]
            self._dropped += dropped
        if first and monotonic_ns is not None and not self._stream_closed:
            line = self._encode(monotonic_ns, {"journal": "stopped_writing", "state": state})
            if line is not None and self._written_bytes + len(line) + 1 <= self._budget:
                self._put(line)
        if first and self._on_degraded is not None:
            self._on_degraded(state, self._error)


def _dropped_marker(count):
    return {"journal": "lines_dropped", "dropped_lines": count}


class _CappedStream:
    def __init__(self, stream, limit):
        self._stream, self._limit, self._written, self.omitted = stream, limit, 0, 0

    def write(self, text):
        data = text.encode("utf-8", "replace")
        room = self._limit - self._written
        if room > 0:
            self._stream.write(data[:room].decode("utf-8", "ignore"))
            self._written += min(room, len(data))
        self.omitted += max(0, len(data) - max(room, 0))
        return len(text)

    def flush(self):
        try:
            self._stream.flush()
        except (OSError, ValueError):
            pass

    def fileno(self):
        return self._stream.fileno()


class ListenHost:
    close_budget_ns = 2_400_000_000
    close_join_s = 3.6
    drain_ns = 500_000_000
    thread_join_s = 0.5
    journal_close_s = 0.5
    connection_timeout_s = 10.0
    shutdown_wait_s = 1.5

    def __init__(self, session_dir, runtime_dir, *, lease_fd, clock=None, adapter_factory=None,
                 expected_config_sha256=None, threads=True, journal_opener=None,
                 journal_queue_lines=JOURNAL_QUEUE_LINES, socket_parent=None):
        from .listen_runtime import SystemClock
        self.session_dir = session_root(session_dir)
        self.runtime_dir = Path(runtime_dir).resolve()
        self.lease_fd = lease_fd
        self.clock = clock if clock is not None else SystemClock()
        self.threads = threads
        self._adapter_factory = adapter_factory or _default_adapter
        self._expected_sha = expected_config_sha256
        self._journal_opener = journal_opener
        self._journal_queue_lines = journal_queue_lines
        self._socket_parent = socket_parent
        self.nonce = secrets.token_hex(24)
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._stop_lock = threading.Lock()
        self._active = threading.Condition()
        self._finished = threading.Event()
        self._stop_requested = threading.Event()
        self._stop_done = threading.Event()
        self._capture_stop = threading.Event()
        self.session = self.launch = self.core = self.adapter = self.journal = None
        self.runtime_epoch = self.ordinal = None
        self.socket_dir = self._server = None
        self.stop_result = None
        self._threads = {}
        self._requested_cause = None
        self._stop_begun = self._failed = self._shut_down = False
        self._error = None
        self._active_count = 0
        self._started_utc = _utc()
        self._started_ns = self.clock.monotonic_ns()

    def start(self):
        opened = self._open()
        if isinstance(opened, _StartupFault):
            return self._startup_failed(opened.stage, opened.error)
        return contained(partial(self._begin, opened), on_error=self._begin_failed)

    def _open(self):
        for stage, step in (("session_verify", self._verify), ("socket", self._bind),
                            ("internal", self._open_journal)):
            fault = contained(step, on_error=partial(_StartupFault, stage))
            if isinstance(fault, _StartupFault):
                return fault
        return contained(self._open_adapter, on_error=partial(_StartupFault, "adapter_open"))

    def _verify(self):
        self._verify_lease()
        self._verify_session()

    def _open_adapter(self):
        self.adapter = self._adapter_factory(self.session, self.clock)
        return self.adapter.open()

    def _begin_failed(self, error):
        if self.core is not None:
            return self._failed_after_capture_began(error)
        return self._startup_failed("internal", error)

    def _failed_after_capture_began(self, error):
        print("Listen runtime startup failed after capture began:", file=sys.stderr)
        traceback.print_exception(error, file=sys.stderr)
        with self._stop_lock:
            if not self._stop_begun:
                self._failed = True
                self._error = f"startup: {error}"[:2000]
        if not self.stop("fatal_error")["terminal_sealed"]:
            self._seal_startup_failure("after_capture_started", error)
        self.shutdown()
        return EXIT_FAILED

    def _verify_lease(self):
        fcntl = _fcntl()
        if not isinstance(self.lease_fd, int):
            raise PocketError("The runtime needs the session lease fd")
        # The inherited open description already holds LOCK_EX, so this converts nothing.
        fcntl.flock(self.lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        expected, inherited = os.stat(self.session_dir / LEASE_NAME), os.fstat(self.lease_fd)
        if (expected.st_dev, expected.st_ino) != (inherited.st_dev, inherited.st_ino):
            raise _mismatch("The runtime didn't inherit this session's lease")
        os.set_inheritable(self.lease_fd, False)

    def _verify_session(self):
        _, _, self.session = load_session(self.session_dir)
        match = _RUNTIME_DIR.fullmatch(self.runtime_dir.name)
        newest = newest_runtime(self.session_dir)
        if match is None or self.runtime_dir.parent != self.session_dir / RUNTIMES or newest is None \
                or newest[2] != self.runtime_dir:
            raise _mismatch("The runtime directory isn't this session's newest runtime")
        _check_private(self.runtime_dir, "dir")
        self.launch = read_launch(self.runtime_dir)
        if (self.launch["runtime_epoch"], self.launch["ordinal"]) != (match[2], int(match[1])) \
                or self.launch["session_id"] != self.session["session_id"] \
                or self.launch["config_sha256"] != self.session["config_sha256"]:
            raise _mismatch("The launch record doesn't match this session and runtime directory")
        if self._expected_sha is not None and self._expected_sha != self.session["config_sha256"]:
            raise PocketError("The session's config_sha256 differs from the expected one", code="stale_revision",
                              hint="Pass config_sha256 from this session's listen_prepare receipt unchanged.")
        self.runtime_epoch, self.ordinal = self.launch["runtime_epoch"], self.launch["ordinal"]
        if self.session["config"]["adapter"]["kind"] == "coreaudio":
            if sys.platform != "darwin":
                raise PocketError("The CoreAudio adapter runs on macOS only", code="unsupported_profile",
                                  hint="Use the synthetic adapter on this system.")
            build = verify_build(self.session["config"]["adapter"]["build_id"])
            sealed = self.session["build"]
            if (build["helper_sha256"], build["build_json_sha256"]) != (sealed.get("helper_sha256"),
                                                                       sealed.get("build_json_sha256")):
                raise _mismatch("The helper build changed since listen_prepare",
                                "Build the helper again with listen_build_helper, then prepare a new session.")

    def _bind(self):
        parent = self._socket_parent or ("/private/tmp" if sys.platform == "darwin" else tempfile.gettempdir())
        self.socket_dir = Path(tempfile.mkdtemp(prefix="pocket-listen-", dir=parent))
        _check_private(self.socket_dir, "dir")
        address = self.socket_dir / SOCKET_NAME
        if len(os.fsencode(address)) >= 100:
            raise PocketError("The socket path is too long for AF_UNIX")
        host = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                host.handle_connection(self.request)

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        self._server = Server(str(address), Handler)
        os.chmod(address, 0o600)

    def _open_journal(self):
        self.journal = RuntimeJournal(self.runtime_dir / JOURNAL_NAME, self.clock,
                                      queue_lines=self._journal_queue_lines, opener=self._journal_opener,
                                      on_degraded=self._journal_degraded)
        if self.threads:
            self._threads["journal"] = self.journal.start()
        self.journal.offer({"phase": "launch", "runtime_epoch": self.runtime_epoch, "ordinal": self.ordinal,
                            "launch": self.launch["launch"], "request_id": self.launch["request_id"]})

    def _begin(self, opened):
        if opened.refusal:
            return self._refuse(opened, opened.refusal)
        from .listen import resolve_routes, stale_after_fits_block
        from .listen_runtime import ListenCore
        config = self.session["config"]
        fmt, stale_after = opened.format, config["health"]["stale_after_ms"]
        if not stale_after_fits_block(stale_after, fmt.buffer_frame_size, fmt.sample_rate):
            return self._refuse(opened, ("stale_after_below_block_period",), extra={"block_period": {
                "buffer_frame_size": fmt.buffer_frame_size, "sample_rate": fmt.sample_rate,
                "stale_after_ms": stale_after}})
        resolved, reasons = resolve_routes(config["routes"], fmt.device_input_channels)
        if reasons:
            return self._refuse(opened, reasons, resolved, refused_at="routes")
        source = "synthetic_script" if self.adapter.profile == SYNTHETIC_PROFILE else "device_input"
        core = ListenCore(config, opened, session_id=self.session["session_id"], runtime_epoch=self.runtime_epoch,
                          audio_source=source, adapter_profile=self.adapter.profile,
                          config_sha256=self.session["config_sha256"], clock=self.clock, ordinal=self.ordinal,
                          started_ns=self._started_ns, adapter_stats=self.adapter.stats,
                          journal_status=self.journal.status,
                          security={"peer_uid_check": _peer_check_available(), "socket": "af_unix_only"},
                          on_publish=self.journal.offer_event,
                          provenance=capture_provenance(self.session, self.adapter.profile, opened.detail))
        started = self.adapter.start(core.selected_channels)
        if not started.started:
            return self._refuse(opened, started.refusal or ("helper_protocol_error",), resolved,
                                refused_at="start", start_detail=started.detail or {})
        self.core = core
        core.begin()
        self.journal.offer({"phase": "capture_started", "selected_channels": list(core.selected_channels)})
        if self.threads:
            self._spawn("capture", self._capture_loop)
            self._spawn("worker", self._worker_loop)
            self._spawn("watchdog", self._watchdog_loop)
            self._spawn("server", lambda: self._server.serve_forever(poll_interval=0.2))
        _write_private_json(self.runtime_dir / CONNECTION_NAME, {
            "schema": CONNECTION_SCHEMA, "socket": str(self.socket_dir / SOCKET_NAME), "nonce": self.nonce,
            "pid": os.getpid(), "runtime_epoch": self.runtime_epoch, "session_id": self.session["session_id"]})
        return None

    def _seal_startup_failure(self, stage, error, **extra):
        try:
            _seal(self.runtime_dir / STARTUP_FAILURE_NAME,
                  {"schema": STARTUP_FAILURE_SCHEMA, "runtime_epoch": self.runtime_epoch, "stage": stage,
                   "error": str(error)[:2000], "wall_utc": _utc(), **extra})
        except OSError:
            pass

    def _startup_failed(self, stage, error):
        print(f"Listen runtime startup failed at {stage}:", file=sys.stderr)
        traceback.print_exception(error, file=sys.stderr)
        contained(self._close_adapter, on_error=partial(traceback.print_exception, file=sys.stderr))
        self._capture_stop.set()
        if "server" in self._threads:
            self._server.shutdown()
        self._seal_startup_failure(stage, error)
        if self.journal is not None:
            self.journal.offer({"phase": "startup_failed", "stage": stage})
            self.journal.close(self._threads.get("journal"), self.journal_close_s)
        self._cleanup_socket()
        self._last_journal_close()
        self._release_lease()
        return EXIT_SESSION if stage == "session_verify" else EXIT_FAILED

    def _refuse(self, opened, reasons, resolved=None, *, refused_at="open", start_detail=None, extra=None):
        from .listen import WORKER_TERMINAL_KEYS, coverage
        begun = time.monotonic_ns()
        close_cause, close_detail = self._close_adapter()
        reasons = [r if r in REFUSAL_REASONS else "helper_protocol_error" for r in reasons]
        self.journal.offer({"phase": "refused", "reasons": reasons, "refused_at": refused_at})
        journal = self.journal.close(self._threads.get("journal"), self.journal_close_s)
        identity, config = opened.identity, self.session["config"]
        if resolved is None:
            routes = [{"route_id": r["route_id"], "role": r["role"],
                       "metering": "withheld_until_routing_test" if r["role"] == "cue" else "not_metered",
                       "availability": "unavailable", "availability_at_start": "unavailable",
                       "reason": "routing_test_not_available" if r["role"] == "cue" else "capture_refused"}
                      for r in config["routes"]]
        else:
            routes = [{"route_id": r["route_id"], "role": r["role"], "metering": r["metering"],
                       "availability": "unavailable", "availability_at_start": r["availability"],
                       "reason": r["reason"] or "capture_refused"} for r in resolved]
        open_detail = {k: v for k, v in (opened.detail or {}).items() if k != "stderr_tail"}
        refusal = {"reasons": reasons, "refused_at": refused_at,
                   "inventory": list(opened.inventory)[:32] if opened.inventory else None, "detail": open_detail}
        if start_detail is not None:
            refusal = {**refusal, "detail": {k: v for k, v in start_detail.items() if k != "stderr_tail"},
                       "open_detail": open_detail}
        refusal = {**refusal, **(extra or {})}
        terminal = {
            "schema": TERMINAL_SCHEMA, "session_id": self.session["session_id"],
            "config_sha256": self.session["config_sha256"], "runtime_epoch": self.runtime_epoch,
            "ordinal": self.ordinal, "state": "refused", "cause": "startup_refused", "stop_request_id": None,
            "started_monotonic_ns": self._started_ns, "stopped_monotonic_ns": self.clock.monotonic_ns(),
            "wall": {"started_utc": self._started_utc, "stopped_utc": _utc()},
            "capture": {"final_state": "refused", "ended_cause": None, "refusal": refusal,
                        "adapter": self.adapter.profile,
                        "device": None if identity is None else {
                            "uid": identity.uid, "name": identity.name, "manufacturer": identity.manufacturer,
                            "model_uid": identity.model_uid, "transport": identity.transport,
                            "device_instance": identity.instance},
                        "device_instance": None if identity is None else identity.instance,
                        "device_instance_epoch": None, "topology_epoch": None, "streams": [], "streams_omitted": 0,
                        "missing": {"events": 0, "exact_frames": 0}, "stale_episodes": 0,
                        "nonfinite_samples": 0, "callback_ns": None, "helper_stderr_tail": self._helper_stderr(),
                        "provenance": capture_provenance(self.session, self.adapter.profile, opened.detail)},
            "routes": [{**r, "frames_measured": None, "max_sample_peak_dbfs": None, "clipped_total": None,
                        "nonfinite_total": None, "near_silence_frames": None} for r in routes],
            "broker": {"published": 0, "last_sequence": 0, "evicted": {"normal": 0, "health": 0},
                       "health_lost_through": 0},
            "worker": {"drain": None, **dict.fromkeys(WORKER_TERMINAL_KEYS, 0), "state": "not_started",
                       "error": None},
            "journal": journal,
            "stop": _stop_timing(begun, close_cause, close_detail, None),
            "error": None,
            "coverage": coverage("synthetic_script" if self.adapter.profile == SYNTHETIC_PROFILE else "none",
                                 capture="refused")}
        self.stop_result = self._seal_terminal(terminal, "startup_refused", None)
        sealed = self.stop_result["terminal_sealed"]
        if not sealed:
            self._seal_startup_failure("refused", f"Capture was refused ({', '.join(reasons)}), but its terminal "
                                       "record couldn't be sealed",
                                       refusal={"reasons": reasons, "refused_at": refused_at})
        self._cleanup_socket()
        self._last_journal_close()
        self._release_lease()
        return EXIT_REFUSED if sealed else EXIT_FAILED

    def _spawn(self, name, loop):
        self._threads[name] = _start_thread(name, loop, on_error=partial(self._fail, name))

    def _capture_loop(self):
        while not self._capture_stop.is_set():
            self.core.ingest(self.adapter.read(CAPTURE_READ_NS))

    def _worker_loop(self):
        core = self.core
        while core.wait_for_work(LOOP_WAIT_NS) or core.state != "stopped":
            core.measure_pending()

    def _watchdog_loop(self):
        while not self._capture_stop.wait(LOOP_WAIT_NS / 1e9):
            if self.core.watchdog_tick() == "max_runtime_reached":
                self.stop("max_runtime_reached")
                self._finished.set()
                return

    def step(self, timeout_ns=CAPTURE_READ_NS):
        if self.threads:
            raise RuntimeError("step drives a host built with threads=False")
        if self._stop_begun:
            return self.stop_result
        self.core.ingest(self.adapter.read(timeout_ns))
        cause = self.core.watchdog_tick()
        self.core.measure_pending()
        self.journal.write_pending()
        return self.stop(cause) if cause == "max_runtime_reached" else None

    def _fail(self, where, error):
        print(f"Listen runtime thread {where} failed:", file=sys.stderr)
        traceback.print_exception(error, file=sys.stderr)
        with self._stop_lock:
            if not self._stop_begun:
                self._failed = True
                self._error = f"{where}: {error}"[:2000]
        self.stop("fatal_error")
        self._finished.set()

    def _journal_degraded(self, state, error):
        core = self.core
        if core is not None and core.state in ("running", "stopping"):
            core.publish_health("journal_failed" if state == "failed" else "journal_exhausted", cause=state,
                                state_transition=("writing", state),
                                detail={"error": error, "budget_bytes": JOURNAL_BUDGET_BYTES})

    def run(self):
        while not self._finished.wait(0.1):
            if self._stop_requested.is_set():
                self.stop(self._requested_cause or "signal_terminate")
                self._finished.set()
        self.shutdown()
        return EXIT_FAILED if self._failed else EXIT_STOPPED

    def request_stop(self, cause="signal_terminate"):
        if self._requested_cause is None:
            self._requested_cause = cause
        self._stop_requested.set()

    def handle_connection(self, sock):
        with self._active:
            self._active_count += 1
        op = None
        try:
            sock.settimeout(self.connection_timeout_s)
            uid = peer_uid(sock)
            if uid is not None and uid != os.getuid():
                self._reply(sock, _error_reply(PocketError("Listen runtime refuses connections from another user")))
                return
            try:
                raw = _read_line(sock, REQUEST_BYTES)
                request = None if raw is None else json.loads(raw)
            except ValueError:
                self._reply(sock, _error_reply(PocketError(
                    "Listen runtime requests are one JSON line of at most 64 KiB", hint=_REQUEST_HINT)))
                return
            if raw is None:
                return
            op = request.get("op") if isinstance(request, dict) else None
            slotted = op != "stop"
            if slotted and not self._slots.acquire(blocking=False):
                self._reply(sock, _error_reply(PocketError("Listen runtime is busy", hint=_BUSY_HINT)))
                return
            try:
                result = contained(partial(self._answer, request,
                                           "enforced" if uid is not None else "unavailable"),
                                   on_error=_internal_error_reply)
            finally:
                if slotted:
                    self._slots.release()
            self._reply(sock, result)
        except OSError:
            pass
        finally:
            if op == "stop" and self._stop_done.is_set():
                self._finished.set()
            with self._active:
                self._active_count -= 1
                self._active.notify_all()

    def _reply(self, sock, result):
        try:
            data = canonical_bytes(result)
        except PocketError as error:
            data = canonical_bytes(_error_reply(error))
        if len(data) > RESPONSE_BYTES:
            data = canonical_bytes(_error_reply(PocketError("The Listen runtime reply exceeds 512 KiB",
                                                            hint="Ask for fewer events with max_events or max_bytes.")))
        sock.sendall(data + b"\n")

    def _answer(self, request, peer_check):
        try:
            return self.dispatch(request, peer_check=peer_check)
        except PocketError as error:
            return _error_reply(error)

    def dispatch(self, request, *, peer_check="enforced"):
        if not isinstance(request, dict) or not set(request) <= {"op", "nonce", "args"}:
            raise PocketError("A Listen runtime request has op, nonce and args only", hint=_REQUEST_HINT)
        nonce = request.get("nonce")
        if not isinstance(nonce, str) or not hmac.compare_digest(nonce.encode(), self.nonce.encode()):
            raise PocketError("Listen runtime refused the request: the nonce doesn't match", hint=_NONCE_HINT)
        op, args = request.get("op"), request.get("args", {})
        if op not in OPS:
            raise PocketError("Listen exposes no control operations", hint=_CONTROL_HINT)
        if not isinstance(args, dict):
            raise PocketError("args must be an object", hint=_REQUEST_HINT)
        allowed = {"ping": set(), "observe": set(), "events": _EVENT_ARGS,
                   "stop": {"expected_runtime_epoch", "request_id"}}[op]
        if not set(args) <= allowed:
            raise PocketError(f"{op} takes {', '.join(sorted(allowed)) or 'no arguments'}", hint=_REQUEST_HINT)
        core = self.core
        if op == "ping":
            summary = core.health_summary()
            return {"status": "ok", "runtime_epoch": self.runtime_epoch, "runtime_state": core.state,
                    "capture_state": summary["capture_state"], "capture_cause": summary["capture_cause"],
                    "frames_received": core.tracker.snapshot()["frames_received"], "head_sequence": core.broker.head}
        if op == "observe":
            result = core.observe()
            result["observation"]["security"] = {"peer_uid_check": peer_check, "socket": "af_unix_only"}
            return {"status": "ok", **result}
        if op == "events":
            rest = {k: v for k, v in args.items() if k != "after"}
            return {"status": "ok", **core.poll(args.get("after"), **rest)}
        request_id, epoch = args.get("request_id"), args.get("expected_runtime_epoch")
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id) or not isinstance(epoch, str):
            raise PocketError("stop needs request_id and expected_runtime_epoch",
                              hint="Pass the request_id of this listen_stop and the runtime_epoch to stop.")
        if not hmac.compare_digest(epoch.encode(), self.runtime_epoch.encode()):
            raise PocketError(f"This runtime is {self.runtime_epoch}, not {epoch[:40]}", code="stale_revision",
                              hint=_STALE_EPOCH_HINT)
        return self.stop("listen_stop", request_id=request_id, expected_runtime_epoch=epoch)

    def stop(self, cause, *, request_id=None, expected_runtime_epoch=None):
        with self._stop_lock:
            first = not self._stop_begun
            self._stop_begun = True
        if not first:
            self._stop_done.wait(STOP_BOUND_MS / 1000 + 2)
            return self.stop_result or {"status": "ok", "terminal_sealed": False, "terminal_handle": None,
                                        "reason": "stop_in_progress", "runtime_epoch": self.runtime_epoch}
        result = contained(partial(self._stop_ladder, cause, request_id, expected_runtime_epoch),
                           on_error=partial(self._stop_failed, cause, request_id))
        if not result["terminal_sealed"]:
            self._record_failure(f"stop: {result['reason']}")
        self.stop_result = result
        self._stop_done.set()
        return result

    def _stop_failed(self, cause, request_id, error):
        traceback.print_exception(error, file=sys.stderr)
        self._record_failure(f"stop: {error}")
        return {"status": "ok", "terminal_sealed": False, "terminal_handle": None, "reason": "stop_failed",
                "cause": cause, "stop_request_id": request_id, "runtime_epoch": self.runtime_epoch}

    def _record_failure(self, message):
        with self._stop_lock:
            self._failed = True
            self._error = self._error or message[:2000]

    def _stop_ladder(self, cause, request_id, expected_runtime_epoch):
        begun = time.monotonic_ns()
        deadline = begun + (STOP_BOUND_MS - SEAL_RESERVE_MS) * 1_000_000

        def left(default_s):
            return max(0.0, min(default_s, (deadline - time.monotonic_ns()) / 1e9))

        core = self.core
        if expected_runtime_epoch is not None:
            self.journal.offer({"op": "stop", "request_id": request_id,
                                "expected_runtime_epoch": expected_runtime_epoch})
        self.journal.offer({"phase": "stopping", "cause": cause, "request_id": request_id})
        core.begin_stop(cause, request_id=request_id)
        self._capture_stop.set()
        close_cause, close_detail = self._close_adapter()
        self._join("capture", left(self.thread_join_s))
        try:
            summary = core.finish_stop(drain_ns=int(left(2 * self.drain_ns / 1e9) / 2 * 1e9))
        except RuntimeError:
            summary = core.terminal_summary()
        self._join("worker", left(self.thread_join_s))
        self._join("watchdog", left(self.thread_join_s))
        self.journal.offer({"phase": "stopped", "cause": cause})
        journal = self.journal.close(self._threads.get("journal"), left(self.journal_close_s))
        terminal = {"schema": TERMINAL_SCHEMA, **summary,
                    "state": "failed" if self._failed else summary["state"],
                    "capture": {**summary["capture"], "helper_stderr_tail": self._helper_stderr(),
                                "host_stderr_omitted_bytes": getattr(sys.stderr, "omitted", None)},
                    "wall": {"started_utc": self._started_utc, "stopped_utc": _utc()},
                    "journal": journal,
                    "stop": _stop_timing(begun, close_cause, close_detail,
                                         summary["capture"].get("adapter_dropped_frames_total")),
                    "error": self._error}
        return self._seal_terminal(terminal, cause, request_id)

    def _close_adapter(self):
        adapter = self.adapter
        if adapter is None:
            return "not_started", {}
        box = {}

        def close():
            box["result"] = adapter.close(self.clock.monotonic_ns() + self.close_budget_ns)
        closer = _start_thread("adapter-close", close, on_error=lambda error: box.update(error=error))
        closer.join(self.close_join_s)
        if "result" in box:
            return box["result"].cause, dict(box["result"].detail)
        return ("error", {"error": str(box["error"])[:500]}) if "error" in box else ("timeout", {})

    def _join(self, name, timeout_s):
        thread = self._threads.get(name)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout_s)

    def _helper_stderr(self):
        tail = getattr(self.adapter, "stderr_tail", None)
        return contained(partial(tail, 2048), on_error=lambda error: None) if callable(tail) else None

    def _seal_terminal(self, terminal, cause, request_id):
        result = {"status": "ok", "terminal_sealed": False, "terminal_handle": None, "reason": None,
                  "cause": cause, "stop_request_id": request_id, "runtime_epoch": self.runtime_epoch}
        try:
            handle = put_record(terminal, self.session_dir)
            _write_private_json(self.runtime_dir / TERMINAL_POINTER_NAME,
                                {"schema": TERMINAL_POINTER_SCHEMA, "terminal": handle})
        except (OSError, PocketError, ValueError) as error:
            print(f"Listen couldn't seal the terminal record: {error}", file=sys.stderr)
            return {**result, "reason": "terminal_seal_failed"}
        return {**result, "terminal_sealed": True, "terminal_handle": handle}

    def shutdown(self):
        if self._shut_down:
            return
        self._shut_down = True
        if self._server is not None and "server" in self._threads:
            self._server.shutdown()
        with self._active:
            self._active.wait_for(lambda: self._active_count == 0, timeout=self.shutdown_wait_s)
        (self.runtime_dir / CONNECTION_NAME).unlink(missing_ok=True)
        self._cleanup_socket()
        self._last_journal_close()
        self._release_lease()

    def _last_journal_close(self):
        if self.journal is not None:
            self.journal.close(self._threads.get("journal"), self.shutdown_wait_s)

    def _cleanup_socket(self):
        if self._server is not None:
            self._server.server_close()
        if self.socket_dir is not None:
            try:
                (self.socket_dir / SOCKET_NAME).unlink(missing_ok=True)
                self.socket_dir.rmdir()
            except OSError:
                pass

    def _release_lease(self):
        # The last fd on the lease's open description; closing it releases the lease.
        if isinstance(self.lease_fd, int) and self.lease_fd >= 0:
            try:
                os.close(self.lease_fd)
            except OSError:
                pass
            self.lease_fd = -1


_CLOSE_KEYS = ("error", "exit_code", "exit_reason", "exit_stage", "exit_osstatus", "exit_event_type",
               "exit_event_value", "microphone_authorization", "items_discarded_while_closing",
               "blocks_discarded_while_closing",
               "audio_frames_discarded_while_closing", "items_left_unread", "blocks_left_unread",
               "audio_frames_left_unread")


def _stop_timing(begun_ns, close_cause, close_detail, seen):
    elapsed_ms = (time.monotonic_ns() - begun_ns) // 1_000_000
    return {"elapsed_ms": elapsed_ms, "adapter_close": close_cause, "bound_ms": STOP_BOUND_MS,
            "bound_exceeded": elapsed_ms > STOP_BOUND_MS, **_close_report(close_detail, seen)}


def _close_report(detail, seen):
    counters = detail.get("exit_counters") if isinstance(detail.get("exit_counters"), dict) else {}
    dropped = counters.get("dropped_frames")
    return {"adapter_exit": {**{k: detail[k] for k in _CLOSE_KEYS if k in detail}, "counters": counters},
            "helper_drops_after_last_block_frames": (max(0, dropped - seen)
                                                     if type(dropped) is int and type(seen) is int else None)}


def capture_provenance(session, profile, detail=None):
    from . import __version__
    if profile == SYNTHETIC_PROFILE:
        return {"pocket_version": __version__, "macos_version": "not_applicable",
                "helper_sha256": "not_applicable", "build_json_sha256": "not_applicable",
                "driver_version": "not_applicable"}
    build = session.get("build") if isinstance(session.get("build"), dict) else {}
    macos = (detail or {}).get("macos_version")
    return {"pocket_version": __version__, "macos_version": macos if isinstance(macos, str) else "not_reported",
            "helper_sha256": build.get("helper_sha256"), "build_json_sha256": build.get("build_json_sha256"),
            "driver_version": "not_reported"}


def _default_adapter(session, clock):
    from .listen_adapters import CoreAudioHelperAdapter, SyntheticAdapter
    config = session["config"]
    uid, rate, adapter = config["device"]["uid"], config["device"]["expected_sample_rate"], config["adapter"]
    if adapter["kind"] == "synthetic":
        return SyntheticAdapter(adapter["script"], uid, rate, clock,
                                pacing="stepped" if hasattr(clock, "advance") else "realtime")
    return CoreAudioHelperAdapter(adapter["build_id"], uid, rate, clock,
                                  expected_helper_sha256=session["build"]["helper_sha256"])


def _install_signals(host):
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: host.request_stop("signal_terminate"))


def _print(value):
    print(json.dumps(value, sort_keys=True), flush=True)


def _foreground(session_dir, expected_config_sha256):
    try:
        root, _, session = load_session(session_dir)
    except PocketError as error:
        _print(_error_reply(error))
        return EXIT_SESSION
    if session["config_sha256"] != expected_config_sha256:
        _print(_error_reply(PocketError("expected_config_sha256 doesn't match this session", code="stale_revision",
                                        hint="Pass config_sha256 from this session's listen_prepare receipt.")))
        return EXIT_SESSION
    fd = acquire_lease(root)
    if fd is None:
        _print({"status": "conflict", "runtime_conflict": "already_running"})
        return EXIT_CONFLICT
    try:
        runtime_dir, _ = prepare_launch(root, request_id=None, launch="foreground")
    except BaseException:
        os.close(fd)
        raise
    host = ListenHost(root, runtime_dir, lease_fd=fd, expected_config_sha256=expected_config_sha256)
    _install_signals(host)
    code = host.start()
    if code is not None:
        _print({"status": "refused" if code == EXIT_REFUSED else "failed", "exit_code": code,
                "runtime_dir": str(runtime_dir)})
        return code
    _print({"status": "running", "runtime_epoch": host.runtime_epoch, "runtime_dir": str(runtime_dir)})
    code = host.run()
    result = host.stop_result or {}
    _print({"status": "stopped" if code == EXIT_STOPPED else "failed", "exit_code": code,
            "terminal_sealed": result.get("terminal_sealed", False), "cause": result.get("cause")})
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m pocket_music.listen_host",
                                     description="Run one Listen runtime for a prepared session. listen_start "
                                                 "launches it; --foreground is for supervised native checks.")
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--runtime-dir")
    parser.add_argument("--lease-fd", type=int)
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--expected-config-sha256")
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.foreground:
        if args.runtime_dir or args.lease_fd is not None or not args.expected_config_sha256:
            parser.error("--foreground takes --session-dir and --expected-config-sha256 only")
        return _foreground(args.session_dir, args.expected_config_sha256)
    if not args.runtime_dir or args.lease_fd is None:
        parser.error("the daemon takes --session-dir, --runtime-dir and --lease-fd from listen_start")
    sys.stderr = _CappedStream(sys.stderr, STDERR_CAP_BYTES)
    host = ListenHost(args.session_dir, args.runtime_dir, lease_fd=args.lease_fd)
    _install_signals(host)
    code = host.start()
    return code if code is not None else host.run()


if __name__ == "__main__":
    # Run the package's own module, not this __main__ copy, so listen.py and the daemon share one.
    from pocket_music.listen_host import main as _main
    sys.exit(_main())
