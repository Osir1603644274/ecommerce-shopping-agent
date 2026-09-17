"""Bounded candidate pool after the full 259-query grouping timed out.

Selection is hash-based and never consults Agent outcomes or relevance labels.
No future formal source may be drawn outside this pool without a new split audit.
"""
import argparse
import asyncio
import json
from pathlib import Path

from . import seed_families
from .artifacts import HERE, append, file_sha, sha, write_new
from .dataset import SEEDS


async def run(output):
    output.mkdir(parents=True, exist_ok=False)
    rows = [json.loads(line) for line in SEEDS.read_text(encoding="utf-8").splitlines() if line.strip()]
    known = {json.loads(path.read_text(encoding="utf-8"))["seedProvenance"]["originalQueryId"]
             for path in (HERE / "dev_dataset002").glob("dev-*.json")}
    selected = [row for row in rows if row["query_id"] in known]
    selected += sorted((row for row in rows if row["query_id"] not in known),
        key=lambda row: sha(["context-history-candidate-pool-v1", row["query_id"]]))[:32 - len(selected)]
    pool = output / "queries.jsonl"
    for row in selected:
        append(pool, row)
    write_new(output / "selection.json", {"source": str(SEEDS), "sourceSha256": file_sha(SEEDS),
        "selectedQueries": len(selected), "poolSha256": file_sha(pool), "qrelsRead": False,
        "selection": "Two already-used development seeds plus deterministic hash-ranked 30 other public queries.",
        "priorAttempt": str(HERE / "seed_families001"), "priorOutcome": "300_SECOND_TIMEOUT_PRESERVED",
        "formalScriptsGenerated": False, "restriction": "Use only this pool for a future audited source-family split."})
    seed_families.SEEDS = pool  # Process-local input binding, not a source edit.
    await seed_families.run(output / "grouping")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    asyncio.run(run(parser.parse_args().output))
