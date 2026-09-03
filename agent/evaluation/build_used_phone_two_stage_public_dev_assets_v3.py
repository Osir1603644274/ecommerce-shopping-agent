"""Build the evaluator-owned public-dev-only judgment identity for v3.

This is an explicit asset-build boundary, not part of scoring.  The builder is
allowed to read the frozen v2 D/V/T judgment source once, verifies its complete
identity, and copies the original canonical bytes for D01-D10 only.  The v3
scorer never accepts or opens that source path.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any


FULL_JUDGMENTS_SHA256 = "87a861389519f66fbd80ecde9f4e5dcc5e0fd61de8d2cc6a0a87788b6dd940df"
PUBLIC_DEV_JUDGMENTS_FILENAME = "judgments_public_dev_v3.jsonl"
PUBLIC_DEV_JUDGMENTS_SHA256 = "3f3389a12717f94da6d8dbc77c62919c707c2adc35089e8b35cab06f01eb003f"
PUBLIC_DEV_JUDGMENTS_BYTE_COUNT = 3_642_387
PUBLIC_DEV_JUDGMENT_COUNT = 2_520
PUBLIC_DEV_PREREGISTRATION_FILENAME = "public_dev_preregistration_v3.json"
PUBLIC_DEV_CASE_IDS = tuple(f"UPV2-RK-D{index:02d}" for index in range(1, 11))
ALL_CASE_IDS = tuple(
    f"UPV2-RK-{prefix}{index:02d}"
    for prefix in ("D", "V", "T")
    for index in range(1, 11)
)
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = (
    REPO_ROOT
    / ".agents"
    / "evaluation-assets"
    / "used-phone-two-stage-ranking-v3-20260813"
)
TOP10_HARD_VIOLATION_RATE_DEFINITION = {
    "denominator": "fixed_k",
    "k": 10,
    "missingRanks": "zero_violation_contribution",
    "name": "top10HardViolationRate",
}


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"refusing to overwrite frozen evaluator asset: {path.name}")
        return hashlib.sha256(payload).hexdigest()
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return hashlib.sha256(payload).hexdigest()


def _expected_case(case_id: str, split: str) -> bool:
    prefix_index = len("UPV2-RK-")
    if not case_id.startswith("UPV2-RK-") or len(case_id) <= prefix_index:
        return False
    expected_split = {
        "D": "dev", "V": "validation", "T": "test",
    }.get(case_id[prefix_index])
    return expected_split == split


def public_dev_preregistration() -> dict[str, Any]:
    return {
        "caseIds": list(PUBLIC_DEV_CASE_IDS),
        "itemCountPerCase": 252,
        "judgmentCount": PUBLIC_DEV_JUDGMENT_COUNT,
        "judgmentsFile": PUBLIC_DEV_JUDGMENTS_FILENAME,
        "judgmentsSha256": PUBLIC_DEV_JUDGMENTS_SHA256,
        "labelBoundary": "AI-designed product-grounded, not human gold",
        "protocolVersion": (
            "used-phone-public-production-agent-runner-v3-two-stage-ranking-dev"
        ),
        "schemaVersion": "used-phone-two-stage-ranking-public-dev-evaluator-v3",
        "sourceBuildAuthority": {
            "input": "frozen v2 D/V/T judgments",
            "inputSha256": FULL_JUDGMENTS_SHA256,
            "scorerMayReadInput": False,
            "selection": "original canonical rows whose caseId is D01-D10",
        },
        "split": "dev",
        "top10HardViolationRateDefinition": TOP10_HARD_VIOLATION_RATE_DEFINITION,
    }


def build_public_dev_assets(
    *, source_judgments_path: Path, output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    source_hash = hashlib.sha256()
    case_counts: Counter[str] = Counter()
    item_ids_by_case: dict[str, set[str]] = {case_id: set() for case_id in ALL_CASE_IDS}
    observed_case_order: list[str] = []
    public_dev_payload: list[bytes] = []

    with source_judgments_path.open("rb") as stream:
        for line_number, raw in enumerate(stream, start=1):
            source_hash.update(raw)
            if not raw.strip():
                raise ValueError(f"blank judgment line at {line_number}")
            if not raw.endswith(b"\n"):
                raise ValueError(f"non-LF-terminated judgment line at {line_number}")
            row = json.loads(raw)
            if not isinstance(row, dict):
                raise ValueError(f"non-object judgment line at {line_number}")
            if raw != canonical_bytes(row):
                raise ValueError(f"non-canonical judgment line at {line_number}")
            case_id = row.get("caseId")
            split = row.get("split")
            item_id = row.get("itemId")
            if (
                case_id not in item_ids_by_case
                or not isinstance(split, str)
                or not _expected_case(case_id, split)
                or not isinstance(item_id, str)
                or not item_id.isdigit()
                or item_id.startswith("0")
                or row.get("schemaVersion") != "used-phone-ranking-judgment-v2"
            ):
                raise ValueError(f"invalid frozen judgment identity at line {line_number}")
            if item_id in item_ids_by_case[case_id]:
                raise ValueError(f"duplicate item in {case_id}")
            if not observed_case_order or observed_case_order[-1] != case_id:
                if case_id in observed_case_order:
                    raise ValueError(f"non-contiguous case block: {case_id}")
                observed_case_order.append(case_id)
            item_ids_by_case[case_id].add(item_id)
            case_counts[case_id] += 1
            if case_id in PUBLIC_DEV_CASE_IDS:
                public_dev_payload.append(raw)

    if source_hash.hexdigest() != FULL_JUDGMENTS_SHA256:
        raise ValueError("frozen full judgment source SHA mismatch")
    if tuple(observed_case_order) != ALL_CASE_IDS:
        raise ValueError("frozen full judgment case order mismatch")
    if any(case_counts[case_id] != 252 for case_id in ALL_CASE_IDS):
        raise ValueError("frozen full judgment case cardinality mismatch")
    universe = item_ids_by_case[ALL_CASE_IDS[0]]
    if len(universe) != 252 or any(
        item_ids_by_case[case_id] != universe for case_id in ALL_CASE_IDS
    ):
        raise ValueError("frozen full judgment item universe mismatch")

    payload = b"".join(public_dev_payload)
    if (
        len(public_dev_payload) != PUBLIC_DEV_JUDGMENT_COUNT
        or len(payload) != PUBLIC_DEV_JUDGMENTS_BYTE_COUNT
        or hashlib.sha256(payload).hexdigest() != PUBLIC_DEV_JUDGMENTS_SHA256
    ):
        raise ValueError("derived public-dev judgment identity mismatch")
    preregistration = public_dev_preregistration()
    judgment_path = output_dir / PUBLIC_DEV_JUDGMENTS_FILENAME
    preregistration_path = output_dir / PUBLIC_DEV_PREREGISTRATION_FILENAME
    judgment_sha = _atomic_write(judgment_path, payload)
    preregistration_sha = _atomic_write(
        preregistration_path, canonical_bytes(preregistration)
    )
    return {
        "judgmentCount": len(public_dev_payload),
        "judgmentsPath": str(judgment_path.resolve()),
        "judgmentsSha256": judgment_sha,
        "preregistrationPath": str(preregistration_path.resolve()),
        "preregistrationSha256": preregistration_sha,
        "sourceSha256": source_hash.hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-judgments", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = build_public_dev_assets(
        source_judgments_path=args.source_judgments,
        output_dir=args.output_dir,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
