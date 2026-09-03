"""Finalize delegated assistant judgments for the remaining Track B short queries.

The output is explicitly assistant-judged and is not promoted to human gold.
Existing project-owner judgments for the first short query are preserved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from evaluation.kuaisearch_synthetic_evidence_benchmark import (
    BENCHMARK_ID,
    JsonlWriter,
    artifact_fingerprint,
    write_json,
)


QUERY_CONFIG = {
    "synq-610da833ad40c6eb": {
        "atomId": "tb-jeans-01-single-a01",
        "true": {"03", "06", "07", "10", "18", "19", "22", "24"},
        "unknown": {"20", "21"},
        "grade0": {"11"},
        "flags": {
            "01": ["title_attribute_waist_conflict"],
            "07": ["title_attribute_waist_ambiguity"],
            "11": ["catalog_category_title_conflict"],
        },
        "unknownEvidence": {},
    },
    "synq-99f722adf4c6b82e": {
        "atomId": "tb-shoes-01-single-a01",
        "true": {"10", "13", "17", "18", "22"},
        "unknown": {"06", "07", "11", "14", "15", "16", "20"},
        "grade0": {"04"},
        "flags": {
            "04": ["catalog_category_title_conflict", "title_attribute_heel_conflict"],
            "14": ["attribute_heel_conflict_flat_vs_mid"],
            "15": ["attribute_heel_conflict_flat_vs_high"],
            "20": ["attribute_heel_conflict_flat_vs_mid"],
        },
        "unknownEvidence": {
            "14": "conflicting",
            "15": "conflicting",
            "20": "conflicting",
        },
    },
}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError(f"expected object at {path}:{line_number}")
                rows.append(row)
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    writer = JsonlWriter(path)
    for row in rows:
        writer.write(row)
    return writer.close()


def suffix(row: dict[str, Any]) -> str:
    return row["candidateDisplayId"].rsplit("-c", 1)[-1]


def judge_row(
    row: dict[str, Any], *, delegated_at: str, delegation_evidence_sha256: str
) -> dict[str, Any]:
    config = QUERY_CONFIG[row["queryId"]]
    candidate_suffix = suffix(row)
    flags = config["flags"].get(candidate_suffix, [])
    if candidate_suffix in config["true"]:
        grade: int = 3
        eligible: bool | str = True
        atom_status = "pass"
        evidence_state = "explicit_match"
        reason = "Semantic category and the requested hard attribute both have supporting evidence."
    elif candidate_suffix in config["unknown"]:
        grade = 1
        eligible = "unknown"
        atom_status = "unknown"
        evidence_state = config["unknownEvidence"].get(candidate_suffix, "missing")
        reason = (
            "Same-category relevance is retained, but conflicting evidence prevents a reliable hard-attribute decision."
            if evidence_state == "conflicting"
            else "Same-category relevance is retained, but the requested hard attribute lacks evidence."
        )
    elif candidate_suffix in config["grade0"]:
        grade = 0
        eligible = False
        atom_status = "fail"
        evidence_state = "explicit_category_conflict"
        reason = "Title semantics explicitly contradict the requested product category."
    else:
        grade = 1
        eligible = False
        atom_status = "fail"
        evidence_state = "explicit_controlled_alternative"
        reason = "Product remains category-relevant, but explicit evidence fails the requested hard attribute."

    product = row["product"]
    decision = {
        "benchmarkId": BENCHMARK_ID,
        "queryId": row["queryId"],
        "queryText": row["queryText"],
        "candidateDisplayId": row["candidateDisplayId"],
        "itemId": product["itemId"],
        "relevanceGrade": grade,
        "eligible": eligible,
        "requirementAtoms": [
            {
                "atomId": config["atomId"],
                "status": atom_status,
                "evidenceState": evidence_state,
            }
        ],
        "evidenceSnapshot": {
            "title": product["title"],
            "categoryPath": product["categoryPath"],
            "rawAttributeEvidence": [
                evidence["rawValue"] for evidence in product["rawAttributeEvidence"]
            ],
        },
        "reason": reason,
        "qualityFlags": flags,
        "judgmentProvenance": {
            "type": "user_delegated_assistant_judgment",
            "assistantId": "codex",
            "rubricVersion": "short-query-hierarchical-v2",
            "delegatedAt": delegated_at,
            "delegationEvidenceSha256": delegation_evidence_sha256,
            "humanConfirmed": False,
            "automaticRuleOnly": False,
        },
        "goldStatus": "not_human_gold_assistant_judged",
    }
    return decision


def complete_review(
    benchmark_dir: Path, *, delegated_at: str, delegation_evidence_sha256: str
) -> dict[str, Any]:
    ready_path = benchmark_dir / "blind_stage_2_product_review_ready.jsonl"
    ready_rows = load_jsonl(ready_path)
    target_rows = [row for row in ready_rows if row["queryId"] in QUERY_CONFIG]
    if len(target_rows) != 48:
        raise ValueError(f"expected 48 remaining short-query candidates, got {len(target_rows)}")
    for query_id in QUERY_CONFIG:
        query_rows = [row for row in target_rows if row["queryId"] == query_id]
        if len(query_rows) != 24 or {suffix(row) for row in query_rows} != {
            f"{index:02d}" for index in range(1, 25)
        }:
            raise ValueError(f"incomplete candidate set for {query_id}")

    assistant_decisions = [
        judge_row(
            row,
            delegated_at=delegated_at,
            delegation_evidence_sha256=delegation_evidence_sha256,
        )
        for row in target_rows
    ]
    assistant_artifact = write_jsonl(
        benchmark_dir / "assistant_short_query_decisions_v1.jsonl", assistant_decisions
    )

    human_paths = [
        benchmark_dir / f"product_review_decisions_batch_{index:03d}.jsonl"
        for index in range(1, 5)
    ]
    human_rows = [row for path in human_paths for row in load_jsonl(path)]
    if len(human_rows) != 24:
        raise ValueError(f"expected 24 project-owner decisions, got {len(human_rows)}")
    combined_rows = [*human_rows, *assistant_decisions]
    if len({row["candidateDisplayId"] for row in combined_rows}) != 72:
        raise ValueError("short-query decisions contain duplicate candidate IDs")
    combined_artifact = write_jsonl(
        benchmark_dir / "short_query_decisions_combined_v1.jsonl", combined_rows
    )

    summaries = []
    for query_id in (
        "synq-c47854c5df3032ba",
        "synq-610da833ad40c6eb",
        "synq-99f722adf4c6b82e",
    ):
        rows = [row for row in combined_rows if row["queryId"] == query_id]
        summaries.append(
            {
                "queryId": query_id,
                "candidateCount": len(rows),
                "gradeCounts": dict(sorted(Counter(row["relevanceGrade"] for row in rows).items())),
                "eligibleCounts": dict(
                    sorted(Counter(str(row["eligible"]).lower() for row in rows).items())
                ),
                "qualityFlagCount": sum(bool(row.get("qualityFlags")) for row in rows),
                "humanConfirmedCount": sum(
                    bool(row.get("humanConfirmed"))
                    or bool(row.get("judgmentProvenance", {}).get("humanConfirmed"))
                    for row in rows
                ),
            }
        )

    audit = {
        "benchmarkId": BENCHMARK_ID,
        "status": "short_query_review_complete_mixed_provenance_not_gold",
        "shortQueryCount": 3,
        "candidateDecisionCount": 72,
        "provenanceCounts": {
            "projectOwnerHumanConfirmed": 24,
            "userDelegatedAssistantJudged": 48,
        },
        "querySummaries": summaries,
        "rubric": {
            "grade3": "semantic category match and explicit hard-attribute pass",
            "grade1False": "semantic category match but explicit hard-attribute fail",
            "grade1Unknown": "semantic category match but hard-attribute evidence missing or conflicting",
            "grade0": "semantic product-category mismatch",
            "eligibleTrue": "category and every hard requirement pass",
            "eligibleFalse": "at least one hard requirement explicitly fails",
            "eligibleUnknown": "no explicit hard failure, but a hard requirement is unresolved",
        },
        "inputs": {
            "blindProductReviewReady": artifact_fingerprint(ready_path, rows=216),
            "humanDecisionBatches": [artifact_fingerprint(path, rows=6) for path in human_paths],
        },
        "outputs": {
            "assistantDecisions": assistant_artifact,
            "combinedShortQueryDecisions": combined_artifact,
        },
        "goldStatus": "not_gold_requires_human_confirmation_of_48_assistant_judgments",
        "nextGate": "Human audit or confirmation of delegated assistant judgments before freezing qrels.",
    }
    audit_artifact = write_json(benchmark_dir / "short_query_review_audit_v1.json", audit)
    manifest = {
        "benchmarkId": BENCHMARK_ID,
        "layer": "short_query_review_v1",
        "baseManifest": artifact_fingerprint(benchmark_dir / "manifest.json"),
        "status": audit["status"],
        "artifacts": {
            "assistantDecisions": assistant_artifact,
            "combinedShortQueryDecisions": combined_artifact,
            "reviewAudit": audit_artifact,
        },
    }
    write_json(benchmark_dir / "short_query_review_manifest_v1.json", manifest)
    return audit


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-dir", type=Path, required=True)
    parser.add_argument("--delegated-at", required=True)
    parser.add_argument("--delegation-evidence-sha256", required=True)
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    complete_review(
        args.benchmark_dir,
        delegated_at=args.delegated_at,
        delegation_evidence_sha256=args.delegation_evidence_sha256,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
