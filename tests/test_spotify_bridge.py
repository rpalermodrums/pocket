import json
from pathlib import Path

import pytest

from pocket_music import spotify_bridge as bridge
from pocket_music.errors import PocketError

URI = "spotify:track:" + "a" * 22
OTHER = "spotify:track:" + "b" * 22
PID = "p" * 22


class FakeAPI:
    def __init__(self):
        self.items = []
        self.playlist = None
        self.creates = self.adds = 0
        self.lose_create = self.lose_add = False

    def __call__(self, method, path, token, body=None):
        assert token == "fixture-secret"
        if path == "/me":
            return {"id": "fixture-owner"}
        if method == "POST" and path == "/me/playlists":
            self.creates += 1
            self.playlist = {**body, "id": PID, "owner": {"id": "fixture-owner"}, "snapshot_id": "s1"}
            if self.lose_create:
                self.lose_create = False
                raise bridge._APIError(None)
            return self.playlist
        if path.startswith("/me/playlists?"):
            return {"items": [self.playlist] if self.playlist else []}
        if method == "GET" and "/items?" in path:
            offset = int(path.split("offset=")[-1])
            return {"items": [{"item": {"uri": u}} for u in self.items[offset:offset + 100]]}
        if method == "POST" and path.endswith("/items"):
            self.adds += 1
            assert body["position"] == len(self.items)
            self.items.extend(body["uris"])
            if self.lose_add:
                self.lose_add = False
                raise bridge._APIError(None)
            return {"snapshot_id": "s2"}
        return self.playlist


def setup(tmp_path, monkeypatch, uris=None):
    api = FakeAPI()
    monkeypatch.setattr(bridge, "_request", api)
    monkeypatch.setenv("POCKET_SPOTIFY_ACCESS_TOKEN", "fixture-secret")
    handle = bridge.plan_spotify_playlist(uris or [URI, OTHER, URI], str(tmp_path / "plan"), name="Fixture")
    return api, handle


def execute(handle):
    return bridge.execute_spotify_playlist(handle["plan_dir"], expected_plan_sha256=handle["sha256"])


def test_catalog_unknown_and_unavailable_are_preserved(tmp_path):
    handle = bridge.import_spotify_items([
        {"uri": URI, "title": "A", "artists": ["Artist"], "available": False},
        {"uri": OTHER}, {"uri": "spotify:local:unknown", "is_local": True},
    ], str(tmp_path / "import"))
    assert len(handle["unresolved"]) == 3
    assert handle["tracks"][0]["available"] is False
    assert "explicit" not in handle["tracks"][0]
    assert handle["model_input_allowed"] is False


@pytest.mark.parametrize("value", [["title only"], [], [URI] * 501])
def test_bad_uri_list_rejected_before_output(tmp_path, value):
    with pytest.raises(PocketError):
        bridge.plan_spotify_playlist(value, str(tmp_path / "bad"), name="Fixture")
    assert not (tmp_path / "bad").exists()


def test_new_private_order_duplicates_and_idempotent_readback(tmp_path, monkeypatch):
    api, handle = setup(tmp_path, monkeypatch, [URI] * 101)
    assert execute(handle)["ready"] is True
    assert api.creates == 1 and api.adds == 2
    assert execute(handle)["ready"] is True
    assert api.creates == 1 and api.adds == 2
    assert api.playlist["public"] is False and api.playlist["collaborative"] is False
    assert "fixture-secret" not in "".join(p.read_text() for p in (tmp_path / "plan").glob("*.json*"))


@pytest.mark.parametrize("lost", ["lose_create", "lose_add"])
def test_lost_write_reconciles_without_duplicate(tmp_path, monkeypatch, lost):
    api, handle = setup(tmp_path, monkeypatch)
    setattr(api, lost, True)
    assert execute(handle)["ready"] is False
    assert execute(handle)["ready"] is True
    assert api.creates == 1 and api.adds == 1
    assert api.items == [URI, OTHER, URI]


def test_unresolved_create_does_not_post_again(tmp_path, monkeypatch):
    api, handle = setup(tmp_path, monkeypatch)
    api.lose_create = True
    execute(handle)
    api.playlist = None
    assert execute(handle)["reason"] == "uncertain_create_not_uniquely_found"
    assert api.creates == 1


def test_external_order_and_removed_items_are_not_repaired(tmp_path, monkeypatch):
    api, handle = setup(tmp_path, monkeypatch)
    execute(handle)
    api.items.pop()
    assert execute(handle)["ready"] is False
    assert api.adds == 1
    api.items = [OTHER]
    assert execute(handle)["reason"] == "unexpected_playlist_order"


def test_plan_stale_and_output_overwrite_rejected(tmp_path, monkeypatch):
    _, handle = setup(tmp_path, monkeypatch)
    with pytest.raises(PocketError, match="already exists"):
        bridge.plan_spotify_playlist([URI], handle["plan_dir"], name="Again")
    with pytest.raises(PocketError, match="identity"):
        bridge.execute_spotify_playlist(handle["plan_dir"], expected_plan_sha256="0" * 64)
    (tmp_path / "plan" / "plan.json").write_text("{}")
    with pytest.raises(PocketError, match="changed"):
        execute(handle)


def test_manual_verification_requires_observed_order_and_private(tmp_path):
    handle = bridge.plan_spotify_playlist([URI, OTHER], str(tmp_path / "plan"), name="Fixture")
    args = {"expected_plan_sha256": handle["sha256"],
            "playlist_url": f"https://open.spotify.com/playlist/{PID}",
            "observer": "fixture operator", "observed_uris": [URI, OTHER]}
    no = bridge.verify_spotify_playlist_ui(handle["plan_dir"], **args, observed_private=False)
    assert no["ready"] is False
    yes = bridge.verify_spotify_playlist_ui(handle["plan_dir"], **args, observed_private=True)
    assert yes["ready"] and not yes["independently_verified"]
    assert yes["observed_count"] == 2
    assert json.loads(Path(yes["path"]).read_text())["plan_sha256"] == handle["sha256"]


def test_missing_token_is_manual_no_network(tmp_path, monkeypatch):
    _, handle = setup(tmp_path, monkeypatch)
    monkeypatch.delenv("POCKET_SPOTIFY_ACCESS_TOKEN")
    assert execute(handle)["status"] == "manual_action_required"


def test_malformed_catalog_position_and_flag(tmp_path):
    for extra in ({"position": 2}, {"explicit": "false"}, {"duration_seconds": float("nan")}):
        with pytest.raises(PocketError):
            bridge.import_spotify_items([{"uri": URI, **extra}], str(tmp_path / "unused"))


def test_malformed_readback_never_blindly_retries_add(tmp_path, monkeypatch):
    api, handle = setup(tmp_path, monkeypatch)
    def request(method, path, token, body=None):
        if "/items?" in path:
            return {"items": "bad"}
        return api(method, path, token, body)
    monkeypatch.setattr(bridge, "_request", request)
    assert execute(handle)["ready"] is False
    assert api.adds == 0


def test_quota_error_returns_without_retry(tmp_path, monkeypatch):
    _, handle = setup(tmp_path, monkeypatch)
    calls = []
    def request(*args):
        calls.append(args)
        raise bridge._APIError(429, "60", True)
    monkeypatch.setattr(bridge, "_request", request)
    result = execute(handle)
    assert result["quota_exceeded"] and result["retry_after_seconds"] == "60"
    assert len(calls) == 1
