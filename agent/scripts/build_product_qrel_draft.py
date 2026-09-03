"""Build the deterministic 90-query Chinese qrel-review pool (never human labels)."""

import argparse
import json
from pathlib import Path


CATEGORY_CASES = {
    "phone": {
        "label": "手机",
        "budgets": [2000, 3000, 4000],
        "preferences": [
            ("日常通勤", "至少 8GB 内存", "memory_gb", "gte", 8, "GB"),
            ("多任务办公", "至少 12GB 内存", "memory_gb", "gte", 12, "GB"),
            ("拍照旅行", "至少 256GB 存储", "storage_gb", "gte", 256, "GB"),
            ("长时间户外", "电池至少 5000mAh", "battery_mah", "gte", 5000, "mAh"),
            ("高速网络", "必须支持 5G", "supports_5g", "eq", True, "bool"),
            ("轻度游戏", "至少 12GB 内存", "memory_gb", "gte", 12, "GB"),
            ("给长辈使用", "至少 128GB 存储", "storage_gb", "gte", 128, "GB"),
            ("视频拍摄", "至少 512GB 存储", "storage_gb", "gte", 512, "GB"),
            ("备用机", "至少 8GB 内存", "memory_gb", "gte", 8, "GB"),
            ("移动办公", "电池至少 4500mAh", "battery_mah", "gte", 4500, "mAh"),
        ],
    },
    "laptop": {
        "label": "笔记本",
        "budgets": [5000, 7000, 9000],
        "preferences": [
            ("日常办公", "至少 16GB 内存", "memory_gb", "gte", 16, "GB"),
            ("代码开发", "至少 32GB 内存", "memory_gb", "gte", 32, "GB"),
            ("出差携带", "重量不超过 1.5kg", "weight_kg", "lte", 1.5, "kg"),
            ("资料归档", "至少 1TB 存储", "storage_gb", "gte", 1024, "GB"),
            ("大屏表格", "屏幕至少 15 英寸", "screen_inch", "gte", 15, "inch"),
            ("大学学习", "至少 16GB 内存", "memory_gb", "gte", 16, "GB"),
            ("图像处理", "至少 32GB 内存", "memory_gb", "gte", 32, "GB"),
            ("移动写作", "重量不超过 1.3kg", "weight_kg", "lte", 1.3, "kg"),
            ("本地虚拟机", "至少 1TB 存储", "storage_gb", "gte", 1024, "GB"),
            ("居家网课", "屏幕至少 14 英寸", "screen_inch", "gte", 14, "inch"),
        ],
    },
    "headphones": {
        "label": "耳机",
        "budgets": [500, 1000, 2000],
        "preferences": [
            ("地铁通勤", "必须支持主动降噪", "noise_cancelling", "eq", True, "bool"),
            ("长途旅行", "续航至少 30 小时", "battery_hours", "gte", 30, "hour"),
            ("运动使用", "必须无线连接", "wireless", "eq", True, "bool"),
            ("久戴办公", "重量不超过 250g", "weight_g", "lte", 250, "g"),
            ("开放办公室", "必须支持主动降噪", "noise_cancelling", "eq", True, "bool"),
            ("日常听歌", "续航至少 20 小时", "battery_hours", "gte", 20, "hour"),
            ("手机搭配", "必须无线连接", "wireless", "eq", True, "bool"),
            ("跨城出差", "续航至少 40 小时", "battery_hours", "gte", 40, "hour"),
            ("轻量佩戴", "重量不超过 200g", "weight_g", "lte", 200, "g"),
            ("图书馆学习", "必须支持主动降噪", "noise_cancelling", "eq", True, "bool"),
        ],
    },
}


def build_rows() -> list[dict]:
    rows = []
    for category, config in CATEGORY_CASES.items():
        index = 0
        for budget in config["budgets"]:
            for use_case, text, key, operator, value, unit in config["preferences"]:
                index += 1
                rows.append({
                    "schemaVersion": "product-qrel-v1",
                    "queryId": f"{category}-{index:03d}",
                    "language": "zh-CN",
                    "category": category,
                    "split": "validation" if index <= 20 else "sealed_test",
                    "query": f"{use_case}，预算不超过{budget}元，{text}，请推荐{config['label']}。",
                    "requirements": [
                        {
                            "key": "price_minor", "operator": "lte", "value": budget * 100,
                            "unit": "CNY_MINOR", "priority": "hard",
                            "source": f"用户原话：预算不超过{budget}元",
                        },
                        {
                            "key": key, "operator": operator, "value": value,
                            "unit": unit, "priority": "hard", "source": f"用户原话：{text}",
                        },
                    ],
                    "catalogSource": "KuaiSearch",
                    "reviewStatus": "draft",
                    "judgments": [],
                })
    assert len(rows) == 90
    assert all(
        sum(row["category"] == category and row["split"] == "validation" for row in rows) == 20
        and sum(row["category"] == category and row["split"] == "sealed_test" for row in rows) == 10
        for category in CATEGORY_CASES
    )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path,
        default=Path(__file__).parents[1] / "evaluation/product_qrel_draft.jsonl",
    )
    args = parser.parse_args()
    rows = build_rows()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(args.output), "count": len(rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
