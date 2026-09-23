"""Shared guards for Pocket's loopback-only browser adapters.

These checks protect a local browser boundary: exact Host, same-origin writes
with a per-process CSRF token, bounded JSON bodies and restrictive headers. They
are not a sandbox against another local process.
"""
from __future__ import annotations

import hmac
import json

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
       "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


class RequestRefused(Exception):
    """A transport-level refusal with an HTTP status and a short message."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def send(handler, status, body, content_type="application/json; charset=utf-8", headers=()):
    payload = body if isinstance(body, bytes) else json.dumps(body, allow_nan=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("Content-Security-Policy", CSP)
    for name, value in headers:
        handler.send_header(name, value)
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(payload)


def guard(handler, authority, origin, csrf, write=False, refresh_message="Refresh to obtain the local session token"):
    """Send a 403 and return False unless the request matches this exact local server."""
    if handler.headers.get("Host") != authority:
        send(handler, 403, {"error": "Unexpected local host"})
        return False
    supplied = handler.headers.get("Origin")
    if supplied is not None and supplied != origin:
        send(handler, 403, {"error": "Cross-origin requests are not allowed"})
        return False
    if handler.headers.get("Sec-Fetch-Site") == "cross-site":
        send(handler, 403, {"error": "Cross-site requests are not allowed"})
        return False
    # Compare bytes: headers arrive as latin-1 text, and str comparison rejects non-ASCII.
    token = handler.headers.get("X-Pocket-CSRF", "").encode("latin-1", "replace")
    if write and (supplied != origin or not hmac.compare_digest(token, csrf.encode())):
        send(handler, 403, {"error": refresh_message})
        return False
    return True


def read_json(handler, limit):
    """Read one bounded JSON object; chunked or non-JSON bodies are refused."""
    if handler.headers.get_content_type() != "application/json" or handler.headers.get("Transfer-Encoding"):
        raise RequestRefused(415, "Use a bounded JSON request")
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError as error:
        raise RequestRefused(400, "Invalid Content-Length") from error
    if not 0 < length <= limit:
        raise RequestRefused(413, f"JSON body must be 1–{limit} bytes")
    try:
        body = json.loads(handler.rfile.read(length))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise RequestRefused(400, "Malformed JSON") from error
    if not isinstance(body, dict):
        raise RequestRefused(400, "Request must be a JSON object")
    return body
