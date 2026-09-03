"""Build the independent, synthetic, evidence-grounded KuaiSearch Track B audit.

This module never treats the source KuaiSearch query or editorial score as a
Track B label.  It joins the pinned item and relevance snapshots only to attach
opaque ``attr_value`` evidence to an exact item/category record.  The 2.79 GB
item source is processed in one streaming pass; memory is bounded by category
counters plus the much smaller relevance join-key set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator


DATASET_ID = "benchen4395/KuaiSearch"
DATASET_REVISION = "09807c773ce67360ed8df30842e372182fcf7ad9"
BENCHMARK_ID = "kuaisearch-synthetic-evidence-track-b-v01"
BUILDER_VERSION = "track-b-audit-v01"
ITEMS_EXPECTED = {
    "bytes": 2_789_868_956,
    "sha256": "5c04e031324a37636afb2f822a4d862378d7f54eada93875d686fa8f31bd2621",
}
RELEVANCE_EXPECTED = {
    "bytes": 15_827_789,
    "sha256": "b26c79a3ae6fde4aadc756c7b9eda9f6dcc6e112b725db603def868ab9f9c720",
}

AMBIGUOUS_VALUES = {
    "",
    "-",
    "--",
    "unknown",
    "其他",
    "其它",
    "通用",
    "无",
    "是",
    "否",
    "默认",
    "不详",
    "无品牌",
    "其他/other",
}
OPAQUE_CODE_RE = re.compile(r"^(?=.*\d)[a-z0-9._/-]{5,}$", re.IGNORECASE)
UNKEYED_NUMBER_RE = re.compile(
    r"^[<>≤≥~～]?\d+(?:\.\d+)?(?:[-~～—]\d+(?:\.\d+)?)?(?:%|％|cm|mm|kg|g|l|ml|寸|码|支|件)?$",
    re.IGNORECASE,
)


CONTROLLED_GROUPS: dict[str, dict[str, list[str]]] = {
    "1/39/0": {
        "sleeve_length": ["短袖", "长袖", "无袖", "五分袖", "七分袖", "九分袖"],
        "fit": ["宽松型", "修身型", "标准型"],
        "material": ["棉", "涤纶(聚酯纤维)", "聚酯纤维"],
        "collar": ["圆领", "v领", "polo领"],
    },
    "1/13/103": {
        "waist_height": ["高腰", "中腰", "低腰"],
        "leg_shape": ["直筒裤", "阔脚裤", "微喇裤", "小脚裤", "喇叭裤"],
        "fit": ["宽松", "修身"],
        "material": ["牛仔布", "棉"],
    },
    "23/40/217": {
        "closure": ["系带", "套脚", "魔术贴", "一脚蹬"],
        "heel_height": ["平底", "低跟(1-3cm)", "中跟(3-5cm)", "高跟(5-8cm)"],
        "upper_material": ["合成革", "织物/纺织品", "合成纤维", "真皮"],
        "toe_shape": ["圆头", "尖头", "方头"],
    },
}


PILOT_CATEGORIES = {
    "1/39/0": "女装/T恤/UNKNOWN",
    "1/13/103": "女装/裤子/牛仔裤装",
    "23/40/217": "女鞋/休闲鞋/休闲板鞋",
}


PILOT_QUERY_BLUEPRINTS: list[dict[str, Any]] = [
    {
        "slotId": "tb-tshirt-01-single",
        "categoryKey": "1/39/0",
        "queryType": "single_attribute",
        "queryText": "想找短袖的女款T恤，其他款式都可以。",
        "requirements": [("hard", "sleeve_length", "短袖")],
        "expectedAction": "RETRIEVE_AND_RANK",
    },
    {
        "slotId": "tb-tshirt-02-multi",
        "categoryKey": "1/39/0",
        "queryType": "multi_constraint_ranking",
        "queryText": "想找女款T恤，长袖、版型宽松，棉质的优先。",
        "requirements": [
            ("hard", "sleeve_length", "长袖"),
            ("hard", "fit", "宽松型"),
            ("soft", "material", "棉"),
        ],
        "expectedAction": "RETRIEVE_FILTER_AND_RANK",
    },
    {
        "slotId": "tb-tshirt-03-conflict",
        "categoryKey": "1/39/0",
        "queryType": "conflicting_constraints",
        "queryText": "想找一件女款T恤，袖长要同时是短袖和长袖。",
        "requirements": [
            ("hard", "sleeve_length", "短袖"),
            ("hard", "sleeve_length", "长袖"),
        ],
        "expectedAction": "CLARIFY",
    },
    {
        "slotId": "tb-jeans-01-single",
        "categoryKey": "1/13/103",
        "queryType": "single_attribute",
        "queryText": "想找中腰的女款牛仔裤。",
        "requirements": [("hard", "waist_height", "中腰")],
        "expectedAction": "RETRIEVE_AND_RANK",
    },
    {
        "slotId": "tb-jeans-02-multi",
        "categoryKey": "1/13/103",
        "queryType": "multi_constraint_ranking",
        "queryText": "想找高腰直筒的女款牛仔裤，牛仔布材质优先。",
        "requirements": [
            ("hard", "waist_height", "高腰"),
            ("hard", "leg_shape", "直筒裤"),
            ("soft", "material", "牛仔布"),
        ],
        "expectedAction": "RETRIEVE_FILTER_AND_RANK",
    },
    {
        "slotId": "tb-jeans-03-conflict",
        "categoryKey": "1/13/103",
        "queryType": "conflicting_constraints",
        "queryText": "想找女款牛仔裤，腰型要同时是高腰和低腰。",
        "requirements": [
            ("hard", "waist_height", "高腰"),
            ("hard", "waist_height", "低腰"),
        ],
        "expectedAction": "CLARIFY",
    },
    {
        "slotId": "tb-shoes-01-single",
        "categoryKey": "23/40/217",
        "queryType": "single_attribute",
        "queryText": "想找平底的女款休闲板鞋。",
        "requirements": [("hard", "heel_height", "平底")],
        "expectedAction": "RETRIEVE_AND_RANK",
    },
    {
        "slotId": "tb-shoes-02-multi",
        "categoryKey": "23/40/217",
        "queryType": "multi_constraint_ranking",
        "queryText": "想找系带的女款休闲板鞋，中跟款，合成革鞋面优先。",
        "requirements": [
            ("hard", "closure", "系带"),
            ("hard", "heel_height", "中跟(3-5cm)"),
            ("soft", "upper_material", "合成革"),
        ],
        "expectedAction": "RETRIEVE_FILTER_AND_RANK",
    },
    {
        "slotId": "tb-shoes-03-conflict",
        "categoryKey": "23/40/217",
        "queryType": "conflicting_constraints",
        "queryText": "想找女款休闲板鞋，鞋跟要同时是平底和高跟。",
        "requirements": [
            ("hard", "heel_height", "平底"),
            ("hard", "heel_height", "高跟(5-8cm)"),
        ],
        "expectedAction": "CLARIFY",
    },
]


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_text(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).strip().lower().split())


def join_key(title: Any, brand: Any, seller: Any) -> tuple[str, str, str]:
    return normalize_text(title), normalize_text(brand), normalize_text(seller)


def category_key(row: dict[str, Any]) -> str:
    return "/".join(
        str(int(row.get(field) or 0))
        for field in ("category_level1_id", "category_level2_id", "category_level3_id")
    )


def iter_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any], bytes]]:
    with path.open("rb") as stream:
        for line_number, raw in enumerate(stream, 1):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at {path}:{line_number}: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"expected object at {path}:{line_number}")
            yield line_number, row, raw


def split_attr_values(raw_value: Any) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for raw_token in str(raw_value or "").split(","):
        token = normalize_text(raw_token)
        if token and token not in seen:
            seen.add(token)
            values.append(token)
    return values


def classify_attr_value(value: str, *, brand: str = "") -> str:
    if value in AMBIGUOUS_VALUES or value == normalize_text(brand):
        return "ambiguous_or_identity"
    if OPAQUE_CODE_RE.fullmatch(value):
        return "opaque_code"
    if UNKEYED_NUMBER_RE.fullmatch(value) or re.fullmatch(r"\d{4}年(?:春|夏|秋|冬)季", value):
        return "unkeyed_numeric_or_date"
    if len(value) == 1 and not value.isalpha():
        return "ambiguous_or_identity"
    return "lexically_self_describing_candidate"


def artifact_fingerprint(path: Path, *, rows: int | None = None) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    result: dict[str, Any] = {
        "path": path.name,
        "bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }
    if rows is not None:
        result["rows"] = rows
    return result


def write_json(path: Path, value: Any) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    path.write_bytes(payload)
    return {
        "path": path.name,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def write_text(path: Path, text: str) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (text.rstrip() + "\n").encode("utf-8")
    path.write_bytes(payload)
    return {
        "path": path.name,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def copy_static_artifact(source: Path, destination: Path, relative_name: str) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = source.read_bytes()
    destination.write_bytes(payload)
    return {
        "path": relative_name,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


class JsonlWriter:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.stream = path.open("wb")
        self.digest = hashlib.sha256()
        self.rows = 0

    def write(self, row: dict[str, Any]) -> None:
        payload = (canonical_json(row) + "\n").encode("utf-8")
        self.stream.write(payload)
        self.digest.update(payload)
        self.rows += 1

    def close(self) -> dict[str, Any]:
        self.stream.close()
        return {
            "path": self.path.name,
            "rows": self.rows,
            "bytes": self.path.stat().st_size,
            "sha256": self.digest.hexdigest(),
        }


@dataclass
class RelevanceEvidence:
    line_number: int
    attr_raw: str
    attr_values: list[str]


@dataclass
class JoinedProduct:
    item_id: str
    title: str
    brand: str
    seller: str
    category_key: str
    category_names: tuple[str, str, str]
    evidence: list[RelevanceEvidence] = field(default_factory=list)

    @property
    def attr_values(self) -> list[str]:
        return sorted({value for ref in self.evidence for value in ref.attr_values})


@dataclass
class ScanResult:
    products: list[JoinedProduct]
    source_audit: dict[str, Any]
    category_audit: list[dict[str, Any]]


def _compact_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_id": str(row.get("item_id") or ""),
        "title": str(row.get("item_title") or ""),
        "brand": str(row.get("brand_name") or ""),
        "seller": str(row.get("seller_name") or ""),
        "category_key": category_key(row),
        "category_names": (
            str(row.get("category_level1_name") or "UNKNOWN"),
            str(row.get("category_level2_name") or "UNKNOWN"),
            str(row.get("category_level3_name") or "UNKNOWN"),
        ),
    }


def scan_sources(items_path: Path, relevance_path: Path, *, verify_pinned: bool = True) -> ScanResult:
    relevance_by_key: dict[tuple[str, str, str], list[RelevanceEvidence]] = defaultdict(list)
    relevance_digest = hashlib.sha256()
    relevance_rows = 0
    for line_number, row, raw in iter_jsonl(relevance_path):
        relevance_digest.update(raw)
        relevance_rows += 1
        evidence = RelevanceEvidence(
            line_number=line_number,
            attr_raw=str(row.get("attr_value") or ""),
            attr_values=split_attr_values(row.get("attr_value")),
        )
        relevance_by_key[join_key(row.get("item_title"), row.get("brand"), row.get("seller_name"))].append(
            evidence
        )

    category_counts: Counter[str] = Counter()
    category_names: dict[str, tuple[str, str, str]] = {}
    matching_items: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    items_digest = hashlib.sha256()
    item_rows = 0
    for _, row, raw in iter_jsonl(items_path):
        items_digest.update(raw)
        item_rows += 1
        key = category_key(row)
        category_counts[key] += 1
        category_names.setdefault(
            key,
            (
                str(row.get("category_level1_name") or "UNKNOWN"),
                str(row.get("category_level2_name") or "UNKNOWN"),
                str(row.get("category_level3_name") or "UNKNOWN"),
            ),
        )
        candidate_key = join_key(row.get("item_title"), row.get("brand_name"), row.get("seller_name"))
        if candidate_key in relevance_by_key:
            compact = _compact_item(row)
            matching_items[candidate_key][compact["item_id"]] = compact

    items_actual = {"bytes": items_path.stat().st_size, "sha256": items_digest.hexdigest()}
    relevance_actual = {
        "bytes": relevance_path.stat().st_size,
        "sha256": relevance_digest.hexdigest(),
    }
    if verify_pinned and items_actual != ITEMS_EXPECTED:
        raise ValueError(f"items_lite fingerprint mismatch: {items_actual}")
    if verify_pinned and relevance_actual != RELEVANCE_EXPECTED:
        raise ValueError(f"relevance fingerprint mismatch: {relevance_actual}")

    products_by_id: dict[str, JoinedProduct] = {}
    matched_relevance_rows = 0
    unmatched_relevance_rows = 0
    ambiguous_relevance_rows = 0
    for key, evidence_rows in relevance_by_key.items():
        item_matches = matching_items.get(key, {})
        if not item_matches:
            unmatched_relevance_rows += len(evidence_rows)
            continue
        if len(item_matches) != 1:
            ambiguous_relevance_rows += len(evidence_rows)
            continue
        item = next(iter(item_matches.values()))
        matched_relevance_rows += len(evidence_rows)
        product = products_by_id.get(item["item_id"])
        if product is None:
            product = JoinedProduct(
                item_id=item["item_id"],
                title=item["title"],
                brand=item["brand"],
                seller=item["seller"],
                category_key=item["category_key"],
                category_names=item["category_names"],
            )
            products_by_id[product.item_id] = product
        product.evidence.extend(evidence_rows)

    products = sorted(products_by_id.values(), key=lambda product: (product.category_key, product.item_id))
    products_by_category: dict[str, list[JoinedProduct]] = defaultdict(list)
    for product in products:
        products_by_category[product.category_key].append(product)

    category_audit: list[dict[str, Any]] = []
    for key, item_count in category_counts.items():
        evidence_products = products_by_category.get(key, [])
        token_product_counts: Counter[str] = Counter()
        raw_variants: dict[str, set[str]] = defaultdict(set)
        signatures: set[tuple[str, ...]] = set()
        products_with_attr = 0
        classification_counts: Counter[str] = Counter()
        for product in evidence_products:
            values = product.attr_values
            if values:
                products_with_attr += 1
            signatures.add(tuple(values))
            for value in values:
                token_product_counts[value] += 1
                classification_counts[classify_attr_value(value, brand=product.brand)] += 1
            for ref in product.evidence:
                for raw_token in ref.attr_raw.split(","):
                    normalized = normalize_text(raw_token)
                    if normalized:
                        raw_variants[normalized].add(raw_token.strip())
        evidence_count = len(evidence_products)
        top_values = []
        for value, count in token_product_counts.most_common(50):
            top_values.append(
                {
                    "value": value,
                    "productCount": count,
                    "coverageAmongEvidenceProducts": round(count / evidence_count, 6) if evidence_count else 0.0,
                    "classification": classify_attr_value(value),
                    "rawVariantCount": len(raw_variants[value]),
                }
            )
        names = category_names[key]
        category_audit.append(
            {
                "categoryKey": key,
                "categoryLevel1Name": names[0],
                "categoryLevel2Name": names[1],
                "categoryLevel3Name": names[2],
                "itemCount": item_count,
                "evidenceProductCount": evidence_count,
                "evidenceCoverage": round(evidence_count / item_count, 6),
                "productsWithNonemptyAttr": products_with_attr,
                "distinctBrands": len({p.brand for p in evidence_products}),
                "distinctSellers": len({p.seller for p in evidence_products}),
                "distinctTitles": len({p.title for p in evidence_products}),
                "distinctAttrSignatures": len(signatures),
                "uniqueNormalizedAttrValues": len(token_product_counts),
                "attrValueClassCounts": dict(sorted(classification_counts.items())),
                "topAttrValues": top_values,
            }
        )
    category_audit.sort(key=lambda row: (-row["evidenceProductCount"], row["categoryKey"]))

    source_audit = {
        "benchmarkId": BENCHMARK_ID,
        "benchmarkTrack": "B",
        "queryOrigin": "synthetic_evidence_grounded",
        "goldStatus": "annotation_ready_not_gold",
        "dataset": {"id": DATASET_ID, "revision": DATASET_REVISION},
        "sources": {
            "itemsLite": {**items_actual, "rows": item_rows},
            "relevance": {**relevance_actual, "rows": relevance_rows},
        },
        "join": {
            "normalizedFields": ["item_title", "brand/brand_name", "seller_name"],
            "matchedRelevanceRows": matched_relevance_rows,
            "unmatchedRelevanceRows": unmatched_relevance_rows,
            "ambiguousRelevanceRowsExcluded": ambiguous_relevance_rows,
            "uniqueEvidenceProducts": len(products),
        },
        "evidencePolicy": {
            "itemIdentityAndCategory": "items_lite only",
            "attributeEvidence": "relevance.attr_value only; comma-delimited values have no source field names",
            "excludedFromTrackBLabels": ["relevance.query", "relevance.score", "relevance.split"],
            "missingAttributeTreatment": "unknown, never automatic fail",
        },
        "boundedMemory": {
            "itemRowsRetained": "only rows whose normalized join key occurs in relevance",
            "unboundedCatalogMaterialization": False,
        },
    }
    return ScanResult(products=products, source_audit=source_audit, category_audit=category_audit)


def product_to_audit_row(product: JoinedProduct) -> dict[str, Any]:
    return {
        "itemId": product.item_id,
        "title": product.title,
        "brand": product.brand,
        "seller": product.seller,
        "categoryKey": product.category_key,
        "categoryPath": list(product.category_names),
        "normalizedAttrValues": product.attr_values,
        "evidenceRefs": [
            {
                "source": "relevance",
                "lineNumber": ref.line_number,
                "field": "attr_value",
                "rawValue": ref.attr_raw,
            }
            for ref in product.evidence
        ],
    }


def stable_hash(*parts: Any) -> str:
    return hashlib.sha256("\x1f".join(normalize_text(part) for part in parts).encode("utf-8")).hexdigest()


def search_tokens(text: str) -> list[str]:
    normalized = normalize_text(text)
    ascii_tokens = re.findall(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", normalized)
    chinese_runs = re.findall(r"[\u3400-\u9fff]+", normalized)
    tokens = list(ascii_tokens)
    for run in chinese_runs:
        tokens.extend(run if len(run) == 1 else (run[index : index + 2] for index in range(len(run) - 1)))
    return tokens


def bm25_scores(query: str, products: list[JoinedProduct]) -> dict[str, float]:
    documents = [
        search_tokens(" ".join([product.title, product.brand, *product.category_names, *product.attr_values]))
        for product in products
    ]
    document_frequencies: Counter[str] = Counter()
    term_frequencies: list[Counter[str]] = []
    for document in documents:
        counts = Counter(document)
        term_frequencies.append(counts)
        document_frequencies.update(counts)
    average_length = sum(map(len, documents)) / max(len(documents), 1)
    query_terms = set(search_tokens(query))
    scores: dict[str, float] = {}
    for product, document, counts in zip(products, documents, term_frequencies):
        score = 0.0
        for term in query_terms:
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            df = document_frequencies[term]
            inverse_document_frequency = math.log(1 + (len(documents) - df + 0.5) / (df + 0.5))
            denominator = frequency + 1.5 * (1 - 0.75 + 0.75 * len(document) / max(average_length, 1))
            score += inverse_document_frequency * frequency * 2.5 / denominator
        scores[product.item_id] = round(score, 8)
    return scores


def requirement_atoms(blueprint: dict[str, Any]) -> list[dict[str, Any]]:
    atoms = []
    for index, (importance, group, value) in enumerate(blueprint["requirements"], 1):
        atoms.append(
            {
                "atomId": f"{blueprint['slotId']}-a{index:02d}",
                "importance": importance,
                "attributeGroup": group,
                "expectedValue": value,
                "missingTreatment": "unknown",
                "sourceField": "relevance.attr_value",
            }
        )
    return atoms


def query_record(blueprint: dict[str, Any]) -> dict[str, Any]:
    query_id = "synq-" + stable_hash(BENCHMARK_ID, blueprint["slotId"], blueprint["queryText"])[:16]
    return {
        "recordType": "syntheticQuery",
        "benchmarkId": BENCHMARK_ID,
        "queryId": query_id,
        "slotId": blueprint["slotId"],
        "queryText": blueprint["queryText"],
        "queryType": blueprint["queryType"],
        "categoryKey": blueprint["categoryKey"],
        "categoryPath": PILOT_CATEGORIES[blueprint["categoryKey"]].split("/"),
        "requirementAtoms": requirement_atoms(blueprint),
        "expectedAgentAction": blueprint["expectedAction"],
        "queryOrigin": "synthetic_evidence_grounded",
        "syntheticProvenance": {
            "method": "product_first_controlled_attribute_template",
            "builderVersion": BUILDER_VERSION,
            "sourceUserLog": False,
            "llmAuthored": False,
        },
        "humanApproval": {
            "queryNaturalness": "pending",
            "semanticFidelity": "pending",
            "reviewerId": None,
            "reviewedAt": None,
            "notes": None,
        },
        "goldStatus": "not_gold_pending_human_approval",
    }


def _matching_evidence_refs(product: JoinedProduct, values: set[str]) -> list[dict[str, Any]]:
    refs = []
    for ref in product.evidence:
        matched = sorted(values.intersection(ref.attr_values))
        if matched:
            refs.append(
                {
                    "source": "relevance",
                    "lineNumber": ref.line_number,
                    "field": "attr_value",
                    "matchedValues": matched,
                    "rawValue": ref.attr_raw,
                }
            )
    return refs


def auto_judgment(query: dict[str, Any], product: JoinedProduct) -> dict[str, Any]:
    groups = CONTROLLED_GROUPS[query["categoryKey"]]
    product_values = set(product.attr_values)
    atom_results = []
    for atom in query["requirementAtoms"]:
        expected = atom["expectedValue"]
        alternatives = set(groups[atom["attributeGroup"]]) - {expected}
        if expected in product_values:
            status = "pass"
            supporting_values = {expected}
        elif alternatives.intersection(product_values):
            status = "fail"
            supporting_values = alternatives.intersection(product_values)
        else:
            status = "missing"
            supporting_values = set()
        atom_results.append(
            {
                "atomId": atom["atomId"],
                "importance": atom["importance"],
                "attributeGroup": atom["attributeGroup"],
                "expectedValue": expected,
                "status": status,
                "observedControlledValues": sorted(supporting_values),
                "evidenceRefs": _matching_evidence_refs(product, supporting_values),
            }
        )

    hard_results = [result for result in atom_results if result["importance"] == "hard"]
    soft_results = [result for result in atom_results if result["importance"] == "soft"]
    conflict = query["queryType"] == "conflicting_constraints"
    if conflict:
        eligible: bool | str = False
        relevance_grade = 1 if any(result["status"] == "pass" for result in hard_results) else 0
        grade_reason = "same-category option may support clarification; query constraints are mutually exclusive"
    elif any(result["status"] == "fail" for result in hard_results):
        eligible = False
        relevance_grade = 0
        grade_reason = "at least one hard atom has an explicit controlled-group contradiction"
    elif any(result["status"] == "missing" for result in hard_results):
        eligible = "unknown"
        relevance_grade = 1
        grade_reason = "same exact category, but at least one hard atom lacks evidence"
    else:
        eligible = True
        all_soft_pass = all(result["status"] == "pass" for result in soft_results)
        relevance_grade = 3 if all_soft_pass else 2
        grade_reason = (
            "all hard atoms and all soft preferences have explicit evidence"
            if relevance_grade == 3
            else "all hard atoms pass; at least one soft preference is missing or contradicted"
        )

    return {
        "recordType": "automaticCandidatePrelabel",
        "benchmarkId": BENCHMARK_ID,
        "queryId": query["queryId"],
        "itemId": product.item_id,
        "categoryKey": product.category_key,
        "autoPrelabel": {
            "relevanceGrade": relevance_grade,
            "eligible": eligible,
            "requirementAtoms": atom_results,
            "reason": grade_reason,
        },
        "labelProvenance": {
            "type": "deterministic_rule_suggestion",
            "builderVersion": BUILDER_VERSION,
            "usesSourceEditorialScore": False,
            "humanConfirmed": False,
        },
        "humanJudgment": {
            "relevanceGrade": "pending",
            "eligible": "pending",
            "requirementAtoms": "pending",
            "reviewerId": None,
            "reviewedAt": None,
            "notes": None,
        },
        "goldStatus": "not_gold_automatic_prelabel",
    }


def _candidate_grade_targets(query_type: str) -> dict[int, int]:
    if query_type == "single_attribute":
        return {3: 8, 1: 6, 0: 10}
    if query_type == "multi_constraint_ranking":
        return {3: 6, 2: 5, 1: 5, 0: 8}
    return {1: 12, 0: 12}


def select_candidate_pool(
    query: dict[str, Any], products: list[JoinedProduct]
) -> tuple[list[tuple[JoinedProduct, dict[str, Any]]], list[dict[str, Any]]]:
    scores = bm25_scores(query["queryText"], products)
    lexical_order = sorted(products, key=lambda product: (-scores[product.item_id], product.item_id))
    lexical_rank = {product.item_id: index for index, product in enumerate(lexical_order, 1)}

    seen_brands: set[str] = set()
    diversity_order: list[JoinedProduct] = []
    for product in sorted(products, key=lambda p: stable_hash(query["queryId"], "diversity", p.item_id)):
        brand_key = normalize_text(product.brand) or "__missing__"
        if brand_key not in seen_brands:
            seen_brands.add(brand_key)
            diversity_order.append(product)
    diversity_order.extend(
        product
        for product in sorted(products, key=lambda p: stable_hash(query["queryId"], "fallback", p.item_id))
        if product not in diversity_order
    )
    diversity_rank = {product.item_id: index for index, product in enumerate(diversity_order, 1)}

    judged = [(product, auto_judgment(query, product)) for product in products]
    selected: list[tuple[JoinedProduct, dict[str, Any]]] = []
    requested_targets = _candidate_grade_targets(query["queryType"])
    available_by_grade = Counter(
        pair[1]["autoPrelabel"]["relevanceGrade"] for pair in judged
    )
    targets = {
        grade: min(target, available_by_grade[grade]) for grade, target in requested_targets.items()
    }
    shortage = 24 - sum(targets.values())
    for grade in (0, 1, 2, 3):
        if shortage <= 0:
            break
        capacity = available_by_grade[grade] - targets.get(grade, 0)
        addition = min(shortage, max(capacity, 0))
        if addition:
            targets[grade] = targets.get(grade, 0) + addition
            shortage -= addition
    if shortage:
        raise ValueError(f"pilot gate failed for {query['queryId']}: fewer than 24 category products")
    if available_by_grade[3] and targets.get(3, 0) < 3:
        raise ValueError(f"pilot gate failed for {query['queryId']}: fewer than 3 strong positives")
    if targets.get(0, 0) < 6:
        raise ValueError(f"pilot gate failed for {query['queryId']}: fewer than 6 explicit hard negatives")
    for grade, target in targets.items():
        bucket = [pair for pair in judged if pair[1]["autoPrelabel"]["relevanceGrade"] == grade]
        bucket.sort(
            key=lambda pair: (
                min(lexical_rank[pair[0].item_id], diversity_rank[pair[0].item_id]),
                lexical_rank[pair[0].item_id],
                stable_hash(query["queryId"], "grade", pair[0].item_id),
            )
        )
        selected.extend(bucket[:target])

    provenance = []
    for product, judgment in selected:
        provenance.append(
            {
                "queryId": query["queryId"],
                "itemId": product.item_id,
                "poolingRoutes": [
                    {
                        "route": "offline_bm25_diagnostic",
                        "rankWithinExactCategory": lexical_rank[product.item_id],
                        "score": scores[product.item_id],
                    },
                    {
                        "route": "deterministic_brand_diversity",
                        "rankWithinExactCategory": diversity_rank[product.item_id],
                    },
                    {
                        "route": "automatic_evidence_grade_stratum",
                        "stratum": judgment["autoPrelabel"]["relevanceGrade"],
                    },
                ],
                "hiddenFromBlindReview": True,
                "formalPoolStatus": "incomplete_no_dense_or_hybrid_pool",
            }
        )
    selected.sort(key=lambda pair: stable_hash(query["queryId"], "blind-order", pair[0].item_id))
    return selected, provenance


def leak_check(query: dict[str, Any], products: list[JoinedProduct]) -> dict[str, Any]:
    query_text = normalize_text(query["queryText"])
    longest = 0
    matched_item_id: str | None = None
    for product in products:
        title = normalize_text(product.title)
        current = 0
        previous = [0] * (len(title) + 1)
        for left_char in query_text:
            row = [0]
            for index, right_char in enumerate(title, 1):
                value = previous[index - 1] + 1 if left_char == right_char else 0
                row.append(value)
                if value > current:
                    current = value
            previous = row
        if current > longest:
            longest = current
            matched_item_id = product.item_id
    return {
        "queryId": query["queryId"],
        "usesItemId": False,
        "usesBrandOrSeller": False,
        "longestContiguousTitleOverlapChars": longest,
        "maxOverlapItemIdInternalOnly": matched_item_id,
        "thresholdChars": 12,
        "passesMechanicalCheck": longest < 12,
        "note": "generic category/attribute phrases are allowed; this check only flags long verbatim title overlap",
    }


def build_pilot_artifacts(
    scan: ScanResult, output_dir: Path
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    products_by_category: dict[str, list[JoinedProduct]] = defaultdict(list)
    for product in scan.products:
        products_by_category[product.category_key].append(product)

    selected_category_audits = {
        row["categoryKey"]: row for row in scan.category_audit if row["categoryKey"] in PILOT_CATEGORIES
    }
    pilot_gate_checks = []
    for key in PILOT_CATEGORIES:
        audit = selected_category_audits[key]
        pilot_gate_checks.append(
            {
                "categoryKey": key,
                "evidenceProductsAtLeast150": audit["evidenceProductCount"] >= 150,
                "distinctBrandsAtLeast25": audit["distinctBrands"] >= 25,
                "distinctAttributeSignaturesAtLeast120": audit["distinctAttrSignatures"] >= 120,
            }
        )
    if not all(all(value for name, value in check.items() if name != "categoryKey") for check in pilot_gate_checks):
        raise ValueError(f"pilot category gate failed: {pilot_gate_checks}")

    query_rows: list[dict[str, Any]] = []
    prelabel_rows: list[dict[str, Any]] = []
    provenance_rows: list[dict[str, Any]] = []
    blind_query_rows: list[dict[str, Any]] = []
    blind_product_rows: list[dict[str, Any]] = []
    leak_rows: list[dict[str, Any]] = []
    pool_summaries = []

    for blueprint in PILOT_QUERY_BLUEPRINTS:
        query = query_record(blueprint)
        products = products_by_category[query["categoryKey"]]
        selected, provenance = select_candidate_pool(query, products)
        query_rows.append(query)
        provenance_rows.extend(provenance)
        leak_rows.append(leak_check(query, products))
        grade_counts: Counter[int] = Counter()
        eligible_counts: Counter[str] = Counter()
        for display_index, (product, prelabel) in enumerate(selected, 1):
            prelabel["candidateDisplayId"] = f"{query['queryId']}-c{display_index:02d}"
            prelabel["evidenceProduct"] = product_to_audit_row(product)
            prelabel_rows.append(prelabel)
            grade_counts[prelabel["autoPrelabel"]["relevanceGrade"]] += 1
            eligible_counts[str(prelabel["autoPrelabel"]["eligible"]).lower()] += 1
            blind_product_rows.append(
                {
                    "benchmarkId": BENCHMARK_ID,
                    "reviewStage": 2,
                    "gateStatus": "blocked_until_stage_1_query_approval",
                    "queryId": query["queryId"],
                    "queryText": query["queryText"],
                    "candidateDisplayId": prelabel["candidateDisplayId"],
                    "product": {
                        "itemId": product.item_id,
                        "title": product.title,
                        "brand": product.brand,
                        "seller": product.seller,
                        "categoryPath": list(product.category_names),
                        "rawAttributeEvidence": [
                            {
                                "source": "relevance",
                                "lineNumber": ref.line_number,
                                "field": "attr_value",
                                "rawValue": ref.attr_raw,
                            }
                            for ref in product.evidence
                        ],
                    },
                    "humanJudgment": {
                        "relevanceGrade": "pending",
                        "eligible": "pending",
                        "requirementAtoms": "pending",
                        "reviewerId": None,
                        "reviewedAt": None,
                        "notes": None,
                    },
                }
            )
        pool_summaries.append(
            {
                "queryId": query["queryId"],
                "candidateCount": len(selected),
                "automaticGradeCounts": dict(sorted(grade_counts.items())),
                "automaticEligibleCounts": dict(sorted(eligible_counts.items())),
            }
        )
        blind_query_rows.append(
            {
                "benchmarkId": BENCHMARK_ID,
                "reviewStage": 1,
                "queryId": query["queryId"],
                "slotId": query["slotId"],
                "categoryPath": query["categoryPath"],
                "queryType": query["queryType"],
                "proposedQuery": query["queryText"],
                "intendedRequirementSummary": [
                    {
                        "importance": atom["importance"],
                        "attributeGroup": atom["attributeGroup"],
                        "value": atom["expectedValue"],
                    }
                    for atom in query["requirementAtoms"]
                ],
                "humanApproval": {
                    "queryNaturalness": "pending",
                    "semanticFidelity": "pending",
                    "decision": "pending",
                    "suggestedRewrite": None,
                    "reviewerId": None,
                    "reviewedAt": None,
                    "notes": None,
                },
            }
        )

    artifacts: dict[str, dict[str, Any]] = {}
    for name, filename, rows in (
        ("pilotQueries", "pilot_queries.jsonl", query_rows),
        ("automaticCandidatePrelabels", "pilot_candidate_prelabels_not_gold.jsonl", prelabel_rows),
        ("candidatePoolProvenance", "candidate_pool_provenance_internal.jsonl", provenance_rows),
        ("blindQueryReview", "blind_stage_1_query_review_pending.jsonl", blind_query_rows),
        ("blindProductReview", "blind_stage_2_product_review_pending.jsonl", blind_product_rows),
    ):
        writer = JsonlWriter(output_dir / filename)
        for row in rows:
            writer.write(row)
        artifacts[name] = writer.close()

    ontology = {
        "benchmarkId": BENCHMARK_ID,
        "status": "pilot_controlled_vocabulary_not_source_field_schema",
        "warning": "KuaiSearch attr_value has values but no attribute keys; group assignment is a Track B controlled interpretation pending human semantic approval.",
        "groupsByExactCategory": CONTROLLED_GROUPS,
    }
    artifacts["controlledAttributeOntology"] = write_json(
        output_dir / "controlled_attribute_ontology_pending_approval.json", ontology
    )
    artifacts["leakageAudit"] = write_json(output_dir / "query_leakage_audit.json", leak_rows)

    readiness = {
        "benchmarkId": BENCHMARK_ID,
        "pilotStatus": "GO_annotation_ready_not_gold",
        "scaleStatus": "NO_GO",
        "pilotGateChecks": pilot_gate_checks,
        "pilot": {
            "categories": len(PILOT_CATEGORIES),
            "querySlots": len(query_rows),
            "candidateJudgments": len(prelabel_rows),
            "poolSummaries": pool_summaries,
            "humanQueryApprovalsCompleted": 0,
            "humanProductJudgmentsCompleted": 0,
        },
        "scaleBlockers": [
            "Selected exact categories have only 0.4%-0.5% evidence-product coverage of the full item category.",
            "attr_value is comma-delimited and does not expose source attribute field names.",
            "25,208 of 46,422 relevance rows do not uniquely join to an item; another 2,414 are ambiguous.",
            "Pilot pooling has offline BM25 plus deterministic diversity, but no Dense or Hybrid route.",
            "No query naturalness, semantic fidelity, or product judgment has been approved by a human.",
        ],
        "nextGate": "Complete blind stage 1 query approval before opening stage 2 product judgments.",
    }
    artifacts["readiness"] = write_json(output_dir / "go_no_go_readiness.json", readiness)
    return artifacts, readiness


def render_feasibility_report(scan: ScanResult, readiness: dict[str, Any]) -> str:
    audits = {row["categoryKey"]: row for row in scan.category_audit}
    products_by_category: dict[str, list[JoinedProduct]] = defaultdict(list)
    for product in scan.products:
        products_by_category[product.category_key].append(product)
    lines = [
        "# KuaiSearch Synthetic Evidence Track B — feasibility audit",
        "",
        f"- Benchmark ID: `{BENCHMARK_ID}` (independent Track B)",
        "- Query origin: synthetic, product-first, evidence-grounded; never a real-user-log claim",
        "- Status: annotation-ready-not-gold Pilot; expansion NO-GO",
        f"- Source items: {scan.source_audit['sources']['itemsLite']['rows']:,}",
        f"- Source relevance rows: {scan.source_audit['sources']['relevance']['rows']:,}",
        f"- Unique evidence products: {scan.source_audit['join']['uniqueEvidenceProducts']:,}",
        f"- Exact category paths audited: {len(scan.category_audit):,}",
        "",
        "## Recommended first Pilot categories",
        "",
        "| Exact category | Full items | Evidence products | Coverage | Brands | Attribute signatures | Supported Pilot types |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for key, path in PILOT_CATEGORIES.items():
        row = audits[key]
        lines.append(
            f"| `{key}` {path} | {row['itemCount']:,} | {row['evidenceProductCount']:,} | "
            f"{row['evidenceCoverage']:.3%} | {row['distinctBrands']:,} | {row['distinctAttrSignatures']:,} | "
            "single attribute; multi-constraint/ranking; conflict/clarify |"
        )
    lines.extend(
        [
            "",
            "These are closed evidence-bearing subsets. Their low full-category evidence coverage forbids extrapolation to the whole catalog.",
            "",
            "## Controlled value coverage used by the Pilot",
            "",
            "Counts below mean exact normalized value occurrence in `relevance.attr_value`, not an inferred product fact.",
            "",
            "| Exact category | Controlled group | Values (evidence-product count) |",
            "| --- | --- | --- |",
        ]
    )
    for key, groups in CONTROLLED_GROUPS.items():
        products = products_by_category[key]
        for group, values in groups.items():
            counts = [f"{value}={sum(value in product.attr_values for product in products)}" for value in values]
            lines.append(f"| `{key}` | `{group}` | {', '.join(counts)} |")
    lines.extend(
        [
            "",
            "## Noise and normalization limits",
            "",
            "- `attr_value` is a comma-delimited list without source attribute keys. Grouping values such as `短袖/长袖` into a controlled field is a Track B interpretation pending human semantic approval.",
            "- Brand tokens, generic values (`其他`, `通用`, `是/否`), opaque model codes, and unkeyed numeric/date values are audited but excluded from Pilot requirements.",
            "- Exact NFKC/lowercase/whitespace normalization is used. Semantic synonym merging is not performed automatically.",
            "- Missing evidence is always `unknown`; it is never automatic `fail`.",
            "",
            "## Supported and unsupported claims",
            "",
            "Supported only inside the closed Pilot: exact category, exact controlled attribute value, mutually exclusive value contradiction, hard-constraint eligibility, and soft-preference ranking.",
            "",
            "Not supported: game FPS, battery life, noise cancellation quality, durability, build quality, value for money, comfort, real-world performance, or any claim inferred only from title/common sense. Price is intentionally outside this Pilot.",
            "",
            "## Go / no-go",
            "",
            f"- Pilot: **{readiness['pilotStatus']}** — 3 categories, 9 slots, 216 blinded candidate work items.",
            f"- Expansion: **{readiness['scaleStatus']}** — stop after this Pilot until evidence coverage, keyed attributes, multi-retriever pooling, and human approvals improve.",
            "- Next mandatory decision: blind stage-1 human approval of Query naturalness and semantic fidelity.",
            "",
            "The complete per-category table, including zero-evidence categories, is in `exact_category_attribute_feasibility_audit.json`.",
        ]
    )
    return "\n".join(lines)


def render_readme() -> str:
    return f"""# {BENCHMARK_ID}

Independent KuaiSearch Track B artifacts. All queries are synthetic and evidence-grounded; no file in this directory is a human gold label.

## Review order

1. Review `blind_stage_1_query_review_pending.jsonl` for naturalness and semantic fidelity.
2. Do not open stage 2 until every accepted query has a recorded stage-1 decision.
3. Then review `blind_stage_2_product_review_pending.jsonl`. It intentionally hides automatic grades, pool routes, ranks, and retrieval scores.
4. Keep `pilot_candidate_prelabels_not_gold.jsonl` and `candidate_pool_provenance_internal.jsonl` away from first-pass annotators.

## Reproduce offline

From the repository's `agent` directory:

```powershell
python -m evaluation.kuaisearch_synthetic_evidence_benchmark `
  --items <pinned-items_lite-train.jsonl> `
  --relevance <pinned-relevance-train.jsonl> `
  --output ..\\data\\processed\\ecommerce\\{BENCHMARK_ID}\\{DATASET_REVISION}
```

The builder verifies byte size and SHA-256 for both pinned sources, scans the item file once with bounded memory, and requires no network access.
"""


def build_audit(items_path: Path, relevance_path: Path, output_dir: Path, *, verify_pinned: bool = True) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    scan = scan_sources(items_path, relevance_path, verify_pinned=verify_pinned)
    artifacts: dict[str, dict[str, Any]] = {}
    artifacts["sourceAudit"] = write_json(output_dir / "source_audit.json", scan.source_audit)
    artifacts["categoryAudit"] = write_json(
        output_dir / "exact_category_attribute_feasibility_audit.json", scan.category_audit
    )
    writer = JsonlWriter(output_dir / "evidence_products_audit.jsonl")
    for product in scan.products:
        writer.write(product_to_audit_row(product))
    artifacts["evidenceProductsAudit"] = writer.close()
    pilot_artifacts, readiness = build_pilot_artifacts(scan, output_dir)
    artifacts.update(pilot_artifacts)
    artifacts["feasibilityReport"] = write_text(
        output_dir / "feasibility_audit_report.md", render_feasibility_report(scan, readiness)
    )
    artifacts["reproductionReadme"] = write_text(output_dir / "README.md", render_readme())
    schema_dir = Path(__file__).resolve().parent
    for artifact_name, filename in (
        ("querySchema", "track_b_query.schema.json"),
        ("candidatePrelabelSchema", "track_b_candidate_prelabel.schema.json"),
    ):
        artifacts[artifact_name] = copy_static_artifact(
            schema_dir / filename,
            output_dir / "schemas" / filename,
            f"schemas/{filename}",
        )
    manifest = {
        "benchmarkId": BENCHMARK_ID,
        "benchmarkTrack": "B",
        "builderVersion": BUILDER_VERSION,
        "status": "annotation_ready_not_gold_scale_no_go",
        "datasetRevision": DATASET_REVISION,
        "trackAIndependence": {
            "track": "B",
            "usesIndependentBenchmarkId": True,
            "trackAArtifactsUsedAsGold": False,
            "trackAArtifactsModified": False,
        },
        "humanApprovalCounts": {
            "query": 0,
            "candidate": 0,
        },
        "goNoGo": {
            "pilot": readiness["pilotStatus"],
            "scale": readiness["scaleStatus"],
        },
        "artifacts": artifacts,
    }
    write_json(output_dir / "manifest.json", manifest)


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=Path, required=True)
    parser.add_argument("--relevance", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-pinned-verification", action="store_true")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    build_audit(
        args.items,
        args.relevance,
        args.output,
        verify_pinned=not args.skip_pinned_verification,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
