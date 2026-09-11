# Optional local music embeddings

This adapter is an experiment in semantic record/passages retrieval. It does not establish a downbeat, key, phrase boundary, isolated instrument, or a good mix. On Deck and Set Workshop remain usable without it.

## Provider and preparation

The current provider is [LAION CLAP HTSAT unfused](https://huggingface.co/laion/clap-htsat-unfused), pinned at revision `8fa0f1c6d0433df6e97c127f64b2a1d6c0dcda8a`. The eight required files total **617,895,823 bytes**. Code checks all eight SHA-256 hashes, including the 614,525,833-byte PyTorch weight. The model repository is tagged Apache-2.0; upstream LAION code has its separate license. Model weights are never committed to Pocket.

Use `model_preflight(model_dir)` to inspect a cache. It neither downloads nor loads torch. Missing or modified bytes are explicit. Runtime package presence is not a successful import/inference claim.

Only an explicitly approved setup should download the pinned files. Run the adapter in a separate Python environment with torch and Transformers. The tested baseline used Python 3.12, torch 2.12.0 and Transformers 4.57.6, CPU float32. It did not require torchaudio or a second model. Core Pocket retains its smaller dependency set. Getter outputs must be a single 512-D tensor; changed runtime return types are rejected rather than misread as embeddings. No install or network request is triggered by ordinary ranking or session operations.

`LocalClapAdapter(model_dir, device="cpu", threads=4)` verifies the cache then loads it with `local_files_only=True` and `trust_remote_code=False`. MPS is opt-in; hardware availability alone is not a model compatibility or performance guarantee. Automatic backend fallback is disabled and runtime details are recorded. Initialization and offline feature extraction happen outside the performance decision loop.

## Exact audio and text evidence

```python
from pocket_music.music_embeddings import LocalClapAdapter
adapter = LocalClapAdapter("approved-local-model-directory")
receipt = adapter.embed_audio_region(
    "independently-acquired.wav", start_frame=480000, frames=480000,
    expected_audio_sha256="...", source_origin="independently_acquired",
)
query = adapter.embed_text("warm-up", text_origin="user_authored")
```

Audio requests must use exact integer source frames, within file bounds and no longer than ten seconds. The adapter identifies the entire source, checks read stability, selects exactly the requested region, averages channels, resamples deterministically to 48 kHz, and zero-pads a short request to ten seconds. All transformations and original/model-input frame hashes are recorded. It applies no waveform gain normalization or fades. Exact full-size model input prevents the processor's default random-truncation path from selecting a hidden crop. Near-silent, mono-cancelled or nonfinite regions abstain with an error.

Allowed audio origin assertions are `independently_acquired` and `user_recording`; text requires `user_authored`. These are caller assertions, not automatic rights verification. Spotify content, streams, previews and imported metadata are ineligible for model inference. Keep any permitted catalog/playlist bridge separate. [Spotify Developer Policy](https://developer.spotify.com/policy).

A `pocket.music-embedding/v1` receipt records source identity/region or query hash, source-origin assertion, model revision/weight and auxiliary hashes, adapter/runtime versions, device/dtype, inference duration, 512-dimensional L2-normalized vector and vector hash. It explicitly labels the result semantic evidence. Text is bounded to 1–1000 characters and 77 tokens; truncation is not hidden.

## Cached retrieval

`build_embedding_index(audio_receipts, new_output_path)` verifies provider identity and seals a new index. Multiple designated regions of one exact audio SHA use an explicitly labeled mean of their normalized vectors followed by L2 normalization. This is a selected-passage aggregate, not a verified whole-track characterization. Keep the individual receipts.

`rank_embedding_query(text_receipt, index_handle, limit=10)` searches cached vectors locally. It returns audio hashes, cosine scores, source regions and a qualification, never a musical probability. `apply_embedding_index` joins a bag only on exact identified audio SHA and model revision. A changed recording never inherits its prior embedding by title. On Deck may use current-record/candidate cached similarity as one small, separately visible score component; annotation evidence and unknowns remain separate.

For an isolated process, `python -m pocket_music.embedding_worker --model-dir ... --request request.json --output-dir new-directory` accepts 1–100 audio/text requests and writes receipts/index. Request audio entries use the method arguments above (`path`, `start_frame`, `frames`, `source_origin`, optional expected hash). Text entries use `query` and `text_origin`. Existing outputs are not overwritten. The worker does not download or upload anything.

## Evidence and limits from the initial local pilot

The first tested `larger_clap_music` checkpoint failed to discriminate even contrasting descriptive queries: text vectors clustered near cosine 0.999 despite matching the official processor path, valid masks, complete checkpoint loading and an eager-attention check. It is **not** an eligible production provider; its earlier receipts/indices fail current model-identity validation. No silent reuse across checkpoints is permitted.

The selected unfused control produced distinct descriptive text vectors (pairwise cosine approximately 0.113–0.501) and different rankings for jazz, rap, breakbeat and atmospheric descriptions across four bounded diagnostic passages. This is a useful discrimination sanity check, not a listener-rated musical recommendation pass. Agent-authored diagnostic descriptions were explicitly labeled as such in private evidence; the public text API retains its user-authored gate. The bounded production-path smoke uses eight independently sourced recordings and exact source windows. Timings, hashes and ranking outputs remain outside Git. Do not present successful tensor execution as a complete musical-quality benchmark.

Useful next evaluation is listener-rated, descriptive intent retrieval against a metadata/annotation baseline, using held-out passages. No training, remote audio service, automatic cue edit, or global correctness claim is included in this provider.
