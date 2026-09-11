import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from pocket_music import on_deck as deck
from pocket_music.errors import PocketError


def track(name, energy=None, bpm=120, tags=None, roles=None, local=True):
    value = {"track_id": name, "title": name, "artists": ["Test"],
             "profile": {"provenance": "user", "bpm": bpm, "energy": energy,
                         "tags": tags or ["house"], "roles": roles or []}}
    if local:
        value["audio"] = {"status": "identified", "identity": {"sha256": hashlib.sha256(name.encode()).hexdigest()}}
    return value


@pytest.fixture
def tracks():
    return [track("current", .5), track("hold", .52), track("lift", .8),
            track("different", .4, 119, ["ambient"], ["texture"]), track("unknown", local=False)]


@pytest.fixture
def session(tmp_path, monkeypatch, tracks):
    monkeypatch.setattr(deck, "_load_bag", lambda handle: {"tracks": copy.deepcopy(tracks)})
    return deck.prepare_session({"schema": "test", "path": "bag", "sha256": "a" * 64}, tmp_path / "live",
                                current_track_id="current")


def test_rank_is_pure_diverse_and_deterministic(tracks):
    before = copy.deepcopy(tracks)
    options = deck.rank_next_tracks(tracks, "current", limit=3)
    assert {o["lane"] for o in options} == {"hold", "lift", "left_turn"}
    assert len({o["track_id"] for o in options}) == 3
    assert options == deck.rank_next_tracks(list(reversed(tracks)), "current", limit=3)
    assert tracks == before
    assert all(o["proposed_transition"]["status"] == "proposal_not_auditioned" for o in options)


def test_filters_exact_directed_pairs_and_unavailable(tracks):
    tracks.append({**track("off", .5), "available": False})
    options = deck.rank_next_tracks(tracks, "current", played_ids=["hold"],
                                   intent={"avoid_pairs": [["current", "lift"], ["different", "current"]],
                                           "require_local_audio": True})
    assert [o["track_id"] for o in options] == ["different"]


def test_opening_unknown_does_not_imply_match_or_analyze_title():
    records = [{"track_id": "a", "title": "120 BPM perfect key techno", "artists": [],
                "catalog_source": "spotify_api"}, track("b", .35)]
    options = deck.rank_next_tracks(records, None, intent={"setting": "warm_up"})
    assert options[0]["track_id"] == "b"
    unknown = next(o for o in options if o["track_id"] == "a")
    assert unknown["evidence"]["feature_count"] == 0
    assert unknown["tempo_options"] == []
    assert any("profile" in s for s in unknown["unknowns"])


def test_tempo_half_double_remains_hypothesis_and_stretch_cap():
    options = deck.rank_next_tracks([track("a", .5, 120), track("b", .5, 60)], "a", limit=1)
    tempo = options[0]["tempo_options"][0]
    assert tempo["pulse_ratio"] == 2
    assert tempo["stretch_percent"] == 0
    assert "hypothesis" in tempo["status"]
    options = deck.rank_next_tracks([track("a", .5, 120), track("b", .5, 145)], "a", limit=1)
    assert options[0]["tempo_options"][0]["status"] == "natural_tempo_reset_proposal"


@pytest.mark.parametrize("kw", [{"limit": True}, {"limit": 129}, {"intent": {"target_energy": 2}},
                                 {"intent": {"max_stretch_percent": float('nan')}},
                                 {"played_ids": [False]}])
def test_invalid_rank_requests(tracks, kw):
    with pytest.raises(PocketError):
        deck.rank_next_tracks(tracks, "current", **kw)


def test_unattributed_fields_abstain_and_nulls_stay_unknown():
    a, b = track("a", .5), track("b", .5)
    b["profile"].pop("provenance")
    result = deck.rank_next_tracks([a, b], "a")[0]
    assert result["evidence"]["feature_count"] == 0
    assert result["evidence"]["profile_provenance"] is None


def update(snapshot, action, **kwargs):
    return deck.update_session(snapshot["session_dir"], expected_revision=snapshot["revision"],
                               expected_sha256=snapshot["sha256"], action=action, **kwargs)


def test_session_immutable_history_cas_and_manual_replay(session):
    from pathlib import Path
    root = Path(session["session_dir"])
    old = (root / "revisions/000000.json").read_bytes()
    skipped = update(session, "skip", track_id="lift", note="Save this for later")
    assert "lift" not in [o["track_id"] for o in deck.session_options(root)["options"]]
    chosen = update(skipped, "choose", track_id="hold")
    intent = update(chosen, "intent", intent={"direction": "left_turn"})
    replay = update(intent, "choose", track_id="current")
    assert replay["revision"] == 4
    assert replay["current_track_id"] == "current"
    assert len(replay["history"]) == 5
    assert (root / "revisions/000000.json").read_bytes() == old
    assert deck.session_snapshot(root)["sha256"] == replay["sha256"]
    with pytest.raises(PocketError, match="changed"):
        update(session, "choose", track_id="hold")


def test_bad_hash_read_write_and_history_tamper(session):
    from pathlib import Path
    with pytest.raises(PocketError, match="hash"):
        deck.session_options(session["session_dir"], expected_sha256="0" * 64)
    current = update(session, "choose", track_id="hold")
    path = Path(session["session_dir"]) / "revisions/000000.json"
    value = json.loads(path.read_text())
    value["current_track_id"] = "different"
    path.write_text(json.dumps(value))
    with pytest.raises(PocketError, match="hash"):
        deck.session_snapshot(current["session_dir"])


def test_no_session_overwrite_and_invalid_actions(session, tracks, monkeypatch):
    with pytest.raises(PocketError, match="create session"):
        deck.prepare_session(session["bag"], session["session_dir"])
    for kwargs in ({"action": "skip", "track_id": "current"},
                   {"action": "choose", "track_id": "missing"},
                   {"action": "intent", "intent": {"target_energy": -1}}):
        with pytest.raises(PocketError):
            deck.update_session(session["session_dir"], expected_revision=0, expected_sha256=session["sha256"], **kwargs)
    assert deck.session_snapshot(session["session_dir"])["revision"] == 0


def test_concurrent_writers_only_one_wins(session):
    def attempt(name):
        try:
            return update(session, "choose", track_id=name)["revision"]
        except PocketError:
            return "conflict"
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(attempt, ["hold", "lift"]))
    assert sorted(outcomes, key=str) == [1, "conflict"]
    assert deck.session_snapshot(session["session_dir"])["revision"] == 1


def test_bag_verification_is_not_bypassed_on_reload(session, monkeypatch):
    def changed(_handle):
        raise PocketError("Local audio changed")
    monkeypatch.setattr(deck, "_load_bag", changed)
    with pytest.raises(PocketError, match="audio changed"):
        deck.session_options(session["session_dir"])


def test_workshop_filtered_frontier_accepts_external_history(tracks):
    options = deck.rank_next_tracks(tracks[:2], "current", played_ids=["earlier-elsewhere"],
                                   intent={"avoid_pairs": [["external", "other"]]}, limit=64)
    assert [o["track_id"] for o in options] == ["hold"]


def test_null_availability_is_unknown_and_id_not_silently_rewritten():
    records = [track("a", .5), {**track("b", .5), "available": None}]
    assert deck.rank_next_tracks(records, "a")[0]["track_id"] == "b"
    records[1]["track_id"] = " b "
    with pytest.raises(PocketError, match="whitespace"):
        deck.rank_next_tracks(records, "a")
