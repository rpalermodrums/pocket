# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations
import sqlite3
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from pocket_music.errors import PocketError
from pocket_music import shuttle
from pocket_music.shuttle_host import Runtime, _clock, _match, validate_action


def runtime(tmp_path):
    r = Runtime.__new__(Runtime)
    r.lock = threading.RLock()
    r.action_lock = threading.Lock()
    r.owner = "s"
    r.lease = "lease"
    r.revision = 0
    r.directory = tmp_path
    r.cancel = tmp_path / "cancel"
    r.requests = {}
    r.last_physical_ns = 0
    r.ready = True
    r.topology_changed = False
    r.midi = SimpleNamespace(poll=lambda: None)
    r.capture = SimpleNamespace(poll=lambda: None)
    r.audio = {"at_ns": time.monotonic_ns(), "finite": True, "rms_dbfs": [-20, -20]}
    r.events = []
    r.log = r.events.append
    r.gains = {"left": 64, "right": 64}
    return r


def test_request_replay_does_not_toggle_twice(tmp_path):
    r = runtime(tmp_path)
    calls = []
    r._execute = lambda *args: calls.append(args) or {"status": "verified"}
    request = {
        "session_id": "s",
        "request_id": "one",
        "expected_revision": 0,
        "action": {"kind": "play", "deck": "left"},
    }
    assert r.act(request)["status"] == "verified"
    assert r.act(request)["replayed"] is True
    assert len(calls) == 1
    with pytest.raises(PocketError, match="different inputs"):
        r.act({**request, "action": {"kind": "pause", "deck": "left"}})
    assert len(calls) == 1


def test_stale_revision_blocks_before_native_dispatch(tmp_path):
    r = runtime(tmp_path)
    r.revision = 2
    r._execute = lambda *args: pytest.fail("native action must not run")
    with pytest.raises(PocketError, match="Stale"):
        r.act(
            {
                "session_id": "s",
                "request_id": "one",
                "expected_revision": 1,
                "action": {"kind": "play", "deck": "left"},
            }
        )


@pytest.mark.parametrize("change", [{"at_ns": 0}, {"finite": False}])
def test_stale_or_invalid_audio_blocks_control(tmp_path, change):
    r = runtime(tmp_path)
    r.audio.update(change)
    with pytest.raises(PocketError, match="stale"):
        r._guard_monitor()


def test_takeover_interrupts_gain_without_restoring_or_finishing(tmp_path):
    r = runtime(tmp_path)
    r.observe = lambda _: {"decks": {"left": {"track_id": "a", "playing": True}}}
    values = []

    def send(messages):
        values.append(messages[0][-1])
        r._yield("physical controller")

    r._send = send
    with pytest.raises(PocketError, match="yielded"):
        r._execute("s", {"kind": "gain", "deck": "left", "value": 0, "duration_seconds": 0.1}, "lease")
    assert values == [32]
    assert r.gains["left"] == 32
    assert r.owner is None and r.cancel.exists()


def test_explicit_release_can_interrupt_an_action_in_flight(tmp_path):
    r = runtime(tmp_path)
    began = threading.Event()
    finish = threading.Event()
    results = []

    def execute(*args):
        began.set()
        finish.wait(2)
        return {"status": "verified"}

    r._execute = execute
    t = threading.Thread(
        target=lambda: results.append(
            r.act(
                {
                    "session_id": "s",
                    "request_id": "gain",
                    "expected_revision": 0,
                    "action": {"kind": "gain", "deck": "left", "value": 0},
                }
            )
        )
    )
    t.start()
    assert began.wait(1)
    result = r.act(
        {"session_id": "s", "request_id": "release", "expected_revision": 1, "action": {"kind": "release"}}
    )
    assert result["owned"] is False and not finish.is_set()
    finish.set()
    t.join(2)
    assert results[0]["status"] == "unverified"


def test_controller_input_yields_and_forgets_commanded_gain(tmp_path):
    import json

    r = runtime(tmp_path)
    r.condition = threading.Condition(r.lock)
    r.physical_events = 0
    r.midi = SimpleNamespace(stdout=[json.dumps({
        "origin": "flx4", "bytes": [176, 1, 70],
    })])
    r._midi_reader()
    assert r.owner is None and r.physical_events == 1
    assert r.gains == {"left": None, "right": None}
    assert r.cancel.exists()


@pytest.mark.parametrize(
    "action",
    [
        {"kind": "gain", "deck": "left", "value": 127},
        {"kind": "gain", "deck": "left", "value": True},
        {"kind": "gain", "deck": "left", "value": 20, "duration_seconds": float("nan")},
        {"kind": "loop", "deck": "left", "beats": 4},
        {"kind": "play", "deck": "left", "surprise": "field"},
    ],
)
def test_unqualified_actions_are_refused(action):
    with pytest.raises(PocketError):
        validate_action(action)


def test_load_never_replaces_playing_deck(tmp_path):
    r = runtime(tmp_path)
    r.observe = lambda _: {"decks": {"left": {"playing": True}}}
    r._send = lambda _: pytest.fail("no native dispatch")
    with pytest.raises(PocketError, match="playing"):
        r._execute("s", {"kind": "load", "deck": "left", "track_id": "a"}, "lease")


def test_load_forgets_previous_record_trim_before_dispatch(tmp_path, monkeypatch):
    from pathlib import Path
    from pocket_music import shuttle_host

    r = runtime(tmp_path)
    source = tmp_path / "source.wav"
    sf.write(source, np.zeros(480), 48000)
    target = {"track_id": "a", "native_path": str(source), "pcm": shuttle._pcm_identity(source), "index": 0}
    r.sessions = {"s": {"assets": [target]}}
    r.verified_pcm = {}
    r.binding = {"left": "old"}
    r.build = tmp_path
    r.observe = lambda _: {"decks": {"left": {"playing": False, "track_id": "a"}}}
    log = tmp_path / "DJ.INFO"
    log.write_text("")
    monkeypatch.setattr(shuttle_host.subprocess, "run", lambda *args, **kwargs: None)
    original_expand = Path.expanduser
    monkeypatch.setattr(Path, "expanduser", lambda p: log if str(p).endswith("/Logs/DJ.INFO") else original_expand(p))

    def pulse(note):
        assert note == 50 and r.gains["left"] is None and "left" not in r.binding
        log.write_text("[View] The cache `" + str(source) + "` was requested\n")

    r._pulse = pulse
    assert r._execute("s", {"kind": "load", "deck": "left", "track_id": "a"}, "lease")["status"] == "verified"
    with pytest.raises(PocketError, match="initial native gain is unknown"):
        r._execute("s", {"kind": "gain", "deck": "left", "value": 0, "duration_seconds": 1}, "lease")


def test_display_ambiguity_is_not_recording_identity():
    assets = [
        {"artist": "A", "duration_seconds": 30, "track_id": "a"},
        {"artist": "A", "duration_seconds": 30, "track_id": "b"},
    ]
    assert _match({"artist": "A", "duration_seconds": 30}, assets) is None
    assert _clock("01:2.9") is None
    assert _clock("01:12.9") == 72.9


def test_pcm_identity_distinguishes_tags_from_audio(tmp_path):
    p = tmp_path / "a.wav"
    x = np.sin(np.arange(4800) * 0.1) * 0.1
    sf.write(p, x, 48000, subtype="PCM_24")
    before = shuttle._pcm_identity(p)
    container = shuttle._hash(p)
    with p.open("ab") as f:
        f.write(b"JUNK" + b"\0" * 16)
    assert shuttle._pcm_identity(p) == before
    assert shuttle._hash(p) != container
    sf.write(p, x * 0.5, 48000, subtype="PCM_24")
    assert shuttle._pcm_identity(p) != before


def test_prepare_keeps_immutable_media_when_native_tags_change(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    sf.write(source, np.sin(np.arange(4800) * 0.1) * 0.1, 48000, subtype="PCM_24")
    db = tmp_path / "library.sqlite"
    with sqlite3.connect(db) as c:
        c.executescript("""create table container(id integer,name text);
          create table location_container(id integer,container_id integer);
          create table container_asset(location_container_id integer,asset_id integer,list_order integer);
          create table asset(id integer,artist text,name text,bpm real,key text,length_ms integer,portable_id text);
          insert into container values(1,'test');insert into location_container values(1,1);
          insert into container_asset values(1,1,1);""")
        c.execute(
            "insert into asset values(1,?,?,?,?,?,?)",
            ("Artist", "Title", 120, "C", 100, str(source).lstrip("/")),
        )
    monkeypatch.setattr(shuttle, "_rpc", lambda *args: {"status": "registered"})
    result = shuttle.shuttle_prepare("test", str(tmp_path / "session"), str(tmp_path / "runtime"), str(db))
    manifest = shuttle._session(result["session_dir"])
    with source.open("ab") as f:
        f.write(b"JUNK" + b"\0" * 12)
    bag = shuttle.load_record_bag(manifest["bag"])
    assert len(bag["tracks"]) == 1
    assert shuttle._hash(source) != manifest["assets"][0]["snapshot_sha256"]
    assert shuttle._pcm_identity(source) == manifest["assets"][0]["pcm"]
    assert result["running_order"] is None


def test_solo_play_requires_master_signal_unless_quiet_is_explicit(tmp_path, monkeypatch):
    r = runtime(tmp_path)
    r.observe = lambda _: {
        "decks": {"left": {"playing": True, "track_id": "a"}, "right": {"playing": False}},
        "audio": {"rms_dbfs": [-240, -240]},
    }
    r._guard_monitor = lambda: {"rms_dbfs": [-240, -240]}
    r._pulse = lambda _: pytest.fail("already playing; never blindly toggle")
    with pytest.raises(PocketError, match="master remains near-silent"):
        r._execute("s", {"kind": "play", "deck": "left"}, "lease")
    assert r._execute("s", {"kind": "play", "deck": "left", "allow_silence": True}, "lease")["playing"]


def test_unknown_loop_feedback_cannot_claim_exit(tmp_path):
    r = runtime(tmp_path)
    r.observe = lambda _: {
        "decks": {"left": {"track_id": "a", "loop_beats": None, "loop_state_known": False}}
    }
    r._pulse = lambda _: pytest.fail("unknown state must not toggle")
    with pytest.raises(PocketError, match="feedback is unknown"):
        r._execute("s", {"kind": "loop", "deck": "left", "beats": None}, "lease")


def test_replayed_transport_does_not_rewrite_choice_history(monkeypatch):
    monkeypatch.setattr(shuttle, "_session", lambda _: {"runtime_dir": "r", "session_id": "s"})
    receipt = {"status": "verified", "track_id": "a", "replayed": True}
    monkeypatch.setattr(shuttle, "_rpc", lambda *args: receipt)
    monkeypatch.setattr(shuttle, "session_snapshot", lambda _: pytest.fail("replay is historical"))
    assert shuttle.shuttle_act("s", 0, "one", {"kind": "play", "deck": "left"}) == receipt


def test_shuttle_cli_and_stdio_publish_the_same_bounded_contract(tmp_path):
    import asyncio
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    spec = tmp_path / "spec.json"
    arguments = {"session_dir": str(tmp_path / "missing")}
    spec.write_text(json.dumps(arguments))
    cli = subprocess.run(
        [sys.executable, "-m", "pocket_music.cli", "shuttle", "observe", "--spec", str(spec)],
        text=True, capture_output=True, timeout=15,
    )
    assert cli.returncode == 2
    assert json.loads(cli.stderr)["error"] == "PocketError"

    async def exchange():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "pocket_music.mcp_server"],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name: t for t in (await session.list_tools()).tools}
            assert {"shuttle_build_bridge", "shuttle_prepare", "shuttle_observe", "shuttle_options",
                    "shuttle_act", "shuttle_request_status"} <= tools.keys()
            schema = tools["shuttle_act"].inputSchema
            action = schema["properties"]["action"]
            if "$ref" in action:
                action = schema["$defs"][action["$ref"].split("/")[-1]]
            assert set(action["properties"]["kind"]["enum"]) == {
                "claim", "release", "load", "play", "pause", "gain", "loop", "sync"
            }
            assert tools["shuttle_observe"].annotations.readOnlyHint
            assert tools["shuttle_act"].annotations.idempotentHint
            response = await session.call_tool("shuttle_observe", arguments)
            assert response.isError
            assert "sealed Shuttle session" in response.content[0].text

    asyncio.run(exchange())
