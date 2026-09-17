import unittest

from recommendation.yelp_zh import (
    build_yelp_review_retrieval_text,
    generate_yelp_content_zh_sql,
    select_yelp_reviews_with_translations,
)


class YelpZhTests(unittest.TestCase):
    def test_summarize_yelp_review_to_zh_mentions_work_and_coffee(self):
        retrieval_text = build_yelp_review_retrieval_text(
            {
                "text": "Quiet coffee shop with wifi and outlets for working on a laptop.",
                "shopName": "Bob's Cafe",
                "stars": 5,
            },
            {"name": "Bob's Cafe", "typeName": "咖啡"},
        )

        self.assertIn("WiFi/插座", retrieval_text)
        self.assertIn("咖啡", retrieval_text)
        self.assertIn("舒适度", retrieval_text)

    def test_generate_yelp_content_zh_sql_updates_selected_reviews(self):
        sample = {
            "shops": [{"shopId": 100001, "name": "Bob's Cafe", "typeName": "咖啡"}],
            "reviews": [
                {
                    "id": "yelp-review-a",
                    "shopId": 100001,
                    "shopName": "Bob's Cafe",
                    "text": "Quiet cafe with outlets.",
                    "stars": 5,
                }
            ],
        }

        sql = generate_yelp_content_zh_sql(
            sample,
            limit=1,
            translations={"yelp-review-a": "这是一家安静、有插座的咖啡馆。"},
        )

        self.assertIn("content_zh", sql)
        self.assertIn("这是一家安静、有插座的咖啡馆。", sql)
        self.assertIn("translation_status = 'translated'", sql)
        self.assertIn("yelp-review-a", sql)

    def test_select_yelp_reviews_with_translations_requires_matching_sample(self):
        with self.assertRaises(ValueError):
            select_yelp_reviews_with_translations(
                {"reviews": []},
                {"yelp-missing": "缺失评论的译文"},
                limit=1,
            )


if __name__ == "__main__":
    unittest.main()
