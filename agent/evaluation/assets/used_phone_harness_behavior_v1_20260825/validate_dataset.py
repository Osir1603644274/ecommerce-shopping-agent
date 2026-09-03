from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public" / "scenarios.jsonl"
PRIVATE = ROOT / "private" / "expectations.jsonl"
MANIFEST = ROOT / "manifest.json"


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        assert isinstance(value, dict), f"{path}:{line_number}: row must be object"
        rows.append(value)
    return rows


def main() -> None:
    public = read_jsonl(PUBLIC)
    private = read_jsonl(PRIVATE)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    public_ids = [row["scenarioId"] for row in public]
    private_ids = [row["scenarioId"] for row in private]
    assert len(public_ids) == len(set(public_ids)), "duplicate public scenarioId"
    assert len(private_ids) == len(set(private_ids)), "duplicate private scenarioId"
    assert set(public_ids) == set(private_ids), "public/private scenario mismatch"

    oracle_by_id = {row["scenarioId"]: row for row in private}
    turn_count = 0
    for scenario in public:
        scenario_id = scenario["scenarioId"]
        assert scenario["schemaVersion"] == "used-phone-harness-behavior-public-v1"
        turns = scenario["turns"]
        assert turns, f"{scenario_id}: turns must not be empty"
        turn_ids = [turn["turnId"] for turn in turns]
        assert len(turn_ids) == len(set(turn_ids)), f"{scenario_id}: duplicate turnId"
        assert all(turn["text"].strip() for turn in turns), f"{scenario_id}: blank text"
        if scenario["provenanceKind"] == "real_web":
            assert scenario["sourceRefs"], f"{scenario_id}: real row lacks request refs"
        oracle_turn_ids = [
            turn["turnId"] for turn in oracle_by_id[scenario_id]["turnExpectations"]
        ]
        assert turn_ids == oracle_turn_ids, f"{scenario_id}: oracle turn mismatch"
        turn_count += len(turns)

    provenance = Counter(row["provenanceKind"] for row in public)
    tiers = Counter(row["executionTier"] for row in public)
    assert len(public) == manifest["scenarioCount"]
    assert turn_count == manifest["turnCount"]
    assert dict(provenance) == manifest["provenanceCounts"]
    assert dict(tiers) == manifest["executionTierCounts"]

    print(json.dumps({
        "status": "ok",
        "scenarios": len(public),
        "turns": turn_count,
        "provenance": dict(provenance),
        "executionTiers": dict(tiers),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
