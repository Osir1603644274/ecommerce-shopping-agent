"""Build the evidence-bound, black-and-white one-page Chinese resume V8.

V8 preserves the visually verified V7 layout and only tightens evidence-bound
project bullets for the immediate HR delivery.
"""

from __future__ import annotations

from pathlib import Path
from copy import deepcopy

from docx import Document
from docx.text.paragraph import Paragraph

from build_resume_v7 import set_run_font


SOURCE = Path(r"C:\Users\ming\Desktop\袁明珠_简历\袁明珠_Agent开发工程师Java_岗位定向简历_V7.docx")
OUTPUT = Path(r"C:\Users\ming\Desktop\袁明珠_简历\袁明珠_Agent开发工程师Java_岗位定向简历_V8.docx")


BULLETS = {
    "受约束 ReAct：": "模型仅选择签名动作，服务端执行与校验，保留 fixed_v1 回滚；24 场景安全门 24/24、工具身份失败 0。",
    "Context / 指代：": "25 组冻结合成长历史压力集两臂 25/25 等价，Prompt/总 Token 分别降低 26.43%/22.92%；真实网页验证显式序号与 focus，相关回归 577/577。",
    "Checkpoint：": "Redis＋ToolInbox logical slot/lease/fence；130/130 恢复、12/12 篡改拒绝，重复/缺失副作用与旧 Worker 覆盖均为 0；进程内 RTO P50/P95 为 42.182/75.475 ms。",
    "检索选型：": "在 Amazon ESCI 50 个 US 查询、1,036 对官方人工标注 query-product pairs 上完成候选列表重排，最佳 macro nDCG@10 为 0.7069（随机顺序 0.5283），配对差值 0.1786，95% CI [0.1163, 0.2430]。",
    "Multi-Agent：": "证据缺口触发只读 EvidenceResearchAgent，Redis 原子合并、父端确定性渲染；公开开发单 Agent/旧版/修复后为 5/9、7/9、9/9，冻结合成留出 8/8、P95 5.50 s、泄漏 0。",
    "记忆 / Skill / MCP：": "构建 MySQL 权威＋Redis 投影的可治理长期记忆，439 商品真实三会话链路 42/42；实现 10 路 UNJUDGED 候选池，显式 Skill 与本地 STDIO MCP 共用确定性核心并保持字节一致。",
    "权限与交易：": "Agent 不持有订单写能力；Java 回查 JWT 与明确确认，以 GETDEL＋Idempotency-Key 防重复消费/建单，Redis Lua＋数据库唯一边界保证一人一单。",
    "异步一致性：": "订单与 Outbox 同事务，Kafka→Inbox→Elasticsearch 以 projection cursor/external_gte 处理重复、乱序与旧版本；异常路径显式回到 MySQL 权威。",
    "缓存与并发：": "Redis Lua 原子预扣＋MySQL 条件扣减＋唯一索引形成多层库存防护；200 个客户端任务争 50 库存，50 成功/150 售罄/0 超卖，服务端峰值并发 7。",
    "有界微服务拆分：": "拆分 Gateway、Catalog/Search、Trade，引入 Feign、Bulkhead 与 CircuitBreaker；正式场景 10/10。同源对照拆分 P50/P95 增加 104.65%/11.88%、启动增加 158.64%，故模块化单体继续默认。",
}


def replace_bullet(paragraph, label: str, body: str) -> None:
    runs = paragraph.runs
    if len(runs) < 2:
        raise RuntimeError(f"unexpected bullet run structure: {label}")
    runs[0].text = label
    runs[1].text = body
    for run in runs[2:]:
        run.text = ""


def insert_after(paragraph, *, label: str, body: str) -> Paragraph:
    clone = deepcopy(paragraph._p)
    paragraph._p.addnext(clone)
    inserted = Paragraph(clone, paragraph._parent)
    replace_bullet(inserted, label, body)
    return inserted


def insert_backend_stack(after_paragraph) -> None:
    node = deepcopy(after_paragraph._p)
    after_paragraph._p.addnext(node)
    paragraph = Paragraph(node, after_paragraph._parent)
    for run in paragraph.runs:
        run.text = ""
    paragraph.paragraph_format.space_before = after_paragraph.paragraph_format.space_before
    paragraph.paragraph_format.space_after = after_paragraph.paragraph_format.space_after
    paragraph.paragraph_format.line_spacing = 1.0
    first = paragraph.runs[0] if paragraph.runs else paragraph.add_run()
    first.text = "后端技术栈："
    set_run_font(first, 9.7, bold=True, east_asia="黑体")
    second = paragraph.add_run(
        "Java 17 / Spring Boot / Spring Security / MyBatis / Spring Cloud / MySQL / Redis / Caffeine / Kafka / Elasticsearch"
    )
    set_run_font(second, 9.7, latin="Arial")


def build() -> None:
    if not SOURCE.is_file():
        raise FileNotFoundError(SOURCE)
    doc = Document(SOURCE)
    matched: set[str] = set()
    cache_paragraph = None
    for paragraph in doc.paragraphs:
        if paragraph.text.startswith("电商购物 Agent 与 Java 交易平台"):
            paragraph.runs[0].text = "电商购物 Agent  |  个人项目"
        elif paragraph.text.startswith("技术栈："):
            paragraph.runs[0].text = "Agent 技术栈："
            paragraph.runs[1].text = "Python / FastAPI / LangGraph / MCP / Redis / MySQL / Elasticsearch"
        elif paragraph.text.startswith("面向多轮商品检索"):
            paragraph.runs[0].text = "面向多轮商品检索、比较、证据核验与交易确认：Agent 负责搜推决策，Java 后端负责身份、库存、订单与异步投影。"
        elif paragraph.text == "Java 交易后端与 Spring Cloud":
            paragraph.runs[0].text = "Java 电商交易后端"
            insert_backend_stack(paragraph)
        for label, body in BULLETS.items():
            if paragraph.text.startswith(label):
                replace_bullet(paragraph, label, body)
                matched.add(label)
                if label == "缓存与并发：":
                    paragraph.runs[0].text = "库存与并发："
                    cache_paragraph = paragraph
                break
    missing = set(BULLETS) - matched
    if missing:
        raise RuntimeError(f"missing V7 bullets: {sorted(missing)}")
    if cache_paragraph is None:
        raise RuntimeError("missing backend inventory/cache paragraph")
    insert_after(
        cache_paragraph,
        label="缓存治理：",
        body="实现 Caffeine＋Redis 两级缓存；Redis 命中回填本地缓存，商品更新后跨实例失效，缓存异常路径显式回到 MySQL 权威。",
    )

    doc.core_properties.title = "袁明珠 - Agent 开发工程师（Java）实习简历 V8"
    doc.core_properties.subject = "岗位定向一页中文简历（证据更新版）"
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(OUTPUT)
    print(OUTPUT)


if __name__ == "__main__":
    build()
