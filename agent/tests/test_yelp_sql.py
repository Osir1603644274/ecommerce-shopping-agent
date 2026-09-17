import json
import tempfile
import unittest
from pathlib import Path

from recommendation.yelp_sql import generate_yelp_import_sql, save_yelp_import_sql, sql_string


class YelpSqlTests(unittest.TestCase):
    def test_sql_string_escapes_quotes_and_backslashes(self):
        self.assertEqual(sql_string("Bob's Cafe"), "'Bob''s Cafe'")
        self.assertEqual(sql_string(r"C:\data"), r"'C:\\data'")
        self.assertEqual(sql_string(None), "NULL")

    def test_generate_yelp_import_sql_contains_shop_review_and_behavior(self):
        sample = {
            "shops": [
                {
                    "shopId": 100001,
                    "name": "Bob's Cafe",
                    "typeId": 2,
                    "address": "1 Main St",
                    "avgPrice": 100,
                    "phone": "",
                    "longitude": -75.16,
                    "latitude": 39.95,
                }
            ],
            "reviews": [
                {
                    "id": "yelp-review-a",
                    "shopId": 100001,
                    "text": "Quiet cafe with outlets.",
                    "tags": ["yelp", "咖啡", "stars:5"],
                    "sourceReviewId": "review-a",
                    "sourceUserId": "user-a",
                    "stars": 5,
                    "sourceShopName": "Original Cafe",
                    "reviewEvidenceScope": "source_only",
                    "createdAt": "2024-01-01 10:00:00",
                }
            ],
            "userBehaviors": [
                {
                    "id": "yelp-behavior-review-a",
                    "userId": "demo-user-1",
                    "shopId": 100001,
                    "behaviorType": "rating",
                    "score": 5,
                    "source": "yelp_review",
                    "sourceUserId": "user-a",
                    "sourceReviewId": "review-a",
                    "occurredAt": "2024-01-01 10:00:00",
                }
            ],
        }

        sql = generate_yelp_import_sql(sample)

        self.assertIn("INSERT INTO shop", sql)
        self.assertIn("Bob''s Cafe", sql)
        self.assertIn("INSERT INTO review", sql)
        self.assertIn("source_review_id, source_user_id, stars", sql)
        self.assertIn("'review-a', 'user-a', 5", sql)
        self.assertIn("'[\"yelp\",\"咖啡\",\"stars:5\"]'", sql)
        self.assertIn("INSERT INTO user_behavior", sql)
        self.assertIn("source_user_id, source_review_id", sql)
        self.assertIn("'demo-user-1'", sql)
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)

    def test_save_yelp_import_sql_reads_sample_and_writes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            sample_path = Path(tmp) / "sample.json"
            output_path = Path(tmp) / "yelp_sample.sql"
            sample_path.write_text(
                json.dumps(
                    {
                        "shops": [],
                        "reviews": [],
                        "userBehaviors": [],
                    }
                ),
                encoding="utf-8",
            )

            sql = save_yelp_import_sql(sample_path, output_path)

            self.assertTrue(output_path.exists())
            self.assertEqual(output_path.read_text(encoding="utf-8"), sql)
            self.assertIn("SET NAMES utf8mb4", sql)


if __name__ == "__main__":
    unittest.main()
