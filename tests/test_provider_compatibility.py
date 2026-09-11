"""Provider naming changes preserve legacy imports and shared module state."""
import importlib
import inspect
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


PROVIDERS = [
    ("track_map", "peek", "identify_audio", "analyze_region"),
    ("set_map", "thread", "_stamp", "inspect_set"),
    ("set_queries", "thread_queries", "_load", "query_set_region"),
    ("transition_lab", "stitch", "_now", "create_trial"),
]


def fresh_python(code, *args):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return subprocess.run([sys.executable, "-c", code, *args], env=env,
                          capture_output=True, text=True, check=True)


@pytest.mark.parametrize("legacy,canonical,helper,function", PROVIDERS)
@pytest.mark.parametrize("legacy_first", [False, True])
def test_import_order_preserves_module_identity(legacy, canonical, helper, function, legacy_first):
    first, second = (legacy, canonical) if legacy_first else (canonical, legacy)
    fresh_python("""
import importlib, sys
import pocket_music
first = importlib.import_module('pocket_music.' + sys.argv[1])
second = importlib.import_module('pocket_music.' + sys.argv[2])
assert first is second
assert getattr(pocket_music, sys.argv[1]) is first
assert getattr(pocket_music, sys.argv[2]) is first
assert sys.modules['pocket_music.' + sys.argv[1]] is first
assert sys.modules['pocket_music.' + sys.argv[2]] is first
""", first, second)


@pytest.mark.parametrize("legacy,canonical,helper,function", PROVIDERS)
def test_legacy_monkeypatch_changes_canonical_function_globals(monkeypatch, legacy, canonical, helper, function):
    old = importlib.import_module(f"pocket_music.{legacy}")
    new = importlib.import_module(f"pocket_music.{canonical}")
    sentinel = object()
    monkeypatch.setattr(old, helper, sentinel)
    assert getattr(new, helper) is sentinel
    assert getattr(new, function).__globals__[helper] is sentinel
    assert getattr(old, function) is getattr(new, function)


def test_root_exports_stay_lazy_and_names_are_modules():
    fresh_python("""
import importlib, sys
from types import ModuleType
import pocket_music
assert not any('pocket_music.' + name in sys.modules for name in ('peek', 'thread', 'stitch'))
assert 'numpy' not in sys.modules
for name, function in [('peek', 'analyze_region'), ('thread', 'inspect_set'), ('stitch', 'create_trial')]:
    module = getattr(pocket_music, name)
    assert isinstance(module, ModuleType)
    assert module is importlib.import_module('pocket_music.' + name)
    assert getattr(pocket_music, function) is getattr(module, function)
    assert getattr(pocket_music, name) is module
assert pocket_music.export_thread is pocket_music.export_set_map
""")


def test_export_alias_and_serialized_contracts_are_preserved():
    import pocket_music
    from pocket_music import peek, stitch, thread, thread_queries
    from pocket_music.set_queries import SetHandle, export_set_map
    from pocket_music.transition_lab import TrialVariant

    assert all(isinstance(module, ModuleType) for module in (peek, thread, stitch))
    assert pocket_music.export_thread is pocket_music.export_set_map is export_set_map
    assert thread_queries.export_thread is thread_queries.export_set_map is export_set_map
    assert SetHandle is thread_queries.SetHandle
    assert TrialVariant is stitch.TrialVariant
    assert list(inspect.signature(thread.source_position).parameters) == [
        "set_map", "clip_id", "arrangement_beat",
    ]
    assert list(inspect.signature(thread.arrangement_position).parameters) == [
        "set_map", "clip_id", "source_seconds",
    ]
    assert peek.SCHEMA == "pocket.track-map/v1"
    assert thread.SCHEMA == "pocket.set-map/v1"
