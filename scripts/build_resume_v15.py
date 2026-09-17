"""Build the V15 Java/Agent role-targeted resume from the preserved V14.

V15 tightens the evidence hierarchy without changing the black-and-white layout:
- restores ReAct Harness as the runtime-governance layer;
- clarifies the architecture blind-review and checkpoint contracts;
- merges product retrieval and merchant RAG under one retrieval/evaluation story;
- scopes Context and Multi-Agent metrics to their actual experiments;
- rewrites long-term memory and the Agent-Java integration in business language.
"""

from __future__ import annotations

from pathlib import Path

from docx import Document


SOURCE = Path(
    r"C:\Users\ming\Desktop\袁明珠_简历\袁明珠_Agent开发工程师Java_岗位定向简历_V14.docx"
)
OUTPUT = Path(
    r"C:\Users\ming\Desktop\袁明珠_简历\袁明珠_Agent开发工程师Java_岗位定向简历_V15.docx"
)


REPLACEMENTS = {
    "Agent 架构与选型：": (
        "Agent 架构与运行治理：",
        "针对固定 PAE 难以适配灵活追问，将决策层收敛为受约束 ReAct，并以 ReAct Harness 统一结构化动作、工具权限、轮次/超时预算、循环检测与异常回退；"
        "24 个场景每臂 33 轮，5 个差异场景双评审共 10 次判断为 ReAct/原方案/平局=7/0/3。",
    ),
    "持久化执行与故障恢复：": (
        "持久化执行与故障恢复：",
        "基于 LangGraph 状态图、Interrupt 与 Redis Checkpoint 支持澄清中断、主动暂停和进程恢复；"
        "以 ToolInbox slot、租约和 fencing token 处理工具成功但 Checkpoint 未确认的重放窗口，130 项确定性恢复合同及 12 项篡改测试通过，"
        "重复/缺失副作用和旧 Worker 覆盖均为 0，RTO P95 约 75ms。",
    ),
    "上下文与指代：": (
        "短期状态、上下文与指代：",
        "以 ContextPack 汇总 TaskState、近期对话、工具证据与引用，ReferenceContext 服务端收据绑定 session/task/revision、展示序号和 focus；"
        "query-focused ContextCompiler 在 25 组合成长历史中两臂 25/25 正确、总 Token -22.9%、P95 -9.1%，"
        "8 个真实会话 21 轮确定性回答/证据 21/21 一致。",
    ),
    "检索与数据评测：": (
        "检索与 RAG 评测：",
        "基于 KuaiSearch 46,079 篇商品文档、507 条真实 Query、12 个品类构建基准，并以 439 件二手手机、24 条复杂 Query 深测约束检索；"
        "对比 ES/Dense/RRF/Cross-Encoder/LLM 重排后，选用 ES Standard Top50、MySQL 事实核验与确定性重排。"
        "扩展至 Yelp 300 家商户、6,774 条评论的 RAG，Vector+BM25 双路召回及证据冲突重排使 6 道独立密封题 nDCG@5 由 0.368 升至 0.788。",
    ),
    "评测工程化：": (
        "检索评测工具链：",
        "将 10 路检索候选池构建封装为显式 Skill 与本地 STDIO MCP，共用确定性内核及 manifest/receipt/SHA 证据链；"
        "冻结 30 个源文件、133 项 SHA 复核零失败，显式 Skill/MCP 输出字节一致，并完成真实 ES 8.17.6+BGE 双次只读重放。",
    ),
    "Multi-Agent：": (
        "Multi-Agent：",
        "仅在证据缺口触发只读 EvidenceResearchAgent，请求与回执绑定 task/revision/CandidateScope，经 Redis 原子合并后由父端确定性渲染；"
        "公开开发 research 由 5/9→7/9→9/9，冻结合成留出 8/8、P95 5.50s、原始子观测泄漏 0，异常回退单 Agent。",
    ),
    "长期记忆：": (
        "长期记忆：",
        "针对普通对话误写偏好，设计异步候选→用户确认→MySQL 权威版本链→Redis 投影，支持跨 Session 召回、纠正、停用与删除；"
        "三次隔离会话全链路 42/42，425 名用户未来行为离线评测 nDCG@10 +0.0074（95% CI [0.0037,0.0116]）。",
    ),
    "交易闭环：": (
        "Agent—Java 业务闭环：",
        "Agent 通过只读工具调用 Java 商品检索与事实核验接口；下单仅生成 CandidateScope 绑定提案，由 Java 回查 JWT 并在用户二次确认后写入；"
        "订单查询按归属校验后由 Java 确定性回传，GETDEL 与 Idempotency-Key 分别防止确认重放和重复建单。",
    ),
}

REMOVE_LABELS = {"商家评论 RAG："}


def replace_bullet(paragraph, new_label: str, body: str) -> None:
    if len(paragraph.runs) < 2:
        raise RuntimeError(f"bullet has no body run: {paragraph.text}")
    paragraph.runs[0].text = new_label
    paragraph.runs[1].text = body
    for run in paragraph.runs[2:]:
        run.text = ""


def remove_paragraph(paragraph) -> None:
    element = paragraph._element
    element.getparent().remove(element)


def build() -> None:
    doc = Document(SOURCE)
    doc.core_properties.title = "袁明珠 - Agent 开发工程师（Java）实习简历 V15"
    doc.core_properties.subject = "岗位定向一页中文简历（架构治理与检索评测收口版）"

    found: set[str] = set()
    removed: set[str] = set()
    for paragraph in list(doc.paragraphs):
        if not paragraph.runs:
            continue
        label = paragraph.runs[0].text
        if (
            label == "技术栈："
            and len(paragraph.runs) >= 2
            and paragraph.runs[1].text.startswith("Java 17 /")
        ):
            paragraph.runs[1].text = (
                paragraph.runs[1]
                .text.replace(" / ", "/")
                .replace("Spring Cloud", "Spring\u00a0Cloud")
            )
        if label in REMOVE_LABELS:
            remove_paragraph(paragraph)
            removed.add(label)
            continue
        replacement = REPLACEMENTS.get(label)
        if replacement is None:
            continue
        replace_bullet(paragraph, *replacement)
        found.add(label)

    if found != set(REPLACEMENTS):
        raise RuntimeError(
            f"V15 replacement incomplete: missing={sorted(set(REPLACEMENTS) - found)}"
        )
    if removed != REMOVE_LABELS:
        raise RuntimeError(
            f"V15 removal incomplete: missing={sorted(REMOVE_LABELS - removed)}"
        )

    doc.save(OUTPUT)
    print(OUTPUT)


if __name__ == "__main__":
    build()
