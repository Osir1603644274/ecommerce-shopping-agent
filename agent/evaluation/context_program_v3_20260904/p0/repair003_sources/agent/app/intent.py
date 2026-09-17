def looks_like_shop_type_query(message: str) -> bool:
    keywords = ["分类", "类型", "品类", "有哪些", "哪些", "shop type", "category"]
    normalized = message.lower()
    return any(keyword in normalized for keyword in keywords)


# 分类名 -> 后端 shop_type 表里的 id。
# 这是“写死”的映射，先把链路跑通；以后接入大模型后，可以改成模型理解或动态查后端。
SHOP_TYPE_NAME_TO_ID = {
    "美食": 1,
    "咖啡": 2,
    "电影": 3,
    "酒店": 4,
    "健身": 5,
}


def extract_shop_type_keyword(message: str) -> str | None:
    for shop_type in SHOP_TYPE_NAME_TO_ID:
        if shop_type in message:
            return shop_type
    return None


def extract_shop_type_id(message: str) -> int | None:
    for name, type_id in SHOP_TYPE_NAME_TO_ID.items():
        if name in message:
            return type_id
    return None


def looks_like_shop_query(message: str) -> bool:
    normalized = message.lower()
    keywords = [
        "商家",
        "店",
        "附近",
        "火锅",
        "美食",
        "咖啡",
        "电影",
        "酒店",
        "健身",
        "优惠券",
        "评分",
        "shop",
        "restaurant",
        "nearby",
        "hotpot",
        "coffee",
        "coupon",
        "rating",
    ]
    return any(keyword in normalized for keyword in keywords)
