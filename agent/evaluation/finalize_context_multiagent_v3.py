"""Finalize the zero-model MA2 remediation evidence package."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "agent/evaluation/runs/context_multiagent_public_pilot_v3_attempt001_remediation001"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    result = json.loads((OUTPUT / "summary.json").read_text(encoding="utf-8"))
    report = f"""# Multi-Agent V2 公开开发配对：最终证据与结论

## 结论

`{result['status']}`。

旧 V1 在 9 个 research 场景中单 Agent 与 Multi-Agent 均为 `4/9`。本轮定位到子 Agent 压缩报告丢失“机况选择”信息，新增绑定 `taskId/revision/CandidateScope/agentRunId/sequence` 的请求—回执，并将服务端核验的最小决策摘要回传父 Agent。

## 数据

- 全部 33 场景：强单 Agent `29/33`，MA2 `31/33`。
- 9 个 research 场景：强单 Agent `5/9`，MA2 `7/9`；MA2 独有成功 2，反向 0。
- Claim precision：两臂均 `0.7778`。
- 父 Agent 最终上下文估算 Token：P50 `2588 → 2033`，均值降低 `23.17%`。
- 总 Provider Input Token：`54838 → 54510`，基本持平。
- P95 延迟：`9612.815 → 9904.398 ms`；安全失败 0，通信合同失败 0。

## 失败保全

原 attempt 在 66 条 arm trace 与 36 条模型回执全部落盘后，因汇总器误读 SLO 字段名退出。失败目录保留；相邻 remediation 只执行零模型调用重算，未覆盖 trace 或改变阈值。

## 边界

这是公开开发集 remediation，不是 untouched confirmation；McNemar 双侧 `p=0.5`，Bootstrap 质量差异区间仍含 0。允许写“公开开发集从 5/9 提升到 7/9”，禁止写“已证明 Multi-Agent 普遍提升质量”或“生产默认已切换”。
"""
    (OUTPUT / "FINAL_EVIDENCE_AND_DECISION_2026-09-02.md").write_text(report, encoding="utf-8", newline="\n")
    files = sorted(path for path in OUTPUT.rglob("*") if path.is_file() and path.name not in {"SHA256SUMS.txt", "verification.json"} and "__pycache__" not in path.parts)
    checksums = "\n".join(f"{sha(path)}  {path.relative_to(OUTPUT).as_posix()}" for path in files) + "\n"
    (OUTPUT / "SHA256SUMS.txt").write_text(checksums, encoding="utf-8", newline="\n")
    failures = []
    for line in checksums.splitlines():
        expected, relative = line.split("  ", 1)
        if sha(OUTPUT / relative) != expected:
            failures.append(relative)
    verification = {"status": "PASS" if not failures else "FAIL", "checkedFiles": len(files), "failures": failures, "verdict": result["status"]}
    (OUTPUT / "verification.json").write_text(json.dumps(verification, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(verification, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
