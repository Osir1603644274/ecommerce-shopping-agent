"""Build a self-contained human/LLM review handoff from immutable live traces."""
from __future__ import annotations

import json
from pathlib import Path
import sys


def block(value):
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```\n"


def main():
    root = Path(sys.argv[1])
    target = root / "FOR_GPT_REVIEW.md"
    if target.exists():
        raise FileExistsError(target)
    trace = json.loads((root / "TRACE.json").read_text(encoding="utf-8"))
    diagnostics = json.loads((root / "DIAGNOSTICS.json").read_text(encoding="utf-8"))
    turns = [turn for scenario in trace["scenarios"] for turn in scenario["turns"]]
    lines = [
        "# 5173 导购 Agent 真实执行链路：独立审查材料",
        "",
        "请作为独立审查者检查**意图理解 → 需求状态 → 检索查询 → 候选审核 → 回答/引用**是否一致。",
        "请把每个结论标为「已由记录证明」「代码推断」「设计取舍」或「证据不足」；给出影响、最小修复和针对性回归测试。",
        "请尤其判断：用户表达偏好与下单式请求是否应同等约束；硬约束允许未知证据时应如何表述；候选主体审核失败时能否展示。",
        "不要把 9 条合成请求推断成总体质量或线上统计。",
        "",
        "## 运行范围与数据真实性",
        "",
        f"- 真实入口：`{trace['baseUrl']}` 的 `/api/commerce-demo/workspace/run`，`mode=step`；三个全新访客会话、九轮合成输入。",
        f"- 结果：{sum(t['final']['status'] == 'completed' for t in turns)}/{len(turns)} 轮完成；解析尝试均为一次；随后每轮显式推进四个检查点。",
        "- 对话执行了真实本地检索和只读商品事实查询；没有选购、下单、付款或售后写操作。",
        "- `modelPlan` 是结构校验、默认值填充、服务端派生 `route/action` 之后、查询后处理之前的记录；**模型原始 tool-call JSON 未被持久保存**，不能把 `modelPlan` 的每个键都称作模型原始输出。",
        "- `TRACE.json` 保存每轮阶段快照、完整回答、Token/耗时和证据哈希；`DIAGNOSTICS.json` 保存下面引用的候选审核明细。两者不含 Cookie、CSRF 或用户账号。",
        "",
        "## 实际代码链路",
        "",
        "```text",
        "5173 POST /workspace/run",
        "  → ensure_shopping_state（新会话建空需求或旧状态迁移）",
        "  → plan_turn：一次结构化模型调用，产出 intent + 完整需求",
        "  → expand_intent：服务端从 intent 派生内部 route/action",
        "  → plan_turn 后处理：可能改写 query/retrievalQuery",
        "  → _requirement：mode→hard/soft，核对 hard 值是否有原文锚点",
        "  → transition + TaskState CAS 写入 domainState.shopping；此时单步运行才返回 waiting",
        "  → prepare → retrieve → answer → publish（每步单独保存）",
        "```",
        "",
        "关键源码：`F:\\agent\\agent\\app\\catalog_conversation.py:26-60,188-227,301-351`；",
        "`F:\\agent\\agent\\app\\guide_interpreter.py:27-69,103-172`；",
        "`F:\\agent\\agent\\app\\guide_state.py:86-115`；",
        "`F:\\agent\\agent\\app\\api\\catalog_workspace.py:139-259`；",
        "`F:\\agent\\agent\\app\\catalog_requirements.py:32-82`。",
        "",
        "## 九轮总览",
        "",
        "| 会话/轮次 | 用户原文 | intent | 最终 retrievalQuery | 解析后 history 条数 | 发布 scope 的候选组 | 耗时 |",
        "|---|---|---|---|---:|---:|---:|",
    ]
    for scenario in trace["scenarios"]:
        for index, turn in enumerate(scenario["turns"], 1):
            run = turn["afterSubmission"]["run"]
            shopping = turn["afterSubmission"]["task"]["shopping"]
            scope = turn["checkpoints"][-1]["run"]["publishedScope"]
            lines.append(f"| {scenario['name']}/{index} | {turn['input']} | `{run['intent']}` | "
                f"`{shopping['retrievalQuery']}` | {len(shopping['history'])} | "
                f"{scope['groupCount'] if scope else 0} | {turn['final']['elapsedSeconds']}s |")
    lines += ["", "## 逐轮对象与回答", "",
        "以下每轮都是真实持久化快照。`modelPlan` 中 `route/action` 是服务端兼容字段，故此处只展示模型语义字段。",
        "`effectivePlan` 是后处理和引用绑定后实际用于执行的计划；`shopping` 是解析完成后写入的需求状态。",
        ""]
    semantic_keys = ("intent", "query", "retrievalQuery", "requirements", "numbers", "question", "followup", "accessory", "referenceModel")
    for scenario in trace["scenarios"]:
        lines.append(f"### 会话 {scenario['name']}")
        lines.append("")
        for index, turn in enumerate(scenario["turns"], 1):
            initial = turn["afterSubmission"]
            run = initial["run"]
            model = run["modelPlan"] or {}
            effective = run["effectivePlan"] or {}
            lines += [f"#### 第 {index} 轮：{turn['input']}", "",
                f"解析调用 {run['parseAttempts']} 次；解析 Token {((run['parseUsage'] or {}).get('total_tokens'))}；"
                f"`literalQueryPreserved={str(run['literalQueryPreserved']).lower()}`；"
                f"`queryRenderedFromRequirements={str(run['queryRenderedFromRequirements']).lower()}`。", "",
                "模型决策记录：", "", block({key: model.get(key) for key in semantic_keys}),
                "执行计划：", "", block({key: effective.get(key) for key in semantic_keys}),
                f"解析后 TaskState `revision={initial['task']['revision']}`，`shopping`：", "",
                block(initial["task"]["shopping"]),
                "执行检查点：", "",
                " → ".join(f"{step['run']['nodes'][-1]['label']} [{step['run']['status']}]"
                           for step in turn["checkpoints"]), "",
                "发布证据摘要：", "", block(turn["checkpoints"][-1]["run"]["publishedScope"]),
                f"最终状态 `{turn['final']['status']}`，回答原文：", "",
                "> " + turn["final"]["answer"].replace("\n", "\n> ") if turn["final"]["answer"] else "> （无回答）", ""]
    phone_case = next(row for row in diagnostics if row["scenario"] == "preference" and row["turn"] == 1)
    case = next(group for group in phone_case["groups"] if "手机壳" in group["title"])
    budget = next(row for row in diagnostics if row["scenario"] == "explicit" and row["turn"] == 2)
    undo = next(row for row in diagnostics if row["scenario"] == "explicit" and row["turn"] == 3)
    lines += ["## 需要重点复核的具体证据", "",
        "这些是审查线索，不预设每项都必须判为缺陷。", "",
        "1. **偏好强度：**“我喜欢苹果手机”得到 `intent=search`，品牌苹果是 `mode=require → priority=hard`。"
        "对照“来一个苹果手机”，两句的结构化约束相同。服务端的 hard 校验只确认值“苹果”出现在原文，不校验“喜欢”所表达的强度。",
        "2. **查询后处理：**模型给第一句的 `retrievalQuery=苹果手机`，但 `literalQueryPreserved=true` "
        "使执行值变为 `我喜欢苹果手机`；“来一个苹果手机”同理，‘找一个玻璃杯’变成 `一个玻璃杯`。"
        "预算修改后还可能复用这一检索词。代码位置 `catalog_conversation.py:201-214`。",
        "3. **品牌放宽：**“不限苹果，安卓也行”之后，`shopping` 确实移除了苹果品牌硬条件，"
        "但 `retrievalQuery=手机 安卓`；需要判断这是否把允许安卓误变为偏向安卓，影响原本也允许的其他品牌。",
        "4. **手机壳候选进入手机结果：**下面是第一轮展示的第 2 项，来自已发布 scope；"
        "模型识别其主体为 `other`，却给出标题中不连续的引文，服务端拒绝该判断并转为 `unknown`；"
        "标题里“手机壳”含“手机”子串，原硬条件证据反而记 `supported`，该候选被保留。",
        block(case),
        "5. **预算未知：**“预算3000元以内”写成 hard 上限，但下例候选的价格证据是 `unknown`，"
        "仍被展示；回答里提示价格需核验。这可能是刻意的‘未知保留’策略，"
        "需要明确用户是否会把展示理解成已满足预算。",
        block({"title": budget["groups"][0]["title"],
               "budgetEvidence": next(item for item in budget["groups"][0]["constraintEvidence"]
                                      if item["facet"] == "预算")}),
        "6. **撤销后的审核差异：**预算修改轮发布的 scope 有 6 组；撤销后重新检索的 scope 有 "
        f"{undo['groupCount']} 组，展示上限仍为 6。撤销的 `answer` 分支直接生成文字，"
        "不调用 `answer_catalog` 的模型主体复核；下例第一项的 `subjectReview=null`。"
        "本样本前 6 个标题未直接证明商品主体错误，但审核合同与普通搜索不同。",
        block({"undoScopeGroupCount": undo["groupCount"],
               "firstTitle": undo["groups"][0]["title"],
               "firstSubjectReview": undo["groups"][0]["subjectReview"]}),
        "7. **引用链：**比较轮与前一轮的 scopeId 相同；问“第1项多少钱”走"
        "“读取所指商品记录 → 回答商品问题”，没有重新搜索，返回的是明确标注的本地模拟参考价 2322.00 元。",
        "",
        "## 请审查者输出", "",
        "对每个问题给出：①对应的输入、模型决策、状态和输出证据；②是否已证实错误，"
        "还是潜在风险/有意设计；③优先级及最小修改位置；④一条能防复发的端到端测试。",
        "特别区分‘模型语义错误’、‘确定性后处理改写’、‘检索召回问题’、‘候选证据/审核问题’，"
        "不要把一层的错归到另一层。", ""]
    target.write_text("\n".join(lines), encoding="utf-8")
    print(f"{target} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
