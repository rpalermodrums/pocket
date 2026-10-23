# SPDX-License-Identifier: AGPL-3.0-only
"""The b2b control journal: append-only, hash-chained NDJSON with a byte budget.

Each line is the canonical JSON of one entry and carries the SHA-256 of the previous line's
bytes, so an offline reader can tell an intact journal from a truncated or altered one. The
chain can't vouch for the last line, which has no successor; a retained receipt is also checked
against its own ``receipt_sha256``. ``append`` is one ``os.write`` to an O_APPEND descriptor, so an
entry is in the kernel before the broker's next adapter call; ``sync`` (fsync) runs outside the
broker lock. This survives a host-process crash. It claims nothing about power loss or an
operating-system crash.

Offline reads (``read_entries``, ``read_request``, ``read_line``) need no host and never repeat
anything; they only report what the journal retained.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import NamedTuple

from .artifact_store import canonical_bytes, digest

ENTRY_SCHEMA = "pocket.b2b-journal-entry/v1"
KINDS = frozenset({
    "host_started", "session_registered", "host_stopping",
    "request_started", "request_completed",
    "authority_changed", "grant_created", "grant_ended",
    "notice_created", "notice_fetched", "ack_accepted", "ack_rejected", "ack_late", "notice_failed",
    "step_admitted", "step_submitted", "step_ack", "step_cancelled", "readback",
    "human_input", "unknown_input", "gesture", "invitation", "proposal", "permission", "pickup", "dependency",
    "epoch_changed", "panic", "journal_overflow",
})
DEFAULT_MAX_BYTES = 32 * 1024 * 1024
MIN_MAX_BYTES, MAX_MAX_BYTES = 1024 * 1024, 1024 * 1024 * 1024
RESERVE_BYTES = 64 * 1024  # holds the one journal_overflow entry written when the budget is reached
MAX_LINE_BYTES = 256 * 1024
READ_LIMIT = 64 * 1024 * 1024
_IDENTITY_FIELDS = ("request_id", "channel", "operation")  # kept when an oversized entry's data is omitted


class JournalUnavailable(Exception):
    """The journal is full or failed, so the entry was not written."""

    def __init__(self, state: str):
        super().__init__(f"The b2b journal is {state}")
        self.state = state


class JournalRef(NamedTuple):
    seq: int
    offset: int  # byte offset of the entry's line, for read_line


class Journal:
    """One host's journal. The broker appends under its lock; any thread may call ``sync``.

    ``state`` is ``ok``, ``full`` (the budget is spent; one overflow entry was written) or
    ``failed`` (a write or fsync raised). Once it leaves ``ok`` it never returns, and every later
    append raises ``JournalUnavailable`` and counts a lost entry. A failed fsync changes the
    state outside the broker lock, so the broker rereads ``state`` at each entry.
    """

    def __init__(self, path, *, clock, host_epoch: str, max_bytes: int = DEFAULT_MAX_BYTES, writer=None,
                 fsync=None):
        if type(max_bytes) is not int or not MIN_MAX_BYTES <= max_bytes <= MAX_MAX_BYTES:
            raise ValueError("The journal budget must be 1 MiB to 1 GiB")
        self.path = Path(path)
        self.host_epoch = host_epoch
        self.max_bytes = max_bytes
        self._clock = clock
        self._writer = os.write if writer is None else writer
        self._fsync = os.fsync if fsync is None else fsync
        # O_EXCL: a journal is never reopened or appended to by a later host.
        self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
        self._append_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._forbidden = ()
        self.state = "ok"
        self.bytes = 0
        self.seq = 0
        self.lost_entries = 0
        self._prev = None

    def forbid(self, *values: str) -> None:
        """Refuse any entry that contains one of these secrets (page tokens, the agent nonce)."""
        if any(not isinstance(v, str) or len(v) < 16 for v in values):
            raise ValueError("Forbid only secrets of at least 16 characters")
        with self._append_lock:
            self._forbidden = (*self._forbidden, *(v.encode() for v in values))

    def append(self, kind: str, data: dict, *, session_epoch: str | None, monotonic_ns: int | None = None
               ) -> JournalRef:
        """Write one entry with a single write; raise JournalUnavailable when it can't be written."""
        if kind not in KINDS:
            raise ValueError(f"Unknown journal entry kind {kind}")
        if not isinstance(data, dict):
            raise TypeError("Journal entry data is a JSON object")
        with self._append_lock:
            if self.state != "ok" or self._fd is None:
                self.lost_entries += 1
                raise JournalUnavailable(self.state if self.state != "ok" else "closed")
            context = {"host_epoch": self.host_epoch, "session_epoch": session_epoch,
                       "monotonic_ns": self._clock.now_ns() if monotonic_ns is None else monotonic_ns,
                       "wall_time_utc": self._clock.wall_utc()}
            line = self._line(kind, data, context)
            if any(secret in line for secret in self._forbidden):
                # A host bug, since the broker refuses inputs that carry a secret. Fail closed: nothing is
                # written, and the journal stops, so agent authority ends at the broker's next entry.
                self.state = "failed"
                self.lost_entries += 1
                raise JournalUnavailable("failed")
            try:
                if self.bytes + len(line) + 1 <= self.max_bytes - RESERVE_BYTES:
                    return self._write(line)
                # The reserve holds exactly this one marker; then the journal stops accepting entries.
                self.state = "full"
                self._write(self._line("journal_overflow", {"refused_kind": kind, "bytes": self.bytes,
                                                            "max_bytes": self.max_bytes}, context))
            except OSError as error:
                self.state = "failed"
                self.lost_entries += 1
                raise JournalUnavailable("failed") from error
            self.lost_entries += 1
            raise JournalUnavailable("full")

    def sync(self) -> str:
        """fsync outside the broker lock. A failure marks the journal failed; returns the state."""
        with self._sync_lock:
            if self._fd is not None and self.state != "failed":
                try:
                    self._fsync(self._fd)
                except OSError:
                    self.state = "failed"
            return self.state

    def status(self) -> dict:
        return {"state": self.state, "bytes": self.bytes, "max_bytes": self.max_bytes,
                "lost_entries": self.lost_entries, "last_seq": self.seq}

    def close(self) -> None:
        with self._append_lock, self._sync_lock:
            if self._fd is None:
                return
            try:
                self._fsync(self._fd)
            except OSError:
                self.state = "failed"
            os.close(self._fd)
            self._fd = None

    def _line(self, kind, data, context) -> bytes:
        entry = {"schema": ENTRY_SCHEMA, "seq": self.seq + 1, "prev_sha256": self._prev, **context,
                 "kind": kind, "data": data}
        line = canonical_bytes(entry)
        if len(line) >= MAX_LINE_BYTES:
            # Receipts are bounded far below this; keep the entry's identity so offline reads still find it.
            kept = {k: data[k] for k in _IDENTITY_FIELDS
                    if isinstance(data.get(k), str) and len(data[k]) <= 128}
            line = canonical_bytes({**entry, "data": {**kept, "omitted_bytes": len(line),
                                                      "data_sha256": digest(data)}})
        return line

    def _write(self, line) -> JournalRef:
        payload = line + b"\n"
        written = 0
        while written < len(payload):
            count = self._writer(self._fd, payload[written:])
            if not isinstance(count, int) or count <= 0:
                raise OSError("The journal writer accepted no bytes")
            written += count
        ref = JournalRef(self.seq + 1, self.bytes)
        self.bytes += len(payload)
        self.seq += 1
        self._prev = hashlib.sha256(line).hexdigest()
        return ref


def _parse(body: bytes):
    try:
        entry = json.loads(body)
    except ValueError:
        return None
    if (not isinstance(entry, dict) or entry.get("schema") != ENTRY_SCHEMA
            or type(entry.get("seq")) is not int or entry.get("kind") not in KINDS
            or not isinstance(entry.get("data"), dict)):
        return None
    return entry


def read_entries(path, max_bytes: int = READ_LIMIT) -> tuple[list[tuple[int, dict]], dict]:
    """Read and verify a journal without a host: (trusted [(offset, entry)], chain status).

    The chain status is ``intact``; ``truncated_at_seq`` when the file ends inside a line or the
    read bound is reached; or ``chain_broken`` when a line is unreadable or its predecessor
    hash, sequence or schema disagrees. ``last_seq`` is the last trusted entry. When a line
    disagrees with its predecessor's hash, either could be the altered one, so neither is
    trusted. Nothing after a break is returned.
    """
    status = {"chain": "intact", "last_seq": 0, "bytes_read": 0, "read_limit_reached": False}
    trusted, pending, prev, offset = [], None, None, 0
    with Path(path).open("rb") as stream:
        while True:
            line = stream.readline(MAX_LINE_BYTES + 1)
            if not line:
                break
            if offset + len(line) > max_bytes:
                status.update(chain="truncated_at_seq", read_limit_reached=True)
                break
            status["bytes_read"] = offset + len(line)
            if not line.endswith(b"\n"):
                # A torn tail from a crash mid-write; an overlong line can't be a journal entry.
                status["chain"] = "chain_broken" if len(line) > MAX_LINE_BYTES else "truncated_at_seq"
                break
            body = line[:-1]
            entry = _parse(body)
            if entry is None:
                status["chain"] = "chain_broken"
                break
            expected_seq = pending[1]["seq"] + 1 if pending is not None else 1
            if entry.get("prev_sha256") != prev or entry["seq"] != expected_seq:
                status["chain"] = "chain_broken"
                pending = None
                break
            if pending is not None:
                trusted.append(pending)
            pending, prev = (offset, entry), hashlib.sha256(body).hexdigest()
            offset += len(line)
    if pending is not None:
        trusted.append(pending)
    status["last_seq"] = trusted[-1][1]["seq"] if trusted else 0
    return trusted, status


def read_request(path, request_id: str, max_bytes: int = READ_LIMIT, channel: str = "agent") -> dict:
    """Reconstruct one request's retained state from a journal; never re-executes anything.

    ``complete`` needs that channel's ``request_completed``; ``outcome_unknown`` means a step was
    admitted (admission is written before the adapter is called) with no completion;
    ``stopped_before_dispatch`` means the request started and nothing was admitted. A broken chain or
    a read stopped at its bound can't prove "nothing was sent", so those read ``outcome_unknown``. The
    broker writes ``channel`` in both ``request_started`` and ``request_completed``.
    """
    entries, chain = read_entries(path, max_bytes)
    started, completed, admitted = None, None, 0
    for _, entry in entries:
        data = entry["data"]
        if data.get("request_id") != request_id:
            continue
        if entry["kind"] == "request_started" and data.get("channel") == channel and started is None:
            started = data
        elif started is not None and entry["kind"] == "step_admitted":
            admitted += 1
        elif started is not None and entry["kind"] == "request_completed" and data.get("channel") == channel:
            completed = data
    receipt = None
    if completed is not None:
        candidate = completed.get("receipt")
        if isinstance(candidate, dict) and digest(candidate) == completed.get("receipt_sha256"):
            receipt = candidate
        state = "complete"
    elif admitted:
        state = "outcome_unknown"
    elif chain["chain"] == "chain_broken" or chain["read_limit_reached"]:
        # Entries past the break or the read bound are unread, so "nothing was sent" can't be claimed.
        state = "outcome_unknown"
    else:
        # A torn last line can only cut off entries written after it; admission is written before
        # any submit, so a request with no admission on record sent nothing.
        state = "stopped_before_dispatch" if started is not None else "not_found"
    return {"state": state, "receipt": receipt,
            "receipt_unavailable": state == "complete" and receipt is None,
            "operation": None if started is None else started.get("operation"),
            "input_sha256": None if started is None else started.get("input_sha256"),
            "steps_admitted": admitted, "journal": chain}


def read_line(path, offset: int) -> dict | None:
    """Read the one entry at a byte offset, or None when it can't be read whole."""
    if type(offset) is not int or offset < 0:
        return None
    try:
        with Path(path).open("rb") as stream:
            stream.seek(offset)
            line = stream.readline(MAX_LINE_BYTES + 1)
    except OSError:
        return None
    if not line.endswith(b"\n"):
        return None
    return _parse(line[:-1])
