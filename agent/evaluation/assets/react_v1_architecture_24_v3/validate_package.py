#!/usr/bin/env python3
"""Strict static validator for react_v1_architecture_24_v3.

It loads all four package schemas. It does not run a model/SUT arm or create a
sealed or anonymous mapping.
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

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[3]
EXPECTED_IDS = [f"scenario-{number:03d}" for number in range(1, 25)]
NEUTRAL_ID = re.compile(r"^scenario-(00[1-9]|01[0-9]|02[0-4])$")
SCHEMA_FILES = {
    "public": "public_scenarios.schema.json",
    "metadata": "runner_metadata.schema.json",
    "invariants": "data_invariants.schema.json",
    "provenance": "provenance_receipt.schema.json",
}
PRICE_PATH = "data/derived/ecommerce/used_phone_catalog_expansion_kuaisearch_09807c_20260823_r3/prices.jsonl"
PRICE_SHA256 = "0296cfa77b722ec0ddfa823b093f0751adb187332faccf50353f82676fae5608"
PRICE_ROWS = 439
PRICE_FIELD = "referencePriceMinor"
PRICE_MIN_MINOR = 36100
APPROVED_STALE_REFERENTS = {
    "最开始那两个",
    "最开始的两个",
    "之前那两个",
    "原来那两个",
    "先前那两个",
}
DETERMINISTIC_FIELDS = {
    "budget",
    "brand_exclusion",
    "os",
    "battery_health",
    "battery_originality",
    "screen_originality",
    "motherboard_repair",
    "scratch_level",
    "shell_condition",
}
UNCONTROLLED_FILTERS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"全新",
        r"内存",
        r"存储",
        r"屏幕尺寸",
        r"英寸",
        r"\bNFC\b",
        r"无线充电",
        r"\bRAM\b",
        r"\bstorage\b",
        r"\bscreen size\b",
        r"\bwireless charging\b",
    )
]
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
        r"自适应|确定性(?:场景|样本)|负例|零结果|不支持证据|陈旧引用",
        r"运行臂|对照臂|实验臂|评分器|预期胜方|工具路径|候选范围失效",
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
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        require(bool(line.strip()), f"{path.name}:{line_number}: blank line")
        value = json.loads(line)
        require(isinstance(value, dict), f"{path.name}:{line_number}: object required")
        records.append(value)
    return records


def canonical_manifest_hash(entries: list[tuple[str, str]]) -> str:
    payload = "".join(f"{digest}  {path}\n" for path, digest in sorted(entries))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def schema_validate(validator: Draft202012Validator, value: Any, label: str) -> None:
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.absolute_path))
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "<root>"
        raise ValidationFailure(f"schema failure {label} at {location}: {first.message}")


def load_four_schemas() -> dict[str, Draft202012Validator]:
    schema_dir = ROOT / "schemas"
    actual = {path.name for path in schema_dir.glob("*.json")}
    require(actual == set(SCHEMA_FILES.values()), f"expected exactly four schema files, got {sorted(actual)}")
    validators: dict[str, Draft202012Validator] = {}
    for role, filename in SCHEMA_FILES.items():
        schema = load_json(schema_dir / filename)
        Draft202012Validator.check_schema(schema)
        validators[role] = Draft202012Validator(schema, format_checker=FormatChecker())
    require(len(validators) == 4, "all four schemas must be loaded")
    return validators


def validate_public(validator: Draft202012Validator) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    scenarios = load_jsonl(ROOT / "public" / "scenarios.jsonl")
    require(len(scenarios) == 24, "exactly 24 public scenarios required")
    ids = [record.get("scenarioId") for record in scenarios]
    require(ids == EXPECTED_IDS, "IDs must be neutral ordered scenario-001..scenario-024")
    require(all(NEUTRAL_ID.fullmatch(str(value)) for value in ids), "non-neutral scenario ID")
    for record in scenarios:
        schema_validate(validator, record, record["scenarioId"])
        for turn in record["turns"]:
            text = turn["text"]
            for pattern in UNCONTROLLED_FILTERS:
                require(not pattern.search(text), f"{record['scenarioId']}: uncontrolled filter leaked: {pattern.pattern}")
            for pattern in LEAK_PATTERNS:
                require(not pattern.search(text), f"{record['scenarioId']}: runner/scorer leakage: {pattern.pattern}")
    return scenarios, {record["scenarioId"]: record for record in scenarios}


def infer_deterministic_fields(text: str) -> set[str]:
    fields: set[str] = set()
    if "预算" in text:
        fields.add("budget")
    if re.search(r"(?:不要|不考虑|排除).{0,8}品牌", text):
        fields.add("brand_exclusion")
    if "安卓" in text or re.search(r"iOS", text, re.IGNORECASE):
        fields.add("os")
    if "电池健康" in text:
        fields.add("battery_health")
    if "原装电池" in text:
        fields.add("battery_originality")
    if "原装屏幕" in text:
        fields.add("screen_originality")
    if "主板" in text and "维修" in text:
        fields.add("motherboard_repair")
    if "划痕" in text:
        fields.add("scratch_level")
    if "外壳" in text:
        fields.add("shell_condition")
    return fields


def validate_expected_trace_cross_contract(record: dict[str, Any]) -> None:
    trigger = record["triggerContract"]
    trace = record["expectedTrace"]
    if trigger is None:
        require(trace == {"adaptiveTrigger": None, "reactDecisionCalls": 0}, f"{record['scenarioId']}: null trigger must cross-match null/0 expectedTrace")
        return
    require(trace["adaptiveTrigger"] == trigger["adaptiveTrigger"], f"{record['scenarioId']}: expectedTrace trigger differs from triggerContract")
    calls = trace["reactDecisionCalls"]
    require(calls == {"min": 1, "maxPerUserTurn": 2}, f"{record['scenarioId']}: adaptive decision-call contract invalid")


def validate_metadata(
    validator: Draft202012Validator,
    scenarios_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    metadata = load_jsonl(ROOT / "runner_metadata.jsonl")
    require(len(metadata) == 24, "exactly 24 metadata records required")
    ids = [record.get("scenarioId") for record in metadata]
    require(ids == EXPECTED_IDS and set(ids) == set(scenarios_by_id), "public/metadata IDs differ")
    for record in metadata:
        schema_validate(validator, record, record["scenarioId"])
        validate_expected_trace_cross_contract(record)
        projection = record["sutProjection"]
        require(projection == {"mode": "turn_text_only_v1", "include": "turns[].text", "excludeAllRunnerMetadata": True}, f"{record['scenarioId']}: invalid SUT projection declaration")

    class_counts = Counter(record["behaviorClass"] for record in metadata)
    require(class_counts == Counter({"adaptive": 8, "deterministic": 8, "negative": 8}), f"invalid 8/8/8 split: {class_counts}")

    adaptive = [record for record in metadata if record["behaviorClass"] == "adaptive"]
    trigger_counts = Counter(record["triggerContract"]["adaptiveTrigger"] for record in adaptive)
    require(trigger_counts == Counter({"zero_result": 2, "unsupported_evidence": 3, "stale_reference": 3}), f"invalid 2/3/3 split: {trigger_counts}")
    contract_ids = [record["triggerContract"]["contractId"] for record in adaptive]
    require(len(contract_ids) == len(set(contract_ids)) == 8, "adaptive triggerContract IDs must be unique")
    require(all(record["currentCapability"] == record["category"] == "phone" for record in adaptive), "adaptive scenarios must be phone-only")

    zero_records = [record for record in adaptive if record["triggerContract"]["adaptiveTrigger"] == "zero_result"]
    require({record["dataInvariantId"] for record in zero_records} == {"inv-001", "inv-002"}, "zero-result invariant binding mismatch")

    unsupported = [record for record in adaptive if record["triggerContract"]["adaptiveTrigger"] == "unsupported_evidence"]
    require(len(unsupported) == 3, "three unsupported-evidence scenarios required")
    for record in unsupported:
        turns = scenarios_by_id[record["scenarioId"]]["turns"]
        require(len(turns) >= 2, f"{record['scenarioId']}: unsupported evidence must first establish candidates")
        first = turns[0]["text"]
        followup = turns[-1]["text"]
        require("二手" in first and "手机" in first and re.search(r"两款|几款", first) is not None, f"{record['scenarioId']}: current candidates not established")
        require(re.search(r"哪款|哪个|比较|对比", followup) is not None, f"{record['scenarioId']}: explicit comparison semantics missing")
        require(re.search(r"当前|这两款|这几款|候选", followup) is not None, f"{record['scenarioId']}: current-candidate reference missing")
        require(re.search(r"游戏|帧率|散热|发热|相机|拍照", followup) is not None, f"{record['scenarioId']}: frozen unsupported-evidence topic missing")

    stale = [record for record in adaptive if record["triggerContract"]["adaptiveTrigger"] == "stale_reference"]
    require(len(stale) == 3, "three stale-reference scenarios required")
    for record in stale:
        turns = scenarios_by_id[record["scenarioId"]]["turns"]
        require(len(turns) == 3, f"{record['scenarioId']}: stale-reference scenario must have three turns")
        require("找两款" in turns[0]["text"], f"{record['scenarioId']}: first turn must retrieve two candidates")
        require("预算改成" in turns[1]["text"], f"{record['scenarioId']}: second turn must change a hard condition")
        matches = [phrase for phrase in APPROVED_STALE_REFERENTS if phrase in turns[2]["text"]]
        require(len(matches) == 1, f"{record['scenarioId']}: final turn must use one exact approved plural referent")
        require(re.search(r"刚才第[一二三]款", turns[2]["text"]) is None, f"{record['scenarioId']}: forbidden singular stale referent")

    deterministic = [record for record in metadata if record["behaviorClass"] == "deterministic"]
    for record in deterministic:
        require(record["currentCapability"] == record["category"] == "phone", f"{record['scenarioId']}: deterministic positive effect must be phone")
        require(record["triggerContract"] is None, f"{record['scenarioId']}: deterministic trigger must be null")
        declared = set(record["deterministicContractFields"] or [])
        require(declared and declared.issubset(DETERMINISTIC_FIELDS), f"{record['scenarioId']}: non-contract deterministic field")
        text = " ".join(turn["text"] for turn in scenarios_by_id[record["scenarioId"]]["turns"])
        inferred = infer_deterministic_fields(text)
        require(inferred == declared, f"{record['scenarioId']}: declared fields {sorted(declared)} != text fields {sorted(inferred)}")

    negative = [record for record in metadata if record["behaviorClass"] == "negative"]
    scope_categories = {"tablet", "camera", "smartwatch", "monitor", "e_reader"}
    scope_records = [record for record in negative if record["category"] in scope_categories]
    require(len(scope_records) == 5 and {record["category"] for record in scope_records} == scope_categories, "five scope-negative categories required exactly once")
    for record in scope_records:
        require(record["currentCapability"] == "scope_negative", f"{record['scenarioId']}: scope-negative capability missing")
        require(record["boundaryContract"] == "out_of_scope_no_product_tools", f"{record['scenarioId']}: no-product-tools boundary missing")

    zero_data = [record for record in negative if record["currentCapability"] == "zero_data_boundary"]
    require({record["category"] for record in zero_data} == {"laptop", "headphones"}, "laptop/headphones must only be zero-data safety boundaries")
    for record in zero_data:
        require(record["boundaryContract"] == "known_empty_439_snapshot_safe_response", f"{record['scenarioId']}: zero-data boundary missing")
        require(record["snapshotFact"] == {"source": "AUTHOR_CONTRACT", "expectedCategoryCount": 0}, f"{record['scenarioId']}: zero-data fact mismatch")

    positive_effect = [record for record in metadata if record["behaviorClass"] in {"adaptive", "deterministic"}]
    require(all(record["category"] == "phone" for record in positive_effect), "non-phone positive-effect scenario found")
    return metadata


def validate_invariants(
    validator: Draft202012Validator,
    metadata: list[dict[str, Any]],
    scenarios_by_id: dict[str, dict[str, Any]],
) -> None:
    invariants = load_jsonl(ROOT / "public" / "data_invariants.jsonl")
    require(len(invariants) == 2, "exactly two zero-result invariants required")
    for record in invariants:
        schema_validate(validator, record, record["invariantId"])
    by_id = {record["invariantId"]: record for record in invariants}
    require(set(by_id) == {"inv-001", "inv-002"}, "invariant IDs mismatch")

    data_path = REPO_ROOT / PRICE_PATH
    require(sha256_file(data_path) == PRICE_SHA256, "prices.jsonl SHA-256 mismatch")
    rows = load_jsonl(data_path)
    require(len(rows) == PRICE_ROWS, f"prices.jsonl row count mismatch: {len(rows)}")
    prices = [row.get(PRICE_FIELD) for row in rows]
    require(all(isinstance(value, int) and not isinstance(value, bool) for value in prices), "invalid referencePriceMinor value")
    require(min(prices) == PRICE_MIN_MINOR, f"prices.jsonl minimum mismatch: {min(prices)}")

    expected_budgets = {"inv-001": 10000, "inv-002": 20000}
    expected_scenarios = {"inv-001": "scenario-001", "inv-002": "scenario-002"}
    for invariant_id, invariant in by_id.items():
        require(invariant["dataFile"] == PRICE_PATH and invariant["fileSha256"] == PRICE_SHA256, f"{invariant_id}: snapshot binding mismatch")
        require(invariant["rowCount"] == PRICE_ROWS and invariant["minMinor"] == PRICE_MIN_MINOR, f"{invariant_id}: snapshot statistics mismatch")
        require(invariant["maxBudgetMinor"] == expected_budgets[invariant_id], f"{invariant_id}: budget mismatch")
        require(invariant["maxBudgetMinor"] < PRICE_MIN_MINOR and invariant["expectedCandidateCount"] == 0, f"{invariant_id}: zero-result implication invalid")
        require(invariant["scenarioId"] == expected_scenarios[invariant_id], f"{invariant_id}: scenario link mismatch")
        yuan = invariant["maxBudgetMinor"] // 100
        text = scenarios_by_id[invariant["scenarioId"]]["turns"][0]["text"]
        require(f"预算{yuan}元以内" in text, f"{invariant_id}: public budget does not match invariant")

    metadata_links = {
        record["dataInvariantId"]: record["scenarioId"]
        for record in metadata
        if record["dataInvariantId"] is not None
    }
    require(metadata_links == {"inv-001": "scenario-001", "inv-002": "scenario-002"}, "metadata/invariant cross-links mismatch")


def load_projection_module() -> Any:
    path = ROOT / "sut_projection.py"
    spec = importlib.util.spec_from_file_location("react_v1_architecture_24_v3_projection", path)
    require(spec is not None and spec.loader is not None, "cannot load projection implementation")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_projection(scenarios: list[dict[str, Any]], receipt: dict[str, Any]) -> None:
    module = load_projection_module()
    require(module.PROJECTION_MODE == "turn_text_only_v1", "projection implementation mode mismatch")
    for scenario in scenarios:
        expected = [turn["text"] for turn in scenario["turns"]]
        require(module.project_sut_input(scenario) == expected, f"{scenario['scenarioId']}: projection is not turns-only")
    projection = receipt["sutProjection"]
    require(projection["mode"] == "turn_text_only_v1", "receipt projection mode mismatch")
    require(projection["implementationPath"] == "sut_projection.py", "receipt projection path mismatch")
    require(projection["implementationSha256"] == sha256_file(ROOT / "sut_projection.py"), "projection SHA mismatch")


def validate_prior_assets(receipt: dict[str, Any]) -> None:
    baseline = load_json(ROOT / "provenance" / "prior_asset_hashes.json")
    require(baseline["observationMode"] == "HASH_ONLY_NO_SCENARIO_CONTENT_READ", "prior asset observation mode mismatch")
    manifest_map: dict[str, str] = {}
    for asset_set in baseline["assetSets"]:
        asset_root_text = asset_set["assetRoot"]
        asset_root = REPO_ROOT / asset_root_text
        expected = {entry["path"]: entry for entry in asset_set["files"]}
        actual_paths = sorted(path.relative_to(asset_root).as_posix() for path in asset_root.rglob("*") if path.is_file())
        require(actual_paths == sorted(expected), f"prior asset file set changed: {asset_root_text}")
        entries: list[tuple[str, str]] = []
        for relative_path, frozen in expected.items():
            path = asset_root / relative_path
            digest = sha256_file(path)
            require(digest == frozen["sha256"], f"prior asset changed: {asset_root_text}/{relative_path}")
            require(path.stat().st_size == frozen["bytes"], f"prior asset size changed: {asset_root_text}/{relative_path}")
            entries.append((relative_path, digest))
        manifest = canonical_manifest_hash(entries)
        require(manifest == asset_set["manifestSha256"], f"prior asset manifest mismatch: {asset_root_text}")
        manifest_map[asset_root_text] = manifest
    require(receipt["priorAssetHashBaseline"] == manifest_map, "receipt prior-asset manifests mismatch")


def validate_provenance(validator: Draft202012Validator) -> dict[str, Any]:
    receipt = load_json(ROOT / "provenance_receipt.json")
    schema_validate(validator, receipt, "provenance_receipt")
    require(receipt["gitHead"] == "f0f1be5f5f6dbd347fad6cbd214cb5fddaa44129", "git HEAD mismatch")
    expected_sources = {
        "docs/REACT_V1_EXPERIMENT_AUTHOR_CONTRACT_2026-08-27.md": "36c0a21ae5d3054ccb08e881c1d22a478635d63778e0e88dafb8c258ad266361",
        "docs/ARCHITECTURE_DECISION_CLOSURE_2026-08-27.md": "dfc6d73fc5ef142910015866db7e5432a6f3d09cec39283354e8a6442d144f6c",
    }
    observed_sources = {entry["path"]: entry["sha256"] for entry in receipt["permittedSources"]}
    require(observed_sources == expected_sources, "permitted-source receipt mismatch")
    for relative_path, expected_hash in expected_sources.items():
        require(sha256_file(REPO_ROOT / relative_path) == expected_hash, f"permitted source changed: {relative_path}")

    snapshot = receipt["priceSnapshot"]
    require(snapshot == {"path": PRICE_PATH, "sha256": PRICE_SHA256, "rowCount": PRICE_ROWS, "priceField": PRICE_FIELD, "minMinor": PRICE_MIN_MINOR}, "price snapshot receipt mismatch")
    prohibited = set(receipt["prohibitedSourcesNotRead"])
    required = {"agent/app implementation", "v1 scenario body", "v2 scenario body", "blind review scores", "sealed mapping", "private oracle", "arm outputs"}
    require(required.issubset(prohibited), "prohibited-source declaration incomplete")
    require(receipt["executionState"] == "NOT_RUN", "model/SUT execution must remain NOT_RUN")
    require(receipt["mappingState"] == "NOT_GENERATED", "A/B mapping must remain NOT_GENERATED")
    require(receipt["releaseDecision"] == "HOLD", "release decision must remain HOLD")

    files = sorted(
        path for path in ROOT.rglob("*")
        if path.is_file() and path.name not in {"provenance_receipt.json", "SHA256SUMS.txt"}
        and "__pycache__" not in path.parts
    )
    entries = [(path.relative_to(ROOT).as_posix(), sha256_file(path)) for path in files]
    require(receipt["packageContentManifestSha256"] == canonical_manifest_hash(entries), "package content manifest hash mismatch")
    return receipt


def validate_sha256sums() -> None:
    entries: dict[str, str] = {}
    for line_number, line in enumerate((ROOT / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines(), 1):
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        require(match is not None, f"SHA256SUMS.txt:{line_number}: malformed entry")
        digest, relative_path = match.groups()
        require(relative_path not in entries, f"duplicate checksum entry: {relative_path}")
        entries[relative_path] = digest
    actual = sorted(
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS.txt" and "__pycache__" not in path.parts
    )
    require(sorted(entries) == actual, "SHA256SUMS file set mismatch")
    for relative_path, digest in entries.items():
        require(sha256_file(ROOT / relative_path) == digest, f"checksum mismatch: {relative_path}")


def validate_no_mapping_artifacts() -> None:
    forbidden = re.compile(r"(?:^|[_-])(ab|mapping|sealed|oracle)(?:[_\.-]|$)", re.IGNORECASE)
    offenders = [path.relative_to(ROOT).as_posix() for path in ROOT.rglob("*") if path.is_file() and forbidden.search(path.name)]
    require(not offenders, f"mapping/sealed/private artifact present: {offenders}")


def main() -> int:
    try:
        validators = load_four_schemas()
        scenarios, scenarios_by_id = validate_public(validators["public"])
        metadata = validate_metadata(validators["metadata"], scenarios_by_id)
        validate_invariants(validators["invariants"], metadata, scenarios_by_id)
        receipt = validate_provenance(validators["provenance"])
        validate_projection(scenarios, receipt)
        validate_prior_assets(receipt)
        validate_sha256sums()
        validate_no_mapping_artifacts()
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("PASS: react_v1_architecture_24_v3 strict static validation")
    print("SCHEMAS_LOADED: 4")
    print("COUNTS: adaptive=8 deterministic=8 negative=8")
    print("TRIGGERS: zero_result=2 unsupported_evidence=3 stale_reference=3")
    print("PRICE_SNAPSHOT: rows=439 minMinor=36100 sha256=0296cfa77b722ec0ddfa823b093f0751adb187332faccf50353f82676fae5608")
    print("EFFECT_SCOPE: phone_only; laptop/headphones=zero_data_safety_only")
    print("EXECUTION: NOT_RUN; MAPPING: NOT_GENERATED; RELEASE: HOLD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
