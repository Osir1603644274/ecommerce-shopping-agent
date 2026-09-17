from scripts.seed_product_evaluation_prices import (
    CATEGORY_PRICE_RANGES,
    classify_primary,
    deterministic_price_minor,
)


def test_deterministic_price_is_stable_and_inside_category_range():
    first = deterministic_price_minor(6520546, "headphones")
    second = deterministic_price_minor(6520546, "headphones")
    minimum, maximum = CATEGORY_PRICE_RANGES["headphones"]
    assert first == second
    assert minimum <= first <= maximum


def test_primary_category_filter_excludes_known_pollution():
    assert classify_primary("手机壳配件", "红米手机壳") is None
    assert classify_primary("笔记本/记事本", "学生笔记本") is None
    assert classify_primary("耳机配件", "蓝牙耳机保护套") is None
    assert classify_primary("手机设备", "旗舰智能手机") == "phone"
    assert classify_primary("笔记本电脑", "轻薄笔记本电脑") == "laptop"
    assert classify_primary("耳机/麦克风", "主动降噪蓝牙耳机") == "headphones"
