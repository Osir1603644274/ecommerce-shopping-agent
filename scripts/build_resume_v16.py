"""Build V16 from V15 with a minimal LangGraph architecture clarification."""

from pathlib import Path

from docx import Document


SOURCE = Path(
    r"C:\Users\ming\Desktop\袁明珠_简历\袁明珠_Agent开发工程师Java_岗位定向简历_V15.docx"
)
OUTPUT = Path(
    r"C:\Users\ming\Desktop\袁明珠_简历\袁明珠_Agent开发工程师Java_岗位定向简历_V16.docx"
)

REPLACEMENTS = {
    "Agent 架构与运行治理：": (
        "底层以 LangGraph 状态图和 Interrupt 承载执行、中断与恢复，决策层针对固定 PAE 难以适配灵活追问的问题收敛为受约束 ReAct，"
        "外围以 ReAct Harness 统一结构化动作、工具权限、轮次/超时预算、循环检测与异常回退；"
        "24 个场景每臂 33 轮，5 个差异场景双评审共 10 次判断为 ReAct/原方案/平局=7/0/3。"
    ),
    "持久化执行与故障恢复：": (
        "在该底座上，以 Redis Checkpoint 支持澄清中断、主动暂停和进程恢复；"
        "以 ToolInbox slot、租约和 fencing token 处理工具成功但 Checkpoint 未确认的重放窗口，130 项确定性恢复合同及 12 项篡改测试通过，"
        "重复/缺失副作用和旧 Worker 覆盖均为 0，RTO P95 约 75ms。"
    ),
}


def build() -> None:
    doc = Document(SOURCE)
    doc.core_properties.title = "袁明珠 - Agent 开发工程师（Java）实习简历 V16"
    doc.core_properties.subject = "岗位定向一页中文简历（LangGraph 架构关系修订版）"

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
            f"V16 replacement incomplete: missing={sorted(set(REPLACEMENTS) - found)}"
        )

    doc.save(OUTPUT)
    print(OUTPUT)


if __name__ == "__main__":
    build()
