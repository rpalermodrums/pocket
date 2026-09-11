"""Pocket: evidence-linked musical tools with lazy, offline-capable providers."""
from importlib import import_module

__version__ = "0.3.0"

# Importing Pocket does not load a model, contact a service, or start a workspace.
_PUBLIC = {
    "identify_audio": "assets", "analyze_region": "peek",
    "inspect_set": "thread", "source_position": "thread", "arrangement_position": "thread",
    "inspect_set_summary": "thread_queries", "query_set_region": "thread_queries",
    "find_clips": "thread_queries", "export_thread": "thread_queries", "export_set_map": "thread_queries",
    "create_trial": "stitch", "prepare_native_trial": "stitch",
    "attach_completed_render": "stitch", "validate_native_trial": "stitch",
    "record_feedback": "stitch", "query_feedback": "feedback",
    "create_record_bag": "record_bag", "load_record_bag": "record_bag",
    "query_record_bag": "record_bag", "revise_record_bag": "record_bag",
    "plan_set_routes": "set_workshop", "load_set_plan": "set_workshop",
    "record_plan_feedback": "set_workshop", "replan_set": "set_workshop",
    "rank_next_tracks": "on_deck", "prepare_session": "on_deck",
    "session_snapshot": "on_deck", "session_options": "on_deck", "update_session": "on_deck",
    "import_spotify_items": "spotify_bridge", "plan_spotify_playlist": "spotify_bridge",
    "execute_spotify_playlist": "spotify_bridge", "verify_spotify_playlist_ui": "spotify_bridge",
    "discover_sources": "acquisition", "inspect_source_formats": "acquisition",
    "plan_acquisition": "acquisition", "acquire_source": "acquisition",
    "model_preflight": "music_embeddings", "LocalClapAdapter": "music_embeddings",
    "build_embedding_index": "music_embeddings", "load_embedding_index": "music_embeddings",
    "rank_embedding_query": "music_embeddings", "start_workspace": "workspace",
}
_MODULES = ("peek", "thread", "stitch")
__all__ = ["__version__"] + list(_PUBLIC) + list(_MODULES)


def __getattr__(name):
    if name in _MODULES:
        value = import_module(f".{name}", __name__)
    elif name in _PUBLIC:
        value = getattr(import_module(f".{_PUBLIC[name]}", __name__), name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
