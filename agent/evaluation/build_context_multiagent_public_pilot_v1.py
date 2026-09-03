"""Build the public 33-cluster CTX1b/MA1 pilot without answer leakage.

The builder reads only public scenario text and CandidateScope IDs from a prior
public development run. It never reads prior answers, private expectations,
blind mappings, validation, or sealed data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
UPHB = (
    ROOT
    / "agent/evaluation/assets/used_phone_harness_behavior_v1_20260825/public/scenarios.jsonl"
)
UPRG = (
    ROOT
    / "agent/evaluation/assets/used_phone_react_generalization_v1_20260826/public/scenarios.jsonl"
)
HISTORICAL_RECEIPTS = (
    ROOT
    / "agent/evaluation/runs/shopping_task_state_context_ab_v6_20260829_attempt001/live/control/receipts.jsonl"
)
CATALOG = (
    ROOT
    / "data/derived/ecommerce/used_phone_catalog_expansion_kuaisearch_09807c_20260823_r3/catalog.jsonl"
)
DEFAULT_OUTPUT = (
    ROOT
    / "agent/evaluation/assets/context_multiagent_public_pilot_v1_20260831/scenarios.jsonl"
)

RESEARCH_GAPS: dict[str, list[str]] = {
    "UPHB-V1-003": ["battery_health", "gaming_performance"],
    "UPHB-V1-005": ["camera_quality", "screen_originality"],
    "UPHB-V1-006": [
        "battery_health",
        "battery_originality",
        "motherboard_repair",
    ],
    "UPHB-V1-007": [
        "battery_health",
        "motherboard_repair",
        "screen_originality",
    ],
    "UPHB-V1-011": [
        "battery_health",
        "motherboard_repair",
        "screen_originality",
    ],
    "UPHB-V1-014": ["battery_health", "battery_originality"],
    "UPHB-V1-016": ["motherboard_repair", "screen_originality"],
    "UPHB-V1-024": ["gaming_performance", "thermal_performance"],
    "UPRG-V1-002": ["gaming_performance", "thermal_performance"],
}
RESEARCH_REQUIRED = {"UPHB-V1-024", "UPRG-V1-002"}
MUST_CLARIFY = {
    "UPHB-V1-019",
    "UPHB-V1-022",
    "UPRG-V1-003",
    "UPRG-V1-008",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def historical_candidate_ids() -> dict[str, list[int]]:
    final: dict[str, tuple[int, list[int]]] = {}
    with HISTORICAL_RECEIPTS.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            state = row.get("taskState") or {}
            domain = state.get("domainState") or {}
            v2 = domain.get("shoppingTaskStateV2") or {}
            scope = v2.get("candidateScope") or {}
            turn_number = int(str(row["turnId"]).removeprefix("T"))
            final[row["scenarioId"]] = (
                turn_number,
                [int(value) for value in scope.get("candidateIds", [])],
            )
    return {scenario_id: value[1] for scenario_id, value in final.items()}


def deterministic_candidates(
    scenario_id: str, catalog_ids: list[int], count: int = 3
) -> list[int]:
    start = int(hashlib.sha256(scenario_id.encode("utf-8")).hexdigest()[:12], 16)
    start %= len(catalog_ids)
    return [catalog_ids[(start + offset) % len(catalog_ids)] for offset in range(count)]


def route_for(scenario_id: str) -> str:
    if scenario_id in MUST_CLARIFY:
        return "MUST_CLARIFY"
    if scenario_id in RESEARCH_REQUIRED:
        return "RESEARCH_REQUIRED"
    if scenario_id in RESEARCH_GAPS:
        return "RESEARCH_ELIGIBLE"
    return "MUST_DIRECT"


def execute(output: Path) -> None:
    if output.exists():
        raise RuntimeError(f"refusing to overwrite frozen pilot source: {output}")
    catalog_rows = load_jsonl(CATALOG)
    catalog_ids = [int(row["itemId"]) for row in catalog_rows]
    catalog_id_set = set(catalog_ids)
    prior_candidates = historical_candidate_ids()
    source_rows = [("UPHB", row) for row in load_jsonl(UPHB)] + [
        ("UPRG", row) for row in load_jsonl(UPRG)
    ]
    result: list[dict[str, Any]] = []
    for source_family, row in source_rows:
        scenario_id = row["scenarioId"]
        if source_family == "UPHB":
            candidate_ids = prior_candidates.get(scenario_id, [])
            scope_source = "PUBLIC_DEVELOPMENT_RECEIPT_CANDIDATE_IDS_ONLY"
        else:
            candidate_ids = deterministic_candidates(scenario_id, catalog_ids)
            scope_source = "DETERMINISTIC_PUBLIC_CATALOG_SLICE"
        if any(candidate_id not in catalog_id_set for candidate_id in candidate_ids):
            raise RuntimeError(f"candidate outside frozen 439 catalog: {scenario_id}")
        route = route_for(scenario_id)
        if route.startswith("RESEARCH_") and not candidate_ids:
            raise RuntimeError(f"research case has no candidates: {scenario_id}")
        result.append(
            {
                "schemaVersion": "context-multiagent-public-pilot-case-v1",
                "scenarioId": scenario_id,
                "sourceFamily": source_family,
                "provenanceKind": row.get("provenanceKind"),
                "sourceTags": row.get("tags", []),
                "turns": row["turns"],
                "currentQuery": row["turns"][-1]["text"],
                "routeGold": route,
                "routeGoldSource": "FROZEN_MANUAL_RULE_TABLE_NOT_INDEPENDENT_HUMAN_GOLD",
                "candidateIds": candidate_ids,
                "candidateScopeSource": scope_source,
                "evidenceGapKeys": RESEARCH_GAPS.get(scenario_id, []),
            }
        )
    if len(result) != 33:
        raise RuntimeError(f"expected 33 public clusters, got {len(result)}")
    if sum(1 for row in result if row["routeGold"].startswith("RESEARCH_")) != 9:
        raise RuntimeError("expected exactly 9 frozen research cases")
    output.parent.mkdir(parents=True, exist_ok=False)
    output.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for row in result
        ),
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "output": str(output.relative_to(ROOT)).replace("\\", "/"),
                "rows": len(result),
                "researchRows": 9,
                "sha256": sha256_file(output),
                "sealedDataUsed": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    execute(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
