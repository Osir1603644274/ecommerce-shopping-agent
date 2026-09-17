from __future__ import annotations

import argparse
import json
import os
import shutil
import warnings
from pathlib import Path
from typing import Any

from .common import (
    MODEL_ID,
    MODEL_LICENSE,
    MODEL_REVISION,
    MODEL_WEIGHTS_SHA256,
    PACKAGE_DIR,
    REPO_DIR,
    sha256_file,
    write_json,
)


SMALL_FILES = (
    "README.md",
    "config.json",
    "generation_config.json",
    "merges.txt",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--local-dir",
        type=Path,
        default=(
            REPO_DIR
            / ".cache"
            / "post_training_models_direct"
            / "Qwen2.5-1.5B-Instruct-775b11af-attempt005"
        ),
    )
    parser.add_argument(
        "--reuse-weight",
        type=Path,
        default=(
            REPO_DIR
            / ".cache"
            / "post_training_models_direct"
            / "Qwen2.5-1.5B-Instruct-775b11af"
            / "model.safetensors"
        ),
    )
    parser.add_argument("--allow-unverified-tls", action="store_true")
    args = parser.parse_args()

    source_receipt = PACKAGE_DIR / "model_source_v2.json"
    if source_receipt.exists():
        raise FileExistsError(f"refusing to overwrite model source receipt: {source_receipt}")
    if args.local_dir.exists():
        raise FileExistsError(f"refusing to reuse fetch target: {args.local_dir}")
    if not args.reuse_weight.is_file():
        raise FileNotFoundError(f"verified weight source is missing: {args.reuse_weight}")
    if sha256_file(args.reuse_weight) != MODEL_WEIGHTS_SHA256:
        raise RuntimeError("reuse weight failed SHA-256 verification")

    import requests
    import urllib3

    tls_mode = "verified"
    if args.allow_unverified_tls:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        tls_mode = "unverified_due_to_local_untrusted_root__content_hash_required"
        warnings.warn(
            "TLS certificate verification is disabled for pinned small-file downloads; "
            "the model uses built-in Qwen2 code only and every artifact is hashed.",
            RuntimeWarning,
        )

    snapshot = args.local_dir.resolve()
    snapshot.mkdir(parents=True)
    weights = snapshot / "model.safetensors"
    reuse_method = "hardlink"
    try:
        os.link(args.reuse_weight, weights)
    except OSError:
        shutil.copy2(args.reuse_weight, weights)
        reuse_method = "copy"

    session = requests.Session()
    session.verify = not args.allow_unverified_tls
    model_info_response = session.get(
        f"https://huggingface.co/api/models/{MODEL_ID}/revision/{MODEL_REVISION}",
        timeout=(30, 180),
    )
    model_info_response.raise_for_status()
    model_info = model_info_response.json()
    if (
        model_info.get("sha") != MODEL_REVISION
        or (model_info.get("cardData") or {}).get("license") != "apache-2.0"
    ):
        raise RuntimeError("model API identity or license differs from the frozen contract")
    response_metadata: dict[str, dict[str, Any]] = {}
    for name in SMALL_FILES:
        url = (
            f"https://huggingface.co/{MODEL_ID}/resolve/"
            f"{MODEL_REVISION}/{name}?download=true"
        )
        response = session.get(url, timeout=(30, 180), allow_redirects=True)
        response.raise_for_status()
        content = response.content
        if not content:
            raise RuntimeError(f"downloaded empty model file: {name}")
        target = snapshot / name
        target.write_bytes(content)
        response_metadata[name] = {
            "sourceHost": response.url.split("/", 3)[2],
            "etag": response.headers.get("etag"),
            "size": len(content),
        }

    for name in (
        "config.json",
        "generation_config.json",
        "tokenizer_config.json",
        "tokenizer.json",
    ):
        json.loads((snapshot / name).read_text(encoding="utf-8"))
    config = json.loads((snapshot / "config.json").read_text(encoding="utf-8"))
    if (
        config.get("model_type") != "qwen2"
        or config.get("architectures") != ["Qwen2ForCausalLM"]
        or config.get("num_hidden_layers") != 28
        or config.get("hidden_size") != 1536
    ):
        raise RuntimeError("downloaded config does not match Qwen2.5-1.5B")

    actual_hash = sha256_file(weights)
    if actual_hash != MODEL_WEIGHTS_SHA256:
        raise RuntimeError(
            f"model weight SHA-256 mismatch: expected={MODEL_WEIGHTS_SHA256} "
            f"actual={actual_hash}"
        )
    files = {
        path.name: {"size": path.stat().st_size, "sha256": sha256_file(path)}
        for path in sorted(snapshot.iterdir())
        if path.is_file()
    }
    if any(entry["size"] <= 0 for entry in files.values()):
        raise RuntimeError("model snapshot contains an empty file")
    result = {
        "schemaVersion": "posttraining-model-source-v2",
        "modelId": MODEL_ID,
        "revision": MODEL_REVISION,
        "license": MODEL_LICENSE,
        "snapshotPath": str(snapshot),
        "tlsMode": tls_mode,
        "weightReuse": {
            "source": str(args.reuse_weight.resolve()),
            "method": reuse_method,
            "sha256VerifiedBeforeReuse": True,
        },
        "responseMetadata": response_metadata,
        "modelApi": {
            "sha": model_info.get("sha"),
            "license": (model_info.get("cardData") or {}).get("license"),
        },
        "files": files,
        "weightsSha256Expected": MODEL_WEIGHTS_SHA256,
        "weightsSha256Actual": actual_hash,
        "status": "PASS",
    }
    write_json(source_receipt, result)
    print(json.dumps({
        "status": "PASS",
        "modelId": MODEL_ID,
        "revision": MODEL_REVISION,
        "snapshotPath": str(snapshot),
        "tlsMode": tls_mode,
        "weightReuse": reuse_method,
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
