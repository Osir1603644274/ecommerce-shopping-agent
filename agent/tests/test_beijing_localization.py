import unittest

from recommendation.beijing_localization import (
    COORDINATE_SYSTEM,
    DATA_NATURE,
    LOCALIZATION_VERSION,
    REVIEW_EVIDENCE_SCOPE,
    build_projection,
    localize_sample,
)
from recommendation.beijing_localization_quality import (
    audit_beijing_localization,
)


ANCHOR = {
    "id": "beijing-park-test",
    "name": "朝阳公园",
    "district": "朝阳区",
    "location": {
        "longitude": 116.487585,
        "latitude": 39.951219,
        "coordinateSystem": "BD-09",
    },
}


class BeijingLocalizationTests(unittest.TestCase):
    def setUp(self):
        self.items = [
            {
                "itemId": 100001,
                "name": "Original Cafe",
                "typeId": 2,
                "address": "1 Main St, Philadelphia",
                "longitude": -75.1,
                "latitude": 39.9,
            },
            {
                "itemId": 100002,
                "name": "Original Grill",
                "typeId": 1,
                "address": "2 Main St, Philadelphia",
                "longitude": -75.2,
                "latitude": 39.8,
            },
        ]

    def test_projection_is_deterministic_and_preserves_source_lineage(self):
        first = build_projection(self.items, anchors=[ANCHOR])
        second = build_projection(self.items, anchors=[ANCHOR])

        self.assertEqual(first, second)
        self.assertEqual(set(first), {100001, 100002})
        self.assertEqual(first[100001]["originalName"], "Original Cafe")
        self.assertEqual(first[100001]["originalLongitude"], -75.1)
        self.assertEqual(first[100001]["coordinateSystem"], COORDINATE_SYSTEM)
        self.assertEqual(first[100001]["dataNature"], DATA_NATURE)
        self.assertEqual(first[100001]["anchorPlaceId"], ANCHOR["id"])
        self.assertEqual(
            first[100001]["projectionMethod"],
            "deterministic_real_anchor_radial_projection",
        )
        self.assertFalse(first[100001]["realWorldNavigationSupported"])
        self.assertIn("北京市朝阳区朝阳公园周边", first[100001]["address"])
        self.assertTrue(115.0 < first[100001]["longitude"] < 118.0)
        self.assertTrue(39.0 < first[100001]["latitude"] < 41.0)
        self.assertNotEqual(first[100001]["displayName"], first[100002]["displayName"])

    def test_sample_localization_keeps_ids_reviews_and_behavior_edges(self):
        sample = {
            "metadata": {"city": "Philadelphia"},
            "shops": [
                {
                    "shopId": 100001,
                    "sourceBusinessId": "source-1",
                    "name": "Original Cafe",
                    "typeId": 2,
                    "typeName": "Coffee",
                    "address": "1 Main St, Philadelphia",
                    "longitude": -75.1,
                    "latitude": 39.9,
                }
            ],
            "reviews": [
                {
                    "id": "review-1",
                    "shopId": 100001,
                    "shopName": "Original Cafe",
                    "stars": 5,
                    "text": "source review",
                    "tags": ["source-tag"],
                }
            ],
            "userBehaviors": [
                {
                    "id": "behavior-1",
                    "userId": "user-1",
                    "shopId": 100001,
                    "behaviorType": "rating",
                }
            ],
        }
        projection = build_projection(self.items[:1], anchors=[ANCHOR])

        localized = localize_sample(
            sample,
            projection,
            translations={
                "review-1": "Original Cafe 是我在 Philadelphia 常去的咖啡店。"
            },
        )

        shop = localized["shops"][0]
        review = localized["reviews"][0]
        self.assertEqual(shop["shopId"], 100001)
        self.assertEqual(shop["originalName"], "Original Cafe")
        self.assertEqual(shop["localizationVersion"], LOCALIZATION_VERSION)
        self.assertEqual(review["id"], "review-1")
        self.assertEqual(review["shopId"], 100001)
        self.assertEqual(review["shopName"], shop["name"])
        self.assertEqual(review["sourceShopName"], "Original Cafe")
        self.assertEqual(
            review["reviewEvidenceScope"],
            REVIEW_EVIDENCE_SCOPE,
        )
        self.assertEqual(
            review["contentZh"],
            "Original Cafe 是我在 Philadelphia 常去的咖啡店。",
        )
        self.assertIn("source-tag", review["tags"])
        self.assertIn("source_review", review["tags"])
        self.assertEqual(localized["userBehaviors"], sample["userBehaviors"])
        self.assertEqual(localized["metadata"]["city"], "Beijing")
        self.assertEqual(localized["metadata"]["dataNature"], DATA_NATURE)
        self.assertFalse(localized["metadata"]["realWorldNavigationSupported"])

    def test_quality_gate_proves_lineage_geography_and_review_boundary(self):
        sample = {
            "metadata": {"city": "Philadelphia"},
            "shops": [
                {
                    "shopId": 100001,
                    "sourceBusinessId": "source-1",
                    "name": "Original Cafe",
                    "typeId": 2,
                    "typeName": "Coffee",
                    "address": "1 Main St, Philadelphia",
                    "avgPrice": 25,
                    "phone": "",
                    "longitude": -75.1,
                    "latitude": 39.9,
                }
            ],
            "reviews": [
                {
                    "id": "review-1",
                    "shopId": 100001,
                    "shopName": "Original Cafe",
                    "text": "source review",
                    "tags": ["yelp"],
                    "sourceReviewId": "source-review-1",
                    "sourceUserId": "source-user-1",
                    "stars": 5,
                    "createdAt": "2020-01-01 00:00:00",
                }
            ],
            "userBehaviors": [
                {
                    "id": "behavior-1",
                    "sourceUserId": "source-user-1",
                    "userId": "user-1",
                    "shopId": 100001,
                    "behaviorType": "rating",
                    "score": 5,
                    "source": "yelp",
                    "occurredAt": "2020-01-01 00:00:00",
                    "sourceReviewId": "source-review-1",
                }
            ],
        }
        items = {"items": [{**self.items[0], "reviewCount": 1, "avgPrice": 25}]}
        projection = build_projection(items["items"], anchors=[ANCHOR])
        translation = {
            "review-1": "Original Cafe 位于 Philadelphia。",
        }
        localized_sample = localize_sample(
            sample,
            projection,
            translations=translation,
        )
        from recommendation.beijing_localization import (
            localize_recommendation_items,
        )

        localized_items = localize_recommendation_items(items, projection)
        report = audit_beijing_localization(
            source_sample=sample,
            localized_sample=localized_sample,
            source_items=items,
            localized_items=localized_items,
            projection=projection,
            anchors=[ANCHOR],
            expected_translations=translation,
        )

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["summary"]["failedRequiredChecks"], [])
        self.assertTrue(all(check["passed"] for check in report["checks"]))


if __name__ == "__main__":
    unittest.main()
