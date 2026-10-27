# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic catalog transfer; no streaming, recommendations or model ingestion."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import uuid
from pathlib import Path
from typing import Literal, NotRequired
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from typing_extensions import TypedDict

from .errors import PocketError
from .stitch import _load_sealed, _now, _seal, _text


class SpotifyCatalogItem(TypedDict):
    """A human/API catalog row. Missing version information stays unknown."""

    uri: str
    title: NotRequired[str]
    artists: NotRequired[list[str]]
    album: NotRequired[str]
    duration_seconds: NotRequired[float]
    explicit: NotRequired[bool]
    is_local: NotRequired[bool]
    available: NotRequired[bool]
    version_note: NotRequired[str]
    isrc: NotRequired[str]
    position: NotRequired[int]


_URI = re.compile(r"spotify:track:[A-Za-z0-9]{22}\Z")
_ID = re.compile(r"[A-Za-z0-9]{22}\Z")
_PLAN = "pocket.spotify-export-plan/v1"


def _uris(values: list[str]) -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= 500:
        raise PocketError("Provide 1–500 ordered Spotify track URIs")
    if any(not isinstance(v, str) or not _URI.fullmatch(v) for v in values):
        raise PocketError("Only exact spotify:track:<22 character ID> URIs are supported")
    return list(values)


def _new_dir(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    try:
        path.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise PocketError("Output already exists; choose a new directory") from exc
    return path


def _checked_plan(folder: Path, expected: str, schema: str = _PLAN) -> dict:
    plan, digest = _load_sealed(folder, "plan.json", schema)
    if expected != digest:
        raise PocketError("Plan identity differs from expected_plan_sha256")
    return plan


def import_spotify_items(
    items: list[SpotifyCatalogItem], output_dir: str, *, title: str = "Spotify selection",
    catalog_source: Literal["spotify_api", "spotify_ui"] = "spotify_ui",
) -> dict:
    """Seal catalog order without resolving editions or inventing musical features."""
    title = _text(title, "title", 200)
    if catalog_source not in ("spotify_api", "spotify_ui"):
        raise PocketError("catalog_source must be spotify_api or spotify_ui")
    if not isinstance(items, list) or not 1 <= len(items) <= 500:
        raise PocketError("Provide 1–500 catalog items")
    rows, tracks, unresolved = [], [], []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise PocketError("Each catalog item must be an object")
        uri = item.get("uri")
        if not isinstance(uri, str) or len(uri) > 2000:
            raise PocketError("Each catalog item needs a bounded URI")
        if "position" in item and (type(item["position"]) is not int or item["position"] != index):
            raise PocketError("Supplied positions must equal the zero-based input order")
        row = {"position": index, "uri": uri}
        for field in ("title", "album", "version_note", "isrc"):
            if field in item:
                row[field] = _text(item[field], field, 1000)
        for field in ("explicit", "available", "is_local"):
            if field in item:
                if type(item[field]) is not bool:
                    raise PocketError(f"{field} must be a boolean when known")
                row[field] = item[field]
        if "artists" in item:
            if not isinstance(item["artists"], list) or len(item["artists"]) > 30:
                raise PocketError("artists must be a bounded string list")
            row["artists"] = [_text(a, "artist", 200) for a in item["artists"]]
        if "duration_seconds" in item:
            duration = item["duration_seconds"]
            if isinstance(duration, bool) or not isinstance(duration, (int, float)):
                raise PocketError("duration_seconds must be a positive number")
            if not math.isfinite(duration) or duration <= 0:
                raise PocketError("duration_seconds must be a positive number")
            row["duration_seconds"] = duration
        reasons = []
        if not _URI.fullmatch(uri) or row.get("is_local"):
            reasons.append("local_or_unsupported_uri")
        if row.get("available") is False:
            reasons.append("unavailable")
        if not row.get("title") or not row.get("artists"):
            reasons.append("missing_catalog_identity")
        row["status"] = "unresolved" if reasons else "catalog_only"
        row["reasons"] = reasons
        rows.append(row)
        if reasons:
            unresolved.append({"position": index, "reasons": reasons})
        if not row.get("title") or not row.get("artists") or not _URI.fullmatch(uri):
            continue
        track = {"track_id": f"spotify-{uri.rsplit(':', 1)[1]}-row-{index + 1}",
                 "spotify_uri": uri, "catalog_source": catalog_source,
                 "title": row["title"], "artists": row["artists"]}
        for field in ("album", "duration_seconds", "explicit", "version_note", "available"):
            if field in row:
                track[field] = row[field]
        tracks.append(track)
    folder = _new_dir(output_dir)
    manifest = {"schema": "pocket.spotify-import/v1", "created_at": _now(), "title": title,
                "catalog_source": catalog_source, "rows": rows, "tracks": tracks,
                "unresolved": unresolved, "recording_identity": "unverified",
                "model_input_allowed": False}
    digest = _seal(folder, "import.json", manifest)
    return {"schema": "pocket.spotify-import-handle/v1", "path": str(folder / "import.json"),
            "sha256": digest, "tracks": tracks, "unresolved": unresolved,
            "model_input_allowed": False}


def plan_spotify_playlist(
    uris: list[str], output_dir: str, *, name: str, description: str = "",
) -> dict:
    """Create an immutable plan for a NEW private, non-collaborative playlist."""
    ordered = _uris(uris)
    name = _text(name, "name", 100)
    if not isinstance(description, str) or len(description) > 200:
        raise PocketError("description must be at most 200 characters")
    folder = _new_dir(output_dir)
    marker = f"pocket-operation:{uuid.uuid4()}"
    plan = {"schema": _PLAN, "created_at": _now(), "name": name, "uris": ordered,
            "description": f"{description}\n{marker}".strip(), "operation_marker": marker,
            "public": False, "collaborative": False, "existing_playlist_mutation": False,
            "scopes": ["playlist-modify-private", "playlist-read-private"]}
    digest = _seal(folder, "plan.json", plan)
    return {"schema": "pocket.spotify-export-handle/v1", "plan_dir": str(folder),
            "path": str(folder / "plan.json"), "sha256": digest, "count": len(ordered),
            "status": "planned", "manual_checklist": ordered}


class _APIError(Exception):
    def __init__(self, status: int | None, retry_after: str | None = None, quota_exceeded: bool = False):
        self.status, self.retry_after = status, retry_after
        self.quota_exceeded = quota_exceeded


def _request(method: str, path: str, token: str, body: dict | None = None) -> dict:
    """No raw server error, request headers or token is exposed in receipts."""
    if not path.startswith("/") or path.startswith("//"):
        raise PocketError("Invalid internal Spotify API path")
    request = Request("https://api.spotify.com/v1" + path, method=method,
                      data=None if body is None else json.dumps(body).encode(),
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=25) as response:
            payload = response.read(2_000_001)
        if len(payload) > 2_000_000:
            raise _APIError(None)
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise _APIError(None)
        return value
    except HTTPError as exc:
        retry = exc.headers.get("Retry-After", "")
        quota = False
        try:
            error = json.loads(exc.read(8192)).get("error", {})
            quota = isinstance(error, dict) and error.get("reason") == "QUOTA_EXCEEDED"
        except (ValueError, AttributeError):
            pass
        raise _APIError(exc.code, retry if retry.isdigit() and len(retry) < 10 else None, quota) from None
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise _APIError(None) from exc


def _event(folder: Path, value: dict) -> None:
    data = (json.dumps({"at": _now(), **value}, allow_nan=False) + "\n").encode()
    with (folder / "journal.jsonl").open("ab") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _events(folder: Path) -> list[dict]:
    path = folder / "journal.jsonl"
    try:
        events = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        for event in events:
            if not isinstance(event, dict) or event.get("event") not in (
                "create_started", "created", "add_started", "add_verified"
            ):
                raise PocketError("Journal has an unsupported event; manual reconciliation required")
            if event["event"] == "created" and not _ID.fullmatch(str(event.get("playlist_id", ""))):
                raise PocketError("Journal has an invalid playlist identity")
            for field in ("before_count", "after_count"):
                if field in event and (type(event[field]) is not int or not 0 <= event[field] <= 500):
                    raise PocketError("Journal has an invalid item count")
            if event["event"] == "add_started" and not {"before_count", "after_count"} <= event.keys():
                raise PocketError("Journal has an incomplete write event")
        return events
    except (ValueError, OSError) as exc:
        raise PocketError("Journal is malformed; manual reconciliation required") from exc


def _owner(value: dict) -> str | None:
    owner = value.get("owner")
    return owner.get("id") if isinstance(owner, dict) else None


def _playlist_items(playlist_id: str, token: str) -> list[str]:
    result = []
    for offset in range(0, 601, 100):
        page = _request("GET", f"/playlists/{playlist_id}/items?limit=100&offset={offset}", token)
        rows = page.get("items")
        if not isinstance(rows, list) or len(rows) > 100:
            raise PocketError("Malformed playlist readback")
        for row in rows:
            item = row.get("item", row.get("track")) if isinstance(row, dict) else None
            if not isinstance(item, dict) or not _URI.fullmatch(str(item.get("uri", ""))):
                raise PocketError("Playlist contains unreadable/local/unavailable items")
            result.append(item["uri"])
        if len(rows) < 100:
            return result
    raise PocketError("Playlist readback exceeds bounded trial size")


def _reconcile_create(folder: Path, plan: dict, token: str, owner: str) -> str | None:
    matches = []
    for offset in range(0, 1000, 50):
        page = _request("GET", f"/me/playlists?limit=50&offset={offset}", token)
        rows = page.get("items")
        if not isinstance(rows, list) or len(rows) > 50:
            raise PocketError("Malformed current-playlists readback")
        for row in rows:
            if (isinstance(row, dict) and plan["operation_marker"] in (row.get("description") or "")
                    and _owner(row) == owner
                    and _ID.fullmatch(str(row.get("id", "")))):
                matches.append(row["id"])
        if len(rows) < 50:
            break
    if len(matches) == 1:
        _event(folder, {"event": "created", "playlist_id": matches[0], "reconciled": True})
        return matches[0]
    return None


def _execute(folder: Path, plan: dict, token: str) -> dict:
    owner = _request("GET", "/me", token).get("id")
    if not isinstance(owner, str) or not owner:
        raise PocketError("Current account identity is unavailable")
    history = _events(folder)
    created = [e for e in history if e.get("event") == "created"]
    playlist_id = created[-1]["playlist_id"] if created else None
    if not playlist_id and any(e.get("event") == "create_started" for e in history):
        playlist_id = _reconcile_create(folder, plan, token, owner)
        if not playlist_id:
            return {"status": "needs_reconciliation", "reason": "uncertain_create_not_uniquely_found"}
    if not playlist_id:
        _event(folder, {"event": "create_started", "owner": owner})
        response = _request("POST", "/me/playlists", token,
                            {k: plan[k] for k in ("name", "description", "public", "collaborative")})
        playlist_id = response.get("id")
        if not isinstance(playlist_id, str) or not _ID.fullmatch(playlist_id):
            return {"status": "needs_reconciliation", "reason": "create_response_missing_id"}
        _event(folder, {"event": "created", "playlist_id": playlist_id})
    metadata = _request("GET", f"/playlists/{playlist_id}", token)
    if (_owner(metadata) != owner or metadata.get("public") is not False
            or metadata.get("collaborative") is not False
            or plan["operation_marker"] not in (metadata.get("description") or "")):
        return {"status": "needs_reconciliation", "reason": "owner_privacy_or_marker_changed"}
    current = _playlist_items(playlist_id, token)
    desired = plan["uris"]
    if current != desired[:len(current)]:
        return {"status": "needs_reconciliation", "reason": "unexpected_playlist_order",
                "playlist_id": playlist_id}
    # A lost write may have committed. Exact prefix readback determines whether it did.
    history = _events(folder)
    verified_count = max((e.get("after_count", 0) for e in history
                          if e.get("event") == "add_verified"), default=0)
    if len(current) < verified_count:
        return {"status": "needs_reconciliation", "reason": "previously_verified_items_removed"}
    pending = [e for e in history if e.get("event") == "add_started"]
    if pending:
        last = pending[-1]
        if len(current) not in (last["before_count"], last["after_count"], len(desired)):
            return {"status": "needs_reconciliation", "reason": "partial_or_external_write"}
        if len(current) == last["before_count"] and not any(
            e.get("event") == "add_verified" and e.get("after_count") == last["after_count"]
            for e in history
        ):
            return {"status": "needs_reconciliation", "reason": "uncertain_add_absent_on_readback"}
        if len(current) == last["after_count"]:
            _event(folder, {"event": "add_verified", "after_count": len(current), "reconciled": True})
    while len(current) < len(desired):
        before = len(current)
        batch = desired[before:before + 100]
        _event(folder, {"event": "add_started", "before_count": before,
                        "after_count": before + len(batch)})
        _request("POST", f"/playlists/{playlist_id}/items", token,
                 {"uris": batch, "position": before})
        current = _playlist_items(playlist_id, token)
        if current != desired[:before + len(batch)]:
            return {"status": "needs_reconciliation", "reason": "write_readback_mismatch"}
        _event(folder, {"event": "add_verified", "after_count": len(current)})
    final = _request("GET", f"/playlists/{playlist_id}", token)
    if final.get("public") is not False or _owner(final) != owner:
        return {"status": "needs_reconciliation", "reason": "final_privacy_or_owner_changed"}
    return {"status": "verified", "verification_method": "spotify_api_readback",
            "playlist_id": playlist_id, "playlist_url": f"https://open.spotify.com/playlist/{playlist_id}",
            "count": len(current), "order_sha256": hashlib.sha256(json.dumps(current).encode()).hexdigest(),
            "snapshot_id": final.get("snapshot_id"), "ready": True}


def execute_spotify_playlist(
    plan_dir: str, *, expected_plan_sha256: str, token_env: str = "POCKET_SPOTIFY_ACCESS_TOKEN",
) -> dict:
    """Execute/reconcile a fresh plan. Token comes only from the named environment variable."""
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,100}", token_env):
        raise PocketError("token_env must be an environment variable name, never a credential")
    folder = Path(plan_dir).expanduser().resolve()
    plan = _checked_plan(folder, expected_plan_sha256)
    _uris(plan["uris"])
    if plan.get("public") is not False or plan.get("collaborative") is not False:
        raise PocketError("Only fresh private non-collaborative playlist plans are supported")
    token = os.environ.get(token_env)
    if not token:
        return {"status": "manual_action_required", "reason": "access_token_not_configured",
                "ready": False, "plan_sha256": expected_plan_sha256}
    with (folder / ".operation.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PocketError("Another executor owns this playlist operation") from exc
        try:
            result = _execute(folder, plan, token)
        except _APIError as exc:
            result = {"status": "needs_reconciliation", "reason": "api_request_failed",
                      "http_status": exc.status, "retry_after_seconds": exc.retry_after,
                      "quota_exceeded": exc.quota_exceeded, "ready": False}
        except PocketError as exc:
            result = {"status": "needs_reconciliation", "reason": str(exc), "ready": False}
        result = {"schema": "pocket.spotify-export-receipt/v1", "at": _now(),
                  "plan_sha256": expected_plan_sha256, "ready": False, **result}
        filename = f"receipt-{uuid.uuid4()}.json"
        digest = _seal(folder, filename, result)
        return {**result, "path": str(folder / filename), "sha256": digest}


def verify_spotify_playlist_ui(
    plan_dir: str, *, expected_plan_sha256: str, playlist_url: str,
    observed_uris: list[str], observed_private: bool, observer: str,
) -> dict:
    """Record exact human-observed order; this is not independent API verification."""
    folder = Path(plan_dir).expanduser().resolve()
    plan = _checked_plan(folder, expected_plan_sha256)
    parsed = urlparse(playlist_url)
    if (parsed.scheme != "https" or parsed.netloc != "open.spotify.com"
            or not re.fullmatch(r"/playlist/[A-Za-z0-9]{22}", parsed.path)):
        raise PocketError("Provide an exact Spotify playlist HTTPS URL")
    observed = _uris(observed_uris)
    if type(observed_private) is not bool:
        raise PocketError("observed_private must report the actually observed privacy flag")
    result = {"schema": "pocket.spotify-ui-verification/v1", "at": _now(),
              "plan_sha256": expected_plan_sha256, "playlist_url": playlist_url,
              "observer": _text(observer, "observer", 200), "observed_uris": observed,
              "order_matches": observed == plan["uris"], "verification_method": "operator_reported_ui",
              "independently_verified": False, "observed_private": observed_private,
              "observed_count": len(observed), "expected_count": len(plan["uris"]),
              "ready": observed == plan["uris"] and observed_private}
    name = f"ui-verification-{uuid.uuid4()}.json"
    digest = _seal(folder, name, result)
    return {**result, "path": str(folder / name), "sha256": digest}
