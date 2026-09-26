# SPDX-License-Identifier: AGPL-3.0-only
"""Optional isolated runtime entry point. No remote requests or auto-downloads."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .errors import PocketError
from .music_embeddings import LocalClapAdapter, build_embedding_index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--device", choices=("cpu", "mps"), default="cpu")
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text())
    audio, texts = request.get("audio", []), request.get("text", [])
    if not isinstance(audio, list) or not isinstance(texts, list) or not 1 <= len(audio) + len(texts) <= 100:
        raise PocketError("A worker request needs 1–100 bounded audio/text entries")
    destination = Path(args.output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=False)
    adapter = LocalClapAdapter(args.model_dir, device=args.device)
    receipts = []
    for item in audio:
        receipts.append(adapter.embed_audio_region(**item))
    text_receipts = [adapter.embed_text(**item) for item in texts]
    handle = build_embedding_index(receipts, destination / "index.json") if receipts else None
    result = {"schema": "pocket.embedding-worker-result/v1", "audio": receipts,
              "text": text_receipts, "index": handle, "runtime": adapter.runtime}
    with (destination / "receipts.json").open("x") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"output": str(destination / "receipts.json"), "index": handle,
                      "audio_receipts": len(receipts), "text_receipts": len(text_receipts)}))


if __name__ == "__main__":
    main()
