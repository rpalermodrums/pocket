# SPDX-License-Identifier: AGPL-3.0-only
"""Real stdlib-only child probes distinguish a venv entry from its byte-identical base."""
import hashlib
import shutil
import sys
import venv
from pathlib import Path

import pytest

import pocket_music.audio_pulse_hypotheses as pulse
from pocket_music.errors import PocketError


def isolated_executable(directory):
    """A real declared interpreter with no inherited site packages or installed pip."""
    venv.EnvBuilder(with_pip=False, system_site_packages=False, symlinks=True).create(directory)
    executable = Path(directory) / "bin" / "python"
    return {"path": str(executable), "sha256": hashlib.sha256(executable.read_bytes()).hexdigest()}


@pytest.fixture
def runtime_entry(tmp_path, monkeypatch):
    environment = tmp_path / "declared-environment"
    binding = isolated_executable(environment)
    executable = Path(binding["path"])
    weights = tmp_path / "not-a-model.ckpt"
    weights.write_bytes(b"Synthetic declaration; the probe never deserializes weights")
    # Keep the actual byte-pinned runner untouched. The temporary protocol probe
    # has no optional dependencies and records only the interpreter that ran it.
    owner = tmp_path / "probe" / "audio_pulse_hypotheses.py"
    owner.parent.mkdir()
    runner = owner.with_name("audio_model_runner.py")
    runner.write_text('''import hashlib, json, sys
from pathlib import Path
profile = {"prefix": sys.prefix, "base_prefix": sys.base_prefix, "launch": sys.executable}
encoded = json.dumps(profile, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
result = {"profile": profile, "profile_sha256": hashlib.sha256(encoded).hexdigest(),
          "qualification": None, "analysis": None}
Path(sys.argv[2]).write_text(json.dumps({"status": "ok", "result": result}))
''')
    monkeypatch.setattr(pulse, "__file__", str(owner))
    declaration = {"adapter": "beat_this_cpu_v1", "expected_profile": None,
                   "executable": binding,
                   "weights": {"path": str(weights), "sha256": hashlib.sha256(weights.read_bytes()).hexdigest()}}
    return environment, executable, weights, runner, declaration


def test_runner_preserves_the_declared_virtual_environment(runtime_entry):
    environment, executable, weights, _, declaration = runtime_entry
    result, binding = pulse._runner(declaration, "none", weights)
    assert result["profile"]["prefix"] == str(environment)
    assert result["profile"]["base_prefix"] != str(environment)
    assert result["profile"]["launch"] == str(executable)
    assert binding["executable"]["supplied_path"] == str(executable)
    assert binding["executable"]["resolved_path"] == str(executable.resolve())
    assert binding["executable"]["sha256"] == declaration["executable"]["sha256"]


def test_explicit_base_interpreter_still_means_base(runtime_entry):
    _, executable, weights, _, declaration = runtime_entry
    declaration["executable"]["path"] = str(executable.resolve())
    result, binding = pulse._runner(declaration, "none", weights)
    assert result["profile"]["prefix"] == sys.base_prefix
    assert binding["executable"]["supplied_path"] == declaration["executable"]["path"]


def test_persistent_entry_symlink_retarget_refuses_the_result(runtime_entry, tmp_path, monkeypatch):
    _, executable, weights, _, declaration = runtime_entry
    other = tmp_path / "another-interpreter"
    shutil.copy2(executable.resolve(), other)
    run = pulse.subprocess.run

    def run_then_retarget(*args, **kwargs):
        process = run(*args, **kwargs)
        executable.unlink()
        executable.symlink_to(other)
        return process

    monkeypatch.setattr(pulse.subprocess, "run", run_then_retarget)
    with pytest.raises(PocketError, match="Model execution binding changed"):
        pulse._runner(declaration, "none", weights)


def test_declared_interpreter_digest_still_blocks_dispatch(runtime_entry, monkeypatch):
    _, _, weights, _, declaration = runtime_entry
    declaration["executable"]["sha256"] = "0" * 64
    monkeypatch.setattr(pulse.subprocess, "run", lambda *a, **k: pytest.fail("Dispatched an unverified interpreter"))
    with pytest.raises(PocketError, match="digest mismatch"):
        pulse._runner(declaration, "none", weights)


def test_temporary_runner_change_still_refuses_the_result(runtime_entry, monkeypatch):
    _, _, weights, runner, declaration = runtime_entry
    run = pulse.subprocess.run

    def run_then_change(*args, **kwargs):
        process = run(*args, **kwargs)
        runner.write_text(runner.read_text() + "\n# changed temporary probe\n")
        return process

    monkeypatch.setattr(pulse.subprocess, "run", run_then_change)
    with pytest.raises(PocketError, match="Model execution binding changed"):
        pulse._runner(declaration, "none", weights)
