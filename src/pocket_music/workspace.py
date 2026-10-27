# SPDX-License-Identifier: AGPL-3.0-only
"""Loopback-only human workspace over the shared Weave and Whisker providers."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .errors import PocketError
from .loopback import guard, send
from .selection_types import BagHandle

_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8"),
          "/pip.svg": ("pip.svg", "image/svg+xml")}
_POSTS = {"/api/import", "/api/routes", "/api/feedback", "/api/session", "/api/session/action",
          "/api/session/attach"}


class _Workspace:
    def __init__(self, folder: Path, bag_handle: BagHandle | None):
        from .record_bag import load_record_bag
        self.folder = folder
        self.mutex = threading.RLock()
        self.csrf = secrets.token_urlsafe(32)
        folder.mkdir(parents=True, exist_ok=True)
        self.lock = (folder / ".workspace.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock.close()
            raise PocketError("This workspace is already served by another process") from exc
        try:
            self.path = folder / "workspace.json"
            if self.path.exists():
                state = self.read()
                if bag_handle is not None and state["bag"] != bag_handle:
                    raise PocketError("Existing workspace has a different bag; import or choose a new workspace")
                if state["bag"]:
                    load_record_bag(state["bag"])
            else:
                if bag_handle:
                    load_record_bag(bag_handle)
                self.write({"schema": "pocket.workspace/v1", "revision": 0, "bag": bag_handle,
                            "plan": None, "session_dir": None})
        except Exception:
            self.lock.close()
            raise

    def read(self):
        try:
            state = json.loads(self.path.read_text())
            if state.get("schema") != "pocket.workspace/v1" or type(state.get("revision")) is not int:
                raise ValueError("schema")
            return state
        except (OSError, ValueError, AttributeError) as exc:
            raise PocketError("Workspace state is unreadable; preserve it and choose a new directory") from exc

    def write(self, state):
        temporary = self.folder / (".workspace-" + uuid.uuid4().hex)
        with temporary.open("x") as stream:
            json.dump(state, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    def child(self, group):
        return str(self.folder / group / uuid.uuid4().hex)

    def session_path(self, state):
        if not state["session_dir"]:
            raise PocketError("Prepare a Whisker session first")
        target = (self.folder / state["session_dir"]).resolve()
        if not target.is_relative_to(self.folder):
            raise PocketError("Session path must stay inside this workspace")
        return str(target)

    def snapshot(self):
        from .record_bag import load_record_bag
        from .weave import load_set_plan
        from .whisker import session_snapshot
        state = self.read()
        bag = load_record_bag(state["bag"]) if state["bag"] else None
        session = session_snapshot(self.session_path(state)) if state["session_dir"] else None
        current = next((t for t in bag["tracks"] if session and t["track_id"] == session["current_track_id"]),
                       None) if bag else None
        lookup = {track["track_id"]: track for track in bag["tracks"]} if bag else {}
        history = [{**entry, "track": {key: lookup[entry["track_id"]][key]
                                     for key in ("track_id", "title", "artists")}
                    if entry.get("track_id") in lookup else None}
                   for entry in (session["history"][-15:] if session else [])]
        return {"revision": state["revision"], "csrf_token": self.csrf,
                "bag": {"title": bag["title"], "summary": bag["summary"]} if bag else None,
                "plan_handle": state["plan"],
                "plan": load_set_plan(state["plan"]) if state["plan"] else None,
                "session": session, "history": history,
                "current_track": {k: current[k] for k in ("track_id", "title", "artists")}
                if current else None}

    def mutate(self, endpoint, data):
        from .record_bag import create_record_bag
        from .weave import plan_set_routes, record_plan_feedback, replan_set
        from .whisker import prepare_session, session_snapshot, update_session
        state = self.read()
        revision = data.get("expected_workspace_revision")
        if type(revision) is not int or revision != state["revision"]:
            raise PocketError("Workspace changed; refresh before making this change")
        if endpoint == "/api/import":
            tracks = data.get("tracks")
            if not isinstance(tracks, list):
                raise PocketError("Import a JSON object with a tracks list")
            if any(not isinstance(t, dict) or any(k in t for k in ("local_path", "audio", "path")) for t in tracks):
                raise PocketError("Browser imports accept catalog fields; initialize verified local audio with a bag handle")
            result = create_record_bag(tracks, self.child("bags"), data.get("title", "Imported records"))
            state.update(bag=result["handle"], plan=None, session_dir=None)
        elif not state["bag"]:
            raise PocketError("Import or initialize a record bag first")
        elif endpoint == "/api/routes":
            if data.get("use_feedback") and state["plan"]:
                result = replan_set(state["plan"], self.child("plans"), seed=data.get("seed", 0),
                                    brief=data.get("brief"), route_count=3)
            else:
                result = plan_set_routes(state["bag"], data.get("brief", {}), self.child("plans"),
                                         seed=data.get("seed", 0), route_count=3)
            state["plan"] = result["handle"]
        elif endpoint == "/api/feedback":
            if not state["plan"]:
                raise PocketError("Make routes before leaving feedback")
            if data.get("expected_plan_sha256") != state["plan"]["sha256"]:
                raise PocketError("Route plan changed; refresh before leaving feedback")
            result = record_plan_feedback(state["plan"], data.get("route_id"), data.get("disposition"),
                                          self.child("plans"), from_track_id=data.get("from_track_id"),
                                          to_track_id=data.get("to_track_id"), note=data.get("note", ""))
            state["plan"] = result["handle"]
        elif endpoint == "/api/session":
            result = prepare_session(state["bag"], self.child("sessions"),
                                     current_track_id=data.get("track_id"), intent=data.get("intent"))
            state["session_dir"] = str(Path(result["session_dir"]).relative_to(self.folder))
        elif endpoint == "/api/session/attach":
            relative = data.get("session_dir")
            if not isinstance(relative, str) or Path(relative).is_absolute():
                raise PocketError("Use a session path relative to this workspace")
            trial = {**state, "session_dir": relative}
            result = session_snapshot(self.session_path(trial))
            if result["bag"] != state["bag"]:
                raise PocketError("Session uses a different bag revision")
            state["session_dir"] = relative
        elif endpoint == "/api/session/action":
            result = update_session(self.session_path(state), expected_revision=data.get("expected_revision"),
                                    expected_sha256=data.get("expected_sha256"), action=data.get("action"),
                                    track_id=data.get("track_id"), intent=data.get("intent"), note=data.get("note"))
        else:
            raise PocketError("Unsupported workspace action")
        state["revision"] += 1
        self.write(state)
        return self.snapshot()


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_close(self):
        super().server_close()
        self.workspace.lock.close()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        send(self, status, body, content_type)

    def _guard(self, write=False):
        return guard(self, self.server.authority, self.server.origin, self.server.workspace.csrf, write,
                     "Refresh this workspace to obtain its local session token")

    def do_GET(self):
        if not self._guard():
            return
        parsed = urlsplit(self.path)
        if parsed.path in _FILES:
            filename, mime = _FILES[parsed.path]
            return self._send(200, (Path(__file__).parent / "web" / filename).read_bytes(), mime)
        workspace = self.server.workspace
        try:
            with workspace.mutex:
                state = workspace.read()
                if parsed.path == "/api/state":
                    result = workspace.snapshot()
                elif parsed.path == "/api/tracks":
                    from .record_bag import query_record_bag
                    query = parse_qs(parsed.query)
                    result = query_record_bag(state["bag"], query.get("q", [""])[0], limit=40,
                                              offset=int(query.get("offset", ["0"])[0])) if state["bag"] else {
                                                  "tracks": [], "total_matches": 0, "next_offset": None}
                elif parsed.path == "/api/options":
                    from .whisker import session_options
                    result = session_options(workspace.session_path(state), limit=6)
                else:
                    return self._send(404, {"error": "Unknown workspace endpoint"})
            self._send(200, result)
        except (PocketError, ValueError, TypeError, KeyError) as exc:
            self._send(409, {"error": str(exc)})

    def do_POST(self):
        if not self._guard(write=True):
            return
        if self.path not in _POSTS:
            return self._send(404, {"error": "Unknown workspace action"})
        try:
            if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
                return self._send(415, {"error": "Use a bounded JSON request"})
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2_000_000:
                return self._send(413, {"error": "JSON import must be under 2 MB"})
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise PocketError("Request must be a JSON object")
            with self.server.workspace.mutex:
                result = self.server.workspace.mutate(self.path, body)
            self._send(200, result)
        except (PocketError, ValueError, TypeError, KeyError) as exc:
            self._send(409, {"error": str(exc)})


def _make_server(workspace_dir: str, bag_handle: BagHandle | None = None,
                 host: str = "127.0.0.1", port: int = 0) -> _Server:
    if host != "127.0.0.1" or type(port) is not int or not 0 <= port <= 65535:
        raise PocketError("Workspace must bind 127.0.0.1 with port 0–65535")
    workspace = _Workspace(Path(workspace_dir).expanduser().resolve(), bag_handle)
    try:
        server = _Server((host, port), _Handler)
    except Exception:
        workspace.lock.close()
        raise
    server.workspace = workspace
    server.authority = f"127.0.0.1:{server.server_port}"
    server.origin = "http://" + server.authority
    return server


def start_workspace(workspace_dir: str, bag_handle: BagHandle | None = None,
                    host: str = "127.0.0.1", port: int = 0) -> None:
    """Print the local URL and serve until interrupted. Never controls decks or playback."""
    server = _make_server(workspace_dir, bag_handle, host, port)
    print(json.dumps({"url": server.origin, "workspace_dir": str(server.workspace.folder)}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
