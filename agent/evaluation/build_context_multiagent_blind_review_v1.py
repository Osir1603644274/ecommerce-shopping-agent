"""Build deterministic mirrored blind-review packages from public attempt002."""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

from evaluation.context_multiagent_public_pilot_v1 import candidate_detail, load_jsonl


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "agent/evaluation/runs/context_multiagent_public_pilot_v2_attempt002"
DATASET = (
    ROOT
    / "agent/evaluation/assets/context_multiagent_public_pilot_v1_20260831/scenarios.jsonl"
)
CATALOG = (
    ROOT
    / "data/derived/ecommerce/used_phone_catalog_expansion_kuaisearch_09807c_20260823_r3/catalog.jsonl"
)
OUTPUT = RUN / "blind-review-v1"
SEED = 20260831


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for row in rows
        ),
        encoding="utf-8",
        newline="\n",
    )


def public_evidence(candidate_ids: list[int], catalog: dict[int, dict]) -> list[dict]:
    result = []
    for candidate_id in candidate_ids:
        detail = candidate_detail(catalog[candidate_id])
        result.append(
            {
                "candidateId": candidate_id,
                "brand": detail["brand"],
                "unverifiedListingTitle": detail["title"],
                "verifiedAttributes": [
                    {
                        "key": item["key"],
                        "value": item["value"],
                        "evidenceRef": item["evidenceRef"],
                    }
                    for item in detail["attributes"]
                    if item["status"] == "known"
                ],
            }
        )
    return result


def build() -> None:
    if OUTPUT.exists():
        raise RuntimeError(f"refusing to overwrite {OUTPUT}")
    summary_path = RUN / "summary.json"
    trace_path = RUN / "traces.jsonl"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary["diagnostics"]["armExecutionFailureCount"] != 0:
        raise RuntimeError("cannot blind-review an attempt with execution failures")
    if summary["diagnostics"]["contractSafetyFailureCount"] != 0:
        raise RuntimeError("cannot blind-review an attempt with contract safety failures")
    visible = list(summary["visibleDifferenceScenarioIds"])
    cases = {row["scenarioId"]: row for row in load_jsonl(DATASET)}
    catalog = {int(row["itemId"]): row for row in load_jsonl(CATALOG)}
    traces: dict[str, dict[str, dict]] = {}
    for row in load_jsonl(trace_path):
        traces.setdefault(row["scenarioId"], {})[row["arm"]] = row

    rng = random.Random(SEED)
    reviewer01: list[dict[str, Any]] = []
    reviewer02: list[dict[str, Any]] = []
    sealed: list[dict[str, Any]] = []
    for index, scenario_id in enumerate(visible, start=1):
        item_id = f"blind-{index:02d}"
        case = cases[scenario_id]
        pair = traces[scenario_id]
        first_a = "MA1" if rng.randrange(2) else "CTX1b"
        first_b = "CTX1b" if first_a == "MA1" else "MA1"
        common = {
            "schemaVersion": "context-multiagent-human-blind-item-v1",
            "itemId": item_id,
            "conversation": case["turns"],
            "currentUserRequest": case["currentQuery"],
            "candidateEvidence": public_evidence(case["candidateIds"], catalog),
            "evidenceBoundary": (
                "unverifiedListingTitle is unverified listing text; only "
                "verifiedAttributes may be treated as established facts"
            ),
        }
        reviewer01.append(
            {
                **common,
                "candidateA": pair[first_a]["finalAnswer"]["answer"],
                "candidateB": pair[first_b]["finalAnswer"]["answer"],
            }
        )
        reviewer02.append(
            {
                **common,
                "candidateA": pair[first_b]["finalAnswer"]["answer"],
                "candidateB": pair[first_a]["finalAnswer"]["answer"],
            }
        )
        sealed.append(
            {
                "itemId": item_id,
                "scenarioId": scenario_id,
                "reviewer01": {"A": first_a, "B": first_b},
                "reviewer02": {"A": first_b, "B": first_a},
                "ctx1bAnswerSha256": hashlib.sha256(
                    pair["CTX1b"]["finalAnswer"]["answer"].encode("utf-8")
                ).hexdigest(),
                "ma1AnswerSha256": hashlib.sha256(
                    pair["MA1"]["finalAnswer"]["answer"].encode("utf-8")
                ).hexdigest(),
            }
        )

    OUTPUT.mkdir(parents=True, exist_ok=False)
    reviewer01_path = OUTPUT / "reviewer01.jsonl"
    reviewer02_path = OUTPUT / "reviewer02.jsonl"
    sealed_path = OUTPUT / "SEALED_DO_NOT_SHARE.json"
    write_jsonl(reviewer01_path, reviewer01)
    write_jsonl(reviewer02_path, reviewer02)
    sealed_path.write_text(
        json.dumps(
            {
                "schemaVersion": "context-multiagent-human-blind-mapping-v1",
                "seed": SEED,
                "items": sealed,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    instructions = """# Independent blind review instructions

Review only your assigned JSONL. Do not open `SEALED_DO_NOT_SHARE.json`, the
other reviewer's file, source traces, automatic scores, or experiment results.
Do not communicate with the other reviewer before both reviews are frozen.

For every `itemId`, score Candidate A and B independently from 1 to 5 on:

- constraintFidelity
- evidenceDiscipline
- taskProgression
- usefulness

Then provide `overallPreference` as `A`, `B`, or `tie`, plus one short reason.
Treat listing titles as unverified text; only `verifiedAttributes` are established
facts. Return one JSON object per line using `review-template.json`.
"""
    (OUTPUT / "README_FOR_REVIEWERS.md").write_text(
        instructions, encoding="utf-8", newline="\n"
    )
    template = {
        "itemId": "blind-01",
        "review": {
            "candidateA": {
                "constraintFidelity": 1,
                "evidenceDiscipline": 1,
                "taskProgression": 1,
                "usefulness": 1,
            },
            "candidateB": {
                "constraintFidelity": 1,
                "evidenceDiscipline": 1,
                "taskProgression": 1,
                "usefulness": 1,
            },
            "overallPreference": "tie",
            "reason": "short reason",
        },
    }
    (OUTPUT / "review-template.json").write_text(
        json.dumps(template, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    receipt = {
        "schemaVersion": "context-multiagent-human-blind-package-receipt-v1",
        "status": "HOLD_PENDING_TWO_INDEPENDENT_HUMAN_REVIEWS",
        "itemCount": len(visible),
        "mirrorSeed": SEED,
        "attemptResultHash": summary["resultHash"],
        "summarySha256": sha256_file(summary_path),
        "tracesSha256": sha256_file(trace_path),
        "datasetSha256": sha256_file(DATASET),
        "catalogSha256": sha256_file(CATALOG),
        "generatorSha256": sha256_file(Path(__file__)),
        "reviewer01Sha256": sha256_file(reviewer01_path),
        "reviewer02Sha256": sha256_file(reviewer02_path),
        "sealedMappingSha256": sha256_file(sealed_path),
        "mappingDisclosureAllowedBeforeReviewFreeze": False,
    }
    receipt["receiptHash"] = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    (OUTPUT / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    build()
