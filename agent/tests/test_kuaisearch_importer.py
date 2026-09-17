import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts/import_kuaisearch_products.py"
SPEC = importlib.util.spec_from_file_location("kuaisearch_importer", SCRIPT)
importer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(importer)


def row(item_id, category_l3, title="测试商品"):
    return {
        "item_id": item_id,
        "item_title": title,
        "brand_name": "测试品牌",
        "seller_name": "测试卖家",
        "category_level1_name": "手机/数码/电脑办公",
        "category_level2_name": "数码",
        "category_level3_name": category_l3,
    }


def test_title_price_requires_explicit_cny_evidence():
    assert importer.verified_title_price("旗舰手机 2999") == (None, "unverified")
    assert importer.verified_title_price("旗舰手机 ￥2999") == (299900, "verified")
    assert importer.verified_title_price("旗舰手机 2999元") == (299900, "verified")


def test_import_filter_requires_traceable_catalog_fields():
    valid = row(1, "智能手机")
    assert importer.is_qualified(valid)
    invalid = {**valid, "seller_name": "UNKNOWN"}
    assert not importer.is_qualified(invalid)


def test_stream_selection_caps_each_supported_category():
    rows = [
        row(1, "智能手机"),
        row(2, "智能手机"),
        row(3, "笔记本电脑"),
        row(4, "笔记本电脑"),
        row(5, "蓝牙耳机"),
        row(6, "蓝牙耳机"),
    ]
    selected = importer.choose_rows(rows, target_per_category=1, max_scanned=100)
    assert {key: len(value) for key, value in selected.items()} == {
        "phone": 1, "laptop": 1, "headphones": 1
    }
