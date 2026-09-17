"""Generate descriptive integration report, never formal efficiency claims."""
import json
import statistics
from . import pilot as p


def main(attempt):
    out = p.HERE / attempt
    p.verify_freeze()
    rows = [p.read(f) for f in sorted(out.glob("[0-9][0-9]-*/result.json"))]
    fixtures = {f"pilot-{i:02d}": p.read(p.HERE / "inputs" / f"pilot-{i:02d}.json") for i in range(1, 7)}
    analysis = p.read(out / "analysis.json")
    audit = p.read(p.HERE / "offline_audit.json")
    successful = [r for r in rows if r["status"] == "COMPLETED"]
    groups = []
    for stage in ("state", "final"):
        for arm in p.ARMS:
            values = [r for r in successful if r["arm"] == arm and fixtures[r["fixture"]]["stage"] == stage]
            if not values: continue
            groups.append({"stage": stage, "arm": arm, "n": len(values),
                "meanInputTokens": statistics.mean(r["usage"]["input_tokens"] for r in values),
                "meanOutputTokens": statistics.mean(r["usage"]["output_tokens"] for r in values),
                "meanCachedInputTokens": statistics.mean(r["usage"]["cached_input_tokens"] for r in values),
                "meanCliWallSeconds": statistics.mean(r["cliWallMs"]/1000 for r in values),
                "minCliWallSeconds": min(r["cliWallMs"]/1000 for r in values),
                "maxCliWallSeconds": max(r["cliWallMs"]/1000 for r in values)})
    states = [r for r in rows if fixtures[r["fixture"]]["stage"] == "state"]
    finals = [r for r in rows if fixtures[r["fixture"]]["stage"] == "final"]
    identical_input_comparisons = []
    for f in fixtures.values():
        b = next((r for r in rows if r["fixture"] == f["id"] and r["arm"] == "B_PACK"), None)
        c = next((r for r in rows if r["fixture"] == f["id"] and r["arm"] == "C_PACK_COMPILER"), None)
        if b and c and f["prompts"]["B_PACK"] == f["prompts"]["C_PACK_COMPILER"]:
            identical_input_comparisons.append({"fixture": f["id"], "applicationPromptByteEqual": True,
                "promptSha256": p.sha(f["prompts"]["B_PACK"]),
                "bInputTokens": b["usage"]["input_tokens"], "cInputTokens": c["usage"]["input_tokens"],
                "inputUsageEqual": b["usage"]["input_tokens"] == c["usage"]["input_tokens"]})
    state_ok = sum(r["validation"].get("originalBusinessValidatorAccepted", False) and r["validation"].get("expectedBudget", False) for r in states)
    note_recall = {arm: {"n": sum(r["arm"] == arm for r in finals),
        "early": sum(r["arm"] == arm and r["validation"].get("earlyNoteRetained", False) for r in finals),
        "recent": sum(r["arm"] == arm and r["validation"].get("recentNoteRetained", False) for r in finals)} for arm in p.ARMS}
    data = {"attempt": attempt, "groups": groups, "stateAcceptedAndCorrect": state_ok,
        "stateExecutions": len(states), "noteRecall": note_recall, "quality": "DETERMINISTIC_SPOT_CHECK_ONLY",
        "formalEfficiencyClaim": False, "fullAgentLoopCertified": False,
        "identicalInputComparisons": identical_input_comparisons,
        "applicationOnlyTokenMeasurementGate": "HOLD"}
    p.write_new(out / "descriptive_results.json", data)
    lines = ["# Codex 订阅小规模接入实验报告", "", "## Material Passport", "",
        "- Origin Skill: OpenAI Docs; academic-research-suite / experiment-agent",
        "- Origin Mode: run", "- Origin Date: 2026-09-04", "- Version Label: context_codex_abc_pilot_v1",
        "- Verification Status: bounded integration observations; formal AB/BC HOLD", "",
        "## 结论", "",
        f"本批计划 18 次，已执行 {len(rows)} 次，执行门禁通过 {len(successful)} 次；不同线程 ID {analysis['distinctThreads']} 个。",
        f"任务状态抽取：{state_ok}/{len(states)} 同时通过原始业务校验且预算为 150000 分。最终回答 {len(finals)} 条，只做确定性备注检查，未做盲评。",
        "订阅模型可以提供结构化结果并接入现有阶段校验器；尚未认证完整 Agent 多轮/工具调用闭环，不可直接宣布替换 DeepSeek 或启动正式效能结论。", "",
        "关键反例：pilot-06 的 B/C 应用 prompt 字节完全相同，input_tokens 却为 6692 与 6128，相差 564。不能把这 564 Token 解释成 Compiler 节省；宿主附加上下文/内部请求等来源尚不能由现有记录完全区分。因此接入可用，但应用独立 Token 计量门禁 HOLD。", "",
        "## 固定实验条件", "",
        "- 模型 gpt-5.6-sol，xhigh，default service tier；原生 ChatGPT 登录。未提取/转发凭据，无 API-key 计费、无额度重置。",
        "- 6 个 AI 编写开发样本（3 个状态抽取、3 个最终回答），每样本 A/B/C 各一次、串行，六种臂顺序均使用。非正式数据集、非盲测、非最优窗口搜索。",
        "- A：直接来自完整授权 TaskState、全部合成历史和相同商品材料，不从 Pack 反推 RAW。",
        "- B：调用真实 ContextPack；状态抽取直接用 Pack；最终回答使用真实 FinalAnswer View。",
        "- C：与 B 相同 Pack 再经 query_focused ContextCompiler，随后与 B 相同 View；20000 估算 token 非约束上限，零 budget eviction。",
        "- 三档历史为 607/2450/6025 个字符，不是 4K/8K/16K token。商品是历史目录属性快照；fixture 中 snapshotPriceMinor=null/priceStatus=unverified。提示中模拟价说明仅为通用限制，不能据此认为输入有已验证模拟价格。",
        "- 每次新建 ephemeral CLI，不 resume/fork；空工作目录。冻结源文件和每个输入哈希，原业务代码不改动、不持久化购物状态。", "",
        "## 实测计量（按阶段分开）", "",
        "下表仅为本次接入样本的描述值；不能用于总体效能排名。每格 n=3 时尤其不推断 P95、显著性或质量不劣。", "",
        "| 阶段 | 配置 | n | 平均输入 Token | 平均输出 Token | 平均缓存输入 Token | 平均 CLI 秒 | CLI 秒范围 |",
        "|---|---|---:|---:|---:|---:|---:|---:|"]
    for g in groups:
        lines.append(f"| {g['stage']} | {g['arm']} | {g['n']} | {g['meanInputTokens']:.1f} | {g['meanOutputTokens']:.1f} | {g['meanCachedInputTokens']:.1f} | {g['meanCliWallSeconds']:.2f} | {g['minCliWallSeconds']:.2f}–{g['maxCliWallSeconds']:.2f} |")
    lines += ["", "Token 来自原生 turn.completed usage；cached_input_tokens 是独立报告字段，不再从 input_tokens 扣除来冒充输入长度。reasoning_output_tokens 单列保存在原始记录，本报告不将其额外相加；其与 output_tokens 的包含关系未由此次计量独立验证。",
        "CLI wall = 启动该 Codex 子进程前至退出的单调时钟差，含启动/连接/排队/生成。不是纯模型推理时延、TTFT 或完整 Agent 端到端时延。Pack/Compiler/View 构建耗时为离线单次测量；业务校验耗时未计入 CLI wall。", "",
        "## 边界与质量观察", "",
        "相同应用输入对照（用于发现计量混入项）：", "",
        "| 样本 | B 输入 Token | C 输入 Token | 应用 prompt 字节相同 |",
        "|---|---:|---:|---|"]
    for pair in identical_input_comparisons:
        lines.append(f"| {pair['fixture']} | {pair['bInputTokens']} | {pair['cInputTokens']} | 是 |")
    lines += ["", "备注信息保留检查：", "",
        "| 配置 | 最终回答条数 | 复述最早备注 | 复述最近长消息末尾备注 |",
        "|---|---:|---:|---:|"]
    for arm, val in note_recall.items(): lines.append(f"| {arm} | {val['n']} | {val['early']} | {val['recent']} |")
    lines += ["", "这只是精确字符串命中，不等于购物答案整体质量。人工构造的备注是信息保留探针，并非自然用户总体分布。实际 FinalAnswer View 不传历史；因此 B/C 可能都无法完成备注要求。不得把缺失信息造成的短输出当作等质压缩收益。",
        "离线 6 样本输入边界检查、严格 schema 两个负例、冻结目录两次一致读取、一次写工具本地拒绝均通过。冻结目录检查未由模型发起，不能写成模型已跑通工具闭环。", "",
        "## 接入限制与失败记录", "",
        "1. 原生 CLI 仍注入本机通用技能目录和全局规则。已保存两个独立 debug prompt-input 导出并对公共前缀做等值检查；没有前一探针或本次讨论历史。这不等价于仅应用字段的裸 API 输入。该导出不包含完整 provider wire、全部工具 schema 和 base instructions，且不是对每个实际请求的 wire 抓取。",
        "2. 运行事件没有允许的业务工具执行；已禁用的 code-mode-host 启动提示单独保留。没有因该提示重新开启执行能力。",
        "3. attempt001/002 配置预检失败；attempt003/004 发现通用技能注入，均零采样。attempt005 只有一次抽取，虽答案合规但连接重试污染计量：9428 input、302 output、131.172 秒；不混入本批对比，也不抹去其用量。",
        "4. Windows 既有本地代理为 127.0.0.1:17891。后续仅给子进程沿用该代理，不改机器配置。attempt006 的两个预检前缀一度出现 Default 模式说明差异，门禁 HOLD、零采样；attempt007 未改 runner 再次预检后前缀一致。不能由此宣称宿主提示永久稳定。",
        "5. 内部实际 provider 请求次数不可由 CLI 事件完整证明；本批是否有显式重连/错误由每条 result.json 和原始 events.jsonl 保留。缓存冷热未控制，单轮无重复，不能做可靠效率归因。", "",
        "## 后续正式 AB/BC 前必须补齐", "",
        "- 原生接入层逐请求输入、schema/工具清单和公共开销的可审计冻结；明确接受 Codex 宿主开销还是要求裸 API。",
        "- 真正接入 Agent 的状态写入、只读工具调用、最终回答和恢复路径，保持 runId/Checkpoint 身份。",
        "- 对最终回答 View 的必要历史字段做任务相关保留；不能让 A 能回答、B/C 被移除任务必需信息后再比较效率。修复只能进新输入版本。",
        "- 独立无上下文盲评、同一模型与采样配置、固定数据与缓存策略、长短窗口分层、配对重复；正式数据集不得复用本开发探针充当密封测试。", "",
        "## 审计入口", "",
        f"- `{attempt}/schedule.json`：固定顺序；每个调用目录含 prompt/request/events/stderr/result。",
        f"- `{attempt}/runtime.json`、`pilot_source.py`：确切运行配置和 runner 快照。",
        "- `inputs/manifest.json`：159 个源文件及输入哈希；`offline_audit.json`：离线断言。",
        f"- `{attempt}/analysis.json`、`descriptive_results.json`：聚合数值；`DEVELOPMENT_NOTES.md`：未掩盖的修复轨迹。",
        "- 官方依据：[非交互 CLI](https://learn.chatgpt.com/docs/non-interactive-mode)、[登录](https://learn.chatgpt.com/docs/auth)、[配置](https://learn.chatgpt.com/docs/config-file/config-reference)。", ""]
    (out / "REPORT.md").open("x", encoding="utf-8").write("\n".join(lines))
    p.write_new(out / "artifact_manifest.json", {"at": p.now(), "files": {
        str(f.relative_to(out)): p.file_sha(f) for f in sorted(out.rglob("*")) if f.is_file() and f.name != "artifact_manifest.json"}})
    p.write_new(p.HERE / "package_manifest.json", {"at": p.now(), "files": {
        str(f.relative_to(p.HERE)): p.file_sha(f) for f in sorted(p.HERE.rglob("*"))
        if f.is_file() and f.name != "package_manifest.json"}})
    package = p.read(p.HERE / "package_manifest.json")
    assert all(p.file_sha(p.HERE / path) == digest for path, digest in package["files"].items())
    print(p.canonical(data))


if __name__ == "__main__":
    import sys
    main(sys.argv[1])
