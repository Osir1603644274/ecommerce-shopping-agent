"""Materialize a completed second-human review from a human-confirmed WIP record."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
ANNOTATION = ROOT / "data" / "annotations" / "ecommerce" / "used_phone_human_qrel_v1"
EXPECTED_QUERY_IDS = {"uphq-008", "uphq-009", "uphq-012"}
LABELS = {
    None: "unknown",
    0: "not_relevant",
    1: "marginal",
    2: "relevant",
    3: "highly_relevant",
}
GROUPS = {
    "grade3": 3,
    "grade2": 2,
    "grade1": 1,
    "grade0": 0,
    "unknown": None,
}


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _judgment_text(query_id: str, grade: int | None) -> tuple[list[str], str]:
    if query_id == "uphq-008":
        if grade == 3:
            return ["title_claim", "attribute_fact"], "候选具有1亿或2亿等本池最高档、直接明确的像素证据。"
        if grade == 2:
            return ["title_claim", "attribute_fact"], "候选具有4000万至6400万等明显较高的像素证据。"
        if grade == 1:
            return ["title_claim", "attribute_fact"], "候选只有较低像素记录或明确但较弱的影像描述。"
        if grade == 0:
            return [], "候选只是手机，现有标题和属性没有像素或影像证据。"
        return ["title_claim", "attribute_fact"], "候选字段明显冲突，无法可靠判断影像检索相关性。"
    if query_id == "uphq-009":
        if grade == 3:
            return ["title_claim", "attribute_fact", "synthetic_price"], "候选明确为Android，且AI合成模拟参考价低于1000元；模拟价格非真实报价。"
        if grade == 2:
            return ["title_claim", "attribute_fact", "synthetic_price"], "候选明确为Android，但AI合成模拟参考价超过1000元；价格仅为软偏好且非真实报价。"
        if grade == 1:
            return ["title_claim", "attribute_fact", "synthetic_price"], "候选仅部分满足Android百元机检索意图。"
        if grade == 0:
            return ["title_claim", "attribute_fact", "brand_fact", "constraint_conflict"], "候选明确违反Android硬约束，或随机发货无法保证Android。"
        return ["title_claim", "attribute_fact"], "候选系统字段缺失，或Android与HarmonyOS证据冲突，无法可靠判断。"
    if grade == 3:
        return ["title_claim", "brand_fact", "synthetic_price"], "标题明确且唯一指向苹果iPhone 13 Pro；模拟参考价仅支持离线价格信息相关性，非当前真实价格。"
    if grade == 0:
        return ["title_claim", "brand_fact", "constraint_conflict"], "候选为其他品牌、其他iPhone型号、普通13或13 Pro Max，违反苹果iPhone 13 Pro精确型号硬约束。"
    if grade is None:
        return ["title_claim", "brand_fact"], "标题同时混写iPhone 13 Pro与Pro Max，无法确定实际型号。"
    return ["title_claim", "brand_fact", "synthetic_price"], "候选仅部分满足苹果iPhone 13 Pro价格信息检索意图。"


def materialize(blind_rows: list[dict[str, Any]], wip: dict[str, Any], completed_at: str) -> list[dict[str, Any]]:
    reviewer_id = str(wip.get("reviewerId") or "").strip()
    if not reviewer_id or wip.get("reviewerKind") != "independent_human":
        raise ValueError("WIP lacks independent human reviewer provenance")
    if wip.get("firstHumanLabelsDisclosed") is not False:
        raise ValueError("WIP is not a blind independent review")
    confirmed = wip.get("confirmedQueryJudgments")
    if not isinstance(confirmed, dict) or set(confirmed) != EXPECTED_QUERY_IDS:
        raise ValueError("WIP must contain exactly the three preregistered confirmed queries")
    if {str(row.get("queryId")) for row in blind_rows} != EXPECTED_QUERY_IDS:
        raise ValueError("blind batch identity mismatch")

    completed: list[dict[str, Any]] = []
    for source_row in blind_rows:
        row = copy.deepcopy(source_row)
        query_id = str(row["queryId"])
        confirmation = confirmed[query_id]
        if confirmation.get("humanConfirmed") is not True:
            raise ValueError(f"query is not human-confirmed: {query_id}")
        grade_by_product: dict[int, int | None] = {}
        for group_name, grade in GROUPS.items():
            product_ids = confirmation.get(group_name)
            if not isinstance(product_ids, list):
                raise ValueError(f"missing judgment group: {query_id}:{group_name}")
            for raw_product_id in product_ids:
                product_id = int(raw_product_id)
                if product_id in grade_by_product:
                    raise ValueError(f"duplicate grouped product: {query_id}:{product_id}")
                grade_by_product[product_id] = grade
        candidate_ids = {int(candidate["productId"]) for candidate in row["candidates"]}
        if set(grade_by_product) != candidate_ids:
            raise ValueError(f"grouped judgment coverage mismatch: {query_id}")
        if int(confirmation.get("candidateCount", -1)) != len(candidate_ids):
            raise ValueError(f"candidate count mismatch: {query_id}")

        row["schemaVersion"] = "used-phone-human-qrel-second-review-completed-v1"
        row["reviewProtocol"]["secondHumanReviewerId"] = reviewer_id
        row["reviewProtocol"]["reviewCompletedAt"] = completed_at
        reviewed_at = str(confirmation.get("confirmedAt") or "").strip()
        if not reviewed_at:
            raise ValueError(f"query confirmation lacks timestamp: {query_id}")
        for candidate in row["candidates"]:
            product_id = int(candidate["productId"])
            grade = grade_by_product[product_id]
            evidence, reason = _judgment_text(query_id, grade)
            candidate["secondHumanJudgment"] = {
                "grade": grade,
                "label": LABELS[grade],
                "evidenceBasis": evidence,
                "reason": reason,
                "reviewStatus": "human_confirmed",
                "reviewerId": reviewer_id,
                "reviewedAt": reviewed_at,
            }
        completed.append(row)
    return sorted(completed, key=lambda item: item["queryId"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blind-review", type=Path, default=ANNOTATION / "review_batches" / "second_human_blind_batch_001.json")
    parser.add_argument("--wip", type=Path, default=ANNOTATION / "review_batches" / "second_human_review_wip_attempt_002.json")
    parser.add_argument("--output", type=Path, default=ANNOTATION / "review_batches" / "second_human_completed_batch_001.json")
    parser.add_argument("--review-completed-at", required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"refusing to overwrite completed human review: {args.output}")
    completed = materialize(_load(args.blind_review), _load(args.wip), args.review_completed_at)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(completed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "queryCount": len(completed), "candidateCount": sum(len(row["candidates"]) for row in completed)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
