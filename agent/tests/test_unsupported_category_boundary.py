from agent.app.llm import _explicit_unsupported_product_category


def test_phone_camera_capability_is_not_misclassified_as_camera_category() -> None:
    assert (
        _explicit_unsupported_product_category(
            "预算3000元以内，安卓，推荐三款，并核验游戏表现、散热表现和相机表现"
        )
        is None
    )
    assert _explicit_unsupported_product_category("这款手机相机怎么样") is None


def test_explicit_camera_purchase_remains_unsupported_category() -> None:
    assert _explicit_unsupported_product_category("预算5000元，想买一台相机") == "相机"
    assert _explicit_unsupported_product_category("给我推荐一款微单") == "相机"


def test_other_unsupported_categories_remain_fail_closed() -> None:
    assert _explicit_unsupported_product_category("想买一个智能手表") == "智能手表"
    assert _explicit_unsupported_product_category("推荐电子书阅读器") == "电子书阅读器"
