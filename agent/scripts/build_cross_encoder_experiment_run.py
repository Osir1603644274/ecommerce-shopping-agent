"""Offline-only adapter for externally precomputed Cross-Encoder scores.

This script never imports or serves a model. It turns precomputed experimental
scores into a run that the strict evaluator can compare with online systems.
"""

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--source-system", default="rrf_rule")
    args = parser.parse_args()
    output = []
    for line in args.input.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        candidates = row["runs"][args.source_system]
        if not all("crossEncoderScore" in item for item in candidates):
            raise ValueError(
                f"{row['queryId']} has candidates without externally precomputed crossEncoderScore"
            )
        reranked = sorted(
            candidates,
            key=lambda item: (-float(item["crossEncoderScore"]), int(item["productId"])),
        )
        updated = {**row, "runs": {**row["runs"], "cross_encoder_experiment": reranked}}
        output.append(updated)
    args.output.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in output) + "\n",
        "utf-8",
    )
    print(json.dumps({"rows": len(output), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
