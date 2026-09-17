"""Read-only verifier for Context Program P0-P8, plus final receipt writer."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Any


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def verify_sums(directory: Path) -> list[str]:
    checked: list[str] = []
    for line in (directory / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        path = directory / name
        if not path.is_file() or sha_file(path) != digest:
            raise ValueError(f"checksum mismatch: {path}")
        checked.append(name)
    return checked


def junit(path: Path) -> dict[str, int | float]:
    root = ET.parse(path).getroot()
    suite = root.find("testsuite")
    if suite is None:
        raise ValueError(f"missing testsuite: {path}")
    return {
        "tests": int(suite.attrib["tests"]),
        "failures": int(suite.attrib["failures"]),
        "errors": int(suite.attrib["errors"]),
        "skipped": int(suite.attrib["skipped"]),
        "time": float(suite.attrib["time"]),
    }


def run_checks() -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def check(name: str, condition: bool, detail: Any = None) -> None:
        checks.append({"name": name, "status": "PASS" if condition else "FAIL", "detail": detail})

    source = load_json(HERE / "p0/source_freeze.json")
    source_results = []
    for item in source["sourceFiles"]:
        path = ROOT / item["path"]
        current = sha_file(path) if path.is_file() else None
        source_results.append({"path": item["path"], "expected": item["sha256"], "actual": current})
    check(
        "P0 current production sources match freeze",
        all(item["expected"] == item["actual"] for item in source_results),
        source_results,
    )

    legacy = load_json(HERE / "p0/legacy_evidence.json")
    legacy_results = []
    for item in legacy["evidence"]:
        path = ROOT / item["path"]
        actual = sha_file(path) if path.is_file() else None
        legacy_results.append({"evidenceId": item["evidenceId"], "expected": item["sha256"], "actual": actual})
    check(
        "P0 legacy evidence remained byte-identical",
        all(item["expected"] == item["actual"] for item in legacy_results),
        legacy_results,
    )

    manifest = load_json(HERE / "p1/dataset_manifest.json")
    dataset = load_jsonl(HERE / "p1/conversations.jsonl")
    turns = [turn for conversation in dataset for turn in conversation["turns"]]
    check(
        "P1 dataset binding and cardinality",
        sha_file(HERE / "p1/conversations.jsonl") == manifest["datasetSha256"]
        and len(dataset) == manifest["conversationCount"] == 8
        and len(turns) == manifest["turnCount"] == 29
        and sum(turn["turnProvenance"] == "AI_CURATED_CONTEXT_STRESS" for turn in turns) == 8,
        {"conversations": len(dataset), "turns": len(turns)},
    )

    p2 = junit(HERE / "p2/pytest-context-attempt002.xml")
    check(
        "P2 bounded contract suite",
        p2["tests"] == 245 and p2["failures"] == 0 and p2["errors"] == 0 and p2["skipped"] == 2,
        p2,
    )

    p3_files = verify_sums(HERE / "p3/diagnostic001")
    p3 = load_json(HERE / "p3/diagnostic001/result.json")
    check(
        "P3 diagnostic is complete and internally gated",
        len(p3_files) == 4
        and p3["providerCalls"] == 276
        and all(p3["gatesUsingHistoricalV4Thresholds"].values())
        and p3["diagnosticVerdict"] == "V4_LATENCY_HOLD_NOT_REPRODUCED_IN_DIAGNOSTIC",
        {"files": p3_files, "verdict": p3["diagnosticVerdict"]},
    )

    partial_expected = {
        "preflight.json": "b3bb4f93ea8ac3970daf9dd22e95dd92bcc22b8b66c040467c867f15adbf44fc",
        "started.json": "1eac0d62778faa8bba4c8cdcf41ed79ba76d254002c34a441c3b476a0a021087",
        "paired_outputs.jsonl": "efa3885085a84c768f578c2ea0e1808e5a7d59defa038fc5a30375c88dbe52af",
        "private_turn_receipts.jsonl": "908120d668c2c70675c63147b26b5504c0acab686fee7bf63abdff5551ab6f53",
    }
    check(
        "P4 attempt001 partial failure evidence preserved",
        all(sha_file(HERE / "p4/attempt001" / name) == digest for name, digest in partial_expected.items()),
        partial_expected,
    )

    p4_files = verify_sums(HERE / "p4/attempt002")
    p4 = load_json(HERE / "p4/attempt002/result.json")
    p4_receipt = load_json(HERE / "p4/attempt002/receipt.json")
    p4_binding = p4_receipt.pop("receiptBindingSha256")
    p4_outputs = load_jsonl(HERE / "p4/attempt002/paired_outputs.jsonl")
    check(
        "P4 completed HOLD is fully bound",
        len(p4_files) == 6
        and len(p4_outputs) == p4_receipt["pairedOutputRowCount"] == 58
        and sha_file(HERE / "p4/attempt002/paired_outputs.jsonl") == p4_receipt["pairedOutputsSha256"]
        and sha_file(HERE / "p4/attempt002/result.json") == p4_receipt["resultSha256"]
        and hashlib.sha256(canonical(p4_receipt).encode("utf-8")).hexdigest() == p4_binding
        and p4["strictDecision"] == "HOLD_MODEL_IN_LOOP_FIDELITY"
        and not p4["gates"]["all58ArmTurnsSucceeded"],
        {"files": p4_files, "decision": p4["strictDecision"], "rows": len(p4_outputs)},
    )

    p5_files = verify_sums(HERE / "p5/attempt001")
    p5 = load_json(HERE / "p5/attempt001/result.json")
    check(
        "P5 deterministic mechanics gate",
        len(p5_files) == 4
        and p5["rows"] == 400
        and all(p5["gates"].values())
        and p5["strictDecision"] == "BOUNDED_OFFLINE_BUDGET_MECHANICS_ACCEPT"
        and p5["claimBoundary"]["p4HoldStillBinding"],
        {"files": p5_files, "decision": p5["strictDecision"]},
    )

    p6 = junit(HERE / "p6/pytest-durable-web.xml")
    check(
        "P6 durable and web route suite",
        p6["tests"] == 182 and p6["failures"] == p6["errors"] == p6["skipped"] == 0,
        p6,
    )
    p7 = junit(HERE / "p7/pytest-memory-multiagent.xml")
    check(
        "P7 memory and multi-agent isolation suite",
        p7["tests"] == 202 and p7["failures"] == p7["errors"] == p7["skipped"] == 0,
        p7,
    )
    p8 = junit(HERE / "p8/pytest-agent-full.xml")
    p8_redis = junit(HERE / "p8/pytest-redis-supplement.xml")
    check(
        "P8 full Agent regression and Redis supplement",
        p8["tests"] == 3636 and p8["failures"] == p8["errors"] == 0 and p8["skipped"] == 12
        and p8_redis["tests"] == 6 and p8_redis["failures"] == p8_redis["errors"] == p8_redis["skipped"] == 0,
        {"full": p8, "redisSupplement": p8_redis},
    )

    stats = load_json(HERE / "p8/statistical_validation.json")
    check(
        "P8 statistical fallacy coverage",
        stats["fallacyCoverage"] == "11/11 checked"
        and len(stats["fallacies"]) == 11
        and stats["decisionImpact"].startswith("No positive production"),
        {"confidence": stats["overallConfidence"], "coverage": stats["fallacyCoverage"]},
    )

    registry = load_json(HERE / "program_registry.json")
    check(
        "Program registry closes at P8 while P4 HOLD remains binding",
        registry["phases"]["P4"].startswith("HOLD_MODEL_IN_LOOP_FIDELITY")
        and registry["phases"]["P8"].startswith("PROGRAM_EXECUTION_COMPLETE_HOLD_CONTEXT_DEFAULT")
        and registry["productionDefaultPolicy"] == "DO_NOT_CHANGE_WITHOUT_SEPARATE_USER_AUTHORIZATION",
        registry["phases"],
    )
    final_text = (HERE / "FINAL_DECISION.md").read_text(encoding="utf-8")
    check(
        "Final decision is conservative and zero-human",
        "PROGRAM_EXECUTION_COMPLETE__HOLD_CONTEXT_DEFAULT" in final_text
        and "人工标注与人工验收次数为 `0`" in final_text
        and "切换生产默认" in final_text,
        None,
    )
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--sha-output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists() or args.sha_output.exists():
        raise RuntimeError("refusing to overwrite final verification artifacts")
    checks = run_checks()
    passed = sum(item["status"] == "PASS" for item in checks)
    result = {
        "schemaVersion": "context-program-verification-v1",
        "programId": "context_program_v1_20260904",
        "verificationStatus": "PASS" if passed == len(checks) else "FAIL",
        "checksPassed": passed,
        "checksTotal": len(checks),
        "programDecision": "PROGRAM_EXECUTION_COMPLETE__HOLD_CONTEXT_DEFAULT",
        "checks": checks,
    }
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    important = [
        Path(__file__),
        HERE / "README.md",
        HERE / "program_registry.json",
        HERE / "FINAL_DECISION.md",
        HERE / "p0/source_freeze.json",
        HERE / "p0/legacy_evidence.json",
        HERE / "p1/conversations.jsonl",
        HERE / "p1/dataset_manifest.json",
        HERE / "p2/pytest-context-attempt002.xml",
        HERE / "p3/diagnostic001/result.json",
        HERE / "p3/diagnostic001/SHA256SUMS.txt",
        HERE / "p4/attempt001/FAILURE.md",
        HERE / "p4/attempt002/result.json",
        HERE / "p4/attempt002/SHA256SUMS.txt",
        HERE / "p5/attempt001/result.json",
        HERE / "p5/attempt001/SHA256SUMS.txt",
        HERE / "p6/pytest-durable-web.xml",
        HERE / "p7/pytest-memory-multiagent.xml",
        HERE / "p8/pytest-agent-full.xml",
        HERE / "p8/pytest-redis-supplement.xml",
        HERE / "p8/statistical_validation.json",
        args.output,
    ]
    args.sha_output.write_text(
        "".join(f"{sha_file(path)}  {path.relative_to(HERE).as_posix()}\n" for path in important),
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["verificationStatus"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
