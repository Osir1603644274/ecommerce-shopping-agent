#!/usr/bin/env python3
"""Strict, offline validator for the independent v2 author package.

This validator does not run either SUT arm and does not create A/B mappings.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[3]
OLD_ROOT = ROOT.parent / "react_v1_architecture_24_v1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
NEUTRAL_ID = re.compile(r"^scenario-(00[1-9]|01[0-9]|02[0-4])$")
EXPECTED_IDS = [f"scenario-{index:03d}" for index in range(1, 25)]
LEAK_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\badaptive\b",
        r"\bdeterministic\b",
        r"\bnegative\b",
        r"\bzero_result\b",
        r"\bunsupported_evidence\b",
        r"\bstale_reference\b",
        r"\bfixed_v1\b",
        r"\breact_v1\b",
        r"\barm mapping\b",
        r"\bscorer\b",
        r"\bmust (?:call|clarify|answer|choose)\b",
        r"\bdo not call\b",
        r"\btool\b",
        r"自适应",
        r"确定性(?:场景|样本)",
        r"负例",
        r"零结果",
        r"不支持证据",
        r"陈旧引用",
        r"运行臂|对照臂|实验臂",
        r"评分器|预期胜方|工具路径|候选范围失效",
        r"必须(?:澄清|回答|选择)",
        r"调用.{0,8}工具|不要.{0,8}工具",
    )
]


class ValidationFailure(Exception):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationFailure(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        require(bool(line.strip()), f"{path.name}:{number}: blank line")
        value = json.loads(line)
        require(isinstance(value, dict), f"{path.name}:{number}: object required")
        records.append(value)
    return records


def canonical_manifest_hash(entries: list[tuple[str, str]]) -> str:
    payload = "".join(f"{digest}  {path}\n" for path, digest in sorted(entries))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_public_scenarios() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    records = load_jsonl(ROOT / "public" / "scenarios.jsonl")
    require(len(records) == 24, "public scenarios must contain exactly 24 records")
    ids = [record.get("scenarioId") for record in records]
    require(ids == EXPECTED_IDS, "scenario IDs must be the neutral ordered sequence scenario-001..024")
    require(all(NEUTRAL_ID.fullmatch(str(value)) for value in ids), "non-neutral scenario ID found")

    for record in records:
        require(set(record) == {"scenarioId", "turns"}, f"{record.get('scenarioId')}: public record leaks metadata")
        turns = record["turns"]
        require(isinstance(turns, list) and turns, f"{record['scenarioId']}: turns must be non-empty")
        for turn in turns:
            require(isinstance(turn, dict) and set(turn) == {"text"}, f"{record['scenarioId']}: turn must contain text only")
            text = turn["text"]
            require(isinstance(text, str) and text.strip(), f"{record['scenarioId']}: empty turn text")
            for pattern in LEAK_PATTERNS:
                require(not pattern.search(text), f"{record['scenarioId']}: forbidden public-text leakage: {pattern.pattern}")
    return records, {record["scenarioId"]: record for record in records}


def validate_runner_metadata(public_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    records = load_jsonl(ROOT / "runner_metadata.jsonl")
    require(len(records) == 24, "runner metadata must contain exactly 24 records")
    ids = [record.get("scenarioId") for record in records]
    require(ids == EXPECTED_IDS and set(ids) == set(public_by_id), "runner/public scenario IDs differ")

    classes = Counter(record.get("behaviorClass") for record in records)
    require(classes == Counter({"adaptive": 8, "deterministic": 8, "negative": 8}), f"invalid 8/8/8 split: {classes}")

    adaptive = [record for record in records if record["behaviorClass"] == "adaptive"]
    triggers = Counter(record.get("triggerContract", {}).get("adaptiveTrigger") for record in adaptive)
    require(triggers == Counter({"zero_result": 2, "unsupported_evidence": 3, "stale_reference": 3}), f"invalid trigger split: {triggers}")
    contract_ids = [record["triggerContract"].get("contractId") for record in adaptive]
    require(len(set(contract_ids)) == 8 and all(contract_ids), "adaptive triggerContract IDs must be unique")
    require(all(record.get("currentCapability") == "phone" for record in adaptive), "all adaptive scenarios must be phone")

    deterministic = [record for record in records if record["behaviorClass"] == "deterministic"]
    for record in deterministic:
        require(record.get("currentCapability") == "phone", f"{record['scenarioId']}: positive effect evidence must be phone-only")
        require(record.get("triggerContract") is None, f"{record['scenarioId']}: deterministic triggerContract must be null")
        require(record.get("expectedTrace") == {"adaptiveTrigger": None, "reactDecisionCalls": 0}, f"{record['scenarioId']}: deterministic trace contract invalid")

    negatives = [record for record in records if record["behaviorClass"] == "negative"]
    scope_categories = {"tablet", "camera", "smartwatch", "monitor", "e_reader"}
    scope_records = [record for record in negatives if record.get("category") in scope_categories]
    require({record.get("category") for record in scope_records} == scope_categories, "all five scope-negative categories are required")
    require(len(scope_records) == 5, "each scope-negative category must appear exactly once")
    for record in scope_records:
        require(record.get("currentCapability") == "scope_negative", f"{record['scenarioId']}: scope-negative capability label required")
        require(record.get("boundaryContract") == "out_of_scope_no_product_tools", f"{record['scenarioId']}: out-of-scope no-tool contract missing")

    zero_boundaries = [record for record in negatives if record.get("currentCapability") == "zero_data_boundary"]
    require({record.get("category") for record in zero_boundaries} == {"laptop", "headphones"}, "laptop/headphones must be zero-data boundaries")
    require(all(record.get("boundaryContract") == "known_empty_snapshot_safe_response" for record in zero_boundaries), "zero-data safety boundary contract missing")
    require(all(record.get("publicDataInvariantId") in {"inv-003", "inv-004"} for record in zero_boundaries), "zero-data boundary invariant missing")

    positive_effect = [record for record in records if record["behaviorClass"] in {"adaptive", "deterministic"}]
    require(all(record.get("currentCapability") == "phone" for record in positive_effect), "non-phone positive effect sample found")

    for record in records:
        projection = record.get("sutProjection")
        require(isinstance(projection, dict), f"{record['scenarioId']}: missing sutProjection")
        require(projection.get("mode") == "turn_text_only_v1", f"{record['scenarioId']}: projection mode mismatch")
        require(projection.get("include") == "turns[].text", f"{record['scenarioId']}: projection include mismatch")
        excluded = set(projection.get("exclude", []))
        require({"scenarioId", "behaviorClass", "triggerContract", "currentCapability"}.issubset(excluded), f"{record['scenarioId']}: runner fields not excluded")
    return records


def validate_invariants(metadata: list[dict[str, Any]]) -> None:
    records = load_jsonl(ROOT / "public" / "data_invariants.jsonl")
    require(len(records) == 4, "exactly four public data invariants required")
    by_id = {record.get("invariantId"): record for record in records}
    require(set(by_id) == {"inv-001", "inv-002", "inv-003", "inv-004"}, "invariant IDs mismatch")

    for invariant_id in ("inv-001", "inv-002"):
        record = by_id[invariant_id]
        require(record.get("visibility") == "public", f"{invariant_id}: visibility must be public")
        require(record.get("snapshotBinding") == "PENDING_RUNNER_BINDING", f"{invariant_id}: snapshot must await runner binding")
        require(record.get("verificationStatus") == "PENDING_RUNNER_VERIFICATION", f"{invariant_id}: must await runner verification")
        predicate = record.get("predicate", {})
        require(predicate.get("category") == "phone" and predicate.get("expectedCandidateCount") == 0, f"{invariant_id}: zero-result predicate invalid")

    expected_empty = {"inv-003": "laptop", "inv-004": "headphones"}
    for invariant_id, category in expected_empty.items():
        record = by_id[invariant_id]
        predicate = record.get("predicate", {})
        require(record.get("verificationStatus") == "PENDING_RUNNER_REVERIFICATION", f"{invariant_id}: runner reverification required")
        require(predicate.get("category") == category and predicate.get("expectedCategoryCount") == 0, f"{invariant_id}: 439 snapshot boundary invalid")

    referenced = {
        record.get("triggerContract", {}).get("publicDataInvariantId")
        for record in metadata
        if isinstance(record.get("triggerContract"), dict) and record["triggerContract"].get("adaptiveTrigger") == "zero_result"
    }
    require(referenced == {"inv-001", "inv-002"}, "zero-result scenarios must bind both pending public invariants")


def load_projection_module() -> Any:
    path = ROOT / "sut_projection.py"
    spec = importlib.util.spec_from_file_location("react_v1_v2_sut_projection", path)
    require(spec is not None and spec.loader is not None, "cannot load projection module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_projection(scenarios: list[dict[str, Any]], receipt: dict[str, Any]) -> None:
    module = load_projection_module()
    require(module.PROJECTION_MODE == "turn_text_only_v1", "projection module mode mismatch")
    for scenario in scenarios:
        expected = [turn["text"] for turn in scenario["turns"]]
        actual = module.project_sut_input(scenario)
        require(actual == expected, f"{scenario['scenarioId']}: projection is not turns[].text only")
        require(all(value not in actual for key, value in scenario.items() if key != "turns"), f"{scenario['scenarioId']}: metadata leaked into projection")
    projection_receipt = receipt.get("sutProjection", {})
    require(projection_receipt.get("mode") == "turn_text_only_v1", "receipt projection mode mismatch")
    require(projection_receipt.get("implementationPath") == "sut_projection.py", "receipt projection path mismatch")
    require(projection_receipt.get("implementationSha256") == sha256_file(ROOT / "sut_projection.py"), "projection implementation hash mismatch")


def validate_old_assets(receipt: dict[str, Any]) -> None:
    baseline = load_json(ROOT / "provenance" / "old_asset_hashes.json")
    require(baseline.get("observationMode") == "HASH_ONLY_NO_CONTENT_READ", "old asset observation mode mismatch")
    expected_files = baseline.get("files")
    require(isinstance(expected_files, list) and expected_files, "old asset baseline empty")
    expected = {entry["path"]: entry for entry in expected_files}
    actual_paths = sorted(path.relative_to(OLD_ROOT).as_posix() for path in OLD_ROOT.rglob("*") if path.is_file())
    require(actual_paths == sorted(expected), "old asset file set changed")
    entries: list[tuple[str, str]] = []
    for relative_path, entry in expected.items():
        path = OLD_ROOT / relative_path
        digest = sha256_file(path)
        require(digest == entry.get("sha256"), f"old asset changed: {relative_path}")
        require(path.stat().st_size == entry.get("bytes"), f"old asset size changed: {relative_path}")
        entries.append((relative_path, digest))
    manifest_hash = canonical_manifest_hash(entries)
    receipt_baseline = receipt.get("priorAssetHashBaseline", {})
    require(receipt_baseline.get("assetRoot") == baseline.get("assetRoot"), "receipt old asset root mismatch")
    require(receipt_baseline.get("manifestSha256") == manifest_hash, "receipt old asset manifest hash mismatch")


def validate_provenance() -> dict[str, Any]:
    receipt = load_json(ROOT / "provenance_receipt.json")
    required = {
        "receiptVersion", "createdAt", "authorRole", "gitHead", "workingTreePolicy",
        "permittedSources", "prohibitedSourcesNotRead", "sutProjection",
        "priorAssetHashBaseline", "packageContentManifestSha256", "executionState",
        "releaseDecision", "holdReasons",
    }
    require(required.issubset(receipt), f"provenance missing fields: {sorted(required - set(receipt))}")
    require(receipt["authorRole"] == "independent_scenario_author_2", "author role mismatch")
    require(receipt["gitHead"] == "f0f1be5f5f6dbd347fad6cbd214cb5fddaa44129", "frozen git HEAD mismatch")
    require(receipt["workingTreePolicy"] == "PROTECTED_DIRTY_NO_DESTRUCTIVE_GIT", "working tree policy mismatch")
    require(receipt["executionState"] == "NOT_RUN", "model/SUT execution is forbidden for author package")
    require(receipt["releaseDecision"] == "HOLD", "author package must remain HOLD")
    require(isinstance(receipt["holdReasons"], list) and receipt["holdReasons"], "HOLD reasons required")

    sources = receipt["permittedSources"]
    expected_sources = {
        "docs/REACT_V1_EXPERIMENT_AUTHOR_CONTRACT_2026-08-27.md": "bfc6bf2e0751886ca55efcb36719d571b457a524595d0ca8cbd7ed1984c9c6eb",
        "docs/ARCHITECTURE_DECISION_CLOSURE_2026-08-27.md": "dfc6d73fc5ef142910015866db7e5432a6f3d09cec39283354e8a6442d144f6c",
    }
    observed_sources = {entry.get("path"): entry.get("sha256") for entry in sources}
    require(observed_sources == expected_sources, "permitted source list/hash mismatch")
    for relative_path, digest in expected_sources.items():
        require(sha256_file(REPO_ROOT / relative_path) == digest, f"permitted source changed: {relative_path}")

    prohibited = set(receipt["prohibitedSourcesNotRead"])
    required_prohibited = {"agent/app implementation", "prior scenario body", "blind review scores", "sealed mapping", "private oracle", "arm outputs"}
    require(required_prohibited.issubset(prohibited), "prohibited-source declaration incomplete")

    content_files = sorted(
        path for path in ROOT.rglob("*")
        if path.is_file() and path.name not in {"provenance_receipt.json", "SHA256SUMS.txt"}
        and "__pycache__" not in path.parts
    )
    entries = [(path.relative_to(ROOT).as_posix(), sha256_file(path)) for path in content_files]
    require(receipt["packageContentManifestSha256"] == canonical_manifest_hash(entries), "package content manifest hash mismatch")
    require(HEX64.fullmatch(receipt["packageContentManifestSha256"]) is not None, "invalid package manifest SHA-256")
    return receipt


def validate_sha256sums() -> None:
    path = ROOT / "SHA256SUMS.txt"
    entries: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        require(match is not None, f"SHA256SUMS.txt:{number}: malformed line")
        digest, relative_path = match.groups()
        require(relative_path not in entries, f"duplicate checksum entry: {relative_path}")
        entries[relative_path] = digest
    actual_files = sorted(
        file.relative_to(ROOT).as_posix()
        for file in ROOT.rglob("*")
        if file.is_file() and file.name != "SHA256SUMS.txt" and "__pycache__" not in file.parts
    )
    require(sorted(entries) == actual_files, "SHA256SUMS file set mismatch")
    for relative_path, digest in entries.items():
        require(sha256_file(ROOT / relative_path) == digest, f"checksum mismatch: {relative_path}")


def validate_no_ab_artifacts() -> None:
    forbidden_names = re.compile(r"(?:^|[_-])(ab|mapping|sealed|oracle)(?:[_\.-]|$)", re.IGNORECASE)
    offenders = [path.relative_to(ROOT).as_posix() for path in ROOT.rglob("*") if path.is_file() and forbidden_names.search(path.name)]
    require(not offenders, f"forbidden A/B or sealed artifact present: {offenders}")


def main() -> int:
    try:
        scenarios, public_by_id = validate_public_scenarios()
        metadata = validate_runner_metadata(public_by_id)
        validate_invariants(metadata)
        receipt = validate_provenance()
        validate_projection(scenarios, receipt)
        validate_old_assets(receipt)
        validate_sha256sums()
        validate_no_ab_artifacts()
    except (ValidationFailure, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("PASS: react_v1_architecture_24_v2 strict static validation")
    print("COUNTS: adaptive=8 deterministic=8 negative=8")
    print("TRIGGERS: zero_result=2 unsupported_evidence=3 stale_reference=3")
    print("EFFECT_SCOPE: phone_only; laptop/headphones=zero_data_safety_only")
    print("EXECUTION: NOT_RUN; RELEASE: HOLD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
