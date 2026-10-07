"""Summarize an immutable 10-scenario live shopping comparison batch."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("prefix", choices=("baseline", "after"))
    args = parser.parse_args()
    sequence = ("core", "cross", "compare", "product", "core", "cross",
                "compare", "product", "core", "core")
    files = [args.directory / f"{args.prefix}-{index:02d}-{name}.json"
             for index, name in enumerate(sequence, 1)]
    rows = [row for path in files for row in json.loads(path.read_text(encoding="utf-8"))]
    durations = sorted(row["durationSeconds"] for row in rows)
    parse = [next((node for node in row["nodes"] if node["parseUsage"]), {}) for row in rows]
    output = {"scenarioFiles": [path.name for path in files], "turns": len(rows),
              "completed": sum(row["status"] == "completed" for row in rows),
              "parseAttempts": [node.get("parseAttempts") for node in parse],
              "parseTotalTokens": sum((node.get("parseUsage") or {}).get("total_tokens") or 0 for node in parse),
              "latencySeconds": {"median": statistics.median(durations),
                  "p95NearestRank": durations[math.ceil(len(durations) * .95) - 1],
                  "max": durations[-1]},
              "answerHashes": [hashlib.sha256((row.get("answer") or "").encode("utf-8")).hexdigest()
                               for row in rows]}
    target = args.directory / f"{args.prefix}-summary.json"
    if target.exists():
        raise FileExistsError(target)
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: output[key] for key in ("turns", "completed", "parseTotalTokens", "latencySeconds")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
