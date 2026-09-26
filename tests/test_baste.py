# SPDX-License-Identifier: AGPL-3.0-only
import http.client
import json
import os
import shutil
import struct
import subprocess
import time
from pathlib import Path

import pytest

from pocket_music import baste
from pocket_music.errors import PocketError

NODE = shutil.which("node")


def test_fake_liveapi_suite():
    if not NODE:
        pytest.skip("Node is required for Max reader/transport tests")
    result = subprocess.run([NODE, "--test", str(Path(__file__).with_name("baste_reader.test.cjs")),
                             str(Path(__file__).with_name("baste_bridge.test.cjs"))],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    if not NODE:
        pytest.skip("Node is required for Max transport tests")
    folder = tmp_path / "bridge"
    proc = subprocess.Popen([NODE, str(Path(__file__).with_name("baste_bridge_fixture.cjs")), str(folder)])
    try:
        deadline = time.monotonic() + 5
        while not list(folder.glob("connection-*.json")):
            assert time.monotonic() < deadline and proc.poll() is None
            time.sleep(0.01)
        monkeypatch.setattr(baste, "_live_running", lambda: True)
        yield folder
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_real_transport_fresh_requests(bridge):
    a = baste.observe_live(bridge_dir=str(bridge))
    b = baste.observe_live(bridge_dir=str(bridge))
    assert a["disposition"] == b["disposition"] == "ok"
    assert a["request_id"] != b["request_id"]
    assert a["observation"]["tracks"] == []
    assert "token" not in json.dumps(a)
    assert len(list(bridge.iterdir())) == 1  # connection only, no saved observations
    assert "atomic" in a["consistency"]


def test_transport_rejects_unauthenticated_foreign_origin_and_other_routes(bridge):
    desc = json.loads(next(bridge.iterdir()).read_text())
    def request(route, headers, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", desc["port"], timeout=3)
        try:
            conn.request("POST" if body else "GET", route, body, headers)
            response = conn.getresponse()
            response.read()
            return response.status
        finally:
            conn.close()
    auth = {"Authorization": "Bearer " + desc["token"]}
    assert request("/health", {}) == 403
    assert request("/health", {**auth, "Origin": "https://example.com"}) == 403
    assert request("/set", auth, '{}') == 404
    assert request("/observe", auth, '{"request_id":"bad"}') == 400
    assert request("/observe", auth, '{"request_id":"' + 'a' * 32 + '","timeout_ms":10,"path":"evil"}') == 400
    assert os.stat(next(bridge.iterdir())).st_mode & 0o777 == 0o600


def test_multiple_devices_fail_closed(bridge):
    # A second reachable descriptor is ambiguous even with the same transport.
    shutil.copyfile(next(bridge.iterdir()), bridge / "connection-second.json")
    result = baste.observe_live(bridge_dir=str(bridge))
    assert result["disposition"] == "ambiguous_devices"
    assert result["observation"] is None


def test_failure_dispositions(tmp_path, monkeypatch):
    monkeypatch.setattr(baste, "_live_running", lambda: False)
    assert baste.observe_live(bridge_dir=str(tmp_path))["disposition"] == "ableton_not_running"
    monkeypatch.setattr(baste, "_live_running", lambda: True)
    assert baste.observe_live(bridge_dir=str(tmp_path))["disposition"] == "device_not_loaded"
    (tmp_path / "connection-bad.json").write_text('{"host":"example.com"}')
    assert baste.observe_live(bridge_dir=str(tmp_path))["disposition"] == "bridge_error"


@pytest.mark.parametrize("failure", ["path_invalid", "unsupported_property", "read_limit"])
def test_device_failure_never_becomes_empty_success(bridge, monkeypatch, failure):
    original = baste._request
    def request(descriptor, route, timeout, payload=None):
        if payload:
            return {"schema": baste.SCHEMA, "request_id": payload["request_id"], "disposition": failure,
                    "observation": {"partial": "discard me"}, "error": "test failure"}
        return original(descriptor, route, timeout, payload)
    monkeypatch.setattr(baste, "_request", request)
    result = baste.observe_live(bridge_dir=str(bridge))
    assert result["disposition"] == failure and result["observation"] is None


def test_stale_reply_and_timeout_fail_closed(bridge, monkeypatch):
    original = baste._request
    def wrong(descriptor, route, timeout, payload=None):
        result = original(descriptor, route, timeout, payload)
        if payload:
            result["request_id"] = "old-reply"
        return result
    monkeypatch.setattr(baste, "_request", wrong)
    assert baste.observe_live(bridge_dir=str(bridge))["disposition"] == "bridge_error"
    def timeout(descriptor, route, seconds, payload=None):
        if payload:
            raise TimeoutError()
        return original(descriptor, route, seconds, payload)
    monkeypatch.setattr(baste, "_request", timeout)
    assert baste.observe_live(bridge_dir=str(bridge))["disposition"] == "observation_timeout"


@pytest.mark.parametrize("value", [0, -1, 61, True, float("nan"), "20"])
def test_invalid_budget(value):
    with pytest.raises(PocketError, match="timeout_seconds"):
        baste.observe_live(timeout_seconds=value)


def test_editable_device_bundle_and_no_overwrite(tmp_path):
    result = baste.build_baste_device(str(tmp_path / "device"))
    binary = Path(result["device_path"]).read_bytes()
    assert binary[:4] == b"ampf" and binary[24:28] == b"ptch"
    assert struct.unpack("<I", binary[28:32])[0] == len(binary) - 32
    patch = json.loads(binary[32:-1])
    boxes = {b["box"]["id"]: b["box"] for b in patch["patcher"]["boxes"]}
    assert boxes["deferrequest"]["text"] == "deferlow"
    assert boxes["loaded"]["text"] == "live.thisdevice"
    assert (tmp_path / "device/baste_bridge.js").is_file()
    with pytest.raises(PocketError, match="already exists"):
        baste.build_baste_device(str(tmp_path / "device"))
    assert Path(result["device_path"]).read_bytes() == binary
