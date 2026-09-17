"""Post-hoc explicit-user-state checks; never imported by the Agent or judges."""
import argparse
import json
from pathlib import Path

from agent.app.domains.ecommerce.models import SPEC_REGISTRY, ShoppingRequirement, _evaluate
from agent.app.domains.ecommerce.used_phone_attributes import USED_PHONE_ATTRIBUTE_REGISTRY
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, file_sha, write_new

SCRIPT_SHA = "60e945655d27f36a9ffe10d5e1e39e99d458b0370a948e561440db1f91573485"


def expected(turn):
    if not 2 <= turn <= 48:
        raise ValueError("undefined_turn")
    budget = 160000 if turn < 7 else 140000 if turn < 14 else 150000 if turn < 27 else 144000 if turn < 39 else 160000
    hard = {"price_minor": ("lte", budget)}
    if turn < 9:
        hard["os"] = ("eq", "android")
    if turn >= 3:
        hard["shell_condition"] = ("eq", "normal")
        if turn < 13 or turn >= 26:
            hard["screen_originality"] = ("eq", "original")
    if 10 <= turn < 34 or turn >= 46:
        hard["battery_health"] = ("in", ["80_90", "90_plus"])
    soft = {}
    if turn >= 9:
        soft["os"] = ("eq", "android" if turn < 36 else "ios")
    if 13 <= turn < 26:
        soft["screen_originality"] = ("eq", "original")
    if turn < 20 or 38 <= turn < 43:
        soft["brand"] = ("eq", "vivo")
    return hard, soft


def normalized(value):
    return sorted(value) if isinstance(value, list) else value


def equivalent_requirement(key, operator, value, actual):
    if actual.get("operator") == operator and normalized(actual.get("value")) == normalized(value):
        return True
    spec = USED_PHONE_ATTRIBUTE_REGISTRY.get(key)
    if spec is None or operator not in {"eq", "in", "not_in"}:
        return False
    try:
        requirement = ShoppingRequirement.model_validate({"unit": "enum", **actual})
    except ValueError:
        return False
    if requirement.operator not in {"eq", "in", "not_in"}:
        return False
    for candidate in [*spec.allowed_values, None]:
        if candidate is None:
            wanted = "unknown"
        else:
            accepted = candidate == value if operator == "eq" else candidate in value if operator == "in" else candidate not in value
            wanted = "pass" if accepted else "fail"
        if _evaluate(candidate, requirement) != wanted:
            return False
    return True


def lane_view(lane, requirements, hard, soft):
    # The controlled shopping part of TaskConstraint projects only hard
    # requirements. Other rows can be user action restrictions, not filters.
    if lane == "constraints":
        return [{**r, "priority": "hard"} for r in requirements if r["key"] in SPEC_REGISTRY["phone"]], (("hard", hard),)
    return requirements, (("hard", hard), ("soft", soft))


def inspect(directory, through):
    script_path = HERE / "core_dataset48_vivo002/script.json"
    if file_sha(script_path) != SCRIPT_SHA:
        raise ValueError("unexpected_script_source")
    script = json.loads(script_path.read_text(encoding="utf-8"))
    findings, checks, hashes = [], [], {}
    for turn in range(1, through + 1):
        path = directory / f"turn-{turn:02}.json"
        row = json.loads(path.read_text(encoding="utf-8"))
        if row["query"] != script["turns"][turn - 1]["userText"] or row["turn"] != turn:
            raise ValueError("script_turn_mismatch")
        hashes[path.name] = file_sha(path)
        if turn == 1:  # Ambiguous initial model-name query; no fabricated exact-state oracle.
            continue
        hard, soft = expected(turn)
        state = row["postState"]
        lanes = {"guide": state["domainState"]["shoppingGuide"]["requirements"],
            "v2Guide": state["domainState"]["shoppingTaskStateV2"]["shoppingGuide"]["requirements"],
            "constraints": state["constraints"]}
        for lane, requirements in lanes.items():
            requirements, expected_lanes = lane_view(lane, requirements, hard, soft)
            by_key = {}
            for item in requirements:
                by_key.setdefault(item["key"], []).append(item)
            for priority, wanted in expected_lanes:
                for key, (operator, value) in wanted.items():
                    actual = by_key.get(key, [])
                    if len(actual) != 1 or actual[0].get("priority") != priority or not equivalent_requirement(key, operator, value, actual[0]):
                        findings.append({"turn": turn, "lane": lane, "code": "explicit_requirement_mismatch",
                            "key": key, "expected": [operator, value, priority], "actual": actual})
            for key, items in by_key.items():
                if key not in hard and any(r.get("priority") == "hard" for r in items):
                    findings.append({"turn": turn, "lane": lane, "code": "unrequested_hard_filter", "key": key, "actual": items})
            if "brand" not in soft and by_key.get("brand"):
                findings.append({"turn": turn, "lane": lane, "code": "withdrawn_brand_filter_retained", "actual": by_key["brand"]})
        checks.append({"turn": turn, "expectedHard": hard, "expectedSoft": soft})
    return {"status": "EXPLICIT_STATE_CHECKS_PASS_NOT_FULL_QUALITY" if not findings else "EXPLICIT_STATE_CHECKS_HOLD",
        "directory": str(directory.resolve()), "throughTurn": through, "findings": findings, "checks": checks,
        "turnHashes": hashes, "scriptSha256": SCRIPT_SHA, "diagnosticSha256": file_sha(__file__),
        "modelCalls": 0, "businessWrites": 0, "humanGold": False, "fullAnswerQualityAcceptance": False,
        "note": "Expected filters derived by the primary agent from explicit fixed user turns; read-only after execution, never injected into SUT or blind packets. Only controlled phone fields of TaskConstraint are checked; action restrictions are not product filters. Not an audit of free-text notes, all tool outputs, or complete answer quality."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--through", required=True, type=int)
    args = parser.parse_args()
    if not 2 <= args.through <= 48:
        raise ValueError("unsupported_prefix")
    value = inspect(args.directory, args.through)
    write_new(args.output, value)
    print(json.dumps({"status": value["status"], "findingCount": len(value["findings"]), "firstFindings": value["findings"][:5]}, ensure_ascii=False))
