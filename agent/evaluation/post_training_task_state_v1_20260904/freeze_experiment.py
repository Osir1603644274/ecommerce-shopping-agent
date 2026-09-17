from __future__ import annotations

import json

from .common import (
    MODEL_ID,
    MODEL_LICENSE,
    MODEL_REVISION,
    PACKAGE_DIR,
    read_json,
    sha256_file,
    write_json,
)
from .evaluate import GENERATION_CONFIG
from .train_qlora import TRAINING_CONFIG


GATES = {
    "minimumSemanticExactRate": 0.85,
    "minimumSemanticExactLift": 0.10,
    "maximumFalseReadyRate": 0.02,
    "maximumMcNemarPValueExclusive": 0.05,
    "contractValidNonRegression": True,
    "falseReadyNonRegression": True,
}


def main() -> None:
    output = PACKAGE_DIR / "frozen_config.json"
    if output.exists():
        raise FileExistsError(f"refusing to overwrite frozen config: {output}")
    manifest_path = PACKAGE_DIR / "datasets" / "manifest.json"
    model_source_path = PACKAGE_DIR / "model_source_v2.json"
    manifest = read_json(manifest_path)
    model_source = read_json(model_source_path)
    script_names = (
        "common.py",
        "build_dataset.py",
        "fetch_model.py",
        "train_qlora.py",
        "preflight_qlora.py",
        "evaluate.py",
        "compare_and_adjudicate.py",
        "verify_package.py",
    )
    scripts = {
        name: sha256_file(PACKAGE_DIR / name)
        for name in script_names
    }
    frozen = {
        "schemaVersion": "posttraining-frozen-config-v1",
        "frozenAt": "2026-09-04T02:00:00+08:00",
        "model": {
            "id": MODEL_ID,
            "revision": MODEL_REVISION,
            "license": MODEL_LICENSE,
            "weightsSha256": model_source["weightsSha256Actual"],
        },
        "dataset": {
            split: {
                "rowCount": manifest["splits"][split]["rowCount"],
                "sha256": manifest["splits"][split]["sha256"],
            }
            for split in ("train", "dev", "test")
        },
        "trainingConfig": TRAINING_CONFIG,
        "generationConfig": GENERATION_CONFIG,
        "gates": GATES,
        "scriptHashes": scripts,
        "manifestSha256": sha256_file(manifest_path),
        "modelSourceSha256": sha256_file(model_source_path),
        "qloraPreflightSha256": sha256_file(PACKAGE_DIR / "preflight_qlora.json"),
        "terminalAuthority": {
            "maximumAcceptance": "ACCEPT_OFFLINE_ADAPTER",
            "productionDefault": "HOLD",
            "mayModifyProduction": False,
        },
    }
    write_json(output, frozen)
    print(json.dumps({
        "status": "PASS",
        "frozenConfig": str(output),
        "sha256": sha256_file(output),
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
