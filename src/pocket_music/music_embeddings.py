# SPDX-License-Identifier: AGPL-3.0-only
"""Optional offline CLAP evidence. Importing this module does not load torch.

Only independently acquired local audio and user-authored text are eligible.
The caller supplies source provenance; this is an explicit eligibility assertion,
not an automatic copyright or content-origin classifier.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import math
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from .assets import identify_audio, sha256_file
from .errors import PocketError

MODEL_ID = "laion/clap-htsat-unfused"
MODEL_REVISION = "8fa0f1c6d0433df6e97c127f64b2a1d6c0dcda8a"
WEIGHT_SHA256 = "1cd3c601bc4afe0fa87be3de4c13dd2cfadd249fac1e29acf74a9b296c3219bb"
MODEL_FILES = {
    "config.json": 5390,
    "merges.txt": 456356,
    "preprocessor_config.json": 541,
    "pytorch_model.bin": 614525833,
    "special_tokens_map.json": 280,
    "tokenizer.json": 2108746,
    "tokenizer_config.json": 384,
    "vocab.json": 798293
}
MODEL_FILE_SHA256 = {
    "config.json": "9efb9557bc804f2ca6e394486af2e45dfed0b18554909735a99c6220b84e4288",
    "merges.txt": "fe36cab26d4f4421ed725e10a2e9ddb7f799449c603a96e7f29b5a3c82a95862",
    "preprocessor_config.json": "9739f58296aa6f9ac18008fd0150fb2649bc554985fbde86d0a4041c882ac753",
    "pytorch_model.bin": "1cd3c601bc4afe0fa87be3de4c13dd2cfadd249fac1e29acf74a9b296c3219bb",
    "special_tokens_map.json": "06e405a36dfe4b9604f484f6a1e619af1a7f7d09e34a8555eb0b77b66318067f",
    "tokenizer.json": "77ef92283d67f0d97e1454909a964afcbfa2019f0fb9f18f8e88d5c25c3ba729",
    "tokenizer_config.json": "377f91458f7729a4574a84c77bdce67dbc3c58c1a345a29bbf8c4eb1307948a3",
    "vocab.json": "ed19656ea1707df69134c4af35c8ceda2cc9860bf2c3495026153a133670ab5e"
}
ADAPTER_VERSION = "1.1.0"
DIMENSION = 512
_SAMPLE_RATE = 48000
_WINDOW_FRAMES = 480000
_ORIGINS = {"independently_acquired", "user_recording"}


def _canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _sha(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PocketError(f"{name} must be an integer >= {minimum}")
    return value


def _unit(vector):
    try:
        values = np.asarray(vector, dtype=np.float64)
    except (ValueError, TypeError) as exc:
        raise PocketError("Invalid embedding vector") from exc
    if values.shape != (DIMENSION,) or not np.all(np.isfinite(values)):
        raise PocketError("Embedding must contain 512 finite values")
    norm = float(np.linalg.norm(values))
    if norm < 1e-12:
        raise PocketError("Embedding has zero norm")
    return values / norm


def model_preflight(model_dir):
    """Verify approved local checkpoint files. Never download or import torch."""
    folder = Path(model_dir).expanduser().resolve()
    missing, mismatched, hashes = [], [], {}
    for filename, size in MODEL_FILES.items():
        path = folder / filename
        if not path.is_file():
            missing.append(filename)
        elif path.stat().st_size != size:
            mismatched.append(filename)
        else:
            hashes[filename] = sha256_file(path)
    for filename, digest in hashes.items():
        if digest != MODEL_FILE_SHA256[filename]:
            mismatched.append(filename + ":sha256")
    # All pinned auxiliary bytes are checked; no remote model code runs.
    packages = {}
    for package in ("torch", "transformers"):
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            packages[package] = None
    return {"schema": "pocket.music-model-preflight/v1", "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION, "model_dir": str(folder), "license": "Apache-2.0",
            "required_bytes": sum(MODEL_FILES.values()), "missing_files": missing,
            "mismatched_files": mismatched, "file_sha256": hashes, "runtime_packages": packages,
            "files_ready": not missing and not mismatched,
            "provider_status": "discriminative_control_listener_relevance_unverified",
            "runtime_imports_tested": False, "download_performed": False,
            "network_required_for_inference": False}


def _read_audio_region(path, start_frame, frames, expected_audio_sha256, source_origin):
    if source_origin not in _ORIGINS:
        raise PocketError("Only independently_acquired or user_recording local audio is eligible; no Spotify content")
    _integer(start_frame, "start_frame")
    _integer(frames, "frames", 1)
    source = Path(path).expanduser().resolve()
    try:
        before = source.stat()
    except OSError as exc:
        raise PocketError(f"Cannot read source audio: {exc.strerror or exc}") from exc
    asset = identify_audio(source)
    if expected_audio_sha256 is not None and asset["sha256"] != expected_audio_sha256:
        raise PocketError("Audio identity does not match expected SHA-256")
    if start_frame + frames > asset["frames"]:
        raise PocketError("Source region exceeds exact file bounds")
    if frames / asset["sample_rate"] > 10:
        raise PocketError("Select an explicit window of at most 10 seconds; implicit random crops are forbidden")
    with sf.SoundFile(source) as audio:
        audio.seek(start_frame)
        samples = audio.read(frames, dtype="float32", always_2d=True)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ino, after.st_ctime_ns):
        raise PocketError("Audio changed during region extraction")
    if samples.shape != (frames, asset["channels"]) or not np.all(np.isfinite(samples)):
        raise PocketError("Source region is incomplete or nonfinite")
    mono = samples.astype(np.float64).mean(axis=1)
    rms = float(np.sqrt(np.mean(mono * mono)))
    if rms <= 10 ** (-80 / 20):
        raise PocketError("Near-silent or cancelled mono region: abstaining from semantic embedding")
    divisor = math.gcd(asset["sample_rate"], _SAMPLE_RATE)
    model_audio = resample_poly(mono, _SAMPLE_RATE // divisor, asset["sample_rate"] // divisor)
    resampled_frames = len(model_audio)
    if resampled_frames > _WINDOW_FRAMES:
        raise PocketError("Resampling produced a window beyond the declared model bound")
    padded = np.pad(model_audio, (0, _WINDOW_FRAMES - resampled_frames)).astype(np.float32)
    recipe = {"source_start_frame": start_frame, "source_frames": frames,
              "source_end_frame_exclusive": start_frame + frames,
              "source_sample_rate": asset["sample_rate"], "source_channels": asset["channels"],
              "source_region_pcm_sha256": hashlib.sha256(samples.astype('<f4').tobytes()).hexdigest(),
              "downmix": "arithmetic_channel_mean", "resampler": "scipy.signal.resample_poly_default_kaiser",
              "model_sample_rate": _SAMPLE_RATE, "resampled_frames": resampled_frames,
              "zero_padding_frames": _WINDOW_FRAMES - resampled_frames, "model_frames": _WINDOW_FRAMES,
              "model_input_pcm_sha256": hashlib.sha256(padded.astype('<f4').tobytes()).hexdigest(),
              "source_mono_rms_dbfs": 20 * math.log10(rms), "waveform_gain_change_db": 0,
              "random_crop": False, "source_origin": source_origin,
              "source_origin_basis": "caller_assertion_not_automatic_rights_verification"}
    return asset, padded, recipe


class LocalClapAdapter:
    """Prepare once outside performance; CPU baseline, explicitly selected MPS.

    Missing packages/checkpoint fail with an actionable error. Calling code can
    continue using annotation-only ranking rather than silently changing models.
    """
    def __init__(self, model_dir, *, device="cpu", threads=4):
        if device not in {"cpu", "mps"}:
            raise PocketError("device must be cpu or mps")
        _integer(threads, "threads", 1)
        if threads > 16:
            raise PocketError("threads must not exceed 16")
        start = time.perf_counter()
        self.preflight = model_preflight(model_dir)
        if not self.preflight["files_ready"]:
            raise PocketError("Pinned CLAP checkpoint is missing or mismatched; prepare the approved cache first")
        try:
            import torch
            from transformers import ClapFeatureExtractor, ClapModel, RobertaTokenizerFast
        except (ImportError, RuntimeError) as exc:
            raise PocketError("Optional local CLAP runtime unavailable; use its isolated environment or annotation fallback") from exc
        if device == "mps" and not torch.backends.mps.is_available():
            raise PocketError("MPS unavailable; explicitly choose CPU instead")
        torch.set_num_threads(threads)
        self.torch = torch
        self.device = device
        self.model = ClapModel.from_pretrained(str(model_dir), local_files_only=True,
                                               trust_remote_code=False).float().eval().to(device)
        self.extractor = ClapFeatureExtractor.from_pretrained(str(model_dir), local_files_only=True)
        self.tokenizer = RobertaTokenizerFast.from_pretrained(str(model_dir), local_files_only=True)
        self.runtime = {"device": device, "dtype": "float32", "threads": threads,
                        "torch": torch.__version__, "transformers": importlib.metadata.version("transformers"),
                        "numpy": np.__version__, "scipy": importlib.metadata.version("scipy"),
                        "soundfile": sf.__version__, "initialization_seconds": time.perf_counter() - start,
                        "automatic_backend_fallback": False, "trust_remote_code": False,
                        "local_files_only": True}

    def _single_vector(self, result):
        # Transformers versions differ on getter return types; never reinterpret
        # a ModelOutput/hidden-state tensor as a contrastive retrieval vector.
        if not isinstance(result, self.torch.Tensor) or tuple(result.shape) != (1, DIMENSION):
            raise PocketError("Unsupported CLAP output contract; use the tested Transformers 4.57.6 runtime")
        return result[0].detach().cpu().numpy()

    def _receipt(self, vector, elapsed):
        unit = _unit(vector)
        return {"schema": "pocket.music-embedding/v1", "adapter_version": ADAPTER_VERSION,
                "model_id": MODEL_ID, "model_revision": MODEL_REVISION, "model_license": "Apache-2.0",
                "checkpoint_sha256": WEIGHT_SHA256, "model_files_sha256": self.preflight["file_sha256"],
                "dimension": DIMENSION, "normalization": "l2", "vector": unit.tolist(),
                "vector_f64le_sha256": hashlib.sha256(unit.astype('<f8').tobytes()).hexdigest(),
                "runtime": self.runtime, "inference_seconds": elapsed,
                "interpretation": "semantic_retrieval_evidence_not_musical_approval",
                "provider_status": "discriminative_control_listener_relevance_unverified"}

    def embed_audio_region(self, path, *, start_frame, frames, source_origin, expected_audio_sha256=None):
        asset, audio, recipe = _read_audio_region(path, start_frame, frames,
                                                 expected_audio_sha256, source_origin)
        start = time.perf_counter()
        inputs = self.extractor(audio, sampling_rate=_SAMPLE_RATE, return_tensors="pt",
                                truncation="rand_trunc", padding="pad")
        # Exact 480000-frame input makes the processor's random-truncation branch unreachable.
        inputs = {key: value.to(self.device) for key, value in inputs.items()}
        with self.torch.inference_mode():
            vector = self._single_vector(self.model.get_audio_features(**inputs))
        receipt = self._receipt(vector, time.perf_counter() - start)
        return {**receipt, "modality": "audio", "asset": asset, "source_region": recipe}

    def embed_text(self, query, *, text_origin):
        if text_origin != "user_authored":
            raise PocketError("Text inference accepts user-authored queries only; no imported platform content")
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise PocketError("query must contain 1–1000 characters")
        start = time.perf_counter()
        inputs = self.tokenizer([query], padding=True, truncation=False, return_tensors="pt")
        if inputs["input_ids"].shape[1] > 77:
            raise PocketError("Query exceeds CLAP's 77-token bound; shorten it explicitly")
        inputs = {key: value.to(self.device) for key, value in inputs.items()}
        with self.torch.inference_mode():
            vector = self._single_vector(self.model.get_text_features(**inputs))
        return {**self._receipt(vector, time.perf_counter() - start), "modality": "text",
                "query_sha256": hashlib.sha256(query.encode()).hexdigest(), "text_origin": text_origin}


def _validate_receipt(receipt, *, modality=None):
    if not isinstance(receipt, dict) or receipt.get("schema") != "pocket.music-embedding/v1":
        raise PocketError("Unsupported embedding receipt")
    if (receipt.get("model_id"), receipt.get("model_revision"), receipt.get("checkpoint_sha256")) != (
            MODEL_ID, MODEL_REVISION, WEIGHT_SHA256):
        raise PocketError("Embedding model identity/revision does not match approved local adapter")
    if modality is not None and receipt.get("modality") != modality:
        raise PocketError("Wrong embedding modality")
    _unit(receipt.get("vector"))
    if receipt.get("dimension") != DIMENSION:
        raise PocketError("Wrong embedding dimension")
    if receipt.get("modality") == "audio":
        if not _sha((receipt.get("asset") or {}).get("sha256")):
            raise PocketError("Audio embedding lacks exact source identity")
        region = receipt.get("source_region") or {}
        if region.get("source_origin") not in _ORIGINS:
            raise PocketError("Ineligible audio provenance in embedding receipt")
        _integer(region.get("source_start_frame"), "source_start_frame")
        _integer(region.get("source_frames"), "source_frames", 1)
    elif receipt.get("modality") == "text":
        if receipt.get("text_origin") != "user_authored":
            raise PocketError("Ineligible text provenance in embedding receipt")
    else:
        raise PocketError("Unsupported embedding modality")


def build_embedding_index(receipts, output_path):
    """Seal local passage vectors; explicit mean pooling is separate from inference."""
    if not isinstance(receipts, list) or not 1 <= len(receipts) <= 10000:
        raise PocketError("Provide 1–10000 audio receipts")
    groups = {}
    for receipt in receipts:
        _validate_receipt(receipt, modality="audio")
        groups.setdefault(receipt["asset"]["sha256"], []).append(receipt)
    entries = {}
    for digest, rows in groups.items():
        vector = _unit(np.mean([_unit(r["vector"]) for r in rows], axis=0))
        entries[digest] = {"model_id": MODEL_ID, "model_revision": MODEL_REVISION,
                           "audio_sha256": digest, "vector": vector.tolist(),
                           "aggregation": "l2_mean_of_explicit_source_passages",
                           "source_regions": [r["source_region"] for r in rows],
                           "receipt_sha256": [hashlib.sha256(_canonical(r)).hexdigest() for r in rows]}
    index = {"schema": "pocket.embedding-index/v1", "model_id": MODEL_ID,
             "model_revision": MODEL_REVISION, "checkpoint_sha256": WEIGHT_SHA256,
             "entries": entries, "purpose": "local_semantic_retrieval_only"}
    payload = _canonical(index)
    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(payload)
    except OSError as exc:
        raise PocketError(f"Cannot create embedding index: {exc.strerror or exc}") from exc
    return {"schema": "pocket.embedding-index-handle/v1", "path": str(path),
            "sha256": hashlib.sha256(payload).hexdigest()}


def _validate_index(value):
    if (not isinstance(value, dict) or value.get("schema") != "pocket.embedding-index/v1"
            or value.get("model_id") != MODEL_ID or value.get("model_revision") != MODEL_REVISION
            or value.get("checkpoint_sha256") != WEIGHT_SHA256):
        raise PocketError("Embedding index model/schema mismatch")
    entries = value.get("entries")
    if not isinstance(entries, dict) or len(entries) > 10000:
        raise PocketError("Invalid embedding index entries")
    for digest, entry in entries.items():
        if (not isinstance(entry, dict) or not _sha(digest) or entry.get("audio_sha256") != digest
                or entry.get("model_id") != MODEL_ID
                or entry.get("model_revision") != MODEL_REVISION):
            raise PocketError("Embedding entry identity mismatch")
        _unit(entry.get("vector"))
    return value


def load_embedding_index(handle):
    if not isinstance(handle, dict) or handle.get("schema") != "pocket.embedding-index-handle/v1":
        raise PocketError("Provide a sealed embedding-index handle")
    try:
        raw = Path(handle["path"]).expanduser().resolve().read_bytes()
        if hashlib.sha256(raw).hexdigest() != handle["sha256"]:
            raise PocketError("Embedding index hash changed")
        return _validate_index(json.loads(raw))
    except (OSError, KeyError, ValueError) as exc:
        raise PocketError(f"Cannot verify embedding index: {exc}") from exc


def apply_embedding_index(tracks, index):
    _validate_index(index)
    result = copy.deepcopy(tracks)
    for track in result:
        track.pop("embedding", None)
        audio = track.get("audio") or {}
        digest = (audio.get("identity") or {}).get("sha256") if audio.get("status") == "identified" else None
        if digest in index["entries"]:
            track["embedding"] = copy.deepcopy(index["entries"][digest])
    return result


def matching_cosine(left, right, left_audio_sha256, right_audio_sha256):
    """Ignore stale/unmatched vectors rather than treating them as current sound."""
    for entry, expected in ((left, left_audio_sha256), (right, right_audio_sha256)):
        if (not expected or not isinstance(entry, dict) or entry.get("audio_sha256") != expected
                or entry.get("model_id") != MODEL_ID or entry.get("model_revision") != MODEL_REVISION):
            return None
    try:
        return float(np.clip(np.dot(_unit(left["vector"]), _unit(right["vector"])), -1, 1))
    except (PocketError, KeyError):
        return None


def rank_embedding_query(query_receipt, index_handle, *, limit=10):
    """Offline user-query retrieval from already computed receipts; no inference."""
    _validate_receipt(query_receipt, modality="text")
    _integer(limit, "limit", 1)
    if limit > 100:
        raise PocketError("limit must be <=100")
    index = load_embedding_index(index_handle)
    query = _unit(query_receipt["vector"])
    rows = [{"audio_sha256": digest, "cosine": float(np.clip(np.dot(query, _unit(row["vector"])), -1, 1)),
             "source_regions": row["source_regions"], "status": "semantic_retrieval_not_transition_approval"}
            for digest, row in index["entries"].items()]
    return sorted(rows, key=lambda row: (-row["cosine"], row["audio_sha256"]))[:limit]
