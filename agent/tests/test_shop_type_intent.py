import unittest

from app.intent import (
    extract_shop_type_id,
    extract_shop_type_keyword,
    looks_like_shop_type_query,
)


class ShopTypeIntentTests(unittest.TestCase):

    def test_detects_shop_type_query(self):
        self.assertTrue(looks_like_shop_type_query("现在有哪些商户分类？"))
        self.assertTrue(looks_like_shop_type_query("支持哪些品类？"))

    def test_ignores_unrelated_query(self):
        self.assertFalse(looks_like_shop_type_query("帮我找附近适合两个人吃的火锅店"))

    def test_extracts_known_shop_type_keyword(self):
        self.assertEqual(extract_shop_type_keyword("有没有咖啡相关分类？"), "咖啡")
        self.assertEqual(extract_shop_type_keyword("我想看看酒店类型"), "酒店")

    def test_returns_none_for_unknown_keyword(self):
        self.assertIsNone(extract_shop_type_keyword("有没有奶茶分类？"))

    def test_extracts_shop_type_id(self):
        self.assertEqual(extract_shop_type_id("附近有什么美食？"), 1)
        self.assertEqual(extract_shop_type_id("想喝咖啡"), 2)

    def test_returns_none_for_unknown_shop_type_id(self):
        self.assertIsNone(extract_shop_type_id("有没有奶茶？"))


if __name__ == "__main__":
    unittest.main()
