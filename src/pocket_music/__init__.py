"""Pocket: evidence-linked musical tools with lazy, offline-capable providers."""
import sys
from importlib import import_module
from types import ModuleType

__version__ = "0.4.0"

# Importing Pocket does not load a model, contact a service, or start a workspace.
_PUBLIC = {
    "observe_live": "baste", "build_baste_device": "baste",
    "promote_trial": "pipette", "validate_promotion": "pipette",
    "identify_audio": "assets", "analyze_region": "peek",
    "inspect_set": "thread", "source_position": "thread", "arrangement_position": "thread",
    "inspect_set_summary": "thread_queries", "query_set_region": "thread_queries",
    "find_clips": "thread_queries", "export_thread": "thread_queries", "export_set_map": "thread_queries",
    "create_trial": "stitch", "prepare_native_trial": "stitch",
    "attach_completed_render": "stitch", "validate_native_trial": "stitch",
    "record_feedback": "stitch", "query_feedback": "feedback",
    "create_record_bag": "record_bag", "load_record_bag": "record_bag",
    "query_record_bag": "record_bag", "revise_record_bag": "record_bag",
    "plan_set_routes": "weave", "load_set_plan": "weave",
    "record_plan_feedback": "weave", "replan_set": "weave",
    "rank_next_tracks": "whisker", "prepare_session": "whisker",
    "session_snapshot": "whisker", "session_options": "whisker", "update_session": "whisker",
    "import_spotify_items": "spotify_bridge", "plan_spotify_playlist": "spotify_bridge",
    "execute_spotify_playlist": "spotify_bridge", "verify_spotify_playlist_ui": "spotify_bridge",
    "discover_sources": "acquisition", "inspect_source_formats": "acquisition",
    "plan_acquisition": "acquisition", "acquire_source": "acquisition",
    "model_preflight": "music_embeddings", "LocalClapAdapter": "music_embeddings",
    "build_embedding_index": "music_embeddings", "load_embedding_index": "music_embeddings",
    "rank_embedding_query": "music_embeddings", "start_workspace": "workspace",
    "start_practice_review": "practice_review",
}
from .capabilities import PUBLIC_CAPABILITIES

_PUBLIC.update({row[0]: row[1] for row in PUBLIC_CAPABILITIES})
_PUBLIC['capabilities_list'] = 'capabilities'
_MODULES = ("peek", "thread", "stitch", "weave", "whisker", "baste", "pipette")
__all__ = ["__version__"] + list(_PUBLIC) + list(_MODULES)


class _CallableProviderModule(ModuleType):
    """Keep same-name provider calls and normal submodule imports compatible."""

    def __call__(self, *args, **kwargs):
        return getattr(self, self.__name__.rsplit('.', 1)[-1])(*args, **kwargs)

    @property
    def __signature__(self):
        from inspect import signature
        return signature(getattr(self, self.__name__.rsplit('.', 1)[-1]))


class _PocketModule(ModuleType):
    def __setattr__(self, name, value):
        # Import machinery publishes submodules on their parent after loading.
        # Several public providers deliberately share that submodule's name.
        # Preserve the module (including monkeypatchable provider attributes)
        # while making package-level calls independent of import order.
        if _PUBLIC.get(name) == name and isinstance(value, ModuleType):
            value.__class__ = _CallableProviderModule
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _PocketModule


def __getattr__(name):
    if name in _MODULES:
        value = import_module(f".{name}", __name__)
    elif name in _PUBLIC:
        provider = import_module(f".{_PUBLIC[name]}", __name__)
        value = provider if _PUBLIC[name] == name else getattr(provider, name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
