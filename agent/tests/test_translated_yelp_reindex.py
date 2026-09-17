import unittest

from scripts.reindex_translated_yelp_reviews import merchant_review_point


class TranslatedYelpReindexTests(unittest.TestCase):
    def test_merchant_review_point_uses_chinese_text_language(self):
        point = merchant_review_point(
            {
                "reviewId": "yelp-001",
                "shopId": 7,
                "shopName": "Example Shop",
                "text": "一个安静的地方。",
                "originalText": "A quiet place.",
                "contentZh": "一个安静的地方。",
                "source": "yelp",
                "language": "zh",
                "sourceLanguage": "en",
                "translationStatus": "translated",
                "tags": [],
            },
            [0.1, 0.2],
        )

        self.assertEqual(point.payload["text"], "一个安静的地方。")
        self.assertEqual(point.payload["language"], "zh")
        self.assertEqual(point.payload["sourceLanguage"], "en")
        self.assertEqual(point.payload["originalText"], "A quiet place.")


if __name__ == "__main__":
    unittest.main()
