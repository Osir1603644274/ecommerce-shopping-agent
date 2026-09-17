import json
import tempfile
import unittest
from pathlib import Path

from recommendation.yelp import build_yelp_sample, convert_yelp_sample, match_shop_type


class YelpSampleTests(unittest.TestCase):
    def test_match_shop_type_maps_yelp_categories_to_project_types(self):
        self.assertEqual(match_shop_type("Coffee & Tea, Cafes"), {"typeId": 2, "typeName": "咖啡"})
        self.assertEqual(match_shop_type("Restaurants, Chinese"), {"typeId": 1, "typeName": "美食"})
        self.assertEqual(match_shop_type("Gyms, Fitness & Instruction"), {"typeId": 5, "typeName": "健身"})
        self.assertIsNone(match_shop_type("Pet Groomers"))

    def test_build_sample_converts_business_reviews_and_demo_user(self):
        businesses = [
            {
                "business_id": "biz-coffee-1",
                "name": "Morning Coffee",
                "categories": "Coffee & Tea, Cafes",
                "address": "1 Main St",
                "city": "Philadelphia",
                "state": "PA",
                "latitude": 39.95,
                "longitude": -75.16,
                "attributes": {"RestaurantsPriceRange2": "2"},
                "is_open": 1,
            },
            {
                "business_id": "biz-food-1",
                "name": "Noodle House",
                "categories": "Restaurants, Chinese",
                "address": "2 Main St",
                "city": "Philadelphia",
                "state": "PA",
                "latitude": 39.96,
                "longitude": -75.17,
                "attributes": {"RestaurantsPriceRange2": "1"},
                "is_open": 1,
            },
            {
                "business_id": "biz-pet-1",
                "name": "Pet Place",
                "categories": "Pet Groomers",
                "city": "Philadelphia",
                "is_open": 1,
            },
        ]
        reviews = [
            {
                "review_id": "review-a",
                "user_id": "real-user-1",
                "business_id": "biz-coffee-1",
                "stars": 5,
                "text": "Quiet cafe with outlets, great for work.",
                "date": "2024-01-01 10:00:00",
            },
            {
                "review_id": "review-b",
                "user_id": "real-user-1",
                "business_id": "biz-food-1",
                "stars": 4,
                "text": "Good noodles and fast service.",
                "date": "2024-01-02 10:00:00",
            },
            {
                "review_id": "review-c",
                "user_id": "real-user-2",
                "business_id": "biz-pet-1",
                "stars": 5,
                "text": "This business should be filtered out.",
                "date": "2024-01-03 10:00:00",
            },
        ]

        sample = build_yelp_sample(
            businesses,
            reviews,
            city="Philadelphia",
            start_shop_id=100,
            min_demo_user_behaviors=2,
        )

        self.assertEqual([shop["shopId"] for shop in sample["shops"]], [100, 101])
        self.assertEqual(sample["shops"][0]["typeName"], "咖啡")
        self.assertEqual(sample["shops"][0]["avgPrice"], 100)
        self.assertEqual(len(sample["reviews"]), 2)
        self.assertEqual(sample["reviews"][0]["shopId"], 100)
        self.assertEqual(len(sample["userBehaviors"]), 2)
        self.assertEqual({item["userId"] for item in sample["userBehaviors"]}, {"demo-user-1"})
        self.assertEqual(sample["demoUsers"], [{"userId": "demo-user-1", "sourceUserId": "real-user-1", "behaviorCount": 2}])
        self.assertEqual(sample["metadata"]["businessCount"], 2)
        self.assertEqual(sample["metadata"]["reviewCount"], 2)

    def test_build_sample_respects_review_limit_per_shop(self):
        businesses = [
            {
                "business_id": "biz-coffee-1",
                "name": "Morning Coffee",
                "categories": "Coffee & Tea",
                "city": "Philadelphia",
                "latitude": 39.95,
                "longitude": -75.16,
                "is_open": 1,
            }
        ]
        reviews = [
            {
                "review_id": f"review-{index}",
                "user_id": "real-user-1",
                "business_id": "biz-coffee-1",
                "stars": 5,
                "text": f"review text {index}",
                "date": "2024-01-01",
            }
            for index in range(3)
        ]

        sample = build_yelp_sample(
            businesses,
            reviews,
            max_reviews_per_shop=2,
            min_demo_user_behaviors=2,
        )

        self.assertEqual(len(sample["reviews"]), 2)
        self.assertEqual(len(sample["userBehaviors"]), 2)

    def test_convert_yelp_sample_reads_jsonl_and_writes_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw_dir = Path(tmp) / "raw"
            output_path = Path(tmp) / "processed" / "sample.json"
            raw_dir.mkdir()
            business_path = raw_dir / "yelp_academic_dataset_business.json"
            review_path = raw_dir / "yelp_academic_dataset_review.json"
            business_path.write_text(
                json.dumps(
                    {
                        "business_id": "biz-coffee-1",
                        "name": "Morning Coffee",
                        "categories": "Coffee & Tea",
                        "city": "Philadelphia",
                        "latitude": 39.95,
                        "longitude": -75.16,
                        "is_open": 1,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            review_path.write_text(
                json.dumps(
                    {
                        "review_id": "review-a",
                        "user_id": "real-user-1",
                        "business_id": "biz-coffee-1",
                        "stars": 5,
                        "text": "Quiet cafe.",
                        "date": "2024-01-01",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            sample = convert_yelp_sample(
                raw_dir,
                output_path,
                city="Philadelphia",
                min_demo_user_behaviors=1,
            )

            self.assertTrue(output_path.exists())
            written = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(written, sample)
            self.assertEqual(written["metadata"]["demoUserId"], "demo-user-1")


if __name__ == "__main__":
    unittest.main()