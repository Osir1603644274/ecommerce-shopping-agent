"""Build the final black-and-white one-page Chinese resume V11."""

from __future__ import annotations

from pathlib import Path

from docx import Document

import build_resume_v10


OUTPUT = Path(
    r"C:\Users\ming\Desktop\袁明珠_简历\袁明珠_Agent开发工程师Java_岗位定向简历_V11.docx"
)


INTRO = (
    "面向二手 3C 多轮导购，打通自然语言检索与比较、受控下单、订单状态查询；"
    "Python Agent 负责决策，Java 后端持有商品、身份、库存、订单与支付权威。"
)


BULLETS = {
    "Agent 运行时：": (
        "由固定 PAE 演进至 LangGraph PAE，以状态图、Interrupt 与 Checkpoint 支撑暂停/恢复；"
        "保留持久化底座并将决策层收敛为受约束 ReAct，动作由服务端签发、执行与校验。"
        "24 场景每臂 33 轮，差异回答双人盲评 10 票，ReAct/fixed/tie=7/0/3，保留 fixed_v1 回滚。"
    ),
    "可靠执行：": (
        "以 Redis Checkpoint、ToolInbox、租约/fencing token 处理工具成功后崩溃、重复执行及旧 Worker 覆盖；"
        "130/130 恢复、12/12 篡改拒绝，重复/缺失副作用为 0，进程内 RTO P95 约 75ms。"
    ),
    "上下文与指代：": (
        "对比原始全量历史与 ContextCompiler，25 组冻结合成长历史均保持 25/25 结果等价，总 Token 下降 22.9%；"
        "ReferenceContext 收据绑定会话、任务版本、CandidateScope 与真实展示顺序，过期、跨任务引用失败关闭。"
    ),
    "检索 / RAG 选型：": (
        "在 439 商品、24 Query 上对比 ES、Dense、RRF 及模型重排；混合方案 nDCG@10 +1.45pp 但 Recall@50 -5.01pp，"
        "故保留 ES Standard Top50＋MySQL 核验＋确定性重排。商家评论 RAG 以双路召回和内容重排将 nDCG@5 从 0.368 升至 0.788，"
        "未过延迟/忠实度门则仅保留 Shadow。"
    ),
    "Multi-Agent：": (
        "仅在证据缺口时触发只读 EvidenceResearchAgent，消息绑定任务版本与 CandidateScope，Redis 原子合并后由父 Agent 确定性渲染；"
        "开发集 5/9→9/9、冻结合成留出 8/8、原始子观测泄漏 0，异常回退单 Agent。"
    ),
    "长期记忆：": (
        "为防点击和普通对话误写为长期偏好，设计“异步候选→用户确认→MySQL 权威版本链→Redis 投影”，"
        "支持跨 Session 召回、纠正、停用与删除；真实 MySQL/Redis/ES/Java/模型三 Session 链路完成 42/42 有界验证。"
    ),
    "库存与订单补偿：": (
        "Redis Lua 原子预扣/一人一单，MySQL 条件扣减与唯一索引兜底；200 个客户端任务争 50 库存："
        "50 成功、150 售罄、0 超卖。订单过期、库存/优惠券释放与 Outbox 同事务，2 个并发 Worker 仅产生 1 次迁移和 1 条事件。"
    ),
    "异步投影：": (
        "订单/商品变更与 Outbox 同事务，经 Kafka→Inbox→Elasticsearch 更新搜索投影；"
        "以版本游标和 external_gte 处理重复、乱序及旧事件，异常路径回查 MySQL 权威数据。"
    ),
    "缓存与限流：": (
        "商品详情采用 Caffeine＋Redis 两级缓存，事务提交后删除 Redis 并广播跨实例 L1 失效；"
        "Redis ZSET＋Lua 按 IP、JWT 用户和归一化接口限流，100 并发、额度 20 时放行 20、拒绝 80。"
    ),
    "架构取舍：": (
        "验证 Gateway、Catalog/Search、Trade 拆分形态并引入 Feign、Bulkhead、CircuitBreaker；"
        "同源对照中拆分 P50 由 8.08ms 增至 16.53ms、启动由 17.43s 增至 45.07s，故默认保留模块化单体。"
    ),
}


TRANSACTION = (
    "打通商品事实查询、受控下单和订单状态查询；Agent 下单绑定当前 CandidateScope 并要求明确确认，"
    "Java 回查 JWT，以一次性确认令牌防重复确认、Idempotency-Key 防重复建单；"
    "只读查询经所有权校验后从 MySQL 权威记录确定性返回。"
)


def _replace_bullet(paragraph, label: str, text: str) -> None:
    if len(paragraph.runs) < 2:
        raise RuntimeError(f"bullet has no body run: {label}")
    paragraph.runs[0].text = label
    paragraph.runs[1].text = text
    for run in paragraph.runs[2:]:
        run.text = ""


def build() -> None:
    build_resume_v10.OUTPUT = OUTPUT
    build_resume_v10.build()

    doc = Document(OUTPUT)
    doc.core_properties.title = "袁明珠 - Agent 开发工程师（Java）实习简历 V11"
    doc.core_properties.subject = "岗位定向一页中文简历（最终投递版）"

    found_intro = False
    found = set()
    for paragraph in doc.paragraphs:
        if paragraph.text.startswith("面向多轮商品检索、证据核验与受控交易"):
            paragraph.runs[0].text = INTRO
            for run in paragraph.runs[1:]:
                run.text = ""
            found_intro = True
            continue
        if not paragraph.runs:
            continue
        label = paragraph.runs[0].text
        if label == "交易写边界：":
            _replace_bullet(paragraph, "交易闭环：", TRANSACTION)
            found.add(label)
        elif label in BULLETS:
            _replace_bullet(paragraph, label, BULLETS[label])
            found.add(label)

    expected = set(BULLETS) | {"交易写边界："}
    if not found_intro or found != expected:
        missing = sorted(expected - found)
        raise RuntimeError(f"V11 replacement incomplete: intro={found_intro}, missing={missing}")

    doc.save(OUTPUT)
    print(OUTPUT)


if __name__ == "__main__":
    build()
