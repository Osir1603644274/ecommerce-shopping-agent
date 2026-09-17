"""Build the V14 Java/Agent role-targeted resume from the preserved V13.

V14 makes three evidence-alignment edits only:
- name the first Agent section as architecture/selection rather than runtime;
- distinguish the default ContextPack from the experimental ContextCompiler;
- bind the merchant-review RAG claim to its Yelp corpus and sealed evaluation.
"""

from __future__ import annotations

from pathlib import Path

from docx import Document

import build_resume_v13


OUTPUT = Path(
    r"C:\Users\ming\Desktop\袁明珠_简历\袁明珠_Agent开发工程师Java_岗位定向简历_V14.docx"
)


REPLACEMENTS = {
    "Agent 运行时：": (
        "Agent 架构与选型：",
        "针对固定 PAE 难以适配灵活追问的问题，先以 LangGraph 状态图、Interrupt 与 Checkpoint 建立可暂停/恢复底座，"
        "再将决策层收敛为受约束 ReAct；24 场景每臂 33 轮，差异回答双评审盲评 ReAct/原方案/平局=7/0/3。",
    ),
    "上下文与指代：": (
        "上下文与指代：",
        "以 ContextPack 汇总 TaskState、近期对话、工具证据与引用，并由 ReferenceContext 服务端收据绑定商品指代；"
        "另实现 query-focused ContextCompiler 实验路径，25 组合成长历史中总 Token -22.9%，8 个真实会话 21 轮回答/证据 21/21 字节一致。",
    ),
    "商家评论 RAG：": (
        "商家评论 RAG：",
        "基于 Yelp 300 家商户、6,774 条中文化评论，针对短评证据分散与否定语义误排，采用 Vector+BM25 各 Top30 双路召回及支持/冲突内容重排；"
        "6 道独立密封题中 nDCG@5 由 0.368 升至 0.788、候选证据商户召回 100%，因在线延迟约 13s 与忠实度未过门保留 Shadow。",
    ),
}


def build() -> None:
    build_resume_v13.OUTPUT = OUTPUT
    build_resume_v13.build()

    doc = Document(OUTPUT)
    doc.core_properties.title = "袁明珠 - Agent 开发工程师（Java）实习简历 V14"
    doc.core_properties.subject = "岗位定向一页中文简历（Context 与商家 RAG 证据修订版）"

    found: set[str] = set()
    for paragraph in doc.paragraphs:
        if not paragraph.runs:
            continue
        old_label = paragraph.runs[0].text
        replacement = REPLACEMENTS.get(old_label)
        if replacement is None:
            continue
        new_label, body = replacement
        if len(paragraph.runs) < 2:
            raise RuntimeError(f"bullet has no body run: {old_label}")
        paragraph.runs[0].text = new_label
        paragraph.runs[1].text = body
        for run in paragraph.runs[2:]:
            run.text = ""
        found.add(old_label)

    if found != set(REPLACEMENTS):
        raise RuntimeError(
            f"V14 replacement incomplete: missing={sorted(set(REPLACEMENTS) - found)}"
        )

    doc.save(OUTPUT)
    print(OUTPUT)


if __name__ == "__main__":
    build()
