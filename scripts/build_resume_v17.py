"""Build the immediate-submission V17 resume from V16 with four wording fixes."""

from pathlib import Path

from docx import Document


SOURCE = Path(
    r"C:\Users\ming\Desktop\袁明珠_简历\袁明珠_Agent开发工程师Java_岗位定向简历_V16.docx"
)
OUTPUT = Path(
    r"C:\Users\ming\Desktop\袁明珠_简历\袁明珠_Agent开发工程师Java_岗位定向简历_V17.docx"
)

REPLACEMENTS = {
    "持久化执行与故障恢复：": (
        "在该底座上，以 Redis Checkpoint 支持澄清中断、主动暂停和进程恢复；"
        "以 ToolInbox slot、租约和 fencing token 处理工具成功但 Checkpoint 未确认的重放窗口，130 项确定性恢复合同及 12 项篡改测试通过，"
        "重复/缺失副作用和旧 Worker 覆盖均为 0，进程内恢复耗时 P95 约 75ms。"
    ),
    "短期状态、上下文与指代：": (
        "以 ContextPack 汇总 TaskState、近期对话、工具证据与引用，ReferenceContext 服务端收据绑定 session/task/revision、展示序号和 focus；"
        "query-focused ContextCompiler 在 25 组合成长历史中两臂关键状态与引用 25/25 一致、总 Token -22.9%、端到端延迟 P95 -9.1%；"
        "8 个真实会话 21 轮确定性回答/证据 21/21 一致。"
    ),
    "Multi-Agent：": (
        "仅在证据缺口触发只读 EvidenceResearchAgent，请求与回执绑定 task/revision/CandidateScope；"
        "Redis 原子认领父侧合并槽位，父端确定性校验并渲染。公开开发 research 由 5/9→7/9→9/9，"
        "冻结合成留出 8/8、P95 5.50s、原始子观测泄漏 0，异常回退单 Agent。"
    ),
    "库存与订单补偿：": (
        "Redis Lua 原子预扣配合 MySQL 条件扣减、用户/请求唯一索引与 Outbox/Kafka/Inbox；"
        "副作用完成但回执未写时回查 Java 权威状态并按原幂等键至多补偿一次，未决保持 UNKNOWN；"
        "200 并发争抢 50 库存时 50 成功、150 售罄、0 超卖。"
    ),
}


def build() -> None:
    doc = Document(SOURCE)
    doc.core_properties.title = "袁明珠 - Agent 开发工程师（Java）实习简历 V17"
    doc.core_properties.subject = "岗位定向一页中文简历（投递口径微调版）"

    found: set[str] = set()
    for paragraph in doc.paragraphs:
        if len(paragraph.runs) < 2:
            continue
        label = paragraph.runs[0].text
        body = REPLACEMENTS.get(label)
        if body is None:
            continue
        paragraph.runs[1].text = body
        for run in paragraph.runs[2:]:
            run.text = ""
        found.add(label)

    if found != set(REPLACEMENTS):
        raise RuntimeError(
            f"V17 replacement incomplete: missing={sorted(set(REPLACEMENTS) - found)}"
        )

    doc.save(OUTPUT)
    print(OUTPUT)


if __name__ == "__main__":
    build()
