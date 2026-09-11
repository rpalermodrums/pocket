"""Whisker: offline next-record proposals and immutable, compare-and-swap session history.

Heuristics use attributed annotations; they do not certify a musical transition.
No model inference, source decoding, network call, or deck control occurs here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path

from .errors import PocketError

ON_DECK_VERSION = "1.0.2"
_SESSION = "pocket.on-deck-session/v1"
_INTENT_FIELDS = {"setting", "direction", "target_energy", "tags", "creativity", "max_stretch_percent",
                  "require_local_audio", "avoid_track_ids", "avoid_pairs"}


def _text(value, name, maximum=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise PocketError(f"{name} must be nonempty text of at most {maximum} characters")
    return value.strip()


def _int(value, name, lower=0, upper=10000):
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise PocketError(f"{name} must be an integer in [{lower}, {upper}]")
    return value


def _number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise PocketError(f"{name} must be finite")
    if not low <= value <= high:
        raise PocketError(f"{name} must be in [{low}, {high}]")
    return float(value)


def _strings(value, name):
    if not isinstance(value, (list, tuple, set)) or len(value) > 10000:
        raise PocketError(f"{name} must be a bounded sequence of strings")
    return [_text(v, name) for v in value]


def _intent(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - _INTENT_FIELDS:
        raise PocketError("intent has unsupported fields")
    result = copy.deepcopy(value)
    for key, choices in (("setting", {"warm_up", "peak_time", "after_hours", "open"}),
                         ("direction", {"hold", "lift", "left_turn", "explore"})):
        if key in result and result[key] not in choices:
            raise PocketError(f"Unsupported {key}")
    for key in ("target_energy", "creativity"):
        if result.get(key) is not None:
            result[key] = _number(result[key], key, 0, 1)
    if result.get("max_stretch_percent") is not None:
        result["max_stretch_percent"] = _number(result["max_stretch_percent"], "max_stretch_percent", 0, 50)
    for key in ("tags", "avoid_track_ids"):
        if key in result:
            result[key] = _strings(result[key], key)
    if "require_local_audio" in result and not isinstance(result["require_local_audio"], bool):
        raise PocketError("require_local_audio must be boolean")
    pairs = result.get("avoid_pairs", [])
    if not isinstance(pairs, list) or len(pairs) > 10000:
        raise PocketError("avoid_pairs must be a bounded list")
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise PocketError("avoid_pairs must contain directed two-track pairs")
        _strings(pair, "avoid_pairs")
    return result


def _profile(track):
    value = track.get("profile") or {}
    if not isinstance(value, dict):
        raise PocketError("profile must be a dictionary")
    if value and value.get("provenance") not in {"user", "agent_hypothesis", "measured"}:
        # Unattributed properties must never silently become evidence.
        return {}
    if not value:
        return {}
    result = dict(value)
    for name in ("energy", "vocal_density"):
        if result.get(name) is not None:
            result[name] = _number(result[name], name, 0, 1)
    if result.get("bpm") is not None:
        result["bpm"] = _number(result["bpm"], "bpm", 20, 400)
    for name in ("tags", "roles"):
        result[name] = {s.casefold() for s in _strings(result.get(name) or [], name)}
    candidates = result.get("bpm_candidates") or []
    if not isinstance(candidates, list) or len(candidates) > 8:
        raise PocketError("bpm_candidates must have at most eight entries")
    result["bpm_candidates"] = [_number(v, "bpm candidate", 20, 400) for v in candidates]
    return result


def _audio_sha(track):
    audio = track.get("audio") or {}
    return (audio.get("identity") or {}).get("sha256") if audio.get("status") == "identified" else None


def _tempos(source, current, max_stretch):
    values = list(dict.fromkeys(([source["bpm"]] if source.get("bpm") else [])
                               + source.get("bpm_candidates", [])))
    targets = list(dict.fromkeys(([current["bpm"]] if current.get("bpm") else [])
                                + current.get("bpm_candidates", [])))
    if not values or not targets:
        return [], None
    options = []
    for bpm in values:
        for target in targets:
            for ratio in (1, 0.5, 2):
                change = (target / (bpm * ratio) - 1) * 100
                if abs(change) <= max_stretch:
                    options.append({"source_bpm": bpm, "target_bpm": target,
                                    "pulse_ratio": ratio, "stretch_percent": round(change, 4),
                                    "status": "clock_hypothesis_requires_phrase_review"})
    options.sort(key=lambda o: (abs(o["stretch_percent"]), o["pulse_ratio"] != 1, o["source_bpm"]))
    if not options:
        return [{"source_bpm": values[0], "target_bpm": None, "pulse_ratio": None,
                 "stretch_percent": None, "status": "natural_tempo_reset_proposal"}], 0.15
    best = options[0]
    fit = 1 - abs(best["stretch_percent"]) / max(max_stretch, 0.001)
    # Half/double clocks are alternatives, not evidence of a matching musical pulse.
    return options[:3], max(0.25, fit) * (0.75 if best["pulse_ratio"] != 1 else 1)


def rank_next_tracks(tracks, current_track_id=None, *, played_ids=(), intent=None, limit=6):
    """Return deterministic, diverse proposals. Score is a heuristic, not probability.

    Input tracks are prepared catalogue dictionaries. This pure hot path neither
    validates files on disk nor converts Spotify metadata into acoustic evidence.
    """
    _int(limit, "limit", 1, 128)
    if not isinstance(tracks, list) or len(tracks) > 10000:
        raise PocketError("tracks must contain at most 10,000 prepared records")
    intent = _intent(intent)
    by_id = {}
    for track in tracks:
        if not isinstance(track, dict):
            raise PocketError("Each track must be a dictionary")
        track_id = _text(track.get("track_id"), "track_id")
        if track_id != track["track_id"]:
            raise PocketError("track_id must not contain surrounding whitespace")
        if track_id in by_id:
            raise PocketError("Duplicate track_id")
        if track.get("available") is not None and not isinstance(track["available"], bool):
            raise PocketError("available must be boolean")
        by_id[track_id] = track
    if current_track_id is not None and current_track_id not in by_id:
        raise PocketError("Current track is not in this bag")
    played = set(_strings(played_ids, "played_ids"))
    current_track = by_id.get(current_track_id, {})
    current = _profile(current_track)
    excluded = played | set(intent.get("avoid_track_ids", [])) | {current_track_id}
    excluded |= {p[1] for p in intent.get("avoid_pairs", []) if p[0] == current_track_id}
    direction = intent.get("direction", "explore")
    target = intent.get("target_energy")
    if target is None and current.get("energy") is not None and direction in {"hold", "lift"}:
        target = min(1, current["energy"] + (0.15 if direction == "lift" else 0))
    if target is None:
        target = {"warm_up": 0.35, "peak_time": 0.8, "after_hours": 0.45}.get(intent.get("setting"))
    if target is None and current.get("energy") is not None:
        target = current["energy"]
    tags = {s.casefold() for s in intent.get("tags", [])}
    max_stretch = intent.get("max_stretch_percent")
    max_stretch = 6 if max_stretch is None else max_stretch
    options = []
    for track_id, track in by_id.items():
        if track_id in excluded or track.get("available") is False:
            continue
        if intent.get("require_local_audio") and not _audio_sha(track):
            continue
        profile = _profile(track)
        energy = profile.get("energy")
        reasons, unknowns, components = [], [], {}
        if energy is not None and target is not None:
            components["energy"] = (0.3, max(0, 1 - abs(energy - target)))
            reasons.append(f"Annotated energy {energy:.2f}; requested target {target:.2f}.")
        elif energy is None:
            unknowns.append("Energy is unannotated.")
        tag_ref = tags or current.get("tags", set())
        if profile.get("tags") and tag_ref:
            shared = sorted(profile["tags"] & tag_ref)
            overlap = len(shared) / len(profile["tags"] | tag_ref)
            components["tags"] = (0.25, overlap)
            if shared:
                reasons.append("Shared annotated tags: " + ", ".join(shared[:4]) + ".")
        else:
            overlap = None
            unknowns.append("Comparable sound tags are unavailable.")
        tempo_options, tempo_fit = _tempos(profile, current, max_stretch)
        if tempo_fit is not None:
            components["tempo"] = (0.25, tempo_fit)
            reasons.append("Tempo treatments use supplied annotations; pulse interpretation remains open.")
        else:
            unknowns.append("Comparable tempo evidence is unavailable.")
        roles = profile.get("roles", set())
        if roles:
            components["roles"] = (0.1, 0.7)
        if energy is not None and current.get("energy") is not None and energy - current["energy"] > 0.08:
            lane = "lift"
        elif (overlap is not None and overlap < 0.15) or roles & {"texture", "bridge", "left_turn"}:
            lane = "left_turn"
        elif current_track_id is None and energy is not None and target is not None and energy > target + 0.1:
            lane = "lift"
        else:
            lane = "hold"
        if lane == "left_turn" and overlap is not None:
            creativity = intent.get("creativity")
            components["contrast"] = (0.05 + 0.15 * (0.5 if creativity is None else creativity), 1 - overlap)
            reasons.append("Contrasting annotated sound offers a change of direction.")
        similarity = None
        if current_track.get("embedding") and track.get("embedding"):
            from .music_embeddings import matching_cosine
            similarity = matching_cosine(current_track["embedding"], track["embedding"],
                                         _audio_sha(current_track), _audio_sha(track))
            if similarity is not None:
                components["embedding"] = (0.1, (similarity + 1) / 2)
                reasons.append("Cached local-audio semantic similarity; not a mix-compatibility score.")
        if not _audio_sha(track):
            unknowns.append("No identified local recording; catalogue identity only.")
        if not profile:
            unknowns.append("No attributed musical profile; title/artist are not analyzed as sound.")
        unknowns.append("Musical bar position, phrase fit and overlap require review.")
        known_weight = sum(w for w, _ in components.values())
        # Missing fields are not treated as perfect matches or silently imputed.
        raw = sum(w * v for w, v in components.values())
        score = 0.1 + 0.85 * raw / max(1, known_weight)
        if direction == lane:
            score += 0.03
        candidate_vocals = profile.get("vocal_density")
        current_vocals = current.get("vocal_density")
        dense = (candidate_vocals is not None and current_vocals is not None
                 and candidate_vocals > 0.6 and current_vocals > 0.6)
        reset = bool(tempo_options and tempo_options[0]["status"] == "natural_tempo_reset_proposal")
        treatment = ("short foreground exchange; avoid prolonged vocal overlap" if dense else
                     "natural-tempo reset or short texture bridge" if reset else
                     "test an intro/body overlap with one bass owner at a time")
        options.append({"track_id": track_id, "title": track.get("title", track_id),
                        "artists": copy.deepcopy(track.get("artists", [])), "score": round(min(score, 1), 6),
                        "lane": lane, "reasons": reasons[:4], "unknowns": unknowns[:6],
                        "tempo_options": tempo_options,
                        "proposed_transition": {"treatment": treatment, "basis": "annotation_and_cached_evidence",
                                                "status": "proposal_not_auditioned"},
                        "evidence": {"profile_provenance": profile.get("provenance"),
                                     "feature_count": len(components), "semantic_cosine": similarity,
                                     "components": {k: round(v, 6) for k, (_, v) in components.items()},
                                     "score_meaning": "bounded_heuristic_not_probability"}})
    options.sort(key=lambda o: (-o["score"], o["track_id"]))
    if limit == 1:
        return options[:1]
    # Offer genuinely available lanes, without fabricating candidates or duplicates.
    selected = options[:1]
    for lane in ("hold", "lift", "left_turn"):
        if len(selected) >= limit:
            break
        if not any(o["lane"] == lane for o in selected):
            candidate = next((o for o in options if o["lane"] == lane), None)
            if candidate is not None:
                selected.append(candidate)
    used = {o["track_id"] for o in selected}
    selected += [o for o in options if o["track_id"] not in used][:limit - len(selected)]
    return selected


def _load_bag(handle):
    from .record_bag import load_record_bag
    return load_record_bag(handle)


def _bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _read_revision(path):
    try:
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if path.with_suffix(".sha256").read_text().strip() != digest:
            raise PocketError("Session revision hash mismatch")
        value = json.loads(raw)
        if value.get("schema") != _SESSION:
            raise PocketError("Unsupported session schema")
        return value, digest
    except (OSError, ValueError, TypeError) as exc:
        raise PocketError(f"Cannot verify session revision: {exc}") from exc


def _read_session(folder):
    paths = sorted((folder / "revisions").glob("[0-9][0-9][0-9][0-9][0-9][0-9].json"))
    if not paths:
        raise PocketError("No complete Whisker session found")
    # Verify the immutable hash chain, including earlier human/agent decisions.
    previous_hash = None
    for revision, path in enumerate(paths):
        value, digest = _read_revision(path)
        if path.stem != f"{revision:06d}" or value.get("revision") != revision:
            raise PocketError("Session history has a missing or invalid revision")
        if value.get("previous_sha256") != previous_hash:
            raise PocketError("Session history chain mismatch")
        previous_hash = digest
    return value, digest


def _snapshot(folder, state, digest):
    return {**copy.deepcopy(state), "session_dir": str(folder), "sha256": digest}


def _write_revision(folder, state):
    payload = _bytes(state)
    digest = hashlib.sha256(payload).hexdigest()
    base = folder / "revisions" / f"{state['revision']:06d}"
    # Publish JSON last: readers see complete immutable revisions only.
    with base.with_suffix(".sha256").open("x") as stream:
        stream.write(digest + "\n")
    temporary = base.with_suffix(".pending")
    with temporary.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.link(temporary, base.with_suffix(".json"))
    temporary.unlink()
    return digest


def prepare_session(bag_handle, output_dir, *, current_track_id=None, intent=None, embedding_index=None):
    """Prepare local state once. output_dir must be new; no implicit model work."""
    bag = _load_bag(bag_handle)
    clean_intent = _intent(intent)
    tracks = bag["tracks"]
    if current_track_id is not None and current_track_id not in {t["track_id"] for t in tracks}:
        raise PocketError("Current track is not in this bag")
    index = None
    if embedding_index is not None:
        from .music_embeddings import load_embedding_index
        index = load_embedding_index(embedding_index)
    folder = Path(output_dir).expanduser().resolve()
    try:
        folder.mkdir(parents=True, exist_ok=False)
        (folder / "revisions").mkdir()
        if index is not None:
            payload = _bytes(index)
            with (folder / "embeddings.json").open("xb") as stream:
                stream.write(payload)
            index = {"path": "embeddings.json", "sha256": hashlib.sha256(payload).hexdigest()}
        state = {"schema": _SESSION, "revision": 0, "previous_sha256": None,
                 "bag": copy.deepcopy(bag_handle), "current_track_id": current_track_id,
                 "played_ids": [current_track_id] if current_track_id else [], "skipped_ids": [],
                 "intent": clean_intent, "embedding_index": index,
                 "history": [{"action": "prepare", "at": datetime.now(UTC).isoformat(),
                              "track_id": current_track_id}], "status": "prepared_no_deck_control"}
        digest = _write_revision(folder, state)
    except OSError as exc:
        raise PocketError(f"Cannot create session: {exc.strerror or exc}") from exc
    return _snapshot(folder, state, digest)


def session_snapshot(session_dir):
    """Read the current immutable snapshot and validate its bag and history."""
    folder = Path(session_dir).expanduser().resolve()
    state, digest = _read_session(folder)
    _load_bag(state["bag"])
    return _snapshot(folder, state, digest)


def _expect(state, digest, revision, expected_hash):
    if revision is not None:
        _int(revision, "expected_revision")
        if state["revision"] != revision:
            raise PocketError("Session changed; reload its current revision")
    if expected_hash is not None and expected_hash != digest:
        raise PocketError("Session hash changed; reload its current snapshot")


def session_options(session_dir, *, limit=6, expected_revision=None, expected_sha256=None):
    folder = Path(session_dir).expanduser().resolve()
    state, digest = _read_session(folder)
    _expect(state, digest, expected_revision, expected_sha256)
    bag = _load_bag(state["bag"])
    tracks = copy.deepcopy(bag["tracks"])
    embedding_status = "not_prepared_annotation_fallback"
    if state.get("embedding_index"):
        from .music_embeddings import apply_embedding_index
        index_path = folder / state["embedding_index"]["path"]
        try:
            raw = index_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != state["embedding_index"]["sha256"]:
                raise PocketError("Prepared embedding index changed")
            tracks = apply_embedding_index(tracks, json.loads(raw))
            embedding_status = "prepared_local_audio_only_no_inference"
        except (OSError, ValueError) as exc:
            raise PocketError(f"Cannot verify prepared embedding index: {exc}") from exc
    options = rank_next_tracks(tracks, state["current_track_id"],
                               played_ids=list(dict.fromkeys(state["played_ids"] + state["skipped_ids"])),
                               intent=state["intent"], limit=limit)
    return {"schema": "pocket.on-deck-options/v1", "session_dir": str(folder),
            "revision": state["revision"], "sha256": digest, "current_track_id": state["current_track_id"],
            "embedding_status": embedding_status, "options": options,
            "status": "proposals_only_manual_choice_required"}


def update_session(session_dir, *, expected_revision, expected_sha256, action,
                   track_id=None, intent=None, note=None):
    """Append a manual choice/skip/intent event with revision+hash CAS checks."""
    _int(expected_revision, "expected_revision")
    _text(expected_sha256, "expected_sha256", 64)
    if action not in {"choose", "skip", "intent"}:
        raise PocketError("action must be choose, skip or intent")
    if action == "intent" and (intent is None or track_id is not None):
        raise PocketError("intent action requires intent and no track_id")
    if action in {"choose", "skip"} and (track_id is None or intent is not None):
        raise PocketError("choose/skip require track_id and no intent")
    if note is not None:
        note = _text(note, "note", 2000)
    folder = Path(session_dir).expanduser().resolve()
    lock = folder / ".write-lock"
    try:
        lock.mkdir()
    except OSError as exc:
        raise PocketError("Session is busy or unavailable; reload before retrying") from exc
    try:
        state, digest = _read_session(folder)
        _expect(state, digest, expected_revision, expected_sha256)
        bag = _load_bag(state["bag"])
        event = {"action": action, "at": datetime.now(UTC).isoformat()}
        if action in {"choose", "skip"}:
            track_id = _text(track_id, "track_id")
            track = next((t for t in bag["tracks"] if t["track_id"] == track_id), None)
            if track is None or track.get("available") is False:
                raise PocketError("Chosen/skipped track is absent or unavailable")
            event["track_id"] = track_id
            if action == "choose":
                # A human may explicitly replay a record; automatic options exclude it.
                state["current_track_id"] = track_id
                if track_id not in state["played_ids"]:
                    state["played_ids"].append(track_id)
                state["skipped_ids"] = [v for v in state["skipped_ids"] if v != track_id]
            else:
                if track_id == state["current_track_id"]:
                    raise PocketError("Cannot skip the currently playing track")
                if track_id not in state["skipped_ids"]:
                    state["skipped_ids"].append(track_id)
        else:
            state["intent"] = _intent(intent)
            event["intent"] = copy.deepcopy(state["intent"])
        if note is not None:
            event["note"] = note
        state["history"].append(event)
        state["revision"] += 1
        if state["revision"] > 10000:
            raise PocketError("Session history limit reached; prepare a new session")
        state["previous_sha256"] = digest
        new_hash = _write_revision(folder, state)
        return _snapshot(folder, state, new_hash)
    except OSError as exc:
        raise PocketError(f"Cannot append session event: {exc.strerror or exc}") from exc
    finally:
        lock.rmdir()
