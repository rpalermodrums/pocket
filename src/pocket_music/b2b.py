# SPDX-License-Identifier: AGPL-3.0-only
"""b2b: share named controls with an agent through a b2b host the person starts.

These nine providers are what Python, the CLI and MCP all call. Each sends one bounded request
to the agent socket of the host named in a sealed session, and returns the host's record
unchanged. Only ``b2b_request_status`` can also answer from the retained journal after the host
stops. Nothing here opens Serato, a controller, MIDI or an audio device: this version's only
adapter is the host's built-in synthetic two-deck mixer.

The agent channel proposes standing permissions but never issues one. Issuing a permission,
acknowledging a notice and inviting the agent happen only on the host's page.
"""
from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path

from .artifact_store import receipt
from .b2b_broker import COVERAGE, PROVENANCE, REQUEST_ID
from .b2b_journal import read_request
from .b2b_types import B2bAction, B2bSettings, ClaimBasis, EventCursor, PermissionDraft, RevokeTarget
from .error_contracts import CODES
from .errors import PocketError, valid_hint

SESSION_SCHEMA = "pocket.b2b-session/v1"
CONNECTION_SCHEMA = "pocket.b2b-connection/v1"
REQUEST_STATUS_SCHEMA = "pocket.b2b-request-status/v1"
MAX_RPC_BYTES = 256 * 1024
RPC_TIMEOUT_S = 30

_NOT_RECEIVED = ("The b2b host did not receive this request, so nothing was recorded. Check that the host "
                 "you started is still running.")
_MAY_HAVE = ("The b2b host may have recorded this request. Call b2b_request_status with this session_dir and "
             "request_id before anything else; never retry under a new request_id.")
_PREPARE_MAY_HAVE = ("The b2b host may have registered this session. Call b2b_prepare again with exactly the "
                     "same arguments; an identical retry returns the same session.")
_READ_FAILED = ("The b2b host did not answer this read, and nothing changed. Check that the host you started "
                "is still running, then read again.")
_MANIFEST_HINT = ("Pass the session_dir that b2b_prepare created, unchanged; b2b_prepare seals b2b.json and "
                  "its .sha256 there.")
_CONNECTION_HINT = ("Start a host with python -m pocket_music.b2b_host --runtime-dir <new folder> in your "
                    "own terminal, then pass that runtime_dir.")
_OLD_HOST_HINT = ("A new host holds no authority from an earlier one; prepare a new session on it. "
                  "b2b_request_status still reads this session's journal.")
_STATUS_NEXT = {
    "complete": ("The request finished; its retained receipt is above. Call b2b_observe before anything "
                 "new."),
    "running": ("The host is still running this request. Call b2b_request_status again shortly; never resend "
                "it under a new request_id."),
    "not_found": ("No host recorded this request_id, so nothing was sent for it. If the host is running, "
                  "send it again with the same request_id."),
    "stopped_before_dispatch": ("The host stopped before any step of this request was admitted, so nothing "
                                "was sent. Grants never carry over; prepare a new session on a new host."),
    "outcome_unknown": ("A step of this request may have gone out, and the journal can't show otherwise. "
                        "Never retry it automatically; observe a new host before any move."),
}


def _bad(message: str, hint: str) -> PocketError:
    return PocketError(message, code="invalid_arguments", hint=hint)


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        raise _bad(f"{name} must be a path of 1 to 4096 characters", f"Pass {name} as a string path.")
    return value


def _check_request_id(request_id) -> str:
    if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
        raise _bad("request_id must be 1-96 letters, digits, '.', '_' or '-' starting with a letter or digit",
                   "Pass a new request_id such as claim-0001 for each new request; reuse it only to retry.")
    return request_id


def _encode(message: dict) -> bytes:
    try:
        payload = json.dumps(message, allow_nan=False, separators=(",", ":")).encode() + b"\n"
    except (TypeError, ValueError) as error:
        raise _bad("b2b inputs must be finite JSON values",
                   "Pass whole numbers, strings, booleans, lists and objects; no NaN or infinity.") from error
    if len(payload) > MAX_RPC_BYTES:
        raise _bad("This b2b request is larger than 256 KiB", "Send fewer or shorter values.")
    return payload


def _seal(path: Path, value: dict) -> None:
    data = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    with path.open("xb") as stream:
        stream.write(data)
    with Path(str(path) + ".sha256").open("x") as stream:
        stream.write(hashlib.sha256(data).hexdigest() + "\n")


def _session(session_dir) -> dict:
    """The sealed manifest b2b_prepare wrote; any change to it is evidence_mismatch."""
    path = Path(_text(session_dir, "session_dir")).expanduser().resolve() / "b2b.json"
    try:
        data = path.read_bytes()
        expected = Path(str(path) + ".sha256").read_text().strip()
    except OSError as error:
        raise PocketError("Cannot read this b2b session's sealed manifest", code="io_error",
                          hint=_MANIFEST_HINT) from error
    try:
        value = json.loads(data) if hashlib.sha256(data).hexdigest() == expected else None
    except ValueError:
        value = None
    fields = ("session_id", "runtime_dir", "host_epoch")
    if (not isinstance(value, dict) or value.get("schema") != SESSION_SCHEMA
            or not all(isinstance(value.get(k), str) for k in fields)):
        raise PocketError("This b2b session manifest changed after it was sealed", code="evidence_mismatch",
                          hint=_MANIFEST_HINT)
    return value


def _connection(runtime_dir: str) -> dict:
    path = Path(runtime_dir) / "connection.json"
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise PocketError("No b2b host connection is readable in this runtime_dir", code="io_error",
                          hint=_CONNECTION_HINT) from error
    if (not isinstance(value, dict) or value.get("schema") != CONNECTION_SCHEMA
            or not all(isinstance(value.get(k), str) for k in ("socket", "nonce", "host_epoch"))):
        raise PocketError("This runtime_dir's connection.json is not a b2b host connection", code="io_error",
                          hint=_CONNECTION_HINT)
    return value


def _lost(sent: bool, mutation: bool, lost_hint: str) -> PocketError:
    if not sent:
        return PocketError("The b2b host could not be reached", code="io_error", hint=_NOT_RECEIVED)
    if mutation:
        return PocketError("The b2b host's response was lost", code="request_not_complete", hint=lost_hint)
    return PocketError("The b2b host's response was lost", code="io_error", hint=_READ_FAILED)


def _send(connection: dict, op: str, fields: dict, *, mutation: bool, lost_hint: str = _MAY_HAVE) -> dict:
    """One newline-delimited JSON request and its one response line, both bounded to 256 KiB."""
    payload = _encode({"op": op, "nonce": connection["nonce"], **fields})
    sent = False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(RPC_TIMEOUT_S)
            stream.connect(connection["socket"])
            stream.sendall(payload)
            sent = True
            raw = stream.makefile("rb").readline(MAX_RPC_BYTES + 1)
        response = json.loads(raw) if raw.endswith(b"\n") and len(raw) <= MAX_RPC_BYTES else None
    except (OSError, ValueError) as error:
        raise _lost(sent, mutation, lost_hint) from error
    if not isinstance(response, dict) or response.get("status") not in ("ok", "error"):
        raise _lost(True, mutation, lost_hint)
    if response["status"] == "error":
        code = response.get("code") if response.get("code") in CODES else "invalid_request"
        hint = response.get("hint") if valid_hint(response.get("hint")) else None
        message = str(response.get("error") or "The b2b host refused this request")
        raise PocketError(message, code=code, hint=hint)
    if not isinstance(response.get("result"), dict):
        raise _lost(True, mutation, lost_hint)
    return response["result"]


def _call(session: dict, op: str, fields: dict, *, mutation: bool) -> dict:
    connection = _connection(session["runtime_dir"])
    if connection["host_epoch"] != session["host_epoch"]:
        raise PocketError("This session's b2b host is no longer the one in its runtime_dir", code="io_error",
                          hint=_OLD_HOST_HINT)
    return _send(connection, op, {"session_id": session["session_id"], **fields}, mutation=mutation)


def _mutation(session_dir: str, op: str, request: dict) -> dict:
    session = _session(session_dir)
    _check_request_id(request["request_id"])
    _encode(request)
    return _call(session, op, {"request": request}, mutation=True)


def _triple(expected_session_epoch, expected_control_revision, expected_state_sha256) -> dict:
    return {"expected_session_epoch": expected_session_epoch,
            "expected_control_revision": expected_control_revision,
            "expected_state_sha256": expected_state_sha256}


def b2b_prepare(runtime_dir: str, output_dir: str, settings: B2bSettings | None = None,
                human_reserved: list[str] | None = None,
                proposed_policy: PermissionDraft | None = None) -> dict:
    """Bind a new session_dir to the running b2b host and record an optional proposal; grants nothing."""
    runtime = Path(_text(runtime_dir, "runtime_dir")).expanduser().resolve()
    output = Path(_text(output_dir, "output_dir")).expanduser().resolve()
    if output.exists() or output.is_symlink():
        raise PocketError("output_dir already exists", code="io_error",
                          hint="Pass a new output_dir; b2b_prepare never writes into an existing folder.")
    request = {"output_dir_resolved": str(output)}
    for name, value in (("settings", settings), ("human_reserved", human_reserved),
                        ("proposed_policy", proposed_policy)):
        if value is not None:
            request[name] = value
    _encode(request)
    connection = _connection(str(runtime))
    # The folder comes first, so a failed register leaves nothing behind and an identical retry works.
    output.mkdir(parents=True, exist_ok=False)
    try:
        registered = _send(connection, "register", {"request": request}, mutation=True,
                           lost_hint=_PREPARE_MAY_HAVE)
    except BaseException:
        output.rmdir()
        raise
    session = {"schema": SESSION_SCHEMA, "session_id": registered["session_id"], "runtime_dir": str(runtime),
               **{k: registered[k] for k in ("host_epoch", "adapter_profile", "mapping_sha256", "settings",
                                             "settings_sha256", "human_reserved", "proposal_id")}}
    _seal(output / "b2b.json", session)
    next_actions = [("Ask the person to open the b2b page link from the host terminal and keep it visible, "
                     "then call b2b_observe with this session_dir.")]
    if registered["proposal_id"] is not None:
        next_actions.append(f"Ask the person to review proposal {registered['proposal_id']} on the b2b host "
                            "page.")
    return receipt(None, "ok", change_summary={"operation": "prepare", "outcome": "prepared"},
                   coverage=dict(COVERAGE), next_actions=next_actions, provenance=dict(PROVENANCE),
                   operation="prepare", replayed=registered["replayed"],
                   outcome={"kind": "prepared", "session_dir": str(output),
                            "session_id": session["session_id"], "proposal_id": registered["proposal_id"]},
                   session_epoch=registered["session_epoch"], control_revision=registered["control_revision"],
                   state_sha256=registered["state_sha256"], revision_check="not_required",
                   monotonic_ns=registered["monotonic_ns"], host_epoch=registered["host_epoch"],
                   adapter_profile=registered["adapter_profile"], mapping_sha256=registered["mapping_sha256"],
                   settings=registered["settings"], human_reserved=registered["human_reserved"])


def b2b_authorize(session_dir: str, request_id: str, expected_session_epoch: str,
                  expected_control_revision: int, expected_state_sha256: str, permission: PermissionDraft,
                  expected_permission_id: str | None = None, expected_permission_version: int | None = None,
                  expected_permission_sha256: str | None = None) -> dict:
    """Propose a standing permission for the person to review; an agent call is always refused as issuer."""
    request = {"request_id": request_id,
               **_triple(expected_session_epoch, expected_control_revision, expected_state_sha256),
               "permission": permission}
    for name, value in (("expected_permission_id", expected_permission_id),
                        ("expected_permission_version", expected_permission_version),
                        ("expected_permission_sha256", expected_permission_sha256)):
        if value is not None:
            request[name] = value
    return _mutation(session_dir, "authorize", request)


def b2b_observe(session_dir: str) -> dict:
    """Owners, grants, notices, permissions and synthetic adapter evidence, each with its own age."""
    return _call(_session(session_dir), "observe", {}, mutation=False)


def b2b_claim(session_dir: str, request_id: str, expected_session_epoch: str, expected_control_revision: int,
              expected_state_sha256: str, resources: list[str], basis: ClaimBasis, reason: str, lease_ms: int,
              not_after_monotonic_ns: int) -> dict:
    """Claim 1-16 idle named resources at once under an invitation or the active standing permission."""
    return _mutation(session_dir, "claim", {
        "request_id": request_id,
        **_triple(expected_session_epoch, expected_control_revision, expected_state_sha256),
        "resources": resources, "basis": basis, "reason": reason, "lease_ms": lease_ms,
        "not_after_monotonic_ns": not_after_monotonic_ns})


def b2b_act(session_dir: str, request_id: str, expected_session_epoch: str, expected_control_revision: int,
            expected_state_sha256: str, grant_id: str, action: B2bAction, not_after_monotonic_ns: int,
            max_evidence_age_ms: int | None = None) -> dict:
    """One bounded move (one step, or 50 ms steps for a ramp of up to 4 s); waits for its outcome.

    ``max_evidence_age_ms`` defaults to the session's setting (500 ms unless prepared otherwise).
    """
    request = {"request_id": request_id,
               **_triple(expected_session_epoch, expected_control_revision, expected_state_sha256),
               "grant_id": grant_id, "action": action, "not_after_monotonic_ns": not_after_monotonic_ns}
    if max_evidence_age_ms is not None:
        request["max_evidence_age_ms"] = max_evidence_age_ms
    return _mutation(session_dir, "act", request)


def b2b_release(session_dir: str, request_id: str, expected_session_epoch: str,
                expected_control_revision: int, expected_state_sha256: str, grant_id: str,
                resources: list[str] | None = None) -> dict:
    """Give back some or all of an own grant; undispatched steps cancel and controls stay where they are."""
    request = {"request_id": request_id,
               **_triple(expected_session_epoch, expected_control_revision, expected_state_sha256),
               "grant_id": grant_id}
    if resources is not None:
        request["resources"] = resources
    return _mutation(session_dir, "release", request)


def b2b_revoke(session_dir: str, request_id: str, expected_session_epoch: str, expected_control_revision: int,
               expected_state_sha256: str, target: RevokeTarget) -> dict:
    """Shorten an own lease, withdraw an own proposal or drop all own authority; never expands anything."""
    return _mutation(session_dir, "revoke", {
        "request_id": request_id,
        **_triple(expected_session_epoch, expected_control_revision, expected_state_sha256),
        "target": target})


def b2b_events(session_dir: str, after: EventCursor | None = None, max_events: int = 64,
               max_bytes: int = 65536) -> dict:
    """Up to 256 sequenced events (and 192 KiB) after a cursor; an expired cursor says so."""
    request = {"max_events": max_events, "max_bytes": max_bytes}
    if after is not None:
        request["after"] = after
    return _call(_session(session_dir), "events", {"request": request}, mutation=False)


def b2b_request_status(session_dir: str, request_id: str) -> dict:
    """Inspect one retained request without redispatch, from the host or, after it stops, its journal."""
    session = _session(session_dir)
    _check_request_id(request_id)
    host, host_error = None, None
    try:
        host = _call(session, "request_status", {"request_id": request_id}, mutation=False)
    except PocketError as error:
        # The host is gone or couldn't answer; the retained journal still can, and is read either way.
        host_error = error.code
    # An answer of any kind, an error included, means a host process is still there to give it.
    host_running = host is not None or (host_error is not None and host_error != "io_error")
    journal_path = Path(session["runtime_dir"]) / "journal.ndjson"
    try:
        retained = read_request(journal_path, request_id)
    except OSError as error:
        raise PocketError("Cannot read this session's b2b journal", code="io_error",
                          hint="The journal is runtime_dir/journal.ndjson; keep the runtime_dir the host "
                               "wrote.") from error
    if host is not None and not (host["state"] == "not_found" and retained["state"] != "not_found"):
        state, found, unavailable = host["state"], host["receipt"], host["receipt_unavailable"]
    else:  # the host is gone, couldn't answer, or no longer holds a request its journal records
        host = None
        state, found, unavailable = retained["state"], retained["receipt"], retained["receipt_unavailable"]
    chain = retained["journal"]
    return {"schema": REQUEST_STATUS_SCHEMA, "request_id": request_id,
            "source": "host" if host is not None else "retained_journal",
            "host_running": host_running,
            "state": state,
            "receipt": None if found is None else {**found, "replayed": True, "historical": True},
            "receipt_unavailable": unavailable,
            "journal": {"chain": chain["chain"], "last_seq": chain["last_seq"],
                        "bytes_read": chain["bytes_read"]},
            "host_error": host_error, "redispatched": False, "automatic_retry": False,
            "durability": "process_crash_only", "next_actions": [_STATUS_NEXT[state]]}
