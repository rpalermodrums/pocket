# SPDX-License-Identifier: AGPL-3.0-only
"""Bridge discovery reports local prerequisites without attempting a build."""

import pytest

from pocket_music import capabilities
from pocket_music.errors import PocketError


def bridge(**kwargs):
    result = capabilities.capabilities_list(domain="performance", operation="build_bridge", **kwargs)
    assert len(result["capabilities"]) == 1
    return result["capabilities"][0]


@pytest.mark.parametrize("platform,binaries,status", [
    ("linux", {"swiftc", "ffmpeg"}, "unsupported"),
    ("win32", {"swiftc", "ffmpeg"}, "unsupported"),
    ("darwin", set(), "requires_optional_setup"),
    ("darwin", {"swiftc"}, "requires_optional_setup"),
    ("darwin", {"ffmpeg"}, "requires_optional_setup"),
    ("darwin", {"swiftc", "ffmpeg"}, "available"),
])
def test_bridge_discovery_requires_actual_local_platform_and_build_tools(monkeypatch, platform, binaries, status):
    monkeypatch.setattr(capabilities.sys, "platform", platform)
    monkeypatch.setattr(capabilities.shutil, "which", lambda name: f"/tools/{name}" if name in binaries else None)
    row = bridge(host_profile={"native_verified": True, "platform": "darwin", "swiftc": True, "ffmpeg": True})
    assert row["status"] == status
    assert row["native_verified"] is False
    assert row["implementation_status"] == "implemented"
    assert row["side_effects"] == ["new_local_artifacts"]
    assert any("swiftc" in text and "ffmpeg" in text for text in row["prerequisites"])


def test_bridge_discovery_never_runs_tools_or_opens_a_host(monkeypatch):
    import subprocess

    def forbidden(*args, **kwargs):
        pytest.fail("Capability discovery must not execute a build or a host")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    result = capabilities.capabilities_list(domain="performance", operation="build_bridge")
    assert result["coverage"]["discovery_executes_tools"] is False


def test_build_prerequisite_change_invalidates_discovery_cursor(monkeypatch):
    monkeypatch.setattr(capabilities.sys, "platform", "darwin")
    monkeypatch.setattr(capabilities.shutil, "which", lambda name: f"/tools/{name}")
    first = capabilities.capabilities_list(limit=1)
    monkeypatch.setattr(capabilities.shutil, "which", lambda name: None)
    with pytest.raises(PocketError, match="cursor"):
        capabilities.capabilities_list(limit=1, cursor=first["next_cursor"])


def test_bridge_discovery_actual_cli_matches_python(tmp_path):
    from test_midi_interfaces import cli_call

    arguments = {"domain": "performance", "operation": "build_bridge",
                 "host_profile": {"native_verified": True, "platform": "darwin"}}
    assert cli_call(tmp_path, "capabilities_list", arguments) == capabilities.capabilities_list(**arguments)
