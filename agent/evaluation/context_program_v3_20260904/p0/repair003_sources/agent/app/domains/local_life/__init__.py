from ..base import DomainSpec, register_domain
from ..context_skill import CONTEXT_CONTRACT_VERSION, LOCAL_LIFE_CONTEXT_SKILL_ID

LOCAL_LIFE_TOOL_NAMES = (
    "list_shop_types",
    "search_shops",
    "get_shop_detail",
    "recommend_shops",
    "search_shop_reviews",
    "search_knowledge",
    "search_places",
    "get_place_detail",
)

_LOCAL_LIFE_MARKERS = (
    "附近",
    "商户",
    "餐厅",
    "咖啡",
    "火锅",
    "公园",
    "博物馆",
    "景点",
    "评论",
    "推荐吃",
)


def is_local_life_message(message: str) -> bool:
    normalized = message.strip().lower()
    return any(marker in normalized for marker in _LOCAL_LIFE_MARKERS)


LOCAL_LIFE_DOMAIN = register_domain(
    DomainSpec(
        domain_id="local_life",
        task_type="local_life",
        label="本地生活",
        tool_names=LOCAL_LIFE_TOOL_NAMES,
        matches=is_local_life_message,
        context_skill_id=LOCAL_LIFE_CONTEXT_SKILL_ID,
        context_skill_version=CONTEXT_CONTRACT_VERSION,
    )
)

__all__ = [
    "LOCAL_LIFE_DOMAIN",
    "LOCAL_LIFE_TOOL_NAMES",
    "is_local_life_message",
]
