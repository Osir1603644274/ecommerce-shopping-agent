"""Build the public-only v2 controlled-semantics development set.

The cases and expectations are intentionally public and may be iterated on.
They are a contract regression set, not a sealed validation/test benchmark.
The SUT receives only ``cases_public.jsonl``; the scorer separately reads
``expected_public.jsonl`` after predictions have been frozen.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "used-phone-contract-dev-v2"
CASE_SCHEMA_VERSION = "used-phone-complex-cases-v1"
EXPECTATION_SCHEMA_VERSION = "used-phone-contract-dev-expectation-v2"
CONTROLLED_GROUPS = frozenset({
    "battery_health", "battery_originality", "motherboard_repair", "os",
    "scratch_level", "screen_originality", "shell_condition",
})
ALLOWED_VALUES = {
    "battery_health": frozenset({"70_80", "80_90", "90_plus"}),
    "battery_originality": frozenset({"original", "non_original"}),
    "motherboard_repair": frozenset({"repaired", "not_repaired"}),
    "os": frozenset({"ios", "android"}),
    "scratch_level": frozenset({"none", "light", "obvious"}),
    "screen_originality": frozenset({"original", "non_original"}),
    "shell_condition": frozenset({"normal", "damaged"}),
}


def _atom(
    group: str,
    values: str | list[str],
    *,
    importance: str = "hard",
    operator: str = "IN",
) -> dict[str, Any]:
    return {
        "allowedValues": [values] if isinstance(values, str) else values,
        "group": group,
        "importance": importance,
        "operator": operator,
    }


def _case(
    number: int,
    family: str,
    turns: str | tuple[str, str],
    *,
    hard: Iterable[dict[str, Any]] = (),
    soft: Iterable[dict[str, Any]] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    texts = (turns,) if isinstance(turns, str) else turns
    case_id = f"UPV2-CD-{number:02d}"
    case = {
        "behaviorFamily": family,
        "caseId": case_id,
        "lengthClass": "multi_turn" if len(texts) == 2 else "short",
        "schemaVersion": CASE_SCHEMA_VERSION,
        "split": "dev",
        "turns": [
            {"role": "user", "text": text, "turnId": f"{case_id}-T{index}"}
            for index, text in enumerate(texts, 1)
        ],
        "userVisibleCandidateContext": [],
    }
    expectation = {
        "caseId": case_id,
        "expectedAction": (
            "UPDATE_STATE_THEN_RETRIEVE"
            if len(texts) == 2 else "RETRIEVE_FILTER_AND_RANK"
        ),
        "expectedState": {"hard": list(hard), "soft": list(soft)},
        "schemaVersion": EXPECTATION_SCHEMA_VERSION,
    }
    return case, expectation


def definitions() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    h, s = "hard", "soft"
    return [
        _case(1, "clause_scope", "iOS，主板不能修过。",
              hard=[_atom("os", "ios"), _atom("motherboard_repair", "not_repaired")]),
        _case(2, "clause_scope", "安卓，电池90%以上，屏幕要原装。",
              hard=[_atom("os", "android"), _atom("battery_health", "90_plus"), _atom("screen_originality", "original")]),
        _case(3, "clause_scope", "iOS、原装电池、无划痕。",
              hard=[_atom("os", "ios"), _atom("battery_originality", "original"), _atom("scratch_level", "none")]),
        _case(4, "clause_scope", "安卓；外壳正常；主板未维修。",
              hard=[_atom("os", "android"), _atom("shell_condition", "normal"), _atom("motherboard_repair", "not_repaired")]),
        _case(5, "clause_scope", "iOS并且主板没修过。",
              hard=[_atom("os", "ios"), _atom("motherboard_repair", "not_repaired")]),
        _case(6, "clause_scope", "安卓同时电池健康90%以上。",
              hard=[_atom("os", "android"), _atom("battery_health", "90_plus")]),
        _case(7, "clause_scope", "iOS还要原装屏。",
              hard=[_atom("os", "ios"), _atom("screen_originality", "original")]),
        _case(8, "clause_scope", "安卓，原装屏更好。",
              hard=[_atom("os", "android")], soft=[_atom("screen_originality", "original", importance=s)]),
        _case(9, "clause_scope", "iOS和主板没修过更好。",
              hard=[_atom("os", "ios")], soft=[_atom("motherboard_repair", "not_repaired", importance=s)]),
        _case(10, "clause_scope", "优先电池健康90%以上和主板没修过。",
              soft=[_atom("battery_health", "90_plus", importance=s), _atom("motherboard_repair", "not_repaired", importance=s)]),
        _case(11, "clause_scope", "iOS，外壳正常，轻微划痕优先。",
              hard=[_atom("os", "ios"), _atom("shell_condition", "normal")], soft=[_atom("scratch_level", "light", importance=s)]),
        _case(12, "clause_scope", "安卓，主板修过，非原装电池。",
              hard=[_atom("os", "android"), _atom("motherboard_repair", "repaired"), _atom("battery_originality", "non_original")]),
        _case(13, "negation_scope", "iOS，主板修过的不要，原装屏优先。",
              hard=[_atom("os", "ios"), _atom("motherboard_repair", "repaired", operator="NOT_IN")], soft=[_atom("screen_originality", "original", importance=s)]),
        _case(14, "negation_scope", "不要非原装屏，同时电池90%以上。",
              hard=[_atom("screen_originality", "non_original", operator="NOT_IN"), _atom("battery_health", "90_plus")]),
        _case(15, "negation_scope", "排除非原装电池，iOS。",
              hard=[_atom("battery_originality", "non_original", operator="NOT_IN"), _atom("os", "ios")]),
        _case(16, "negation_scope", "主板修过的不要，外壳正常。",
              hard=[_atom("motherboard_repair", "repaired", operator="NOT_IN"), _atom("shell_condition", "normal")]),
        _case(17, "negation_scope", "不要有划痕，同时只看安卓二手机。",
              hard=[_atom("scratch_level", ["light", "obvious"], operator="NOT_IN"), _atom("os", "android")]),
        _case(18, "negation_scope", "安卓系统的不要，主板没修过。",
              hard=[_atom("os", "android", operator="NOT_IN"), _atom("motherboard_repair", "not_repaired")]),
        _case(19, "multi_turn_delta", ("先看安卓、外壳正常的二手机。", "系统从安卓改成iOS，其他条件保留。"),
              hard=[_atom("os", "ios"), _atom("shell_condition", "normal")]),
        _case(20, "multi_turn_delta", ("先看iOS、电池健康90%以上的二手机。", "取消电池健康条件，系统条件保留。"),
              hard=[_atom("os", "ios")]),
        _case(21, "multi_turn_delta", ("先看iOS二手机，主板没修过更好。", "主板没修过改为硬条件，系统条件保留。"),
              hard=[_atom("os", "ios"), _atom("motherboard_repair", "not_repaired")]),
        _case(22, "multi_turn_delta", ("安卓二手机，原装屏优先。", "原来的偏好保留，再加原装电池。"),
              hard=[_atom("os", "android"), _atom("battery_originality", "original")], soft=[_atom("screen_originality", "original", importance=s)]),
        _case(23, "multi_turn_delta", ("iOS、电池健康90%以上，外壳正常更好。", "取消外壳偏好，其他条件保留。"),
              hard=[_atom("os", "ios"), _atom("battery_health", "90_plus")]),
        _case(24, "multi_turn_delta", ("安卓，主板没修过。", "主板没修过改为优先项，系统条件保留。"),
              hard=[_atom("os", "android")], soft=[_atom("motherboard_repair", "not_repaired", importance=s)]),
        _case(25, "multi_turn_delta", ("iOS，原装屏。", "原装屏改为优先项，系统条件保留。"),
              hard=[_atom("os", "ios")], soft=[_atom("screen_originality", "original", importance=s)]),
        _case(26, "multi_turn_delta", ("安卓二手机，电池健康90%以上更好。", "我改主意了，系统换成iOS，原来的偏好保留。"),
              hard=[_atom("os", "ios")], soft=[_atom("battery_health", "90_plus", importance=s)]),
        _case(27, "multi_turn_delta", ("iOS、外壳正常的二手机。", "原来的条件保留，再加上主板不能修过。"),
              hard=[_atom("os", "ios"), _atom("shell_condition", "normal"), _atom("motherboard_repair", "not_repaired")]),
        _case(28, "multi_turn_delta", ("iOS、主板没修过、电池健康90%以上。", "取消主板条件，其他条件保留。"),
              hard=[_atom("os", "ios"), _atom("battery_health", "90_plus")]),
        _case(29, "multi_turn_delta", ("安卓二手机，无划痕优先。", "原来的偏好保留，再加外壳正常。"),
              hard=[_atom("os", "android"), _atom("shell_condition", "normal")], soft=[_atom("scratch_level", "none", importance=s)]),
        _case(30, "multi_turn_delta", ("iOS二手机，原装屏优先。", "原装屏改为硬条件，系统条件保留。"),
              hard=[_atom("os", "ios"), _atom("screen_originality", "original")]),
    ]


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _validate_atom(atom: dict[str, Any], importance: str) -> None:
    if set(atom) != {"allowedValues", "group", "importance", "operator"}:
        raise ValueError("constraint atom shape mismatch")
    group = atom["group"]
    values = atom["allowedValues"]
    if group not in CONTROLLED_GROUPS or atom["importance"] != importance:
        raise ValueError("constraint group/importance mismatch")
    if atom["operator"] not in {"IN", "NOT_IN"}:
        raise ValueError("constraint operator mismatch")
    if not isinstance(values, list) or not values or len(values) != len(set(values)):
        raise ValueError("constraint allowedValues mismatch")
    if not set(values).issubset(ALLOWED_VALUES[group]):
        raise ValueError("constraint value outside controlled registry")


def build(output_dir: Path) -> dict[str, Any]:
    rows = definitions()
    cases = [case for case, _expected in rows]
    expectations = [expected for _case_row, expected in rows]
    expected_ids = [f"UPV2-CD-{index:02d}" for index in range(1, 31)]
    if [row["caseId"] for row in cases] != expected_ids:
        raise ValueError("case identity/order mismatch")
    if {row["split"] for row in cases} != {"dev"}:
        raise ValueError("contract dev cannot contain sealed splits")
    family_counts: dict[str, int] = {}
    for row in cases:
        family = row["behaviorFamily"]
        family_counts[family] = family_counts.get(family, 0) + 1
        if not 1 <= len(row["turns"]) <= 2:
            raise ValueError("turn count mismatch")
    if family_counts != {"clause_scope": 12, "negation_scope": 6, "multi_turn_delta": 12}:
        raise ValueError("family counts mismatch")
    for row in expectations:
        for importance in ("hard", "soft"):
            for atom in row["expectedState"][importance]:
                _validate_atom(atom, importance)

    output_dir.mkdir(parents=True, exist_ok=True)
    cases_payload = b"".join(canonical_json_bytes(row) for row in cases)
    expected_payload = b"".join(canonical_json_bytes(row) for row in expectations)
    cases_path = output_dir / "cases_public.jsonl"
    expected_path = output_dir / "expected_public.jsonl"
    cases_path.write_bytes(cases_payload)
    expected_path.write_bytes(expected_payload)
    manifest = {
        "caseCount": len(cases),
        "casesSha256": hashlib.sha256(cases_payload).hexdigest(),
        "expectationCount": len(expectations),
        "expectationsSha256": hashlib.sha256(expected_payload).hexdigest(),
        "familyCounts": family_counts,
        "sealed": False,
        "splitCounts": {"dev": len(cases)},
        "schemaVersion": SCHEMA_VERSION,
        "usage": "public contract regression only; not validation or test",
    }
    (output_dir / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    return manifest


def score_predictions(predictions_path: Path, expectations_path: Path) -> dict[str, Any]:
    predictions = {
        row["caseId"]: row
        for row in _read_jsonl(predictions_path)
    }
    expectations = _read_jsonl(expectations_path)
    expected_ids = [row["caseId"] for row in expectations]
    if not predictions or not set(predictions).issubset(expected_ids):
        raise ValueError("prediction identity mismatch")
    selected_expectations = [
        expected for expected in expectations
        if expected["caseId"] in predictions
    ]
    case_results = []
    for expected in selected_expectations:
        prediction = predictions[expected["caseId"]]
        predicted_state = prediction.get(
            "predictedState", prediction.get("predictedConstraints")
        )
        action_ok = prediction.get("predictedAction") == expected["expectedAction"]
        try:
            state_ok = _canonical_state(predicted_state) == _canonical_state(
                expected["expectedState"]
            )
        except ValueError:
            state_ok = False
        case_results.append({
            "actionOk": action_ok,
            "caseId": expected["caseId"],
            "passed": action_ok and state_ok,
            "stateOk": state_ok,
        })
    passed = sum(row["passed"] for row in case_results)
    return {
        "caseCount": len(case_results),
        "caseResults": case_results,
        "passCount": passed,
        "passRate": passed / len(case_results),
        "schemaVersion": "used-phone-contract-dev-score-v2",
    }


def _canonical_state(value: Any) -> tuple[tuple[Any, ...], ...]:
    if not isinstance(value, dict) or set(value) != {"hard", "soft"}:
        raise ValueError("constraint state shape mismatch")
    rows: list[tuple[Any, ...]] = []
    seen_groups: set[str] = set()
    for importance in ("hard", "soft"):
        atoms = value[importance]
        if not isinstance(atoms, list):
            raise ValueError("constraint bucket must be an array")
        for atom in atoms:
            if not isinstance(atom, dict):
                raise ValueError("constraint atom must be an object")
            _validate_atom(atom, importance)
            group = atom["group"]
            if group in seen_groups:
                raise ValueError("constraint group must be unique across state")
            seen_groups.add(group)
            rows.append((
                group,
                atom["operator"],
                tuple(sorted(atom["allowedValues"])),
                importance,
            ))
    return tuple(sorted(rows))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("non-object JSONL row")
            rows.append(value)
    return rows


__all__ = ["build", "canonical_json_bytes", "definitions", "score_predictions"]
