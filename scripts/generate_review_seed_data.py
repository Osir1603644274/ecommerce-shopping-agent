from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = ROOT / "agent" / "rag" / "corpus" / "merchant_reviews.json"
MYSQL_REVIEW_SQL_PATH = ROOT / "db" / "review.sql"
H2_DATA_SQL_PATH = ROOT / "backend" / "src" / "test" / "resources" / "data.sql"

TARGET_REVIEWS_PER_SHOP = 30
BASE_REVIEW_COUNT = 45

SHOP_CATEGORY = {
    1: "food",
    2: "food",
    9: "food",
    3: "coffee",
    7: "coffee",
    8: "coffee",
    4: "movie",
    10: "movie",
    11: "movie",
    5: "hotel",
    12: "hotel",
    13: "hotel",
    6: "fitness",
    14: "fitness",
    15: "fitness",
}

EXTRA_REVIEW_TEMPLATES = {
    "food": [
        ("扫码点餐流程清楚，菜品图片和备注说明比较完整，第一次来不容易点错。", ["点餐", "菜单", "体验"]),
        ("服务员加汤和收空盘比较及时，整体节奏稳定，适合日常随便吃一顿。", ["服务", "日常", "稳定"]),
        ("餐具和桌面清理得比较快，翻台后基本没有油渍，卫生感不错。", ["卫生", "桌面", "翻台"]),
        ("调味区选择比较多，酱料、葱花和蒜泥都能自取，口味可以自己调整。", ["调味", "自取", "口味"]),
        ("店门口招牌显眼，从路口走过来很好找，外地朋友集合也不费劲。", ["位置", "招牌", "集合"]),
        ("支持团购券和普通扫码付款，结账速度快，适合不想排长队买单的人。", ["付款", "团购", "结账"]),
        ("菜品整体稳定，偶尔新品会有试吃活动，想尝鲜可以问问店员。", ["新品", "试吃", "稳定"]),
    ],
    "coffee": [
        ("菜单上咖啡豆产地标注清楚，店员能简单介绍风味，适合慢慢挑饮品。", ["咖啡豆", "风味", "饮品"]),
        ("甜品柜更新比较勤，巴斯克和司康经常卖完，想吃甜点最好早点到。", ["甜品", "司康", "早点"]),
        ("杯具和吧台打理得干净，出杯速度稳定，高峰时也不会太混乱。", ["吧台", "出杯", "卫生"]),
        ("外带窗口动线顺畅，早上买一杯带走比较方便。", ["外带", "早上", "动线"]),
        ("饮品甜度可以调整，拿铁和燕麦奶选项都有，口味选择比较灵活。", ["甜度", "拿铁", "燕麦奶"]),
        ("座位设计有一些小装饰，拍照效果不错，但不主打网红打卡。", ["座位", "拍照", "装饰"]),
        ("会员积分能抵扣部分饮品，常来的话性价比会稍微好一些。", ["会员", "积分", "性价比"]),
    ],
    "movie": [
        ("大厅指引牌清楚，第一次来也容易找到对应影厅和检票口。", ["指引", "检票", "影厅"]),
        ("影厅清洁速度比较快，上一场散场后地面基本没有明显垃圾。", ["清洁", "散场", "影厅"]),
        ("爆米花甜度偏高，饮料套餐选择多，观影前买零食比较方便。", ["爆米花", "饮料", "零食"]),
        ("音量整体偏足，动作片氛围感强，怕吵的人可以考虑普通厅。", ["音量", "动作片", "普通厅"]),
        ("候场区座位有限，人多时可能需要站一会儿。", ["候场", "座位", "人多"]),
        ("卫生间离入口近，散场后动线还算顺，不容易迷路。", ["卫生间", "动线", "入口"]),
        ("会员日小食折扣比较划算，常看电影可以顺手注册会员。", ["会员日", "折扣", "小食"]),
    ],
    "hotel": [
        ("入住登记材料要求清楚，前台确认订单速度快，初次入住不麻烦。", ["入住", "前台", "订单"]),
        ("浴室水压稳定，洗漱用品补得及时，短住一两晚比较省心。", ["浴室", "水压", "洗漱用品"]),
        ("电梯和走廊指示清楚，外卖可以放在前台或机器人送到楼层。", ["电梯", "外卖", "机器人"]),
        ("房卡感应灵敏，楼层门禁安全感不错，夜间进出也有人值班。", ["房卡", "门禁", "值班"]),
        ("房间电视和空调操作简单，遥控器反应正常，不需要反复找前台。", ["电视", "空调", "遥控器"]),
        ("周边便利店和小吃选择不少，晚上临时买东西比较方便。", ["便利店", "小吃", "周边"]),
        ("发票开具流程顺，电子发票通常当天能收到，报销比较方便。", ["发票", "报销", "流程"]),
    ],
    "fitness": [
        ("前台登记流程清楚，第一次进馆会说明储物和场地规则。", ["前台", "储物", "规则"]),
        ("饮水区位置明显，运动中补水方便，纸杯补充也比较及时。", ["饮水区", "补水", "纸杯"]),
        ("地面防滑垫维护不错，拉伸区清洁度较好，训练前后都能使用。", ["防滑垫", "拉伸区", "清洁"]),
        ("课程表在小程序里能看到，取消和预约规则写得比较明白。", ["课程表", "小程序", "预约"]),
        ("馆内通风比想象中好，运动后闷热感不明显。", ["通风", "运动", "闷热"]),
        ("会员顾问介绍价格时比较直接，年卡和次卡差异说得清楚。", ["会员", "年卡", "次卡"]),
        ("体脂秤和基础测量设备放在入口旁，想记录变化比较方便。", ["体脂秤", "测量", "记录"]),
    ],
}


EXTRA_REVIEW_VARIANTS = {
    "food": [
        ("", []),
        ("午餐时段上菜更快，适合附近上班族简单聚餐。", ["午餐", "上班族"]),
        ("工作日晚餐人流相对稳定，不像周末那样容易拥挤。", ["工作日", "晚餐"]),
        ("临近打烊前部分菜品可能售罄，晚到最好提前电话确认。", ["打烊", "售罄"]),
    ],
    "coffee": [
        ("", []),
        ("工作日白天座位周转慢，适合短时间处理消息或阅读。", ["工作日", "阅读"]),
        ("傍晚外带订单会变多，堂食区通常还能保持基本安静。", ["傍晚", "外带"]),
        ("如果需要长时间停留，最好避开周末下午的高峰。", ["长时间", "周末"]),
    ],
    "movie": [
        ("", []),
        ("工作日晚场人少一些，取票和入场都比较从容。", ["工作日", "晚场"]),
        ("热门档期零食柜台会排队，建议先取票再买小食。", ["热门档期", "零食"]),
        ("散场后电梯容易拥挤，赶时间可以提前看好楼梯位置。", ["散场", "电梯"]),
    ],
    "hotel": [
        ("", []),
        ("工作日入住价格通常更稳，临时出差不用太担心满房。", ["工作日", "出差"]),
        ("夜间前台响应速度还可以，晚到客人办理入住不太费劲。", ["夜间", "前台"]),
        ("节假日价格波动明显，确定行程后最好提前预订。", ["节假日", "预订"]),
    ],
    "fitness": [
        ("", []),
        ("工作日中午人相对少，想避开晚高峰可以这个时段来。", ["工作日", "午间"]),
        ("晚间团课结束后更衣区会变拥挤，洗澡可能需要排队。", ["晚间", "更衣区"]),
        ("周末上午新会员体验课较多，自由训练区反而比较稳定。", ["周末", "体验课"]),
    ],
}


def sql_quote(value: str) -> str:
    return value.replace("'", "''")


def merge_tags(*tag_groups: list[str]) -> list[str]:
    merged: list[str] = []
    for tags in tag_groups:
        for tag in tags:
            if tag not in merged:
                merged.append(tag)
    return merged


def build_reviews() -> list[dict[str, Any]]:
    source_reviews = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    base_reviews = [
        review
        for review in source_reviews
        if int(str(review["id"]).removeprefix("review-")) <= BASE_REVIEW_COUNT
    ]
    base_reviews.sort(key=lambda item: int(str(item["id"]).removeprefix("review-")))

    reviews_by_shop: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for review in base_reviews:
        reviews_by_shop[int(review["shopId"])].append(review)

    next_review_number = BASE_REVIEW_COUNT + 1
    expanded_reviews = list(base_reviews)
    for shop_id in sorted(reviews_by_shop):
        shop_reviews = reviews_by_shop[shop_id]
        shop_name = shop_reviews[0]["shopName"]
        category = SHOP_CATEGORY[shop_id]
        templates = EXTRA_REVIEW_TEMPLATES[category]
        variants = EXTRA_REVIEW_VARIANTS[category]
        needed = TARGET_REVIEWS_PER_SHOP - len(shop_reviews)
        if needed < 0:
            raise ValueError(f"shop {shop_id} already has too many base reviews")
        max_review_count = len(templates) * len(variants)
        if needed > max_review_count:
            raise ValueError(
                f"shop {shop_id} needs {needed} generated reviews, "
                f"but only {max_review_count} template combinations are available"
            )
        for offset in range(needed):
            text_template, tags = templates[offset % len(templates)]
            detail, detail_tags = variants[offset // len(templates)]
            expanded_reviews.append(
                {
                    "id": f"review-{next_review_number:03d}",
                    "shopId": shop_id,
                    "shopName": shop_name,
                    "text": f"{text_template}{detail}",
                    "tags": merge_tags(tags, detail_tags),
                }
            )
            next_review_number += 1

    return expanded_reviews


def build_review_insert_sql(reviews: list[dict[str, Any]]) -> str:
    rows = []
    for review in reviews:
        tags = json.dumps(review["tags"], ensure_ascii=False, separators=(",", ":"))
        rows.append(
            "    "
            f"('{sql_quote(review['id'])}', "
            f"{int(review['shopId'])}, "
            f"'{sql_quote(review['text'])}', "
            f"'{sql_quote(tags)}')"
        )
    return "INSERT INTO review (id, shop_id, content, tags)\nVALUES\n" + ",\n".join(rows) + ";\n"


def write_corpus(reviews: list[dict[str, Any]]) -> None:
    CORPUS_PATH.write_text(
        json.dumps(reviews, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_mysql_review_sql(reviews: list[dict[str, Any]]) -> None:
    content = (
        f"-- review seed 数据：15 家商户、每家 {TARGET_REVIEWS_PER_SHOP} 条评论，共 {len(reviews)} 条\n"
        "DROP TABLE IF EXISTS review;\n\n"
        "CREATE TABLE review (\n"
        "    id VARCHAR(64) PRIMARY KEY,\n"
        "    shop_id BIGINT NOT NULL,\n"
        "    content TEXT NOT NULL,\n"
        "    tags VARCHAR(255) NOT NULL,\n"
        "    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,\n"
        "    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,\n"
        "    INDEX idx_review_shop_id (shop_id)\n"
        ") CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\n\n"
        f"{build_review_insert_sql(reviews)}"
    )
    MYSQL_REVIEW_SQL_PATH.write_text(content, encoding="utf-8")


def write_h2_data_sql(reviews: list[dict[str, Any]]) -> None:
    existing = H2_DATA_SQL_PATH.read_text(encoding="utf-8")
    prefix = existing.split("DELETE FROM review;", maxsplit=1)[0]
    content = f"{prefix}DELETE FROM review;\n\n{build_review_insert_sql(reviews)}"
    H2_DATA_SQL_PATH.write_text(content, encoding="utf-8")


def main() -> None:
    reviews = build_reviews()
    write_corpus(reviews)
    write_mysql_review_sql(reviews)
    write_h2_data_sql(reviews)
    print(f"已生成 {len(reviews)} 条评论。")


if __name__ == "__main__":
    main()
