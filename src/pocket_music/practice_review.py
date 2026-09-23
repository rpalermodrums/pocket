"""Loopback-only practice review over one selected comparison.

The launcher is an adapter, not a provider: every preview and report goes
through the public `practice_preview`, `practice_feedback` and
`practice_feedback_query` functions. Only the comparison chosen at launch and
its member renders are reachable; there is no store browser or path parameter.
Playback in the browser never creates a report; a person must confirm the
interval, author and note.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .artifact_store import canonical_bytes, read_bytes, read_record, verification_scope
from .errors import PocketError
from .loopback import RequestRefused, guard, read_json, send

SESSION_SCHEMA = "pocket.practice-review-session/v1"
PREVIEW_PROFILE = "browser-pcm16-original-rate/v1"
MAX_REPORTS = 128
MAX_BODY = 64 * 1024
_WEB = Path(__file__).parent / "web" / "practice-review"
_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
          "/review.js": ("review.js", "text/javascript; charset=utf-8"),
          "/review-model.mjs": ("review-model.mjs", "text/javascript; charset=utf-8"),
          "/review.css": ("review.css", "text/css; charset=utf-8")}
_ITEM = re.compile(r"(baseline|variant-[1-8])")
_CLIENT_REQUEST = re.compile(r"[0-9a-f]{32}")
_RANGE = re.compile(r"bytes=(\d{0,18})-(\d{0,18})")
_ABSOLUTE_PATH = re.compile(r"(?<![\w.>-])/[^\s'\"]+")
COMPARISON_LABELS = {"pocket.practice-comparison/v1": "Same context revision",
                     "pocket.practice-revision-comparison/v1": "Edited revisions with explicit correspondence",
                     "pocket.practice-processed-comparison/v1": "Declared processing of one exact baseline"}


def _load_handle(path, key):
    """Accept a bare handle or a receipt containing artifacts[key]."""
    try:
        value = json.loads(Path(path).expanduser().read_text())
    except (OSError, ValueError) as error:
        raise PocketError(f"Cannot read {key} file: {error}") from error
    if isinstance(value, dict) and isinstance(value.get("artifacts"), dict) and key in value["artifacts"]:
        value = value["artifacts"][key]
    if not isinstance(value, dict) or value.get("schema") != "pocket.artifact-handle/v1":
        raise PocketError(f"Expected a {key} handle or a receipt containing artifacts.{key}")
    return value


def _load_report_handles(path):
    try:
        value = json.loads(Path(path).expanduser().read_text())
    except (OSError, ValueError) as error:
        raise PocketError(f"Cannot read reports file: {error}") from error
    if not isinstance(value, list) or len(value) > MAX_REPORTS:
        raise PocketError(f"Reports file must be a list of at most {MAX_REPORTS} feedback handles or receipts")
    handles = []
    for item in value:
        if isinstance(item, dict) and isinstance(item.get("artifacts"), dict) and "feedback" in item["artifacts"]:
            item = item["artifacts"]["feedback"]
        handles.append(item)
    return handles


def _short(sha):
    return sha[:12]


def _processing(render):
    profile = render["processing"]["profile"]
    if profile == "linear-loop-join-envelope/v1":
        rate = render["signal"]["sample_rate"]
        joins = "; ".join(f"{j['fade_out_frames']} frames out / {j['fade_in_frames']} in at "
                          f"{j['boundary_frame'] / rate:.3f} s" for j in render["joins"])
        return {"profile": profile, "label": f"Declared linear join envelope ({joins})", "source_exact": False}
    return {"profile": profile, "label": "Exact source samples at original rate", "source_exact": True}


def _alignment(schema, record, renders):
    """Switch at the same output frame only when the comparison itself proves correspondence."""
    if schema == "pocket.practice-processed-comparison/v1" and len({canonical_bytes(r["mappings"]) for r in renders}) == 1:
        return True, "Every member derives from one exact baseline with identical timing and frame counts"
    if schema == "pocket.practice-revision-comparison/v1" and all(
            pair["baseline_interval_frames"] == pair["variant_interval_frames"]
            for item in record["correspondence"] for pair in item["pairs"]):
        return True, ("The explicit occurrence correspondence pairs every occurrence at identical output frames, "
                      "so the same frame is the same position within its paired occurrence")
    return False, "No frame correspondence is established for switching; each item keeps its own position"


class _Review:
    """One comparison, its members and a small disposable index of retained handles."""

    def __init__(self, store_root, comparison, session_dir, report_handles=()):
        from .practice_audio import load_comparison
        self.store = str(Path(store_root).expanduser().resolve())
        self.folder = Path(session_dir).expanduser().resolve()
        self.mutex = threading.RLock()
        self.preview_slot = threading.BoundedSemaphore(1)
        self.csrf = secrets.token_urlsafe(32)
        record, renders = load_comparison(comparison, self.store)
        self.comparison, self.record = comparison, record
        handles = [record["baseline"], *record["variants"]]
        self.items = {("baseline" if i == 0 else f"variant-{i}"): {"render": h, "record": r}
                      for i, (h, r) in enumerate(zip(handles, renders, strict=True))}
        self.folder.mkdir(parents=True, exist_ok=True)
        self.lock = (self.folder / ".review.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.lock.close()
            raise PocketError("This review session is already served by another process") from error
        try:
            self.path = self.folder / "review-session.json"
            if self.path.exists():
                state = self.read()
                if state["comparison"] != comparison:
                    raise PocketError("This session directory belongs to a different comparison; choose a new one")
            else:
                state = {"schema": SESSION_SCHEMA, "revision": 0, "comparison": comparison, "previews": {},
                         "reports": []}
            extra = [h for h in report_handles if h not in state["reports"]]
            if extra:
                self._verify_reports(extra)
                state["reports"] = [*state["reports"], *extra]
                if len(state["reports"]) > MAX_REPORTS:
                    raise PocketError(f"A review session holds at most {MAX_REPORTS} reports")
                state["revision"] += 1
            self.write(state)
        except Exception:
            self.lock.close()
            raise

    def read(self):
        try:
            state = json.loads(self.path.read_text())
        except (OSError, ValueError) as error:
            raise PocketError("Review session index is unreadable; preserve it and choose a new directory") from error
        if (not isinstance(state, dict) or state.get("schema") != SESSION_SCHEMA
                or set(state) != {"schema", "revision", "comparison", "previews", "reports"}
                or type(state["revision"]) is not int or not isinstance(state["previews"], dict)
                or not isinstance(state["reports"], list) or len(state["reports"]) > MAX_REPORTS
                or any(key not in self.items for key in state["previews"])):
            raise PocketError("Review session index is malformed; preserve it and choose a new directory")
        return state

    def write(self, state):
        temporary = self.folder / (".review-" + uuid.uuid4().hex)
        with temporary.open("x") as stream:
            json.dump(state, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    def _verify_reports(self, handles):
        """Fully validate supplied reports and require membership in this comparison."""
        from .practice_feedback_query import practice_feedback_query
        practice_feedback_query(self.store, handles, limit=128, max_bytes=65536)  # validates every handle
        for handle in handles:
            if read_record(handle, self.store)["comparison"] != self.comparison:
                raise PocketError("Every report must belong to the selected comparison")

    def item(self, item_id):
        if not isinstance(item_id, str) or not _ITEM.fullmatch(item_id) or item_id not in self.items:
            raise RequestRefused(404, "Unknown review item")
        return self.items[item_id]

    def preview_for(self, state, item_id):
        """Fully revalidate the retained preview for one item, or None."""
        from .practice_previews import load_practice_preview
        handle = state["previews"].get(item_id)
        if handle is None:
            return None
        record, _ = load_practice_preview(handle, self.store)
        if record["parent"] != self.items[item_id]["render"]:
            raise PocketError("Session preview does not belong to this item", code="evidence_mismatch")
        return handle, record

    def summary(self):
        from .practice_audio import load_comparison
        state = self.read()
        record, renders = load_comparison(self.comparison, self.store)
        items = []
        for (item_id, item), render in zip(self.items.items(), renders, strict=True):
            signal = render["signal"]
            preview = self.preview_for(state, item_id)
            items.append({
                "item_id": item_id, "role": "baseline" if item_id == "baseline" else "variant",
                "label": "Baseline" if item_id == "baseline" else f"Variant {item_id.split('-')[1]}",
                "short_id": _short(render["audio"]["sha256"]), "processing": _processing(render),
                "frames": signal["frames"], "sample_rate": signal["sample_rate"], "channels": signal["channels"],
                "signal": {k: signal[k] for k in ("disposition", "usable_for_expectation", "sample_peak", "rms",
                                                  "sample_overload_count")},
                "occurrences": [{"occurrence_id": m["occurrence_id"], "output_span_frames": m["output_span_frames"]}
                                for m in render["mappings"]],
                "preview": None if preview is None else self._preview_summary(preview[0], preview[1])})
        synchronized, basis = _alignment(self.comparison["artifact_schema"], record, renders)
        # Hash-verified records; the reports endpoint performs full provider validation.
        kinds = [read_record(handle, self.store)["actor_kind"] for handle in state["reports"]]
        return {"revision": state["revision"], "csrf_token": self.csrf, "question": record["question"],
                "comparison": {"short_id": _short(self.comparison["sha256"]),
                               "family": COMPARISON_LABELS[self.comparison["artifact_schema"]],
                               "signal_ready": record["signal_ready"]},
                "alignment": {"synchronized_switching": synchronized, "basis": basis},
                "items": items, "report_count": len(state["reports"]),
                "human_report_count": kinds.count("human"), "agent_report_count": kinds.count("agent"),
                "max_reports": MAX_REPORTS,
                "listening": "not_recorded_by_playback"}

    @staticmethod
    def _preview_summary(handle, record):
        stats = record["quantization"]
        return {"short_id": _short(record["audio"]["sha256"]), "sha256": record["audio"]["sha256"],
                "profile": record["profile"], "bytes": record["format"]["bytes"],
                "exact": stats["exact_samples"] == stats["samples"],
                "rounded_samples": stats["samples"] - stats["exact_samples"],
                "max_abs_error_lsb": stats["max_abs_error_lsb"],
                "label": ("PCM16 copy, every sample identical to the render" if stats["exact_samples"] == stats["samples"]
                          else f"PCM16 copy, {stats['samples'] - stats['exact_samples']} samples rounded "
                               f"(≤ {stats['max_abs_error_lsb']:g} LSB), no dither"),
                "preview": handle}

    def detail(self, item_id):
        from .practice_audio import load_practice_audio
        item = self.item(item_id)
        render = load_practice_audio(item["render"], self.store)
        state = self.read()
        preview = self.preview_for(state, item_id)
        context = render["context"]
        return {"item_id": item_id, "render": item["render"], "audio_sha256": render["audio"]["sha256"],
                "context_sha256": context["sha256"], "processing": render["processing"],
                "joins": render.get("joins"), "attribution": render.get("attribution"),
                "signal": render["signal"], "input_signal": render.get("input_signal"),
                "mappings": render["mappings"][:32], "mapping_total": len(render["mappings"]),
                "preview": None if preview is None else {
                    **self._preview_summary(*preview), "format": preview[1]["format"],
                    "conversion": preview[1]["conversion"], "quantization": preview[1]["quantization"],
                    "signal": preview[1]["signal"], "playback": preview[1]["playback"]}}

    def create_preview(self, data):
        from .practice_previews import practice_preview
        item_id = data.get("item_id")
        item = self.item(item_id)
        if not self.preview_slot.acquire(blocking=False):
            raise RequestRefused(429, "Another preview is being prepared; try again when it finishes")
        try:
            with self.mutex:
                self._expect(data)
            request_id = self._preview_request(item["render"])
            result = practice_preview(self.store, request_id, item["render"], PREVIEW_PROFILE)
            with self.mutex:
                state = self.read()
                handle = result["artifacts"]["preview"]
                if state["previews"].get(item_id) != handle:
                    state["previews"][item_id] = handle
                    state["revision"] += 1
                    self.write(state)
            return {"item_id": item_id, "warnings": result["warnings"], "state": self.summary()}
        finally:
            self.preview_slot.release()

    def _preview_request(self, render):
        """Reuse a completed preview request; otherwise evaluate under a fresh request ID.

        A failed or interrupted journal is left untouched for inspection, never
        replayed, deleted or unlocked. Because a preview is a deterministic,
        content-addressed derivative, the next numbered request ID can safely run the
        provider again, so a refusal is explained on every attempt and an interrupted
        attempt never blocks the item.
        """
        from .artifact_store import request_status
        base = "review-preview-" + render["sha256"][:40]
        folder = Path(self.store) / "requests"
        numbers = [1] if (folder / base).exists() else []
        if folder.is_dir():
            numbers += [int(name.rsplit("-", 1)[1]) for name in os.listdir(folder)
                        if re.fullmatch(re.escape(base) + r"-[0-9]{1,9}", name)]
        for number in sorted(numbers):
            request_id = base if number == 1 else f"{base}-{number}"
            status = request_status(self.store, request_id)
            if status["journal_state"] == "complete" and not status["coverage"]["lock_present"]:
                return request_id
        following = max(numbers, default=0) + 1
        return base if following == 1 else f"{base}-{following}"

    def _expect(self, data):
        state = self.read()
        revision = data.get("expected_revision")
        if type(revision) is not int or revision != state["revision"]:
            raise RequestRefused(409, "This review changed; refresh before continuing")
        return state

    def save_report(self, data):
        from .practice_audio import practice_feedback
        allowed = {"expected_revision", "item_id", "preview_sha256", "interval_frames", "actor", "note",
                   "decision", "listened", "client_request_id"}
        if set(data) - allowed:
            raise RequestRefused(400, "Unexpected report fields")
        if data.get("listened") is not True:
            raise RequestRefused(400, "Confirm that you listened to this interval before saving")
        client = data.get("client_request_id")
        if not isinstance(client, str) or not _CLIENT_REQUEST.fullmatch(client):
            raise RequestRefused(400, "Missing report draft identity")
        item_id = data.get("item_id")
        item = self.item(item_id)
        from .artifact_store import request_status
        request_id = "review-report-" + client
        with self.mutex:
            if request_status(self.store, request_id)["journal_state"] == "complete":
                # A retried save of the same draft (e.g. after a lost response) replays its retained
                # report even though the page's revision is now stale; changed words still conflict.
                state = self.read()
            else:
                state = self._expect(data)
                if len(state["reports"]) >= MAX_REPORTS:
                    raise RequestRefused(409, f"This review session holds {MAX_REPORTS} reports; start a new session")
            preview = self.preview_for(state, item_id)
            if preview is None or data.get("preview_sha256") != preview[1]["audio"]["sha256"]:
                raise RequestRefused(409, "The preview you heard is not this item's current preview; refresh")
            result = practice_feedback(self.store, request_id, self.comparison, item["render"],
                                       data.get("interval_frames"), data.get("actor"), "human", data.get("note"),
                                       data.get("decision"), preview=preview[0])
            handle = result["artifacts"]["feedback"]
            if handle not in state["reports"]:
                if len(state["reports"]) >= MAX_REPORTS:
                    raise RequestRefused(409, f"This review session holds {MAX_REPORTS} reports; the report is "
                                              "retained in the store but cannot be listed here")
                state["reports"].append(handle)
                state["revision"] += 1
                self.write(state)
            report = self.reports_page(None, [handle])["items"][0]
        return {"report": report, "state": self.summary()}

    def reports_page(self, cursor, handles=None):
        from .practice_feedback_query import practice_feedback_query
        state = self.read()
        handles = state["reports"] if handles is None else handles
        if not handles:
            return {"items": [], "total": 0, "next_cursor": None, "complete": True}
        result = practice_feedback_query(self.store, handles, limit=16, cursor=cursor, max_bytes=65536)
        members = {canonical_bytes(item["render"]): item_id for item_id, item in self.items.items()}
        rows = [{**row, "item_id": members.get(canonical_bytes(row["render"]))} for row in result["items"]]
        return {"items": rows, "total": result["total"], "next_cursor": result["next_cursor"],
                "complete": result["complete"], "status": result["status"]}

    def drain(self, timeout=120):
        """Wait (at most `timeout` seconds in total) for in-flight preview and report work."""
        import time
        deadline = time.monotonic() + timeout
        if not self.preview_slot.acquire(timeout=timeout):
            return False
        self.preview_slot.release()
        if not self.mutex.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return False
        self.mutex.release()
        return True

    def audio(self, item_id):
        """Hash-verified preview bytes for one item; provenance is rechecked by summary and report calls."""
        item = self.item(item_id)
        with self.mutex:
            handle = self.read()["previews"].get(item_id)
        if handle is None:
            raise RequestRefused(404, "Prepare this item's preview first")
        record = read_record(handle, self.store, "pocket.practice-preview/v1")
        if record["parent"] != item["render"]:
            raise PocketError("Session preview does not belong to this item", code="evidence_mismatch")
        return read_bytes(record["audio"], self.store), record["audio"]["sha256"]


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Browsers routinely abort media range requests when seeking; that is not a server fault.
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)

    def server_close(self):
        super().server_close()
        self.review.lock.close()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def _guard(self, write=False):
        return guard(self, self.server.authority, self.server.origin, self.server.review.csrf, write,
                     "Refresh this review to obtain its local session token")

    def _refuse(self, error):
        if isinstance(error, RequestRefused):
            return send(self, error.status, {"error": str(error)})
        # Store and session locations are trusted launch arguments; never echo them to the page.
        message = str(error)
        for location, label in ((self.server.review.store, "<store>"), (str(self.server.review.folder), "<session>")):
            message = message.replace(location, label)
        message = _ABSOLUTE_PATH.sub("<path>", message)
        return send(self, 409, {"error": message, "code": getattr(error, "code", "invalid_request")})

    def do_HEAD(self):
        return send(self, 405, {"error": "Use GET"})

    def do_GET(self):
        if not self._guard():
            return
        parsed = urlsplit(self.path)
        if parsed.path in _FILES:
            filename, mime = _FILES[parsed.path]
            return send(self, 200, (_WEB / filename).read_bytes(), mime)
        review = self.server.review
        try:
            with verification_scope():  # one request is one verified snapshot; nothing persists
                return self._get(review, parsed)
        except (RequestRefused, PocketError, ValueError, TypeError, KeyError) as error:
            return self._refuse(error)

    def _get(self, review, parsed):
        if parsed.path == "/api/review":
            with review.mutex:
                return send(self, 200, review.summary())
        if parsed.path == "/api/review/reports":
            cursor = parse_qs(parsed.query).get("cursor", [None])[0]
            with review.mutex:
                return send(self, 200, review.reports_page(cursor))
        if parsed.path.startswith("/api/review/items/"):
            with review.mutex:
                return send(self, 200, review.detail(parsed.path.removeprefix("/api/review/items/")))
        if parsed.path.startswith("/api/review/audio/"):
            payload, sha = review.audio(parsed.path.removeprefix("/api/review/audio/"))
            return self._audio(payload, sha)
        return send(self, 404, {"error": "Unknown review endpoint"})

    def _audio(self, payload, sha):
        headers = [("Accept-Ranges", "bytes"), ("ETag", f'"{sha}"')]
        requested = self.headers.get("Range")
        if requested is None:
            return send(self, 200, payload, "audio/wav", headers)
        match = _RANGE.fullmatch(requested.strip())
        size = len(payload)
        if match is None or match.groups() == ("", ""):
            return send(self, 416, b"", "audio/wav", [*headers, ("Content-Range", f"bytes */{size}")])
        first, last = match.groups()
        if first == "":
            start, end = max(0, size - int(last)), size - 1
        else:
            start = int(first)
            end = min(size - 1, int(last)) if last else size - 1
        if start >= size or start > end:
            return send(self, 416, b"", "audio/wav", [*headers, ("Content-Range", f"bytes */{size}")])
        return send(self, 206, payload[start:end + 1], "audio/wav",
                    [*headers, ("Content-Range", f"bytes {start}-{end}/{size}")])

    def do_POST(self):
        if not self._guard(write=True):
            return
        review = self.server.review
        routes = {"/api/review/previews": review.create_preview, "/api/review/reports": review.save_report}
        if self.path not in routes:
            return send(self, 404, {"error": "Unknown review action"})
        try:
            body = read_json(self, MAX_BODY)
            with verification_scope():
                result = routes[self.path](body)
            return send(self, 200, result)
        except (RequestRefused, PocketError, ValueError, TypeError, KeyError) as error:
            return self._refuse(error)


def _make_server(store_root, comparison, session_dir, report_handles=(), host="127.0.0.1", port=0):
    if host != "127.0.0.1" or type(port) is not int or not 0 <= port <= 65535:
        raise PocketError("Practice review must bind 127.0.0.1 with port 0–65535")
    review = _Review(store_root, comparison, session_dir, report_handles)
    try:
        server = _Server((host, port), _Handler)
    except Exception:
        review.lock.close()
        raise
    server.review = review
    server.authority = f"127.0.0.1:{server.server_port}"
    server.origin = "http://" + server.authority
    return server


def start_practice_review(store_root: str, comparison_file: str, session_dir: str,
                          reports_file: str | None = None, host: str = "127.0.0.1", port: int = 0) -> None:
    """Print one local URL and serve the selected comparison until interrupted."""
    comparison = _load_handle(comparison_file, "comparison")
    reports = _load_report_handles(reports_file) if reports_file else []
    server = _make_server(store_root, comparison, session_dir, reports, host, port)
    print(json.dumps({"url": server.origin, "session_dir": str(server.review.folder),
                      "comparison": comparison["sha256"]}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if not server.review.drain():
            print(json.dumps({"warning": "A preview or report was still running at shutdown; inspect "
                              "request_status before retrying it"}), file=sys.stderr, flush=True)
        server.server_close()
