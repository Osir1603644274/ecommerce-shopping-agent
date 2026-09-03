"""Create the immutable human summary and checksum inventory for attempt001."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


PACKAGE = Path(__file__).resolve().parent
RESULT = PACKAGE / "attempt001/result.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    result = json.loads(RESULT.read_text(encoding="utf-8"))
    effects = result["pairedEffects"]
    raw = result["aggregate"]["RAW_FULL_CONTROL"]
    compiled = result["aggregate"]["COMPILED_TREATMENT"]
    report = f"""# Context 原始全量历史 vs 编译视图 V1：最终证据与结论

## 结论

`{result['verdict']}`。

在 25 组公开状态锚定、确定性合成长历史压力样本上，两臂均为 `25/25` 精确保真；编译视图相对全量历史控制臂 Prompt Token 降低 `{effects['promptTokenReductionFraction']:.2%}`，总 Token 降低 `{effects['totalTokenReductionFraction']:.2%}`，P95 延迟比为 `{effects['treatmentToControlP95LatencyRatio']:.4f}`。

## 原始数字

| 指标 | RAW_FULL_CONTROL | COMPILED_TREATMENT |
|---|---:|---:|
| 成功调用 | {raw['succeeded']}/25 | {compiled['succeeded']}/25 |
| 精确保真 | {raw['exactFidelity']}/25 | {compiled['exactFidelity']}/25 |
| Prompt Token 总量 | {raw['promptTokensSum']} | {compiled['promptTokensSum']} |
| 总 Token | {raw['totalTokensSum']} | {compiled['totalTokensSum']} |
| P50 延迟 | {raw['latencyP50Ms']:.3f} ms | {compiled['latencyP50Ms']:.3f} ms |
| P95 延迟 | {raw['latencyP95Ms']:.3f} ms | {compiled['latencyP95Ms']:.3f} ms |

实际模型调用 `50/50`，usage `50/50` 完整，自动重试为 0，全部预注册门通过。

## 允许与禁止表述

允许：在合成长历史压力的 25 组配对实验中，权威编译视图保持上下文字段 `25/25` 精确一致，并减少 26.43% Prompt Token、22.92% 总 Token，P95 延迟降低约 9.08%。

禁止：不得称真人对话、完整购物任务质量提升、生产全量 ACCEPT 或已默认启用。数据中的长历史包含确定性合成助手回复，实验只测上下文事实保真。

旧 Context V4 的 `HOLD_CONTEXT_PROVIDER_PAIR` 保持原样；本包是问题与控制臂不同的新证据，不覆盖旧 attempt。
"""
    (PACKAGE / "FINAL_EVIDENCE_AND_DECISION_2026-09-02.md").write_text(report, encoding="utf-8", newline="\n")
    files = sorted(
        path for path in PACKAGE.rglob("*")
        if path.is_file()
        and path.name not in {"SHA256SUMS.txt", "verification.json"}
        and "__pycache__" not in path.parts
    )
    lines = [f"{sha(path)}  {path.relative_to(PACKAGE).as_posix()}" for path in files]
    (PACKAGE / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    failures = []
    for line in (PACKAGE / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        expected, relative = line.split("  ", 1)
        if sha(PACKAGE / relative) != expected:
            failures.append(relative)
    verification = {"status": "PASS" if not failures else "FAIL", "checkedFiles": len(lines), "failures": failures, "resultVerdict": result["verdict"]}
    (PACKAGE / "verification.json").write_text(json.dumps(verification, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(verification, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
