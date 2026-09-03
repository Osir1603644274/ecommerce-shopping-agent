"""Build a pinned, evaluation-only used-phone attribute observation catalog.

The only data input accepted by this module is ``evidence_products_audit.jsonl``.
Facts come exclusively from exact comma-delimited ``relevance.attr_value``
tokens interpreted by the production used-phone controlled-alias ruleset.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from app.domains.ecommerce.used_phone_attributes import (
    USED_PHONE_ATTRIBUTE_REGISTRY_V1 as USED_PHONE_ATTRIBUTE_REGISTRY,
    USED_PHONE_ATTRIBUTE_RULESET_VERSION_V1 as USED_PHONE_ATTRIBUTE_RULESET_VERSION,
    observe_used_phone_attributes_v1 as observe_used_phone_attributes,
    used_phone_attribute_ruleset_payload_v1 as used_phone_attribute_ruleset_payload,
    used_phone_attribute_ruleset_sha256_v1 as used_phone_attribute_ruleset_sha256,
)


SCHEMA_VERSION = "used-phone-attribute-contract-v1"
AUDIT_SCHEMA_VERSION = "used-phone-attribute-contract-audit-v1"
MANIFEST_SCHEMA_VERSION = "used-phone-attribute-contract-manifest-v1"
DATASET_ID = "benchen4395/KuaiSearch"
DATASET_REVISION = "09807c773ce67360ed8df30842e372182fcf7ad9"
TARGET_CATEGORY_KEY = "46/133/185"
TARGET_CATEGORY_PATH = ("二手", "二手手机通讯", "二手手机")
EXPECTED_ITEM_COUNT = 252
INPUT_BASENAME = "evidence_products_audit.jsonl"
PINNED_INPUT_SHA256 = (
    "9e8f9e78ab6a630985dbfb3b202bc06ab010120cfc9bce3b61c042750db669b3"
)
SEMANTIC_STATUS = "controlled_interpretation_not_source_ground_truth"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_ZERO_SHA256 = "0" * 64


class AttributeContractError(ValueError):
    """Raised when the pinned input or output contract is violated."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_line(value: Any) -> bytes:
    return (_canonical_json(value) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_token(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _raw_tokens(raw_value: str) -> tuple[tuple[str, str], ...]:
    tokens: list[tuple[str, str]] = []
    for raw_token in re.split(r"[,，]", raw_value):
        stripped = raw_token.strip()
        normalized = _normalized_token(stripped)
        if normalized:
            tokens.append((normalized, stripped))
    return tuple(tokens)


def _validated_ref(value: Any, *, input_line_number: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AttributeContractError(
            f"evidence ref must be an object at input line {input_line_number}"
        )
    required = {"source", "field", "lineNumber", "rawValue"}
    if not required.issubset(value):
        missing = sorted(required - set(value))
        raise AttributeContractError(
            f"evidence ref missing {missing!r} at input line {input_line_number}"
        )
    if value["source"] != "relevance" or value["field"] != "attr_value":
        raise AttributeContractError(
            f"only relevance.attr_value evidence is allowed at input line {input_line_number}"
        )
    if not isinstance(value["lineNumber"], int) or value["lineNumber"] < 1:
        raise AttributeContractError(
            f"invalid relevance lineNumber at input line {input_line_number}"
        )
    if not isinstance(value["rawValue"], str):
        raise AttributeContractError(
            f"rawValue must be a string at input line {input_line_number}"
        )
    return {
        "field": "attr_value",
        "lineNumber": value["lineNumber"],
        "rawValue": value["rawValue"],
        "source": "relevance",
    }


def _ref_identity(ref: Mapping[str, Any]) -> str:
    return _canonical_json(dict(ref))


def _controlled_aliases() -> frozenset[str]:
    payload = used_phone_attribute_ruleset_payload()
    fields = payload["fields"]
    assert isinstance(fields, dict)
    return frozenset(
        _normalized_token(alias)
        for field in fields.values()
        for alias in field["aliases"]
    )


def _observations_for_product(
    row: Mapping[str, Any], *, input_line_number: int
) -> tuple[dict[str, Any], dict[str, int]]:
    refs_value = row.get("evidenceRefs")
    if not isinstance(refs_value, list) or not refs_value:
        raise AttributeContractError(
            f"target product requires evidenceRefs at input line {input_line_number}"
        )
    source_refs = [
        _validated_ref(value, input_line_number=input_line_number)
        for value in refs_value
    ]
    unique_by_identity = {_ref_identity(ref): ref for ref in source_refs}
    unique_refs = [unique_by_identity[key] for key in sorted(unique_by_identity)]

    raw_token_refs: dict[str, list[tuple[dict[str, Any], str]]] = {}
    for ref in unique_refs:
        for normalized, raw_token in _raw_tokens(ref["rawValue"]):
            raw_token_refs.setdefault(normalized, []).append((ref, raw_token))

    normalized_values = row.get("normalizedAttrValues")
    if not isinstance(normalized_values, list) or not all(
        isinstance(value, str) for value in normalized_values
    ):
        raise AttributeContractError(
            f"normalizedAttrValues must be a string array at input line {input_line_number}"
        )
    catalog_controlled = {
        _normalized_token(value)
        for value in normalized_values
        if _normalized_token(value) in _controlled_aliases()
    }
    raw_controlled = set(raw_token_refs).intersection(_controlled_aliases())
    if catalog_controlled != raw_controlled:
        raise AttributeContractError(
            "controlled normalizedAttrValues lack matching exact raw evidence ref "
            f"at input line {input_line_number}: catalog={sorted(catalog_controlled)!r}, "
            f"raw={sorted(raw_controlled)!r}"
        )

    joined_raw_values = ",".join(ref["rawValue"] for ref in unique_refs)
    observed = observe_used_phone_attributes(joined_raw_values)
    attributes: dict[str, Any] = {}
    for key in sorted(USED_PHONE_ATTRIBUTE_REGISTRY):
        observation = observed[key]
        matched_normalized = {
            _normalized_token(token) for token in observation.matched_raw_tokens
        }
        evidence_refs = []
        matched_tokens = set()
        for ref in unique_refs:
            tokens_in_ref = [
                raw_token
                for normalized, raw_token in _raw_tokens(ref["rawValue"])
                if normalized in matched_normalized
            ]
            if tokens_in_ref:
                matched_tokens.update(tokens_in_ref)
                evidence_refs.append(
                    {
                        **ref,
                        "matchedRawTokens": sorted(
                            set(tokens_in_ref), key=lambda value: (value.casefold(), value)
                        ),
                    }
                )
        if observation.status != "unknown" and not evidence_refs:
            raise AttributeContractError(
                f"{key} observation has no exact supporting evidence ref at input line "
                f"{input_line_number}"
            )
        fact = None
        if observation.fact is not None:
            fact = {
                "key": observation.fact.key,
                "type": observation.fact.type,
                "unit": observation.fact.unit,
                "value": observation.fact.value,
            }
        attributes[key] = {
            "evidenceRefs": evidence_refs,
            "fact": fact,
            "humanConfirmed": False,
            "key": key,
            "matchedRawTokens": sorted(
                matched_tokens, key=lambda value: (value.casefold(), value)
            ),
            "semanticStatus": SEMANTIC_STATUS,
            "status": observation.status,
        }

    raw_value_counts = Counter(ref["rawValue"] for ref in unique_refs)
    ref_stats = {
        "duplicateRefCount": len(source_refs) - len(unique_refs),
        "duplicateRawValueRefCount": sum(count - 1 for count in raw_value_counts.values()),
        "sourceRefCount": len(source_refs),
        "uniqueRefCount": len(unique_refs),
    }
    return attributes, ref_stats


def _catalog_record(
    row: Mapping[str, Any], *, input_line_number: int
) -> tuple[dict[str, Any], dict[str, int]]:
    for field in ("itemId", "title", "brand", "seller", "categoryKey", "categoryPath"):
        if field not in row:
            raise AttributeContractError(
                f"missing {field!r} at input line {input_line_number}"
            )
    if row["categoryKey"] != TARGET_CATEGORY_KEY:
        raise AttributeContractError("internal category filtering error")
    if tuple(row["categoryPath"]) != TARGET_CATEGORY_PATH:
        raise AttributeContractError(
            f"unexpected categoryPath at input line {input_line_number}"
        )
    item_id = str(row["itemId"])
    if not item_id:
        raise AttributeContractError(f"empty itemId at input line {input_line_number}")
    attributes, ref_stats = _observations_for_product(
        row, input_line_number=input_line_number
    )
    record = {
        "attributes": attributes,
        "brand": str(row["brand"]),
        "categoryKey": TARGET_CATEGORY_KEY,
        "categoryPath": list(TARGET_CATEGORY_PATH),
        "datasetRevision": DATASET_REVISION,
        "itemId": item_id,
        "provenance": {
            "evidenceProductAuditLineNumber": input_line_number,
            **ref_stats,
        },
        "schemaVersion": SCHEMA_VERSION,
        "seller": str(row["seller"]),
        "title": str(row["title"]),
    }
    return record, ref_stats


def _audit_payload(
    *,
    input_sha256: str,
    input_row_count: int,
    ignored_row_count: int,
    target_count: int,
    source_ref_count: int,
    unique_ref_count: int,
    duplicate_ref_count: int,
    duplicate_raw_value_ref_count: int,
    multi_ref_product_count: int,
    attribute_counts: Mapping[str, Counter[str]],
    value_counts: Mapping[str, Counter[str]],
) -> dict[str, Any]:
    attributes = {}
    for key in sorted(USED_PHONE_ATTRIBUTE_REGISTRY):
        counts = attribute_counts[key]
        attributes[key] = {
            "conflictCount": counts["conflict"],
            "knownCount": counts["known"],
            "knownCoverage": round(counts["known"] / target_count, 6),
            "unknownCount": counts["unknown"],
            "valueDistribution": dict(sorted(value_counts[key].items())),
        }
    return {
        "attributes": attributes,
        "categoryKey": TARGET_CATEGORY_KEY,
        "categoryPath": list(TARGET_CATEGORY_PATH),
        "datasetRevision": DATASET_REVISION,
        "input": {
            "ignoredRowCount": ignored_row_count,
            "rowCount": input_row_count,
            "sha256": input_sha256,
            "targetRowCount": target_count,
        },
        "itemCount": target_count,
        "rulesetSha256": used_phone_attribute_ruleset_sha256(),
        "schemaVersion": AUDIT_SCHEMA_VERSION,
        "sourceEvidenceRefs": {
            "duplicateRawValueRefCount": duplicate_raw_value_ref_count,
            "duplicateRefCount": duplicate_ref_count,
            "multiRefProductCount": multi_ref_product_count,
            "sourceRefCount": source_ref_count,
            "uniqueRefCount": unique_ref_count,
        },
    }


def _manifest_payload(
    *,
    input_sha256: str,
    catalog_sha256: str,
    audit_sha256: str,
    item_count: int,
) -> dict[str, Any]:
    manifest = {
        "boundaries": {
            "allowedInputBasename": INPUT_BASENAME,
            "categoryScoped": True,
            "exactCommaDelimitedTokensOnly": True,
            "humanConfirmed": False,
            "modelUsed": False,
            "networkUsed": False,
            "readsOnlyEvidenceProductsAudit": True,
            "semanticStatus": SEMANTIC_STATUS,
            "titleBrandSellerUsedForFact": False,
        },
        "dataset": {
            "categoryKey": TARGET_CATEGORY_KEY,
            "categoryPath": list(TARGET_CATEGORY_PATH),
            "id": DATASET_ID,
            "revision": DATASET_REVISION,
        },
        "input": {"basename": INPUT_BASENAME, "sha256": input_sha256},
        "outputs": {
            "audit.json": {"sha256": audit_sha256},
            "catalog.jsonl": {"rowCount": item_count, "sha256": catalog_sha256},
            "manifest.json": {
                "hashConvention": (
                    "sha256_of_canonical_manifest_with_outputs.manifest.json.sha256_zeroed"
                ),
                "sha256": _ZERO_SHA256,
            },
        },
        "ruleset": {
            "payload": used_phone_attribute_ruleset_payload(),
            "sha256": used_phone_attribute_ruleset_sha256(),
            "version": USED_PHONE_ATTRIBUTE_RULESET_VERSION,
        },
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
    }
    manifest["outputs"]["manifest.json"]["sha256"] = _sha256_bytes(
        _canonical_line(manifest)
    )
    return manifest


def build_used_phone_attribute_contract(
    *,
    input_path: Path,
    output_dir: Path,
    expected_input_sha256: str,
    dataset_revision: str,
    expected_item_count: int | None = None,
) -> dict[str, Any]:
    """Build the pinned catalog, audit, and manifest without overwriting output."""

    input_path = Path(input_path)
    output_dir = Path(output_dir)
    if input_path.name != INPUT_BASENAME:
        raise AttributeContractError(f"input must be named {INPUT_BASENAME!r}")
    if not _SHA256_RE.fullmatch(expected_input_sha256):
        raise AttributeContractError("expected input SHA-256 must be 64 lowercase hex")
    if expected_input_sha256 != PINNED_INPUT_SHA256:
        raise AttributeContractError(
            "expected input SHA-256 must equal the repository-pinned source digest"
        )
    if dataset_revision != DATASET_REVISION:
        raise AttributeContractError(
            f"dataset revision must equal pinned full revision {DATASET_REVISION}"
        )
    if expected_item_count is None:
        expected_item_count = EXPECTED_ITEM_COUNT
    if expected_item_count != EXPECTED_ITEM_COUNT:
        raise AttributeContractError(
            f"expected item count must equal pinned count {EXPECTED_ITEM_COUNT}"
        )
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite output directory: {output_dir}")
    actual_input_sha256 = _sha256_file(input_path)
    if actual_input_sha256 != expected_input_sha256:
        raise AttributeContractError(
            f"input SHA-256 mismatch: expected {expected_input_sha256}, got {actual_input_sha256}"
        )

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=str(output_dir.parent))
    )
    try:
        catalog_path = stage / "catalog.jsonl"
        seen_item_ids: set[str] = set()
        relevance_line_owners: dict[int, tuple[str, str]] = {}
        input_row_count = ignored_row_count = target_count = 0
        source_ref_count = unique_ref_count = duplicate_ref_count = 0
        duplicate_raw_value_ref_count = multi_ref_product_count = 0
        attribute_counts = {
            key: Counter() for key in sorted(USED_PHONE_ATTRIBUTE_REGISTRY)
        }
        value_counts = {
            key: Counter() for key in sorted(USED_PHONE_ATTRIBUTE_REGISTRY)
        }
        with input_path.open("r", encoding="utf-8-sig", newline="") as source, catalog_path.open(
            "wb"
        ) as catalog:
            for input_line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                input_row_count += 1
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AttributeContractError(
                        f"invalid JSON at input line {input_line_number}: {exc}"
                    ) from exc
                if not isinstance(row, dict):
                    raise AttributeContractError(
                        f"input line {input_line_number} must be an object"
                    )
                if row.get("categoryKey") != TARGET_CATEGORY_KEY:
                    ignored_row_count += 1
                    continue
                item_id = str(row.get("itemId", ""))
                if item_id in seen_item_ids:
                    raise AttributeContractError(f"duplicate target itemId: {item_id!r}")
                seen_item_ids.add(item_id)
                refs = row.get("evidenceRefs")
                if not isinstance(refs, list):
                    raise AttributeContractError(
                        f"target product requires evidenceRefs at input line {input_line_number}"
                    )
                for raw_ref in refs:
                    ref = _validated_ref(raw_ref, input_line_number=input_line_number)
                    line_number = ref["lineNumber"]
                    identity = (item_id, ref["rawValue"])
                    owner = relevance_line_owners.setdefault(line_number, identity)
                    if owner != identity:
                        raise AttributeContractError(
                            "relevance lineNumber must belong to exactly one item/rawValue: "
                            f"line={line_number}, first={owner!r}, current={identity!r}"
                        )
                record, ref_stats = _catalog_record(
                    row, input_line_number=input_line_number
                )
                catalog.write(_canonical_line(record))
                target_count += 1
                source_ref_count += ref_stats["sourceRefCount"]
                unique_ref_count += ref_stats["uniqueRefCount"]
                duplicate_ref_count += ref_stats["duplicateRefCount"]
                duplicate_raw_value_ref_count += ref_stats["duplicateRawValueRefCount"]
                multi_ref_product_count += int(ref_stats["sourceRefCount"] > 1)
                for key, observation in record["attributes"].items():
                    attribute_counts[key][observation["status"]] += 1
                    if observation["fact"] is not None:
                        value_counts[key][observation["fact"]["value"]] += 1

        if target_count != expected_item_count:
            raise AttributeContractError(
                f"expected {expected_item_count} unique target items, got {target_count}"
            )
        audit = _audit_payload(
            input_sha256=actual_input_sha256,
            input_row_count=input_row_count,
            ignored_row_count=ignored_row_count,
            target_count=target_count,
            source_ref_count=source_ref_count,
            unique_ref_count=unique_ref_count,
            duplicate_ref_count=duplicate_ref_count,
            duplicate_raw_value_ref_count=duplicate_raw_value_ref_count,
            multi_ref_product_count=multi_ref_product_count,
            attribute_counts=attribute_counts,
            value_counts=value_counts,
        )
        audit_path = stage / "audit.json"
        audit_path.write_bytes(_canonical_line(audit))
        catalog_sha256 = _sha256_file(catalog_path)
        audit_sha256 = _sha256_file(audit_path)
        manifest = _manifest_payload(
            input_sha256=actual_input_sha256,
            catalog_sha256=catalog_sha256,
            audit_sha256=audit_sha256,
            item_count=target_count,
        )
        manifest_path = stage / "manifest.json"
        manifest_path.write_bytes(_canonical_line(manifest))
        os.replace(stage, output_dir)
        return {
            "audit": audit,
            "catalogSha256": catalog_sha256,
            "manifest": manifest,
            "manifestFileSha256": _sha256_file(output_dir / "manifest.json"),
        }
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


__all__ = [
    "AttributeContractError",
    "DATASET_REVISION",
    "EXPECTED_ITEM_COUNT",
    "PINNED_INPUT_SHA256",
    "TARGET_CATEGORY_KEY",
    "build_used_phone_attribute_contract",
]
