# SPDX-License-Identifier: AGPL-3.0-only
"""The b2b host's trusted loopback page: notices, owners, Stop, holds, invitations and policy.

Two tokens guard it. The issuer token lives only in host memory and in the page link the host
prints to its own terminal (as the URL fragment); it gates everything that can increase agent
authority, including notice acknowledgements. The public CSRF token is served by
``GET /api/public`` and gates only the reduce-only routes (Stop agent control, Hold, Deny,
Take it back, Revoke, Withdraw), so those keep working after a reload.

This is a local boundary only: any process that can reach the port can trigger the reductions,
and an agent that starts the host itself sees the link. ``TrustedPage`` holds every operation
as a validated method that runs without a socket; the HTTP handler only routes to it.
"""
from __future__ import annotations

import hmac
import re
import secrets
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .errors import PocketError
from .loopback import RequestRefused, guard, read_json, send

MAX_BODY = 16 * 1024
WAIT_SLICES, WAIT_SLICE_S = 20, 0.5  # a long-poll waits at most 10 s in total
_WEB = Path(__file__).parent / "web" / "b2b"
_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
          "/b2b.js": ("b2b.js", "text/javascript; charset=utf-8"),
          "/b2b-model.mjs": ("b2b-model.mjs", "text/javascript; charset=utf-8"),
          "/b2b.css": ("b2b.css", "text/css; charset=utf-8")}
_PUBLIC_POSTS = {"/api/panic": "panic", "/api/holds": "hold", "/api/notices/deny": "deny",
                 "/api/grants/revoke": "revoke_grant", "/api/permissions/revoke": "revoke_permission",
                 "/api/invitations/withdraw": "withdraw_invitation"}
_ISSUER_POSTS = {"/api/attach": "attach", "/api/presence": "presence", "/api/notices/ack": "ack",
                 "/api/invitations": "invite", "/api/holds/release": "release_hold",
                 "/api/pickup/accept": "accept_pickup", "/api/policy/preview": "preview",
                 "/api/policy/issue": "issue", "/api/policy/amend": "amend",
                 "/api/proposals/dismiss": "dismiss"}
_SYNTHETIC_POSTS = {"/api/synthetic/input": "synthetic_input", "/api/synthetic/fault": "synthetic_fault"}
_FAULTS = {"disconnect": "disconnect_controller", "reconnect": "reconnect_controller",
           "lose_route": "lose_route", "restore_route": "restore_route", "restart_writer": "restart_writer"}
_LOCKED = ("Locked: reopen the page link from the host terminal to acknowledge notices or issue permissions. "
           "Stop, Hold and Deny still work.")
_ABSOLUTE_PATH = re.compile(r"(?<![\w.>-])/[^\s'\"]+")


def _bad(message: str, hint: str) -> PocketError:
    return PocketError(message, code="invalid_arguments", hint=hint)


def _only(body, names, where: str) -> dict:
    if not isinstance(body, dict) or set(body) - set(names):
        raise _bad(f"{where} takes only {', '.join(names) or 'an empty object'}",
                   "Refresh the page; it sends only the fields each action declares.")
    return body


class TrustedPage:
    """Every trusted-page operation as a validated method. The HTTP handler only routes here.

    ``adapter`` is the synthetic adapter, for the demo panel and its injection routes only;
    those call nothing but the adapter's own injection methods, so input reaches the broker
    exactly as a controller's would.
    """

    def __init__(self, broker, channel, *, adapter=None, sync=None):
        self._broker = broker
        self._channel = channel
        self._adapter = adapter
        self._sync = sync  # the journal's fsync, run after each page write and before it is answered
        self.issuer_token = secrets.token_urlsafe(32)
        self.public_csrf = secrets.token_urlsafe(32)

    @property
    def synthetic(self) -> bool:
        return self._adapter is not None

    def public(self) -> dict:
        return {**self._broker.trusted_public(), "csrf_token": self.public_csrf}

    def panic(self, body) -> dict:
        return self._broker.panic(body)

    def hold(self, body) -> dict:
        return self._broker.reduce_hold(body)

    def deny(self, body) -> dict:
        return self._broker.reduce_deny(body)

    def revoke_grant(self, body) -> dict:
        return self._broker.reduce_revoke_grant(body)

    def revoke_permission(self, body) -> dict:
        return self._broker.reduce_revoke_permission(body)

    def withdraw_invitation(self, body) -> dict:
        return self._broker.reduce_withdraw_invitation(body)

    def attach(self, body) -> dict:
        _only(body, (), "attach")
        return {"ui_epoch": self._broker.trusted_attach(self._channel)}

    def presence(self, body) -> dict:
        _only(body, ("ui_epoch", "visible"), "presence")
        return self._broker.trusted_presence(self._channel, body.get("ui_epoch"), body.get("visible"))

    def state(self, ui_epoch) -> dict:
        """The full snapshot. For the current page it counts as presence and as a notice fetch."""
        snapshot = self._broker.trusted_state(self._channel, ui_epoch)
        snapshot["synthetic_panel"] = self._panel() if self.synthetic else None
        return snapshot

    def _panel(self) -> dict:
        document = self._adapter.profile  # the panel sends exactly the messages this profile declares
        return {**self._adapter.panel_state(), "inputs": document["physical_inputs"],
                "aliases": document["aliases"], "gestures": document["gestures"]}

    def wait(self, ui_epoch, after_event) -> dict:
        """Long-poll up to 10 s until the event sequence passes ``after_event``; then the snapshot."""
        if not isinstance(after_event, str) or not after_event.isdigit() or len(after_event) > 19:
            raise _bad("after_event must be a decimal event sequence",
                       "Pass the events_cursor sequence from the last snapshot.")
        after = int(after_event)
        if not self._broker.trusted_poll(self._channel, ui_epoch, True)["current"]:
            return self.state(ui_epoch)  # a page epoch that no longer counts: answer at once
        try:
            for _ in range(WAIT_SLICES):
                if self._broker.wait_for_event(after, WAIT_SLICE_S) > after:
                    break
        finally:
            self._broker.trusted_poll(self._channel, ui_epoch, False)
        return self.state(ui_epoch)

    def ack(self, body) -> dict:
        return self._broker.trusted_ack(self._channel, body)

    def invite(self, body) -> dict:
        return self._broker.trusted_invite(self._channel, body)

    def release_hold(self, body) -> dict:
        return self._broker.trusted_release_hold(self._channel, body)

    def accept_pickup(self, body) -> dict:
        return self._broker.trusted_accept_pickup(self._channel, body)

    def preview(self, body) -> dict:
        return self._broker.trusted_preview(self._channel, body)

    def issue(self, body) -> dict:
        return self._broker.trusted_issue(self._channel, body)

    def amend(self, body) -> dict:
        return self._broker.trusted_amend(self._channel, body)

    def dismiss(self, body) -> dict:
        return self._broker.trusted_dismiss_proposal(self._channel, body)

    def synthetic_input(self, body) -> dict:
        self._require_synthetic()
        _only(body, ("message", "origin", "held_ms", "cancelled"), "synthetic input")
        message, origin = body.get("message"), body.get("origin", "controller")
        held_ms = body.get("held_ms")
        if held_ms is not None and (type(held_ms) is not int or not 0 <= held_ms <= 600_000):
            raise _bad("held_ms is a whole number of milliseconds from 0 to 600000",
                       "Send how long the pad was held, or leave it out.")
        if (not isinstance(message, list) or not 1 <= len(message) <= 3
                or any(type(b) is not int or not 0 <= b <= 255 for b in message)):
            raise _bad("message is a list of one to three bytes", "Send [status, data1, data2] as integers.")
        if origin not in ("controller", "feedback"):
            raise _bad("origin is controller or feedback", "Leave origin out for a controller message.")
        cancelled = body.get("cancelled", False)
        if type(cancelled) is not bool:
            raise _bad("cancelled is true or false",
                       "Send cancelled: true only when a Stop or Hold cancelled the press.")
        events = self._adapter.inject_message(message, origin, held_ms=held_ms, cancelled=cancelled)
        return {"delivered": len(events), "types": [event["type"] for event in events]}

    def synthetic_fault(self, body) -> dict:
        self._require_synthetic()
        _only(body, ("action",), "synthetic fault")
        action = body.get("action")
        if action not in _FAULTS:
            raise _bad(f"action is one of {', '.join(_FAULTS)}", "Use the synthetic panel's fault buttons.")
        getattr(self._adapter, _FAULTS[action])()
        return {"applied": action, "panel": self._panel()}

    def sync(self) -> str:
        return "ok" if self._sync is None else self._sync()

    def _require_synthetic(self) -> None:
        if not self.synthetic:
            raise RequestRefused(404, "No synthetic adapter on this host")


class _Server(ThreadingHTTPServer):
    daemon_threads = True


class _Handler(BaseHTTPRequestHandler):
    server_version = "PocketB2b"

    def log_message(self, *_args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def _guard(self, csrf: str, write: bool) -> bool:
        return guard(self, self.server.authority, self.server.origin, csrf, write, _LOCKED)

    def _issuer(self) -> bool:
        token = self.headers.get("X-Pocket-CSRF", "").encode("latin-1", "replace")
        if not hmac.compare_digest(token, self.server.page.issuer_token.encode()):
            send(self, 403, {"error": _LOCKED})
            return False
        return True

    def _refuse(self, error) -> None:
        if isinstance(error, RequestRefused):
            return send(self, error.status, {"error": str(error)})
        message = _ABSOLUTE_PATH.sub("<path>", str(error))
        if error.code == "stale_revision":
            return send(self, 409, {"error": message, "code": "stale_page_state", "hint": error.hint})
        status = 400 if error.code in ("invalid_arguments", "invalid_request", "unsupported_profile") else 409
        return send(self, status, {"error": message, "code": error.code, "hint": error.hint})

    def _fail(self) -> None:
        # A host bug, not a refusal: say so on stderr and to the page, and never hide it.
        traceback.print_exc(file=sys.stderr)
        send(self, 500, {"error": "The b2b host failed while handling this request; see the host terminal."})

    def do_HEAD(self):
        send(self, 405, {"error": "Use GET or POST"})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD

    def do_GET(self):
        page = self.server.page
        if not self._guard(page.public_csrf, False):
            return
        parsed = urlsplit(self.path)
        if parsed.path in _FILES:
            filename, mime = _FILES[parsed.path]
            return send(self, 200, (_WEB / filename).read_bytes(), mime)
        try:
            if parsed.path == "/api/public":
                return send(self, 200, page.public())
            if parsed.path in ("/api/state", "/api/wait"):
                if not self._issuer():
                    return None
                query = parse_qs(parsed.query)
                ui_epoch = query.get("ui_epoch", [None])[0]
                if parsed.path == "/api/state":
                    return send(self, 200, page.state(ui_epoch))
                return send(self, 200, page.wait(ui_epoch, query.get("after_event", [None])[0]))
        except (PocketError, RequestRefused) as error:
            return self._refuse(error)
        except Exception:  # noqa: BLE001 - a host bug is reported, never swallowed
            return self._fail()
        return send(self, 404, {"error": "Unknown b2b page endpoint"})

    def do_POST(self):
        page = self.server.page
        path = urlsplit(self.path).path
        if path in _PUBLIC_POSTS:
            method, csrf = _PUBLIC_POSTS[path], page.public_csrf
        elif path in _ISSUER_POSTS or (path in _SYNTHETIC_POSTS and page.synthetic):
            method, csrf = {**_ISSUER_POSTS, **_SYNTHETIC_POSTS}[path], page.issuer_token
        else:
            if self._guard(page.public_csrf, False):
                send(self, 404, {"error": "Unknown b2b page action"})
            return
        if not self._guard(csrf, True):
            return
        try:
            body = read_json(self, MAX_BODY)
            result = getattr(page, method)(body)
            # fsync outside the broker lock, before the answer leaves the host
            return send(self, 200, with_sync_warning(result, page.sync()))
        except (PocketError, RequestRefused) as error:
            return self._refuse(error)
        except Exception:  # noqa: BLE001 - a host bug is reported, never swallowed
            return self._fail()


def with_sync_warning(result: dict, state: str) -> dict:
    """Say so on the answer when the journal could not be synced after this write."""
    if state == "ok":
        return result
    warning = ("journal_full: the journal reached its budget, so agent control has stopped."
               if state == "full" else
               "journal_sync_failed: this outcome may not survive a host crash, and agent control has "
               "stopped.")
    return {**result, "warnings": [*result.get("warnings", []), warning]}


def make_page_server(page: TrustedPage, port: int = 0) -> ThreadingHTTPServer:
    """Bind 127.0.0.1 only. The caller serves it and prints the link with the issuer token."""
    if type(port) is not int or not 0 <= port <= 65535:
        raise PocketError("The b2b page port must be 0-65535", code="invalid_arguments",
                          hint="Pass --port 0 to let the host choose a free loopback port.")
    server = _Server(("127.0.0.1", port), _Handler)
    server.page = page
    server.authority = f"127.0.0.1:{server.server_port}"
    server.origin = "http://" + server.authority
    return server


def page_link(server) -> str:
    """The private page address: the issuer token travels only in the fragment."""
    return f"{server.origin}/#k={server.page.issuer_token}"
