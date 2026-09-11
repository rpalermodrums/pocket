"""Pocket: evidence-linked musical tools with lazy, offline-capable providers."""
from importlib import import_module

__version__ = "0.3.0"

# Importing Pocket does not load a model, contact a service, or start a workspace.
_PUBLIC = {
    "identify_audio": "assets", "analyze_region": "track_map",
    "inspect_set": "set_map", "source_position": "set_map", "arrangement_position": "set_map",
    "inspect_set_summary": "set_queries", "query_set_region": "set_queries",
    "find_clips": "set_queries", "export_set_map": "set_queries",
    "create_trial": "transition_lab", "prepare_native_trial": "transition_lab",
    "attach_completed_render": "transition_lab", "validate_native_trial": "transition_lab",
    "record_feedback": "transition_lab", "query_feedback": "feedback",
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
__all__ = ["__version__"] + list(_PUBLIC)


def __getattr__(name):
    if name not in _PUBLIC:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{_PUBLIC[name]}", __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
