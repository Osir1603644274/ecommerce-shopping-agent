"""Build the frozen public paired dataset and manifest before execution."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PACKAGE = Path(__file__).resolve().parent
SOURCE_PACKAGE = ROOT / "agent/evaluation/context_compiler_provider_paired_v1_20260901_v4"


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def assistant_scaffold(ordinal: int) -> str:
    variants = (
        "我先保留你刚才的条件；商品信息以服务端候选范围和已核验属性为准，未核验的机况不会猜测。",
        "已记录这一轮要求。若后续修改预算、品牌或成色，我会以最新明确条件为准，并重新校验候选范围。",
        "当前只能基于已展示商品与可验证字段继续，卖家宣传和缺失字段不会自动升级为事实。",
        "我会把硬条件、偏好和待确认项分开；出现‘这个’或序号时只使用服务端绑定的展示顺序。",
    )
    return variants[ordinal % len(variants)]


def main() -> int:
    source_rows = [json.loads(line) for line in (SOURCE_PACKAGE / "scenarios.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    latest: dict[str, dict] = {}
    for row in source_rows:
        prior = latest.get(row["scenarioId"])
        if prior is None or row["turnId"] > prior["turnId"]:
            latest[row["scenarioId"]] = row
    rows = []
    for index, row in enumerate(sorted(latest.values(), key=lambda item: item["scenarioId"])):
        messages = [
            {"role": "user", "content": "顺便问一下，配送和发票一般怎么处理？", "provenance": "SYNTHETIC_PRESSURE"},
            {"role": "assistant", "content": assistant_scaffold(index), "provenance": "SYNTHETIC_PRESSURE"},
        ]
        for h_index, history in enumerate(row["contextPayload"].get("historySummaries", [])):
            messages.append({
                "role": history.get("role", "user"),
                "content": history.get("summary", ""),
                "provenance": "PUBLIC_SUMMARY_EXPANDED_AS_CONTROL_TEXT",
            })
            if history.get("role") == "user":
                messages.append({"role": "assistant", "content": assistant_scaffold(index + h_index + 1), "provenance": "SYNTHETIC_PRESSURE"})
        messages.extend([
            {"role": "user", "content": "如果前面的条件和现在冲突，以我最新明确说的为准。", "provenance": "SYNTHETIC_PRESSURE"},
            {"role": "assistant", "content": assistant_scaffold(index + 2), "provenance": "SYNTHETIC_PRESSURE"},
            {"role": "user", "content": row["query"], "provenance": row["lineageKind"]},
        ])
        copied = json.loads(canonical(row))
        copied["rawFullConversation"] = messages
        copied["rawControlProvenance"] = "PUBLIC_STATE_PLUS_DETERMINISTIC_SYNTHETIC_PRESSURE_NOT_HUMAN_TRANSCRIPT"
        rows.append(copied)
    dataset = PACKAGE / "scenarios.jsonl"
    dataset.write_text("".join(canonical(row) + "\n" for row in rows), encoding="utf-8", newline="\n")
    manifest = {
        "schemaVersion": "context-raw-full-vs-compiled-manifest-v1",
        "status": "FROZEN_BEFORE_EXECUTION",
        "attemptId": "attempt001",
        "randomSeed": 20260902,
        "arms": ["RAW_FULL_CONTROL", "COMPILED_TREATMENT"],
        "dataset": {
            "path": dataset.relative_to(ROOT).as_posix(),
            "sha256": sha(dataset),
            "scenarioCount": len(rows),
            "provenance": "PUBLIC_STATE_PLUS_DETERMINISTIC_SYNTHETIC_PRESSURE_NOT_HUMAN_TRANSCRIPT",
        },
        "provider": {"model": "deepseek-v4-flash", "temperature": 0, "maxTokens": 900, "timeoutSeconds": 45, "automaticRetries": 0},
        "gates": {
            "allCallsSucceeded": True,
            "allUsageObserved": True,
            "bothArmsExactFidelity": True,
            "promptTokenReductionAtLeast": 0.15,
            "totalTokenReductionAtLeast": 0.10,
            "p95LatencyRatioAtMost": 1.30,
            "zeroRetries": True,
        },
        "sourceFreeze": {},
        "productionDefaultsChanged": False,
    }
    for relative in (
        "agent/evaluation/context_raw_full_vs_compiled_v1_20260902_v1/runner.py",
        "agent/evaluation/context_raw_full_vs_compiled_v1_20260902_v1/preregistration.md",
        "agent/evaluation/context_compiler_provider_paired_v1_20260901_v4/runner.py",
        "agent/app/context_compiler_v1.py",
    ):
        manifest["sourceFreeze"][relative] = sha(ROOT / relative)
    write_json(PACKAGE / "manifest.json", manifest)
    print(f"FROZEN {len(rows)} scenarios")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
