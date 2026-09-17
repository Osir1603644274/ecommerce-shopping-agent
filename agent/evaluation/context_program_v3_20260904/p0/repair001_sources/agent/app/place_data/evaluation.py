from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .catalog import PlaceCatalog


DEFAULT_EVAL_CASES_PATH = (
    Path(__file__).resolve().parents[2]
    / "place_data"
    / "eval"
    / "beijing_places_v2_cases.json"
)


def _nested_value(value: dict[str, Any], dotted_key: str) -> Any:
    current: Any = value
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def evaluate_place_catalog(
    catalog: PlaceCatalog,
    cases_path: Path | None = None,
) -> dict[str, Any]:
    path = cases_path or DEFAULT_EVAL_CASES_PATH
    payload = json.loads(path.read_text(encoding="utf-8"))
    case_results = []
    for case in payload["cases"]:
        operation = case["operation"]
        checks: list[dict[str, Any]] = []
        if operation == "search":
            result = catalog.search(**case.get("params", {}))
            actual_ids = [item.id for item in result.items]
            expected = case["expected"]
            if "total" in expected:
                checks.append({
                    "name": "total", "expected": expected["total"],
                    "actual": result.total, "passed": result.total == expected["total"],
                })
            if "topIds" in expected:
                expected_ids = expected["topIds"]
                actual_prefix = actual_ids[: len(expected_ids)]
                checks.append({
                    "name": "topIds", "expected": expected_ids,
                    "actual": actual_prefix, "passed": actual_prefix == expected_ids,
                })
            for field, expected_value in expected.get("allMatch", {}).items():
                actual_values = sorted({getattr(item, field) for item in result.items})
                checks.append({
                    "name": f"allMatch.{field}", "expected": [expected_value],
                    "actual": actual_values, "passed": actual_values == [expected_value],
                })
            actual_summary = {"total": result.total, "ids": actual_ids}
        elif operation == "get":
            record = catalog.get(case["placeId"])
            expected = case["expected"]
            found = record is not None
            checks.append({
                "name": "found", "expected": expected["found"],
                "actual": found, "passed": found == expected["found"],
            })
            detail = record.to_detail() if record else None
            if detail:
                for field, expected_value in expected.get("fields", {}).items():
                    actual_value = _nested_value(detail, field)
                    checks.append({
                        "name": f"field.{field}", "expected": expected_value,
                        "actual": actual_value, "passed": actual_value == expected_value,
                    })
            actual_summary = detail
        else:
            raise ValueError(f"unsupported evaluation operation: {operation}")

        case_results.append({
            "id": case["id"], "question": case["question"], "operation": operation,
            "passed": all(check["passed"] for check in checks),
            "checks": checks, "actual": actual_summary,
        })

    passed = sum(case["passed"] for case in case_results)
    return {
        "evaluationVersion": payload["evaluationVersion"],
        "catalogVersion": catalog.metadata["catalogVersion"],
        "totalCases": len(case_results),
        "passedCases": passed,
        "passRate": passed / len(case_results) if case_results else 0.0,
        "cases": case_results,
    }
