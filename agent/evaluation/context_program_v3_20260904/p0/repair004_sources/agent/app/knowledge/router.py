from .merchant_docs import MERCHANT_DOC_SOURCE_NAME
from .policy_docs import POLICY_DOC_SOURCE_NAME
from .reviews import REVIEW_SOURCE_NAME


POLICY_KEYWORDS = (
    "退款",
    "押金",
    "预付款",
    "充值",
    "投诉",
    "消费者权益",
    "隐私",
    "个人信息",
    "平台规则",
    "平台",
    "规则变更",
    "申诉",
    "入驻",
    "材料",
    "资质",
    "审核",
    "违规",
    "处罚",
    "自动化决策",
    "个性化推荐",
)

MERCHANT_KEYWORDS = (
    "地址",
    "电话",
    "营业",
    "几点",
    "关门",
    "开门",
    "评分",
    "评论数",
    "wifi",
    "wi-fi",
    "无线",
    "外带",
    "配送",
    "停车",
    "户外座位",
    "预约",
    "信用卡",
    "价格档位",
    "类别",
    "分类",
)

REVIEW_KEYWORDS = (
    "安静",
    "吵",
    "氛围",
    "环境",
    "服务",
    "排队",
    "拥挤",
    "体验",
    "适合",
    "好不好",
    "舒服",
    "办公",
    "学习",
    "聊天",
    "约会",
    "口味",
    "实际",
)


def _contains_any(query: str, keywords: tuple[str, ...]) -> bool:
    lowered = query.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def route_knowledge_sources(query: str) -> list[str]:
    """Select one or more knowledge sources for an Agent RAG query.

    First version is intentionally rule-based: it is deterministic, easy to
    test, and keeps multi-source behavior explicit before introducing LLM
    routing.
    """

    normalized_query = query.strip()
    if not normalized_query:
        return [REVIEW_SOURCE_NAME]

    sources: list[str] = []
    if _contains_any(normalized_query, POLICY_KEYWORDS):
        sources.append(POLICY_DOC_SOURCE_NAME)
    if _contains_any(normalized_query, MERCHANT_KEYWORDS):
        sources.append(MERCHANT_DOC_SOURCE_NAME)
    if _contains_any(normalized_query, REVIEW_KEYWORDS):
        sources.append(REVIEW_SOURCE_NAME)

    return sources or [REVIEW_SOURCE_NAME]
