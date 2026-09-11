"""Real loopback requests against shared providers, using fictional catalog fixtures."""
import http.client
import json
import threading
from pathlib import Path

import pytest

from pocket_music.errors import PocketError
from pocket_music.workspace import _make_server


def records():
    return [{"track_id": f"r{i}", "title": f"Record {i}", "artists": ["Fixture artist"],
             "duration_seconds": 300, "profile": {"provenance": "user", "bpm": 118 + i,
             "energy": .3 + i * .07, "tags": ["warm", "percussive"]}} for i in range(7)]


@pytest.fixture
def server(tmp_path):
    value = _make_server(str(tmp_path / "workspace"))
    thread = threading.Thread(target=value.serve_forever, daemon=True)
    thread.start()
    yield value
    value.shutdown()
    value.server_close()
    thread.join(timeout=2)


def call(server, path, data=None, headers=None, raw=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    default = {"Origin": server.origin, "X-Pocket-CSRF": server.workspace.csrf,
               "Content-Type": "application/json"}
    default.update(headers or {})
    body = json.dumps(data).encode() if data is not None else raw
    connection.request("POST" if body is not None else "GET", path, body=body, headers=default)
    response = connection.getresponse()
    payload = response.read()
    status, response_headers = response.status, dict(response.headers)
    connection.close()
    result = json.loads(payload) if "application/json" in response_headers["Content-Type"] else payload
    return status, result, response_headers


def mutate(server, path, state, **data):
    status, result, _ = call(server, path, {"expected_workspace_revision": state["revision"], **data})
    assert status == 200, result
    return result


def imported(server):
    status, state, _ = call(server, "/api/state")
    assert status == 200
    return mutate(server, "/api/import", state, title="Fixture bag", tracks=records())


def test_bag_routes_feedback_and_replan(server):
    state = imported(server)
    assert state["bag"]["summary"]["track_count"] == 7
    status, query, _ = call(server, "/api/tracks?q=Record%203")
    assert status == 200 and [t["track_id"] for t in query["tracks"]] == ["r3"]
    state = mutate(server, "/api/routes", state,
                   brief={"title": "Practice", "setting": "warm_up", "track_count": 4}, seed=5)
    assert len(state["plan"]["routes"]) == 3
    route = state["plan"]["routes"][0]
    previous = state["plan_handle"].copy()
    state = mutate(server, "/api/feedback", state, expected_plan_sha256=previous["sha256"],
                   route_id=route["route_id"], disposition="prefer")
    assert state["plan_handle"] != previous
    assert Path(previous["path"]).exists()
    state = mutate(server, "/api/routes", state, use_feedback=True, seed=6)
    assert len(state["plan"]["feedback"]) == 1
    state = mutate(server, "/api/import", state, title="Another bag", tracks=records())
    assert state["plan"] is None and state["session"] is None
    assert Path(previous["path"]).exists()


def test_session_shared_cas_choose_skip_intent_and_reattach(server):
    from pocket_music.on_deck import update_session
    state = mutate(server, "/api/session", imported(server), track_id="r0", intent={"direction": "hold"})
    stale = state["session"].copy()
    status, options, _ = call(server, "/api/options")
    assert status == 200 and options["options"]
    candidate = options["options"][0]["track_id"]
    external = update_session(stale["session_dir"], expected_revision=stale["revision"],
                              expected_sha256=stale["sha256"], action="skip", track_id=candidate)
    status, error, _ = call(server, "/api/session/action", {
        "expected_workspace_revision": state["revision"], "expected_revision": stale["revision"],
        "expected_sha256": stale["sha256"], "action": "choose", "track_id": candidate})
    assert status == 409 and "error" in error
    _, state, _ = call(server, "/api/state")
    assert state["session"]["sha256"] == external["sha256"]
    session = state["session"]
    state = mutate(server, "/api/session/action", state, expected_revision=session["revision"],
                   expected_sha256=session["sha256"], action="intent", intent={"direction": "lift"})
    session = state["session"]
    state = mutate(server, "/api/session/action", state, expected_revision=session["revision"],
                   expected_sha256=session["sha256"], action="choose", track_id="r6")
    assert state["current_track"]["track_id"] == "r6"
    relative = str(Path(state["session"]["session_dir"]).relative_to(server.workspace.folder))
    state = mutate(server, "/api/session/attach", state, session_dir=relative)
    assert state["session"]["intent"]["direction"] == "lift"
    status, _, _ = call(server, "/api/session/attach", {
        "expected_workspace_revision": state["revision"], "session_dir": "../outside"})
    assert status == 409


def test_stale_workspace_and_plan_rejected(server):
    state = imported(server)
    status, _, _ = call(server, "/api/import", {"expected_workspace_revision": 0, "tracks": records()})
    assert status == 409
    state = mutate(server, "/api/routes", state, brief={"track_count": 3})
    status, _, _ = call(server, "/api/feedback", {"expected_workspace_revision": state["revision"],
        "expected_plan_sha256": "0" * 64, "route_id": state["plan"]["routes"][0]["route_id"],
        "disposition": "prefer"})
    assert status == 409


@pytest.mark.parametrize("headers", [{"Origin": "https://evil.invalid"}, {"Origin": "null"},
    {"X-Pocket-CSRF": "wrong"}, {"Sec-Fetch-Site": "cross-site"}, {"Host": "evil.invalid"}])
def test_cross_site_write_rejected(server, headers):
    status, _, _ = call(server, "/api/import", {"expected_workspace_revision": 0, "tracks": records()}, headers)
    assert status == 403
    assert server.workspace.read()["revision"] == 0


def test_missing_origin_rejected(server):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
    connection.request("POST", "/api/import", body=b"{}", headers={"X-Pocket-CSRF": server.workspace.csrf,
                                                                   "Content-Type": "application/json"})
    response = connection.getresponse()
    assert response.status == 403
    response.read()
    connection.close()


def test_allowlist_bounded_json_and_catalog_only(server):
    assert call(server, "/../../etc/passwd")[0] == 404
    assert call(server, "/api/run", {})[0] == 404
    assert call(server, "/api/import", raw=b"[]")[0] == 409
    assert call(server, "/api/import", raw=b"{")[0] == 409
    assert call(server, "/api/import", {}, {"Content-Type": "text/plain"})[0] == 415
    assert call(server, "/api/import", raw=b"", headers={"Content-Length": "2000001"})[0] == 413
    assert call(server, "/api/import", {"expected_workspace_revision": 0,
                "tracks": [{"title": "no", "local_path": "/private/file"}]})[0] == 409


def test_static_assets_csp_and_untrusted_titles(server):
    _, state, _ = call(server, "/api/state")
    tracks = records()
    tracks[0]["title"] = '<img src=x onerror="alert(1)">'
    mutate(server, "/api/import", state, title="<script>fixture</script>", tracks=tracks)
    status, query, _ = call(server, "/api/tracks?q=img")
    assert status == 200 and query["tracks"][0]["title"] == tracks[0]["title"]
    for endpoint in ("/", "/app.js", "/style.css", "/pip.svg"):
        status, content, headers = call(server, endpoint)
        assert status == 200 and content
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert headers["Cache-Control"] == "no-store"
    script = call(server, "/app.js")[1].decode()
    assert "innerHTML" not in script and ".textContent" in script
    assert "https://" not in script


def test_only_loopback_and_single_server(tmp_path):
    with pytest.raises(PocketError):
        _make_server(str(tmp_path / "bad"), host="0.0.0.0")
    first = _make_server(str(tmp_path / "one"))
    try:
        with pytest.raises(PocketError, match="already served"):
            _make_server(str(tmp_path / "one"))
    finally:
        first.server_close()
    next_server = _make_server(str(tmp_path / "one"))
    next_server.server_close()


def test_initialized_bag(server, tmp_path):
    from pocket_music.record_bag import create_record_bag
    bag = create_record_bag(records(), str(tmp_path / "bag"), "Initialized")
    other = _make_server(str(tmp_path / "initialized"), bag["handle"])
    try:
        assert other.workspace.snapshot()["bag"]["title"] == "Initialized"
    finally:
        other.server_close()
