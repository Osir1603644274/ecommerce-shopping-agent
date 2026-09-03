"""Role-aware freeze, catalog residual audit, and preregistered Qrel baselines.

This is an offline-only evaluator.  It deliberately keeps non-static scenarios
out of retrieval metrics and never converts unknown or pool-external products
into negatives.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from agent.app.domains.ecommerce.models import (
    bm25_rank,
    canonicalize_brand,
    expand_product_query,
    reciprocal_rank_fusion,
)
from agent.app.domains.ecommerce.used_phone_attributes import (
    USED_PHONE_ATTRIBUTE_REGISTRY,
    observe_used_phone_attributes,
)
from agent.evaluation.used_phone_human_qrel_v1 import (
    LocalCrossEncoder, file_sha256, freeze_role_aware_human_qrels, read_jsonl,
    validate_review_rows,
)
from agent.evaluation.used_phone_real_query_retrieval_v1 import build_vector_ranker


PHASE_SCHEMA = "used-phone-human-qrel-phase-v1"
PRIMARY = "primary_retrieval"


def _content_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def runtime_documents(documents: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"id": int(row["product_id"]), "title": str(row.get("item_title") or ""),
             "brand": str(row.get("brand") or ""), "attributeText": str(row.get("attr_value") or "")}
            for row in documents]


def _normalized(row: dict[str, Any]) -> str:
    return re.sub(r"[\s_\-/.]+", "", " ".join(str(row.get(key) or "") for key in ("item_title", "brand", "attr_value"))).lower()


def explicitly_satisfies(document: dict[str, Any], constraint: dict[str, Any]) -> bool:
    """Only return true for positive catalog evidence; lack of evidence is not a match."""
    text = _normalized(document)
    key, value = str(constraint.get("key")), str(constraint.get("value"))
    if key == "brand":
        return (value == "apple" and ("apple" in text or "苹果" in text)) or (value == "oppo" and "oppo" in text)
    if key == "os":
        return value in text or (value == "android" and "安卓" in text)
    if key == "screen_originality":
        return "原装屏" in text and "非原装屏" not in text and "非原装内屏" not in text
    if key == "battery_originality":
        return "原装电池" in text and "非原装电池" not in text
    if key == "model" and value == "oppo_a11x":
        return "oppoa11x" in text
    if key == "model" and value == "iphone_13_pro":
        exact_aliases = ("iphone13pro", "苹果13pro")
        conflicting_aliases = ("iphone13promax", "苹果13promax")
        return any(alias in text for alias in exact_aliases) and not any(
            alias in text for alias in conflicting_aliases
        )
    return False


def _hard_matches(document: dict[str, Any], row: dict[str, Any]) -> bool:
    constraints = row.get("intent", {}).get("hardConstraints", [])
    return all(explicitly_satisfies(document, item) for item in constraints)


def hard_constraint_status(
    document: dict[str, Any], constraint: dict[str, Any],
) -> str:
    """Return pass/fail/unknown from authoritative fields only.

    Unlike ``explicitly_satisfies``, this preserves the distinction between an
    explicit contradiction and missing/conflicting evidence.  That distinction
    matches the production rule reranker's confirmed-before-unknown contract.
    """
    key = str(constraint.get("key") or "")
    operator = str(constraint.get("operator") or "")
    expected = constraint.get("value")
    actual: Any = None
    if key == "brand":
        raw_brand = str(document.get("brand") or "").strip()
        if not raw_brand:
            return "unknown"
        actual = canonicalize_brand(raw_brand)
        if isinstance(expected, str):
            expected = canonicalize_brand(expected)
        elif isinstance(expected, list):
            expected = [
                canonicalize_brand(item) if isinstance(item, str) else item
                for item in expected
            ]
    elif key in USED_PHONE_ATTRIBUTE_REGISTRY:
        observation = observe_used_phone_attributes(
            str(document.get("attr_value") or "")
        )[key]
        if observation.status != "known" or observation.fact is None:
            return "unknown"
        actual = observation.fact.value
    elif key == "model":
        title = re.sub(
            r"[\s_\-/.]+", "", str(document.get("item_title") or "")
        ).casefold()
        if expected == "iphone_13_pro":
            exact = bool(re.search(r"(?:iphone|苹果)13pro(?!max)", title))
            pro_max = "iphone13promax" in title or "苹果13promax" in title
            if exact and pro_max:
                return "unknown"
            if exact:
                return "pass"
            if pro_max or "iphone" in title or "苹果" in title:
                return "fail"
            return "unknown"
        if expected == "oppo_a11x":
            if "oppoa11x" in title:
                return "pass"
            if "oppo" in title and "a11" in title:
                return "fail"
            return "unknown"
        return "unknown"
    else:
        return "unknown"
    if operator == "eq":
        return "pass" if actual == expected else "fail"
    if operator == "in" and isinstance(expected, list):
        return "pass" if actual in expected else "fail"
    if operator == "not_in" and isinstance(expected, list):
        return "pass" if actual not in expected else "fail"
    return "unknown"


def apply_deterministic_hard_gate(
    ranking: list[int],
    row: dict[str, Any],
    docs_by_id: dict[int, dict[str, Any]],
    *,
    candidate_depth: int = 50,
) -> tuple[list[int], dict[str, Any]]:
    """Eliminate explicit hard failures and place unknowns after confirmations."""
    constraints = list(row.get("intent", {}).get("hardConstraints", []))
    confirmed: list[int] = []
    unknown: list[int] = []
    eliminated: list[int] = []
    status_by_product: dict[str, list[str]] = {}
    for product_id in ranking[:candidate_depth]:
        statuses = [
            hard_constraint_status(docs_by_id[product_id], constraint)
            for constraint in constraints
        ]
        status_by_product[str(product_id)] = statuses
        if "fail" in statuses:
            eliminated.append(product_id)
        elif "unknown" in statuses:
            unknown.append(product_id)
        else:
            confirmed.append(product_id)
    output = [*confirmed, *unknown]
    return output, {
        "inputCandidateCount": min(candidate_depth, len(ranking)),
        "confirmedCandidateCount": len(confirmed),
        "unknownCandidateCount": len(unknown),
        "eliminatedExplicitFailureCount": len(eliminated),
        "confirmedBeforeUnknown": True,
        "explicitFailuresExcluded": True,
        "unknownsRetainedAsClosestAlternatives": True,
        "outputCandidateCount": len(output),
        "eliminatedProductIds": eliminated,
        "constraintStatusesByProductId": status_by_product,
    }


def audit_full_catalog_residuals(
    *, review_rows: list[dict[str, Any]], documents: list[dict[str, Any]], residual_depth: int = 10,
) -> dict[str, Any]:
    """Scan all identities; residuals stay unjudged rather than becoming labels."""
    validate_review_rows(review_rows)
    products = runtime_documents(documents)
    by_id = {int(row["product_id"]): row for row in documents}
    audits = []
    for row in review_rows:
        pool_ids = {int(item["productId"]) for item in row["candidates"]}
        query, _ = expand_product_query(str(row["retrievalQuery"]))
        ranking = bm25_rank(query, products)
        residual_ids = [item for item in ranking if item not in pool_ids][:residual_depth]
        hard_constraints = list(row.get("intent", {}).get("hardConstraints", []))
        full_matches = [doc for doc in documents if _hard_matches(doc, row)] if hard_constraints else []
        audits.append({
            "queryId": row["queryId"], "evaluationRole": row["evaluationRole"],
            "catalogScannedProductCount": len(documents), "poolCandidateCount": len(pool_ids),
            "outsidePoolCount": len(documents) - len(pool_ids),
            "outsidePoolIsUnjudgedNotNegative": True,
            "residualMethod": "expanded_bm25_full_catalog_first_unpooled",
            "residualCandidates": [{"productId": product_id, "itemTitle": by_id[product_id]["item_title"],
                                      "brand": by_id[product_id].get("brand", "")}
                                     for product_id in residual_ids],
            "hardConstraintCatalogMatchCount": len(full_matches),
            "hardConstraintCatalogMatches": [{"productId": int(doc["product_id"]), "itemTitle": doc["item_title"]}
                                               for doc in full_matches],
        })
    no_answer = next(item for item in audits if item["queryId"] == "uphq-011")
    return {"schemaVersion": PHASE_SCHEMA, "artifact": "full-catalog-pool-residual-audit-v1",
            "catalogProductCount": len(documents), "queryCount": len(audits),
            "poolExternalProductsAreLabels": False, "audits": audits,
            "noAnswerConstraintConclusion": {"queryId": "uphq-011", "fullCatalogScanned": len(documents),
                "explicitHardConstraintMatchCount": no_answer["hardConstraintCatalogMatchCount"],
                "conclusion": "no_explicit_catalog_answer" if no_answer["hardConstraintCatalogMatchCount"] == 0 else "catalog_match_exists",
                "scope": "frozen 252-product catalog and explicit title/attribute evidence only"}}


def build_second_human_blind_package(review_rows: list[dict[str, Any]], *, query_ids: set[str] | None = None) -> list[dict[str, Any]]:
    """Remove first-review labels and rank/source cues; this is for a human, not AI."""
    validate_review_rows(review_rows)
    wanted = query_ids or {"uphq-008", "uphq-009", "uphq-012"}
    if wanted != {"uphq-008", "uphq-009", "uphq-012"}:
        raise ValueError("second-human package is preregistered for uphq-008/009/012")
    package = []
    for row in review_rows:
        if row["queryId"] not in wanted:
            continue
        candidates = [{"productId": int(candidate["productId"]), "itemTitle": candidate["itemTitle"],
                       "brand": candidate["brand"], "attrValue": candidate["attrValue"],
                       **({"syntheticReferencePrice": candidate["syntheticReferencePrice"]} if "syntheticReferencePrice" in candidate else {}),
                       "secondHumanJudgment": {"grade": None, "label": "unjudged", "evidenceBasis": [], "reason": "",
                                                "reviewStatus": "pending_second_human_review", "reviewerId": "", "reviewedAt": None}}
                      for candidate in row["candidates"]]
        random.Random("used-phone-second-human-v1:" + row["queryId"]).shuffle(candidates)
        package.append({"schemaVersion": "used-phone-human-qrel-second-review-v1", "queryId": row["queryId"],
                        "rawQuery": row["rawQuery"], "retrievalQuery": row["retrievalQuery"], "intent": row["intent"],
                        "evaluationRole": row["evaluationRole"], "stratum": row["stratum"], "split": row["split"],
                        "reviewProtocol": {"requiredReviewerKind": "independent_human", "aiMayNotBeCountedAsSecondHuman": True,
                                           "firstHumanLabelsDisclosed": False, "candidateOrder": "deterministic_blinded_shuffle_v1"},
                        "candidates": candidates})
    if {row["queryId"] for row in package} != wanted:
        raise ValueError("missing preregistered second-human query")
    return sorted(package, key=lambda item: item["queryId"])


def audit_completed_second_human_reviews(
    first_review_rows: list[dict[str, Any]],
    second_review_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate an independent completed review and expose disagreements only.

    This function deliberately cannot emit qrels or release sealed-test labels.
    A separate human adjudication record is still required even when both
    reviewers agree on every candidate.
    """
    validate_review_rows(first_review_rows)
    if not isinstance(second_review_rows, list):
        raise ValueError("completed second review must be a list")
    expected_ids = {"uphq-008", "uphq-009", "uphq-012"}
    first_by_query = {
        row["queryId"]: row for row in first_review_rows
        if row["queryId"] in expected_ids
    }
    if set(first_by_query) != expected_ids:
        raise ValueError("first review is missing preregistered sealed queries")
    second_by_query: dict[str, dict[str, Any]] = {}
    for row in second_review_rows:
        query_id = str(row.get("queryId") or "")
        if query_id in second_by_query:
            raise ValueError(f"duplicate second-human queryId: {query_id!r}")
        second_by_query[query_id] = row
    if set(second_by_query) != expected_ids:
        raise ValueError("second review must contain exactly uphq-008/009/012")

    allowed_labels = {
        None: "unknown",
        0: "not_relevant",
        1: "marginal",
        2: "relevant",
        3: "highly_relevant",
    }
    allowed_evidence = {
        "title_claim", "attribute_fact", "brand_fact",
        "synthetic_price", "constraint_conflict",
    }
    audits = []
    total_candidates = 0
    total_agreements = 0
    for query_id in sorted(expected_ids):
        first = first_by_query[query_id]
        second = second_by_query[query_id]
        if second.get("schemaVersion") != "used-phone-human-qrel-second-review-completed-v1":
            raise ValueError(f"unexpected completed second-review schema: {query_id}")
        for field in ("rawQuery", "retrievalQuery", "intent", "evaluationRole", "stratum", "split"):
            if second.get(field) != first.get(field):
                raise ValueError(f"second-review identity mismatch: {query_id}:{field}")
        protocol = second.get("reviewProtocol")
        if not isinstance(protocol, dict) or (
            protocol.get("requiredReviewerKind") != "independent_human"
            or protocol.get("aiMayNotBeCountedAsSecondHuman") is not True
            or protocol.get("firstHumanLabelsDisclosed") is not False
            or protocol.get("candidateOrder") != "deterministic_blinded_shuffle_v1"
        ):
            raise ValueError(f"invalid second-review protocol: {query_id}")
        reviewer_id = str(protocol.get("secondHumanReviewerId") or "").strip()
        completed_at = str(protocol.get("reviewCompletedAt") or "").strip()
        if not reviewer_id or not completed_at:
            raise ValueError(f"completed second review lacks reviewer provenance: {query_id}")
        first_reviewer_ids = {
            str(first.get("reviewerId") or ""),
            *(str(item["judgment"].get("reviewerId") or "") for item in first["candidates"]),
        }
        if reviewer_id in first_reviewer_ids:
            raise ValueError(f"second reviewer is not independent: {query_id}")

        first_candidates = {int(item["productId"]): item for item in first["candidates"]}
        second_candidates = second.get("candidates")
        if not isinstance(second_candidates, list) or not second_candidates:
            raise ValueError(f"empty completed second review: {query_id}")
        second_by_product: dict[int, dict[str, Any]] = {}
        for candidate in second_candidates:
            product_id = int(candidate["productId"])
            if product_id in second_by_product:
                raise ValueError(f"duplicate second-review candidate: {query_id}:{product_id}")
            second_by_product[product_id] = candidate
        if set(second_by_product) != set(first_candidates):
            raise ValueError(f"second-review candidate identity set mismatch: {query_id}")

        disagreements = []
        agreement_count = 0
        for product_id, first_candidate in first_candidates.items():
            second_candidate = second_by_product[product_id]
            for field in ("itemTitle", "brand", "attrValue", "syntheticReferencePrice"):
                if second_candidate.get(field) != first_candidate.get(field):
                    raise ValueError(
                        f"second-review candidate evidence mismatch: {query_id}:{product_id}:{field}"
                    )
            judgment = second_candidate.get("secondHumanJudgment")
            if not isinstance(judgment, dict):
                raise ValueError(f"missing second-human judgment: {query_id}:{product_id}")
            grade = judgment.get("grade")
            if (grade is not None and type(grade) is not int) or (
                grade not in allowed_labels
                or judgment.get("label") != allowed_labels[grade]
            ):
                raise ValueError(f"second-human grade/label mismatch: {query_id}:{product_id}")
            if judgment.get("reviewStatus") != "human_confirmed":
                raise ValueError(f"second-human judgment is not confirmed: {query_id}:{product_id}")
            if str(judgment.get("reviewerId") or "").strip() != reviewer_id:
                raise ValueError(f"second-human reviewer identity drift: {query_id}:{product_id}")
            if not str(judgment.get("reviewedAt") or "").strip():
                raise ValueError(f"second-human judgment lacks reviewedAt: {query_id}:{product_id}")
            if not str(judgment.get("reason") or "").strip():
                raise ValueError(f"second-human judgment lacks reason: {query_id}:{product_id}")
            evidence = judgment.get("evidenceBasis")
            if not isinstance(evidence, list) or any(item not in allowed_evidence for item in evidence):
                raise ValueError(f"invalid second-human evidence basis: {query_id}:{product_id}")
            first_grade = first_candidate["judgment"].get("grade")
            if grade == first_grade:
                agreement_count += 1
            else:
                disagreements.append({
                    "productId": product_id,
                    "firstHumanGrade": first_grade,
                    "secondHumanGrade": grade,
                    "adjudicationStatus": "pending_human_adjudication",
                    "finalGrade": None,
                })
        candidate_count = len(first_candidates)
        total_candidates += candidate_count
        total_agreements += agreement_count
        audits.append({
            "queryId": query_id,
            "candidateCount": candidate_count,
            "agreementCount": agreement_count,
            "disagreementCount": len(disagreements),
            "exactGradeAgreementRate": agreement_count / candidate_count,
            "disagreements": disagreements,
        })
    source_bindings = {
        "firstSealedReviewContentSha256": _content_sha256([
            first_by_query[query_id] for query_id in sorted(expected_ids)
        ]),
        "completedSecondReviewContentSha256": _content_sha256([
            second_by_query[query_id] for query_id in sorted(expected_ids)
        ]),
    }
    return {
        "schemaVersion": "used-phone-human-qrel-second-review-audit-v1",
        "status": "PENDING_HUMAN_ADJUDICATION_NOT_GOLD",
        "queryIds": sorted(expected_ids),
        "candidateCount": total_candidates,
        "agreementCount": total_agreements,
        "disagreementCount": total_candidates - total_agreements,
        "exactGradeAgreementRate": total_agreements / total_candidates,
        "sealedReleaseAllowed": False,
        "sourceBindings": source_bindings,
        "requiredNextAction": (
            "a human adjudicator must approve agreements and assign final grades "
            "to every disagreement before a separate sealed-release workflow exists"
        ),
        "queries": audits,
    }


def freeze_adjudicated_sealed_human_qrels(
    first_review_rows: list[dict[str, Any]],
    second_review_rows: list[dict[str, Any]],
    adjudication: dict[str, Any],
) -> dict[str, Any]:
    """Freeze sealed qrels only from completed human review and adjudication.

    The result is authorized for one-shot offline evaluation only. It never
    changes production retrieval and is separate from the non-sealed freeze.
    """
    audit = audit_completed_second_human_reviews(
        first_review_rows, second_review_rows
    )
    if adjudication.get("schemaVersion") != "used-phone-human-qrel-adjudication-v1":
        raise ValueError("unexpected human adjudication schemaVersion")
    if adjudication.get("status") != "human_adjudicated":
        raise ValueError("sealed adjudication is not human_adjudicated")
    expected_bindings = {
        **audit["sourceBindings"],
        "secondReviewAuditContentSha256": _content_sha256(audit),
    }
    if adjudication.get("sourceBindings") != expected_bindings:
        raise ValueError("sealed adjudication source binding mismatch")
    adjudicator = adjudication.get("adjudicator")
    if not isinstance(adjudicator, dict) or (
        adjudicator.get("kind") != "human"
        or adjudicator.get("aiMayNotBeCountedAsHuman") is not True
        or not str(adjudicator.get("adjudicatorId") or "").strip()
        or not str(adjudicator.get("adjudicatedAt") or "").strip()
    ):
        raise ValueError("sealed adjudication lacks human provenance")
    adjudicator_id = str(adjudicator["adjudicatorId"])
    adjudicated_at = str(adjudicator["adjudicatedAt"])

    audit_by_query = {row["queryId"]: row for row in audit["queries"]}
    adjudication_rows = adjudication.get("queries")
    if not isinstance(adjudication_rows, list):
        raise ValueError("sealed adjudication queries must be a list")
    decisions_by_query: dict[str, dict[int, dict[str, Any]]] = {}
    seen_queries: set[str] = set()
    allowed_labels = {
        None: "unknown",
        0: "not_relevant",
        1: "marginal",
        2: "relevant",
        3: "highly_relevant",
    }
    for row in adjudication_rows:
        query_id = str(row.get("queryId") or "")
        if query_id in seen_queries or query_id not in audit_by_query:
            raise ValueError(f"invalid or duplicate adjudication queryId: {query_id!r}")
        seen_queries.add(query_id)
        if row.get("approveExactAgreements") is not True or not str(
            row.get("agreementApprovalReason") or ""
        ).strip():
            raise ValueError(f"exact agreements lack human approval: {query_id}")
        expected_disagreements = {
            int(item["productId"]): item
            for item in audit_by_query[query_id]["disagreements"]
        }
        decisions = row.get("disagreementDecisions")
        if not isinstance(decisions, list):
            raise ValueError(f"adjudication decisions must be a list: {query_id}")
        by_product: dict[int, dict[str, Any]] = {}
        for decision in decisions:
            product_id = int(decision["productId"])
            if product_id in by_product:
                raise ValueError(f"duplicate adjudication decision: {query_id}:{product_id}")
            by_product[product_id] = decision
        if set(by_product) != set(expected_disagreements):
            raise ValueError(f"adjudication disagreement set mismatch: {query_id}")
        for product_id, decision in by_product.items():
            expected = expected_disagreements[product_id]
            if (
                decision.get("firstHumanGrade") != expected["firstHumanGrade"]
                or decision.get("secondHumanGrade") != expected["secondHumanGrade"]
            ):
                raise ValueError(f"adjudication grade identity mismatch: {query_id}:{product_id}")
            final_grade = decision.get("finalGrade")
            if (final_grade is not None and type(final_grade) is not int) or (
                final_grade not in allowed_labels
                or decision.get("finalLabel") != allowed_labels[final_grade]
            ):
                raise ValueError(f"invalid adjudicated grade/label: {query_id}:{product_id}")
            if not str(decision.get("reason") or "").strip():
                raise ValueError(f"adjudication decision lacks reason: {query_id}:{product_id}")
        decisions_by_query[query_id] = by_product
    if seen_queries != set(audit_by_query):
        raise ValueError("sealed adjudication must cover every sealed query")

    first_by_query = {
        row["queryId"]: row for row in first_review_rows
        if row["queryId"] in audit_by_query
    }
    second_by_query = {row["queryId"]: row for row in second_review_rows}
    qrels = []
    unknown_count = 0
    for query_id in sorted(audit_by_query):
        first = first_by_query[query_id]
        first_by_product = {int(item["productId"]): item for item in first["candidates"]}
        second_by_product = {
            int(item["productId"]): item
            for item in second_by_query[query_id]["candidates"]
        }
        for product_id, first_candidate in first_by_product.items():
            first_grade = first_candidate["judgment"].get("grade")
            second_grade = second_by_product[product_id]["secondHumanJudgment"].get("grade")
            if first_grade == second_grade:
                final_grade = first_grade
                final_basis = "exact_two_human_agreement"
                final_reason = next(
                    row["agreementApprovalReason"]
                    for row in adjudication_rows if row["queryId"] == query_id
                )
            else:
                decision = decisions_by_query[query_id][product_id]
                final_grade = decision["finalGrade"]
                final_basis = "human_adjudicated_disagreement"
                final_reason = decision["reason"]
            if final_grade is None:
                unknown_count += 1
                continue
            qrels.append({
                "schemaVersion": "used-phone-human-qrel-v1",
                "queryId": query_id,
                "productId": product_id,
                "relevance": final_grade,
                "labelSource": "two_human_review_and_adjudication",
                "finalBasis": final_basis,
                "adjudicatorId": adjudicator_id,
                "adjudicatedAt": adjudicated_at,
                "reason": final_reason,
                "split": "sealed_test",
                "evaluationRole": "primary_retrieval",
                "stratum": "main_retrieval",
            })
    return {
        "schemaVersion": "used-phone-human-qrel-sealed-freeze-v1",
        "status": "SEALED_QREL_READY_FOR_ONE_SHOT_OFFLINE_EVALUATION",
        "productionReleaseAllowed": False,
        "queryIds": sorted(audit_by_query),
        "sourceBindings": expected_bindings,
        "unknownJudgmentCountExcludedNotNegative": unknown_count,
        "qrels": sorted(qrels, key=lambda row: (row["queryId"], row["productId"])),
    }


def _metrics(rankings: dict[str, list[int]], qrels: list[dict[str, Any]], rows_by_query: dict[str, dict[str, Any]], docs_by_id: dict[int, dict[str, Any]]) -> dict[str, Any]:
    by_query: dict[str, dict[int, int]] = defaultdict(dict)
    for qrel in qrels:
        by_query[qrel["queryId"]][int(qrel["productId"])] = int(qrel["relevance"])
    totals = defaultdict(float); errors = []
    for query_id, grades in by_query.items():
        positives = {item for item, grade in grades.items() if grade > 0}
        if not positives:
            continue
        ranked = rankings[query_id]
        totals["queries"] += 1
        for k in (1, 3): totals[f"hit{k}"] += float(bool(positives.intersection(ranked[:k])))
        for k in (20, 50): totals[f"recall{k}"] += len(positives.intersection(ranked[:k])) / len(positives)
        dcg = sum((2 ** grades.get(item, 0) - 1) / math.log2(index + 2) for index, item in enumerate(ranked[:3]))
        ideal = sorted(grades.values(), reverse=True)[:3]
        idcg = sum((2 ** grade - 1) / math.log2(index + 2) for index, grade in enumerate(ideal))
        totals["ndcg3"] += dcg / idcg if idcg else 0
        missed = positives - set(ranked[:50])
        errors.extend({"queryId": query_id, "type": "recall_miss", "productId": item} for item in sorted(missed))
        for product_id in ranked[:3]:
            grade = grades.get(product_id)
            if grade is None:
                errors.append({"queryId": query_id, "type": "insufficient_evidence", "productId": product_id})
            elif grade <= 1:
                constraints = rows_by_query[query_id].get("intent", {}).get("hardConstraints", [])
                kinds = {str(item.get("key")) for item in constraints if not explicitly_satisfies(docs_by_id[product_id], item)}
                error_type = "wrong_brand" if "brand" in kinds else "exact_model_confusion" if "model" in kinds else "numeric_or_price" if any(token in rows_by_query[query_id]["rawQuery"] for token in ("百", "千", "钱", "价")) else "semantic_misjudgment"
                errors.append({"queryId": query_id, "type": error_type, "productId": product_id})
    count = int(totals["queries"])
    return {"queryCount": count, "hitAt1": totals["hit1"] / count if count else None, "hitAt3": totals["hit3"] / count if count else None,
            "recallAt20": totals["recall20"] / count if count else None, "recallAt50": totals["recall50"] / count if count else None,
            "ndcgAt3": totals["ndcg3"] / count if count else None, "errorTaxonomy": errors}


def run_adjudicated_sealed_baselines(
    *,
    review_rows: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    sealed_freeze: dict[str, Any],
    cross_encoder: LocalCrossEncoder,
) -> dict[str, Any]:
    """Run the preregistered variants once against adjudicated sealed Qrels.

    Rankings are completed from a label-free query projection before qrels are
    opened.  The caller must independently authenticate ``sealed_freeze``
    against the two human reviews and adjudication before invoking this helper.
    """
    validate_review_rows(review_rows)
    expected_query_ids = ["uphq-008", "uphq-009", "uphq-012"]
    if (
        sealed_freeze.get("schemaVersion") != "used-phone-human-qrel-sealed-freeze-v1"
        or sealed_freeze.get("status") != "SEALED_QREL_READY_FOR_ONE_SHOT_OFFLINE_EVALUATION"
        or sealed_freeze.get("productionReleaseAllowed") is not False
        or sealed_freeze.get("queryIds") != expected_query_ids
    ):
        raise ValueError("invalid adjudicated sealed freeze contract")
    static_rows = [
        row for row in review_rows if row["queryId"] in expected_query_ids
    ]
    if [row["queryId"] for row in sorted(static_rows, key=lambda item: item["queryId"])] != expected_query_ids:
        raise ValueError("review rows lack the exact sealed query set")
    rows_by_query = {row["queryId"]: row for row in static_rows}
    ranking_inputs = [
        {"queryId": query_id, "retrievalQuery": rows_by_query[query_id]["retrievalQuery"]}
        for query_id in expected_query_ids
    ]
    products = runtime_documents(documents)
    docs_by_id = {int(doc["product_id"]): doc for doc in documents}
    build_started = time.perf_counter()
    vector = build_vector_ranker(products)
    build_ms = (time.perf_counter() - build_started) * 1000
    variants: dict[str, dict[str, list[int]]] = {
        name: {} for name in (
            "bm25", "title_vector", "bm25_vector_rrf", "rrf_cross_encoder",
        )
    }
    latency: dict[str, list[float]] = {name: [] for name in variants}
    stage_latency: dict[str, list[float]] = {
        name: [] for name in (
            "bm25", "title_vector", "rrf_fusion", "cross_encoder_rerank",
        )
    }
    for row in ranking_inputs:
        query_id, query = row["queryId"], row["retrievalQuery"]
        started = time.perf_counter()
        bm25 = bm25_rank(query, products)
        bm25_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        vec = vector(query, products)
        vector_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        rrf = [item for item, _score in reciprocal_rank_fusion([bm25, vec], k=60)]
        fusion_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        reranked = [
            item for item, _score in cross_encoder.rank(
                query,
                [
                    next(product for product in products if product["id"] == item)
                    for item in rrf[:50]
                ],
            )
        ] + rrf[50:]
        rerank_ms = (time.perf_counter() - started) * 1000
        stage_latency["bm25"].append(bm25_ms)
        stage_latency["title_vector"].append(vector_ms)
        stage_latency["rrf_fusion"].append(fusion_ms)
        stage_latency["cross_encoder_rerank"].append(rerank_ms)
        latency["bm25"].append(bm25_ms)
        latency["title_vector"].append(vector_ms)
        latency["bm25_vector_rrf"].append(bm25_ms + vector_ms + fusion_ms)
        latency["rrf_cross_encoder"].append(
            bm25_ms + vector_ms + fusion_ms + rerank_ms
        )
        variants["bm25"][query_id] = bm25
        variants["title_vector"][query_id] = vec
        variants["bm25_vector_rrf"][query_id] = rrf
        variants["rrf_cross_encoder"][query_id] = reranked

    # The sealed labels are deliberately opened only after every prediction is
    # complete.  No qrel field is available to the ranking functions above.
    qrels = sealed_freeze.get("qrels")
    if not isinstance(qrels, list) or not qrels:
        raise ValueError("sealed freeze lacks qrels")
    seen: set[tuple[str, int]] = set()
    for qrel in qrels:
        query_id = str(qrel.get("queryId") or "")
        product_id = qrel.get("productId")
        relevance = qrel.get("relevance")
        identity = (query_id, int(product_id)) if type(product_id) is int else None
        if (
            query_id not in expected_query_ids
            or identity is None
            or identity in seen
            or product_id not in docs_by_id
            or type(relevance) is not int
            or relevance not in (0, 1, 2, 3)
            or qrel.get("split") != "sealed_test"
            or qrel.get("labelSource") != "two_human_review_and_adjudication"
        ):
            raise ValueError("invalid adjudicated sealed qrel")
        seen.add(identity)
    if {query_id for query_id, _product_id in seen} != set(expected_query_ids):
        raise ValueError("sealed qrels do not cover every sealed query")

    report_variants = {}
    for name, rankings in variants.items():
        metrics = _metrics(rankings, qrels, rows_by_query, docs_by_id)
        metric_errors = metrics.pop("errorTaxonomy")
        observed, assessable = 0, 0
        for row in static_rows:
            constraints = row.get("intent", {}).get("hardConstraints", [])
            for product_id in rankings[row["queryId"]][:3]:
                if constraints:
                    assessable += 1
                    observed += int(not _hard_matches(docs_by_id[product_id], row))
        report_variants[name] = {
            **metrics,
            "hardConstraintViolationRate": (
                observed / assessable if assessable else None
            ),
            "hardConstraintViolationDenominator": assessable,
            "errorTaxonomy": metric_errors,
            "queryLatencyMs": {
                "mean": sum(latency[name]) / len(latency[name]),
                "max": max(latency[name]),
            },
        }
    summarized_stage_latency = {
        name: {"mean": sum(values) / len(values), "max": max(values)}
        for name, values in stage_latency.items()
    }
    return {
        "schemaVersion": "used-phone-human-qrel-sealed-evaluation-v1",
        "artifact": "adjudicated-sealed-one-shot-offline-baselines-v1",
        "status": "SEALED_ONE_SHOT_OFFLINE_EVALUATION_COMPLETE_HOLD",
        "productionReleaseAllowed": False,
        "onlineConfigurationChanged": False,
        "queryIds": expected_query_ids,
        "rankingInputFields": ["queryId", "retrievalQuery"],
        "rankingInputContainsHumanJudgments": False,
        "labelsOpenedAfterAllPredictions": True,
        "labelAccessContract": (
            "all four variants complete rankings from label-free projections "
            "before adjudicated sealed qrels are opened"
        ),
        "sealedSourceBindings": sealed_freeze["sourceBindings"],
        "unknownJudgmentCountExcludedNotNegative": sealed_freeze[
            "unknownJudgmentCountExcludedNotNegative"
        ],
        "buildLatencyMs": {
            "titleVector": build_ms,
            "crossEncoder": "loaded_before_run",
        },
        "stageLatencyMs": summarized_stage_latency,
        "queryLatencyContract": (
            "end-to-end per variant; RRF includes both recalls plus fusion; "
            "Cross-Encoder includes recall, fusion, and rerank"
        ),
        "variants": report_variants,
    }


def run_nonsealed_rrf_hard_gate_baseline(
    *,
    review_rows: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    candidate_depth: int = 50,
) -> dict[str, Any]:
    """Compare raw hybrid RRF with the production-style deterministic hard gate."""
    validate_review_rows(review_rows)
    if candidate_depth != 50:
        raise ValueError("the preregistered hard-gate candidate depth is 50")
    static_rows = sorted(
        (
            row for row in review_rows
            if row["evaluationRole"] == PRIMARY and row["split"] != "sealed_test"
        ),
        key=lambda item: item["queryId"],
    )
    expected_query_ids = [
        "uphq-001", "uphq-002", "uphq-005", "uphq-007", "uphq-010",
    ]
    if [row["queryId"] for row in static_rows] != expected_query_ids:
        raise ValueError("nonsealed hard-gate baseline query identity mismatch")
    ranking_inputs = [
        {"queryId": row["queryId"], "retrievalQuery": row["retrievalQuery"]}
        for row in static_rows
    ]
    products = runtime_documents(documents)
    docs_by_id = {int(doc["product_id"]): doc for doc in documents}
    build_started = time.perf_counter()
    vector = build_vector_ranker(products)
    build_ms = (time.perf_counter() - build_started) * 1000
    raw_rankings: dict[str, list[int]] = {}
    gated_rankings: dict[str, list[int]] = {}
    raw_latency: list[float] = []
    gated_latency: list[float] = []
    gate_traces: dict[str, dict[str, Any]] = {}
    stage_latency: dict[str, list[float]] = {
        "bm25": [], "title_vector": [], "rrf_fusion": [], "hard_gate": [],
    }
    rows_by_query = {row["queryId"]: row for row in static_rows}
    for ranking_input in ranking_inputs:
        query_id = ranking_input["queryId"]
        query = ranking_input["retrievalQuery"]
        started = time.perf_counter()
        bm25 = bm25_rank(query, products)
        bm25_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        vec = vector(query, products)
        vector_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        rrf = [item for item, _score in reciprocal_rank_fusion([bm25, vec], k=60)]
        fusion_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        gated, trace = apply_deterministic_hard_gate(
            rrf,
            rows_by_query[query_id],
            docs_by_id,
            candidate_depth=candidate_depth,
        )
        gate_ms = (time.perf_counter() - started) * 1000
        raw_rankings[query_id] = rrf
        gated_rankings[query_id] = gated
        gate_traces[query_id] = trace
        raw_latency.append(bm25_ms + vector_ms + fusion_ms)
        gated_latency.append(bm25_ms + vector_ms + fusion_ms + gate_ms)
        stage_latency["bm25"].append(bm25_ms)
        stage_latency["title_vector"].append(vector_ms)
        stage_latency["rrf_fusion"].append(fusion_ms)
        stage_latency["hard_gate"].append(gate_ms)

    # Human judgments remain unavailable to both ranking variants above.  They
    # are opened only now for metric calculation.
    freeze = freeze_role_aware_human_qrels(review_rows)
    qrels = freeze["qrels"]

    def constraint_metrics(rankings: dict[str, list[int]]) -> dict[str, Any]:
        explicit_failures = 0
        unknowns = 0
        denominator = 0
        for row in static_rows:
            constraints = list(row.get("intent", {}).get("hardConstraints", []))
            if not constraints:
                continue
            for product_id in rankings[row["queryId"]][:3]:
                denominator += 1
                statuses = [
                    hard_constraint_status(docs_by_id[product_id], constraint)
                    for constraint in constraints
                ]
                explicit_failures += int("fail" in statuses)
                unknowns += int("fail" not in statuses and "unknown" in statuses)
        return {
            "top3ExplicitHardViolationRate": (
                explicit_failures / denominator if denominator else None
            ),
            "top3HardUnknownRate": unknowns / denominator if denominator else None,
            "top3HardConstraintDenominator": denominator,
            "top3ExplicitHardViolationCount": explicit_failures,
            "top3HardUnknownCount": unknowns,
        }

    report_variants: dict[str, dict[str, Any]] = {}
    for name, rankings, latency_values in (
        ("bm25_vector_rrf", raw_rankings, raw_latency),
        ("bm25_vector_rrf_hard_gate", gated_rankings, gated_latency),
    ):
        metrics = _metrics(rankings, qrels, rows_by_query, docs_by_id)
        report_variants[name] = {
            **{key: value for key, value in metrics.items() if key != "errorTaxonomy"},
            **constraint_metrics(rankings),
            "errorTaxonomy": metrics["errorTaxonomy"],
            "queryLatencyMs": {
                "mean": sum(latency_values) / len(latency_values),
                "max": max(latency_values),
            },
        }
    return {
        "schemaVersion": "used-phone-rrf-hard-gate-development-validation-v1",
        "artifact": "rrf-hard-gate-development-validation-v1",
        "status": "DEVELOPMENT_VALIDATION_OFFLINE_HOLD",
        "productionReleaseAllowed": False,
        "onlineConfigurationChanged": False,
        "sealedDataRead": False,
        "sealedQueryIds": ["uphq-008", "uphq-009", "uphq-012"],
        "staticMetricQueryIds": expected_query_ids,
        "developmentQueryIds": ["uphq-001", "uphq-002", "uphq-005"],
        "validationQueryIds": ["uphq-007", "uphq-010"],
        "rankingInputFields": ["queryId", "retrievalQuery"],
        "rankingInputContainsHumanJudgments": False,
        "labelsOpenedAfterAllPredictions": True,
        "candidateDepth": candidate_depth,
        "gateContract": (
            "explicit hard failures are eliminated; unknown/conflicting evidence "
            "is retained after fully confirmed candidates; no title-to-fact inference"
        ),
        "buildLatencyMs": {"titleVector": build_ms},
        "stageLatencyMs": {
            name: {"mean": sum(values) / len(values), "max": max(values)}
            for name, values in stage_latency.items()
        },
        "gateTraceByQuery": gate_traces,
        "variants": report_variants,
    }


def run_preregistered_baselines(*, review_rows: list[dict[str, Any]], documents: list[dict[str, Any]], cross_encoder: LocalCrossEncoder, include_sealed_test: bool = False) -> dict[str, Any]:
    """Run fixed variants. Cross-Encoder only sees server-recalled RRF top-50."""
    validate_review_rows(review_rows)
    static_rows = [row for row in review_rows if row["evaluationRole"] == PRIMARY and (include_sealed_test or row["split"] != "sealed_test")]
    ranking_inputs = [
        {"queryId": row["queryId"], "retrievalQuery": row["retrievalQuery"]}
        for row in static_rows
    ]
    products, docs_by_id = runtime_documents(documents), {int(doc["product_id"]): doc for doc in documents}
    build_started = time.perf_counter(); vector = build_vector_ranker(products); build_ms = (time.perf_counter() - build_started) * 1000
    variants: dict[str, dict[str, list[int]]] = {name: {} for name in ("bm25", "title_vector", "bm25_vector_rrf", "rrf_cross_encoder")}
    latency: dict[str, list[float]] = {name: [] for name in variants}
    stage_latency: dict[str, list[float]] = {
        name: [] for name in ("bm25", "title_vector", "rrf_fusion", "cross_encoder_rerank")
    }
    for row in ranking_inputs:
        query = row["retrievalQuery"]
        started = time.perf_counter(); bm25 = bm25_rank(query, products); bm25_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter(); vec = vector(query, products); vector_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter(); rrf = [item for item, _score in reciprocal_rank_fusion([bm25, vec], k=60)]; fusion_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter(); reranked = [item for item, _score in cross_encoder.rank(query, [next(product for product in products if product["id"] == item) for item in rrf[:50]])] + rrf[50:]; rerank_ms = (time.perf_counter() - started) * 1000
        stage_latency["bm25"].append(bm25_ms)
        stage_latency["title_vector"].append(vector_ms)
        stage_latency["rrf_fusion"].append(fusion_ms)
        stage_latency["cross_encoder_rerank"].append(rerank_ms)
        latency["bm25"].append(bm25_ms)
        latency["title_vector"].append(vector_ms)
        latency["bm25_vector_rrf"].append(bm25_ms + vector_ms + fusion_ms)
        latency["rrf_cross_encoder"].append(bm25_ms + vector_ms + fusion_ms + rerank_ms)
        variants["bm25"][row["queryId"]] = bm25
        variants["title_vector"][row["queryId"]] = vec
        variants["bm25_vector_rrf"][row["queryId"]] = rrf
        variants["rrf_cross_encoder"][row["queryId"]] = reranked
    # Ranking receives only queryId/retrievalQuery. Human judgments are opened
    # by the evaluation phase below and never enter any ranking function.
    freeze = freeze_role_aware_human_qrels(review_rows, include_sealed_test=include_sealed_test)
    labels = freeze["qrels"]
    rows_by_query = {row["queryId"]: row for row in static_rows}
    report_variants = {}
    for name, rankings in variants.items():
        metrics = _metrics(rankings, labels, rows_by_query, docs_by_id)
        metric_errors = metrics.pop("errorTaxonomy")
        observed, assessable = 0, 0
        for row in static_rows:
            constraints = row.get("intent", {}).get("hardConstraints", [])
            for product_id in rankings[row["queryId"]][:3]:
                if constraints:
                    assessable += 1; observed += int(not _hard_matches(docs_by_id[product_id], row))
        report_variants[name] = {**metrics, "hardConstraintViolationRate": observed / assessable if assessable else None,
            "hardConstraintViolationDenominator": assessable, "errorTaxonomy": metric_errors,
            "queryLatencyMs": {"mean": sum(latency[name]) / len(latency[name]) if latency[name] else 0, "max": max(latency[name], default=0)}}
    no_answer_row = next(row for row in review_rows if row["queryId"] == "uphq-011")
    no_answer_matches = [doc for doc in documents if _hard_matches(doc, no_answer_row)]
    summarized_stage_latency = {
        name: {
            "mean": sum(values) / len(values) if values else 0,
            "max": max(values, default=0),
        }
        for name, values in stage_latency.items()
    }
    return {"schemaVersion": PHASE_SCHEMA, "artifact": "preregistered-primary-retrieval-baselines-v1", "status": "PRELIMINARY_HOLD",
            "rankingInputFields": ["queryId", "retrievalQuery"], "rankingInputContainsHumanJudgments": False,
            "labelAccessContract": "ranking functions receive label-free projections; qrels are frozen only after predictions complete",
            "staticMetricQueryIds": [row["queryId"] for row in static_rows],
            "withheldSealedTestQueryIds": freeze["withheldSealedTestQueryIds"], "buildLatencyMs": {"titleVector": build_ms, "crossEncoder": "loaded_before_run"},
            "stageLatencyMs": summarized_stage_latency,
            "queryLatencyContract": "end-to-end per variant; RRF includes both recalls plus fusion; Cross-Encoder includes recall, fusion, and rerank",
            "crossEncoderContract": "reranks only top-50 server-side BM25+vector RRF candidates", "variants": report_variants,
            "noAnswerCorrectRejectionRate": 1.0 if not no_answer_matches else 0.0,
            "noAnswerMetricScope": "full-catalog explicit hard-constraint gate; not ordinary Hit/Recall/NDCG", "freeze": {key: value for key, value in freeze.items() if key != "qrels"}}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
